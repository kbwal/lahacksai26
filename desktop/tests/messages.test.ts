import { createRequire } from "node:module";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
const { registerMessages } = createRequire(import.meta.url)("../electron/messages.cjs");
const directories: string[] = [];
afterEach(async () => { for (const dir of directories.splice(0)) await rm(dir, {recursive: true, force: true}); });

async function harness(getWorkspace?: () => Promise<unknown>) {
  const dir = await mkdtemp(path.join(tmpdir(), "margin-messages-"));
  directories.push(dir);
  const handlers: Record<string, Function> = {};
  const connection = {source: "slack", account_id: "U1", workspace_id: "T1"};
  const worker = vi.fn(async (args: string[]) => args.at(-1) === "connections" ? [connection] : {source: "slack", drafts: []});
  const dialog = {showOpenDialog: vi.fn(async () => ({canceled: true, filePaths: [] as string[]}))};
  const trusted = vi.fn();
  registerMessages({app: {getPath: () => dir, isPackaged: false}, ipcMain: {
    handle: (name: string, handler: Function) => { handlers[name] = handler; },
  }, dialog, trusted, getWindow: () => null, getWorkspace, worker});
  return {dir, handlers, worker, dialog, trusted};
}

describe("native chat boundary", () => {
  it("only invokes fixed operations for a saved, source-scoped account", async () => {
    const h = await harness();
    await expect(h.handlers["messages:inbox"]({}, "slack", "U1", "T1")).resolves.toEqual({source: "slack", drafts: []});
    expect(h.worker.mock.calls.at(-1)![0].slice(-5)).toEqual(["--account", "U1", "--workspace", "T1", "inbox"]);
    await expect(h.handlers["messages:inbox"]({}, "slack", "OTHER", "T1")).rejects.toThrow("no longer available");
    await expect(h.handlers["messages:inbox"]({}, "discord", "U1", "T1")).rejects.toThrow("no longer available");
    h.worker.mockClear();
    await expect(h.handlers["messages:connections"]({}, "gmail")).rejects.toThrow("Slack or Discord");
    await expect(h.handlers["messages:inbox"]({}, "slack", "bad\naccount", "T1")).rejects.toThrow("saved account");
    expect(h.worker).not.toHaveBeenCalled();
    expect(h.trusted).toHaveBeenCalledTimes(5);
  });
  it("validates a chosen connection before saving its path, and respects cancellation", async () => {
    const h = await harness();
    expect(await h.handlers["messages:connections"]({}, "slack", true)).toBeNull();
    expect(h.worker).not.toHaveBeenCalled();
    const db = path.join(h.dir, "selected.db");
    h.dialog.showOpenDialog.mockResolvedValue({canceled: false, filePaths: [db]});
    await h.handlers["messages:connections"]({}, "slack", true);
    expect(JSON.parse(await readFile(path.join(h.dir, "messages-slack.json"), "utf8"))).toEqual({db});
    expect(h.worker.mock.calls[0][0]).toEqual(["messages", "--source", "slack", "--db", db, "connections"]);
    h.dialog.showOpenDialog.mockResolvedValue({canceled: false, filePaths: ["relative.db"]});
    await expect(h.handlers["messages:connections"]({}, "slack", true)).rejects.toThrow("absolute");
    expect(JSON.parse(await readFile(path.join(h.dir, "messages-slack.json"), "utf8"))).toEqual({db});
  });
  it("runs bounded sync/generation and then reloads the inbox without a send operation", async () => {
    const h = await harness();
    await writeFile(path.join(h.dir, "compose-model.json"), JSON.stringify({path: h.dir}));
    await h.handlers["messages:sync"]({}, "slack", "U1", "T1");
    const sync = h.worker.mock.calls[1][0];
    expect(sync.slice(-8)).toEqual(["sync", "--max-pages", "5", "--suggest-replies", "--draft-limit", "3", "--model", h.dir]);
    expect(h.worker.mock.calls.at(-1)![0].at(-1)).toBe("inbox");
    expect(h.worker.mock.calls.every(([args]) => !args.some(arg => arg.includes("send")))).toBe(true);
  });
  it("drafts with the first persona allowed for the source, so its adapter loads", async () => {
    const h = await harness(async () => ({roles: [
      {id: "professional", excludedApps: ["slack"]}, {id: "social", excludedApps: []},
    ]}));
    await writeFile(path.join(h.dir, "compose-model.json"), JSON.stringify({path: h.dir}));
    await h.handlers["messages:sync"]({}, "slack", "U1", "T1");
    expect(h.worker.mock.calls[1][0].slice(-3)).toEqual([h.dir, "--persona", "social"]);
  });
  it("does not sync or draft when every persona excludes the source", async () => {
    const h = await harness(async () => ({roles: [
      {id: "professional", excludedApps: ["slack"]}, {id: "social", excludedApps: ["slack"]},
    ]}));
    await expect(h.handlers["messages:sync"]({}, "slack", "U1", "T1")).rejects.toThrow("Allow a persona for Slack");
    expect(h.worker).not.toHaveBeenCalled();
    expect(h.dialog.showOpenDialog).not.toHaveBeenCalled();
  });
  it("cancels model selection without syncing or overwriting the inbox", async () => {
    const h = await harness();
    expect(await h.handlers["messages:sync"]({}, "slack", "U1", "T1")).toBeNull();
    expect(h.worker).toHaveBeenCalledTimes(1);
    expect(h.worker.mock.calls[0][0].at(-1)).toBe("connections");
  });
});
