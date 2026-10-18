import json
from argparse import Namespace
from pathlib import Path

import pytest

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


def test_fetch_installs_finished_lora_beside_its_veri_files(tmp_path, monkeypatch):
    import mlx.core as mx

    monkeypatch.setenv("LAHACKS_MAIL_HOME", str(tmp_path))
    root = tmp_path / "adapters"
    jobs = root / "social" / "veri" / "jobs.json"
    jobs.parent.mkdir(parents=True)
    jobs.write_text(json.dumps({"sft": "job-1"}))
    (root / "professional" / "veri").mkdir(parents=True)
    (root / "professional" / "veri" / "jobs.json").write_text(json.dumps({"sft": "job-2"}))

    def download(*argv):
        assert argv[:3] == ("jobs", "download", "job-1")
        peft = Path(argv[4]) / "job-1" / "final"
        peft.mkdir(parents=True)
        (peft / "adapter_config.json").write_text(json.dumps({
            "r": 4, "lora_alpha": 8, "base_model_name_or_path": "Qwen/Qwen3-4B-Base",
        }))
        key = "base_model.model.model.layers.0.self_attn.q_proj.lora_{}.weight"
        mx.save_safetensors(str(peft / "adapter_model.safetensors"), {
            key.format("A"): mx.zeros((4, 8)), key.format("B"): mx.zeros((8, 4)),
        })
        return ""

    monkeypatch.setattr(veri.shutil, "which", lambda _: "/bin/veri")
    monkeypatch.setattr(veri, "job_status", lambda job: "completed" if job == "job-1" else "running")
    monkeypatch.setattr(veri, "_veri", download)
    assert veri.fetch(root, "models/Qwen3-4B-Base", "base") == ["social"]

    config = json.loads((root / "social" / "adapter_config.json").read_text())
    assert (config["arm"], config["model"], config["adapter_name"]) == ("base", "models/Qwen3-4B-Base", "social")
    assert config["lora_parameters"]["keys"] == ["model.layers.0.self_attn.q_proj"]
    assert (root / "social" / "adapters.safetensors").is_file()
    assert jobs.is_file() and not list((root / "social").glob(".incoming-*"))
    assert not (root / "professional" / "adapters.safetensors").exists()


def test_new_sft_submission_clears_the_previous_dpo_job(tmp_path, monkeypatch):
    jobs = tmp_path / "social" / "veri" / "jobs.json"
    jobs.parent.mkdir(parents=True)
    jobs.write_text(json.dumps({"sft": "old-sft", "dpo": "old-dpo"}))
    monkeypatch.setattr(veri.shutil, "which", lambda _: "/bin/veri")
    monkeypatch.setattr(veri, "submit", lambda *a: "new-sft")
    c = conv((THEM, "Hi"), (ME, "Hello"))
    veri.run(
        args(adapter_root=str(tmp_path), veri_submit=True), ["social"],
        {"social": {"train": [c], "test": []}}, {}, CharTokenizer(),
    )
    assert json.loads(jobs.read_text()) == {"sft": "new-sft"}


@pytest.mark.parametrize("dpo_status", ["running", "failed", "completed"])
def test_fetch_uses_the_latest_completed_stage(tmp_path, monkeypatch, dpo_status):
    jobs = tmp_path / "social" / "veri" / "jobs.json"
    jobs.parent.mkdir(parents=True)
    jobs.write_text(json.dumps({"sft": "sft-job", "dpo": "dpo-job"}))
    monkeypatch.setattr(veri.shutil, "which", lambda _: "/bin/veri")
    monkeypatch.setattr(veri, "job_status", lambda job: dpo_status if job == "dpo-job" else "completed")
    downloads, installed = [], []
    monkeypatch.setattr(veri, "_veri", lambda *a: downloads.append(a))
    monkeypatch.setattr(veri, "peft_dir", lambda p: p)
    monkeypatch.setattr(veri, "install", lambda *a: installed.append(a))
    assert veri.fetch(tmp_path, "models/Qwen3-4B-Base", "base") == ["social"]
    expected = "dpo-job" if dpo_status == "completed" else "sft-job"
    assert downloads[0][2] == expected
    assert installed[0][0] == jobs.parent / "download" / expected


def test_install_excludes_model_readers_through_both_file_replacements(tmp_path, monkeypatch):
    from gmail_agent.locking import AlreadyRunning, process_lock
    from train import peft_to_mlx

    lock = tmp_path / "model.lock"
    target = tmp_path / "adapters" / "social"
    target.mkdir(parents=True)
    (target / "adapters.safetensors").write_text("old weights")
    (target / "adapter_config.json").write_text("old config")

    def convert(peft, out, *args):
        with pytest.raises(AlreadyRunning), process_lock(lock):
            pass
        (out / "adapters.safetensors").write_text("new weights")
        (out / "adapter_config.json").write_text("new config")

    replacements = []
    replace = veri.os.replace

    def locked_replace(src, dest):
        with pytest.raises(AlreadyRunning), process_lock(lock):
            pass
        replacements.append(dest.name)
        return replace(src, dest)

    monkeypatch.setattr(peft_to_mlx, "convert", convert)
    monkeypatch.setattr(veri.os, "replace", locked_replace)
    with process_lock(lock), pytest.raises(AlreadyRunning):
        veri.install(tmp_path, target, "model", "base", "social", model_lock=lock)
    assert (target / "adapters.safetensors").read_text() == "old weights"
    assert not replacements
    veri.install(tmp_path, target, "model", "base", "social", model_lock=lock)
    assert replacements == ["adapters.safetensors", "adapter_config.json"]
    assert (target / "adapter_config.json").read_text() == "new config"
    with process_lock(lock):
        pass


def test_install_rejects_dpo_on_a_different_base_without_replacing_live_adapter(tmp_path):
    peft = tmp_path / "peft"
    peft.mkdir()
    (peft / "adapter_config.json").write_text(json.dumps({
        "base_model_name_or_path": "team/merged-sft", "r": 4,
    }))
    target = tmp_path / "adapters" / "social"
    target.mkdir(parents=True)
    (target / "adapters.safetensors").write_text("old weights")
    (target / "adapter_config.json").write_text("old config")
    with pytest.raises(ValueError, match="merged-sft"):
        veri.install(peft, target, "models/Qwen3-4B-Base", "base", "social",
                     model_lock=tmp_path / "model.lock")
    assert (target / "adapters.safetensors").read_text() == "old weights"
    assert (target / "adapter_config.json").read_text() == "old config"
    assert not list(target.glob(".incoming-*"))
