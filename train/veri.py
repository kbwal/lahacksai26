"""Train an adapter on rented GPUs with Veri (https://veri.studio) instead of mlx-lm.

Opt-in with `train.trainer --backend veri`. This uploads the training text to Veri,
so it breaks the "nothing leaves the device" promise in PROJECT.md: use it on
Enron or other data you're allowed to send off the machine, not a real mailbox.

Veri runs TRL's SFTTrainer and DPOTrainer as separate jobs, so each adapter gets up
to two JSONL datasets and two configs in <adapter-root>/<adapter_name>/veri/:

  sft.jsonl / sft.toml  {prompt, completion}: one row per "me" message (with as much
                        history as fits) and one per edit final
  dpo.jsonl / dpo.toml  {prompt, chosen, rejected}: edit final vs. model draft, for
                        edits whose final differs from the draft

Rows are rendered by train/format.py and decoded back to text, so both arms keep
their own layout: TRL applies no chat template to string rows, puts loss on the
completion only, and the completion already ends in the token generation stops
on. Test records are never uploaded (Veri's SFT/DPO jobs have no eval set).

Without --veri-submit nothing leaves the machine: the files are written and the
commands to price and submit them are printed. With it, the `veri` CLI (`veri login`
first) uploads the datasets and submits the jobs; their ids go in jobs.json.

DPO has to start from the SFT result, which doesn't exist until the SFT job ends.
So when a run has both, only SFT is submitted. Once it finishes, run again with
`--veri-stages dpo --veri-dpo-base <SFT result>` (e.g. a Hugging Face repo the SFT
job pushed its merged model to).
"""

import json
import os
import shutil
import subprocess
import tomllib
from dataclasses import replace
from pathlib import Path

from train.format import Conversation, Edit, ME, masked, masked_edit, preference_example, reply_example

# mlx-lm's LoRA adds scale * x @ A @ B, PEFT adds (alpha / rank) * x @ A @ B.
MLX_LORA_SCALE = 20


def _split(tokenizer, example) -> tuple[str, str]:
    """(prompt, completion) text of a (tokens, mask) example whose mask is 0s then 1s."""
    tokens, mask = example
    i = mask.index(1)
    return tokenizer.decode(tokens[:i]), tokenizer.decode(tokens[i:])


def _dropped(n: int, what: str, why: str):
    if n:
        print(f"{what}: dropped {n} {why} that alone exceed --max-seq-length")


def sft_rows(tokenizer, convs: list[Conversation], edits: list[Edit], arm: str, max_len: int, what: str) -> list[dict]:
    """One {prompt, completion} row per "me" message in `convs` and per edit final.

    `convs` and `edits` should already be masked."""
    examples, dropped = [], 0
    for c in convs:
        for i, m in enumerate(c.messages):
            if m.sender == ME:
                ex = reply_example(tokenizer, replace(c, messages=c.messages[:i]), m.text, arm, max_len)
                dropped += ex is None
                examples.append(ex)
    _dropped(dropped, what, "'me' messages")
    finals = [reply_example(tokenizer, e.context, e.final, arm, max_len) for e in edits]
    _dropped(sum(x is None for x in finals), what, "edit finals")
    return [dict(zip(("prompt", "completion"), _split(tokenizer, x))) for x in examples + finals if x is not None]


def dpo_rows(tokenizer, edits: list[Edit], arm: str, max_len: int, what: str) -> list[dict]:
    """One {prompt, chosen, rejected} row per edit whose (masked) final differs from its draft."""
    changed = [e for e in edits if e.draft.strip() != e.final.strip()]
    pairs = [preference_example(tokenizer, e, arm, max_len) for e in changed]
    _dropped(sum(p is None for p in pairs), what, "edits")
    rows = []
    for p in pairs:
        if p is not None:
            prompt, chosen = _split(tokenizer, p[0])
            rows.append({"prompt": prompt, "chosen": chosen, "rejected": _split(tokenizer, p[1])[1]})
    return rows


def _toml(config: dict) -> str:
    """`config` (top-level scalars, then one level of tables) as TOML."""
    value = lambda v: str(v).lower() if isinstance(v, bool) else json.dumps(v)
    lines = [f"{k} = {value(v)}" for k, v in config.items() if not isinstance(v, dict)]
    for table, items in config.items():
        if isinstance(items, dict):
            lines += ["", f"[{table}]"] + [f"{k} = {value(v)}" for k, v in items.items() if v is not None]
    return "\n".join(lines) + "\n"


def dataset_name(args, name: str, method: str) -> str:
    return f"{args.arm}-{name}-{method.split('_')[0]}"


def job_config(args, name: str, method: str, base: str, rows: int) -> dict:
    """A `kind = "train"` config for one adapter. [dataset] names the dataset it uploads as;
    the submit passes the uploaded id instead."""
    batch = args.batch_size if method == "sft_text" else args.dpo_batch_size
    if args.iters:
        steps = {"max_steps": args.iters}
    elif args.epochs == int(args.epochs):
        steps = {"num_epochs": int(args.epochs)}  # Veri wants an integer
    else:
        steps = {"max_steps": max(1, round(args.epochs * -(-rows // batch)))}
    hyper = {
        "type": method,
        "learning_rate": args.learning_rate,
        **steps,
        "micro_batch_size": batch,
        "lora_rank": args.rank,
        "lora_alpha": MLX_LORA_SCALE * args.rank,
    }
    if method == "sft_text":
        hyper["max_seq_length"] = args.max_seq_length
    else:
        hyper |= {"beta": args.beta, "max_length": args.max_seq_length}
    return {
        "kind": "train",
        "job": {"output_model": dataset_name(args, name, method)},
        "model": {"base": base},
        "dataset": {"id": dataset_name(args, name, method)},
        "method": hyper,
        "resources": {"gpu_type": args.veri_gpu_type, "gpu_count": args.veri_gpu_count},
        **({"provider": {"name": args.veri_provider}} if args.veri_provider else {}),
    }


def api_key() -> str:
    """$VERI_API_KEY, else the key `veri login` saved. Never print it."""
    if key := os.environ.get("VERI_API_KEY"):
        return key
    out = subprocess.run(["veri", "whoami", "--format", "json"], capture_output=True, text=True)
    path = json.loads(out.stdout)["config_path"] if out.returncode == 0 else None
    if not path:
        raise SystemExit("no Veri API key: run `veri login` or set VERI_API_KEY")
    config = tomllib.loads(open(path).read())
    return config["profiles"][config.get("active_profile", "default")]["api_key"]


def _veri(*argv: str) -> str:
    """Run a `veri` command that creates something, and return its id."""
    out = subprocess.run(["veri", *argv, "--quiet"], capture_output=True, text=True)
    if out.returncode:
        raise RuntimeError(f"veri {' '.join(argv)} failed:\n{out.stderr or out.stdout}")
    return out.stdout.strip()


def submit(config: Path, data: Path, dataset_name: str) -> str:
    """Upload `data`, submit `config` on it, and return the job id."""
    dataset = _veri("datasets", "upload", str(data), "--name", dataset_name)  # checks the rows first
    return _veri("jobs", "create", str(config), "--dataset", dataset)


def run(args, names: list[str], convs: dict, edits: dict, tokenizer):
    if args.resume_root or args.replay_frac is not None:
        raise SystemExit("--backend veri trains from scratch: --resume-root and --replay-frac are mlx only")
    if Path(args.model).is_dir() and not args.veri_base_model:
        raise SystemExit("--backend veri needs --veri-base-model (the Hugging Face id of --model, e.g. Qwen/Qwen3-1.7B-Base)")
    if args.veri_submit and not shutil.which("veri"):
        raise SystemExit("--veri-submit needs the veri CLI: uv tool install 'veri-sdk[mcp]', then veri login")
    base = args.veri_base_model or args.model
    empty = {"train": [], "test": []}

    for name in names:
        c, e = convs.get(name, empty)["train"], edits.get(name, empty)["train"]
        if args.mask:
            c, e = [masked(x) for x in c], [masked_edit(x) for x in e]
        what = f"{name} veri"
        sft = sft_rows(tokenizer, c, e, args.arm, args.max_seq_length, what) if args.sft_weight > 0 else []
        dpo = dpo_rows(tokenizer, e, args.arm, args.max_seq_length, what) if args.dpo_weight > 0 else []
        held_out = len(convs.get(name, empty)["test"]) + len(edits.get(name, empty)["test"])
        print(f"{what}: {len(sft)} SFT rows, {len(dpo)} DPO rows ({held_out} test records stay local)")

        out = Path(args.adapter_root) / name / "veri"
        out.mkdir(parents=True, exist_ok=True)
        stages = [(s, rows) for s, rows in (("sft", sft), ("dpo", dpo)) if rows and s in args.veri_stages]
        jobs = {}
        for stage, rows in stages:
            method, model = ("sft_text", base) if stage == "sft" else ("dpo", args.veri_dpo_base or base)
            data, config = out / f"{stage}.jsonl", out / f"{stage}.toml"
            data.write_text("".join(json.dumps(r) + "\n" for r in rows))
            config.write_text(_toml(job_config(args, name, method, model, len(rows))))
            if not args.veri_submit:
                print(f"  {name} {stage}: veri jobs estimate {config}")
            elif stage == "dpo" and "sft" in jobs:
                print(f"  {name} dpo: not submitted yet. When SFT job {jobs['sft']} finishes, rerun with --veri-stages dpo --veri-dpo-base <its result>")
            else:
                jobs[stage] = submit(config, data, dataset_name(args, name, method))
                print(f"  {name} {stage}: job {jobs[stage]} (veri jobs logs {jobs[stage]} -f)")
        if jobs:
            path = out / "jobs.json"
            old = json.loads(path.read_text()) if path.exists() else {}
            path.write_text(json.dumps(old | jobs, indent=2) + "\n")
