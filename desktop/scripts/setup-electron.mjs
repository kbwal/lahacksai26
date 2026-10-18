import { createRequire } from "node:module";
import { spawnSync } from "node:child_process";
import path from "node:path";
const require = createRequire(import.meta.url);
const install = require.resolve("electron/install.js");
const result = spawnSync(process.execPath, [install], {
  stdio: "inherit",
  env: {
    ...process.env,
    electron_config_cache: path.resolve("node_modules/.cache/electron"),
  },
});
if (result.error) throw result.error;
process.exit(result.status ?? 1);
