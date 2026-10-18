import { describe, expect, it } from "vitest";
import type { ChatInbox, ChatSource } from "../src/types";
import { chatStatus, mergeChatInbox } from "../src/lib/chat";
import { allowedRoles, fillDetail, parseWorkspace, searchDrafts } from "../src/lib/domain";
import { sampleWorkspace } from "../src/lib/sample";

function inbox(source: ChatSource = "discord", account = "U1"): ChatInbox {
  return {
    source, accountId: account, workspaceId: "W1",
    status: {source, account_id: account, workspace_id: "W1", retry_at: 0, last_error: null,
      messages: 1, channels: [{channel_id: "C1", selected: true, scan_in_progress: false,
        last_complete_at: 1700000000, last_error: null}]},
    drafts: [{id: `${source}:${account}`, appId: source, source, roleId: "professional",
      subject: "Reply in #team", recipient: "Nico", address: "#team · W1", context: "Joining us?",
      body: "I can join at [TIME].", updatedAt: "2026-10-18T07:00:00Z",
      chat: {source, accountId: account, workspaceId: "W1", channelId: "C1", threadId: "C1",
        suggestionId: 1, stale: false}}],
  };
}

describe("chat workspace integration", () => {
  it("migrates a three-app snapshot without losing its drafts or exclusions", () => {
    const old = sampleWorkspace();
    old.connections = old.connections.filter(c => c.id !== "discord");
    old.drafts = old.drafts.filter(d => d.appId !== "discord");
    old.roles[0].excludedApps = ["slack"];
    const updated = parseWorkspace(old);
    expect(updated.connections.find(c => c.id === "discord")?.connected).toBe(false);
    expect(updated.drafts.map(d => d.id)).toEqual(old.drafts.map(d => d.id));
    expect(allowedRoles(updated.roles, "slack")).toHaveLength(1);
    expect(allowedRoles(updated.roles, "discord")).toHaveLength(2);
  });
  it("loads source-labelled drafts, clears sample readiness and keeps other accounts", () => {
    let workspace = mergeChatInbox(sampleWorkspace(), inbox("discord"));
    expect(workspace.mode).toBe("local");
    expect(workspace.roles.every(role => !role.trainingReady && role.usableExamples === 0)).toBe(true);
    workspace = mergeChatInbox(workspace, inbox("slack"));
    workspace = mergeChatInbox(workspace, inbox("discord", "U2"));
    expect(workspace.drafts).toHaveLength(3);
    expect(searchDrafts(workspace.drafts, "discord nico", workspace.roles)).toHaveLength(2);
    expect(parseWorkspace(JSON.parse(JSON.stringify(workspace))).drafts[0].chat?.source).toBe("discord");
  });
  it("keeps human edits through refresh and flags them if the suggestion disappears", () => {
    let workspace = mergeChatInbox(sampleWorkspace(), inbox());
    workspace.drafts[0] = fillDetail(workspace.drafts[0], "0", "2 PM");
    const changed = inbox();
    changed.drafts[0].body = "A regenerated suggestion.";
    workspace = mergeChatInbox(workspace, changed);
    expect(workspace.drafts[0].body).toBe("I can join at 2 PM.");
    workspace = mergeChatInbox(workspace, {...inbox(), drafts: []});
    expect(workspace.drafts[0].chat?.stale).toBe(true);
    expect(workspace.drafts[0].placeholderEdits?.values["0"].value).toBe("2 PM");
  });
  it("removes superseded machine text without discarding composed messages", () => {
    const workspace = mergeChatInbox(sampleWorkspace(), inbox());
    workspace.drafts.push({...sampleWorkspace().drafts[0], composed: true, id: "manual"});
    expect(mergeChatInbox(workspace, {...inbox(), drafts: []}).drafts.map(d => d.id)).toEqual(["manual"]);
  });
  it("rejects cross-source/account data and invalid suggestion metadata", () => {
    const result = inbox();
    result.drafts[0].chat!.accountId = "other";
    expect(() => mergeChatInbox(sampleWorkspace(), result)).toThrow("different connection");
    const workspace = mergeChatInbox(sampleWorkspace(), inbox());
    workspace.drafts[0].source = "slack";
    expect(() => parseWorkspace(workspace)).toThrow("source");
  });
  it("shows partial scans, channel errors and provider backoff instead of claiming fresh data", () => {
    const status = inbox().status;
    status.channels[0].scan_in_progress = true;
    expect(chatStatus(status)).toContain("in progress");
    status.channels[0].last_error = "http_403";
    expect(chatStatus(status)).toContain("attention");
    status.retry_at = Date.now() / 1000 + 300;
    expect(chatStatus(status)).toContain("Rate limited");
  });
});
