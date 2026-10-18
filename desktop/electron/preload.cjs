const { contextBridge, ipcRenderer } = require("electron");
contextBridge.exposeInMainWorld("margin", {
  loadWorkspace: () => ipcRenderer.invoke("workspace:load"),
  saveWorkspace: (workspace) => ipcRenderer.invoke("workspace:save", workspace),
  openExternal: (url) => ipcRenderer.invoke("external:open", url),
});
