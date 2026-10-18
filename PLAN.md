# Implementation Plan

Companion to [PROJECT.md](PROJECT.md). Target machine: Apple M2, 16 GB unified memory.

## 1. Model

> **Current default (Oct 18, 2026):** `Qwen/Qwen3-4B-Base`, base arm, for training and serving. With no time for overnight local runs before the demo, adapters are trained on Veri GPUs (`train.trainer --backend veri`, then `train.veri fetch`; see [TRAINING_DATA.md](TRAINING_DATA.md)). Local mlx training remains the intended path. The 1.7B analysis below is kept for the local Train now budget.

- **Base arm:** `Qwen/Qwen3-1.7B-Base`. **Instruct arm:** `Qwen/Qwen3-1.7B`.
  - The instruct model was post-trained from the base model, so the only difference between the arms is instruction tuning.
  - They share a tokenizer, so held-out loss can be compared directly between them.
- **Why 1.7B:** "Train now" has to finish in ~2 minutes on stage, so we test the same size we ship. Measured on the M2 (LoRA rank 16, last 16 layers, grad checkpointing): ~235 tok/s, and 8 rows × 1024 tokens peaks at 10.5 GB of the 11.45 GB Metal limit. Two minutes is therefore ~28k training tokens. Checkpointing stays on: without it, memory pressure makes steps slower.
- **Why not Qwen3.5-2B:** 18 of its 24 layers are Gated DeltaNet (linear attention). mlx-lm's kernel for those has no backward pass, so training falls back to a per-token loop that keeps a 1 MB fp32 state per token per layer: ~2 GB per 256-token row, OOM at 4 rows, ~7 tok/s.
- **Precision:** bf16 everywhere (~3.5 GB). If drafting is too slow, move the *whole* pipeline (training and serving) to 8-bit. Never train in one precision and serve in another, and avoid 4-bit at this size.
- **Thinking off:** the instruct model is hybrid-thinking, so always render its chat template with `enable_thinking=False`.
- **Rejected:** Gemma 4 E2B/E4B (larger on disk than they look, and people report trouble with LoRA in MLX), and Llama 3.2 3B (older and weaker).

## 2. Base vs. instruct experiment

- Both arms get the same data, LoRA config (rank 16, last 16 layers), learning rate, steps and seed. Only the prompt rendering differs (see [train/format.py](train/format.py)):
  - **Base:** plain text, `Them: …` / `Me: …` turns, each "me" turn ending in `<|endoftext|>`, with an optional `Request: …` line first.
  - **Instruct:** ChatML with "them" as the user role and "me" as the assistant role. The request goes in the system prompt.
- **Why a custom trainer:** mlx-lm's `completions` format always applies the chat template, which breaks the base arm. mlx-lm also has no DPO. So we build the examples ourselves and reuse its training loop. DPO is a small custom loss ([train/dpo.py](train/dpo.py)) mixed with SFT in one forward pass. The reference is the starting model: the base model for a from-scratch run, or the live adapter for Train now. Its log-probs are precomputed once, so we never hold two models in memory.
- **Split:** by time. Held-out records have `"type": "test"` (the default is `"train"`). When an adapter has test records, the trainer logs test loss every `--eval-every` steps (default 50) on `--eval-batches` batches (default 10, the same ones each time), and on the whole test set at the end. Without test records there is no evaluation.
- **Metrics:**
  - held-out loss on "me" messages
  - placeholder precision/recall
  - hallucinated specifics (unmasked dates, times or amounts that aren't in the conversation or request)
  - simple style stats
  - a blind A/B on ~30 conversations (replies and requests) by the data owner
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
3. **Clean:** the Gmail exporter strips common quoted tails, quoted lines and standard signature delimiters. Unusual formats still need review.
4. **Personas:** the exporter explicitly assigns an adapter name; local `train.augment --relabel` can label mixed history as professional/social. Review labels before training.
5. **Mask ("me" messages only):**
   - regex for `[LINK] [PHONE] [AMOUNT] [TIME] [DATE]`
   - spaCy for `[ADDRESS]` and any dates the regex missed
   - unit-tested
6. **Build:** each conversation, on any channel, becomes one JSONL record: `{"adapter_name", "type"?, "prompt"?, "conversation": [{"from": "me"|"them", "text"}]}`, oldest message first. The source label and scoped provenance remain attached to each record. The newest ~10–30% are marked `"type": "test"`.
   - **Augment:** [train/augment.py](train/augment.py) has the instruct model write a `prompt` for each conversation that starts with "me" (e.g. "email my professor asking to meet next week"). That teaches the adapters to compose new messages from a request, not just reply.
7. **Triage:** received, non-automated messages that the user never replied to become a label for a logistic-regression model. The UI shows it only as "likely no reply needed".

## 5. Training

- **Loss:** on every "me" message in a conversation, in one pass (per-token loss mask). Long conversations are split into windows that each end on a "me" message, with as much history as fits.
- **One adapter per persona:** records are grouped by `adapter_name`, and each group trains `adapters/<arm>/<adapter_name>` from the same base model.
- **Masking at train time:** the trainer re-runs [train/mask.py](train/mask.py) on every "me" message and edit final, so placeholders don't depend on the pipeline (it's a no-op on masked text). Dates and times are always masked. Links, phones, addresses and amounts are kept when an earlier "them" message contains them, and any fact named in the request is kept. Model drafts stay as written, so DPO pushes down the specifics they invented. An edit that only filled in placeholders is not a DPO pair.
- **Initial:** 2 epochs (`--epochs`), lr 1e-5, batch 4. Every persona trains for the same number of epochs at the same batch size, so a persona with less data takes fewer steps.
- **Nightly:** retrain every persona from scratch on all its conversations plus edit finals (SFT), with DPO on (draft, edited final) pairs in the same run.
- **Train now:** a fixed ~30 steps (`--iters`) on the persona's new edits plus replayed older conversations, then hot-swap that persona's adapter.

## 6. Milestones

| # | Milestone | Done when |
|---|---|---|
| 0 | ✅ Env + trainer smoke test on fake pairs | Both arms train and draft in the fake persona |
| 1 | Ingest → filter → clean on a real Takeout, then a Slack export (Enron converter done) | 30 spot-checked "me" messages have no quotes or signatures |
| 2 | Masking, personas, dataset build + prompt augmentation | Mask tests pass, and the train/valid JSONL is written |
| 3 | Train both arms | One adapter per persona, per arm |
| 4 | Eval + A/B → pick the arm | Decision made |
| 5 | Gmail OAuth, 30-minute sync and native reply drafts | A new eligible email receives a correctly threaded draft in Gmail |
| 6 | Thread-mapped sent feedback + Train now (SFT + DPO) | A reply edited and sent in Gmail is captured locally and influences a later draft |
| 7 | Triage classifier | Reply likelihood is available to draft selection |
| 8 | Offline generation and reconnect rehearsal | Cached-context generation works offline; Gmail sync and draft publication resume after reconnect |

Historical data preparation and the Gmail worker implementation can proceed in parallel.

## 7. Setup

The Gmail worker is implemented on the `infra` branch. Follow [GMAIL_SETUP.md](GMAIL_SETUP.md) for Google Cloud setup, OAuth, import, scheduled drafting, and live verification. The local cache and sent feedback feed the conversation trainer through [TRAINING_DATA.md](TRAINING_DATA.md). Model quality and desktop training controls remain separate work.

```bash
uv sync                          # Python 3.12 (MLX lags behind the system 3.15)
hf download Qwen/Qwen3-4B-Base --local-dir models/Qwen3-4B-Base
```

## 8. Open questions

- Whose data is the demo dataset? It needs ≥500 "me" messages per persona. Until then we test on Enron (Jeff Dasovich: 496 train / 248 test conversations, only 84 of them `social`).
- How does a conversation get its `adapter_name`? For training data, the instruct model labels it in augment (step 4). At draft time: probably by channel (Slack workspace → professional), then the contact's last persona, with the model as a fallback the user corrects in the picker.
- Do we need a `[NAME]` placeholder? It's not in the spec, so it's left out for now.
