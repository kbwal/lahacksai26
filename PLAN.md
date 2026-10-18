# Implementation Plan

Companion to [PROJECT.md](PROJECT.md). Target machine: Apple M2, 16 GB unified memory.

## 1. Model

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

```
Takeout mbox / Slack export → pipeline/ → data/messages.db (SQLite) → train/ → adapters/<arm>/<adapter_name>/
                                        ↓
                        server/ (FastAPI on 127.0.0.1, mlx-lm in-process)
                                        ↑
                        extension/ (Chrome MV3 side panel + Gmail / Slack content scripts)
```

- **Side panel:** the main UI. It's bundled with the extension, so it works offline, and the airplane-mode demo runs here. It holds:
  - the inbox from `messages.db`, across channels
  - the draft editor and placeholder fill, for replies and for new messages from a request
  - a persona picker (defaults to the conversation's persona)
  - Train now
  - the A/B page
- **Content scripts (Gmail first, then Slack):** online only. They read the open conversation, insert drafts and block Send while placeholders remain. Both services save drafts on their servers, so our privacy claim is "the model, the data and the adapters never leave the device".
- **Server:** accepts requests only from our extension's origin, plus a per-install token. It keeps the base model loaded and swaps in the persona's adapter per request (adapters are tens of MB).
- **Send guard:** block on `\[[A-Z][A-Z_ ']*\]`, i.e. *any* bracketed uppercase token, in both the UI and the server. The smoke test showed the model inventing new types (`[DINING]`).
- **Placeholder fill:** `[DATE]` and `[TIME]` suggestions come from a local `.ics` calendar export.
- **Sending:** goes to a mock outbox. Real sending would need the network.

## 4. Data pipeline

Each source has its own ingest/filter/clean. Everything after that is channel-agnostic.

1. **Ingest:**
   - Email: `mailbox.mbox` reads the Google Takeout file. The user's mail is identified by `X-Gmail-Labels: Sent`. Threads come from `X-GM-THRID`.
   - Slack: the workspace export (JSON per channel per day). The user's messages are identified by their user ID. Threads come from `thread_ts`, DMs by channel.
   - Test data until we have a real mailbox: Jeff Dasovich's Enron sent mail ([Kaggle](https://www.kaggle.com/datasets/wcukierski/enron-email-dataset)). 750 of his sent emails are in `train/data/enron/dasovich-j.csv` (gitignored). [pipeline/enron.py](pipeline/enron.py) turns them into conversations: quoted Outlook/Notes history is split into me/them messages, quoted-printable is decoded, Notes line wraps are joined, and the newest 250 are `test`. The persona comes from the recipients: `social` when nobody at Enron is on the email and someone has a personal address.
2. **Filter:** drop automated messages. For email, that means `List-Unsubscribe`, `Precedence: bulk|list`, `Auto-Submitted`, or a `noreply` sender. For Slack, bot and system messages (`subtype`).
3. **Clean:** strip anything the model shouldn't learn to copy. For email, `email-reply-parser` strips quoted text, and signatures are found by taking the user's most common trailing block of lines and stripping it. For Slack, resolve `<@U123>` mentions to names and drop file/join stubs.
4. **Personas:** every conversation gets an `adapter_name`, `professional` or `social`. Each persona trains its own LoRA adapter. [train/augment.py](train/augment.py) labels any record without one: the instruct model is asked who the user is talking to, and the higher-scoring of the two words wins. `--relabel` replaces existing labels (e.g. the Enron recipient rule).
5. **Mask ("me" messages only):**
   - regex for `[LINK] [PHONE] [AMOUNT] [TIME] [DATE]`
   - spaCy for `[ADDRESS]` and any dates the regex missed
   - unit-tested
6. **Build:** each conversation, on any channel, becomes one JSONL record: `{"adapter_name", "type"?, "prompt"?, "conversation": [{"from": "me"|"them", "text"}]}`, oldest message first. There's no recipient, subject or channel metadata. The newest ~10–30% are marked `"type": "test"`.
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
| 5 | Server + side panel: inbox, draft, send guard, calendar fill | Masking demo flow works |
| 6 | Edit logging + Train now (SFT + DPO) | An edit changes the next draft |
| 7 | Triage classifier | Badges in the inbox |
| 8 | Airplane-mode rehearsal | Demo runs with Wi-Fi off |

Milestones 1–2 and the UI shell for 5 can run in parallel.

## 7. Setup

```bash
uv sync                          # Python 3.12 (MLX lags behind the system 3.15)
hf download Qwen/Qwen3-1.7B-Base --local-dir models/Qwen3-1.7B-Base
hf download Qwen/Qwen3-1.7B --local-dir models/Qwen3-1.7B
```

## 8. Open questions

- Whose data is the demo dataset? It needs ≥500 "me" messages per persona. Until then we test on Enron (Jeff Dasovich: 496 train / 248 test conversations, only 84 of them `social`).
- How does a conversation get its `adapter_name`? For training data, the instruct model labels it in augment (step 4). At draft time: probably by channel (Slack workspace → professional), then the contact's last persona, with the model as a fallback the user corrects in the picker.
- Do we need a `[NAME]` placeholder? It's not in the spec, so it's left out for now.
