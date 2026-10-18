const { spawn } = require("node:child_process");
const fs = require("node:fs/promises");
const path = require("node:path");
const { existsSync } = require("node:fs");

function runWorker(executable, args, input) {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, args, {
      shell: false,
      stdio: ["pipe", "pipe", "pipe"],
    });
    let output = "",
      errors = "",
      done = false;
    const finish = (error, value) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      error ? reject(error) : resolve(value);
    };
    const timer = setTimeout(() => {
      child.kill();
      finish(
        new Error(
          "The local worker timed out. Check the original app before retrying a send.",
        ),
      );
    }, 300000);
    child.on("error", () =>
      finish(new Error("The local worker could not start.")),
    );
    child.stdin.on("error", () => {});
    child.stdout.on("data", (data) => {
      output += data;
      if (Buffer.byteLength(output) > 16 * 1024 * 1024) {
        child.kill();
        finish(new Error("The local inbox is too large."));
      }
    });
    child.stderr.on("data", (data) => {
      errors = (errors + data).slice(-4000);
    });
    child.on("close", (code) => {
      if (code !== 0)
        return finish(
          new Error(
            errors
              .split("\n")
              .find((line) => line.startsWith("Error: "))
              ?.slice(7) ||
              "The local worker could not complete this request. Check your connection and refresh.",
          ),
        );
      try {
        finish(null, JSON.parse(output));
      } catch {
        finish(new Error("The local worker returned an invalid response."));
      }
    });
    child.stdin.end(input ? JSON.stringify(input) : "");
  });
}

function runSidecar(app, args, input) {
  const root = path.resolve(__dirname, "..", "..");
  const executable = app.isPackaged
    ? path.join(process.resourcesPath, "lahacks-mail-worker", "lahacks-mail-worker")
    : path.join(root, "build", "macos-venv", "bin", "python");
  if (!existsSync(executable))
    throw new Error("The local worker is not installed. Build the mail sidecar first.");
  return runWorker(executable, app.isPackaged ? args : [
    path.join(root, "desktop", "electron", "mail-entry.py"), ...args,
  ], input);
}

function createWorkerClient(app) {
  const configFile = path.join(
    app.getPath("userData"),
    "gmail-connection.json",
  );
  async function invoke(command, account, input) {
    let db = process.env.MARGIN_MAIL_DB;
    if (!db) {
      try {
        db = JSON.parse(await fs.readFile(configFile, "utf8")).db;
      } catch {}
    }
    if (db && !path.isAbsolute(db))
      throw new Error("The Gmail database path must be absolute.");
    const root = path.resolve(__dirname, "..", "..");
    const args = [];
    // A development worker's default paths are relative to cwd; always resolve them here.
    if (!db && !app.isPackaged) db = path.join(root, "data", "mail.db");
    if (db) args.push("--db", db);
    if (account) {
      if (
        typeof account !== "string" ||
        account.length > 320 ||
        /[\r\n\0]/.test(account)
      )
        throw new Error("Choose a valid Gmail account.");
      args.push("--account", account);
    }
    args.push(command);
    return runSidecar(app, args, input);
  }
  return invoke;
}

function registerMail({
  app,
  ipcMain,
  dialog,
  trusted,
  getWorkspace,
  saveWorkspace,
  getWindow,
  worker,
}) {
  let sending = false;
  const configFile = path.join(
    app.getPath("userData"),
    "gmail-connection.json",
  );
  const invoke = worker || createWorkerClient(app);
  ipcMain.handle("gmail:accounts", async (event) => {
    trusted(event);
    return invoke("desktop-accounts");
  });
  ipcMain.handle("gmail:chooseConnection", async (event) => {
    trusted(event);
    const result = await dialog.showOpenDialog(getWindow(), {
      title: "Use an existing Gmail worker connection",
      message: "Choose the mail.db used by your Gmail worker.",
      properties: ["openFile"],
      filters: [
        { name: "Gmail database", extensions: ["db", "sqlite", "sqlite3"] },
      ],
    });
    if (result.canceled) return null;
    await fs.writeFile(
      configFile,
      JSON.stringify({ db: result.filePaths[0] }),
      { mode: 0o600 },
    );
    return invoke("desktop-accounts");
  });
  ipcMain.handle("gmail:inbox", async (event, account) => {
    trusted(event);
    return invoke("desktop-inbox", account);
  });
  ipcMain.handle("gmail:send", async (event, id) => {
    trusted(event);
    if (sending) throw new Error("A send is already in progress.");
    sending = true;
    try {
      const workspace = await getWorkspace();
      const draft = workspace?.drafts?.find((d) => d.id === id);
      if (
        workspace?.mode !== "local" ||
        draft?.appId !== "gmail" ||
        !draft.delivery ||
        !workspace.connections.some((c) => c.id === "gmail" && c.connected)
      )
        throw new Error("Load your connected Gmail drafts before sending.");
      if (!draft.body?.trim() || /\[[A-Z][A-Z0-9_ '-]*\]/.test(draft.body))
        throw new Error("Fill every placeholder before sending.");
      const reviewed = JSON.stringify(draft);
      const answer = await dialog.showMessageBox(getWindow(), {
        type: "question",
        title: "Send reply",
        message: `Send this reply to ${draft.recipient}?`,
        detail: `From: ${draft.delivery.accountId}\nTo: ${draft.address}\nSubject: ${draft.subject}\n\n${draft.body}`,
        buttons: ["Cancel", "Send reply"],
        defaultId: 0,
        cancelId: 0,
        noLink: true,
      });
      if (answer.response !== 1) return { status: "cancelled" };
      const current = await getWorkspace();
      if (
        JSON.stringify(current?.drafts?.find((d) => d.id === id)) !== reviewed
      )
        throw new Error(
          "The draft changed during review. Review it again before sending.",
        );
      const result = await invoke("desktop-send", draft.delivery.accountId, {
        ...draft.delivery,
        body: draft.body,
      });
      if (result.status === "sent") {
        const latest = await getWorkspace();
        try {
          await saveWorkspace({
            ...latest,
            drafts: latest.drafts.filter((d) => d.id !== id),
          });
        } catch {
          /* Provider acknowledgement is authoritative; never misreport as unsent. */
        }
      }
      if (!["sent", "uncertain"].includes(result.status))
        throw new Error(
          "Gmail did not confirm delivery. Check Gmail before retrying.",
        );
      return result;
    } finally {
      sending = false;
    }
  });
}
module.exports = { registerMail, runWorker, runSidecar, createWorkerClient };
