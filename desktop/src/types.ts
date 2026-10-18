export type AppId = "gmail" | "slack" | "outlook";
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
export interface Workspace {
  version: 1;
  mode: "demo" | "local";
  connections: AppConnection[];
  roles: Persona[];
  drafts: Draft[];
  preferences: { automaticDrafts: boolean; learnFromEdits: boolean };
}
export interface TimeChoice {
  date: string;
  time: string;
}
declare global {
  interface Window {
    margin?: {
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
