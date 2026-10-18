This project is a submission for LA AI Hacks 2026. The idea is to make a messaging agent, for email, Slack and other chat apps, that will be two things:

1. Personalized to your writing style (and this will be continually updated)
2. Fully private (i.e., no information leaves the device, and the model is a small local model)

## Model

We LoRA fine-tune a small local model on your past messages (`mlx-lm` on Mac). We'll test two starting points and keep whichever drafts better:

- **Instruct model + LoRA** (e.g. Qwen3 1.7B/4B or Llama 3.2 3B instruct): the safer option, since a few hundred to a few thousand of your messages is enough to learn style but probably not enough to teach a base model instruction-following from scratch.
- **Base model as plain completion** (same sizes, base checkpoints): no instruct-tuning, just continuing the conversation below. Base models may copy your style more faithfully because they don't carry an "assistant" voice.

Compare them on held-out conversations (side by side, plus whether placeholders are emitted correctly). Until we have a real mailbox, we test on one person's mail from the public Enron email dataset.

The model only ever sees a conversation as a chain of messages from you ("me") and from the other side ("them"), oldest first. It doesn't see a recipient, subject line, relationship or channel. That's the same for email threads and Slack DMs, and it is what the model continues:

```
Them: hey are you free tonight?

Me: yeah! what time

Them: 8, the usual

Me:
```

Only your own messages are trained on. The other side's messages are context.

## Personas

People write very differently at work and with friends. Instead of conditioning one model on the relationship, each conversation is tagged with a persona (e.g. `professional`, `social`), and each persona gets its own LoRA adapter. The base model is shared, so an extra persona costs only a small adapter file.

## Writing new messages, not just replies

The agent should also write a message from a request like "email Sarah about getting dinner on Tuesday". To learn that from past data, every conversation that *you* started gets a request prepended to it. The local model writes it from your opening message, as a guess at what you would have typed to get that message written:

```
Request: email sarah about getting dinner on tuesday

Me: hey Sarah! are you free for dinner Tuesday? ...
```

Only conversations that start with your message get a request. A conversation that starts with someone else's message is a reply, and has none.

## Data pipeline

- **Ingestion:** local exports, parsed locally, with no OAuth / API calls: Google Takeout (mbox) for email and a workspace export for Slack. Each source becomes the same thing, a me/them conversation tagged with a persona.
- **Filtering:** drop automated messages before anything else: newsletters and `noreply@` mail (`List-Unsubscribe`, bulk/precedence headers), and bots and system messages in Slack.
- **Cleaning:** strip quoted history ("On Mon, X wrote:") and signatures (e.g. `email-reply-parser` / `talon`) so the model doesn't learn to regurgitate them.
- **Masking (applied to your messages, the training targets):** run your messages through regex/NER and replace hard facts with typed placeholders: `[DATE]`, `[TIME]`, `[AMOUNT]`, `[PHONE]`, `[ADDRESS]`, `[LINK]`. This is what teaches the model to emit placeholders instead of hallucinating "Tuesday works". Without it, the model learns to invent specifics because your real messages contain them. Facts that you named in the request, or that the other side already said, are kept.

## Reply triage

Messages you didn't reply to are used to train a separate "needs reply?" signal (a triage label or lightweight classifier), not mixed into generation. Not replying ≠ "would never reply" (you may have replied in person or forgotten), and after filtering out automated messages this stays a soft signal, shown as "likely no reply needed" rather than hiding messages.

## Drafting + masked tokens

The model drafts replies, and new messages from a request; it never auto-sends.

Important dates / information that are easily hallucinated are masked out. E.g., if a message asks for a time to meet, instead of saying "Tuesday works" without knowing, the model says "[DATE] works!". The user **cannot send** while any placeholder remains. This is enforced by a regex check in the UI as a backstop, independent of what the model outputs.

Placeholders can be filled from local data where possible: e.g. `[DATE]` / `[TIME]` suggest open slots from the local calendar (still on-device).

## Continual learning from edits (IMPORTANT)

When the user edits a draft before sending, we store the pair (model draft, user's final version), tagged with the persona it was drafted with. These are used two ways:

- **SFT** on the final version.
- **DPO** with the model draft as _rejected_ and the user's edit as _chosen_, which is free preference data.

To avoid drift and forgetting from repeatedly training on small batches, each nightly run either retrains each persona's LoRA from scratch on all data, or mixes new edits with a replay sample of older conversations. For the demo, a **"Train now"** button runs a short fine-tune (a few dozen steps) of one persona instead of waiting overnight.

## Privacy notes

- All parsing, training and inference run on-device.
- Known risk: since a persona trains on all of its conversations, a draft to one person could surface details from a conversation with someone else. Masking plus mandatory human review mitigates this. We call it out explicitly.

## Demo plan

- **Airplane mode:** turn off Wi-Fi on stage and draft replies live.
- **Side-by-side:** base model draft vs. personalized draft for the same message, and the same request written by the `professional` vs. `social` persona.
- **Masking:** show a scheduling message → draft with `[DATE]` → send blocked → fill from calendar → send enabled.
- **Learning:** edit a draft, hit "Train now", show the next draft reflect the edit.
