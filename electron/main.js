"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { createHash, randomUUID } = require("node:crypto");
const { isIP } = require("node:net");
const { app, BrowserWindow, dialog, ipcMain, Menu, nativeImage, net, Notification, protocol, safeStorage, screen, session, shell, Tray } = require("electron");

const LEGACY_SERVER_URL = "http://127.0.0.1:8787/";
const RENDERER_SCHEME = "mediacenter";
const APP_URL = `${RENDERER_SCHEME}://app/index.html`;
const RENDERER_ROOT = path.resolve(__dirname, "..", "client", "dist");
const LEGACY_CREDENTIAL_FILE = "api-key.bin";
const SERVER_PROFILES_FILE = "server-profiles.json";
const SERVER_CREDENTIALS_FILE = "server-credentials.bin";
const SOURCE_CREDENTIALS_FILE = "source-credentials.bin";
const WINDOW_STATE_FILE = "window-state.json";
const DESKTOP_SETTINGS_FILE = "desktop-settings.json";
const MODEL_UPLOAD_SESSIONS_FILE = "model-upload-sessions.json";
const MAX_ASSET_BYTES = 256 * 1024 * 1024;
const MAX_ARTIFACT_BYTES = 1024 * 1024 * 1024;
const MAX_PREVIEW_BYTES = 256 * 1024 * 1024;
const MAX_RENDERER_ASSET_BYTES = 16 * 1024 * 1024;
const RENDERER_CONTENT_TYPES = Object.freeze({
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".webp": "image/webp",
  ".woff2": "font/woff2"
});
const MEDIA_TYPES = Object.freeze({
  ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
  ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
  ".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac", ".ogg": "audio/ogg"
});
let mainWindow = null;
let tray = null;
let isQuitting = false;
let desktopSettings = { trayEnabled: false };
let connectionState = { version: 1, activeProfileId: null, revision: 0, profiles: [] };
let connectionCredentials = {};
let sourceCredentials = {};
let modelUploadSessions = { version: 1, sessions: [] };
const modelUploadRuns = new Map();
let serverEventRun = 0;
let serverEventController = null;
const serverEventCursors = new Map();

protocol.registerSchemesAsPrivileged([{
  scheme: RENDERER_SCHEME,
  privileges: { standard: true, secure: true, supportFetchAPI: true }
}]);

function userDataPath(filename) {
  return path.join(app.getPath("userData"), filename);
}

function readLegacyApiKey() {
  if (!safeStorage.isEncryptionAvailable()) return "";
  try {
    const encrypted = fs.readFileSync(userDataPath(LEGACY_CREDENTIAL_FILE));
    return safeStorage.decryptString(encrypted);
  } catch (error) {
    if (error.code !== "ENOENT") console.error("Unable to read encrypted API key:", error.message);
    return "";
  }
}

function normalizeServerUrl(rawValue) {
  const raw = typeof rawValue === "string" ? rawValue.trim() : "";
  if (!raw || raw.length > 2048) throw new Error("服务器地址无效");
  let url;
  try { url = new URL(raw); } catch { throw new Error("服务器地址格式无效"); }
  if (!['http:', 'https:'].includes(url.protocol) || !url.hostname) throw new Error("服务器地址仅支持 HTTP 或 HTTPS");
  if (url.username || url.password || url.search || url.hash || !["", "/"].includes(url.pathname)) {
    throw new Error("服务器地址只能包含协议、主机和端口");
  }
  return `${url.origin}/`;
}

function normalizeProfileName(rawValue, baseUrl) {
  const value = typeof rawValue === "string" ? rawValue.trim() : "";
  if (value.length > 80 || /[\r\n]/u.test(value)) throw new Error("服务器名称格式无效");
  return value || new URL(baseUrl).host;
}

function normalizeApiKey(rawValue, required = true) {
  const value = typeof rawValue === "string" ? rawValue.trim() : "";
  if ((required && !value) || value.length > 512 || /[\r\n]/u.test(value)) throw new Error("API Key 格式无效");
  return value;
}

function readConnectionCredentials() {
  if (!safeStorage.isEncryptionAvailable()) return {};
  try {
    const decrypted = safeStorage.decryptString(fs.readFileSync(userDataPath(SERVER_CREDENTIALS_FILE)));
    const value = JSON.parse(decrypted);
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  } catch (error) {
    if (error.code !== "ENOENT") console.error("Unable to read encrypted server credentials:", error.message);
    return {};
  }
}

function saveConnectionCredentials() {
  if (!safeStorage.isEncryptionAvailable()) throw new Error("系统加密存储不可用，API Key 未保存");
  fs.mkdirSync(app.getPath("userData"), { recursive: true });
  fs.writeFileSync(userDataPath(SERVER_CREDENTIALS_FILE), safeStorage.encryptString(JSON.stringify(connectionCredentials)), { mode: 0o600 });
}

function readSourceCredentials() {
  if (!safeStorage.isEncryptionAvailable()) return {};
  try {
    const decrypted = safeStorage.decryptString(fs.readFileSync(userDataPath(SOURCE_CREDENTIALS_FILE)));
    const value = JSON.parse(decrypted);
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  } catch (error) {
    if (error.code !== "ENOENT") console.error("Unable to read encrypted source credentials:", error.message);
    return {};
  }
}

function saveSourceCredentials() {
  if (!safeStorage.isEncryptionAvailable()) throw new Error("系统加密存储不可用，模型来源凭据未保存");
  fs.mkdirSync(app.getPath("userData"), { recursive: true });
  fs.writeFileSync(userDataPath(SOURCE_CREDENTIALS_FILE), safeStorage.encryptString(JSON.stringify(sourceCredentials)), { mode: 0o600 });
}

function readModelUploadSessions() {
  try {
    const value = JSON.parse(fs.readFileSync(userDataPath(MODEL_UPLOAD_SESSIONS_FILE), "utf8"));
    if (value?.version !== 1 || !Array.isArray(value.sessions)) throw new Error("模型上传会话格式无效");
    return { version: 1, sessions: value.sessions.filter(session =>
      session && typeof session.id === "string" && typeof session.profileId === "string" &&
      Array.isArray(session.files) && session.files.every(file =>
        file && typeof file.absolutePath === "string" && typeof file.relativePath === "string" &&
        Number.isSafeInteger(file.size) && file.size >= 0)) };
  } catch (error) {
    if (error.code !== "ENOENT") console.error("Unable to read model upload sessions:", error.message);
    return { version: 1, sessions: [] };
  }
}

function saveModelUploadSessions() {
  fs.mkdirSync(app.getPath("userData"), { recursive: true });
  fs.writeFileSync(userDataPath(MODEL_UPLOAD_SESSIONS_FILE), JSON.stringify(modelUploadSessions, null, 2), { mode: 0o600 });
}

function readConnectionProfiles() {
  try {
    const value = JSON.parse(fs.readFileSync(userDataPath(SERVER_PROFILES_FILE), "utf8"));
    if (value?.version !== 1 || !Array.isArray(value.profiles)) throw new Error("连接档案格式无效");
    const profiles = value.profiles.map(profile => ({
      id: String(profile.id), name: String(profile.name), baseUrl: normalizeServerUrl(profile.baseUrl),
      createdAt: String(profile.createdAt || ""), lastUsedAt: String(profile.lastUsedAt || ""),
      allowInsecureSourceAuthorization: profile.allowInsecureSourceAuthorization === true
    }));
    const activeProfileId = profiles.some(profile => profile.id === value.activeProfileId) ? value.activeProfileId : null;
    return { version: 1, activeProfileId, revision: 0, profiles };
  } catch (error) {
    if (error.code === "ENOENT") return null;
    console.error("Unable to read server profiles:", error.message);
    return { version: 1, activeProfileId: null, revision: 0, profiles: [] };
  }
}

function saveConnectionProfiles() {
  fs.mkdirSync(app.getPath("userData"), { recursive: true });
  const { revision: _revision, ...persisted } = connectionState;
  fs.writeFileSync(userDataPath(SERVER_PROFILES_FILE), JSON.stringify(persisted, null, 2), { mode: 0o600 });
}

function initializeConnections() {
  connectionCredentials = readConnectionCredentials();
  sourceCredentials = readSourceCredentials();
  modelUploadSessions = readModelUploadSessions();
  const existing = readConnectionProfiles();
  if (existing) { connectionState = existing; return; }
  const now = new Date().toISOString();
  const id = randomUUID();
  connectionState = { version: 1, activeProfileId: id, revision: 0, profiles: [{ id, name: "MediaCenter Server", baseUrl: LEGACY_SERVER_URL, createdAt: now, lastUsedAt: "", allowInsecureSourceAuthorization: false }] };
  const legacyKey = readLegacyApiKey();
  if (legacyKey) connectionCredentials[id] = legacyKey;
  saveConnectionProfiles();
  if (safeStorage.isEncryptionAvailable()) saveConnectionCredentials();
}

function publicConnectionState() {
  return {
    activeProfileId: connectionState.activeProfileId,
    revision: connectionState.revision,
    profiles: connectionState.profiles.map(profile => ({
      ...profile,
      insecure: profile.baseUrl.startsWith("http:"),
      hasCredential: Boolean(connectionCredentials[profile.id]),
      sourceAuthorizationAllowed: sourceCredentialTransportAllowed(profile)
    }))
  };
}

function activeConnectionSnapshot() {
  const profile = connectionState.profiles.find(item => item.id === connectionState.activeProfileId);
  const key = profile ? connectionCredentials[profile.id] : "";
  if (!profile || !key) throw new Error("请先连接服务器");
  return { profile, key, revision: connectionState.revision };
}

function assertConnectionCurrent(snapshot) {
  if (snapshot.revision !== connectionState.revision || snapshot.profile.id !== connectionState.activeProfileId) throw new Error("服务器连接已切换，请重试");
}

function sendServerEventStatus(snapshot, state, detail = "") {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  mainWindow.webContents.send("server:event-status", {
    profileId: snapshot.profile.id, revision: snapshot.revision, state, detail
  });
}

function stopServerEvents() {
  serverEventRun += 1;
  if (serverEventController) serverEventController.abort();
  serverEventController = null;
}

function parseServerEvent(block) {
  let id = "";
  let name = "message";
  const data = [];
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    const value = separator < 0 ? "" : line.slice(separator + 1).replace(/^ /u, "");
    if (field === "id") id = value;
    else if (field === "event") name = value;
    else if (field === "data") data.push(value);
  }
  if (!data.length) return null;
  const raw = data.join("\n");
  if (!id || id.length > 128 || id.includes("\0") || raw.length > 32 * 1024) {
    throw new Error("SSE 事件超过安全合同");
  }
  const payload = JSON.parse(raw);
  if (!payload || payload.protocol !== "mc.client/1" || payload.id !== id ||
      typeof payload.type !== "string" || payload.type !== name) {
    throw new Error("SSE 事件合同无效");
  }
  return payload;
}

async function consumeServerEvents(response, snapshot, run) {
  if (!response.body) throw new Error("服务器未返回 SSE 数据流");
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let buffer = "";
  while (run === serverEventRun) {
    const { value, done } = await reader.read();
    if (done) throw new Error("SSE 连接已关闭");
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n?/gu, "\n");
    if (buffer.length > 128 * 1024) throw new Error("SSE 接收缓冲超过上限");
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const payload = parseServerEvent(block);
      if (!payload) continue;
      assertConnectionCurrent(snapshot);
      serverEventCursors.set(snapshot.profile.id, payload.id);
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send("server:event", {
          profileId: snapshot.profile.id, revision: snapshot.revision, event: payload
        });
      }
    }
  }
}

async function runServerEvents(snapshot, run) {
  let retryMs = 1000;
  while (run === serverEventRun) {
    const controller = new AbortController();
    serverEventController = controller;
    try {
      void restoreSourceAuthorizations(snapshot).then(restored => {
        if (restored) {
          sendServerEventStatus(snapshot, "source-authorization-ready");
        }
      }).catch(error => {
        sendServerEventStatus(snapshot, "source-authorization-error", error.message);
      });
      const url = apiUrl("/api/v1/events", snapshot);
      const headers = { "Accept": "text/event-stream", "X-API-Key": snapshot.key };
      const cursor = serverEventCursors.get(snapshot.profile.id);
      if (cursor) headers["Last-Event-ID"] = cursor;
      const response = await net.fetch(url.toString(), { headers, redirect: "error", signal: controller.signal });
      if (response.url) apiUrl(response.url, snapshot);
      assertConnectionCurrent(snapshot);
      if (!response.ok || !String(response.headers.get("content-type") || "").startsWith("text/event-stream")) {
        throw new Error(`SSE 连接失败 (${response.status})`);
      }
      sendServerEventStatus(snapshot, "connected");
      retryMs = 1000;
      await consumeServerEvents(response, snapshot, run);
    } catch (error) {
      if (run !== serverEventRun || error.name === "AbortError") return;
      sendServerEventStatus(snapshot, "reconnecting", error.message);
      await new Promise(resolve => setTimeout(resolve, retryMs));
      retryMs = Math.min(30000, retryMs * 2);
    } finally {
      if (serverEventController === controller) serverEventController = null;
    }
  }
}

async function startServerEvents(event) {
  assertTrustedSender(event);
  const snapshot = activeConnectionSnapshot();
  const sourceAuthorizationWarning = sourceCredentials[snapshot.profile.id] &&
    !sourceCredentialTransportAllowed(snapshot.profile)
    ? "已保存模型来源凭据，但当前远程 HTTP 连接未获来源授权许可；请改用 HTTPS 或在连接档案中确认信任局域网" : "";
  stopServerEvents();
  const run = serverEventRun;
  void runServerEvents(snapshot, run);
  return { profileId: snapshot.profile.id, revision: snapshot.revision,
           sourceAuthorizationWarning };
}

function stopServerEventsFromRenderer(event) {
  assertTrustedSender(event);
  stopServerEvents();
  return true;
}

async function verifyServerConnection(baseUrl, key) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await net.fetch(new URL("/api/v1/overview", baseUrl).toString(), {
      headers: { "X-API-Key": key }, redirect: "error", signal: controller.signal
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error?.message || `服务器验证失败 (${response.status})`);
    }
    const text = await response.text();
    if (text.length > 2 * 1024 * 1024) throw new Error("服务器验证响应过大");
  } catch (error) {
    if (error.name === "AbortError") throw new Error("服务器连接超时");
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function listConnections(event) {
  assertTrustedSender(event);
  return publicConnectionState();
}

async function connectServer(event, request) {
  assertTrustedSender(event);
  if (!request || typeof request !== "object") throw new Error("服务器连接参数无效");
  if (!safeStorage.isEncryptionAvailable()) throw new Error("系统加密存储不可用，无法保存服务器凭据");
  const existing = connectionState.profiles.find(profile => profile.id === request.profileId);
  const baseUrl = normalizeServerUrl(request.baseUrl || existing?.baseUrl);
  const name = normalizeProfileName(request.name ?? existing?.name, baseUrl);
  if (request.allowInsecureSourceAuthorization != null &&
      typeof request.allowInsecureSourceAuthorization !== "boolean") {
    throw new Error("可信局域网来源授权设置无效");
  }
  const allowInsecureSourceAuthorization = request.allowInsecureSourceAuthorization == null
    ? existing?.allowInsecureSourceAuthorization === true
    : request.allowInsecureSourceAuthorization;
  const suppliedKey = normalizeApiKey(request.apiKey, false);
  const key = suppliedKey || (existing ? connectionCredentials[existing.id] : "");
  if (!key) throw new Error("请输入 API Key");
  const duplicate = connectionState.profiles.find(item => item.id !== existing?.id && item.baseUrl === baseUrl);
  if (duplicate) throw new Error("该服务器地址已经存在");
  await verifyServerConnection(baseUrl, key);
  const now = new Date().toISOString();
  stopServerEvents();
  let profile = existing;
  if (profile) {
    if (profile.baseUrl !== baseUrl) delete sourceCredentials[profile.id];
    profile = { ...profile, name, baseUrl, lastUsedAt: now, allowInsecureSourceAuthorization };
    connectionState.profiles = connectionState.profiles.map(item => item.id === profile.id ? profile : item);
  } else {
    profile = { id: randomUUID(), name, baseUrl, createdAt: now, lastUsedAt: now, allowInsecureSourceAuthorization };
    connectionState.profiles.push(profile);
  }
  connectionCredentials[profile.id] = key;
  connectionState.activeProfileId = profile.id;
  connectionState.revision += 1;
  saveConnectionProfiles();
  saveConnectionCredentials();
  saveSourceCredentials();
  return publicConnectionState();
}

async function switchServer(event, profileId) {
  assertTrustedSender(event);
  const profile = connectionState.profiles.find(item => item.id === profileId);
  if (!profile) throw new Error("服务器连接档案不存在");
  const key = connectionCredentials[profile.id];
  if (!key) return { ...publicConnectionState(), requiresLogin: true, selectedProfileId: profile.id };
  await verifyServerConnection(profile.baseUrl, key);
  stopServerEvents();
  const lastUsedAt = new Date().toISOString();
  connectionState.profiles = connectionState.profiles.map(item => item.id === profile.id ? { ...item, lastUsedAt } : item);
  connectionState.activeProfileId = profile.id;
  connectionState.revision += 1;
  saveConnectionProfiles();
  return publicConnectionState();
}

function logoutServer(event) {
  assertTrustedSender(event);
  stopServerEvents();
  if (connectionState.activeProfileId) {
    delete connectionCredentials[connectionState.activeProfileId];
    delete sourceCredentials[connectionState.activeProfileId];
  }
  connectionState.revision += 1;
  saveConnectionCredentials();
  saveSourceCredentials();
  return publicConnectionState();
}

function removeServer(event, profileId) {
  assertTrustedSender(event);
  const index = connectionState.profiles.findIndex(item => item.id === profileId);
  if (index < 0) throw new Error("服务器连接档案不存在");
  if (connectionState.activeProfileId === profileId) stopServerEvents();
  connectionState.profiles.splice(index, 1);
  delete connectionCredentials[profileId];
  delete sourceCredentials[profileId];
  if (connectionState.activeProfileId === profileId) connectionState.activeProfileId = connectionState.profiles[0]?.id || null;
  connectionState.revision += 1;
  saveConnectionProfiles();
  saveConnectionCredentials();
  saveSourceCredentials();
  return publicConnectionState();
}

function normalizeSourceProvider(rawValue) {
  if (rawValue !== "huggingface") throw new Error("模型来源授权提供方不受支持");
  return rawValue;
}

function normalizeSourceToken(rawValue) {
  const value = typeof rawValue === "string" ? rawValue.trim() : "";
  if (!value || value.length > 4096 || /\s/u.test(value)) throw new Error("Hugging Face 令牌格式无效");
  return value;
}

function sourceCredentialTransportAllowed(profile) {
  const url = new URL(profile.baseUrl);
  if (url.protocol === "https:") return true;
  if (url.hostname === "localhost" || url.hostname === "[::1]") return true;
  if (isIP(url.hostname) === 4 && url.hostname.split(".")[0] === "127") return true;
  return url.protocol === "http:" && profile.allowInsecureSourceAuthorization === true;
}

async function sourceAuthorizationRequest(snapshot, provider, payload) {
  if (!sourceCredentialTransportAllowed(snapshot.profile)) {
    throw new Error("当前服务器使用远程 HTTP；请改用 HTTPS，或在连接档案中确认信任局域网后再提交模型来源令牌");
  }
  const url = apiUrl(`/api/v1/source-authorizations/${provider}`, snapshot);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 12000);
  const headers = { "Content-Type": "application/json", "X-API-Key": snapshot.key };
  if (new URL(snapshot.profile.baseUrl).protocol === "http:" &&
      snapshot.profile.allowInsecureSourceAuthorization === true) {
    headers["X-MediaCenter-Insecure-Transport-Accepted"] = "1";
  }
  let response;
  try {
    response = await net.fetch(url.toString(), {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
      redirect: "error",
      signal: controller.signal
    });
  } catch (error) {
    if (error.name === "AbortError") throw new Error("模型来源授权请求超时");
    throw error;
  } finally {
    clearTimeout(timer);
  }
  if (response.url) apiUrl(response.url, snapshot);
  assertConnectionCurrent(snapshot);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error?.message || `模型来源授权失败 (${response.status})`);
  return data;
}

async function configureSourceAuthorization(event, request) {
  assertTrustedSender(event);
  if (!request || typeof request !== "object") throw new Error("模型来源授权参数无效");
  const provider = normalizeSourceProvider(request.provider);
  const snapshot = activeConnectionSnapshot();
  if (request.action === "clear") {
    const result = await sourceAuthorizationRequest(snapshot, provider, { action: "clear" });
    const credentials = { ...(sourceCredentials[snapshot.profile.id] || {}) };
    delete credentials[provider];
    if (Object.keys(credentials).length) sourceCredentials[snapshot.profile.id] = credentials;
    else delete sourceCredentials[snapshot.profile.id];
    saveSourceCredentials();
    return result;
  }
  if (request.action != null && request.action !== "configure") throw new Error("模型来源授权动作不受支持");
  const token = normalizeSourceToken(request.token);
  const result = await sourceAuthorizationRequest(snapshot, provider, { token });
  sourceCredentials[snapshot.profile.id] = {
    ...(sourceCredentials[snapshot.profile.id] || {}), [provider]: token
  };
  saveSourceCredentials();
  return result;
}

async function restoreSourceAuthorizations(snapshot) {
  const credentials = sourceCredentials[snapshot.profile.id];
  if (!credentials) return false;
  if (!sourceCredentialTransportAllowed(snapshot.profile)) {
    throw new Error("已保存模型来源凭据，但当前远程 HTTP 连接未获来源授权许可；请改用 HTTPS 或在连接档案中确认信任局域网");
  }
  for (const provider of Object.keys(credentials)) {
    if (provider !== "huggingface") continue;
    await sourceAuthorizationRequest(snapshot, provider, { token: credentials[provider] });
  }
  return true;
}

async function openSourceTerms(event, rawUrl) {
  assertTrustedSender(event);
  let url;
  try { url = new URL(rawUrl); } catch { throw new Error("模型许可地址无效"); }
  if (url.origin !== "https://huggingface.co" || url.username || url.password) {
    throw new Error("只允许打开 Hugging Face 官方许可页面");
  }
  await shell.openExternal(url.toString());
  return true;
}

function isAllowedUrl(rawUrl) {
  return rawUrl === APP_URL;
}

function assertTrustedSender(event) {
  const frame = event.senderFrame;
  if (!mainWindow || event.sender !== mainWindow.webContents ||
      frame !== mainWindow.webContents.mainFrame || frame.url !== APP_URL) {
    throw new Error("拒绝未授权的 Electron IPC 来源");
  }
}

function windowStatePath() {
  return path.join(app.getPath("userData"), WINDOW_STATE_FILE);
}

function readWindowState() {
  try {
    const value = JSON.parse(fs.readFileSync(windowStatePath(), "utf8"));
    if (!value || typeof value !== "object") return {};
    const state = {};
    for (const key of ["x", "y", "width", "height"]) {
      if (Number.isInteger(value[key])) state[key] = value[key];
    }
    state.maximized = value.maximized === true;
    return state;
  } catch (error) {
    if (error.code !== "ENOENT") console.error("Unable to read window state:", error.message);
    return {};
  }
}

function visibleBounds(state) {
  const width = Math.max(1050, Math.min(state.width || 1440, 7680));
  const height = Math.max(680, Math.min(state.height || 900, 4320));
  const candidate = { x: state.x, y: state.y, width, height };
  const positioned = Number.isInteger(candidate.x) && Number.isInteger(candidate.y);
  const visible = positioned && screen.getAllDisplays().some(({ workArea }) => {
    const overlapWidth = Math.min(candidate.x + width, workArea.x + workArea.width) - Math.max(candidate.x, workArea.x);
    const overlapHeight = Math.min(candidate.y + height, workArea.y + workArea.height) - Math.max(candidate.y, workArea.y);
    return overlapWidth >= 120 && overlapHeight >= 80;
  });
  return visible ? candidate : { width, height };
}

function saveWindowState(window) {
  if (window.isDestroyed()) return;
  const bounds = window.getNormalBounds();
  const value = { ...bounds, maximized: window.isMaximized() };
  try {
    fs.mkdirSync(app.getPath("userData"), { recursive: true });
    fs.writeFileSync(windowStatePath(), JSON.stringify(value), { mode: 0o600 });
  } catch (error) {
    console.error("Unable to save window state:", error.message);
  }
}

function desktopSettingsPath() {
  return path.join(app.getPath("userData"), DESKTOP_SETTINGS_FILE);
}

function readDesktopSettings() {
  try {
    const value = JSON.parse(fs.readFileSync(desktopSettingsPath(), "utf8"));
    return { trayEnabled: value?.trayEnabled === true };
  } catch (error) {
    if (error.code !== "ENOENT") console.error("Unable to read desktop settings:", error.message);
    return { trayEnabled: false };
  }
}

function saveDesktopSettings() {
  try {
    fs.mkdirSync(app.getPath("userData"), { recursive: true });
    fs.writeFileSync(desktopSettingsPath(), JSON.stringify(desktopSettings), { mode: 0o600 });
  } catch (error) {
    console.error("Unable to save desktop settings:", error.message);
  }
}

function focusMainWindow() {
  const window = mainWindow && !mainWindow.isDestroyed() ? mainWindow : BrowserWindow.getAllWindows()[0];
  if (!window) return;
  if (window.isMinimized()) window.restore();
  if (!window.isVisible()) window.show();
  window.focus();
}

function sendDesktopCommand(channel, value) {
  if (!mainWindow || mainWindow.isDestroyed() || mainWindow.webContents.isLoading()) return;
  mainWindow.webContents.send(channel, value);
}

function controlWindow(event, action) {
  assertTrustedSender(event);
  if (!mainWindow || mainWindow.isDestroyed()) return { maximized: false };
  if (action === "minimize") mainWindow.minimize();
  else if (action === "toggle-maximize") mainWindow.isMaximized() ? mainWindow.unmaximize() : mainWindow.maximize();
  else if (action === "close") mainWindow.close();
  else throw new Error("窗口操作不受支持");
  return { maximized: mainWindow.isMaximized() };
}

function trayImage() {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="#27d99a"/><path d="M8 24V8h5l3 7 3-7h5v16h-4v-9l-4 9-4-9v9z" fill="#09100f"/></svg>`;
  return nativeImage.createFromDataURL(`data:image/svg+xml;base64,${Buffer.from(svg).toString("base64")}`).resize({ width: 16, height: 16 });
}

function updateTray() {
  if (!desktopSettings.trayEnabled) {
    tray?.destroy();
    tray = null;
    return;
  }
  if (!tray) {
    tray = new Tray(trayImage());
    tray.setToolTip("MediaCenter");
    tray.on("double-click", focusMainWindow);
  }
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "打开 MediaCenter", click: focusMainWindow },
    { type: "separator" },
    { label: "完全退出", click: () => { isQuitting = true; app.quit(); } }
  ]));
}

function setTrayEnabled(enabled) {
  desktopSettings = { trayEnabled: enabled === true };
  saveDesktopSettings();
  updateTray();
  installApplicationMenu();
}

function installApplicationMenu() {
  const navigate = (label, accelerator, view) => ({
    label, accelerator, click: () => sendDesktopCommand("desktop:navigate", view)
  });
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    {
      label: "文件",
      submenu: [
        { label: "驻留系统托盘", type: "checkbox", checked: desktopSettings.trayEnabled, click: item => setTrayEnabled(item.checked) },
        { type: "separator" },
        { label: "完全退出", accelerator: "Ctrl+Q", click: () => { isQuitting = true; app.quit(); } }
      ]
    },
    {
      label: "导航",
      submenu: [
        navigate("运行总览", "Ctrl+1", "overview"), navigate("图片生成", "Ctrl+2", "image"),
        navigate("视频生成", "Ctrl+3", "video"), navigate("语音生成", "Ctrl+4", "speech"),
        navigate("音乐合成", "Ctrl+5", "music"), { type: "separator" },
        { label: "刷新控制面", accelerator: "Ctrl+R", click: () => sendDesktopCommand("desktop:refresh") }
      ]
    },
    { label: "窗口", submenu: [{ label: "显示并聚焦", accelerator: "Ctrl+Shift+M", click: focusMainWindow }] }
  ]));
}

function rendererFilePath(rawUrl) {
  const url = new URL(rawUrl);
  if (url.protocol !== `${RENDERER_SCHEME}:` || url.hostname !== "app") throw new Error("本地资源来源无效");
  const pathname = decodeURIComponent(url.pathname === "/" ? "/index.html" : url.pathname);
  if (pathname.includes("\0")) throw new Error("本地资源路径无效");
  const target = path.resolve(RENDERER_ROOT, `.${pathname}`);
  if (target !== RENDERER_ROOT && !target.startsWith(`${RENDERER_ROOT}${path.sep}`)) {
    throw new Error("本地资源路径越界");
  }
  return target;
}

async function handleRendererRequest(request) {
  if (request.method !== "GET") return new Response("Method Not Allowed", { status: 405 });
  try {
    const target = rendererFilePath(request.url);
    const stat = fs.statSync(target);
    if (!stat.isFile()) return new Response("Not Found", { status: 404 });
    if (stat.size > MAX_RENDERER_ASSET_BYTES) return new Response("Payload Too Large", { status: 413 });
    const contentType = RENDERER_CONTENT_TYPES[path.extname(target).toLowerCase()] || "application/octet-stream";
    return new Response(fs.readFileSync(target), {
      status: 200,
      headers: { "Content-Type": contentType, "Content-Length": String(stat.size), "Cache-Control": "no-store" }
    });
  } catch (error) {
    return new Response(error.code === "ENOENT" ? "Not Found" : "Bad Request", { status: error.code === "ENOENT" ? 404 : 400 });
  }
}

function apiUrl(rawPath, snapshot, artifactOnly = false) {
  if (typeof rawPath !== "string" || rawPath.length > 2048) throw new Error("API 路径无效");
  const url = new URL(rawPath, snapshot.profile.baseUrl);
  const prefix = artifactOnly ? "/api/v1/artifacts/" : "/api/v1/";
  if (url.origin !== new URL(snapshot.profile.baseUrl).origin || !url.pathname.startsWith(prefix)) throw new Error("API 路径越界");
  return url;
}

async function proxyApi(event, request) {
  assertTrustedSender(event);
  if (!request || typeof request !== "object") throw new Error("API 请求无效");
  const method = String(request.method || "GET").toUpperCase();
  if (!["GET", "POST", "PATCH"].includes(method)) throw new Error("API 方法不允许");
  const body = request.body == null ? undefined : String(request.body);
  if (body && body.length > 1024 * 1024) throw new Error("API 请求体过大");
  const idempotencyKey = request.idempotencyKey == null
    ? null : String(request.idempotencyKey);
  if (idempotencyKey !== null &&
      (!/^[A-Za-z0-9._:-]{1,128}$/u.test(idempotencyKey) || method !== "POST")) {
    throw new Error("API 幂等键无效");
  }
  let snapshot;
  try { snapshot = activeConnectionSnapshot(); }
  catch (error) { return { ok: false, status: 401, data: { error: { message: error.message } } }; }
  const url = apiUrl(request.path, snapshot);
  if (url.pathname.startsWith("/api/v1/source-authorizations/")) {
    throw new Error("模型来源凭据必须通过专用安全入口提交");
  }
  const headers = { "Content-Type": "application/json", "X-API-Key": snapshot.key };
  if (idempotencyKey !== null) headers["Idempotency-Key"] = idempotencyKey;
  const response = await net.fetch(url.toString(), {
    method,
    headers,
    body: method === "GET" ? undefined : body,
    redirect: "error"
  });
  if (response.url) apiUrl(response.url, snapshot);
  assertConnectionCurrent(snapshot);
  const text = await response.text();
  if (text.length > 2 * 1024 * 1024) throw new Error("API 响应过大");
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { throw new Error("服务器返回了无效 JSON"); }
  return { ok: response.ok, status: response.status, data };
}

async function fetchArtifact(event, rawPath) {
  assertTrustedSender(event);
  const snapshot = activeConnectionSnapshot();
  const url = apiUrl(rawPath, snapshot, true);
  const response = await net.fetch(url.toString(), {
    headers: { "X-API-Key": snapshot.key },
    redirect: "error"
  });
  if (response.url) apiUrl(response.url, snapshot, true);
  assertConnectionCurrent(snapshot);
  if (!response.ok) throw new Error(`产物读取失败 (${response.status})`);
  const declaredLength = Number(response.headers.get("content-length") || 0);
  if (declaredLength > MAX_PREVIEW_BYTES) throw new Error("产物超过 256 MiB 客户端预览上限");
  const buffer = await response.arrayBuffer();
  if (buffer.byteLength > MAX_PREVIEW_BYTES) throw new Error("产物超过 256 MiB 客户端预览上限");
  return {
    bytes: new Uint8Array(buffer),
    contentType: response.headers.get("content-type") || "application/octet-stream"
  };
}

async function uploadAsset(event, payload) {
  assertTrustedSender(event);
  const snapshot = activeConnectionSnapshot();
  if (!payload || typeof payload.name !== "string" || typeof payload.type !== "string" ||
      !(payload.bytes instanceof Uint8Array) || payload.bytes.byteLength > 256 * 1024 * 1024) {
    throw new Error("素材上传参数无效或超过 256MiB");
  }
  const url = apiUrl(`/api/v1/assets?filename=${encodeURIComponent(payload.name)}`, snapshot);
  const response = await net.fetch(url.toString(), {
    method: "POST", headers: { "Content-Type": payload.type, "X-API-Key": snapshot.key },
    body: Buffer.from(payload.bytes), redirect: "error"
  });
  if (response.url) apiUrl(response.url, snapshot);
  assertConnectionCurrent(snapshot);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error?.message || `素材上传失败 (${response.status})`);
  return data;
}

function modelUploadExtensions(format) {
  if (format === "safetensors") return new Set([".safetensors"]);
  if (format === "gguf") return new Set([".gguf"]);
  if (["diffusers", "transformers"].includes(format)) {
    return new Set([".json", ".safetensors", ".txt", ".model", ".tiktoken", ".md"]);
  }
  throw new Error("模型上传格式不受支持");
}

function collectModelFiles(selectedPath, format, directoryMode) {
  const allowed = modelUploadExtensions(format);
  const root = directoryMode ? selectedPath : path.dirname(selectedPath);
  const pending = [selectedPath];
  const files = [];
  while (pending.length) {
    const current = pending.pop();
    const stat = fs.lstatSync(current);
    if (stat.isSymbolicLink()) throw new Error("模型目录不能包含符号链接或 junction");
    if (stat.isDirectory()) {
      for (const name of fs.readdirSync(current)) pending.push(path.join(current, name));
      continue;
    }
    if (!stat.isFile()) throw new Error("模型目录包含不支持的文件类型");
    const extension = path.extname(current).toLowerCase();
    if (!allowed.has(extension)) throw new Error(`模型清单包含不受支持的文件：${path.basename(current)}`);
    const relativePath = directoryMode ? path.relative(root, current).split(path.sep).join("/") : path.basename(current);
    if (!relativePath || relativePath.startsWith("../") || relativePath.includes("\0")) throw new Error("模型文件路径越界");
    files.push({ absolutePath: current, relativePath, size: stat.size });
    if (files.length > 20000) throw new Error("模型文件数量超过 20000");
  }
  files.sort((left, right) => left.relativePath.localeCompare(right.relativePath));
  const folded = new Set();
  for (const file of files) {
    const key = file.relativePath.toLocaleLowerCase("en-US");
    if (folded.has(key)) throw new Error("模型文件路径存在大小写冲突");
    folded.add(key);
  }
  if (!files.length) throw new Error("没有可上传的模型文件");
  return files;
}

function inspectLocalSafetensors(file) {
  const handle = fs.openSync(file.absolutePath, "r");
  try {
    const prefix = Buffer.alloc(8);
    if (fs.readSync(handle, prefix, 0, 8, 0) !== 8) throw new Error("Safetensors 文件过短");
    const headerBytes = Number(prefix.readBigUInt64LE());
    if (!Number.isSafeInteger(headerBytes) || headerBytes < 2 || headerBytes > 16 * 1024 * 1024 || headerBytes > file.size - 8) {
      throw new Error("Safetensors Header 无效或超过 16 MiB");
    }
    const bytes = Buffer.alloc(headerBytes);
    if (fs.readSync(handle, bytes, 0, headerBytes, 8) !== headerBytes) throw new Error("Safetensors Header 读取不完整");
    let header;
    try { header = JSON.parse(bytes.toString("utf8")); } catch { throw new Error("Safetensors Header 不是有效 JSON"); }
    if (!header || Array.isArray(header) || typeof header !== "object") throw new Error("Safetensors Header 必须是对象");
    const metadata = header.__metadata__ && typeof header.__metadata__ === "object" ? header.__metadata__ : {};
    const names = Object.keys(header).filter(name => name !== "__metadata__");
    if (!names.length || names.length > 500000) throw new Error("Safetensors 张量数量无效");
    const folded = names.map(name => name.toLowerCase());
    const metadataText = [metadata["modelspec.architecture"], metadata["ss_network_module"]]
      .filter(value => typeof value === "string").join(" ").toLowerCase();
    const role = metadataText.includes("lora") || folded.some(name => name.includes("lora_") || name.endsWith(".alpha"))
      ? "lora"
      : folded.some(name => name.startsWith("encoder.") || name.startsWith("decoder.")) &&
        !folded.some(name => name.startsWith("model.diffusion_model.") || name.startsWith("conditioner.embedders."))
        ? "vae" : folded.some(name => name.startsWith("model.diffusion_model.") || name.startsWith("conditioner.embedders.") || name.startsWith("unet."))
          ? "checkpoint" : "unknown";
    const architecture = metadataText.includes("stable-diffusion-xl") || metadataText.includes("sdxl") ||
      folded.some(name => name.includes("conditioner.embedders.1") || name.includes("text_encoder_2") || name.includes("lora_te2"))
      ? "sdxl" : "unknown";
    const dtypes = new Set();
    const ranks = new Set();
    for (const name of names) {
      const spec = header[name];
      if (!spec || typeof spec !== "object" || typeof spec.dtype !== "string" || !Array.isArray(spec.shape)) {
        throw new Error("Safetensors 张量描述无效");
      }
      dtypes.add(spec.dtype.toLowerCase());
      if (name.toLowerCase().endsWith(".lora_down.weight") && Number.isSafeInteger(spec.shape[0])) ranks.add(spec.shape[0]);
    }
    const baseIdentity = typeof metadata.ss_new_sd_model_hash === "string" && /^[a-f0-9]{64}$/iu.test(metadata.ss_new_sd_model_hash)
      ? metadata.ss_new_sd_model_hash.toLowerCase() : null;
    return {
      role, architectureFamily: architecture,
      precision: dtypes.size === 1 ? [...dtypes][0] : "mixed",
      tensorCount: names.length,
      suggestedResolution: typeof metadata["modelspec.resolution"] === "string" ? metadata["modelspec.resolution"] : null,
      loraRank: ranks.size === 1 ? [...ranks][0] : ranks.size ? [...ranks].sort((a, b) => a - b) : null,
      loraAlpha: typeof metadata.ss_network_alpha === "string" ? metadata.ss_network_alpha : null,
      baseIdentity,
      sourceModelName: typeof metadata.ss_sd_model_name === "string" ? metadata.ss_sd_model_name.slice(0, 240) : null,
      title: typeof metadata["modelspec.title"] === "string" ? metadata["modelspec.title"].slice(0, 240) : null
    };
  } finally {
    fs.closeSync(handle);
  }
}

function inferDirectoryPreview(selectedPath, files) {
  const indexFile = files.find(file => file.relativePath === "model_index.json");
  if (!indexFile) throw new Error("Diffusers 目录缺少 model_index.json");
  let index;
  try { index = JSON.parse(fs.readFileSync(indexFile.absolutePath, "utf8")); } catch { throw new Error("model_index.json 无效"); }
  const pipelineClass = typeof index?._class_name === "string" ? index._class_name : "";
  return {
    role: "checkpoint",
    architectureFamily: pipelineClass.startsWith("StableDiffusionXL") ? "sdxl" : "unknown",
    precision: "unknown", tensorCount: null, suggestedResolution: null,
    loraRank: null, loraAlpha: null, baseIdentity: null, sourceModelName: path.basename(selectedPath),
    pipelineClass
  };
}

function publicModelUploadSession(session) {
  return {
    id: session.id, profileId: session.profileId, state: session.state,
    selection: session.selection, format: session.format, displayName: session.displayName,
    createdAt: session.createdAt, updatedAt: session.updatedAt,
    files: session.files.map(file => ({ relativePath: file.relativePath, size: file.size, sha256: file.sha256 || null })),
    totalBytes: session.files.reduce((sum, file) => sum + file.size, 0),
    preview: session.preview, transferId: session.transferId || null,
    reusedAsset: session.reusedAsset || null, error: session.error || null
  };
}

function updateModelUploadSession(session, changes) {
  Object.assign(session, changes, { updatedAt: new Date().toISOString() });
  saveModelUploadSessions();
  return publicModelUploadSession(session);
}

function requireModelUploadSession(sessionId, snapshot) {
  const session = modelUploadSessions.sessions.find(item => item.id === sessionId && item.profileId === snapshot.profile.id);
  if (!session) throw new Error("当前服务器没有该模型上传会话");
  return session;
}

async function pickModelForImport(event, request = {}) {
  assertTrustedSender(event);
  const snapshot = activeConnectionSnapshot();
  const directoryMode = request.selection === "directory";
  const selection = await dialog.showOpenDialog(mainWindow, {
    title: directoryMode ? "选择 Diffusers 模型目录" : "选择 Safetensors 模型",
    properties: directoryMode ? ["openDirectory"] : ["openFile"],
    filters: directoryMode ? undefined : [{ name: "安全模型文件", extensions: ["safetensors"] }]
  });
  if (selection.canceled || selection.filePaths.length !== 1) return { canceled: true };
  const format = directoryMode ? "diffusers" : "safetensors";
  const files = collectModelFiles(selection.filePaths[0], format, directoryMode);
  const preview = directoryMode ? inferDirectoryPreview(selection.filePaths[0], files) : inspectLocalSafetensors(files[0]);
  const stat = fs.statSync(selection.filePaths[0]);
  const now = new Date().toISOString();
  const session = {
    id: randomUUID(), profileId: snapshot.profile.id, state: "selected",
    selection: directoryMode ? "directory" : "file", format,
    displayName: preview.title || path.basename(selection.filePaths[0], path.extname(selection.filePaths[0])),
    files: files.map(file => ({ ...file, mtimeMs: fs.statSync(file.absolutePath).mtimeMs })),
    preview, sourceMtimeMs: stat.mtimeMs, transferId: null, createdAt: now, updatedAt: now
  };
  modelUploadSessions.sessions.push(session);
  saveModelUploadSessions();
  return { canceled: false, session: publicModelUploadSession(session) };
}

function listModelUploadSessions(event) {
  assertTrustedSender(event);
  const snapshot = activeConnectionSnapshot();
  return modelUploadSessions.sessions.filter(session => session.profileId === snapshot.profile.id)
    .map(publicModelUploadSession);
}

async function sha256ModelFile(file, session, snapshot) {
  const stat = await fs.promises.stat(file.absolutePath);
  if (!stat.isFile() || stat.size !== file.size || stat.mtimeMs !== file.mtimeMs) throw new Error(`本地模型文件已变化：${file.relativePath}`);
  const digest = createHash("sha256");
  let processed = 0;
  await new Promise((resolve, reject) => {
    const stream = fs.createReadStream(file.absolutePath, { highWaterMark: 4 * 1024 * 1024 });
    stream.on("data", chunk => {
      digest.update(chunk); processed += chunk.length;
      mainWindow?.webContents.send("model-transfer:progress", {
        profileId: snapshot.profile.id, revision: snapshot.revision, sessionId: session.id,
        stage: "hashing", relativePath: file.relativePath, receivedBytes: processed, totalBytes: file.size
      });
    });
    stream.on("error", reject); stream.on("end", resolve);
  });
  return digest.digest("hex");
}

async function modelJsonRequest(snapshot, rawPath, method, body) {
  const url = apiUrl(rawPath, snapshot);
  const response = await net.fetch(url.toString(), {
    method, headers: { "Content-Type": "application/json", "X-API-Key": snapshot.key },
    body: body == null ? undefined : JSON.stringify(body), redirect: "error"
  });
  if (response.url) apiUrl(response.url, snapshot);
  assertConnectionCurrent(snapshot);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error?.message || `模型请求失败 (${response.status})`);
  return data;
}

async function uploadModelSessionFiles(session, transfer, snapshot, control) {
  const serverFiles = new Map(transfer.files.map(file => [file.relative_path, file]));
  const chunkSize = Math.min(Number(transfer.max_chunk_bytes || 8 * 1024 * 1024), 8 * 1024 * 1024);
  for (const file of session.files) {
    const serverFile = serverFiles.get(file.relativePath);
    if (!serverFile || serverFile.expected_bytes !== file.size) throw new Error("服务器上传清单与本地文件不一致");
    let offset = Number(serverFile.received_bytes || 0);
    const stat = await fs.promises.stat(file.absolutePath);
    if (!stat.isFile() || stat.size !== file.size || stat.mtimeMs !== file.mtimeMs) throw new Error(`本地模型文件已变化：${file.relativePath}`);
    const handle = await fs.promises.open(file.absolutePath, "r");
    try {
      while (offset < file.size) {
        if (control?.pauseRequested) return;
        assertConnectionCurrent(snapshot);
        const buffer = Buffer.allocUnsafe(Math.min(chunkSize, file.size - offset));
        const { bytesRead } = await handle.read(buffer, 0, buffer.length, offset);
        if (control?.pauseRequested) return;
        if (!bytesRead) throw new Error("读取模型文件时提前结束");
        const url = apiUrl(`/api/v1/model-transfers/${transfer.id}/files/${serverFile.id}?offset=${offset}`, snapshot);
        const response = await net.fetch(url.toString(), {
          method: "PUT", headers: { "Content-Type": "application/octet-stream",
                                    "X-API-Key": snapshot.key },
          body: buffer.subarray(0, bytesRead), redirect: "error"
        });
        if (response.url) apiUrl(response.url, snapshot);
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error?.message || `模型分块上传失败 (${response.status})`);
        offset = Number(data.received_bytes);
        mainWindow?.webContents.send("model-transfer:progress", {
          profileId: snapshot.profile.id, revision: snapshot.revision,
          sessionId: session.id, transferId: transfer.id, stage: "uploading",
          relativePath: file.relativePath, receivedBytes: offset, totalBytes: file.size
        });
      }
    } finally {
      await handle.close();
    }
  }
}

async function startModelUpload(event, request) {
  assertTrustedSender(event);
  if (!request || typeof request.sessionId !== "string") throw new Error("模型上传会话无效");
  const snapshot = activeConnectionSnapshot();
  const session = requireModelUploadSession(request.sessionId, snapshot);
  if (modelUploadRuns.has(session.id)) throw new Error("该模型上传仍在执行，请等待当前操作完成");
  const control = { kind: "upload", pauseRequested: false, pausePromise: null, resolveDrain: null, drained: null };
  control.drained = new Promise(resolve => { control.resolveDrain = resolve; });
  modelUploadRuns.set(session.id, control);
  try {
    updateModelUploadSession(session, { state: "hashing", error: null });
    for (const file of session.files) {
      if (!file.sha256) {
        file.sha256 = await sha256ModelFile(file, session, snapshot);
        saveModelUploadSessions();
      }
    }
    assertConnectionCurrent(snapshot);
    const manifest = session.files.map(file => ({
      relative_path: file.relativePath, byte_size: file.size, sha256: file.sha256
    }));
    updateModelUploadSession(session, { state: "checking-reuse" });
    const reuse = await modelJsonRequest(snapshot, "/api/v1/model-imports/preflight", "POST", { files: manifest });
    if (reuse.disposition === "reuse" && reuse.asset) {
      updateModelUploadSession(session, { state: "completed", reusedAsset: reuse.asset, transferId: null });
      return { disposition: "reused", session: publicModelUploadSession(session), asset: reuse.asset };
    }
    let transfer = null;
    if (session.transferId) {
      // A failed observation is not a missing transfer. Preserve its identity.
      transfer = await modelJsonRequest(snapshot, `/api/v1/model-transfers/${session.transferId}`, "GET");
    }
    if (transfer?.state === "succeeded") {
      updateModelUploadSession(session, { state: "completed" });
      return { disposition: "uploaded", session: publicModelUploadSession(session), transfer };
    }
    if (transfer?.state === "paused") {
      transfer = await modelJsonRequest(snapshot, `/api/v1/model-transfers/${transfer.id}/resume`, "POST", {});
    } else if (transfer && !["queued", "transferring"].includes(transfer.state)) {
      transfer = null;
      session.transferId = null;
    }
    if (!transfer) {
      const detectedRole = session.preview.role;
      const role = request.role || detectedRole;
      if (!role || role === "unknown") throw new Error("无法自动判断模型角色，请在确认页选择基础模型、LoRA 或 VAE");
      transfer = await modelJsonRequest(snapshot, "/api/v1/model-transfers", "POST", {
        direction: "upload",
        display_name: String(request.displayName || session.displayName || "").trim(),
        media_kind: request.mediaKind || "image", role, format: session.format,
        revision: String(request.revision || manifest[0].sha256).trim(),
        license_declared: String(request.licenseDeclared || "unknown").trim(),
        files: manifest
      });
      session.transferId = transfer.id;
    }
    updateModelUploadSession(session, { state: "uploading", transferId: transfer.id });
    try { await uploadModelSessionFiles(session, transfer, snapshot, control); }
    finally { control.resolveDrain(); }
    if (control.pauseRequested) return { disposition: "paused", ...await control.pausePromise };
    updateModelUploadSession(session, { state: "server-verifying" });
    const completed = await modelJsonRequest(snapshot, `/api/v1/model-transfers/${transfer.id}/complete`, "POST", {});
    updateModelUploadSession(session, { state: "completed" });
    return { disposition: "uploaded", session: publicModelUploadSession(session), transfer: completed };
  } catch (error) {
    control.resolveDrain();
    if (control.pauseRequested) {
      try { return { disposition: "paused", ...await control.pausePromise }; }
      catch (pauseError) { error = pauseError; }
    }
    updateModelUploadSession(session, { state: "failed", error: String(error?.message || error) });
    throw error;
  } finally {
    control.resolveDrain();
    modelUploadRuns.delete(session.id);
  }
}

async function pauseModelUpload(event, sessionId) {
  assertTrustedSender(event);
  const snapshot = activeConnectionSnapshot();
  const session = requireModelUploadSession(sessionId, snapshot);
  if (!session.transferId) throw new Error("模型上传尚未开始");
  let control = modelUploadRuns.get(session.id);
  if (control?.pausePromise) return control.pausePromise;
  if (control && control.kind !== "upload") throw new Error("该模型会话仍在执行其他操作");
  if (control && session.state !== "uploading") throw new Error("当前阶段不能暂停上传");
  const ownsControl = !control;
  if (!control) {
    control = { kind: "pause", pauseRequested: true, pausePromise: null, drained: Promise.resolve() };
    modelUploadRuns.set(session.id, control);
  }
  control.pauseRequested = true;
  const pause = async () => {
    // Stop producing chunks and drain the one already in flight before the
    // server changes state. No rejected late chunk can overwrite paused.
    await control.drained;
    const transfer = await modelJsonRequest(snapshot, `/api/v1/model-transfers/${session.transferId}/pause`, "POST", {});
    updateModelUploadSession(session, { state: "paused", error: null });
    return { session: publicModelUploadSession(session), transfer };
  };
  control.pausePromise = pause().finally(() => {
    if (ownsControl && modelUploadRuns.get(session.id) === control) modelUploadRuns.delete(session.id);
  });
  return control.pausePromise;
}

async function discardModelUpload(event, sessionId) {
  assertTrustedSender(event);
  const snapshot = activeConnectionSnapshot();
  const session = requireModelUploadSession(sessionId, snapshot);
  if (modelUploadRuns.has(session.id)) throw new Error("请先暂停当前上传，再取消会话");
  const control = { kind: "discard" };
  modelUploadRuns.set(session.id, control);
  try {
    if (session.transferId && ["selected", "hashing", "checking-reuse", "uploading", "paused", "failed"].includes(session.state)) {
      await modelJsonRequest(snapshot, `/api/v1/model-transfers/${session.transferId}/cancel`, "POST", {});
    }
    modelUploadSessions.sessions = modelUploadSessions.sessions.filter(item => item.id !== session.id);
    saveModelUploadSessions();
    return true;
  } finally {
    if (modelUploadRuns.get(session.id) === control) modelUploadRuns.delete(session.id);
  }
}

async function pickAndUploadModel(event, request) {
  assertTrustedSender(event);
  if (!request || typeof request !== "object") throw new Error("模型上传参数无效");
  const format = String(request.format || "");
  const directoryMode = request.selection === "directory";
  if (directoryMode && !["diffusers", "transformers"].includes(format)) {
    throw new Error("只有目录型模型格式支持上传目录");
  }
  if (!directoryMode && !["safetensors", "gguf"].includes(format)) {
    throw new Error("目录型模型请选择上传目录");
  }
  const selection = await dialog.showOpenDialog(mainWindow, {
    title: directoryMode ? "选择模型目录" : "选择模型文件",
    properties: directoryMode ? ["openDirectory"] : ["openFile"],
    filters: directoryMode ? undefined : [{ name: "模型文件", extensions: [...modelUploadExtensions(format)].map(value => value.slice(1)) }]
  });
  if (selection.canceled || selection.filePaths.length !== 1) return { canceled: true };
  const files = collectModelFiles(selection.filePaths[0], format, directoryMode);
  const snapshot = activeConnectionSnapshot();
  const transfer = await modelJsonRequest(snapshot, "/api/v1/model-transfers", "POST", {
    direction: "upload", display_name: String(request.displayName || "").trim(),
    media_kind: request.mediaKind, role: request.role, format,
    revision: String(request.revision || "").trim(),
    license_declared: String(request.licenseDeclared || "unknown").trim(),
    files: files.map(file => ({ relative_path: file.relativePath, byte_size: file.size }))
  });
  const serverFiles = new Map(transfer.files.map(file => [file.relative_path, file]));
  const chunkSize = Math.min(Number(transfer.max_chunk_bytes || 8 * 1024 * 1024), 8 * 1024 * 1024);
  for (const file of files) {
    const serverFile = serverFiles.get(file.relativePath);
    if (!serverFile) throw new Error("服务器上传清单与本地文件不一致");
    let offset = Number(serverFile.received_bytes || 0);
    const handle = await fs.promises.open(file.absolutePath, "r");
    try {
      while (offset < file.size) {
        assertConnectionCurrent(snapshot);
        const buffer = Buffer.allocUnsafe(Math.min(chunkSize, file.size - offset));
        const { bytesRead } = await handle.read(buffer, 0, buffer.length, offset);
        if (!bytesRead) throw new Error("读取模型文件时提前结束");
        const url = apiUrl(`/api/v1/model-transfers/${transfer.id}/files/${serverFile.id}?offset=${offset}`, snapshot);
        const response = await net.fetch(url.toString(), {
          method: "PUT", headers: { "Content-Type": "application/octet-stream",
                                    "X-API-Key": snapshot.key },
          body: buffer.subarray(0, bytesRead), redirect: "error"
        });
        if (response.url) apiUrl(response.url, snapshot);
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error?.message || `模型分块上传失败 (${response.status})`);
        offset += bytesRead;
        mainWindow?.webContents.send("model-transfer:progress", {
          profileId: snapshot.profile.id, revision: snapshot.revision,
          transferId: transfer.id, relativePath: file.relativePath,
          receivedBytes: data.received_bytes, totalBytes: data.expected_bytes
        });
      }
    } finally {
      await handle.close();
    }
  }
  const completed = await modelJsonRequest(
    snapshot, `/api/v1/model-transfers/${transfer.id}/complete`, "POST", {});
  return { canceled: false, transfer: completed };
}

function mediaExtensions(kind) {
  if (kind === "image") return [".png", ".jpg", ".jpeg", ".webp"];
  if (kind === "video") return Object.keys(MEDIA_TYPES);
  if (kind === "speech" || kind === "music") return [".wav", ".mp3", ".flac", ".ogg"];
  throw new Error("素材类型无效");
}

function acceptedMediaExtensions(kind, requested) {
  if (!Array.isArray(requested) || !requested.length) return mediaExtensions(kind);
  const prefixes = requested.filter(value => typeof value === "string" && ["image/", "video/", "audio/"].includes(value));
  if (!prefixes.length) throw new Error("素材类型合同无效");
  const extensions = Object.entries(MEDIA_TYPES).filter(([, mime]) => prefixes.some(prefix => mime.startsWith(prefix))).map(([extension]) => extension);
  if (!extensions.length) throw new Error("素材类型合同没有可用格式");
  return extensions;
}

function safeFilename(value, fallback = "mediacenter-artifact") {
  const name = path.basename(String(value || "")).replace(/[<>:"/\\|?*\x00-\x1f]/gu, "_").trim();
  return name && name.length <= 180 ? name : fallback;
}

async function pickAssets(event, request) {
  assertTrustedSender(event);
  const kind = request?.kind;
  const allowed = new Set(acceptedMediaExtensions(kind, request?.accept));
  const result = await dialog.showOpenDialog(mainWindow, {
    title: "选择 MediaCenter 素材",
    properties: request?.multiple === true ? ["openFile", "multiSelections"] : ["openFile"],
    filters: [{ name: "支持的媒体", extensions: [...allowed].map(value => value.slice(1)) }]
  });
  if (result.canceled) return [];
  if (result.filePaths.length > 8) throw new Error("一次最多选择 8 个素材");
  const files = [];
  let totalBytes = 0;
  for (const filePath of result.filePaths) {
    const extension = path.extname(filePath).toLowerCase();
    if (!allowed.has(extension)) throw new Error("选择的素材类型不受支持");
    const stat = fs.statSync(filePath);
    if (!stat.isFile() || stat.size <= 0 || stat.size > MAX_ASSET_BYTES) throw new Error("素材为空或超过 256 MiB");
    totalBytes += stat.size;
    if (totalBytes > MAX_ASSET_BYTES) throw new Error("本次选择的素材总计超过 256 MiB");
    files.push({
      name: safeFilename(filePath, `asset${extension}`),
      type: MEDIA_TYPES[extension],
      size: stat.size,
      bytes: new Uint8Array(fs.readFileSync(filePath))
    });
  }
  return files;
}

async function saveArtifact(event, request) {
  assertTrustedSender(event);
  const snapshot = activeConnectionSnapshot();
  const url = apiUrl(request?.path, snapshot, true);
  const extension = path.extname(url.pathname).toLowerCase();
  if (!MEDIA_TYPES[extension]) throw new Error("产物扩展名不受支持");
  const suggestedName = safeFilename(request?.suggestedName, `mediacenter-artifact${extension}`);
  const selection = await dialog.showSaveDialog(mainWindow, {
    title: "保存 MediaCenter 产物",
    defaultPath: suggestedName,
    filters: [{ name: "MediaCenter 产物", extensions: [extension.slice(1)] }]
  });
  if (selection.canceled || !selection.filePath) return { saved: false };
  const selectedExtension = path.extname(selection.filePath).toLowerCase();
  if (selectedExtension && selectedExtension !== extension) {
    throw new Error(`保存文件扩展名必须为 ${extension}`);
  }
  const outputPath = selectedExtension ? selection.filePath : `${selection.filePath}${extension}`;
  assertConnectionCurrent(snapshot);
  const response = await net.fetch(url.toString(), { headers: { "X-API-Key": snapshot.key }, redirect: "error" });
  if (response.url) apiUrl(response.url, snapshot, true);
  assertConnectionCurrent(snapshot);
  if (!response.ok) throw new Error(`产物读取失败 (${response.status})`);
  const declaredLength = Number(response.headers.get("content-length") || 0);
  if (declaredLength > MAX_ARTIFACT_BYTES) throw new Error("产物超过 1 GiB 客户端保存上限");
  const bytes = Buffer.from(await response.arrayBuffer());
  if (bytes.byteLength > MAX_ARTIFACT_BYTES) throw new Error("产物超过 1 GiB 客户端保存上限");
  fs.writeFileSync(outputPath, bytes, { flag: "w", mode: 0o600 });
  return { saved: true, filename: path.basename(outputPath) };
}

function showTaskNotification(event, payload) {
  assertTrustedSender(event);
  const statuses = { succeeded: "已完成", failed: "失败", canceled: "已取消" };
  const services = { image: "图片", video: "视频", speech: "语音", music: "音乐" };
  const taskId = typeof payload?.taskId === "string" && /^tsk_[a-z0-9]+$/u.test(payload.taskId) ? payload.taskId : "";
  if (!taskId || !statuses[payload?.status] || !services[payload?.service] || !Notification.isSupported()) return false;
  const notification = new Notification({
    title: `${services[payload.service]}任务${statuses[payload.status]}`,
    body: taskId,
    silent: false
  });
  notification.on("click", focusMainWindow);
  notification.show();
  return true;
}

async function loadRenderer(window) {
  try {
    await window.loadURL(APP_URL);
  } catch {
    const result = await dialog.showMessageBox(window, {
      type: "error",
      title: "MediaCenter 客户端资源损坏",
      message: "无法加载本地桌面界面。",
      detail: "请重新安装 MediaCenter 客户端。服务器离线不会触发此错误。",
      buttons: ["重试", "退出"],
      defaultId: 0,
      cancelId: 1,
      noLink: true
    });
    if (result.response === 0) return loadRenderer(window);
    app.quit();
  }
}

function createWindow() {
  const savedState = readWindowState();
  const window = new BrowserWindow({
    ...visibleBounds(savedState),
    minWidth: 1050,
    minHeight: 680,
    show: false,
    autoHideMenuBar: true,
    backgroundColor: "#0b1020",
    title: "MediaCenter",
    frame: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false
    }
  });
  mainWindow = window;

  window.webContents.on("will-navigate", (event, url) => {
    if (!isAllowedUrl(url)) event.preventDefault();
  });
  window.webContents.on("will-redirect", event => event.preventDefault());
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.on("close", event => {
    saveWindowState(window);
    if (desktopSettings.trayEnabled && !isQuitting) {
      event.preventDefault();
      window.hide();
    }
  });
  window.on("closed", () => { if (mainWindow === window) mainWindow = null; });
  window.on("maximize", () => sendDesktopCommand("desktop:window-state", { maximized: true }));
  window.on("unmaximize", () => sendDesktopCommand("desktop:window-state", { maximized: false }));
  window.once("ready-to-show", () => {
    if (savedState.maximized) window.maximize();
    window.show();
  });
  void loadRenderer(window);
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", focusMainWindow);

  app.whenReady().then(() => {
    desktopSettings = readDesktopSettings();
    initializeConnections();
    protocol.handle(RENDERER_SCHEME, handleRendererRequest);
    session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
    session.defaultSession.setPermissionCheckHandler(() => false);
    ipcMain.handle("connections:list", listConnections);
    ipcMain.handle("connections:connect", connectServer);
    ipcMain.handle("connections:switch", switchServer);
    ipcMain.handle("connections:logout", logoutServer);
    ipcMain.handle("connections:remove", removeServer);
    ipcMain.handle("api:request", proxyApi);
    ipcMain.handle("source-authorization:configure", configureSourceAuthorization);
    ipcMain.handle("source-authorization:open-terms", openSourceTerms);
    ipcMain.handle("events:start", startServerEvents);
    ipcMain.handle("events:stop", stopServerEventsFromRenderer);
    ipcMain.handle("artifact:fetch", fetchArtifact);
    ipcMain.handle("artifact:save", saveArtifact);
    ipcMain.handle("asset:upload", uploadAsset);
    ipcMain.handle("asset:pick", pickAssets);
    ipcMain.handle("model:pick-and-upload", pickAndUploadModel);
    ipcMain.handle("model:pick-for-import", pickModelForImport);
    ipcMain.handle("model:upload-sessions", listModelUploadSessions);
    ipcMain.handle("model:start-upload", startModelUpload);
    ipcMain.handle("model:resume-upload", startModelUpload);
    ipcMain.handle("model:pause-upload", pauseModelUpload);
    ipcMain.handle("model:discard-upload", discardModelUpload);
    ipcMain.handle("notification:task", showTaskNotification);
    ipcMain.handle("window:control", controlWindow);
    installApplicationMenu();
    updateTray();
    createWindow();
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });
}

app.on("before-quit", () => { isQuitting = true; stopServerEvents(); });
app.on("window-all-closed", () => { if (!desktopSettings.trayEnabled) app.quit(); });
