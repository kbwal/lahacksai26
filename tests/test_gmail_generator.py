import runpy
import sys
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

from gmail_agent.locking import AlreadyRunning, process_lock
from gmail_agent.replies import LocalGenerator
from gmail_agent.runtime import default_paths


@pytest.fixture
def model_runtime(tmp_path, monkeypatch):
    lock = tmp_path / "model.lock"
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}")
    (model_dir / "weights.safetensors").write_bytes(b"synthetic")
    loaded = []
    cleared = []

    class Model:
        pass

    def load(*_args, **_kwargs):
        # Check the actual OS lock rather than just recording a mock call.
        with pytest.raises(AlreadyRunning), process_lock(lock):
            pass
        model = Model()
        loaded.append(weakref.ref(model))
        return model, object()

    def clear_cache():
        assert all(model() is None for model in loaded)
        with pytest.raises(AlreadyRunning), process_lock(lock):
            pass
        cleared.append(True)

    core = SimpleNamespace(clear_cache=clear_cache)
    monkeypatch.setitem(sys.modules, "mlx", SimpleNamespace(core=core))
    monkeypatch.setitem(sys.modules, "mlx.core", core)
    monkeypatch.setitem(sys.modules, "mlx_lm", SimpleNamespace(load=load))
    draft_module = SimpleNamespace(draft=lambda *_: "Generated reply")
    monkeypatch.setitem(sys.modules, "train.draft", draft_module)
    generator = LocalGenerator(model_dir, model_lock=lock)
    yield generator, loaded, cleared, draft_module
    generator.close()


def test_generator_releases_weights_and_lock_and_reloads_new_adapter(model_runtime):
    generator, loaded, cleared, _ = model_runtime
    context = {
        "recipient": "friend@example.com",
        "thread": "Hello",
        "conversation": [{"from": "them", "text": "Hello"}],
    }
    generator.generate(context)
    generator.generate(context)
    assert len(loaded) == 1
    version = generator.version
    generator.close()
    assert loaded[0]() is None
    assert len(cleared) == 1
    with process_lock(generator.model_lock):
        (generator.model_path / "weights.safetensors").write_bytes(b"updated checkpoint")
    generator.generate(context)
    assert len(loaded) == 2
    assert generator.version != version
    generator.close()
    assert len(cleared) == 2


def test_generation_error_can_release_lock_and_retry(model_runtime):
    generator, loaded, cleared, module = model_runtime

    def fail(*_):
        raise RuntimeError("inference failed")

    module.draft = fail
    with pytest.raises(RuntimeError):
        generator.generate(
            {
                "recipient": "friend@example.com",
                "conversation": [{"from": "them", "text": "Hello"}],
            }
        )
    generator.close()
    assert loaded[0]() is None
    assert cleared
    with process_lock(generator.model_lock):
        pass


@pytest.mark.parametrize("command", ["draft", "trainer"])
@pytest.mark.parametrize("isolated_home", [False, True])
def test_training_commands_obey_shared_lock(tmp_path, monkeypatch, command, isolated_home):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    if isolated_home:
        monkeypatch.setenv("LAHACKS_MAIL_HOME", str(tmp_path / "isolated"))
    else:
        monkeypatch.delenv("LAHACKS_MAIL_HOME", raising=False)
    lock = default_paths()["model_lock"]
    called = []

    def load(*_args, **_kwargs):
        with pytest.raises(AlreadyRunning), process_lock(lock):
            pass
        called.append(True)
        # Stop before training: the integration under test is acquisition/release.
        raise ValueError("stop after checking lock")

    core = SimpleNamespace(random=SimpleNamespace(seed=lambda _: None))
    optim = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "mlx", SimpleNamespace(core=core, optimizers=optim))
    monkeypatch.setitem(sys.modules, "mlx.core", core)
    monkeypatch.setitem(sys.modules, "mlx.optimizers", optim)
    monkeypatch.setitem(sys.modules, "mlx_lm", SimpleNamespace(load=load, generate=lambda: None))
    monkeypatch.setitem(
        sys.modules, "mlx_lm.tuner.trainer", SimpleNamespace(TrainingArgs=None, train=None)
    )
    monkeypatch.setitem(
        sys.modules, "mlx_lm.tuner.utils", SimpleNamespace(linear_to_lora_layers=None)
    )
    monkeypatch.setitem(sys.modules, "mlx_lm.utils", SimpleNamespace(save_config=None))
    # Exercise __main__ as python -m does, replacing only native model libraries.
    monkeypatch.delitem(sys.modules, f"train.{command}", raising=False)
    argv = [command, "--model", "/unused", "--arm", "base"]
    if command == "draft":
        argv.extend(["--pairs", "/unused"])
    else:
        argv.extend(["--train", "/unused", "--adapter-path", "/unused"])
    monkeypatch.setattr(sys, "argv", argv)
    with process_lock(lock), pytest.raises(AlreadyRunning):
        runpy.run_module(f"train.{command}", run_name="__main__")
    assert not called
    with pytest.raises(ValueError, match="stop after checking lock"):
        runpy.run_module(f"train.{command}", run_name="__main__")
    assert called == [True]
    with process_lock(lock):
        pass


def test_generation_matches_installed_training_prompt_contract(tmp_path, monkeypatch):
    from gmail_agent.replies import context_for
    from train import format as training_format

    prompts = []
    tokenizer = SimpleNamespace(
        encode=lambda text, **_: list(text.encode()),
        eos_token_id=0,
    )

    def generate(_model, _tokenizer, *, prompt, max_tokens):
        prompts.append(bytes(prompt).decode())
        return "A reply"

    monkeypatch.setitem(sys.modules, "mlx_lm", SimpleNamespace(generate=generate, load=None))
    monkeypatch.delitem(sys.modules, "train.draft", raising=False)
    # Use the actual train.draft and prompt formatter, with no model/GPU needed.
    generator = LocalGenerator(tmp_path, adapter_name="social", arm="base")
    generator.model, generator.tokenizer = object(), tokenizer
    messages = [
        {
            "id": "sent",
            "headers": {"from": "me@example.com"},
            "labels": ["SENT"],
            "body_text": "Are you free?",
        },
        {
            "id": "incoming",
            "headers": {"from": "friend@example.com"},
            "labels": ["INBOX"],
            "body_text": "Yes!",
        },
    ]
    context = context_for(messages[-1], messages, "me@example.com")
    try:
        assert generator.generate(context) == "A reply"
        if hasattr(training_format, "Conversation"):
            assert "Me: Are you free?" in prompts[0]
            assert "Them: Yes!" in prompts[0]
            assert prompts[0].endswith("Me:")
        else:
            assert "From: me@example.com" in prompts[0]
            assert prompts[0].endswith("Reply:")
    finally:
        sys.modules.pop("train.draft", None)


def test_adapter_persona_defaults_to_directory_and_can_be_overridden(tmp_path):
    assert LocalGenerator(tmp_path).adapter_name == "default"
    assert LocalGenerator(tmp_path, adapter_path=tmp_path / "social").adapter_name == "social"
    assert (
        LocalGenerator(
            tmp_path, adapter_path=tmp_path / "version 2", adapter_name="professional"
        ).adapter_name
        == "professional"
    )
    with pytest.raises(ValueError, match="--adapter-name"):
        LocalGenerator(tmp_path, adapter_path=tmp_path / "version 2")
