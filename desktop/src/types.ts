export type AppId = "gmail" | "slack" | "discord" | "outlook";
export type ChatSource = "slack" | "discord";
export interface ChatConnection {
  source: ChatSource;
  account_id: string;
  workspace_id: string;
  retry_at: number;
  last_error: string | null;
  messages: number;
  channels: { channel_id: string; selected: boolean; last_error: string | null;
    scan_in_progress: boolean; last_complete_at: number | null }[];
}
export interface ChatInbox {
  source: ChatSource;
  accountId: string;
  workspaceId: string;
  status: ChatConnection;
  drafts: Draft[];
}
export type Page = "drafts" | "roles" | "settings";
export interface AppConnection {
  id: AppId;
  name: string;
  connected: boolean;
}
export interface Persona {
  id: string;
  name: string;
  description: string;
  kind: "default" | "custom";
  excludedApps: AppId[];
  usableExamples: number;
  trainingReady: boolean;
}
export interface DetailFill {
  value: string;
  input?: string;
}
export interface PlaceholderEdits {
  template: string;
  values: Record<string, DetailFill>;
}
export interface Draft {
  id: string;
  appId: AppId;
  source?: "gmail" | ChatSource;
  chat?: { source: ChatSource; accountId: string; workspaceId: string; channelId: string;
    threadId: string; suggestionId: number; stale: boolean };
  roleId: string;
  subject: string;
  recipient: string;
  address: string;
  context: string;
  body: string;
  updatedAt: string;
  composed?: boolean;
  delivery?: {
    accountId: string;
    generationId: number;
    gmailDraftId: string;
    snapshot: string;
  };
  externalUrl?: string;
  localChanges?: boolean;
  placeholderEdits?: PlaceholderEdits;
  timeEdit?: { template: string; choice: TimeChoice; rendered: string };
}
export interface PersonalDetails {
  autoFill: boolean;
  name: string;
  email: string;
  phone: string;
  address: string;
  website: string;
}
export interface Workspace {
  version: 1;
  mode: "demo" | "local";
  connections: AppConnection[];
  roles: Persona[];
  drafts: Draft[];
  personalDetails?: PersonalDetails;
  preferences: { automaticDrafts: boolean; learnFromEdits: boolean };
}
export interface TimeChoice {
  date: string;
  time: string;
}
declare global {
  interface Window {
    margin?: {
      chatConnections(source: ChatSource, choose?: boolean): Promise<ChatConnection[] | null>;
      chatInbox(source: ChatSource, account: string, workspace: string): Promise<ChatInbox>;
      syncChat(source: ChatSource, account: string, workspace: string): Promise<ChatInbox | null>;
      copyDraft(id: string): Promise<void>;
      composeMessage(request: { appId: AppId; personaId: string; prompt: string; recipient: string; subject: string; changeModel: boolean }): Promise<{body: string} | null>;
      gmailAccounts(): Promise<string[]>;
      chooseGmailConnection(): Promise<string[] | null>;
      gmailInbox(
        account?: string,
      ): Promise<{ accountId: string; drafts: Draft[] }>;
      sendDraft(id: string): Promise<{
        status: "sent" | "cancelled" | "uncertain";
        message?: string;
      }>;
      loadWorkspace(): Promise<unknown>;
      saveWorkspace(workspace: Workspace): Promise<void>;
      openExternal(url: string): Promise<void>;
    };
  }
}
