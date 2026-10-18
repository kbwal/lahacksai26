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

- Left navigation filters drafts by All apps, Gmail, Slack or Outlook.
- App headers show only the roles allowed for that app.
- Roles are based on relationships, not platforms. Professional, Social and Academic are defaults.
- New roles allow every app. Existing roles can opt out of individual apps.
- Existing drafts remain visible after a role excludes their app.
- Draft previews display the role, incoming context, text and unresolved placeholders.
- `[TIME]` opens an accessible date/time dialog with quick choices and a sentence preview. It replaces all time placeholders in that local draft, preserving other placeholders. Filled times remain clickable and can be corrected after reopening the app.
- Settings persist automatic-drafting and edit-learning preferences. Preferences are not sent to a backend yet.
- Roles, preferences and local draft changes persist across restarts. Native files are saved atomically, with the previous file retained as `workspace.json.bak`.
- Draft buttons open supplied official app URLs, or the app's drafts/home page when no exact link exists.
- JSON snapshots can be imported in Settings with structural validation and an explicit replacement confirmation.

## Integration handoff

The Python training modules are intentionally unchanged. There is no Gmail/Slack OAuth or inference server in this change. The UI starts with a **Sample workspace** label; its connection controls only toggle sample state. Imported workspaces use **Local workspace** and account controls are connector-managed.

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

`src/lib/storage.ts` is the persistence adapter boundary. Replace or extend it with the local service client once the infra endpoints are agreed. Send role/preference mutations to the service at that boundary and send edited draft text through the original app's draft-update integration. Until then, time edits explicitly say **Updated locally** and do not modify Gmail or Slack.

The fine-tuned model decides whether a message needs a reply. Only include resulting auto-drafts in the snapshot; manual drafting can create the same `Draft` record. The UI does not infer readiness from an arbitrary sample-count threshold: the training service provides `trainingReady` after filtering and quality checks. Three short messages must not become a trained persona just because a user created a role.

For automatic drafting, an integration should honor both the connected account and the role's exclusion policy. Changing policy does not erase previously trained information; retraining is needed for that. Gmail/Slack send guards remain the extension/server's responsibility. The dashboard recognizes arbitrary uppercase bracketed tokens, including model-invented types, and never sends messages.

## Storage and native boundary

Electron saves to its `app.getPath('userData')/workspace.json` (normally `~/Library/Application Support/Margin/workspace.json`). Browser preview uses origin-local storage. Snapshot data is not uploaded. Account tokens are not collected or persisted by this UI.

The native shell uses context isolation, renderer sandboxing, disabled Node integration, fixed IPC methods, main-frame sender checks, a restrictive CSP, denied permissions and an external URL allowlist. External content never loads inside the privileged app window.

### Optional local time-edit metadata

`Draft.timeEdit` keeps `{ template, choice: { date, time }, rendered }` so a locally inserted time can be changed without replacing unrelated times in the message. Snapshot validation checks it against `body`. If an integration changes the body, it must remove or regenerate this metadata. Older snapshots without it remain supported.

Role forms reject blank names/descriptions and duplicate names (ignoring case and surrounding spaces). Modal dialogs focus the relevant input when opened.
