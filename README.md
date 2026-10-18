# Margin — LA AI Hacks 2026

A personal writing assistant that learns different voices for professional, social and academic conversations. Models, training data and adapters stay local.

- [Product specification](PROJECT.md)
- [Model and infrastructure plan](PLAN.md)
- [Desktop dashboard setup and integration contract](desktop/README.md)

## Dashboard

Use Node.js 24 LTS and npm:

```sh
cd desktop
npm ci
npm run dev
```

This opens the React dashboard in a native Electron window. The first run downloads Electron if needed. To preview in a browser instead, run `npm run dev:web`.

```sh
npm test            # Domain and import-boundary checks
npm run build       # Type-check and produce the renderer bundle
npm run package     # Create a development Mac .app in release/
```

The initial workspace uses clearly labeled sample data. Gmail loads owned drafts from the local worker and supports reviewed sends. Slack and Discord load source-labelled reply suggestions from saved worker connections, with manual sync, local detail editing, and copy/open actions. First-time Slack/Discord setup still uses the [message pipeline CLI](MESSAGE_PIPELINE.md); OAuth onboarding and Outlook integration remain future work. Personas, preferences, and draft edits are saved locally.

Python model development remains at the repository root (`uv sync`, `train/`). Model weights, user mail, adapters and desktop build artifacts are ignored by Git.
