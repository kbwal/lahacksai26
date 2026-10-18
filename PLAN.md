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
  - **Base:** the raw PROJECT.md prompt (`To: … / Thread: … / Reply:`), then the reply and `<|endoftext|>`.
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

```
Takeout mbox → pipeline/ → data/mail.db (SQLite) → train/ → adapters/
                                   ↓
                   server/ (FastAPI on 127.0.0.1, mlx-lm in-process)
                                   ↑
                   extension/ (Chrome MV3 side panel + Gmail content script)
```

- **Side panel:** the main UI. It's bundled with the extension, so it works offline, and the airplane-mode demo runs here. It holds:
  - the inbox from `mail.db`
  - the draft editor and placeholder fill
  - Train now
  - the A/B page
- **Gmail content script:** online only. It reads the open thread, inserts drafts and blocks Send while placeholders remain. Note that Gmail autosaves drafts to Google, so our privacy claim is "the model, the data and the adapters never leave the device".
- **Server:** accepts requests only from our extension's origin, plus a per-install token.
- **Send guard:** block on `\[[A-Z][A-Z_ ']*\]`, i.e. *any* bracketed uppercase token, in both the UI and the server. The smoke test showed the model inventing new types (`[DINING]`).
- **Placeholder fill:** `[DATE]` and `[TIME]` suggestions come from a local `.ics` calendar export.
- **Sending:** goes to a mock outbox. Real sending would need the network.

## 4. Data pipeline

1. **Ingest:** `mailbox.mbox` reads the Takeout file. The user's sent mail is identified by `X-Gmail-Labels: Sent`. Threads come from `X-GM-THRID`.
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
| 1 | Ingest → filter → clean on a real Takeout | 30 spot-checked replies have no quotes or signatures |
| 2 | Masking, relationships, dataset build | Mask tests pass, and the train/valid JSONL is written |
| 3 | Train both arms | Two adapters |
| 4 | Eval + A/B → pick the arm | Decision made |
| 5 | Server + side panel: inbox, draft, send guard, calendar fill | Masking demo flow works |
| 6 | Edit logging + Train now (SFT + DPO) | An edit changes the next draft |
| 7 | Triage classifier | Badges in the inbox |
| 8 | Airplane-mode rehearsal | Demo runs with Wi-Fi off |

Milestones 1–2 and the UI shell for 5 can run in parallel.

## 7. Setup

```bash
uv sync                          # Python 3.12 (MLX lags behind the system 3.15)
hf download Qwen/Qwen3.5-2B-Base --local-dir models/Qwen3.5-2B-Base
hf download Qwen/Qwen3.5-2B --local-dir models/Qwen3.5-2B
```

## 8. Open questions

- Whose mailbox is the demo dataset? It needs ≥500 sent replies.
- Do we need a `[NAME]` placeholder? It's not in the spec, so it's left out for now.
