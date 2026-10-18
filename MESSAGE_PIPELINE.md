# Gmail, Slack and Discord message pipeline

This implementation adds read-only Slack and Discord ingestion and local reply suggestions
to the macOS worker.
It imports historical messages and polls selected conversations, retaining human-authored
messages from the connected person as well as other participants. Suggestions are generated
locally for the application's review/copy interface; no Slack/Discord send endpoint is
exposed. Gmail remains responsible for native email drafts and matching
generated drafts to the human's sent reply.

## Common data contract

Every message exported to the application has an explicit `source` enum:
`gmail`, `slack`, or `discord`. Provider IDs remain strings, including Slack timestamps
and Discord snowflakes; do not convert them to floating-point numbers.

```json
{
  "schema_version": 1,
  "source": "slack",
  "account_id": "U123",
  "workspace_id": "T123",
  "channel_id": "C123",
  "thread_id": "1792240000.000001",
  "message_id": "1792240060.000002",
  "author_id": "U123",
  "timestamp_ms": 1792240060000,
  "text": "Thanks, I will check the proposal.",
  "is_self": true,
  "is_bot": false,
  "reply_to_id": "1792240000.000001",
  "usage": "context_only"
}
```

Message identity is `(source, account_id, workspace_id, channel_id, message_id)`.
Conversation identity is `(source, account_id, workspace_id, channel_id, thread_id)`.
The application must keep all these fields when joining context or feedback. Identical
IDs from two providers, workspaces, or accounts cannot overwrite each other.

| Source | Account/workspace | Conversation mapping | Human-authored replies |
| --- | --- | --- | --- |
| Gmail | Email address / empty workspace | Gmail `threadId`; channel is `mailbox` | `SENT` or matching From address |
| Slack | Authorized user ID / team ID | `thread_ts`, otherwise the root message's `ts` | Matching Slack user ID |
| Discord | Selected human user ID / guild ID | Native thread channel ID; ordinary channels are channel streams | Matching configured human user ID; bot flag retained |

Slack unthreaded messages remain independent roots: we do not guess which nearby message
they answer. Discord direct reply references remain in `reply_to_id`; they are not
silently treated as Gmail threads. Native Discord threads must be explicitly selected by
their own channel IDs. No server-wide discovery or personal DM scraping is performed.

Only text is imported; attachment bytes, reactions and rich embeds are outside this version.
Slack text retains its native mention/link markup. Bots are flagged, not presented as the
human author. Build training targets only from suitable human replies after filtering.

## Storage, sync and application boundary

```text
Gmail API -> gmail_agent -> mail.db ------> normalized Gmail message export
Slack API -----------\
                      message_pipeline -> messages.db -> normalized chat message export
Discord bot API -----/                       |
                                        per-channel cursor + health
```

Gmail's existing database and sync cursor stay authoritative. Its normalized export reads
the existing database without an API call or schema migration and excludes unsent drafts.
Existing Gmail feedback and generation context now include `source: "gmail"`; legacy
feedback acquires that label on export. The shared Pair prompt starts with `Source: ...`
in both model arms. Older Pair JSON defaults to Gmail. This changes prompt tokens: retrain
or reevaluate existing adapters before comparing them against newly trained adapters.

Slack/Discord use a separate `data/messages.db` in the same app home, with connections,
selected channels, messages and suggestions. Credentials stay in macOS Keychain. Each page and its
next cursor commit together. Replaying a page updates the same message instead of adding
a duplicate. A process lock prevents concurrent sync/disconnect for the same connection.
Status and exception output omit message text and tokens.
Channel-local provider/validation failures appear in `status.channels[].last_error` and
do not block other selected channels or their reply reconciliation. Identity, network and
rate-limit failures still apply to the connection. Automatic suggestions skip channels
with a recorded sync failure until their next successful page.

`sync` performs a bounded scan, resuming any incomplete one. `run` repeats it, waiting
1,800 seconds after each cycle by default. Slack follows each completed scan with a new
history scan, so old threads are revisited even when their new replies are absent
from top-level history. This also refreshes edited text. Slack missing records are removed
only after a complete history-and-replies scan. A failed page never advances its cursor
or triggers removal. Expired Slack cursors restart the scan without discarding cached data.

Discord uses a frozen backward scan for its initial import, then reads forward with
`after` from the newest cached message timestamp. Replaying the final millisecond avoids
losing messages whose snowflakes share that timestamp; pending pages retain exact IDs.
Once initial import finishes, cycles fetch new messages rather than rereading all history.
Edits to older Discord messages are not refreshed by these incremental reads.

This first version favors correctness for a small channel allowlist over high-volume
efficiency. It is **not a guarantee that every message arrives locally within 30 minutes**:
large imports and rate limits can take multiple cycles, and messages newer than an initial
Discord import or Slack scan's starting boundary enter the next scan. `--max-pages` is a
per-channel cycle budget; status exposes `scan_in_progress` and `last_complete_at`.
Production-scale ingestion should
add Slack Events API / Discord Gateway create, edit and delete events with periodic
reconciliation. Discord REST can return an empty history when permission is lost, so this
version does not infer Discord deletions from absence; cached deleted messages can remain
until explicitly purged. Do not use this cache as a live permissions or retention authority.

HTTP 429 delays persist in SQLite as `retry_at`; subsequent calls defer until that time.
The worker never sleeps inside a provider request to retry it. Slack's published limits
for some commercial non-Marketplace apps are 1 request/minute and 15 items for history
and replies; this version uses 15-item Slack pages and honors server backoff. Internal
apps have different limits. A broadly shipped app will need distribution and rate-limit
planning, not just a different polling interval.

## Connect Slack

Create/install a Slack app approved for the workspace. Use a **user OAuth token** for the
person whose writing is being collected, with the history scopes for selected conversation
types (`channels:history`, `groups:history`, `im:history`, `mpim:history`) and the matching
read scopes for `conversations.info` (`channels:read`, `groups:read`, `im:read`, `mpim:read`).
Request only conversation types you intend to select. Workspace policy and retention
still limit what the token can access.

```bash
# Prompts for the existing token without echoing or placing it in shell history.
uv run python -m gmail_agent messages --source slack connect --channels C123 C456
# connect returns account_id and workspace_id. Use those exact IDs below.
uv run python -m gmail_agent messages --source slack --account U123 --workspace T123 sync
uv run python -m gmail_agent messages --source slack --account U123 --workspace T123 run
```

For an application-owned credential handoff, `connect --token-env VARIABLE_NAME` reads
an existing environment variable. The argument is the variable name, never the token.
Reconnecting validates identity and **replaces** the selected channel list. Deselected
channels stop syncing and disappear from suggestion output, while their cached messages
remain available to export until purge. Select them again to resume their saved cursors.
Status includes all retained channels with a `selected` flag. Existing databases acquire
the new selection/error columns automatically without losing messages or checkpoints.

This is token-based developer onboarding, **not a finished browser OAuth flow**. For the
shipped app, implement Slack OAuth installation, state verification, an HTTPS redirect
and a server-side code exchange; do not embed a Slack client secret in the desktop bundle.
The returned user token can then enter this Keychain interface. Token refresh/rotation is
also application work; expired tokens currently require reconnecting.

## Connect Discord

Use a Discord application bot installed in an explicitly chosen server. Enable/obtain
approval for Message Content intent. Grant View Channel and Read Message History on the
selected text channels/threads. Set `--account` to the **human user's ID**, not the bot ID,
and `--workspace` to the server ID. Those IDs are not a Discord user login: they identify
the author and allowed server. Discord user tokens/self-bots are unsupported.

```bash
uv run python -m gmail_agent messages --source discord \
  --account 123456789012345678 --workspace 234567890123456789 \
  connect --channels 345678901234567890

uv run python -m gmail_agent messages --source discord \
  --account 123456789012345678 --workspace 234567890123456789 run
```

The future app flow needs bot installation and a channel picker. Normal Discord OAuth
does not grant access to all of a person's private conversations. The current connector
checks the bot identity, content-intent flags, channel type and guild membership of the
channel. Permission changes can still reduce returned history; check the server's bot
permissions if a scan unexpectedly returns no messages.

## Suggested replies and human feedback

```bash
# Sync, then draft for new incoming messages in the selected conversations.
uv run python -m gmail_agent messages --source slack --account U123 --workspace T123 \
  run --suggest-replies --model models/Qwen3.5-2B

# Explicitly draft for one cached thread, including historical conversations.
uv run python -m gmail_agent messages --source slack --account U123 --workspace T123 \
  suggest --channel C123 --thread 1792240000.000001 --model models/Qwen3.5-2B

# JSON for the app's review/copy interface; includes generated text and context.
uv run python -m gmail_agent messages --source slack --account U123 --workspace T123 suggestions
uv run python -m gmail_agent messages --source slack --account U123 --workspace T123 dismiss 1
```

The same commands work with Discord selectors; its `--thread` is the selected channel or
native thread channel ID. `sync --suggest-replies` runs one cycle. Model arguments also
support `--adapter-path`, `--adapter-name`, `--arm` and `--model-lock`, sharing the same
MLX lock as Gmail drafting and training. A busy model leaves work queued for retry.

Automatic suggestions only consider messages at or after the connection's activation
time. Historical imports therefore do not trigger replies to old conversations. Explicit
`suggest` allows an older cached thread. Suggestions use the last three non-bot, nonempty
messages and carry the source label into the model prompt. No suggestion is generated if
the latest human message is from the connected person. There is no semantic relevance
classifier yet: selecting a busy public channel can generate suggestions for messages
not directed at the user. Select only conversations where assistance is wanted.
Suggestions reflect the locally cached context, including during a partially completed
scan. The app should show sync status beside the suggestion and encourage review of the
current provider conversation before the person sends a reply.

Suggestions persist with `queued`, `ready`, `replied`, `superseded`, `dismissed` or `failed`
states. The app should present only `ready` suggestions for use. Repeating a cycle does
not duplicate a suggestion for the same incoming message. A newer incoming message
supersedes the earlier suggestion, and changed cached context regenerates machine text.
The app must keep any human-edited composition separately rather than treating this
machine suggestion store as an editable draft. Dismissal prevents regeneration for the
same trigger. Source/channel/thread IDs accompany every suggestion.
One failed generation is recorded and does not stop attempts for later conversations;
failed rows retry next cycle. The result includes `generated` and `failed` counts. If all
attempts fail, the first error still surfaces. Suggestion generation fetches only the last
three human messages per eligible conversation, and reconciliation queries only the latest
human message and the first qualifying later reply.

After sync observes a later human-authored reply in the same conversation, it records
`observed_reply_id` and returns that message with the suggestion. This is a conversation
association, not proof the suggestion was used: `suggestion_use_confirmed` remains false.
This distinction matters especially for ordinary Discord channel streams, where a later
message can be unrelated. Feedback remains `usage: "context_only"`; no automatic training
or provider-side sending occurs. The final app still needs its review/copy UI and explicit
user acceptance signals if it wants stronger feedback attribution.

## Export and disconnect

```bash
uv run python -m gmail_agent messages --source slack --account U123 --workspace T123 \
  export --output data/slack-context.jsonl
uv run python -m gmail_agent messages --source gmail --account me@example.com \
  export --gmail-db data/mail.db --output data/gmail-context.jsonl
uv run python -m gmail_agent messages --source slack --account U123 --workspace T123 status
uv run python -m gmail_agent messages --source slack --account U123 --workspace T123 disconnect
```

Exports are local JSONL files with mode 0600; existing output files are never overwritten.
Use the same export command with Discord's IDs for Discord. Because every row has `source`
and scoped IDs, downstream code can combine exports without losing origin. These exports
are context records, not prepared SFT/DPO pairs. They carry `usage: "context_only"`, and the
current SFT loader rejects them. Discord's developer policy prohibits training on API
message content without Discord's express permission. Do not strip that marker to route
chat exports into training. Slack training use also needs a separate policy/consent review.

Disconnect removes the local Keychain entry. `disconnect --purge` also deletes that
connection's cached messages and checkpoints; separately exported files remain. It does
not revoke the token or uninstall the app at the provider. Revoke/uninstall there when
needed. A running polling process checks Keychain under the sync lock before each cycle.
If the credential or connection is removed, `run` exits at that check with a nonzero status
instead of retrying forever; this applies with and without `--purge`.

## Validation and remaining work

Automated tests use synthetic provider responses for pagination, restart recovery,
429 backoff, channel/account isolation, source labels, text edits and export permissions.
Regression coverage includes isolated channel/model failures, disconnect shutdown, channel
selection migration, incremental Discord cursor recovery, and bounded context reads.
All 144 repository tests pass. The rebuilt, relocated macOS executable also generated
real local Qwen3.5-2B suggestions for synthetic Slack and Discord conversations, preserving
the source label in each suggestion and its context.
No live Slack workspace or Discord server has been connected or imported in this change.
End-to-end credential and permission checks need authorized app installations. Local
suggestion generation and conversation-based human reply observation are implemented and
tested with synthetic fixtures. The desktop review/copy UI, native provider draft surfaces,
event subscriptions and production OAuth onboarding remain application work.

## Provider references

- [Slack history](https://docs.slack.dev/reference/methods/conversations.history/)
- [Slack thread replies](https://docs.slack.dev/reference/methods/conversations.replies/)
- [Slack conversation info](https://docs.slack.dev/reference/methods/conversations.info/)
- [Discord message API](https://docs.discord.com/developers/resources/message)
- [Discord intent documentation](https://docs.discord.com/developers/events/gateway#message-content-intent)
- [Discord developer policy](https://support-dev.discord.com/hc/en-us/articles/8563934450327-Discord-Developer-Policy)
