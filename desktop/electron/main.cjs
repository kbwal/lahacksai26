const { app, BrowserWindow, ipcMain, shell, Menu } = require("electron");
const path = require("node:path");
const fs = require("node:fs/promises");
const { pathToFileURL } = require("node:url");
const { isAllowedExternalUrl } = require("./policy.cjs");
let window;
let pendingWrite = Promise.resolve();
let closing = false;
const devUrl = process.env.MARGIN_RENDERER_URL;
if (devUrl && !/^http:\/\/127\.0\.0\.1:5173\/?$/.test(devUrl))
  throw new Error("Invalid renderer origin.");
const rendererPath = path.join(__dirname, "..", "dist", "index.html");
function trusted(event) {
  const frame = event.senderFrame;
  const expected = devUrl || pathToFileURL(rendererPath).href;
  if (
    !frame ||
    event.sender !== window?.webContents ||
    frame !== event.sender.mainFrame ||
    frame.url !== expected
  )
    throw new Error("Untrusted IPC sender.");
}
function stateFile() {
  return path.join(app.getPath("userData"), "workspace.json");
}
function createWindow() {
  window = new BrowserWindow({
    width: 1180,
    height: 830,
    minWidth: 740,
    minHeight: 600,
    title: "Margin",
    backgroundColor: "#f7f8f4",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
    },
  });
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-navigate", (event) => event.preventDefault());
  window.webContents.session.setPermissionRequestHandler(
    (_webContents, _permission, callback) => callback(false),
  );
  if (devUrl) window.loadURL(devUrl);
  else window.loadFile(rendererPath);
  window.on("closed", () => {
    window = null;
  });
}
app.setName("Margin");
app.whenReady().then(() => {
  ipcMain.handle("workspace:load", async (event) => {
    trusted(event);
    await pendingWrite;
    try {
      return JSON.parse(await fs.readFile(stateFile(), "utf8"));
    } catch (e) {
      if (e.code === "ENOENT") return null;
      throw new Error(
        "Saved workspace could not be read. Restore a backup or open sample data.",
      );
    }
  });
  ipcMain.handle("workspace:save", (event, value) => {
    trusted(event);
    const json = JSON.stringify(value);
    if (
      !value ||
      value.version !== 1 ||
      typeof json !== "string" ||
      Buffer.byteLength(json) > 10 * 1024 * 1024
    )
      throw new Error("Invalid workspace payload.");
    const write = pendingWrite
      .catch(() => {})
      .then(async () => {
        const file = stateFile();
        await fs.mkdir(path.dirname(file), { recursive: true });
        try {
          await fs.copyFile(file, file + ".bak");
        } catch (e) {
          if (e.code !== "ENOENT") throw e;
        }
        await fs.writeFile(file + ".tmp", json, { mode: 0o600 });
        await fs.rename(file + ".tmp", file);
      });
    pendingWrite = write;
    return write;
  });
  ipcMain.handle("external:open", async (event, url) => {
    trusted(event);
    if (!isAllowedExternalUrl(url))
      throw new Error("External destination is not allowed.");
    await shell.openExternal(url);
  });
  Menu.setApplicationMenu(
    Menu.buildFromTemplate([
      { role: "appMenu" },
      { role: "editMenu" },
      { role: "viewMenu" },
      { role: "windowMenu" },
    ]),
  );
  createWindow();
  app.on("activate", () => {
    if (!BrowserWindow.getAllWindows().length) createWindow();
  });
});
app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
app.on("before-quit", (event) => {
  if (closing) return;
  event.preventDefault();
  pendingWrite
    .catch(() => {})
    .finally(() => {
      closing = true;
      app.quit();
    });
});
