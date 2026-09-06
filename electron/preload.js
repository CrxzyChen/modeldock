"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld(
  "mediaCenterDesktop",
  Object.freeze({
    listConnections: () => ipcRenderer.invoke("connections:list"),
    connectServer: (request) =>
      ipcRenderer.invoke("connections:connect", request),
    switchServer: (profileId) =>
      ipcRenderer.invoke("connections:switch", profileId),
    logoutServer: () => ipcRenderer.invoke("connections:logout"),
    removeServer: (profileId) =>
      ipcRenderer.invoke("connections:remove", profileId),
    request: (path, options = {}) =>
      ipcRenderer.invoke("api:request", {
        path,
        method: options.method || "GET",
        body: options.body,
        idempotencyKey: options.idempotencyKey,
      }),
    configureSourceAuthorization: (request) =>
      ipcRenderer.invoke("source-authorization:configure", request),
    openSourceTerms: (url) =>
      ipcRenderer.invoke("source-authorization:open-terms", url),
    startEvents: () => ipcRenderer.invoke("events:start"),
    stopEvents: () => ipcRenderer.invoke("events:stop"),
    onServerEvent: (callback) => {
      if (typeof callback !== "function") return () => {};
      const listener = (_event, value) => callback(value);
      ipcRenderer.on("server:event", listener);
      return () => ipcRenderer.removeListener("server:event", listener);
    },
    onServerEventStatus: (callback) => {
      if (typeof callback !== "function") return () => {};
      const listener = (_event, value) => callback(value);
      ipcRenderer.on("server:event-status", listener);
      return () => ipcRenderer.removeListener("server:event-status", listener);
    },
    fetchArtifact: (path) => ipcRenderer.invoke("artifact:fetch", path),
    saveArtifact: (path, suggestedName) =>
      ipcRenderer.invoke("artifact:save", { path, suggestedName }),
    uploadAsset: (payload) => ipcRenderer.invoke("asset:upload", payload),
    pickAssets: (options) => ipcRenderer.invoke("asset:pick", options),
    pickAndUploadModel: (options) =>
      ipcRenderer.invoke("model:pick-and-upload", options),
    pickModelForImport: (options) =>
      ipcRenderer.invoke("model:pick-for-import", options),
    listModelUploadSessions: () => ipcRenderer.invoke("model:upload-sessions"),
    startModelUpload: (options) =>
      ipcRenderer.invoke("model:start-upload", options),
    resumeModelUpload: (options) =>
      ipcRenderer.invoke("model:resume-upload", options),
    pauseModelUpload: (sessionId) =>
      ipcRenderer.invoke("model:pause-upload", sessionId),
    discardModelUpload: (sessionId) =>
      ipcRenderer.invoke("model:discard-upload", sessionId),
    onModelTransferProgress: (callback) => {
      if (typeof callback !== "function") return () => {};
      const listener = (_event, value) => callback(value);
      ipcRenderer.on("model-transfer:progress", listener);
      return () =>
        ipcRenderer.removeListener("model-transfer:progress", listener);
    },
    notifyTask: (payload) => ipcRenderer.invoke("notification:task", payload),
    windowControl: (action) => ipcRenderer.invoke("window:control", action),
    onWindowState: (callback) => {
      if (typeof callback !== "function") return () => {};
      const listener = (_event, value) => callback(value);
      ipcRenderer.on("desktop:window-state", listener);
      return () => ipcRenderer.removeListener("desktop:window-state", listener);
    },
    onNavigate: (callback) => {
      if (typeof callback !== "function") return () => {};
      const listener = (_event, view) => callback(view);
      ipcRenderer.on("desktop:navigate", listener);
      return () => ipcRenderer.removeListener("desktop:navigate", listener);
    },
    onRefresh: (callback) => {
      if (typeof callback !== "function") return () => {};
      const listener = () => callback();
      ipcRenderer.on("desktop:refresh", listener);
      return () => ipcRenderer.removeListener("desktop:refresh", listener);
    },
  }),
);
