# Margin desktop dashboard

React + TypeScript + Vite in a sandboxed Electron shell. The same UI also runs in a browser for development and can be reused by the planned extension.

## Run

Use **Node.js 24 LTS** (or another version supported by `package.json`).

```sh
cd desktop
npm ci
npm run dev
```

`npm run dev` starts Vite at `http://127.0.0.1:5173` and opens Electron. `npm run dev:web` starts only the browser UI. `npm run setup:electron` installs the native runtime explicitly; `dev` and `start` do this automatically when it is missing.

```sh
npm test
npm run build
npm start
npm run package
```

The package command produces `release/Margin-darwin-arm64/Margin.app` on Apple Silicon (or `Margin-darwin-x64/Margin.app` on Intel). It is an ad-hoc signed development build, not a notarized distribution. The package command signs and verifies the local bundle. Build outputs and dependencies are not committed.

## Implemented

- Left navigation filters drafts by All apps, Gmail, Slack or Outlook, with consistent app colors. Inbox search matches people, addresses, message/context text, roles and app names within the selected app. Empty results have a clear-search action.
- App headers show only the roles allowed for that app.
- Roles are based on relationships, not platforms. Professional and Social are defaults.
- New roles allow every app. Existing roles can opt out of individual apps.
- Existing drafts remain visible after a role excludes their app.
- Draft previews display the role, incoming context, text and unresolved placeholders.
- Model placeholders open an accessible detail editor with a preview in the actual reply. `[DATE]`, `[TIME]`, `[AMOUNT]`, `[PHONE]`, `[ADDRESS]`, and `[LINK]` follow the types in the train branch’s `train/mask.py`; custom uppercase placeholders get a text editor. Each occurrence is filled independently, stays editable after reopening, and blocks sending until resolved. Existing literal details are preserved.
- Automatic drafting is always on; Settings has no off switch. Older snapshots with automatic drafting disabled are normalized to enabled. The older preference fields remain in snapshot version 1 for compatibility.
- Roles, preferences and local draft changes persist across restarts. Native files are saved atomically, with the previous file retained as `workspace.json.bak`.
- Draft buttons open supplied official app URLs, or the app's drafts/home page when no exact link exists.
- JSON snapshots can be imported in Settings with structural validation and an explicit replacement confirmation.

## Integration handoff

The `infra` branch Gmail worker is included. Training modules are unchanged. Gmail uses the native worker and macOS Keychain; no HTTP server or renderer tokens are needed. Slack and Outlook sending are not implemented. The UI starts with a **Sample workspace** label; its connection controls only toggle sample state. Imported workspaces use **Local workspace** and account controls are connector-managed.

The shared contract is [`src/types.ts`](src/types.ts). Import [`examples/sample-workspace.json`](examples/sample-workspace.json) through Settings to exercise the boundary. The service should supply:

```ts
interface Workspace {
  version: 1;
  mode: 'local';
  connections: { id: 'gmail' | 'slack' | 'outlook'; name: string; connected: boolean }[];
  roles: {
    id: string; name: string; description: string; kind: 'default' | 'custom';
    excludedApps: ('gmail' | 'slack' | 'outlook')[];
    usableExamples: number; trainingReady: boolean;
  }[];
  drafts: {
    id: string; appId: 'gmail' | 'slack' | 'outlook'; roleId: string;
    subject: string; recipient: string; address: string; context: string;
    body: string; updatedAt: string; externalUrl?: string; localChanges?: boolean;
  }[];
  preferences: { automaticDrafts: boolean; learnFromEdits: boolean };
}
```

All three connection records must be present, including disconnected integrations. IDs must be unique; each draft must refer to a known role. Inputs are bounded to 30 roles, 500 drafts, and a 10 MB file. Official Gmail, Slack and Outlook HTTPS links are allowed; other URLs are rejected in the renderer and native shell.

`src/lib/storage.ts` remains the local workspace persistence boundary. Native Gmail IPC invokes the infra sidecar for account discovery, current drafts and explicit sends. Role/preferences are still local. Time edits say **Updated locally** until sent: sending includes them in the outgoing text, while opening the original app shows the version saved there. Slack draft updates and sending still need their connector.

The fine-tuned model decides whether a message needs a reply. Only include resulting auto-drafts in the snapshot; manual drafting can create the same `Draft` record. The UI does not infer readiness from an arbitrary sample-count threshold: the training service provides `trainingReady` after filtering and quality checks. Three short messages must not become a trained persona just because a user created a role.

For automatic drafting, an integration should honor both the connected account and the role's exclusion policy. Changing policy does not erase previously trained information; retraining is needed for that. Gmail/Slack send guards remain the extension/server's responsibility. The dashboard recognizes arbitrary uppercase bracketed tokens, including model-invented types, and blocks sending until every placeholder is filled.

## Storage and native boundary

Electron saves to its `app.getPath('userData')/workspace.json` (normally `~/Library/Application Support/Margin/workspace.json`). Browser preview uses origin-local storage. Snapshot data is not uploaded. Account tokens are not collected or persisted by this UI.

The native shell uses context isolation, renderer sandboxing, disabled Node integration, fixed IPC methods, main-frame sender checks, a restrictive CSP, denied permissions and an external URL allowlist. External content never loads inside the privileged app window.

### Optional local detail-edit metadata

`Draft.placeholderEdits` keeps `{ template, values }`, where values use zero-based placeholder occurrence keys and store `{ value, input? }`. Raw date/time input supports reopening the picker. Snapshot validation checks the rendered template against `body`. If an integration changes the body, it must remove or regenerate this metadata. Older snapshots, including legacy `Draft.timeEdit` records, remain supported and migrate when a detail is edited.

Role forms reject blank names/descriptions and duplicate names (ignoring case and surrounding spaces). Modal dialogs focus the relevant input when opened.

## Inbox search and Gmail sending

Build the sidecar before packaging the Electron app. The package command embeds the complete `dist/lahacks-mail-worker` directory as a resource, including its interpreter and libraries. See `DESKTOP_PACKAGING.md` for the locked sidecar build. Development uses `build/macos-venv/bin/python` with a fixed entrypoint; there is no shell interpolation.

In Settings, choose **Use existing connection…** and select the `mail.db` used by the infra worker. Then **Load Gmail drafts**, selecting an account if multiple are connected. The default bundled data home is the infra worker's Application Support directory; `MARGIN_MAIL_DB` can supply an absolute existing database path. Existing OAuth credentials remain in Keychain. No sample drafts can be sent. Loading real Gmail data replaces sample drafts and clears sample training-readiness claims.

Only app-owned, published Gmail drafts are listed for sending. **Send** opens a native review showing the sender, recipient, subject and full body. Cancel makes no provider call. After approval the worker refetches the Gmail draft and rejects a changed snapshot or different account/thread. It sends the reviewed local text using [Gmail's native draft send API](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.drafts/send), preserving reply headers. Attachments, Cc/Bcc and ambiguous recipients must be handled in Gmail. Time edits in Margin are included in the outgoing text.

A durable SQLite journal claims each draft before the provider call. Sends are never automatically retried; timeout, server error or incomplete acknowledgement remains uncertain and blocks replay even after restarting the app. A definitive rejecting response permits a later explicit retry. The UI removes a draft only after Gmail acknowledges delivery. Successful sends record the final text for infra's existing feedback pipeline. Scheduled generation continues to create drafts only and never calls the desktop send path.

Tests use fake Gmail clients and isolated databases; they do not read credentials or send real mail. Live OAuth/send testing requires an explicitly reviewed real message. Browser preview and sample data show the send control disabled; use the Mac app with an existing Gmail connection for delivery.

## New messages and personas

Professional and Social are the default personas. Loading an older workspace removes the Academic default and assigns its drafts to Professional (or the first remaining persona), preserving the messages. Custom personas remain supported. Serialized `roles` and `roleId` keys are retained for compatibility with existing snapshots and the infra branch.

**Draft a message** opens an app selector, allowed-persona selector, optional recipient/subject, and message prompt. The native app asks for a local instruct model folder on first use (choose `models/Qwen3.5-2B`, not `Qwen3.5-2B-Base`). This choice is saved on the device; the composer can choose another model later. Generation runs through the bundled MLX worker using the shared model lock, without accessing Gmail or sending anything. It uses the persona description for style; trained adapters are not yet connected to this composer.

New-message drafts persist in the inbox and survive Gmail refresh. Fill any highlighted details and use **Copy draft** to move the text into the destination app. Sending new messages from Margin is not connected; the existing Send action is for owned Gmail reply drafts only. The browser preview displays the composer but local generation requires the Mac app.

## Saved personal details and prompt composer

Settings → **Your details** stores name, email, phone, address, and website locally, with an explicit **Automatically fill my details** toggle. When enabled, it fills matching unresolved placeholders in existing drafts, loaded snapshots, refreshed Gmail replies, and newly generated messages. Existing inserted values are preserved and remain editable; changing a saved value only affects unresolved fields. Disabling automatic filling does not remove earlier insertions.

Names recognize `[NAME]`, `[YOUR NAME]`, `[FULL_NAME]`, and `[SENDER NAME]`; email/phone/address recognize their corresponding named fields, and websites fill `[WEBSITE]` or `[PERSONAL WEBSITE]`. Generic `[LINK]` fields remain unresolved because they may refer to a meeting instead of a personal website. Dates, times, amounts, and unknown fields stay for review. Autofill never sends a message. Old snapshots without `personalDetails` remain valid.

The new-message composer puts a persona picker above a prompt box, with an app chip and arrow submit action. **Message details** expands optional recipient, subject, and model controls. Cmd/Ctrl+Enter submits. Changing personas preserves the prompt; the picker lists only personas allowed for the selected app. Generation still uses the existing local worker and the selected persona’s description.
