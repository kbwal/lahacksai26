# Gmail to persona training

The Gmail worker's SQLite cache can now feed the conversation trainer directly.
Use the existing authenticated import from [GMAIL_SETUP.md](GMAIL_SETUP.md), then:

```sh
uv run python -m pipeline.gmail --db data/mail.db \
  --adapter-name professional --output data/training/gmail

uv run python -m train.trainer --model models/Qwen3-1.7B \
  --arm instruct --data data/training/gmail/conversations.jsonl \
  --edits data/training/gmail/edits.jsonl --adapter-root adapters/instruct
```

Add `--account you@example.com` if the database contains multiple accounts.
The output directory must be new; existing exports are never overwritten.
It contains private `conversations.jsonl`, `edits.jsonl`, and `manifest.json`
files, with owner-only permissions. Keep them under the ignored `data/` directory.
The exporter opens the database read-only, reads a consistent snapshot, and does
not contact Gmail, generate drafts, or send messages.

`--adapter-name` explicitly assigns the exported history to that persona. It does
not infer relationships. To classify mixed history, review the labels or run
`train.augment --relabel` with a local instruct model before training. Existing
feedback retains the persona used to generate the original draft; the legacy
`default` name uses the requested export persona. Desktop personas and trained
adapters still require explicit configuration in the worker.

The exporter drops draft, spam, trash, automated and empty messages. Only messages
with Gmail's `SENT` label become training targets. It strips common quoted tails,
quoted lines and standard `-- ` signatures; unusual signatures and multilingual
quote formats still need dataset review. The trainer masks facts in targets.
Source/account/thread IDs remain attached to every row.

The newest 10% of eligible threads, ordered by their last sent message, become
`type: test`; at least one thread remains for training. All edit pairs follow
their thread's split, including when their persona differs. Saved generation
context is used for feedback, so later messages cannot leak into its prompt.
Use `--test-fraction 0` to disable holdout. Re-exporting after new mail can move
the temporal boundary; use a fixed export for comparable experiments.

`manifest.json` reports imported coverage, filtering counts and split sizes.
An incomplete backfill is reported as such. No eligible sent messages is an error,
not a synthetic replacement. An empty edit file is valid until reviewed drafts
have actually been sent and reconciled. Slack and Discord exports remain
`usage: context_only` and are rejected by the trainer.

## Verified integration

On October 18, 2026, the existing Gmail connection answered a read-only API profile
request. The completed local import contained 154 messages. Export produced 17
conversations: 15 training threads, 2 test threads, and no sent-edit pairs yet.
No bootstrap data was needed. This establishes the data handoff, not sufficient
data or model quality for a trained persona. Model weights and actual training
quality are separate from export validation.

The project recommends Qwen3-1.7B for fine-tuning; the existing desktop defaults
to Qwen3.5-2B for generation. Always load an adapter with the exact base model used
to train it. The optional Veri submission path uploads text and bills GPU time;
the local workflow above does neither.
