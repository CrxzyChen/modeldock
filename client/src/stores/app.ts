import { computed, reactive, ref, shallowRef } from "vue";
import { defineStore } from "pinia";
import { ApiError, api } from "@/services/api";
import { desktopBridge, hasDesktopBridge } from "@/services/desktop";
import { canCancelTask, mediaNames, taskErrorMessage, taskRetryDisabledReason, taskStatusLabel, taskTimedOut } from "@/lib/format";
import type { ImageGenerationDraft } from "@/lib/models";
import { mergeDeploymentOperations } from "@/lib/models";
import type {
  ActionState,
  ConnectionProfile,
  ConnectionState,
  ListResponse,
  MediaAsset,
  MediaKind,
  MediaService,
  MediaTask,
  ModelTransferProgress,
  Overview,
  ResourceItem,
  ServerEventEnvelope,
  ServerEventStatus,
  StatusMessage,
  ToastMessage,
  RuntimeProfile,
  DeploymentPlan,
  DeploymentOperation,
  AssetCompatibility,
} from "@/types/contracts";

export type AppView =
  | "overview"
  | "image"
  | "video"
  | "speech"
  | "music"
  | "services"
  | "deployments"
  | "hardware"
  | "audit";
export type StatusCenterKind = "task" | "message";

const MESSAGE_KEY = "mediacenter_status_messages_v1";
const MESSAGE_LIMIT = 100;
const MAX_ASSET_BYTES = 256 * 1024 * 1024;

function loadMessages(): StatusMessage[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(MESSAGE_KEY) ?? "[]");
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter(
        (item) =>
          item && typeof item.id === "string" && typeof item.title === "string",
      )
      .slice(0, MESSAGE_LIMIT);
  } catch {
    return [];
  }
}

function replaceItem<T extends { id: string; version?: number }>(
  items: T[],
  incoming: T,
  deleted = false,
): T[] {
  const current = items.find((item) => item.id === incoming.id);
  if (
    !deleted &&
    Number.isInteger(current?.version) &&
    Number.isInteger(incoming.version) &&
    Number(incoming.version) < Number(current?.version)
  )
    return items;
  const next = items.filter((item) => item.id !== incoming.id);
  if (!deleted) next.unshift(incoming);
  return next;
}

export const useAppStore = defineStore("app", () => {
  const initialized = ref(false);
  const authenticated = ref(false);
  const loginMessage = ref("");
  const loginSelection = ref<string | null>(null);
  const connectionEpoch = ref(0);
  const connections = ref<ConnectionState>({
    activeProfileId: null,
    revision: 0,
    profiles: [],
  });
  const activeView = ref<AppView>("overview");
  const connectionState = ref<"idle" | "busy" | "error">("idle");
  const connectionLabel = ref("准备连接");
  const lastSyncAt = ref<string | null>(null);
  const overview = ref<Overview>({
    services_online: 0,
    active_tasks: 0,
    completed_tasks: 0,
    failed_tasks: 0,
  });
  const services = ref<MediaService[]>([]);
  const tasks = ref<MediaTask[]>([]);
  const deployments = ref<ResourceItem[]>([]);
  const catalog = ref<ResourceItem[]>([]);
  const installations = ref<ResourceItem[]>([]);
  const assets = ref<ResourceItem[]>([]);
  const transfers = ref<ResourceItem[]>([]);
  const runtimeProfiles = ref<RuntimeProfile[]>([]);
  const deploymentOperations = ref<DeploymentOperation[]>([]);
  const modelSettingsRequest = ref<{ deploymentId: string; nonce: string } | null>(null);
  const focusedDeploymentId = ref<string | null>(null);
  const assetCompatibilities = ref<AssetCompatibility[]>([]);
  const modelImportProgress = reactive<Record<string, ModelTransferProgress>>(
    {},
  );
  const gpuResources = shallowRef<Record<string, any> | null>(null);
  const hardware = shallowRef<Record<string, any> | null>(null);
  const audit = ref<ResourceItem[]>([]);
  const statusMessages = ref<StatusMessage[]>(loadMessages());
  const toasts = ref<ToastMessage[]>([]);
  const actionStates = reactive<Record<string, ActionState | undefined>>({});
  const openStatusCenter = ref<StatusCenterKind | null>(null);
  const expandedMessageId = ref<string | null>(null);
  const userMenuOpen = ref(false);
  const selectedTaskIds = reactive<Partial<Record<MediaKind, string>>>({});
  const generationInputs = reactive<Record<string, MediaAsset[]>>({});
  // Client memory only, scoped by server profile and model. Survives view changes.
  const imageGenerationDrafts = reactive<Record<string, ImageGenerationDraft>>({});
  const confirmation = ref<{
    open: boolean;
    title: string;
    message: string;
    confirmLabel: string;
    tone: "normal" | "danger";
  }>({
    open: false,
    title: "",
    message: "",
    confirmLabel: "确认",
    tone: "normal",
  });
  let confirmationResolver: ((accepted: boolean) => void) | null = null;
  const taskBaseline = new Map<string, string>();
  const eventTimers = new Map<string, ReturnType<typeof setTimeout>>();
  const eventVersions = new Map<string, number>();
  let refreshPromise: Promise<boolean> | null = null;
  // All deployment reads share one ordering fence, including full snapshots.
  // A late pre-removal response must not unlock an instance or hide retry.
  let deploymentReadSequence = 0;
  let snapshotTimer: ReturnType<typeof setTimeout> | null = null;
  let listenersBound = false;

  const activeProfile = computed<ConnectionProfile | null>(
    () =>
      connections.value.profiles.find(
        (profile) => profile.id === connections.value.activeProfileId,
      ) ?? null,
  );
  const activeTasks = computed(() =>
    tasks.value.filter((task) =>
      ["queued", "assigned", "running", "cancel_requested"].includes(
        task.status,
      ),
    ),
  );
  const backgroundCount = computed(
    () =>
      activeTasks.value.length +
      installations.value.filter((item) =>
        [
          "preflight",
          "downloading",
          "paused",
          "verifying",
          "preparing",
          "checking",
        ].includes(String(item.state)),
      ).length +
      transfers.value.filter((item) =>
        ["queued", "transferring", "paused", "verifying"].includes(
          String(item.state),
        ),
      ).length +
      deploymentOperations.value.filter(
        (item) => !["ready", "failed", "canceled"].includes(item.state),
      ).length,
  );
  const unreadCount = computed(
    () => statusMessages.value.filter((message) => !message.read).length,
  );
  const readyModelCount = computed(() =>
    services.value.reduce(
      (sum, service) =>
        sum + service.models.filter((model) => model.healthy).length,
      0,
    ),
  );

  function saveMessages() {
    localStorage.setItem(
      MESSAGE_KEY,
      JSON.stringify(statusMessages.value.slice(0, MESSAGE_LIMIT)),
    );
  }

  function addMessage(
    title: string,
    options: {
      detail?: string;
      type?: "info" | "error";
      task?: MediaTask;
      taskId?: string;
      service?: MediaKind;
    } = {},
  ) {
    const message: StatusMessage = {
      id: `${Date.now()}-${crypto.randomUUID()}`,
      title,
      detail: String(options.detail ?? ""),
      type: options.type === "error" ? "error" : "info",
      createdAt: new Date().toISOString(),
      read: false,
      serverId: activeProfile.value?.id ?? "desktop",
      serverName: activeProfile.value?.name ?? "MediaCenter",
      taskId: options.task?.id ?? options.taskId ?? null,
      service: options.task?.service ?? options.service ?? null,
    };
    statusMessages.value = [message, ...statusMessages.value].slice(
      0,
      MESSAGE_LIMIT,
    );
    saveMessages();
    return message;
  }

  function markMessageRead(id: string) {
    const message = statusMessages.value.find((item) => item.id === id);
    if (message && !message.read) {
      message.read = true;
      saveMessages();
    }
    expandedMessageId.value = expandedMessageId.value === id ? null : id;
  }

  function markAllMessagesRead() {
    statusMessages.value.forEach((message) => {
      message.read = true;
    });
    saveMessages();
  }

  function clearMessages() {
    statusMessages.value = [];
    expandedMessageId.value = null;
    saveMessages();
  }

  function toast(
    title: string,
    options: {
      detail?: string;
      type?: "success" | "error";
      persistent?: boolean;
      record?: boolean;
    } = {},
  ) {
    const item: ToastMessage = {
      id: crypto.randomUUID(),
      title,
      detail: options.detail ?? "",
      type: options.type ?? "success",
      persistent: options.persistent === true,
    };
    toasts.value.push(item);
    if (options.record !== false)
      addMessage(title, {
        detail: item.detail,
        type: item.type === "error" ? "error" : "info",
      });
    if (!item.persistent) setTimeout(() => dismissToast(item.id), 3600);
    return item.id;
  }

  function dismissToast(id: string) {
    toasts.value = toasts.value.filter((item) => item.id !== id);
  }

  function confirm(
    title: string,
    message: string,
    confirmLabel = "确认",
    tone: "normal" | "danger" = "danger",
  ) {
    if (confirmationResolver) confirmationResolver(false);
    confirmation.value = { open: true, title, message, confirmLabel, tone };
    return new Promise<boolean>((resolve) => {
      confirmationResolver = resolve;
    });
  }

  function answerConfirmation(accepted: boolean) {
    confirmation.value.open = false;
    const resolve = confirmationResolver;
    confirmationResolver = null;
    resolve?.(accepted);
  }

  async function runAction<T>(
    key: string,
    label: string,
    action: () => Promise<T>,
    options: {
      success?: string | ((value: T) => string);
      record?: boolean;
    } = {},
  ): Promise<T | null> {
    if (actionStates[key]?.phase === "pending") return null;
    const epoch = connectionEpoch.value;
    actionStates[key] = { phase: "pending", label };
    const pending = actionStates[key];
    try {
      const value = await action();
      if (epoch !== connectionEpoch.value || actionStates[key] !== pending) return null;
      const success =
        typeof options.success === "function"
          ? options.success(value)
          : options.success;
      actionStates[key] = { phase: "success", label: success ?? "操作完成" };
      const completed = actionStates[key];
      if (success && options.record !== false) addMessage(success);
      setTimeout(() => {
        if (actionStates[key] === completed) delete actionStates[key];
      }, 900);
      return value;
    } catch (error) {
      if (epoch !== connectionEpoch.value || actionStates[key] !== pending) return null;
      const message = error instanceof Error ? error.message : String(error);
      actionStates[key] = {
        phase: "error",
        label: `${label}失败`,
        error: message,
      };
      toast(message, { detail: label, type: "error", persistent: true });
      if (error instanceof ApiError && error.status === 401)
        expireSession("API Key 无效或已失效，请重新登录。");
      return null;
    }
  }

  function resetServerResources() {
    answerConfirmation(false);
    overview.value = {
      services_online: 0,
      active_tasks: 0,
      completed_tasks: 0,
      failed_tasks: 0,
    };
    services.value = [];
    tasks.value = [];
    deployments.value = [];
    catalog.value = [];
    installations.value = [];
    assets.value = [];
    transfers.value = [];
    runtimeProfiles.value = [];
    deploymentOperations.value = [];
    modelSettingsRequest.value = null;
    focusedDeploymentId.value = null;
    assetCompatibilities.value = [];
    Object.keys(actionStates).forEach(key => delete actionStates[key]);
    Object.keys(modelImportProgress).forEach(
      (key) => delete modelImportProgress[key],
    );
    gpuResources.value = null;
    hardware.value = null;
    audit.value = [];
    taskBaseline.clear();
    eventVersions.clear();
    Object.keys(selectedTaskIds).forEach(
      (key) => delete selectedTaskIds[key as MediaKind],
    );
    Object.keys(generationInputs).forEach(
      (key) => delete generationInputs[key],
    );
    eventTimers.forEach((timer) => clearTimeout(timer));
    eventTimers.clear();
    if (snapshotTimer) clearTimeout(snapshotTimer);
    snapshotTimer = null;
  }

  function expireSession(message: string) {
    authenticated.value = false;
    loginMessage.value = message;
    connectionState.value = "error";
    connectionLabel.value = "需要登录";
    openStatusCenter.value = null;
  }

  function noteTaskTransitions(next: MediaTask[]) {
    for (const task of next) {
      const previous = taskBaseline.get(task.id);
      if (previous && previous !== task.status && task.status === "cancel_requested" && taskTimedOut(task)) {
        addMessage(`${mediaNames[task.service]}任务超时 · 正在停止`, {
          detail: `${taskErrorMessage(task)}\n${task.prompt}`, type: "error", task,
        });
      }
      if (
        previous &&
        ["queued", "assigned", "running", "cancel_requested"].includes(
          previous,
        ) &&
        ["succeeded", "failed", "canceled", "interrupted"].includes(task.status)
      ) {
        const title = `${mediaNames[task.service]}任务${taskStatusLabel(task)}`;
        addMessage(title, {
          detail: ["failed", "interrupted"].includes(task.status)
            ? `${taskErrorMessage(task)}\n${task.prompt}` : task.prompt,
          type: ["failed", "interrupted"].includes(task.status)
            ? "error"
            : "info",
          task,
        });
        void desktopBridge()
          .notifyTask({
            taskId: task.id,
            service: task.service,
            status: task.status,
          })
          .catch(() => undefined);
      }
      taskBaseline.set(task.id, task.status);
    }
  }

  async function refreshSnapshot(manual = false): Promise<boolean> {
    if (!authenticated.value) return false;
    if (refreshPromise) return refreshPromise;
    const epoch = connectionEpoch.value;
    const deploymentRead = ++deploymentReadSequence;
    const tasksAtRead = new Map(tasks.value.map(task => [task.id, task]));
    if (manual || connectionState.value === "error") {
      connectionState.value = "busy";
      connectionLabel.value = "正在同步";
    }
    refreshPromise = (async () => {
      try {
        const [
          nextOverview,
          nextServices,
          nextTasks,
          nextDeployments,
          nextCatalog,
          nextInstallations,
          nextGpu,
          nextAssets,
          nextTransfers,
          nextRuntimeProfiles,
          nextDeploymentOperations,
          nextAssetCompatibilities,
        ] = await Promise.all([
          api<Overview>("/api/v1/overview"),
          api<ListResponse<MediaService>>("/api/v1/services"),
          api<ListResponse<MediaTask>>("/api/v1/tasks?limit=50"),
          api<ListResponse<ResourceItem>>("/api/v1/deployments"),
          api<ListResponse<ResourceItem>>("/api/v1/service-catalog"),
          api<ListResponse<ResourceItem>>(
            "/api/v1/service-installations?limit=100",
          ),
          api<Record<string, any>>("/api/v1/resources/gpus"),
          api<ListResponse<ResourceItem>>("/api/v1/model-assets?limit=500"),
          api<ListResponse<ResourceItem>>("/api/v1/model-transfers?limit=100"),
          api<ListResponse<RuntimeProfile>>("/api/v1/runtime-profiles"),
          api<ListResponse<DeploymentOperation>>(
            "/api/v1/deployment-operations?limit=100",
          ),
          api<ListResponse<AssetCompatibility>>(
            "/api/v1/asset-compatibility",
          ),
        ]);
        if (epoch !== connectionEpoch.value) return false;
        overview.value = nextOverview;
        services.value = nextServices.items;
        const incomingIds = new Set(nextTasks.items.map(task => task.id));
        const currentTasks = new Map(tasks.value.map(task => [task.id, task]));
        const mergedTasks = [
          ...tasks.value.filter(task => !incomingIds.has(task.id) && tasksAtRead.get(task.id) !== task),
          ...nextTasks.items.map(task => {
            const current = currentTasks.get(task.id);
            return current && Number(current.version) > Number(task.version) ? current : task;
          }),
        ].slice(0, 50);
        noteTaskTransitions(mergedTasks);
        tasks.value = mergedTasks;
        if (deploymentRead === deploymentReadSequence)
          deployments.value = nextDeployments.items;
        catalog.value = nextCatalog.items;
        installations.value = nextInstallations.items;
        gpuResources.value = nextGpu;
        assets.value = nextAssets.items;
        transfers.value = nextTransfers.items;
        runtimeProfiles.value = nextRuntimeProfiles.items;
        deploymentOperations.value = mergeDeploymentOperations(deploymentOperations.value, nextDeploymentOperations.items, focusedDeploymentId.value);
        assetCompatibilities.value = nextAssetCompatibilities.items;
        connectionState.value = "idle";
        connectionLabel.value = `${activeProfile.value?.name ?? "MediaCenter"} 在线`;
        lastSyncAt.value = new Date().toISOString();
        if (manual) toast("数据已刷新", { record: false });
        return true;
      } catch (error) {
        if (epoch !== connectionEpoch.value) return false;
        const message = error instanceof Error ? error.message : String(error);
        connectionState.value = "error";
        connectionLabel.value = "控制面离线";
        if (error instanceof ApiError && error.status === 401)
          expireSession("无法验证已保存的 API Key，请重新登录。");
        else
          toast(message, {
            detail: "控制面同步失败",
            type: "error",
            persistent: true,
          });
        return false;
      } finally {
        refreshPromise = null;
      }
    })();
    return refreshPromise;
  }

  function scheduleSnapshot() {
    if (snapshotTimer) clearTimeout(snapshotTimer);
    const epoch = connectionEpoch.value;
    snapshotTimer = setTimeout(() => {
      snapshotTimer = null;
      if (epoch === connectionEpoch.value && authenticated.value)
        void refreshSnapshot(false);
    }, 350);
  }

  async function refreshEventResource(
    type: string,
    id: string,
    eventVersion?: number,
    deleted = false,
  ) {
    const epoch = connectionEpoch.value;
    try {
      if (type === "task.changed") {
        if (deleted) tasks.value = tasks.value.filter((item) => item.id !== id);
        else {
          const task = await api<MediaTask>(
            `/api/v1/tasks/${encodeURIComponent(id)}`,
          );
          if (epoch !== connectionEpoch.value) return;
          if (
            Number.isInteger(eventVersion) &&
            Number.isInteger(task.version) &&
            Number(task.version) < Number(eventVersion)
          )
            return scheduleResourceEvent(type, id, eventVersion, deleted, 180);
          const next = replaceItem(tasks.value, task);
          noteTaskTransitions(next);
          tasks.value = next.slice(0, 50);
          overview.value.active_tasks = tasks.value.filter((item) =>
            ["queued", "assigned", "running", "cancel_requested"].includes(
              item.status,
            ),
          ).length;
          overview.value.completed_tasks = tasks.value.filter(
            (item) => item.status === "succeeded",
          ).length;
          overview.value.failed_tasks = tasks.value.filter(
            (item) => item.status === "failed",
          ).length;
        }
        return;
      }
      const exact: Record<
        string,
        { route: string; target: typeof installations }
      > = {
        "installation.changed": {
          route: "/api/v1/service-installations/",
          target: installations,
        },
        "transfer.changed": {
          route: "/api/v1/model-transfers/",
          target: transfers,
        },
        "asset.changed": { route: "/api/v1/model-assets/", target: assets },
      };
      if (exact[type]) {
        const descriptor = exact[type];
        if (deleted)
          descriptor.target.value = descriptor.target.value.filter(
            (item) => item.id !== id,
          );
        else {
          const resource = await api<ResourceItem>(
            `${descriptor.route}${encodeURIComponent(id)}`,
          );
          if (epoch !== connectionEpoch.value) return;
          descriptor.target.value = replaceItem(
            descriptor.target.value,
            resource,
          );
        }
        return;
      }
      if (type === "service.changed") {
        const response =
          await api<ListResponse<MediaService>>("/api/v1/services");
        if (epoch === connectionEpoch.value) services.value = response.items;
        return;
      }
      if (type === "deployment.changed" || type === "instance.changed") {
        const deploymentRead = ++deploymentReadSequence;
        const [nextDeployments, nextServices] = await Promise.all([
          api<ListResponse<ResourceItem>>("/api/v1/deployments"),
          api<ListResponse<MediaService>>("/api/v1/services"),
        ]);
        if (epoch === connectionEpoch.value) {
          if (deploymentRead === deploymentReadSequence)
            deployments.value = nextDeployments.items;
          services.value = nextServices.items;
        }
        return;
      }
      if (type === "deployment-operation.changed") {
        const operation = await api<DeploymentOperation>(
          `/api/v1/deployment-operations/${encodeURIComponent(id)}`,
        );
        if (epoch === connectionEpoch.value) {
          deploymentOperations.value = mergeDeploymentOperations(deploymentOperations.value, [operation], focusedDeploymentId.value);
        }
        if (operation.payload?.action === "uninstall" && epoch === connectionEpoch.value) {
          // A failure retains the same removal owner: the deployment row need
          // not change, but its projected retry state does. Refresh that fact
          // once per durable operation event, not on telemetry or a timer.
          const deploymentRead = ++deploymentReadSequence;
          const current = await api<ListResponse<ResourceItem>>("/api/v1/deployments");
          if (epoch === connectionEpoch.value && deploymentRead === deploymentReadSequence)
            deployments.value = current.items;
        }
        return;
      }
      if (type === "compatibility.changed") {
        const response = await api<ListResponse<AssetCompatibility>>(
          "/api/v1/asset-compatibility",
        );
        if (epoch === connectionEpoch.value)
          assetCompatibilities.value = response.items;
        return;
      }
      scheduleSnapshot();
    } catch {
      if (epoch === connectionEpoch.value) scheduleSnapshot();
    }
  }

  function scheduleResourceEvent(
    type: string,
    id: string,
    version?: number,
    deleted = false,
    delay = 80,
  ) {
    if (!id) return scheduleSnapshot();
    const key = `${type}:${id}`;
    const previous = eventTimers.get(key);
    if (previous) clearTimeout(previous);
    eventTimers.set(
      key,
      setTimeout(() => {
        eventTimers.delete(key);
        void refreshEventResource(type, id, version, deleted);
      }, delay),
    );
  }

  function handleServerEvent(envelope: ServerEventEnvelope) {
    if (
      envelope.profileId !== connections.value.activeProfileId ||
      envelope.revision !== connections.value.revision
    )
      return;
    const event = envelope.event;
    if (!event || event.protocol !== "mc.client/1" || event.type === "hello")
      return;
    if (event.type === "gpu.telemetry") {
      const next = event.data as Record<string, any>;
      if (!Array.isArray(next?.gpus)) return;
      gpuResources.value = next;
      if (hardware.value) {
        hardware.value = {
          ...hardware.value,
          captured_at:
            typeof event.occurred_at === "number"
              ? new Date(event.occurred_at * 1000).toISOString()
              : hardware.value.captured_at,
          gpu: {
            ...(hardware.value.gpu ?? {}),
            available: next.telemetry_available === true,
            error: next.telemetry_error ?? null,
            items: next.gpus,
          },
        };
      }
      return;
    }
    if (event.type === "snapshot.required") return scheduleSnapshot();
    if (event.resource_id && Number.isInteger(event.version)) {
      const key = `${event.type}:${event.resource_id}`;
      const version = Number(event.version);
      if ((eventVersions.get(key) ?? -1) >= version) return;
      eventVersions.set(key, version);
    }
    scheduleResourceEvent(
      event.type,
      event.resource_id ?? "",
      event.version,
      event.data?.deleted === true,
    );
  }

  function handleServerEventStatus(status: ServerEventStatus) {
    if (
      status.profileId !== connections.value.activeProfileId ||
      status.revision !== connections.value.revision
    )
      return;
    if (status.state === "connected") {
      connectionState.value = "idle";
      connectionLabel.value = `${activeProfile.value?.name ?? "MediaCenter"} 在线`;
    } else if (status.state === "reconnecting") {
      connectionState.value = "busy";
      connectionLabel.value = "事件流重连中";
    } else if (status.state === "source-authorization-error")
      toast(status.detail || "模型来源授权恢复失败", {
        type: "error",
        persistent: true,
      });
  }

  function handleTransferProgress(progress: ModelTransferProgress) {
    if (
      progress.profileId !== connections.value.activeProfileId ||
      progress.revision !== connections.value.revision
    )
      return;
    if (progress.sessionId) modelImportProgress[progress.sessionId] = progress;
    const item = transfers.value.find(
      (value) => value.id === progress.transferId,
    );
    if (item) {
      item.received_bytes = progress.receivedBytes;
      item.expected_bytes = progress.totalBytes;
      item.current_file = progress.relativePath;
    }
  }

  function bindDesktopEvents() {
    if (listenersBound || !hasDesktopBridge()) return;
    listenersBound = true;
    const bridge = desktopBridge();
    bridge.onServerEvent(handleServerEvent);
    bridge.onServerEventStatus(handleServerEventStatus);
    bridge.onModelTransferProgress(handleTransferProgress);
    bridge.onNavigate((view) => navigate(view as AppView));
    bridge.onRefresh(() => void refreshSnapshot(true));
  }

  async function startSession() {
    connectionEpoch.value += 1;
    resetServerResources();
    authenticated.value = true;
    loginMessage.value = "";
    connectionState.value = "busy";
    connectionLabel.value = "正在连接";
    bindDesktopEvents();
    try {
      const result = await desktopBridge().startEvents();
      if (result.sourceAuthorizationWarning)
        toast(String(result.sourceAuthorizationWarning), {
          type: "error",
          persistent: true,
        });
    } catch {
      connectionLabel.value = "事件流重连中";
    }
    await refreshSnapshot(false);
  }

  async function initialize() {
    if (initialized.value) return;
    if (!hasDesktopBridge()) {
      initialized.value = true;
      loginMessage.value = "请从 MediaCenter PC 客户端打开此工作台。";
      return;
    }
    try {
      connections.value = await desktopBridge().listConnections();
      loginSelection.value = connections.value.activeProfileId;
      bindDesktopEvents();
      if (activeProfile.value?.hasCredential) await startSession();
    } catch (error) {
      loginMessage.value =
        error instanceof Error ? error.message : String(error);
    } finally {
      initialized.value = true;
    }
  }

  async function connectServer(payload: {
    profileId?: string;
    name: string;
    baseUrl: string;
    apiKey: string;
    allowInsecureSourceAuthorization: boolean;
  }) {
    const result = await runAction(
      "connect",
      "正在验证服务器",
      () => desktopBridge().connectServer(payload),
      { record: false },
    );
    if (!result) return false;
    connections.value = result;
    loginSelection.value = result.activeProfileId;
    await startSession();
    return authenticated.value;
  }

  async function switchServer(profileId: string) {
    if (profileId === connections.value.activeProfileId && authenticated.value)
      return true;
    const result = await runAction(
      "switch-server",
      "正在切换服务器",
      () => desktopBridge().switchServer(profileId),
      { record: false },
    );
    if (!result) return false;
    connections.value = result;
    loginSelection.value = result.selectedProfileId ?? profileId;
    if (result.requiresLogin) {
      authenticated.value = false;
      loginMessage.value = "该服务器没有保存的 API Key，请输入后连接。";
      return false;
    }
    await startSession();
    return authenticated.value;
  }

  async function logout() {
    const result = await runAction(
      "logout",
      "正在注销",
      () => desktopBridge().logoutServer(),
      { record: false },
    );
    if (!result) return;
    connections.value = result;
    connectionEpoch.value += 1;
    resetServerResources();
    authenticated.value = false;
    userMenuOpen.value = false;
    loginMessage.value = "已注销当前服务器。";
  }

  async function removeServer(profileId: string) {
    const result = await runAction(
      `remove:${profileId}`,
      "正在移除服务器",
      () => desktopBridge().removeServer(profileId),
      { record: false },
    );
    if (!result) return false;
    const removedActive = profileId === connections.value.activeProfileId;
    connections.value = result;
    loginSelection.value = result.activeProfileId;
    if (removedActive) {
      connectionEpoch.value += 1;
      resetServerResources();
      authenticated.value = false;
    }
    return true;
  }

  async function refreshHardware() {
    const value = await runAction(
      "hardware:refresh",
      "正在读取硬件状态",
      () => api<Record<string, any>>("/api/v1/hardware"),
      { record: false },
    );
    if (value) hardware.value = value;
  }

  async function refreshAudit() {
    const value = await runAction(
      "audit:refresh",
      "正在读取审计日志",
      () => api<ListResponse<ResourceItem>>("/api/v1/audit?limit=100"),
      { record: false },
    );
    if (value) audit.value = value.items;
  }

  function generationInputKey(kind: MediaKind, modelKey: string) {
    return `${kind}:${modelKey}`;
  }

  function selectedTask(kind: MediaKind) {
    const rows = tasks.value.filter((task) => task.service === kind);
    return (
      rows.find((task) => task.id === selectedTaskIds[kind]) ??
      rows.find((task) =>
        ["queued", "assigned", "running", "cancel_requested"].includes(
          task.status,
        ),
      ) ??
      rows[0] ??
      null
    );
  }

  function acceptsAsset(type: string, accept: string[]) {
    return (
      !accept.length ||
      accept.some((value) =>
        value.endsWith("/") ? type.startsWith(value) : type === value,
      )
    );
  }

  async function uploadAssetFiles(
    kind: MediaKind,
    maximum: number,
    modelKey: string,
    files: Array<{
      name: string;
      type: string;
      size: number;
      bytes: Uint8Array;
    }>,
    accept: string[] = [],
  ) {
    if (!files.length)
      return generationInputs[generationInputKey(kind, modelKey)] ?? [];
    const key = generationInputKey(kind, modelKey);
    const current = generationInputs[key] ?? [];
    for (const file of files) {
      if (current.length >= maximum) {
        toast(`最多选择 ${maximum} 个素材`, {
          type: "error",
          persistent: true,
        });
        break;
      }
      if (!acceptsAsset(file.type, accept)) {
        toast(`${file.name} 的媒体类型不符合当前模型合同`, {
          type: "error",
          persistent: true,
        });
        continue;
      }
      if (
        file.size > MAX_ASSET_BYTES ||
        file.bytes.byteLength > MAX_ASSET_BYTES
      ) {
        toast(`${file.name} 超过 256 MiB`, { type: "error", persistent: true });
        continue;
      }
      const asset = await runAction(
        `asset:${kind}:${file.name}`,
        `正在上传 ${file.name}`,
        () =>
          desktopBridge().uploadAsset({
            name: file.name,
            type: file.type,
            bytes: file.bytes,
          }),
        { success: `${file.name} 已上传` },
      );
      if (asset) current.push(asset);
    }
    generationInputs[key] = [...current];
    return generationInputs[key];
  }

  async function uploadPickedAssets(
    kind: MediaKind,
    multiple: boolean,
    maximum: number,
    modelKey: string,
    accept: string[] = [],
  ) {
    const files = await desktopBridge().pickAssets({ kind, multiple, accept });
    return uploadAssetFiles(kind, maximum, modelKey, files, accept);
  }

  async function uploadDroppedAssets(
    kind: MediaKind,
    dropped: File[],
    maximum: number,
    modelKey: string,
    accept: string[] = [],
  ) {
    const files: Array<{
      name: string;
      type: string;
      size: number;
      bytes: Uint8Array;
    }> = [];
    for (const file of dropped) {
      if (!acceptsAsset(file.type, accept)) {
        toast(`${file.name} 的媒体类型不符合当前模型合同`, {
          type: "error",
          persistent: true,
        });
        continue;
      }
      if (file.size > MAX_ASSET_BYTES) {
        toast(`${file.name} 超过 256 MiB`, { type: "error", persistent: true });
        continue;
      }
      files.push({
        name: file.name,
        type: file.type,
        size: file.size,
        bytes: new Uint8Array(await file.arrayBuffer()),
      });
    }
    return uploadAssetFiles(kind, maximum, modelKey, files, accept);
  }

  function removeGenerationAsset(
    kind: MediaKind,
    modelKey: string,
    assetId: string,
  ) {
    const key = generationInputKey(kind, modelKey);
    generationInputs[key] = (generationInputs[key] ?? []).filter(
      (asset) => asset.id !== assetId,
    );
  }

  async function submitTask(
    kind: MediaKind,
    model: string,
    prompt: string,
    options: Record<string, unknown>,
    inputs?: string[],
    loras?: unknown[],
    identity?: Pick<ImageGenerationDraft, 'configuration' | 'execution'>,
  ) {
    const epoch = connectionEpoch.value;
    const task = await runAction(
      `submit:${kind}`,
      `正在提交${mediaNames[kind]}任务`,
      () =>
        api<MediaTask>("/api/v1/tasks", {
          method: "POST",
          body: {
            service: kind,
            model,
            prompt,
            options,
            inputs: inputs ?? [],
            ...(loras?.length ? { loras } : {}),
            ...(identity?.execution ? { expected_binding: identity.execution } : {}),
            ...(identity?.configuration ? { expected_configuration: {
              deployment_id: identity.configuration.deployment_id,
              config_revision: identity.configuration.config_revision,
              config_digest: identity.configuration.config_digest,
            } } : {}),
          },
        }),
      { success: (value) => `任务 ${value.id} 已进入队列` },
    );
    if (!task || epoch !== connectionEpoch.value) return null;
    tasks.value = replaceItem(tasks.value, task);
    selectedTaskIds[kind] = task.id;
    return task;
  }

  async function assessAssetCompatibility(
    subjectAssetId: string,
    baseAssetId: string,
  ) {
    const epoch = connectionEpoch.value;
    const result = await runAction(
      `compatibility:${subjectAssetId}:${baseAssetId}`,
      "正在验证模型兼容性",
      () =>
        api<AssetCompatibility>("/api/v1/asset-compatibility", {
          method: "POST",
          body: {
            subject_asset_id: subjectAssetId,
            base_asset_id: baseAssetId,
          },
        }),
      { record: false },
    );
    if (!result || epoch !== connectionEpoch.value) return null;
    assetCompatibilities.value = [
      result,
      ...assetCompatibilities.value.filter(
        (item) =>
          !(
            item.subject_asset_id === result.subject_asset_id &&
            item.subject_revision === result.subject_revision &&
            item.base_asset_id === result.base_asset_id &&
            item.base_revision === result.base_revision &&
            item.detector_version === result.detector_version
          ),
      ),
    ];
    return result;
  }

  async function cancelTask(task: MediaTask) {
    const epoch = connectionEpoch.value;
    const current = tasks.value.find(item => item.id === task.id);
    if (!current || !canCancelTask(current)) return false;
    if (
      !(await confirm(
        "取消任务",
        `将请求取消任务 ${task.id}。已完成的计算无法撤回。`,
        "取消任务",
      ))
    )
      return false;
    const latest = tasks.value.find(item => item.id === task.id);
    if (epoch !== connectionEpoch.value || !latest || !canCancelTask(latest)) return false;
    const result = await runAction(
      `task:${task.id}:cancel`,
      "正在取消任务",
      () =>
        api<MediaTask>(`/api/v1/tasks/${encodeURIComponent(task.id)}/cancel`, {
          method: "POST",
          body: {},
        }),
      { success: value => value.status === "cancel_requested"
        ? `任务 ${task.id} · ${taskStatusLabel(value)}，等待执行退出确认`
        : `任务 ${task.id} · ${taskStatusLabel(value)}` },
    );
    if (result) tasks.value = replaceItem(tasks.value, result);
    return Boolean(result);
  }

  async function retryTask(task: MediaTask) {
    const current = tasks.value.find(item => item.id === task.id);
    if (!current) return null;
    const disabledReason = taskRetryDisabledReason(current);
    if (disabledReason) {
      addMessage("暂时无法重试", { detail: disabledReason, type: "error", task: current });
      return null;
    }
    const result = await runAction(
      `task:${task.id}:retry`,
      "正在重试任务",
      () =>
        api<MediaTask>(`/api/v1/tasks/${encodeURIComponent(task.id)}/retry`, {
          method: "POST",
          body: { version: current.version },
        }),
      { success: (value) => `重试任务 ${value.id} 已进入队列` },
    );
    if (result) {
      tasks.value = replaceItem(tasks.value, result);
      selectedTaskIds[result.service] = result.id;
    }
    return result;
  }

  async function fetchArtifactUrl(path: string) {
    return URL.createObjectURL(await fetchArtifactBlob(path));
  }

  async function fetchArtifactBlob(path: string) {
    const value = await desktopBridge().fetchArtifact(path);
    const copy = new Uint8Array(value.bytes.byteLength);
    copy.set(value.bytes);
    return new Blob([copy.buffer], {
      type: value.contentType || "application/octet-stream",
    });
  }

  async function saveArtifact(path: string, suggestedName?: string) {
    return runAction(
      `artifact:save:${path}`,
      "正在选择保存位置",
      () =>
        desktopBridge().saveArtifact(
          path,
          suggestedName || path.split("/").pop() || "mediacenter-artifact",
        ),
      {
        success: (value) => (value.saved ? "产物已保存" : "已取消保存"),
        record: false,
      },
    );
  }

  async function configureService(
    service: MediaService,
    changes: Record<string, unknown>,
  ) {
    const result = await runAction(
      `service:${service.kind}:save`,
      "正在保存服务配置",
      () =>
        api<MediaService>(`/api/v1/services/${service.kind}`, {
          method: "PATCH",
          body: changes,
        }),
      { success: `${service.name}配置已更新` },
    );
    if (result)
      services.value = services.value.map((item) =>
        item.kind === result.kind ? result : item,
      );
    return result;
  }

  async function deploymentAction(
    deployment: ResourceItem,
    action: "start" | "stop",
  ) {
    const label = action === "start" ? "启动" : "停止";
    if (
      action === "stop" &&
      !(await confirm(
        "停止模型服务",
        `停止 ${deployment.label || deployment.id} 后将不再接收新任务。`,
        "停止服务",
      ))
    )
      return null;
    const version = deployment.instance_settings?.policy_version;
    if (!Number.isInteger(version))
      return toast("实例策略版本不可用", { type: "error", persistent: true });
    const result = await runAction(
      `deployment:${deployment.id}:${action}`,
      `正在${label}模型服务`,
      () =>
        api<ResourceItem>(
          `/api/v1/deployments/${encodeURIComponent(deployment.id)}/${action}`,
          { method: "POST", body: { version } },
        ),
      { success: `模型服务已${label}` },
    );
    if (result) await refreshSnapshot(false);
    return result;
  }

  async function saveDeploymentPolicy(
    deployment: ResourceItem,
    policy: Record<string, unknown>,
  ) {
    const version = deployment.instance_settings?.policy_version;
    if (!Number.isInteger(version)) {
      toast("实例策略版本不可用", { type: "error", persistent: true });
      return null;
    }
    const result = await runAction(
      `deployment:${deployment.id}:save`,
      "正在应用实例设置",
      () =>
        api<ResourceItem>(
          `/api/v1/deployments/${encodeURIComponent(deployment.id)}/policy`,
          { method: "POST", body: { version, ...policy } },
        ),
      { success: "实例设置已保存" },
    );
    if (result) await refreshSnapshot(false);
    return result;
  }

  async function installRecipe(
    recipe: ResourceItem,
    value: { gpus: number[]; license: boolean },
  ) {
    const result = await runAction(
      `install:${recipe.key}`,
      "正在创建安装任务",
      () =>
        api<ResourceItem>("/api/v1/service-installations", {
          method: "POST",
          body: {
            recipe_key: recipe.key,
            gpu_indices: value.gpus,
            license_accepted: value.license,
            ...(recipe.retained_deployment_id
              ? {
                  deployment_id: recipe.retained_deployment_id,
                  adopt_existing: true,
                }
              : {}),
          },
        }),
      { success: `${recipe.label} 安装任务已提交` },
    );
    if (result) installations.value = replaceItem(installations.value, result);
    return result;
  }

  async function uninstallRecipe(recipe: ResourceItem) {
    const epoch = connectionEpoch.value;
    if (
      !(await confirm(
        "卸载模型服务",
        `将移除 ${recipe.label} 的服务配置和运行容器。模型权重与安装历史会保留。`,
        "卸载服务",
      ))
    )
      return null;
    if (epoch !== connectionEpoch.value) return null;
    if (recipe.user_imported) {
      const instance = recipe.deployments?.[0];
      return instance ? uninstallUserDeployment(instance) : null;
    }
    const result = await runAction(
      `uninstall:${recipe.key}`,
      "正在卸载服务",
      () =>
        api<ResourceItem>(
          `/api/v1/service-catalog/${encodeURIComponent(recipe.key)}/uninstall`,
          { method: "POST", body: {} },
        ),
      { success: `${recipe.label} 服务已卸载` },
    );
    if (result) await refreshSnapshot(false);
    return result;
  }

  async function uninstallUserDeployment(instance: ResourceItem, retryOf?: string) {
    const expected = instance.configuration_binding;
    if (!expected || !instance.instance_settings?.policy_version || !instance.incarnation) {
      toast("实例身份未同步，请刷新后重试卸载。", { type: "error" });
      return null;
    }
    let result = await runAction(
      `uninstall:deployment:${instance.id}`, "正在提交卸载",
      () => api<DeploymentOperation>(`/api/v1/deployments/${encodeURIComponent(instance.id)}/uninstall`, {
        method: "POST", idempotencyKey: `uninstall-${crypto.randomUUID()}`,
        body: { incarnation: instance.incarnation, config_revision: expected.config_revision,
          config_digest: expected.config_digest, policy_version: instance.instance_settings.policy_version,
          retry_of: retryOf ?? instance.removal_operation_id ?? null },
      }), { record: false },
    );
    if (result) {
      deploymentOperations.value = mergeDeploymentOperations(deploymentOperations.value, [result], focusedDeploymentId.value);
      result = deploymentOperations.value.find(item => item.id === result!.id) ?? result;
      addMessage(result.state === "ready" ? "服务已卸载，模型资产已保留" : result.state === "failed"
        ? "卸载未完成，可在后台任务中重试" : "卸载已进入后台，模型资产将保留");
      const observed = result;
      ++deploymentReadSequence;
      deployments.value = deployments.value.map(item => item.id !== instance.id ? item : {
        ...item, accepting_tasks: false, removal_operation_id: observed.state === "ready" ? null : observed.id,
        removal_operation_state: observed.state, install_state: observed.state === "ready" ? "configured" : item.install_state,
      });
      await refreshSnapshot(false);
    }
    return result;
  }

  async function retryUserRemoval(operation: DeploymentOperation) {
    const instance = deployments.value.find(item => item.id === operation.deployment_id);
    if (!instance || instance.removal_operation_id !== operation.id || operation.state !== "failed") {
      toast("卸载状态已变化，请同步后重试。", { type: "error" });
      return null;
    }
    return uninstallUserDeployment(instance, operation.id);
  }

  async function controlInstallation(
    installation: ResourceItem,
    action: "pause" | "resume" | "cancel" | "retry",
  ) {
    const labels = {
      pause: "暂停",
      resume: "继续",
      cancel: "取消",
      retry: "重试",
    };
    const result = await runAction(
      `installation:${installation.id}:${action}`,
      `正在${labels[action]}安装`,
      () =>
        api<ResourceItem>(
          `/api/v1/service-installations/${encodeURIComponent(installation.id)}/${action}`,
          { method: "POST", body: {} },
        ),
      { success: "安装任务状态已更新" },
    );
    if (result) installations.value = replaceItem(installations.value, result);
    return result;
  }

  async function controlTransfer(
    transfer: ResourceItem,
    action: "pause" | "resume" | "cancel" | "retry",
  ) {
    const labels = {
      pause: "暂停",
      resume: "继续",
      cancel: "取消",
      retry: "重试",
    };
    const result = await runAction(
      `transfer:${transfer.id}:${action}`,
      `正在${labels[action]}传输`,
      () =>
        api<ResourceItem>(
          `/api/v1/model-transfers/${encodeURIComponent(transfer.id)}/${action}`,
          { method: "POST", body: {} },
        ),
      { success: "模型传输状态已更新" },
    );
    if (result) transfers.value = replaceItem(transfers.value, result);
    return result;
  }

  async function configureSourceAuthorization(
    provider: string,
    token?: string,
    clear = false,
  ) {
    const result = await runAction(
      `source:${provider}`,
      clear ? "正在清除来源授权" : "正在保存来源授权",
      () =>
        desktopBridge().configureSourceAuthorization({
          provider,
          ...(clear ? { action: "clear" } : { token }),
        }),
      { success: clear ? "来源授权已清除" : "来源授权已保存" },
    );
    if (result) await refreshSnapshot(false);
    return result;
  }

  async function planUserDeployment(payload: Record<string, unknown>) {
    return runAction(
      "deployment:plan",
      "正在计算部署计划",
      () =>
        api<DeploymentPlan>("/api/v1/deployment-plans", {
          method: "POST",
          body: payload,
        }),
      { record: false },
    );
  }

  async function createUserDeployment(
    operation: Record<string, any>,
    idempotencyKey = `deploy-${crypto.randomUUID()}`,
  ) {
    const actionKey = `deployment:create:${operation.deployment_id}`;
    const configuring = Boolean(operation.expected_configuration);
    let result = await runAction(
      actionKey,
      configuring ? "正在提交实例配置" : "正在创建独立运行容器",
      () =>
        api<DeploymentOperation>("/api/v1/deployment-operations", {
          method: "POST",
          body: operation,
          idempotencyKey,
        }),
      { record: false },
    );
    if (result) {
      deploymentOperations.value = mergeDeploymentOperations(deploymentOperations.value, [result], focusedDeploymentId.value);
      result = deploymentOperations.value.find(item => item.id === result!.id) ?? result;
      if (result.state === "failed") {
        actionStates[actionKey] = {
          phase: "error",
          label: configuring ? "配置未完成" : "部署未完成",
          error: result.error_message ?? result.error_code ?? "部署失败",
        };
        toast(result.error_message ?? result.error_code ?? "部署失败", {
          detail: "模型资产与实例配置已保留",
          type: "error",
          persistent: true,
        });
      } else {
        addMessage(
          configuring
            ? (result.state === "ready" ? "实例配置已生效" : "配置操作已进入后台")
            : (result.state === "ready" ? "用户模型部署已就绪" : "部署操作已进入后台"),
        );
      }
    }
    return result;
  }

  async function cancelDeploymentOperation(operation: DeploymentOperation) {
    const result = await runAction(
      `deployment-operation:${operation.id}:cancel`,
      "正在取消部署操作",
      () =>
        api<DeploymentOperation>(
          `/api/v1/deployment-operations/${encodeURIComponent(operation.id)}/cancel`,
          {
            method: "POST",
            body: {},
          },
        ),
      { success: "部署操作已请求取消" },
    );
    if (result)
      deploymentOperations.value = mergeDeploymentOperations(deploymentOperations.value, [result], focusedDeploymentId.value);
    return result;
  }

  function openModelSettings(deploymentId: string) {
    modelSettingsRequest.value = { deploymentId, nonce: crypto.randomUUID() };
    navigate("deployments");
  }

  function navigate(view: AppView) {
    activeView.value = view;
    openStatusCenter.value = null;
    userMenuOpen.value = false;
    if (view === "hardware" && !hardware.value) void refreshHardware();
    if (view === "audit" && !audit.value.length) void refreshAudit();
  }

  function toggleStatusCenter(kind: StatusCenterKind) {
    openStatusCenter.value = openStatusCenter.value === kind ? null : kind;
    if (kind !== "message") expandedMessageId.value = null;
  }

  function closeTransientUi() {
    openStatusCenter.value = null;
    userMenuOpen.value = false;
  }

  function openTask(taskId: string) {
    const task = tasks.value.find((item) => item.id === taskId);
    if (task) {
      selectedTaskIds[task.service] = task.id;
      navigate(task.service);
    }
  }

  return {
    initialized,
    authenticated,
    loginMessage,
    loginSelection,
    connectionEpoch,
    connections,
    activeProfile,
    activeView,
    connectionState,
    connectionLabel,
    lastSyncAt,
    overview,
    services,
    tasks,
    deployments,
    catalog,
    installations,
    assets,
    transfers,
    runtimeProfiles,
    deploymentOperations,
    assetCompatibilities,
    modelImportProgress,
    gpuResources,
    hardware,
    audit,
    statusMessages,
    toasts,
    actionStates,
    openStatusCenter,
    expandedMessageId,
    userMenuOpen,
    activeTasks,
    backgroundCount,
    unreadCount,
    readyModelCount,
    selectedTaskIds,
    generationInputs,
    imageGenerationDrafts,
    confirmation,
    initialize,
    connectServer,
    switchServer,
    logout,
    removeServer,
    refreshSnapshot,
    refreshHardware,
    refreshAudit,
    navigate,
    toggleStatusCenter,
    closeTransientUi,
    addMessage,
    markMessageRead,
    markAllMessagesRead,
    clearMessages,
    toast,
    dismissToast,
    runAction,
    openTask,
    confirm,
    answerConfirmation,
    generationInputKey,
    selectedTask,
    uploadPickedAssets,
    uploadDroppedAssets,
    removeGenerationAsset,
    submitTask,
    assessAssetCompatibility,
    cancelTask,
    retryTask,
    fetchArtifactUrl,
    fetchArtifactBlob,
    saveArtifact,
    configureService,
    deploymentAction,
    saveDeploymentPolicy,
    installRecipe,
    uninstallRecipe,
    retryUserRemoval,
    controlInstallation,
    controlTransfer,
    configureSourceAuthorization,
    planUserDeployment,
    createUserDeployment,
    modelSettingsRequest,
    focusedDeploymentId,
    openModelSettings,
    cancelDeploymentOperation,
  };
});
