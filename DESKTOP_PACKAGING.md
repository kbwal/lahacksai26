# macOS application integration

The shipping unit is a **native Apple Silicon sidecar bundle**. It contains the Gmail worker, its Python interpreter, OAuth libraries, SQLite bindings, MLX, and Metal resources. A Swift, Tauri, or Electron desktop app can embed the entire directory and invoke its executable. End users do not install Python, uv, or the source repository.

This is application packaging, not a Linux container. The existing runtime uses macOS Metal and Keychain directly. Docker's ordinary Linux-container GPU passthrough is not the interface used by this MLX worker. A future hosted or cross-platform version would need a different credential backend and a separately validated inference runtime. [Docker GPU support](https://docs.docker.com/desktop/features/gpu/), [PyInstaller operating model](https://pyinstaller.org/en/stable/operating-mode.html).

## Ownership and layout

```text
Mail Companion.app/
  Contents/Helpers/lahacks-mail-worker/
    lahacks-mail-worker          # executable, controlled by the desktop app
    _internal/                   # interpreter, native libraries, Python modules
    build-info.json              # dependency versions and lockfile checksum

~/Library/Application Support/lahacksai26/
  data/mail.db                   # Gmail history, pending drafts, sync/feedback state
  data/model.lock                # shared by inference and training
  models/Qwen3-4B-Base/          # installed separately, reusable across app updates
  adapters/<persona>/           # one trained LoRA per persona, loaded by persona id
  credentials/                  # app-provided Desktop OAuth client configuration

macOS Keychain                  # per-account access and refresh tokens
~/Library/Logs/lahacksai26/      # counts and sanitized operational errors
```

The executable and `_internal` must move together. Do not copy only the executable. No mailbox, OAuth configuration, tokens, adapters, or model weights are included in the archive. Installing a new app version replaces code; it does not replace the data home. Removing the login service retains user data.

The bundled worker defaults to the Application Support location above, independent of its working directory. The app can set the absolute environment variable `LAHACKS_MAIL_HOME` to select another home. `--db`, `--model`, `--adapter-path`, and `--model-lock` override individual paths. `--adapter-name` selects the training persona label when it differs from the adapter directory name (otherwise `default` without an adapter). Development database/model paths retain their repository-relative defaults unless `LAHACKS_MAIL_HOME` is set. The model lock defaults to the same absolute Application Support path in development and bundled processes, independent of the working directory. Training entrypoints use `runtime.default_paths()["model_lock"]`; use the same `LAHACKS_MAIL_HOME` in training and the worker when selecting an alternate home. Existing installations are not silently migrated; stop the old worker and explicitly reuse its absolute paths, or migrate the database with a SQLite backup before switching ownership.

## App-to-worker interface, version 1

Use a process API with an **argument array**, never a shell command assembled from user input. Finite commands return one JSON value on stdout when successful. Operational messages and errors go to stderr. Exit code `0` means success, `1` means an operation failed, and `2` means invalid arguments. Help is human-readable. Inspect `info` before starting work; reject unsupported `interface_version` values. The `run` command stays alive and writes cycle summaries to stderr; query `status` in a separate short-lived process for persisted progress.

| Application action | Executable arguments | Behavior |
| --- | --- | --- |
| Discover runtime | `info` | Version, architecture and resolved default paths; no DB or Keychain access. |
| Verify runtime | `self-check` | Check Metal, SQLite, CA certificates and Keychain backend loading; does not read tokens or contact Gmail. |
| Verify model installation | `self-check --model /absolute/model/path` | Also generate a synthetic reply locally; no Gmail draft is created. Use `--arm base` for the base model and the same `--model-lock` as the worker. |
| Connect account | `connect --client-secrets /absolute/client.json` | Open system-browser OAuth with PKCE and a loopback callback; save tokens in Keychain. |
| Import history | `--account user@example.com backfill` | Resumable history import; JSON reports whether import completed. |
| Start background work | `--account user@example.com run --model /absolute/model/path --interval 1800` | Sync, reconcile sent replies, generate and publish native drafts; immediate startup catch-up. |
| Read progress | `--account user@example.com status` | Counts, sync cursor, pending/error state and cycle health; no message bodies. |
| Disconnect | `--account user@example.com disconnect` | Revoke OAuth and remove tokens; retain local history. |

Global `--db` and `--account` arguments go **before** the command. App-owned settings supply the model/adapter path, account and interval; the worker does not provide a second settings database. The UI does not directly write the mail database or access Keychain tokens. Normal Gmail HTTP requests and the temporary OAuth callback are the only network interfaces; no permanent local HTTP service or port is required.

The app owns onboarding, model download/progress and verification, account choice, start/stop controls, and reconnect prompts. The worker owns mail storage, sync cursors, draft state, thread matching, inference and Gmail publication. Download model files to a staging directory and atomically promote a verified, complete model directory; stop the worker before changing the model or adapter. Both models remain external assets, so users who only need drafting can install just the instruct model. A model downloader and the desktop UI are future application work; this package consumes an existing local model directory.

## Process lifecycle

Choose **one** supervisor per account in a given database:

- **App-owned process:** launch `run`, continuously drain stderr, and retain the process handle. Send SIGTERM to request shutdown. The worker finishes its current cycle before exiting; allow a grace period because an API retry or model generation can take time. If the app must force termination, the SQLite checkpoints and uncertain-publication state are used on the next launch. Do not report shutdown complete until the process actually exits. Detect unexpected exit and restart with backoff. Closing the UI must either stop the child or intentionally hand background ownership to a login helper.
- **Login service:** the existing `service install/start/stop/status/uninstall` commands manage one LaunchAgent per account and macOS user. Pass global `--account` to select the service when multiple are installed. A bundled installation records the executable's absolute path, not `python -m gmail_agent`, and does not depend on the checkout or virtualenv. Install it only after the app is at its stable installation path. Do not also start an app-owned `run` process. The process lock rejects duplicates.

Read-only `status` may run while the worker is active. Stop the account’s supervisor before `connect`, `disconnect`, `backfill`, `once`, or `recover`. For reconnect, pass global `--account`; other accounts can keep running, and choosing a different Google account is rejected before credentials are saved. Each cycle releases model weights, the Metal cache, and the model lock, allowing training between cycles. A busy model leaves generation queued without using its failure budget and retries after a randomized 1–5 minutes (or sooner if the configured interval is shorter). The next cycle loads updated weights. Hold the shared model lock or stop workers before manually replacing model files; restart the supervisor after an app update. When moving an app that has an installed LaunchAgent, uninstall the old service before moving and reinstall from the final path. A production app can adopt Apple's supported login-helper registration as part of its app lifecycle; the current CLI service remains a development/direct-distribution integration.

## Build and verify

On an Apple Silicon Mac with uv:

```bash
bash scripts/build_macos.sh
```

The script creates an isolated environment under ignored `build/`, installs runtime and packaging dependencies from `uv.lock`, and builds with PyInstaller. It leaves the running development worker's `.venv` alone. Outputs:

```text
dist/lahacks-mail-worker/
dist/lahacks-mail-worker-macos-arm64.zip
dist/lahacks-mail-worker-macos-arm64.zip.sha256
```

Run the repeatable archive test with a local model:

```bash
build/macos-venv/bin/python scripts/smoke_macos.py \
  dist/lahacks-mail-worker-macos-arm64.zip \
  --model models/Qwen3-4B-Base \
  --model-lock "$HOME/Library/Application Support/lahacksai26/data/model.lock"
```

It unpacks into a temporary directory, removes Python environment paths, limits PATH to system binaries, creates an isolated app data home, verifies the executable signature and JSON contract, checks exclusion of private data/weights, and runs a synthetic generation through the actual bundled runtime. It never connects to the mailbox. Omit `--model` for a faster runtime check, or add `--adapter-path adapters/<persona>` to also load a trained adapter. A held model lock makes the check fail safely; wait for or stop the active generator first.

PyInstaller builds are platform-specific. This package targets arm64 macOS; a successful build on the current machine is not proof of compatibility with every macOS version. `build-info.json` records the build OS and exact dependency versions. Validate on the oldest macOS version the application intends to support before declaring that minimum.

### Verified build — October 17, 2026

- Built on Apple Silicon running macOS 26.6.2, with Python 3.12.13, MLX 0.32.4 and mlx-lm 0.32.2.
- Archive size approximately 144 MiB; unpacked bundle approximately 369 MiB, excluding model weights and user data.
- Relocated archive checks passed for **both Qwen3.5-2B instruct and Qwen3.5-2B-Base**, including actual Metal generation from synthetic input, JSON parsing, private database permissions and backend imports.
- All **76 automated tests** passed; Ruff, shell syntax and whitespace checks passed.
- The existing development LaunchAgent remained running. These bundle checks did not read real Keychain tokens, call Gmail, or create drafts. Frozen-binary OAuth onboarding and release signing still need testing in the final desktop app; the owner-deferred live Gmail draft/send exercise remains deferred.

## Release boundary

The default output is **ad hoc signed for local development**, not a notarized consumer application. `APPLE_SIGNING_IDENTITY` can supply a Developer ID identity to the build. When embedding the worker, sign the nested libraries/helper and enclosing app in the application's release pipeline, notarize the finished distribution, and test installation and Keychain prompts on a clean Mac. Preserve a stable signing identity across updates. A sandboxed/Mac App Store app needs a separately validated helper, data-container and Keychain entitlement design; this first package targets direct macOS distribution. [PyInstaller macOS signing](https://pyinstaller.org/en/stable/feature-notes.html#macos-binary-code-signing).

The development Google OAuth project is still in Testing. Its token lifetime and allowed test users apply to this binary too. Public Gmail access requires the OAuth configuration and verification described in [GMAIL_SETUP.md](GMAIL_SETUP.md). Packaging does not change Gmail permissions or enable automatic sending.

## Margin desktop inbox and explicit send commands

Margin also uses these finite JSON commands (global `--db`/`--account` still precede the command):

- `desktop-accounts`: list connected account IDs without reading Keychain.
- `desktop-inbox`: fetch the selected account's current app-owned, published Gmail drafts and snapshot fingerprints for review.
- `desktop-send`: read one reviewed send request from stdin and return `sent` or `uncertain`. Tokens remain inside the sidecar; native IPC never exposes a general command runner.

Only the explicit desktop send path transmits mail, following a native recipient/body confirmation. Scheduled workers never send. The desktop boundary journals attempts durably, rejects stale remote snapshots, enforces unresolved-placeholder guards and requires Gmail for attachments or Cc/Bcc. Uncertain attempts are never replayed automatically. `desktop/README.md` documents selecting an existing Gmail database and using the UI.

## Additional message sources

The same executable exposes `messages --help` for Slack/Discord ingestion and unified
message exports. For example, `lahacks-mail-worker messages --source slack --account U123
--workspace T123 run --suggest-replies` emits one JSON status object per cycle and defaults to a 30-minute
interval. The app should supervise this process separately from the Gmail drafting process.
It uses `data/messages.db` under the same app home and a separate Keychain service.
No extra runtime dependency or model process is needed for ingestion. Optional suggestions
use the bundled MLX runtime and the shared model lock. The `messages ... suggestions`
command supplies generated text and observed human replies to the app's review/copy UI.

See [MESSAGE_PIPELINE.md](MESSAGE_PIPELINE.md) for the message contract, channel selection,
credentials, source labels, provider limits, and outstanding OAuth UI work. The local
archive has been rebuilt with this command: 144 automated tests passed, relocation and
private-data exclusion checks passed, and the relocated executable generated both Slack
and Discord suggestions from synthetic context using Qwen3.5-2B on Metal. No live Slack
or Discord account was accessed. The build discovers MLX's dynamic parsers/templates
from wheel metadata so packaging does not depend on GPU access during module discovery.
