# Run the Gmail reply worker

For embedding this worker in the macOS application, use the self-contained native sidecar described in [DESKTOP_PACKAGING.md](DESKTOP_PACKAGING.md). The instructions below also support running from the development checkout.

The worker imports received and sent mail, catches up every 30 minutes, and creates native reply drafts. Edit and send in Gmail; the next cycle captures the final sent text by account and thread. See [the architecture](GMAIL_ARCHITECTURE.md) for the state machine and data model.

## 1. Enable Gmail API and create a client

If Google Cloud shows **Google Cloud access blocked**, complete the account's two-step verification setup using its **Turn on 2-step verification** button. Google may take a few minutes to recognize the change; refresh the Cloud Console afterward. Account authentication setup must be completed by the owner before project creation can continue.

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create or select a project.
2. Go to **APIs & Services → Library**, search for **Gmail API**, and click **Enable** in that project.
3. Open **Google Auth platform → Branding → Get started**. Enter an app name, support email, and contact email.
4. For a personal Gmail account, choose **External**. Keep the app in **Testing**, then add your Gmail address under **Audience → Test users**. Use Internal only for an eligible Workspace organization.
5. Under **Data Access → Add or Remove Scopes**, add:
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/gmail.compose`
6. Under **Clients → Create client**, select **Desktop app**. For this automated worker, select **Use this client for an AI-powered agent**, create the client, and download its JSON. Keep it outside version control, for example in Downloads or the ignored `data/credentials/` directory. Restrict the file to your user with `chmod 600`.

These steps follow Google's [Gmail setup guide](https://developers.google.com/workspace/gmail/api/quickstart/python) and [OAuth consent guide](https://developers.google.com/workspace/guides/configure-oauth-consent). Use a Desktop client, not an API key or service account. The worker opens a browser for your consent and stores OAuth credentials in macOS Keychain.

Google's [AI agent client designation](https://support.google.com/cloud/answer/15549257#agentic-client) identifies automated use; it does not add permissions. Keep a separate client for any future interactive application.

Google's compose permission includes sending, although this worker exposes only draft creation. External apps in Testing receive refresh tokens that expire after seven days with these scopes; reconnect when necessary. See [scope definitions](https://developers.google.com/workspace/gmail/api/auth/scopes) and [token expiration](https://developers.google.com/identity/protocols/oauth2#expiration).

## 2. Connect and import

Run from the repository on an Apple Silicon Mac. Python dependencies are pinned in `uv.lock`; inference uses the local model files.

```bash
cd /Users/ryanyao/Desktop/lahacksai26
uv sync --locked
.venv/bin/python -m gmail_agent connect --client-secrets /absolute/path/to/client_secret.json
.venv/bin/python -m gmail_agent backfill
.venv/bin/python -m gmail_agent status
```

Replace the client path with the downloaded JSON's actual path. Complete Google sign-in and grant both requested permissions. The browser callback waits up to three minutes. A successful connection prints the connected email address.

If a service is already running, use `service stop` before reconnecting or importing manually, and `service start` afterward. Reconnecting preserves the original activation time and history.

`backfill` reads all available threads except spam and trash, including received and sent messages. It creates no drafts. For a smaller initial import, use `backfill --query 'newer_than:90d'` on the first run. `--max-pages 2` checkpoints after two pages; rerun the same command to resume. Use `--reset` to rebuild a completed import. An import query limits historical loading, not subsequent incremental sync.

Mail is stored in `data/mail.db`. Credentials stay in Keychain. Mail bodies and model files are ignored by Git. Message text is decoded, but signature removal, quote stripping, masking, and training datasets belong to the separate planned pipeline.

Initial imports may pause for Gmail quota backoff. The client retries quota-specific HTTP 403 responses, HTTP 429 responses, and temporary server errors with bounded exponential delays. Google currently charges 120 quota units per full thread read against a 6,000-unit per-user-per-project minute limit for new projects; see [Gmail API quotas](https://developers.google.com/workspace/gmail/api/reference/quota). Completed pages remain saved if a later request fails.

## 3. Generate drafts and run every 30 minutes

```bash
.venv/bin/python -m gmail_agent service install
.venv/bin/python -m gmail_agent service status
.venv/bin/python -m gmail_agent status
```

`service install` starts the worker now and registers `com.lahacksai26.gmail-worker.<account-hash>` under `~/Library/LaunchAgents`. It starts at login, restarts after process exit, and performs an immediate catch-up cycle followed by cycles every 1,800 seconds. The Mac must be awake and the user logged in; sleep or network loss delays processing until it can catch up. Each account has its own log under `~/Library/Logs/lahacksai26/`, named after its service label; `service status` reports the exact path; the plist and log are private to your user and contain no OAuth tokens.

Use `service stop` to stop it until the next login, `service start` to resume, and `service uninstall` to stop it and remove automatic startup. Uninstalling retains mail, model files and Keychain credentials. Install a separate managed login service for each connected account by passing `--account you@example.com` before `service install`. With multiple installed services, use `--account` for start, stop, status, and uninstall too. Existing single-label services remain controllable; uninstall and reinstall them to adopt the new label and default model-lock path. Keep the checkout and virtual environment at their installed paths; uninstall and reinstall the service after moving them or changing its model/adapter options.

For an immediate manual cycle or a foreground process:

```bash
.venv/bin/python -m gmail_agent service stop
.venv/bin/python -m gmail_agent once
.venv/bin/python -m gmail_agent service start
# Alternative while the service is stopped: .venv/bin/python -m gmail_agent run
```

`once` syncs and processes up to 20 reply turns. `run` uses the same worker in the terminal until Ctrl-C. A database lock prevents overlapping workers or manual write commands.

Only eligible inbox messages received **after connecting** start automatic replies. Historical unanswered mail does not get drafted. Existing Gmail drafts, bulk mail, automated notifications, spam, trash, and already answered threads are skipped. Replies go directly to the incoming message's Reply-To or From address; reply-all is outside this version.

The default is the downloaded instruct model, with no personalized adapter. To use a trained adapter:

```bash
.venv/bin/python -m gmail_agent service uninstall
.venv/bin/python -m gmail_agent service install --adapter-path /absolute/path/to/instruct-adapter
```

The persona name defaults to the adapter directory name (or `default` with no adapter). Use `--adapter-name social` to override it, including during `service install`; the name is saved with each generation for training feedback.

For the base arm, pass `--model models/Qwen3.5-2B-Base --arm base` and its matching adapter. The worker releases its model and lock after each cycle, so training can run during idle periods; the next generation loads the updated weights. Worker and training commands default to `~/Library/Application Support/lahacksai26/data/model.lock`, even in development. `LAHACKS_MAIL_HOME` selects an isolated data home and lock; use the same environment in training and the worker. The training commands use this shared default in their `__main__` wrappers; the worker’s optional `--model-lock` must match it. Training fails safely while another process owns the lock; worker generation defers without consuming retries and checks again after a randomized 1–5 minutes, capped by the configured interval. Hold that lock or stop the workers before manually replacing model/adapter files.

With multiple connected accounts, put `--account you@example.com` before the subcommand. Worker locks are per account, so independent continuous workers can share the database. They share a model lock to prevent simultaneous Metal model use. Stop only the target account’s worker before reconnect or other manual account operations. Pass `--account you@example.com` before `connect` to lock that account before opening Google sign-in and reject a different selected Google account. Without `--account`, the lock is acquired after Google identifies the account and before saving credentials. Global `--db` also precedes the subcommand.

## 4. Verify in Gmail

Stop the service before this manual exercise and start it afterward.

1. After connecting, receive a new ordinary message from another address in your inbox, with no existing draft in that conversation.
2. Run `once`. Open that conversation in Gmail and confirm the draft has the correct recipient and thread.
3. Edit the text and send it yourself in Gmail.
4. Run `once` again, then `status`. The local record should be `sent`, with no replacement draft for the same incoming message.
5. Export the original generation and final sent text:

   ```bash
   .venv/bin/python -m gmail_agent export-feedback --output data/feedback-first-test.jsonl
   ```

The export includes private, unmasked email text and never overwrites an existing file. It is written privately to a temporary file and published atomically only after every record succeeds; a failed export leaves no partial destination or temporary file. The JSONL uses the training branch’s `Edit` format; its masking pipeline handles the unmasked text. Thread correspondence intentionally does not prove that a sent reply came from the exact generated draft.

## Recovery and status

`export-feedback` JSONL is directly readable by `train.format.Edit.from_dict` on the training branch. It includes `adapter_name`, the saved `conversation` with `me`/`them` turns, normalized `draft`/`final`, and `type: "train"`, plus the original audit fields. Legacy records reconstruct their context from saved message IDs and use the persona `default` because older records did not save a persona name. Review that label before combining old feedback with a named persona.

`status` works without OAuth and shows sync progress, draft counts, local draft IDs with errors, and `worker` health. Timestamps are Unix time in milliseconds. The health record distinguishes a running cycle, an idle worker after a completed cycle, and a failed cycle; it stores the error class without private exception text. `service status` independently reports whether launchd has a running process. Use both: a saved cycle state cannot prove the process is still alive. Model failures retain queued work; three failed generation attempts mark that turn `failed`. A later cycle retries transient sync failures from the saved cursor.

If a draft upload times out, its state remains `publishing`. A later cycle adopts a matching surviving Gmail draft automatically. Otherwise, inspect the conversation and use one of these explicit recovery commands with the local record ID from `status`:

```bash
.venv/bin/python -m gmail_agent recover 12 --adopt GMAIL_DRAFT_ID
.venv/bin/python -m gmail_agent recover 12 --dismiss
.venv/bin/python -m gmail_agent recover 12 --retry
```

`--adopt` links an existing draft in the same thread. `--dismiss` closes the local record and leaves Gmail untouched. Use `--retry` only after checking that the uncertain attempt created no draft; it refuses when a draft currently exists, and the next cycle performs another preflight. Automatic cycles never rewrite human edits or recreate a discarded reply turn.

For Google sign-in failures, confirm Gmail API is enabled in the client project's console, your address is a test user, and both scopes were granted. Re-run `connect` after token expiry or revocation. To revoke access while keeping local history:

```bash
.venv/bin/python -m gmail_agent service uninstall
.venv/bin/python -m gmail_agent disconnect
```

## Development checks

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check gmail_agent tests
```

The tests exercise simulated Gmail synchronization, draft publication, sent feedback, races, restarts, OAuth configuration, and quota recovery. Local Qwen inference has also been smoke-tested offline. OAuth connection, a complete historical import, and the first incremental sync have been verified against real Gmail. The live draft → human send → feedback check above was skipped at the user's request and remains unverified.

## Verification record — October 17, 2026

| Requirement | Evidence |
| --- | --- |
| Gmail OAuth and private credentials | Desktop client created in `lahacksai26-gmail`; real profile and mailbox reads succeed. Token refresh persists to Keychain in automated tests. Client JSON is ignored by Git with mode `0600`. |
| Past received and sent email | Completed import: 154 messages in 148 threads, including 17 `SENT` messages. SQLite integrity and foreign-key checks passed. |
| Incremental sync and 30-minute operation | The installed LaunchAgent runs the worker with `--interval 1800`; authenticated startup cycles completed. Tests cover history pagination, cursor transactions, expired cursors and import races. |
| Login startup and recovery | LaunchAgent is loaded and running. After graceful process termination, launchd created a new PID and the worker completed another sync from the saved cursor. |
| Local model under service conditions | Qwen3.5-2B generated a nonempty reply to synthetic input in an isolated launchd job using the service environment with offline model settings; job exited successfully. No Gmail draft was created by this check. |
| Native reply construction and human draft ownership | Automated tests verify recipient, subject, reply headers, thread association, existing drafts, human edits during generation, discard behavior and uncertain writes. |
| Thread-based sent feedback | Automated lifecycle and account-isolation tests verify original generation/final-text pairing, both sent-event forms, subsequent reply turns and no duplicate draft on replay. |
| Operation and recovery controls | 76 tests passed, including service install/stop/start/uninstall, worker health persistence, quota backoff, recovery, app data paths, bundled service arguments and JSON output isolation. Ruff and whitespace checks passed. |
| Application packaging | Built the arm64 macOS archive and tested extracted copies outside the repository with an isolated data home and no Python environment paths. Both Qwen3.5-2B and Qwen3.5-2B-Base generated synthetic replies through bundled MLX/Metal. The archive excludes user data and model weights. See [DESKTOP_PACKAGING.md](DESKTOP_PACKAGING.md). |
| Live Gmail draft/edit/send exercise | Explicitly skipped by the owner. This provider-level behavior is not claimed as live-tested. |

The app remains in Google's OAuth Testing mode, with the reconnection requirement described above. Training, cleaning/masking, a learned reply classifier, and a custom send-blocking UI remain separate product components, as scoped in the architecture.
