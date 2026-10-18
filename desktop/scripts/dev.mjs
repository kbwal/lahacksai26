import { createServer } from "vite";
import { spawn } from "node:child_process";
import electron from "electron";
const server = await createServer();
await server.listen();
server.printUrls();
const child = spawn(electron, ["."], {
  stdio: "inherit",
  env: { ...process.env, MARGIN_RENDERER_URL: "http://127.0.0.1:5173/" },
});
let stopping = false;
async function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  child.kill();
  await server.close();
  process.exit(code);
}
child.on("exit", (code) => void stop(code ?? 1));
child.on("error", (error) => {
  console.error(error);
  void stop(1);
});
process.on("SIGINT", () => void stop());
process.on("SIGTERM", () => void stop());
