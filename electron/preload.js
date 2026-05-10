const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  getVersion:         () => ipcRenderer.invoke('app:get-version'),
  installUpdate:      () => ipcRenderer.invoke('updater:install'),
  openReleasePage:    () => ipcRenderer.invoke('app:open-releases'),
  onUpdateAvailable:  cb => ipcRenderer.on('update-available',  (_e, info) => cb(info)),
  onUpdateDownloaded: cb => ipcRenderer.on('update-downloaded', (_e, info) => cb(info)),
});
