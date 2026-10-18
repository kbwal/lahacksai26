const { contextBridge, ipcRenderer } = require("electron");
contextBridge.exposeInMainWorld("margin", {
  gmailAccounts: () => ipcRenderer.invoke("gmail:accounts"),
  chooseGmailConnection: () => ipcRenderer.invoke("gmail:chooseConnection"),
  gmailInbox: (account) => ipcRenderer.invoke("gmail:inbox", account),
  sendDraft: (id) => ipcRenderer.invoke("gmail:send", id),
  loadWorkspace: () => ipcRenderer.invoke("workspace:load"),
  saveWorkspace: (workspace) => ipcRenderer.invoke("workspace:save", workspace),
  openExternal: (url) => ipcRenderer.invoke("external:open", url),
});
