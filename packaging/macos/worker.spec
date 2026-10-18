"""Build on Apple Silicon with the locked packaging environment."""

import os
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

root = Path(SPECPATH).parent.parent

# MLX's Metal library and Transformers' lazy tokenizer imports are not ordinary
# Python imports. Include the Qwen3.5 backend explicitly; model weights stay external.
datas = collect_data_files("mlx") + copy_metadata("mlx-lm", recursive=True)
datas += collect_data_files("certifi")
hiddenimports = [
    # Imported by the native extension, invisible to Python import analysis.
    "mlx._reprlib_fix",
    "mlx.__array_api_info",
    "mlx_lm.models.qwen3_5",
    "transformers.models.qwen2.tokenization_qwen2",
    "keyring.backends.macOS",
]
# Tokenizer configuration selects these by string, including Qwen's tool parser
# even when this application only generates plain-text replies.
hiddenimports += collect_submodules("mlx_lm.tool_parsers")
hiddenimports += collect_submodules("mlx_lm.chat_templates")

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
