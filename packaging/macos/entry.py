"""PyInstaller entrypoint; the desktop app invokes this executable directly."""

import os

# A shipped worker uses models installed by the app, never an implicit Hub download.
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

from gmail_agent.__main__ import main

raise SystemExit(main())
