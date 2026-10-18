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

The initial workspace uses clearly labeled sample data. Role editing, per-role app exceptions, preferences, draft browsing, and [TIME] insertion are implemented and saved locally. Gmail/Slack/Outlook authentication, live draft synchronization, automatic reply detection and training are integration work; the dashboard does not simulate those actions as real account operations.

Python model development remains at the repository root (`uv sync`, `train/`). Model weights, user mail, adapters and desktop build artifacts are ignored by Git.
