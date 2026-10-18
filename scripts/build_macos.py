"""Build a relocatable sidecar directory and zip, excluding all user data."""

import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent.parent


def main():
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise SystemExit("Build this sidecar on an Apple Silicon Mac")
    PyInstaller.__main__.run(
        [
            str(ROOT / "packaging/macos/worker.spec"),
            "--noconfirm",
            "--clean",
            "--distpath",
            str(ROOT / "dist"),
            "--workpath",
            str(ROOT / "build/pyinstaller"),
        ]
    )
    bundle = ROOT / "dist/lahacks-mail-worker"
    manifest = {
        "format": 1,
        "target": "macos-arm64",
        "built_on_macos": platform.mac_ver()[0],
        "python": platform.python_version(),
        "uv_lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
        "dependencies": {
            name: importlib.metadata.version(name)
            for name in ("pyinstaller", "mlx", "mlx-lm", "transformers", "google-auth-oauthlib")
        },
    }
    (bundle / "build-info.json").write_text(json.dumps(manifest, indent=2) + "\n")
    archive = ROOT / "dist/lahacks-mail-worker-macos-arm64.zip"
    # ditto preserves the Python framework symlinks and executable modes on macOS.
    subprocess.run(
        ["/usr/bin/ditto", "-c", "-k", "--keepParent", str(bundle), str(archive)], check=True
    )
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n")
    print(f"Built {archive}")


if __name__ == "__main__":
    main()
