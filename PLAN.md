# Implementation Plan

Companion to [PROJECT.md](PROJECT.md). Target machine: Apple M2, 16 GB unified memory.

## 1. Model

- **Base arm:** `Qwen/Qwen3.5-2B-Base`. **Instruct arm:** `Qwen/Qwen3.5-2B`.
  - The instruct model was post-trained from the base model, so the only difference between the arms is instruction tuning.
  - They share a tokenizer, so held-out loss can be compared directly between them.
- **Why 2B:** "Train now" has to finish in ~2 minutes on stage, so we test the same size we ship. The 4B pair is a stretch goal. If Qwen3.5 gives us trouble, the fallback is `Qwen3-1.7B` / `Qwen3-1.7B-Base`.
- **Precision:** bf16 everywhere (~4.5 GB). If drafting is too slow, move the *whole* pipeline (training and serving) to 8-bit. Never train in one precision and serve in another, and avoid 4-bit at this size.
- **Thinking off:** the instruct model is hybrid-thinking, so always render its chat template with `enable_thinking=False`.
- **Rejected:** Gemma 4 E2B/E4B (larger on disk than they look, and people report trouble with LoRA in MLX), and Llama 3.2 3B (older and weaker).

## 2. Base vs. instruct experiment

- Both arms get the same data, LoRA config (rank 16, last 16 layers), learning rate, steps and seed. Only the prompt rendering differs (see [train/format.py](train/format.py)):
  - **Base:** the source-labelled prompt (`Source: … / To: … / Thread: … / Reply:`), then the reply and `<|endoftext|>`.
  - **Instruct:** the same text as the user turn, plus a one-line system prompt, through the chat template.
- **Why a custom trainer:** mlx-lm's `completions` format always applies the chat template, which breaks the base arm. mlx-lm also has no DPO. So we build the examples ourselves and reuse its training loop. DPO will be a small custom loss, using the base model with the adapter turned off as the reference.
- **Split:** by time. The newest ~10% of sent replies are held out.
- **Metrics:**
  - held-out reply loss
  - placeholder precision/recall
  - hallucinated specifics (unmasked dates, times or amounts that aren't in the thread)
  - simple style stats
  - a blind A/B on ~30 emails by the mailbox owner
- **Decision:** the A/B result picks the winner. Placeholder correctness and hallucination rate are hard requirements.

## 3. Architecture

The automated Gmail reply component is specified in [GMAIL_ARCHITECTURE.md](GMAIL_ARCHITECTURE.md). Gmail is the reading, editing and sending interface. A local worker imports historical mail once, then checks for changes every 30 minutes, generates replies locally, and saves native Gmail drafts.

```
Gmail API ↔ local gmail_agent/ worker ↔ data/mail.db (SQLite)
                       ↕                        ↓
             local model + adapter       pipeline/ → train/
                       ↓
              native Gmail reply draft
                       ↓
              human edits and sends
```

- **Data:** OAuth with `gmail.readonly` and `gmail.compose`; credentials in macOS Keychain. Historical import feeds training without automatically drafting replies to old mail.
- **Schedule:** a per-user macOS LaunchAgent starts the worker at login and restarts it after process exit. Sync every 30 minutes, with catch-up after sleep or restart. Persist the Gmail history cursor and pending reply work together; expose durable cycle health and actual service status separately.
- **Drafts:** generate one active suggestion per thread and create it through Gmail API. Preserve existing drafts and human edits. The worker never sends mail.
- **Feedback:** associate the next sent reply with the active generation using account and Gmail thread ID. Capture the original generation and final sent text on the next sync.
- **Privacy:** processing, training data and adapters stay local; native Gmail drafts are stored by Google.
- **Application delivery:** macOS first, with a native Apple Silicon sidecar containing Python, the Gmail worker and MLX. The desktop UI controls it through process commands and JSON results. Models, adapters and mail data remain in Application Support, with tokens in Keychain. The build and integration contract are in [DESKTOP_PACKAGING.md](DESKTOP_PACKAGING.md).
- **UI scope:** this component works through native Gmail drafts. A custom inbox, extension, calendar fill UI and universal placeholder send guard are separate product work. Gmail's native Send action is not controlled by this worker.

## 4. Data pipeline

Gmail, Slack and Discord share a source-labelled message contract described in
[MESSAGE_PIPELINE.md](MESSAGE_PIPELINE.md). `message_pipeline/` adds selected-channel
Slack/Discord imports, durable pagination and a 30-minute polling command. Every
normalized message carries `source`, account/workspace/channel IDs, and a thread ID.
Gmail exports and draft feedback carry `source: "gmail"`, including legacy records.
Chat exports and observed reply feedback are context-only and do not automatically enter
the training flow below. Slack/Discord local reply suggestions are available through the
worker's JSON interface; connection/review UI and hosted OAuth exchange remain application
work. Gmail remains the only native draft integration.

1. **Ingest:** the Gmail worker imports historical messages and periodic changes into SQLite, preserving Gmail thread IDs and the `SENT` label. Takeout remains an optional import path; its sent labels and thread IDs must be normalized before merging with API data.
2. **Filter:** drop messages with `List-Unsubscribe`, `Precedence: bulk|list`, `Auto-Submitted`, or a `noreply` sender.
3. **Clean:** `email-reply-parser` strips quoted text. Signatures are found by taking the user's most common trailing block of lines and stripping it.
4. **Relationships:** heuristics give a first guess, then the user labels their top ~30 contacts in the UI. Everyone else is `unknown`.
5. **Mask (reply targets only):**
   - regex for `[LINK] [PHONE] [AMOUNT] [TIME] [DATE]`
   - spaCy for `[ADDRESS]` and any dates the regex missed
   - unit-tested
6. **Build:** each sent reply is paired with the last 3 messages of its thread, written out with a time-based split.
7. **Triage:** received, non-automated mail that the user never replied to becomes a label for a logistic-regression model. The UI shows it only as "likely no reply needed".

## 5. Training

- **Initial:** 1–2 epochs, lr 1e-5, batch 4.
- **Nightly:** retrain from scratch on all pairs plus edited drafts (SFT), then DPO on (draft, edited final) pairs.
- **Train now:** ~30 steps on the new edits plus replayed older pairs, then hot-swap the adapter.

## 6. Milestones

| # | Milestone | Done when |
|---|---|---|
| 0 | ✅ Env + trainer smoke test on fake pairs | Both arms train and draft in the fake persona |
| 1 | Historical Gmail import → filter → clean | 30 spot-checked replies have no quotes or signatures |
| 2 | Masking, relationships, dataset build | Mask tests pass, and the train/valid JSONL is written |
| 3 | Train both arms | Two adapters |
| 4 | Eval + A/B → pick the arm | Decision made |
| 5 | Gmail OAuth, 30-minute sync and native reply drafts | A new eligible email receives a correctly threaded draft in Gmail |
| 6 | Thread-mapped sent feedback + Train now (SFT + DPO) | A reply edited and sent in Gmail is captured locally and influences a later draft |
| 7 | Triage classifier | Reply likelihood is available to draft selection |
| 8 | Offline generation and reconnect rehearsal | Cached-context generation works offline; Gmail sync and draft publication resume after reconnect |

Historical data preparation and the Gmail worker implementation can proceed in parallel.

## 7. Setup

The Gmail worker is implemented on the `infra` branch. Follow [GMAIL_SETUP.md](GMAIL_SETUP.md) for Google Cloud setup, OAuth, import, scheduled drafting, and live verification. Cleaning, masking, dataset construction, and feedback-driven training remain separate milestones.

```bash
uv sync                          # Python 3.12 (MLX lags behind the system 3.15)
hf download Qwen/Qwen3.5-2B-Base --local-dir models/Qwen3.5-2B-Base
hf download Qwen/Qwen3.5-2B --local-dir models/Qwen3.5-2B
```

## 8. Open questions

- Whose mailbox is the demo dataset? It needs ≥500 sent replies.
- Do we need a `[NAME]` placeholder? It's not in the spec, so it's left out for now.
