const { contextBridge, ipcRenderer } = require("electron");
contextBridge.exposeInMainWorld("margin", {
  chatConnections: (source, choose) => ipcRenderer.invoke("messages:connections", source, choose),
  chatInbox: (source, account, workspace) => ipcRenderer.invoke("messages:inbox", source, account, workspace),
  syncChat: (source, account, workspace) => ipcRenderer.invoke("messages:sync", source, account, workspace),
  copyDraft: (id) => ipcRenderer.invoke("draft:copy", id),
  composeMessage: (request) => ipcRenderer.invoke("compose:generate", request),
  compareMessage: (request) => ipcRenderer.invoke("compose:compare", request),
  gmailAccounts: () => ipcRenderer.invoke("gmail:accounts"),
  chooseGmailConnection: () => ipcRenderer.invoke("gmail:chooseConnection"),
  gmailInbox: (account) => ipcRenderer.invoke("gmail:inbox", account),
  sendDraft: (id) => ipcRenderer.invoke("gmail:send", id),
  loadWorkspace: () => ipcRenderer.invoke("workspace:load"),
  saveWorkspace: (workspace) => ipcRenderer.invoke("workspace:save", workspace),
  openExternal: (url) => ipcRenderer.invoke("external:open", url),
});
