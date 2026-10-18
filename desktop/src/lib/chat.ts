import type { ChatConnection, ChatInbox, Workspace } from "../types";
import { applyWorkspaceDetails, parseWorkspace } from "./domain";

export function chatKey(account: string, workspace: string) {
  return JSON.stringify([account, workspace]);
}

export function chatStatus(connection: ChatConnection): string {
  const channels = connection.channels.filter(c => c.selected);
  if (connection.retry_at * 1000 > Date.now())
    return `Rate limited · retry after ${new Date(connection.retry_at * 1000).toLocaleTimeString([], {hour: "numeric", minute: "2-digit"})}`;
  if (connection.last_error) return `Sync needs attention · ${connection.last_error}`;
  const failed = channels.filter(c => c.last_error);
  if (failed.length) return `${failed.length} ${failed.length === 1 ? "channel needs" : "channels need"} attention · ${failed.map(c => c.channel_id).join(", ")}`;
  if (!channels.length) return "No channels selected in this connection.";
  if (channels.some(c => c.scan_in_progress)) return "History import in progress · suggestions use the available context.";
  const completed = channels.map(c => c.last_complete_at).filter((v): v is number => v !== null);
  if (completed.length !== channels.length) return "Ready for the first sync.";
  return `Last synced ${new Date(Math.min(...completed) * 1000).toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"})} · ${connection.messages} ${connection.messages === 1 ? "message" : "messages"}`;
}

export function mergeChatInbox(workspace: Workspace, result: ChatInbox): Workspace {
  if (!["slack", "discord"].includes(result.source) || !result.accountId || !result.workspaceId ||
      result.status.source !== result.source || result.status.account_id !== result.accountId ||
      result.status.workspace_id !== result.workspaceId)
    throw new Error("The inbox does not match the selected connection.");
  const roles = workspace.mode === "demo"
    ? workspace.roles.map(role => ({...role, usableExamples: 0, trainingReady: false}))
    : workspace.roles;
  const existing = new Map(workspace.drafts.map(draft => [draft.id, draft]));
  const incoming = result.drafts.map(draft => {
    if (draft.appId !== result.source || draft.source !== result.source ||
        draft.chat?.source !== result.source || draft.chat.accountId !== result.accountId ||
        draft.chat.workspaceId !== result.workspaceId || draft.delivery || draft.composed)
      throw new Error("A suggestion belongs to a different connection.");
    const previous = existing.get(draft.id);
    const sameConversation = previous?.chat && ["source", "accountId", "workspaceId", "channelId", "threadId"].every(
      key => previous.chat![key as keyof NonNullable<typeof previous.chat>] === draft.chat![key as keyof NonNullable<typeof draft.chat>],
    );
    const roleId = roles.some(r => r.id === draft.roleId) ? draft.roleId
      : (roles.find(r => !r.excludedApps.includes(result.source)) || roles[0]).id;
    return previous?.localChanges && sameConversation
      ? {...draft, roleId: previous.roleId, body: previous.body, localChanges: true,
          placeholderEdits: previous.placeholderEdits, timeEdit: previous.timeEdit}
      : {...draft, roleId};
  });
  const incomingIds = new Set(incoming.map(d => d.id));
  const retained = workspace.drafts.flatMap(draft => {
    if (workspace.mode === "demo") return draft.composed ? [draft] : [];
    const sameConnection = draft.chat?.source === result.source &&
      draft.chat.accountId === result.accountId && draft.chat.workspaceId === result.workspaceId;
    if (!sameConnection) return [draft];
    if (incomingIds.has(draft.id)) return [];
    return draft.localChanges ? [{...draft, chat: {...draft.chat!, stale: true}}] : [];
  });
  return applyWorkspaceDetails(parseWorkspace({
    ...workspace, mode: "local", roles,
    connections: workspace.connections.map(c => ({...c,
      connected: c.id === result.source || (workspace.mode !== "demo" && c.connected)})),
    drafts: [...retained, ...incoming],
  }));
}
