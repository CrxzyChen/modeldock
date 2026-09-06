/// <reference types="vite/client" />

import type {
  ConnectionState,
  MediaAsset,
  ModelTransferProgress,
  ServerEventEnvelope,
  ServerEventStatus,
} from "./types/contracts";

declare global {
  interface DesktopApiResult<T = unknown> {
    ok: boolean;
    status: number;
    data: T;
  }

  interface MediaCenterDesktopBridge {
    listConnections(): Promise<ConnectionState>;
    connectServer(request: Record<string, unknown>): Promise<ConnectionState>;
    switchServer(profileId: string): Promise<ConnectionState>;
    logoutServer(): Promise<ConnectionState>;
    removeServer(profileId: string): Promise<ConnectionState>;
    request<T = unknown>(
      path: string,
      options?: { method?: string; body?: string; idempotencyKey?: string },
    ): Promise<DesktopApiResult<T>>;
    configureSourceAuthorization(
      request: Record<string, unknown>,
    ): Promise<unknown>;
    openSourceTerms(url: string): Promise<boolean>;
    startEvents(): Promise<Record<string, unknown>>;
    stopEvents(): Promise<void>;
    onServerEvent(callback: (event: ServerEventEnvelope) => void): () => void;
    onServerEventStatus(
      callback: (status: ServerEventStatus) => void,
    ): () => void;
    fetchArtifact(
      path: string,
    ): Promise<{ bytes: Uint8Array; contentType?: string }>;
    saveArtifact(
      path: string,
      suggestedName: string,
    ): Promise<{ saved: boolean; path?: string }>;
    uploadAsset(payload: Record<string, unknown>): Promise<MediaAsset>;
    pickAssets(
      options: Record<string, unknown>,
    ): Promise<
      Array<{ name: string; type: string; size: number; bytes: Uint8Array }>
    >;
    pickAndUploadModel(
      options: Record<string, unknown>,
    ): Promise<Record<string, unknown>>;
    pickModelForImport(
      options: Record<string, unknown>,
    ): Promise<Record<string, unknown>>;
    listModelUploadSessions(): Promise<Record<string, unknown>[]>;
    startModelUpload(
      options: Record<string, unknown>,
    ): Promise<Record<string, unknown>>;
    resumeModelUpload(
      options: Record<string, unknown>,
    ): Promise<Record<string, unknown>>;
    pauseModelUpload(sessionId: string): Promise<Record<string, unknown>>;
    discardModelUpload(sessionId: string): Promise<boolean>;
    onModelTransferProgress(
      callback: (progress: ModelTransferProgress) => void,
    ): () => void;
    notifyTask(payload: Record<string, unknown>): Promise<boolean>;
    windowControl(action: string): Promise<{ maximized: boolean }>;
    onWindowState(
      callback: (state: { maximized: boolean }) => void,
    ): () => void;
    onNavigate(callback: (view: string) => void): () => void;
    onRefresh(callback: () => void): () => void;
  }

  interface Window {
    mediaCenterDesktop?: MediaCenterDesktopBridge;
  }
}
