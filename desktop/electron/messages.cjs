const fs = require("node:fs/promises");
const path = require("node:path");
const { runSidecar } = require("./mail.cjs");

function validSource(source) {
  if (!["slack", "discord"].includes(source)) throw new Error("Choose Slack or Discord.");
}
function validId(value) {
  if (typeof value !== "string" || !value || value.length > 320 || /[\r\n\0]/.test(value))
    throw new Error("Choose a saved account and workspace.");
}

// The persona a source drafts in by default: the first one allowed for it, as the inbox shows.
async function personaFor(getWorkspace, source) {
  if (!getWorkspace) return null;
  const roles = (await getWorkspace()).roles || [];
  const role = roles.find(r => !(r.excludedApps || []).includes(source));
  if (!role || !/^[\w-]{1,100}$/.test(role.id))
    throw new Error(`Allow a persona for ${source === "slack" ? "Slack" : "Discord"} in Settings before drafting.`);
  return role.id;
}

function registerMessages({ app, ipcMain, dialog, trusted, getWindow, getWorkspace, worker }) {
  const invoke = worker || ((args) => runSidecar(app, args));
  const busy = new Set();
  const configFile = (source) => path.join(app.getPath("userData"), `messages-${source}.json`);
  async function argsFor(source, dbOverride) {
    validSource(source);
    let db = dbOverride || process.env.MARGIN_MESSAGES_DB;
    if (!db) {
      try { db = JSON.parse(await fs.readFile(configFile(source), "utf8")).db; }
      catch (error) { if (error.code !== "ENOENT") throw new Error("Choose your local messages connection again."); }
    }
    if (!db && !app.isPackaged) db = path.resolve(__dirname, "../../data/messages.db");
    if (db && !path.isAbsolute(db)) throw new Error("The messages database path must be absolute.");
    return ["messages", "--source", source, ...(db ? ["--db", db] : [])];
  }
  async function selectedArgs(source, account, workspace) {
    validId(account); validId(workspace);
    const args = await argsFor(source);
    const saved = await invoke([...args, "connections"]);
    if (!saved.some(c => c.source === source && c.account_id === account && c.workspace_id === workspace))
      throw new Error("This connection is no longer available. Choose it again in Settings.");
    return [...args, "--account", account, "--workspace", workspace];
  }
  ipcMain.handle("messages:connections", async (event, source, choose = false) => {
    trusted(event); validSource(source);
    if (busy.has(source)) throw new Error("Wait for the current sync to finish.");
    if (!choose) return invoke([...(await argsFor(source)), "connections"]);
    const selected = await dialog.showOpenDialog(getWindow(), {
      title: `Use an existing ${source === "slack" ? "Slack" : "Discord"} connection`,
      message: "Choose the messages.db created by your local worker.",
      properties: ["openFile"], filters: [{name: "Messages database", extensions: ["db", "sqlite", "sqlite3"]}],
    });
    if (selected.canceled || !selected.filePaths[0]) return null;
    const db = selected.filePaths[0];
    const result = await invoke([...(await argsFor(source, db)), "connections"]);
    await fs.mkdir(app.getPath("userData"), {recursive: true});
    await fs.writeFile(configFile(source), JSON.stringify({db}), {mode: 0o600});
    return result;
  });
  ipcMain.handle("messages:inbox", async (event, source, account, workspace) => {
    trusted(event);
    return invoke([...(await selectedArgs(source, account, workspace)), "inbox"]);
  });
  ipcMain.handle("messages:sync", async (event, source, account, workspace) => {
    trusted(event); validSource(source);
    if (busy.has(source)) throw new Error("This app is already syncing.");
    busy.add(source);
    try {
      const persona = await personaFor(getWorkspace, source);
      const args = await selectedArgs(source, account, workspace);
      const modelConfig = path.join(app.getPath("userData"), "compose-model.json");
      let modelPath;
      try { modelPath = JSON.parse(await fs.readFile(modelConfig, "utf8")).path; }
      catch (error) { if (error.code !== "ENOENT") throw new Error("Choose your local model again in the composer."); }
      if (!modelPath) {
        const result = await dialog.showOpenDialog(getWindow(), {
          title: "Choose your local model", message: "Select the Qwen3-4B-Base model folder.",
          properties: ["openDirectory"],
        });
        if (result.canceled || !result.filePaths[0]) return null;
        modelPath = result.filePaths[0];
        await fs.access(path.join(modelPath, "config.json"));
        await fs.mkdir(app.getPath("userData"), {recursive: true});
        await fs.writeFile(modelConfig, JSON.stringify({path: modelPath}), {mode: 0o600});
      }
      if (typeof modelPath !== "string" || !path.isAbsolute(modelPath)) throw new Error("Choose an absolute local model folder.");
      // The worker loads <adapters>/<persona> when that persona has been trained.
      await invoke([...args, "sync", "--max-pages", "5", "--suggest-replies",
        "--draft-limit", "3", "--model", modelPath, ...(persona ? ["--persona", persona] : [])]);
      return invoke([...args, "inbox"]);
    } finally { busy.delete(source); }
  });
}
module.exports = { registerMessages };
