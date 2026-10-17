This project is a submission for LA AI Hacks 2026. The idea is to make an email agent that will be two things:

1. Personalized to your writing style (and this will be continually updated)
2. Fully private (i.e., no information leaves the device, and the model is a small local model)

## Model

We LoRA fine-tune a small local model on your past emails (`mlx-lm` on Mac). We'll test two starting points and keep whichever drafts better:

- **Instruct model + LoRA** (e.g. Qwen3 1.7B/4B or Llama 3.2 3B instruct): the safer option, since a few hundred to a few thousand sent emails is enough to learn style but probably not enough to teach a base model instruction-following from scratch.
- **Base model as plain completion** (same sizes, base checkpoints): no instruct-tuning, just continuing the prompt below. Base models may copy your style more faithfully because they don't carry an "assistant" voice.

Compare them on held-out sent emails (side by side, plus whether placeholders are emitted correctly).

The prompt is conditioned on the recipient and relationship (e.g. boss, friend, professor), since people write very differently to different people:

```
To: <recipient> (<relationship>)
Thread: <cleaned thread>
Reply:
```

## Data pipeline

- **Ingestion:** Google Takeout (mbox) export, parsed locally. No OAuth / API calls needed.
- **Filtering:** drop automated mail (`List-Unsubscribe`, `noreply@`, bulk/precedence headers) before anything else.
- **Cleaning:** strip quoted history ("On Mon, X wrote:") and signatures (e.g. `email-reply-parser` / `talon`) so the model doesn't learn to regurgitate them.
- **Masking (applied to training targets):** run your sent replies through regex/NER and replace hard facts with typed placeholders: `[DATE]`, `[TIME]`, `[AMOUNT]`, `[PHONE]`, `[ADDRESS]`, `[LINK]`. This is what teaches the model to emit placeholders instead of hallucinating "Tuesday works". Without it, the model learns to invent specifics because your real emails contain them.

## Reply triage

Emails you didn't reply to are used to train a separate "needs reply?" signal (a triage label or lightweight classifier), not mixed into generation. Not replying by email ≠ "would never reply" (you may have replied in person or forgotten), and after filtering out automated mail this stays a soft signal, shown as "likely no reply needed" rather than hiding emails.

## Drafting + masked tokens

The model drafts replies; it never auto-sends.

Important dates / information that are easily hallucinated are masked out. E.g., if an email asks for a time to meet, instead of saying "Tuesday works" without knowing, the model says "[DATE] works!". The user **cannot send** while any placeholder remains. This is enforced by a regex check in the UI as a backstop, independent of what the model outputs.

Placeholders can be filled from local data where possible: e.g. `[DATE]` / `[TIME]` suggest open slots from the local calendar (still on-device).

## Continual learning from edits (IMPORTANT)

When the user edits a draft before sending, we store the pair (model draft, user's final version). These are used two ways:

- **SFT** on the final version.
- **DPO** with the model draft as _rejected_ and the user's edit as _chosen_, which is free preference data.

To avoid drift and forgetting from repeatedly training on small batches, each nightly run either retrains the LoRA from scratch on all data, or mixes new edits with a replay sample of older emails. For the demo, a **"Train now"** button runs a short fine-tune (a few dozen steps) instead of waiting overnight.

## Privacy notes

- All parsing, training and inference run on-device.
- Known risk: since the model trains on all your mail, a draft to one person could surface details from a thread with someone else. Masking plus mandatory human review mitigates this. We call it out explicitly.

## Demo plan

- **Airplane mode:** turn off Wi-Fi on stage and draft replies live.
- **Side-by-side:** base model draft vs. personalized draft for the same email.
- **Masking:** show a scheduling email → draft with `[DATE]` → send blocked → fill from calendar → send enabled.
- **Learning:** edit a draft, hit "Train now", show the next draft reflect the edit.
