"""Fixed development entrypoint; the app never constructs shell commands."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from gmail_agent.__main__ import main

raise SystemExit(main())
