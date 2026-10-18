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
  externalUrl?: string;
  localChanges?: boolean;
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
      loadWorkspace(): Promise<unknown>;
      saveWorkspace(workspace: Workspace): Promise<void>;
      openExternal(url: string): Promise<void>;
    };
  }
}
