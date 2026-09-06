<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import ActionButton from "@/components/ActionButton.vue";
import AppIcon from "@/components/AppIcon.vue";
import StateBadge from "@/components/StateBadge.vue";
import { humanBytes } from "@/lib/format";
import { residencyLabels, deploymentOperationLabels as operationLabels } from "@/lib/models";
import { api } from "@/services/api";
import { desktopBridge } from "@/services/desktop";
import { useAppStore } from "@/stores/app";
import type {
  DeploymentOperation,
  DeploymentPlan,
  ModelUploadSession,
  ResourceItem,
  RuntimeProfile,
} from "@/types/contracts";

const props = defineProps<{ open: boolean }>();
const emit = defineEmits<{ close: []; completed: [] }>();
const store = useAppStore();

type ImportRole = "checkpoint" | "lora" | "vae";
const step = ref(1);
const sessions = ref<ModelUploadSession[]>([]);
const session = ref<ModelUploadSession | null>(null);
const asset = ref<ResourceItem | null>(null);
const plan = ref<DeploymentPlan | null>(null);
const operation = ref<DeploymentOperation | null>(null);
const importDisposition = ref<"uploaded" | "reused" | null>(null);
const idempotencyKey = ref("");
const loadingSessions = ref(false);
const restoringTransfer = ref(false);
const restoreError = ref("");
const restoredProgress = ref<{ stage: string; relativePath: string; receivedBytes: number; totalBytes: number } | null>(null);
const serverScope = computed(() => JSON.stringify([store.activeProfile?.id ?? null, store.connectionEpoch]));
let flowOwner = serverScope.value;
let flowRevision = 0;
let sessionsRequest = 0;
function currentFlow() { return { owner: flowOwner, revision: flowRevision }; }
function sameFlow(value: ReturnType<typeof currentFlow>) {
  return value.owner === serverScope.value && value.owner === flowOwner && value.revision === flowRevision;
}

const form = reactive({
  selection: "file" as "file" | "directory",
  displayName: "",
  role: "checkpoint" as ImportRole,
  revision: "",
  licenseDeclared: "unknown",
});
const deployment = reactive({
  deploymentId: "",
  runtimeProfileKey: "",
  vaeAssetId: "",
  gpuUuids: [] as string[],
  residency: "on_demand",
  sharingMode: "shared",
  requiredVramMib: 16384,
  licenseAccepted: false,
  experimentalAccepted: false,
});

const stepItems = [
  { value: 1, label: "来源" },
  { value: 2, label: "预检" },
  { value: 3, label: "兼容" },
  { value: 4, label: "计划" },
  { value: 5, label: "执行" },
];
const roleLabels: Record<string, string> = {
  checkpoint: "基础模型",
  lora: "LoRA",
  vae: "VAE",
  unknown: "待确认",
};
const stageLabels: Record<string, string> = {
  paused: "上传已暂停",
  failed: "上传未完成",
  hashing: "计算内容摘要",
  uploading: "传输到服务器",
  "server-verifying": "服务器独立校验",
};
const progress = computed(() => {
  const current = session.value;
  if (!current) return null;
  const latest = restoredProgress.value ?? store.modelImportProgress[current.id];
  if (!latest) return null;
  // A terminal local transition takes precedence over the last chunk event.
  return ["paused", "failed"].includes(current.state)
    ? { ...latest, stage: current.state }
    : latest;
});
const progressRatio = computed(() => {
  const value = progress.value;
  return value?.totalBytes
    ? Math.min(1, value.receivedBytes / value.totalBytes)
    : 0;
});
const activeProfileName = computed(
  () => store.activeProfile?.name ?? "MediaCenter",
);
const profiles = computed(() => store.runtimeProfiles);
const selectedProfile = computed(
  () =>
    profiles.value.find(
      (item) =>
        `${item.profile_id}/${item.revision}` === deployment.runtimeProfileKey,
    ) ?? null,
);
const currentOperation = computed(() => {
  const deploymentId = plan.value?.operation.deployment_id ?? operation.value?.deployment_id;
  // The POST response is only a fallback until the snapshot/SSE owns this id.
  // Keeping both copies lets a stale accepted response hide a terminal state.
  const byId = new Map<string, DeploymentOperation>();
  if (operation.value?.deployment_id === deploymentId && operation.value)
    byId.set(operation.value.id, operation.value);
  for (const item of store.deploymentOperations)
    if (item.deployment_id === deploymentId) byId.set(item.id, item);
  const candidates = [...byId.values()];
  const active = candidates.find(
    (item) => !["ready", "failed", "canceled"].includes(item.state),
  );
  if (active) return active;
  return candidates.sort((left, right) =>
    String(right.updated_at ?? right.created_at ?? "").localeCompare(
      String(left.updated_at ?? left.created_at ?? ""),
    ),
  )[0] ?? null;
});
const gpus = computed<ResourceItem[]>(() => store.gpuResources?.gpus ?? []);
const vaeAssets = computed(() =>
  store.assets.filter(
    (item) =>
      item.state === "ready" &&
      item.role === "vae" &&
      item.media_kind === "image",
  ),
);
const isCheckpoint = computed(() => form.role === "checkpoint");
const canEditSource = computed(
  () =>
    !session.value?.transferId &&
    !["hashing", "uploading", "server-verifying"].includes(
      session.value?.state ?? "",
    ),
);
const canSubmitPlan = computed(() =>
  Boolean(
    asset.value &&
    selectedProfile.value &&
    deployment.deploymentId &&
    deployment.gpuUuids.length &&
    deployment.licenseAccepted &&
    profileReason(selectedProfile.value) === "" &&
    deployment.gpuUuids.every(
      (uuid) =>
        !gpuDisabledReason(gpus.value.find((item) => item.uuid === uuid)),
    ),
  ),
);

watch(
  serverScope,
  (value) => {
    flowOwner = value;
    resetFlow();
    sessions.value = [];
    loadingSessions.value = false;
    sessionsRequest++;
    emit("close");
  },
  { flush: "sync" },
);
watch(
  () => props.open,
  (value) => {
    if (value) void loadSessions();
  },
  { immediate: true },
);
watch(selectedProfile, (profile) => {
  if (!profile) return;
  deployment.requiredVramMib = Number(profile.required_vram_mib);
  if (!profile.residency_modes.includes(deployment.residency)) {
    deployment.residency = profile.residency_modes[0] ?? "on_demand";
  }
  chooseDefaultGpu();
});

function safeDeploymentId(label: string) {
  const slug = label
    .toLowerCase()
    .replace(/[^a-z0-9.-]+/gu, "-")
    .replace(/^-+|-+$/gu, "")
    .slice(0, 42);
  return `${slug || "user-sdxl"}-${crypto.randomUUID().slice(0, 6)}`;
}

function resetFlow() {
  flowRevision++;
  restoredProgress.value = null;
  restoringTransfer.value = false;
  restoreError.value = "";
  step.value = 1;
  session.value = null;
  asset.value = null;
  plan.value = null;
  operation.value = null;
  importDisposition.value = null;
  idempotencyKey.value = "";
  Object.assign(form, {
    selection: "file",
    displayName: "",
    role: "checkpoint",
    revision: "",
    licenseDeclared: "unknown",
  });
  Object.assign(deployment, {
    deploymentId: "",
    runtimeProfileKey: "",
    vaeAssetId: "",
    gpuUuids: [],
    residency: "on_demand",
    sharingMode: "shared",
    requiredVramMib: 16384,
    licenseAccepted: false,
    experimentalAccepted: false,
  });
}

async function loadSessions() {
  const flow = currentFlow(), request = ++sessionsRequest;
  loadingSessions.value = true;
  try {
    const values = (await desktopBridge().listModelUploadSessions()) as unknown as ModelUploadSession[];
    if (sameFlow(flow) && request === sessionsRequest)
      sessions.value = values.filter(item => item.profileId === store.activeProfile?.id);
  } catch (error) {
    if (sameFlow(flow) && request === sessionsRequest) store.toast(error instanceof Error ? error.message : String(error), {
      type: "error",
      persistent: true,
    });
  } finally {
    if (sameFlow(flow) && request === sessionsRequest) loadingSessions.value = false;
  }
}

function applySession(value: ModelUploadSession) {
  if (flowOwner !== serverScope.value || value.profileId !== store.activeProfile?.id) return;
  flowRevision++;
  restoredProgress.value = null;
  restoringTransfer.value = false;
  restoreError.value = "";
  session.value = value;
  form.selection = value.selection;
  form.displayName = value.displayName;
  form.role = (
    ["checkpoint", "lora", "vae"].includes(value.preview.role)
      ? value.preview.role
      : "checkpoint"
  ) as ImportRole;
  form.revision =
    value.preview.baseIdentity ||
    value.files.find((item) => item.sha256)?.sha256 ||
    `local-${value.createdAt.replace(/\D/gu, "").slice(0, 14)}`;
  deployment.deploymentId = safeDeploymentId(value.displayName);
  if (value.reusedAsset) {
    asset.value = value.reusedAsset;
    importDisposition.value = "reused";
    step.value = 3;
  } else step.value = 2;
}

async function chooseSource(selection: "file" | "directory") {
  const flow = currentFlow();
  if (!sameFlow(flow)) return;
  form.selection = selection;
  const result = await store.runAction(
    "model-import:pick",
    "正在读取模型元数据",
    () => desktopBridge().pickModelForImport({ selection }),
    { record: false },
  );
  if (!sameFlow(flow) || !result || result.canceled || !result.session) return;
  applySession(result.session as ModelUploadSession);
  await loadSessions();
}

async function resumeSession(value: ModelUploadSession) {
  if (value.profileId !== store.activeProfile?.id || flowOwner !== serverScope.value) return;
  applySession(value);
  if (session.value?.id !== value.id || !value.transferId) return;
  const flow = currentFlow();
  restoringTransfer.value = true;
  try {
    const transfer = await api<ResourceItem>(`/api/v1/model-transfers/${encodeURIComponent(value.transferId)}`);
    if (!sameFlow(flow)) return;
    if (transfer.id !== value.transferId || transfer.direction !== "upload" ||
        !Number.isSafeInteger(transfer.received_bytes) || !Number.isSafeInteger(transfer.expected_bytes) ||
        transfer.received_bytes < 0 || transfer.expected_bytes !== value.totalBytes ||
        transfer.received_bytes > transfer.expected_bytes ||
        ![transfer.revision, transfer.display_name, transfer.license_declared].every(
          field => typeof field === "string" && field.trim().length > 0) ||
        !["checkpoint", "lora", "vae"].includes(transfer.role))
      throw new Error("原传输信息不匹配，未恢复上传");
    Object.assign(form, { displayName: transfer.display_name, role: transfer.role,
      revision: transfer.revision, licenseDeclared: transfer.license_declared });
    restoredProgress.value = { stage: value.state, relativePath: "服务器已接收",
      receivedBytes: transfer.received_bytes, totalBytes: transfer.expected_bytes };
  } catch (error) {
    if (sameFlow(flow)) restoreError.value = `无法读取原传输，请重试。${error instanceof Error ? error.message : String(error)}`;
  } finally {
    if (sameFlow(flow)) restoringTransfer.value = false;
  }
}

async function discardSession(value: ModelUploadSession) {
  const flow = currentFlow();
  if (!sameFlow(flow) || value.profileId !== store.activeProfile?.id) return;
  if (
    !(await store.confirm(
      "移除上传会话",
      "只移除这台 PC 保存的续传记录；已发布的模型资产不会删除。",
      "移除",
    ))
  )
    return;
  if (!sameFlow(flow)) return;
  await desktopBridge().discardModelUpload(value.id);
  if (!sameFlow(flow)) return;
  if (session.value?.id === value.id) resetFlow();
  await loadSessions();
}

async function publishAsset() {
  if (restoringTransfer.value) return;
  if (restoreError.value && session.value) { await resumeSession(session.value); return; }
  const flow = currentFlow();
  if (
    !sameFlow(flow) ||
    !session.value ||
    session.value.profileId !== store.activeProfile?.id ||
    !form.displayName.trim() ||
    !form.revision.trim() ||
    !form.licenseDeclared.trim()
  )
    return;
  const resume = ["paused", "failed"].includes(session.value.state);
  const result = await store.runAction(
    `model-import:publish:${session.value.id}`,
    "正在校验并发布模型资产",
    async () => {
      // Retire the previous pause/chunk view before the new run emits progress.
      delete store.modelImportProgress[session.value!.id];
      restoredProgress.value = null;
      session.value = { ...session.value!, state: "hashing" };
      const uploaded = await (resume
        ? desktopBridge().resumeModelUpload({
            sessionId: session.value!.id,
            displayName: form.displayName.trim(),
            mediaKind: "image",
            role: form.role,
            revision: form.revision.trim(),
            licenseDeclared: form.licenseDeclared.trim(),
          })
        : desktopBridge().startModelUpload({
            sessionId: session.value!.id,
            displayName: form.displayName.trim(),
            mediaKind: "image",
            role: form.role,
            revision: form.revision.trim(),
            licenseDeclared: form.licenseDeclared.trim(),
          }));
      if (uploaded.disposition === "paused" || !sameFlow(flow)) return uploaded;
      if (uploaded.asset) return uploaded;
      const assetId = (uploaded.transfer as ResourceItem | undefined)?.asset_id;
      if (!assetId) throw new Error("服务器完成传输但未返回模型资产");
      // Publication and the follow-up read are one user action. A failed read
      // keeps this source recoverable instead of becoming an unhandled rejection.
      try {
        const published = await api<ResourceItem>(`/api/v1/model-assets/${encodeURIComponent(assetId)}`);
        return { ...uploaded, asset: published };
      } catch (error) {
        throw new Error(`模型已入库，但读取详情失败；可重试恢复，不会重复上传。${error instanceof Error ? error.message : String(error)}`);
      }
    },
    {
      success: (value) =>
        value.disposition === "paused"
          ? "上传已暂停，可从原进度继续"
          : value.disposition === "reused"
          ? "已复用服务器相同资产"
          : "模型资产已校验入库",
    },
  );
  if (!sameFlow(flow)) return;
  if (!result) {
    await loadSessions();
    if (sameFlow(flow)) session.value = sessions.value.find(item => item.id === session.value?.id) ?? session.value;
    return;
  }
  if (result.disposition === "paused") {
    await loadSessions();
    if (sameFlow(flow)) session.value = sessions.value.find(item => item.id === session.value?.id) ?? session.value;
    return;
  }
  const uploaded = result as unknown as {
    disposition?: string;
    asset?: ResourceItem;
    transfer?: ResourceItem;
  };
  const found = uploaded.asset;
  if (!sameFlow(flow)) return;
  if (!found) {
    store.toast("服务器完成传输但未返回模型资产", {
      type: "error",
      persistent: true,
    });
    return;
  }
  asset.value = found;
  importDisposition.value =
    uploaded.disposition === "reused" ? "reused" : "uploaded";
  if (["checkpoint", "lora", "vae"].includes(String(found.role))) {
    form.role = found.role as ImportRole;
  }
  step.value = 3;
  await store.refreshSnapshot(false);
  if (!sameFlow(flow)) return;
  await loadSessions();
}

async function pauseUpload() {
  const flow = currentFlow();
  if (!sameFlow(flow) || session.value?.profileId !== store.activeProfile?.id) return;
  if (!session.value) return;
  const result = await store.runAction(
    `model-import:pause:${session.value.id}`,
    "正在暂停模型上传",
    () => desktopBridge().pauseModelUpload(session.value!.id),
    { success: "上传已暂停" },
  );
  if (!sameFlow(flow)) return;
  if (result?.session) session.value = result.session as ModelUploadSession;
  await loadSessions();
}

function profileReason(profile: RuntimeProfile | null) {
  if (!profile || !asset.value) return "资产尚未就绪";
  if (
    !profile.architecture_families.includes(
      String(asset.value.architecture_family),
    )
  )
    return `不支持 ${asset.value.architecture_family || "未知"} 架构`;
  if (!profile.main_formats.includes(String(asset.value.format)))
    return `不支持 ${asset.value.format || "未知"} 格式`;
  return "";
}

function beginDeployment() {
  if (!isCheckpoint.value) {
    step.value = 5;
    return;
  }
  const profile = profiles.value.find((item) => !profileReason(item));
  deployment.runtimeProfileKey = profile
    ? `${profile.profile_id}/${profile.revision}`
    : "";
  if (asset.value)
    deployment.deploymentId = safeDeploymentId(
      String(asset.value.display_name || form.displayName),
    );
  chooseDefaultGpu();
  step.value = 4;
}

function gpuDisabledReason(gpu: ResourceItem | undefined) {
  if (!gpu) return "GPU 不存在";
  if (!gpu.configured_for_mediacenter)
    return "未加入当前服务器的 MediaCenter 资源池";
  if (!gpu.uuid) return "GPU UUID 不可用";
  const total = Number(gpu.memory_total_mib);
  const minimum =
    deployment.requiredVramMib +
    8192 +
    Number(store.gpuResources?.system_reserve_mib ?? 2048);
  if (!Number.isFinite(total)) return "显存遥测不可用";
  if (total < minimum)
    return `总显存低于 ${Math.ceil(minimum / 1024)} GiB 部署预算`;
  if (
    deployment.sharingMode === "exclusive" &&
    Array.isArray(gpu.processes) &&
    gpu.processes.length
  )
    return "检测到外部进程，不能独占";
  return "";
}

function chooseDefaultGpu() {
  const available = gpus.value.find((item) => !gpuDisabledReason(item));
  if (
    available?.uuid &&
    !deployment.gpuUuids.some(
      (uuid) =>
        !gpuDisabledReason(gpus.value.find((item) => item.uuid === uuid)),
    )
  ) {
    deployment.gpuUuids = [available.uuid];
  }
}

function toggleGpu(gpu: ResourceItem) {
  if (gpuDisabledReason(gpu) || !gpu.uuid) return;
  deployment.gpuUuids = deployment.gpuUuids.includes(gpu.uuid)
    ? deployment.gpuUuids.filter((value) => value !== gpu.uuid)
    : [gpu.uuid];
  plan.value = null;
}

async function calculatePlan() {
  const flow = currentFlow();
  if (!sameFlow(flow)) return;
  if (!canSubmitPlan.value || !asset.value || !selectedProfile.value) return;
  const result = await store.planUserDeployment({
    deployment_id: deployment.deploymentId,
    base_asset_id: asset.value.id,
    vae_asset_id: deployment.vaeAssetId || null,
    runtime_profile_id: selectedProfile.value.profile_id,
    runtime_profile_revision: selectedProfile.value.revision,
    gpu_uuids: [...deployment.gpuUuids],
    residency: deployment.residency,
    sharing_mode: deployment.sharingMode,
    required_vram_mib: deployment.requiredVramMib,
    license_accepted: deployment.licenseAccepted,
    experimental_compatibility_accepted: deployment.experimentalAccepted,
  });
  if (!sameFlow(flow) || !result) return;
  plan.value = result;
  idempotencyKey.value = `deploy-${crypto.randomUUID()}`;
  step.value = 5;
}

async function commitDeployment() {
  const flow = currentFlow();
  if (!sameFlow(flow)) return;
  if (!plan.value?.capacity.schedulable || !idempotencyKey.value) return;
  const result = await store.createUserDeployment(
    plan.value.operation,
    idempotencyKey.value,
  );
  if (!sameFlow(flow) || !result) return;
  operation.value = result;
  await store.refreshSnapshot(false);
  if (!sameFlow(flow)) return;
  emit("completed");
}

async function cancelCurrentDeployment() {
  const flow = currentFlow();
  if (!sameFlow(flow)) return;
  const current = currentOperation.value;
  if (
    !current ||
    ["ready", "failed", "canceled", "canceling", "rollback"].includes(
      current.state,
    )
  )
    return;
  const result = await store.cancelDeploymentOperation(current);
  if (sameFlow(flow) && result) operation.value = result;
}

async function retryDeployment() {
  const flow = currentFlow();
  if (!sameFlow(flow)) return;
  const current = currentOperation.value;
  if (
    !current ||
    !["failed", "canceled"].includes(current.state) ||
    !current.payload
  )
    return;
  operation.value = null;
  idempotencyKey.value = `deploy-${crypto.randomUUID()}`;
  const result = await store.createUserDeployment(
    current.payload,
    idempotencyKey.value,
  );
  if (!sameFlow(flow) || !result) return;
  operation.value = result;
  await store.refreshSnapshot(false);
  if (!sameFlow(flow)) return;
  if (result.state === "ready") emit("completed");
}

function reviseDeployment() {
  operation.value = null;
  plan.value = null;
  idempotencyKey.value = "";
  deployment.deploymentId = safeDeploymentId(form.displayName);
  step.value = 4;
}

function finishAssetOnly() {
  emit("completed");
  emit("close");
  resetFlow();
}
function close() {
  emit("close");
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog">
      <div
        v-if="open"
        class="dialog-backdrop model-import-backdrop"
        @pointerdown.self="close"
      >
        <section
          class="model-import-wizard"
          role="dialog"
          aria-modal="true"
          aria-label="导入并部署用户模型"
        >
          <header class="import-titlebar">
            <div>
              <AppIcon name="package" :size="16" /><span
                ><b>导入并部署</b><small>本地模型 · 受控 Runtime</small></span
              >
            </div>
            <button type="button" aria-label="关闭导入向导" @click="close">
              <AppIcon name="close" :size="14" />
            </button>
          </header>

          <nav class="import-steps" aria-label="导入步骤">
            <button
              v-for="item in stepItems"
              :key="item.value"
              type="button"
              :class="{ active: step === item.value, done: step > item.value }"
              :disabled="item.value > step || Boolean(currentOperation)"
              @click="step = item.value"
            >
              <i>{{ step > item.value ? "✓" : item.value }}</i
              ><span>{{ item.label }}</span>
            </button>
          </nav>

          <main class="import-stage">
            <section v-if="step === 1" class="import-source-stage">
              <header>
                <small>01 / SOURCE</small>
                <h2>选择模型来源</h2>
                <p>文件路径不会进入页面或服务器日志。</p>
              </header>
              <div class="source-options">
                <button type="button" @click="chooseSource('file')">
                  <AppIcon name="fileModel" :size="22" /><span
                    ><b>Safetensors 文件</b
                    ><small>基础模型、LoRA 或 VAE</small></span
                  ><AppIcon name="chevron" :size="13" />
                </button>
                <button type="button" @click="chooseSource('directory')">
                  <AppIcon name="folderModel" :size="22" /><span
                    ><b>Diffusers 目录</b
                    ><small>受控白名单目录结构</small></span
                  ><AppIcon name="chevron" :size="13" />
                </button>
              </div>
              <section class="resume-sessions">
                <header>
                  <b>未完成会话</b
                  ><small>{{
                    loadingSessions
                      ? "读取中"
                      : `${sessions.filter((item) => item.state !== "completed").length} 项`
                  }}</small>
                </header>
                <article
                  v-for="item in sessions.filter(
                    (value) => value.state !== 'completed',
                  )"
                  :key="item.id"
                >
                  <AppIcon
                    :name="
                      item.selection === 'directory'
                        ? 'folderModel'
                        : 'fileModel'
                    "
                    :size="15"
                  />
                  <span
                    ><b>{{ item.displayName }}</b
                    ><small
                      >{{ humanBytes(item.totalBytes) }} ·
                      {{ item.state }}</small
                    ></span
                  >
                  <button type="button" @click="resumeSession(item)">
                    继续
                  </button>
                  <button
                    type="button"
                    title="移除续传记录"
                    @click="discardSession(item)"
                  >
                    <AppIcon name="close" :size="12" />
                  </button>
                </article>
                <p
                  v-if="
                    !loadingSessions &&
                    !sessions.some((item) => item.state !== 'completed')
                  "
                >
                  没有待续传内容
                </p>
              </section>
            </section>

            <section
              v-else-if="step === 2 && session"
              class="import-identify-stage"
            >
              <header>
                <small>02 / INSPECT</small>
                <h2>{{ session.displayName }}</h2>
                <p>
                  {{ session.format }} · {{ humanBytes(session.totalBytes) }} ·
                  {{ session.files.length }} 个文件
                </p>
              </header>
              <div class="inspection-grid">
                <dl>
                  <div>
                    <dt>识别角色</dt>
                    <dd>{{ roleLabels[session.preview.role] }}</dd>
                  </div>
                  <div>
                    <dt>架构家族</dt>
                    <dd>{{ session.preview.architectureFamily }}</dd>
                  </div>
                  <div>
                    <dt>张量精度</dt>
                    <dd>{{ session.preview.precision }}</dd>
                  </div>
                  <div>
                    <dt>张量数量</dt>
                    <dd>{{ session.preview.tensorCount ?? "—" }}</dd>
                  </div>
                  <div>
                    <dt>LoRA Rank</dt>
                    <dd>{{ session.preview.loraRank ?? "—" }}</dd>
                  </div>
                  <div>
                    <dt>基础身份</dt>
                    <dd>
                      <code>{{
                        session.preview.baseIdentity?.slice(0, 12) || "未声明"
                      }}</code>
                    </dd>
                  </div>
                </dl>
                <form class="inspection-form" @submit.prevent="publishAsset">
                  <label
                    >显示名称<input
                      v-model.trim="form.displayName"
                      maxlength="120"
                      :disabled="!canEditSource"
                  /></label>
                  <label
                    >资产角色<select
                      v-model="form.role"
                      :disabled="!canEditSource"
                    >
                      <option value="checkpoint">基础模型</option>
                      <option value="lora">LoRA</option>
                      <option value="vae">VAE</option>
                    </select></label
                  >
                  <label
                    >固定版本<input
                      v-model.trim="form.revision"
                      maxlength="160"
                      :disabled="!canEditSource"
                  /></label>
                  <label
                    >许可证声明<input
                      v-model.trim="form.licenseDeclared"
                      maxlength="160"
                      :disabled="!canEditSource"
                  /></label>
                </form>
              </div>
              <section v-if="progress" class="import-live-progress">
                <header>
                  <b>{{
                    stageLabels[progress.stage || ""] || progress.stage
                  }}</b
                  ><small>{{ Math.round(progressRatio * 100) }}%</small>
                </header>
                <i
                  ><em
                    :style="{ width: `${Math.round(progressRatio * 100)}%` }"
                /></i>
                <p>
                  {{ progress.relativePath }} ·
                  {{ humanBytes(progress.receivedBytes) }} /
                  {{ humanBytes(progress.totalBytes) }}
                </p>
              </section>
              <p v-if="restoringTransfer" role="status">正在读取原传输进度…</p>
              <p v-if="restoreError" class="import-blocked" role="alert">{{ restoreError }}</p>
            </section>

            <section v-else-if="step === 3 && asset" class="import-asset-stage">
              <header>
                <small>03 / COMPATIBILITY</small>
                <h2>资产与运行环境兼容检查</h2>
                <p>
                  {{
                    importDisposition === "reused"
                      ? "服务器已存在相同内容，直接复用物理资产。"
                      : "内容寻址发布完成；相同字节不会重复占用空间。"
                  }}
                </p>
              </header>
              <div class="asset-verdict">
                <span class="verdict-mark"
                  ><AppIcon name="check" :size="22"
                /></span>
                <div>
                  <b>{{ asset.display_name }}</b
                  ><code>{{ asset.id }}</code>
                </div>
                <StateBadge label="已校验" tone="ready" />
              </div>
              <dl class="asset-facts">
                <div>
                  <dt>角色</dt>
                  <dd>{{ roleLabels[asset.role] ?? asset.role }}</dd>
                </div>
                <div>
                  <dt>架构</dt>
                  <dd>{{ asset.architecture_family }}</dd>
                </div>
                <div>
                  <dt>格式</dt>
                  <dd>{{ asset.format }}</dd>
                </div>
                <div>
                  <dt>精度</dt>
                  <dd>{{ asset.tensor_precision || "—" }}</dd>
                </div>
                <div>
                  <dt>大小</dt>
                  <dd>{{ humanBytes(asset.total_bytes) }}</dd>
                </div>
                <div>
                  <dt>Manifest</dt>
                  <dd>
                    <code>{{
                      String(asset.manifest_digest || "").slice(0, 12)
                    }}</code>
                  </dd>
                </div>
              </dl>
              <p v-if="!isCheckpoint" class="asset-only-note">
                {{ roleLabels[form.role] }}
                是任务级或部署级可选资产，不创建独立服务容器；现在已可在兼容的图片工作区中选择。
              </p>
              <p
                v-else-if="!profiles.some((item) => !profileReason(item))"
                class="import-blocked"
              >
                没有兼容的受控 Runtime。资产保留在库中，但当前不能部署。
              </p>
            </section>

            <section
              v-else-if="step === 4 && asset"
              class="import-deploy-stage"
            >
              <header>
                <small>04 / PLAN</small>
                <h2>规划独立服务实例</h2>
                <p>选择运行环境、GPU 与显存驻留策略。</p>
              </header>
              <div class="deploy-columns">
                <section>
                  <h3>Runtime</h3>
                  <label
                    v-for="profile in profiles"
                    :key="`${profile.profile_id}/${profile.revision}`"
                    class="runtime-choice"
                    :class="{ disabled: Boolean(profileReason(profile)) }"
                    :tabindex="profileReason(profile) ? 0 : undefined"
                    :aria-disabled="Boolean(profileReason(profile))"
                    :aria-label="
                      profileReason(profile)
                        ? `${profile.label} 不可用：${profileReason(profile)}`
                        : undefined
                    "
                    :title="profileReason(profile) || '可用于当前资产'"
                    ><input
                      v-model="deployment.runtimeProfileKey"
                      type="radio"
                      name="runtime-profile"
                      :value="`${profile.profile_id}/${profile.revision}`"
                      :disabled="Boolean(profileReason(profile))"
                    /><span
                      ><b>{{ profile.label }}</b
                      ><small
                        >{{ profile.profile_id }}/{{ profile.revision }} ·
                        {{ Math.ceil(profile.required_vram_mib / 1024) }}
                        GiB</small
                      ><em v-if="profileReason(profile)">{{
                        profileReason(profile)
                      }}</em></span
                    ></label
                  >
                  <p v-if="!profiles.length" class="import-blocked">
                    服务器未发布可用 Runtime Profile
                  </p>
                </section>
                <section>
                  <h3>GPU</h3>
                  <button
                    v-for="gpu in gpus"
                    :key="gpu.uuid || gpu.index"
                    type="button"
                    class="import-gpu-choice"
                    :class="{
                      active: deployment.gpuUuids.includes(gpu.uuid),
                      disabled: Boolean(gpuDisabledReason(gpu)),
                    }"
                    :aria-disabled="Boolean(gpuDisabledReason(gpu))"
                    :aria-label="
                      gpuDisabledReason(gpu)
                        ? `GPU ${gpu.index} 不可用：${gpuDisabledReason(gpu)}`
                        : `选择 GPU ${gpu.index}`
                    "
                    :title="gpuDisabledReason(gpu) || '可用于当前部署'"
                    @click="toggleGpu(gpu)"
                  >
                    <span
                      ><b>GPU {{ gpu.index }} · {{ gpu.name }}</b
                      ><small
                        >{{
                          Math.round(Number(gpu.memory_free_mib || 0) / 1024)
                        }}
                        /
                        {{
                          Math.round(Number(gpu.memory_total_mib || 0) / 1024)
                        }}
                        GiB 空闲</small
                      ><em v-if="gpuDisabledReason(gpu)">{{
                        gpuDisabledReason(gpu)
                      }}</em></span
                    ><i />
                  </button>
                </section>
                <section>
                  <h3>实例</h3>
                  <label
                    >实例标识<input
                      v-model.trim="deployment.deploymentId"
                      maxlength="64"
                      pattern="[a-z0-9][a-z0-9.-]*"
                      @input="plan = null" /></label
                  ><label
                    >可选 VAE<select
                      v-model="deployment.vaeAssetId"
                      @change="plan = null"
                    >
                      <option value="">使用模型内置 VAE</option>
                      <option
                        v-for="item in vaeAssets"
                        :key="item.id"
                        :value="item.id"
                      >
                        {{ item.display_name }}
                      </option>
                    </select></label
                  ><label
                    >资源方式<select
                      v-model="deployment.sharingMode"
                      @change="
                        chooseDefaultGpu();
                        plan = null;
                      "
                    >
                      <option value="shared">共享 GPU</option>
                      <option value="exclusive">独占 GPU</option>
                    </select></label
                  >
                </section>
              </div>
              <section class="deploy-residency">
                <h3>显存策略</h3>
                <label
                  v-for="value in ['on_demand', 'idle', 'resident']"
                  :key="value"
                  :class="{
                    disabled: !selectedProfile?.residency_modes.includes(value),
                  }"
                  :tabindex="
                    selectedProfile?.residency_modes.includes(value)
                      ? undefined
                      : 0
                  "
                  :aria-disabled="
                    !selectedProfile?.residency_modes.includes(value)
                  "
                  :aria-label="
                    selectedProfile?.residency_modes.includes(value)
                      ? undefined
                      : `${residencyLabels[value]}不可用：当前 Runtime 不支持此策略`
                  "
                  :title="
                    selectedProfile?.residency_modes.includes(value)
                      ? '当前 Runtime 支持'
                      : '当前 Runtime 不支持此策略'
                  "
                >
                  <input
                    v-model="deployment.residency"
                    type="radio"
                    name="import-residency"
                    :value="value"
                    :disabled="
                      !selectedProfile?.residency_modes.includes(value)
                    "
                    @change="plan = null"
                  /><span
                    ><b>{{ residencyLabels[value] }}</b
                    ><small>{{
                      value === "on_demand"
                        ? "任务到达时加载权重"
                        : value === "idle"
                          ? "空闲后延迟释放"
                          : "启动后保持显存就绪"
                    }}</small
                    ><em
                      v-if="!selectedProfile?.residency_modes.includes(value)"
                      >当前 Runtime 不支持</em
                    ></span
                  ></label
                >
              </section>
              <label class="import-license"
                ><input
                  v-model="deployment.licenseAccepted"
                  type="checkbox"
                />我确认该资产的许可证声明，并授权在当前服务器部署</label
              >
              <label v-if="deployment.vaeAssetId" class="import-license"
                ><input
                  v-model="deployment.experimentalAccepted"
                  type="checkbox"
                />若 VAE 只有实验性兼容证据，允许继续规划</label
              >
            </section>

            <section v-else-if="step === 5" class="import-confirm-stage">
              <header>
                <small>05 / EXECUTE</small>
                <h2>
                  {{
                    currentOperation
                      ? operationLabels[currentOperation.state]
                      : isCheckpoint
                        ? "核对原子部署计划"
                        : "资产导入完成"
                  }}
                </h2>
                <p>
                  {{
                    isCheckpoint
                      ? "提交后创建独立容器，但不会自动启动服务。"
                      : "物理资产与运行服务保持分离。"
                  }}
                </p>
              </header>
              <template v-if="!isCheckpoint && asset"
                ><div class="completion-panel">
                  <AppIcon name="check" :size="24" /><span
                    ><b>{{ asset.display_name }}</b
                    ><small
                      >{{ roleLabels[form.role] }} ·
                      {{ asset.architecture_family }} ·
                      {{ humanBytes(asset.total_bytes) }}</small
                    ></span
                  >
                </div></template
              >
              <template v-else-if="plan"
                ><dl class="plan-review">
                  <div>
                    <dt>基础资产</dt>
                    <dd>{{ asset?.display_name }}</dd>
                  </div>
                  <div>
                    <dt>导入结果</dt>
                    <dd>
                      {{
                        importDisposition === "reused"
                          ? "内容复用 · 0 B"
                          : `${humanBytes(session?.totalBytes || asset?.total_bytes || 0)} · 已上传校验`
                      }}
                    </dd>
                  </div>
                  <div>
                    <dt>资产摘要</dt>
                    <dd>
                      <code>{{
                        String(asset?.manifest_digest || "").slice(0, 20)
                      }}</code>
                    </dd>
                  </div>
                  <div>
                    <dt>实例</dt>
                    <dd>
                      <code>{{ plan.operation.deployment_id }}</code>
                    </dd>
                  </div>
                  <div>
                    <dt>Runtime</dt>
                    <dd>
                      {{ plan.runtime_profile.label }} /
                      {{ plan.runtime_profile.revision }}
                    </dd>
                  </div>
                  <div>
                    <dt>镜像摘要</dt>
                    <dd>
                      <code>{{
                        plan.runtime_profile.image_digest.slice(0, 20)
                      }}</code>
                    </dd>
                  </div>
                  <div>
                    <dt>GPU</dt>
                    <dd>{{ plan.operation.gpu_uuids.join(" · ") }}</dd>
                  </div>
                  <div>
                    <dt>显存策略</dt>
                    <dd>{{ residencyLabels[plan.operation.residency] }}</dd>
                  </div>
                  <div>
                    <dt>容器动作</dt>
                    <dd>创建但不启动</dd>
                  </div>
                </dl>
                <p
                  :class="
                    plan.capacity.schedulable ? 'plan-ready' : 'import-blocked'
                  "
                >
                  {{
                    plan.capacity.schedulable
                      ? "容量合同通过，可以提交部署。"
                      : `当前不可调度：${plan.capacity.reason}`
                  }}
                </p></template
              >
              <section
                v-if="currentOperation"
                class="operation-result"
                :class="currentOperation.state"
              >
                <header>
                  <StateBadge
                    :label="
                      operationLabels[currentOperation.state] ||
                      currentOperation.state
                    "
                    :tone="
                      currentOperation.state === 'ready'
                        ? 'ready'
                        : currentOperation.state === 'failed'
                          ? 'error'
                          : currentOperation.state === 'canceled'
                            ? 'neutral'
                            : 'busy'
                    "
                  /><code>{{ currentOperation.id }}</code>
                </header>
                <p v-if="currentOperation.state === 'ready'">
                  容器已物化，服务保持停止；可在模型中心启动并按所选策略加载权重。
                </p>
                <p v-else-if="currentOperation.state === 'failed'">
                  {{ currentOperation.error_message || currentOperation.error_code }}
                </p>
                <p v-else-if="currentOperation.state === 'canceled'">
                  已停止部署并回收本次运行资源；模型资产和实例配置仍保留。
                </p>
                <p v-else>
                  {{ operationLabels[currentOperation.state] }}，关闭窗口不会中断执行。
                </p>
              </section>
            </section>
          </main>

          <footer class="import-footer">
            <span
              ><b>{{ activeProfileName }}</b
              ><small v-if="session">{{ session.id.slice(0, 8) }}</small></span
            >
            <div>
              <button type="button" class="flat-button" @click="close">
                后台继续
              </button>
              <button
                v-if="step > 1 && !currentOperation && step < 5"
                type="button"
                class="flat-button"
                @click="step--"
              >
                上一步
              </button>
              <ActionButton
                v-if="step === 2"
                :action-key="`model-import:publish:${session?.id}`"
                icon="upload"
                :label="restoreError ? '重新读取' : session?.state === 'paused' ? '继续上传' : session?.state === 'failed' ? '重试上传' : '校验并入库'"
                tone="primary"
                :disabled="
                  restoringTransfer || !form.displayName || !form.revision || !form.licenseDeclared
                "
                @click="publishAsset"
              />
              <ActionButton
                v-if="step === 2 && progress?.stage === 'uploading' && !['paused', 'failed', 'completed'].includes(session?.state || '')"
                :action-key="`model-import:pause:${session?.id}`"
                icon="stop"
                label="暂停"
                @click="pauseUpload"
              />
              <ActionButton
                v-if="step === 3"
                icon="chevron"
                :label="isCheckpoint ? '配置部署' : '完成'"
                tone="primary"
                :disabled="
                  isCheckpoint && !profiles.some((item) => !profileReason(item))
                "
                @click="beginDeployment"
              />
              <ActionButton
                v-if="step === 4"
                action-key="deployment:plan"
                icon="scan"
                label="生成计划"
                tone="primary"
                :disabled="!canSubmitPlan"
                @click="calculatePlan"
              />
              <ActionButton
                v-if="step === 5 && !isCheckpoint"
                icon="check"
                label="完成"
                tone="primary"
                @click="finishAssetOnly"
              />
              <ActionButton
                v-if="step === 5 && isCheckpoint && !currentOperation"
                :action-key="`deployment:create:${plan?.operation.deployment_id}`"
                icon="package"
                label="创建容器"
                tone="primary"
                :disabled="!plan?.capacity.schedulable"
                @click="commitDeployment"
              />
              <ActionButton
                v-if="step === 5 && currentOperation?.state === 'ready'"
                icon="check"
                label="返回模型中心"
                tone="primary"
                @click="finishAssetOnly"
              />
              <ActionButton
                v-if="
                  step === 5 &&
                  currentOperation &&
                  ![
                    'ready',
                    'failed',
                    'canceled',
                    'canceling',
                    'rollback',
                  ].includes(currentOperation.state)
                "
                :action-key="`deployment-operation:${currentOperation.id}:cancel`"
                icon="stop"
                label="取消"
                tone="danger"
                @click="cancelCurrentDeployment"
              />
              <ActionButton
                v-if="
                  step === 5 &&
                  (currentOperation?.state === 'canceled' ||
                    (currentOperation?.state === 'failed' &&
                      currentOperation.error_class === 'recoverable'))
                "
                :action-key="`deployment:create:${plan?.operation.deployment_id}`"
                icon="refresh"
                label="重试"
                tone="primary"
                @click="retryDeployment"
              />
              <ActionButton
                v-if="
                  step === 5 &&
                  currentOperation?.state === 'failed' &&
                  currentOperation.error_class === 'non_recoverable'
                "
                icon="sliders"
                label="修改计划"
                @click="reviseDeployment"
              />
            </div>
          </footer>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>
