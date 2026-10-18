"""Build on Apple Silicon with the locked packaging environment."""

import os
from importlib.metadata import distribution
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    copy_metadata,
)

root = Path(SPECPATH).parent.parent

# MLX's Metal library and Transformers' lazy tokenizer imports are not ordinary
# Python imports. Include the Qwen3 backend explicitly; model weights stay external.
datas = collect_data_files("mlx") + copy_metadata("mlx-lm", recursive=True)
datas += collect_data_files("certifi")
hiddenimports = [
    # Imported by the native extension, invisible to Python import analysis.
    "mlx._reprlib_fix",
    "mlx.__array_api_info",
    "mlx_lm.models.qwen3",
    # Drafting imports these lazily, inside functions.
    "train.draft",
    "train.format",
    "train.mask",
    "transformers.models.qwen2.tokenization_qwen2",
    "keyring.backends.macOS",
]
# Tokenizer configuration selects these by string, including Qwen's tool parser
# even when this application only generates plain-text replies.
# collect_submodules imports mlx_lm to discover nested packages and can silently
# return [] when Metal is unavailable in the build sandbox. Read wheel metadata
# instead; discovering Python sources must not depend on an available GPU.
for path in distribution("mlx-lm").files or []:
    name = str(path)
    if name.startswith(("mlx_lm/tool_parsers/", "mlx_lm/chat_templates/")) and name.endswith(".py"):
        hiddenimports.append(name[:-3].replace("/", ".").removesuffix(".__init__"))

a = Analysis(
    [str(root / "packaging/macos/entry.py")],
    pathex=[str(root)],
    binaries=collect_dynamic_libs("mlx"),
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "torch", "tensorflow", "jax", "matplotlib", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="lahacks-mail-worker",
    console=True,
    target_arch="arm64",
    codesign_identity=os.environ.get("APPLE_SIGNING_IDENTITY") or None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="lahacks-mail-worker")
