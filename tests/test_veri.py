import json
from argparse import Namespace

from train import veri
from train.format import ME, THEM, Conversation, Edit, Message

from test_format import EOS, CharTokenizer


def conv(*messages, prompt=""):
    return Conversation("social", [Message(s, t) for s, t in messages], prompt)


def test_sft_rows_one_per_me_message():
    c = conv((ME, "dinner?"), (THEM, "yes"), (ME, "great"), prompt="ask about dinner")
    rows = veri.sft_rows(CharTokenizer(), [c], [], "base", None, "t")
    assert rows == [
        {"prompt": "Request: ask about dinner\n\nMe:", "completion": f" dinner?{EOS}"},
        {"prompt": f"Request: ask about dinner\n\nMe: dinner?{EOS}\n\nThem: yes\n\nMe:", "completion": f" great{EOS}"},
    ]


def test_sft_rows_include_edit_finals_and_drop_what_does_not_fit():
    e = Edit(conv((THEM, "free tonight?")), draft="no", final="yes!")
    long = conv((ME, "x" * 50))
    rows = veri.sft_rows(CharTokenizer(), [long], [e], "base", 32, "t")
    assert rows == [{"prompt": "Them: free tonight?\n\nMe:", "completion": f" yes!{EOS}"}]


def test_dpo_rows_skip_unchanged_edits():
    ctx = conv((THEM, "free tonight?"))
    edits = [Edit(ctx, draft="no", final="yes!"), Edit(ctx, draft="same", final=" same ")]
    assert veri.dpo_rows(CharTokenizer(), edits, "base", None, "t") == [
        {"prompt": "Them: free tonight?\n\nMe:", "chosen": f" yes!{EOS}", "rejected": f" no{EOS}"}
    ]


def args(**kw):
    return Namespace(
        **{
            "arm": "base",
            "model": "Qwen/Qwen3-1.7B-Base",
            "veri_base_model": None,
            "veri_dpo_base": None,
            "veri_stages": ["sft", "dpo"],
            "veri_gpu_type": "A100-80GB",
            "veri_gpu_count": 1,
            "veri_provider": "vast",
            "veri_submit": False,
            "resume_root": None,
            "replay_frac": None,
            "iters": None,
            "epochs": 2.0,
            "learning_rate": 1e-5,
            "batch_size": 4,
            "dpo_batch_size": 2,
            "rank": 16,
            "beta": 0.1,
            "max_seq_length": 2048,
            "sft_weight": 1.0,
            "dpo_weight": 1.0,
            "mask": True,
        }
        | kw
    )


def test_job_config_matches_mlx_lora_scale():
    assert "num_epochs = 2\n" in veri._toml(veri.job_config(args(), "social", "sft_text", "q", 10))
    assert "max_steps = 4\n" in veri._toml(veri.job_config(args(epochs=1.5), "social", "sft_text", "q", 10))
    toml = veri._toml(veri.job_config(args(iters=30), "social", "dpo", "Qwen/Qwen3-1.7B-Base", 10))
    assert 'kind = "train"' in toml
    assert "[method]\ntype = \"dpo\"" in toml
    assert "max_steps = 30" in toml and "num_epochs" not in toml
    assert "lora_alpha = 320" in toml and "beta = 0.1" in toml
    assert '[provider]\nname = "vast"' in toml


def test_run_writes_files_without_submitting(tmp_path, monkeypatch):
    monkeypatch.setattr(veri, "submit", lambda *a: (_ for _ in ()).throw(AssertionError("submitted")))
    c = conv((THEM, "call me at 555-123-4567"), (ME, "ok, see you on 3/14"))
    test = Conversation("social", [Message(ME, "held out")], type="test")
    e = Edit(conv((THEM, "free tonight?")), draft="no", final="yes!")
    convs = {"social": {"train": [c], "test": [test]}}
    edits = {"social": {"train": [e], "test": []}}
    veri.run(args(adapter_root=str(tmp_path)), ["social"], convs, edits, CharTokenizer())

    out = tmp_path / "social" / "veri"
    sft = [json.loads(l) for l in (out / "sft.jsonl").read_text().splitlines()]
    assert [r["completion"] for r in sft] == [f" ok, see you on [DATE]{EOS}", f" yes!{EOS}"]
    assert all("held out" not in r["prompt"] + r["completion"] for r in sft)
    assert (out / "dpo.jsonl").exists() and (out / "sft.toml").exists() and (out / "dpo.toml").exists()
    assert not (out / "jobs.json").exists()


def test_run_holds_dpo_until_sft_is_done(tmp_path, monkeypatch):
    submitted = []
    monkeypatch.setattr(veri.shutil, "which", lambda _: "/bin/veri")
    monkeypatch.setattr(veri, "submit", lambda config, data, name: submitted.append(name) or f"job-{len(submitted)}")
    e = Edit(conv((THEM, "free tonight?")), draft="no", final="yes!")
    edits = {"social": {"train": [e], "test": []}}
    veri.run(args(adapter_root=str(tmp_path), veri_submit=True), ["social"], {}, edits, CharTokenizer())
    assert submitted == ["base-social-sft"]

    veri.run(
        args(adapter_root=str(tmp_path), veri_submit=True, veri_stages=["dpo"], veri_dpo_base="me/sft"),
        ["social"], {}, edits, CharTokenizer(),
    )
    assert submitted == ["base-social-sft", "base-social-dpo"]
    assert 'base = "me/sft"' in (tmp_path / "social" / "veri" / "dpo.toml").read_text()
    jobs = json.loads((tmp_path / "social" / "veri" / "jobs.json").read_text())
    assert jobs == {"sft": "job-1", "dpo": "job-2"}


def test_submit_uploads_then_creates_on_the_dataset_id(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return Namespace(returncode=0, stdout=f"id{len(calls)}\n", stderr="")

    monkeypatch.setattr(veri.subprocess, "run", fake_run)
    assert veri.submit("sft.toml", "sft.jsonl", "base-social-sft") == "id2"
    assert calls == [
        ["veri", "datasets", "upload", "sft.jsonl", "--name", "base-social-sft", "--quiet"],
        ["veri", "jobs", "create", "sft.toml", "--dataset", "id1", "--quiet"],
    ]
