// What the EMB website can ask the Windows app for. Nothing else from the computer is exposed.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("embDesktop", {
  print: (opts) => ipcRenderer.invoke("emb:print", opts || {}),
  printers: () => ipcRenderer.invoke("emb:printers"),
  settings: () => ipcRenderer.invoke("emb:settings"),
  saveSettings: (s) => ipcRenderer.invoke("emb:save-settings", s),
  version: () => ipcRenderer.invoke("emb:version"),
});
