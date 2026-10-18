"""LoRA SFT + DPO for both arms on top of mlx_lm.tuner, one adapter per persona.

Every record has an `adapter_name` ("professional", "social", ...). Records are
grouped by it, and each group trains its own adapter in
<adapter-root>/<adapter_name>, starting from the same base model. Every adapter
gets the same batch size and --epochs, so a persona with less data trains for
fewer steps.

mlx-lm's `completions` dataset always applies the chat template, which would
defeat the base arm, and puts loss on a single completion. So we build
(tokens, loss mask) examples ourselves (train/format.py) and reuse mlx-lm's
training loop.

  --data: {adapter_name, type?, prompt?, conversation: [{from, text}]}.
      Loss goes on every "me" message.
  --edits: {adapter_name, type?, prompt?, conversation, draft, final}, where
      `conversation` is everything before the drafted message.

Records with type "test" are held out. If an adapter has any, its test loss is
logged every --eval-every steps (on --eval-batches batches, the same ones each
time), and on the whole test set at the end. Without test records there is no
evaluation. Each edit is used
      twice: SFT on the final, and DPO with the final as chosen and the model draft
      as rejected. See train/dpo.py.

"me" messages and edit finals are run through train/mask.py, so the model learns
to write [DATE] etc. instead of specifics (a no-op on already masked data).
Drafts are left as written. Old messages are dropped so every example fits in
--max-seq-length.

Initial / nightly (from scratch on all conversations, plus any edits so far):

    uv run python -m train.trainer --model models/Qwen3-4B-Base --arm base \\
        --data data/conversations.jsonl --edits data/edits.jsonl --adapter-root adapters

Train now (continue one persona's live adapter on new edits plus replayed older
data, for a fixed number of steps, since an epoch would be a full pass over the replay pool):

    uv run python -m train.trainer --model models/Qwen3-4B-Base --arm base \\
        --data data/conversations.jsonl --edits data/new_edits.jsonl --replay-frac 0.5 \\
        --adapter-names social --resume-root adapters --adapter-root adapters --iters 30

On Veri GPUs instead of this machine (uploads the training text, see train/veri.py;
drop --veri-submit to only write the datasets and configs):

    uv run python -m train.trainer --backend veri --model models/Qwen3-4B-Base \\
        --veri-base-model Qwen/Qwen3-4B-Base --arm base \\
        --data data/conversations.jsonl --edits data/edits.jsonl --adapter-root adapters --veri-submit

then, when the jobs finish, `python -m train.veri fetch` installs them in <adapter-root>.
The worker and desktop app load <adapter-root>/<persona> (default root: adapters/).
"""

import argparse
import json
from collections import defaultdict
from functools import partial
from pathlib import Path

import mlx.core as mx
import mlx.optimizers as optim
from mlx_lm import load
from mlx_lm.tuner.trainer import TrainingArgs, evaluate, train
from mlx_lm.tuner.utils import linear_to_lora_layers, load_adapters
from mlx_lm.utils import save_config

from train import veri
from train.dpo import MixedDataset, iterate_mixed_batches, mixed_loss, preference_logps, preference_report
from train.format import (
    ARMS,
    TYPES,
    Conversation,
    Edit,
    masked,
    masked_edit,
    preference_example,
    reply_example,
    training_examples,
)


def read_jsonl(path: str) -> list[dict]:
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def by_adapter(paths: list[str] | None, parse) -> dict[str, dict[str, list]]:
    """Records from `paths`, grouped by adapter_name, then by type."""
    groups = defaultdict(lambda: {t: [] for t in TYPES})
    for path in paths or []:
        for d in read_jsonl(path):
            # Chat exports from the message pipeline are context, not training data
            # (Discord forbids training on API message content without permission).
            if d.get("usage") == "context_only":
                raise ValueError(f"{path}: context-only message exports cannot be used as training data")
            x = parse(d)
            groups[x.adapter_name][x.type].append(x)
    return dict(groups)


def fitted(examples: list, what: str) -> list:
    kept = [x for x in examples if x is not None]
    if len(kept) < len(examples):
        print(f"{what}: dropped {len(examples) - len(kept)} edits whose reply alone exceeds --max-seq-length")
    return kept


def sft_examples(tokenizer, convs: list[Conversation], args, what: str) -> list:
    out, dropped = [], 0
    for c in convs:
        examples, n = training_examples(tokenizer, masked(c) if args.mask else c, args.arm, args.max_seq_length)
        out += examples
        dropped += n
    if dropped:
        print(f"{what}: dropped {dropped} 'me' messages that alone exceed --max-seq-length")
    return out


def mixed_dataset(tokenizer, model, args, convs: list[Conversation], edits: list[Edit], what: str) -> MixedDataset | None:
    """SFT on conversations + edit finals, DPO on edits whose final differs from the draft."""
    replay = sft_examples(tokenizer, convs, args, what)
    if args.mask:
        edits = [masked_edit(e) for e in edits]
    finals = fitted([reply_example(tokenizer, e.context, e.final, args.arm, args.max_seq_length) for e in edits], what)
    # Compared after masking, so an edit that only filled in placeholders isn't a preference.
    changed = [e for e in edits if e.draft.strip() != e.final.strip()]
    dpo = fitted([preference_example(tokenizer, e, args.arm, args.max_seq_length) for e in changed], what)

    sft = []
    if args.sft_weight > 0:
        if args.replay_frac is None or not (replay and finals):
            pool = replay + finals
            sft = [(pool, args.batch_size)] if pool else []
        else:
            k = round(args.batch_size * args.replay_frac)
            sft = [(pool, n) for pool, n in ((replay, k), (finals, args.batch_size - k)) if n]
    if not sft and not dpo:
        return None

    ref = preference_logps(model, dpo, args.max_seq_length) if dpo else []
    print(f"{what}: {len(convs)} conversations -> {len(replay)} examples, {len(edits)} edits ({len(dpo)} changed -> DPO)")
    return MixedDataset(sft=sft, dpo=dpo, ref=ref, dpo_batch=args.dpo_batch_size, seed=args.seed)


def train_adapter(args, name: str, data: dict, checkpoint: bool) -> bool:
    """Train one adapter. False if it had nothing to train on."""
    print(f"=== {name}")
    mx.random.seed(args.seed)
    model, tokenizer = load(args.model)
    model.freeze()
    resume = Path(args.resume_root) / name if args.resume_root else None
    if resume and (resume / "adapter_config.json").exists():
        load_adapters(model, str(resume))
        with open(resume / "adapter_config.json") as f:
            resumed = json.load(f)
        num_layers, lora_parameters = resumed["num_layers"], resumed["lora_parameters"]
    else:
        if resume:
            print(f"no adapter at {resume}, starting {name} from scratch")
        num_layers = args.num_layers
        lora_parameters = {"rank": args.rank, "scale": 20.0, "dropout": 0.0}
        linear_to_lora_layers(model, num_layers, lora_parameters)

    # Built after the adapter is in place: the model as it is now is the DPO reference.
    train_set = mixed_dataset(tokenizer, model, args, data["train"], data["train_edits"], f"{name} train")
    test_set = mixed_dataset(tokenizer, model, args, data["test"], data["test_edits"], f"{name} test")
    if train_set is None:
        print(f"{name}: nothing to train on, skipped")
        return False

    iters = args.iters or max(1, round(args.epochs * train_set.epoch_batches()))
    print(f"{name}: {iters} steps" + ("" if args.iters else f" ({args.epochs:g} epochs of {train_set.epoch_batches()} batches)"))

    adapter_path = Path(args.adapter_root) / name
    adapter_path.mkdir(parents=True, exist_ok=True)
    # First three keys are what mlx_lm.load(..., adapter_path=...) reads back.
    save_config(
        {
            "fine_tune_type": "lora",
            "num_layers": num_layers,
            "lora_parameters": lora_parameters,
            "model": args.model,
            "arm": args.arm,
            "adapter_name": name,
        },
        adapter_path / "adapter_config.json",
    )

    loss = partial(mixed_loss, beta=args.beta, sft_weight=args.sft_weight, dpo_weight=args.dpo_weight)
    train(
        model=model,
        optimizer=optim.Adam(learning_rate=args.learning_rate),
        train_dataset=train_set,
        val_dataset=test_set,
        args=TrainingArgs(
            batch_size=train_set.batch_size,
            iters=iters,
            # mlx-lm calls it "val"; it is the loss on `test_set`.
            steps_per_eval=args.eval_every if test_set else iters + 1,
            val_batches=args.eval_batches,
            steps_per_save=args.save_every or iters,
            adapter_file=str(adapter_path / "adapters.safetensors"),
            max_seq_length=args.max_seq_length,
            grad_checkpoint=checkpoint,
        ),
        loss=loss,
        iterate_batches=iterate_mixed_batches,
    )

    if test_set:
        full = evaluate(
            model, test_set, test_set.batch_size, -1, args.max_seq_length, loss=loss, iterate_batches=iterate_mixed_batches
        )
        print(f"{name} test loss, whole test set: {full:.3f}")
    for split, ds in (("train", train_set), ("test", test_set)):
        if ds and ds.dpo:
            print(f"DPO {name} {split}: {preference_report(model, ds.dpo, ds.ref, args.max_seq_length, args.beta)}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=("mlx", "veri"), default="mlx", help="train here with mlx-lm, or on Veri GPUs (train/veri.py)")
    ap.add_argument("--model", required=True)
    ap.add_argument("--arm", choices=ARMS, required=True)
    ap.add_argument("--data", nargs="+", help="JSONL of conversations (SFT, the replay pool when resuming, and type 'test' ones for eval)")
    ap.add_argument("--edits", nargs="+", help="JSONL of {adapter_name, type?, prompt?, conversation, draft, final}")
    ap.add_argument("--adapter-root", required=True, help="each adapter is saved to <adapter-root>/<adapter_name>")
    ap.add_argument("--adapter-names", nargs="+", help="only train these adapters (default: every adapter_name in the data)")
    ap.add_argument("--resume-root", help="start each adapter from <resume-root>/<adapter_name> (also the DPO reference)")
    steps = ap.add_mutually_exclusive_group()
    steps.add_argument(
        "--epochs",
        type=float,
        default=2.0,
        help="passes over each adapter's data (its largest pool when mixing), so smaller personas take fewer steps",
    )
    steps.add_argument("--iters", type=int, help="a fixed number of steps per adapter instead of --epochs (e.g. Train now)")
    ap.add_argument("--batch-size", type=int, default=4, help="SFT rows per step")
    ap.add_argument("--dpo-batch-size", type=int, default=2, help="edit pairs per step (2 rows each)")
    ap.add_argument("--beta", type=float, default=0.1)
    ap.add_argument("--sft-weight", type=float, default=1.0)
    ap.add_argument("--dpo-weight", type=float, default=1.0)
    ap.add_argument(
        "--replay-frac",
        type=float,
        help="fraction of each SFT batch drawn from --data, the rest from edit finals "
        "(default: one pool, sampled uniformly)",
    )
    ap.add_argument("--eval-every", type=int, default=50, help="steps between test evals (only with type 'test' records)")
    ap.add_argument("--eval-batches", type=int, default=10, help="test batches per periodic eval (the whole test set is evaluated at the end)")
    ap.add_argument("--save-every", type=int, help="also save the adapter every N steps, so a stopped run keeps its latest checkpoint (default: only at the end)")
    ap.add_argument("--learning-rate", type=float, default=1e-5)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--num-layers", type=int, default=16)
    ap.add_argument("--max-seq-length", type=int, default=2048)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument(
        "--no-mask",
        dest="mask",
        action="store_false",
        help="train on 'me' messages and edit finals as given, without train/mask.py placeholders",
    )
    v = ap.add_argument_group("veri (with --backend veri)")
    v.add_argument("--veri-base-model", help="Hugging Face id of --model (needed when --model is a local directory)")
    v.add_argument("--veri-stages", nargs="+", choices=("sft", "dpo"), default=["sft"], help="which jobs to prepare/submit (default: SFT only)")
    v.add_argument("--veri-dpo-base", help="model the DPO job starts from, i.e. the SFT result (default: --veri-base-model)")
    v.add_argument("--veri-gpu-type", default="A100-80GB")
    v.add_argument("--veri-gpu-count", type=int, default=1)
    v.add_argument("--veri-provider", default="vast", help="vast, aws or digitalocean (aws only rents A100/H100 as whole 8-GPU nodes)")
    v.add_argument("--veri-submit", action="store_true", help="upload the datasets and submit the jobs (bills GPU time)")
    args = ap.parse_args()
    if not (args.data or args.edits):
        ap.error("need --data and/or --edits")

    convs, edits = by_adapter(args.data, Conversation.from_dict), by_adapter(args.edits, Edit.from_dict)
    empty = {t: [] for t in TYPES}
    # An adapter with only test records has nothing to train.
    names = sorted(n for n in set(convs) | set(edits) if convs.get(n, empty)["train"] or edits.get(n, empty)["train"])
    if args.adapter_names:
        if missing := set(args.adapter_names) - set(names):
            ap.error(f"no training data for adapter(s): {', '.join(sorted(missing))}")
        names = args.adapter_names
    print(f"adapters: {', '.join(names)}")

    if args.backend == "veri":
        from transformers import AutoTokenizer

        veri.run(args, names, convs, edits, AutoTokenizer.from_pretrained(args.model))
        return

    # mlx-lm turns on gradient checkpointing by patching the layer class, which every
    # reloaded model shares, so it is only asked for once (again would nest it).
    checkpointed = False
    for name in names:
        c, e = convs.get(name, empty), edits.get(name, empty)
        data = {"train": c["train"], "test": c["test"], "train_edits": e["train"], "test_edits": e["test"]}
        checkpointed |= train_adapter(args, name, data, checkpoint=not checkpointed)
        mx.clear_cache()


if __name__ == "__main__":
    import sys

    from gmail_agent.locking import process_lock
    from gmail_agent.runtime import default_paths

    # Veri trains remotely, so it needn't wait for a local worker to release the GPU.
    if "veri" in sys.argv[sys.argv.index("--backend") + 1 : sys.argv.index("--backend") + 2] if "--backend" in sys.argv else False:
        main()
    else:
        with process_lock(default_paths()["model_lock"]):
            main()
