import { createRequire } from "node:module";
import { describe, expect, it, vi } from "vitest";
import { sampleWorkspace } from "../src/lib/sample";
const require = createRequire(import.meta.url);
const { registerMail } = require("../electron/mail.cjs");

function harness() {
  let workspace = sampleWorkspace();
  workspace.mode = "local";
  workspace.drafts[0].body = "Thanks, 2:00 PM works for me.";
  workspace.drafts[0].delivery = {
    accountId: "me@example.com",
    generationId: 1,
    gmailDraftId: "remote",
    snapshot: "a".repeat(64),
  };
  const handlers = new Map<string, Function>();
  const dialog = { showMessageBox: vi.fn(async () => ({ response: 1 })) };
  const worker = vi.fn(async () => ({ status: "sent", messageId: "sent" }));
  const saveWorkspace = vi.fn(async (value) => {
    workspace = value;
  });
  registerMail({
    app: { getPath: () => "/tmp" },
    ipcMain: { handle: (name: string, fn: Function) => handlers.set(name, fn) },
    dialog,
    trusted: vi.fn(),
    getWorkspace: async () => workspace,
    saveWorkspace,
    getWindow: () => ({}),
    worker,
  });
  return {
    get workspace() {
      return workspace;
    },
    dialog,
    worker,
    saveWorkspace,
    send: () => handlers.get("gmail:send")!({}, "office"),
  };
}

describe("native send boundary", () => {
  it("cancels without accessing Gmail", async () => {
    const h = harness();
    h.dialog.showMessageBox.mockResolvedValue({ response: 0 });
    expect(await h.send()).toEqual({ status: "cancelled" });
    expect(h.worker).not.toHaveBeenCalled();
    expect(h.workspace.drafts).toHaveLength(5);
  });
  it("shows the real recipient and body, then removes only the acknowledged draft", async () => {
    const h = harness();
    expect((await h.send()).status).toBe("sent");
    expect(h.dialog.showMessageBox.mock.calls[0][1].detail).toContain(
      "professor.chen@university.example",
    );
    expect(h.dialog.showMessageBox.mock.calls[0][1].detail).toContain(
      "2:00 PM works for me.",
    );
    expect(h.worker).toHaveBeenCalledWith(
      "desktop-send",
      "me@example.com",
      expect.objectContaining({
        generationId: 1,
        body: "Thanks, 2:00 PM works for me.",
      }),
    );
    expect(h.workspace.drafts).toHaveLength(4);
    expect(h.workspace.drafts.some((d) => d.id === "office")).toBe(false);
  });
  it("blocks sample drafts and missing details before confirmation", async () => {
    const h = harness();
    h.workspace.mode = "demo";
    await expect(h.send()).rejects.toThrow("Load your connected");
    h.workspace.mode = "local";
    for (const token of ["DATE", "TIME", "AMOUNT", "PHONE", "ADDRESS", "LINK", "MEETING_LINK_2"]) {
      h.workspace.drafts[0].body = `Missing [${token}].`;
      await expect(h.send()).rejects.toThrow("placeholder");
    }
    expect(h.dialog.showMessageBox).not.toHaveBeenCalled();
    expect(h.worker).not.toHaveBeenCalled();
  });
  it("requires a fresh review when the message changes during confirmation", async () => {
    const h = harness();
    h.dialog.showMessageBox.mockImplementation(async () => {
      h.workspace.drafts[0].body = "Changed";
      return { response: 1 };
    });
    await expect(h.send()).rejects.toThrow("changed during review");
    expect(h.worker).not.toHaveBeenCalled();
  });
  it("keeps the draft when Gmail cannot confirm delivery", async () => {
    const h = harness();
    h.worker.mockResolvedValue({ status: "uncertain", messageId: "" });
    expect((await h.send()).status).toBe("uncertain");
    expect(h.saveWorkspace).not.toHaveBeenCalled();
    expect(h.workspace.drafts).toHaveLength(5);
  });
  it("does not report failed delivery if local persistence fails after Gmail acknowledges", async () => {
    const h = harness();
    h.saveWorkspace.mockRejectedValue(new Error("Disk full"));
    expect((await h.send()).status).toBe("sent");
    expect(h.worker).toHaveBeenCalledTimes(1);
  });
});
