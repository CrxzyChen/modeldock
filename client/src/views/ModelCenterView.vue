<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import ActionButton from "@/components/ActionButton.vue";
import AppIcon from "@/components/AppIcon.vue";
import ModelImportWizard from "@/components/ModelImportWizard.vue";
import StateBadge from "@/components/StateBadge.vue";
import { formatDate, humanBytes } from "@/lib/format";
import {
  activeInstallationStates,
  buildCatalogModels,
  canUninstall,
  configurationLabels,
  containerLabels,
  deploymentFor,
  deploymentSettingsDraft,
  deploymentOperationLabels,
  expectedConfiguration,
  isUserDeployment,
  installationActions,
  installationLabels,
  isInstalled,
  isRunning,
  mediaLabels,
  modelState,
  residencyLabels,
  runtimeErrorHint,
  runtimeErrorLabel,
  runtimeLabels,
  serviceLabels,
  sameModelIdentity,
  userConfigurationRequest,
  type CatalogModel,
} from "@/lib/models";
import { api } from "@/services/api";
import { desktopBridge } from "@/services/desktop";
import { useAppStore, type AppView } from "@/stores/app";
import type { DeploymentOperation, DeploymentPlan, ResourceItem, RuntimeProfile } from "@/types/contracts";

type Category = "all" | "image" | "video" | "speech" | "music";
type InstallFilter = "all" | "installed" | "available";
type ModelTab = "overview" | "assets" | "history";
type AssetMode = "services" | "assets";
const store = useAppStore();
const mode = ref<AssetMode>("services");
const query = ref("");
const category = ref<Category>("all");
const statusFilter = ref<"all" | "running" | "stopped">("all");
const selectedKey = ref("");
const selectedDeploymentId = ref("");
const tab = ref<ModelTab>("overview");
const installOpen = ref(false);
const installFilter = ref<InstallFilter>("all");
const installSelectedKey = ref("");
const settings = reactive(deploymentSettingsDraft(null));
const settingsDirty = ref(false);
const settingsOpen = ref(false);
const settingsSnapshot = ref<ResourceItem | null>(null);
const settingsPlan = ref<DeploymentPlan | null>(null);
const settingsOperation = ref<DeploymentOperation | null>(null);
const settingsBusy = ref(false);
const settingsKey = ref("");
const settingsSubmitted = ref(false);
const settingsBoundAssets = ref<ResourceItem[]>([]);
let settingsRevision = 0;
const serverScope = computed(() => JSON.stringify([store.activeProfile?.id, store.connectionEpoch]));
const installDrafts = reactive<
  Record<string, { gpus: number[]; license: boolean; token: string }>
>({});
const importWizardOpen = ref(false);
const downloadOpen = ref(false);
const download = reactive({
  display_name: "",
  url: "",
  filename: "",
  revision: "",
  expected_bytes: 0,
  expected_sha256: "",
  media_kind: "image",
  role: "checkpoint",
  format: "safetensors",
  license_declared: "unknown",
});
const models = computed(() =>
  buildCatalogModels(
    store.catalog,
    store.deployments,
    store.installations,
    store.assets,
  ),
);
const installedModels = computed(() => models.value.filter(isInstalled));
const filtered = computed(() =>
  installedModels.value.filter(
    (item) =>
      (category.value === "all" || item.kind === category.value) &&
      (statusFilter.value === "all" ||
        (statusFilter.value === "running" ? isRunning(item) : !isRunning(item))) &&
      `${item.label} ${item.model_id}`
        .toLowerCase()
        .includes(query.value.toLowerCase()),
  ),
);
const running = computed(() => filtered.value.filter(isRunning));
const stopped = computed(() =>
  filtered.value.filter((item) => !isRunning(item)),
);
const selected = computed(
  () =>
    installedModels.value.find((item) => item.key === selectedKey.value) ??
    filtered.value[0] ??
    installedModels.value[0] ??
    null,
);
const deployment = computed(() =>
  deploymentFor(selected.value, selectedDeploymentId.value),
);
const gpus = computed<ResourceItem[]>(() =>
  (store.gpuResources?.gpus ?? []).filter(
    (item: ResourceItem) => item.configured_for_mediacenter && item.uuid,
  ),
);
const installModels = computed(() =>
  models.value.filter(
    (item) =>
      (category.value === "all" || item.kind === category.value) &&
      `${item.label} ${item.model_id}`
        .toLowerCase()
        .includes(query.value.toLowerCase()) &&
      (installFilter.value === "all" ||
        (installFilter.value === "installed"
          ? isInstalled(item)
          : !isInstalled(item))),
  ),
);
const installSelected = computed(
  () =>
    models.value.find((item) => item.key === installSelectedKey.value) ??
    installModels.value[0] ??
    null,
);
const installDeployment = computed(() => deploymentFor(installSelected.value));
const activeInstall = computed(
  () => installSelected.value?.installations[0] ?? null,
);
const currentInstallDraft = computed(() =>
  installSelected.value
    ? ensureInstallDraft(installSelected.value)
    : { gpus: [], license: false, token: "" },
);
const residencyOptions = [
  { value: "on_demand", label: "按需", detail: "任务时加载，完成后释放" },
  { value: "idle", label: "保温", detail: "空闲超时后释放" },
  { value: "resident", label: "常驻", detail: "启动后保持显存就绪" },
];
const userSettings = computed(() => Boolean(deployment.value && isUserDeployment(deployment.value)));
const settingsProfile = computed(() => store.runtimeProfiles.find(
  item => `${item.profile_id}/${item.revision}` === settings.runtime_profile_key) ?? null);
const settingsAssets = computed(() => [...new Map([...settingsBoundAssets.value, ...store.assets].map(item => [item.id, item])).values()]);
const settingsBaseAssets = computed(() => settingsAssets.value.filter(item => item.role === "checkpoint" && item.media_kind === "image"));
const settingsVaeAssets = computed(() => settingsAssets.value.filter(item => item.role === "vae" && item.media_kind === "image"));
const settingsStale = computed(() => Boolean(settingsSnapshot.value && deployment.value && (
  !sameModelIdentity(settingsSnapshot.value.configuration_binding, deployment.value.configuration_binding) ||
  settingsSnapshot.value.instance_settings?.policy_version !== deployment.value.instance_settings?.policy_version)));
const activeSettingsOperation = computed(() => store.deploymentOperations.find(item =>
  item.deployment_id === deployment.value?.id && !["ready", "failed", "canceled"].includes(item.state)) ?? null);
const currentSettingsOperation = computed(() => activeSettingsOperation.value ?? (
  store.deploymentOperations.find(item => item.id === settingsOperation.value?.id) ?? settingsOperation.value));
const settingsApplying = computed(() => Boolean(activeSettingsOperation.value || deployment.value?.pending_config_revision != null ||
  (deployment.value && deployment.value.configuration_state !== "applied") ||
  (currentSettingsOperation.value && !["ready", "failed", "canceled"].includes(currentSettingsOperation.value.state))));
const settingsLocked = computed(() => Boolean(deployment.value?.removal_operation_id) || settingsBusy.value || settingsApplying.value || settingsSubmitted.value);
const settingsBlock = computed(() => {
  if (deployment.value?.removal_operation_id) return "实例卸载尚未完成，配置已锁定；失败后可在后台任务中重试卸载。";
  if (settingsApplying.value) return "配置正在应用，完成或回滚后可继续编辑。";
  if (settingsSubmitted.value) return "已提交配置；可查看后台进度，或重新读取生效配置。";
  if (settingsStale.value) return "服务器配置版本已变化；编辑内容已保留，请读取最新配置后重新确认。";
  if (!settings.gpu_uuids.length || settings.gpu_uuids.some(uuid => !gpus.value.some(gpu => gpu.uuid === uuid))) return "请选择当前可分配的 GPU。";
  if (userSettings.value) {
    if (!expectedConfiguration(settingsSnapshot.value)) return "实例配置版本不可用，请同步后重试。";
    if (!settingsBaseAssets.value.some(item => item.id === settings.base_asset_id && item.state === "ready")) return "基础模型尚未就绪。";
    const profile = settingsProfile.value;
    if (!profile || profileDisabledReason(profile)) return "请选择兼容且可用的运行环境。";
    if (settings.vae_asset_id && (!profile.optional_deployment_roles.includes("vae") ||
      !settingsVaeAssets.value.some(item => item.id === settings.vae_asset_id && item.state === "ready"))) return "外部 VAE 尚未就绪或运行环境不支持。";
    if (!Number.isInteger(settings.required_vram_mib) || settings.required_vram_mib < profile.required_vram_mib || settings.required_vram_mib > 196608) return "显存预算低于运行环境要求或超过上限。";
    if (!settings.license_accepted) return "请确认所选模型的许可与使用责任。";
  }
  if (!residencySupported(settings.residency)) return "所选运行环境不支持该显存策略。";
  if (!Number.isInteger(settings.idle_minutes * 60) || settings.idle_minutes < 0 || settings.idle_minutes > 1440) return "保温时长应为 0–1440 分钟。";
  return "";
});
function profileDisabledReason(profile: RuntimeProfile) {
  const base = settingsBaseAssets.value.find(item => item.id === settings.base_asset_id);
  return !base || !profile.architecture_families?.includes(base.architecture_family) || !profile.main_formats?.includes(base.format)
    ? "与基础模型不兼容" : "";
}

watch(
  () => selected.value?.key,
  () => {
    const item = selected.value;
    if (item) {
      selectedKey.value = item.key;
      if (!item.deployments.some(dep => dep.id === selectedDeploymentId.value))
        selectedDeploymentId.value = deploymentFor(item)?.id ?? "";
    }
  },
  { immediate: true },
);
watch(
  () => deployment.value?.id,
  () => {
    resetSettings();
  },
  { immediate: true },
);
watch(serverScope, () => {
  settingsRevision++;
  settingsOpen.value = false;
  settingsSnapshot.value = null;
  settingsBoundAssets.value = [];
  settingsPlan.value = null;
  settingsOperation.value = null;
  settingsDirty.value = false;
  settingsBusy.value = false;
  settingsSubmitted.value = false;
}, { flush: "sync" });
watch(settingsOpen, open => {
  if (!open) { settingsRevision++; settingsBusy.value = false; }
}, { flush: "sync" });
watch([settingsOpen, () => deployment.value?.id], () => {
  store.focusedDeploymentId = settingsOpen.value ? deployment.value?.id ?? null : null;
}, { flush: "sync" });
onBeforeUnmount(() => { store.focusedDeploymentId = null; });
watch([() => store.modelSettingsRequest, models], () => {
  const request = store.modelSettingsRequest;
  if (!request) return;
  const item = models.value.find(model => model.deployments.some(dep => dep.id === request.deploymentId));
  if (!item) return;
  openSettings(item, request.deploymentId);
  store.modelSettingsRequest = null;
}, { immediate: true });
watch(
  installSelected,
  (item) => {
    if (item) installSelectedKey.value = item.key;
  },
  { immediate: true },
);
function selectModel(item: CatalogModel) {
  selectedKey.value = item.key;
  selectedDeploymentId.value = deploymentFor(item)?.id ?? "";
  tab.value = "overview";
  settingsOpen.value = false;
}
function openSettings(item: CatalogModel, deploymentId?: string) {
  selectedKey.value = item.key;
  selectedDeploymentId.value = deploymentFor(item, deploymentId)?.id ?? "";
  resetSettings();
  settingsOpen.value = true;
  void loadSettingsAssets();
}
function markDirty() {
  settingsRevision++;
  settingsDirty.value = true;
  settingsPlan.value = null;
  settingsKey.value = "";
}
function resetSettings() {
  const value = deployment.value;
  settingsRevision++;
  settingsSnapshot.value = value ? JSON.parse(JSON.stringify(value)) : null;
  settingsBoundAssets.value = [];
  Object.assign(settings, deploymentSettingsDraft(value));
  settingsPlan.value = null;
  settingsOperation.value = null;
  settingsKey.value = "";
  settingsBusy.value = false;
  settingsSubmitted.value = false;
  settingsDirty.value = false;
  if (settingsOpen.value) void loadSettingsAssets();
}
async function loadSettingsAssets() {
  const owner = serverScope.value, instance = deployment.value?.id;
  const ids = [settingsSnapshot.value?.configuration_binding?.base_asset_id, settingsSnapshot.value?.configuration_binding?.vae_asset_id]
    .filter((id): id is string => typeof id === 'string' && !store.assets.some(item => item.id === id));
  for (const id of ids) {
    const result = await store.runAction(`settings-asset:${owner}:${instance}:${id}`, '正在读取已绑定资产',
      () => api<ResourceItem>(`/api/v1/model-assets/${encodeURIComponent(id)}`), {record: false});
    if (owner !== serverScope.value || instance !== deployment.value?.id || !settingsOpen.value) return;
    if (result?.id === id) settingsBoundAssets.value = [...settingsBoundAssets.value.filter(item => item.id !== id), result];
  }
}
function settingsFlow() { return JSON.stringify([serverScope.value, deployment.value?.id, settingsRevision]); }
async function previewSettings() {
  if (!settingsSnapshot.value || !settingsProfile.value || settingsBlock.value || settingsBusy.value) return;
  const flow = settingsFlow();
  settingsBusy.value = true;
  try {
    const result = await store.planUserDeployment(userConfigurationRequest(settingsSnapshot.value, settings, settingsProfile.value));
    if (flow !== settingsFlow() || !result || settingsStale.value) return;
    settingsPlan.value = result;
    settingsKey.value = `configure-${crypto.randomUUID()}`;
  } finally { if (flow === settingsFlow()) settingsBusy.value = false; }
}
async function saveSettings() {
  if (!settingsSnapshot.value || settingsBlock.value || settingsBusy.value) return;
  const flow = settingsFlow();
  settingsBusy.value = true;
  try {
    if (userSettings.value) {
      if (!settingsPlan.value?.capacity.schedulable || !settingsKey.value) return;
      const result = await store.createUserDeployment(settingsPlan.value.operation, settingsKey.value);
      if (flow !== settingsFlow() || !result) return;
      settingsOperation.value = result;
      settingsSubmitted.value = true;
      await store.refreshSnapshot(false);
    } else {
      const result = await store.saveDeploymentPolicy(settingsSnapshot.value, {
        gpu_uuids: [...settings.gpu_uuids], sharing_mode: settings.sharing_mode,
        external_reserve_mib: settings.external_reserve_mib, residency: settings.residency,
        idle_minutes: settings.idle_minutes, restart_recovery: settings.restart_recovery,
      });
      if (flow === settingsFlow() && result) resetSettings();
    }
  } finally { if (flow === settingsFlow()) settingsBusy.value = false; }
}
function ensureInstallDraft(item: CatalogModel) {
  return (installDrafts[item.key] ??= {
    gpus: (item.recommended_gpus ?? []).filter((index: number) =>
      gpus.value.some((gpu) => gpu.index === index),
    ),
    license: false,
    token: "",
  });
}
function installBlocked(item: CatalogModel | null) {
  if (!item) return "没有选择模型";
  const draft = ensureInstallDraft(item);
  if (
    item.authentication?.needed &&
    !store.activeProfile?.sourceAuthorizationAllowed
  )
    return "当前连接没有授权提交模型来源令牌";
  if (item.authentication?.needed) return "请先完成 Hugging Face 来源授权";
  if (item.runtime_available === false) return "运行环境制品当前不可用";
  if (item.prerequisites_ready === false)
    return `请先安装：${(item.prerequisites ?? [])
      .filter((value: any) => !value.installed)
      .map((value: any) => value.label)
      .join("、")}`;
  if (
    draft.gpus.length < (item.min_gpus ?? 1) ||
    draft.gpus.length > (item.max_gpus ?? item.min_gpus ?? 1)
  )
    return `请选择 ${item.min_gpus ?? 1}–${item.max_gpus ?? item.min_gpus ?? 1} 张 GPU`;
  if (!draft.license) return "接受许可证后才能安装";
  return "";
}
async function authorize(item: CatalogModel) {
  const auth = item.authentication;
  const draft = ensureInstallDraft(item);
  if (!auth || !draft.token.trim()) return;
  if (
    await store.configureSourceAuthorization(auth.provider, draft.token.trim())
  )
    draft.token = "";
}
async function clearAuthorization(item: CatalogModel) {
  if (!item.authentication) return;
  if (
    await store.confirm(
      "清除来源授权",
      "会从当前服务器内存和这台 PC 的加密存储中清除令牌。",
      "清除授权",
    )
  )
    await store.configureSourceAuthorization(
      item.authentication.provider,
      undefined,
      true,
    );
}
async function install(item: CatalogModel) {
  const draft = ensureInstallDraft(item);
  if (installBlocked(item)) return;
  await store.installRecipe(item, draft);
}
async function uninstall(item: CatalogModel) {
  await store.uninstallRecipe(item);
}
async function downloadModel() {
  const result = await store.runAction(
    "model:download",
    "正在创建模型下载",
    () =>
      api<ResourceItem>("/api/v1/model-transfers", {
        method: "POST",
        body: { direction: "download", source_type: "https", ...download },
      }),
    { success: "模型下载任务已创建" },
  );
  if (result) {
    downloadOpen.value = false;
    store.transfers.unshift(result);
  }
}
async function assetAction(asset: ResourceItem, action: "archive" | "restore") {
  const result = await store.runAction(
    `asset:${asset.id}:${action}`,
    action === "archive" ? "正在归档资产" : "正在恢复资产",
    () =>
      api<ResourceItem>(
        `/api/v1/model-assets/${encodeURIComponent(asset.id)}/${action}`,
        { method: "POST", body: {} },
      ),
    { success: action === "archive" ? "模型资产已归档" : "模型资产已恢复" },
  );
  if (result) await store.refreshSnapshot(false);
}
function stateLabel(item: CatalogModel) {
  return modelState(item);
}
function short(value: unknown, size = 11) {
  const text = String(value ?? "");
  return text.length > size * 2 + 1
    ? `${text.slice(0, size)}…${text.slice(-size)}`
    : text || "—";
}
function groupRow(item: CatalogModel) {
  const dep = deploymentFor(item);
  const gpu = dep?.instance_settings?.gpu_uuids
    ?.map((value: string) => short(value, 4))
    .join("+") || "GPU —";
  const vramMib = Number(
    dep?.required_vram_mib ??
      Number(dep?.instance_settings?.base_mib ?? 0) +
        Number(dep?.instance_settings?.task_mib ?? 0),
  );
  const residency =
    residencyLabels[dep?.instance_settings?.residency] ?? "策略未配置";
  return {
    dep,
    state: stateLabel(item),
    residency,
    summary: `${short(item.revision, 4)} · ${gpu} · ${
      vramMib ? humanBytes(vramMib * 1024 * 1024) : "显存 —"
    } · ${residency}`,
  };
}
function serviceStateLabel(value: ResourceItem | null) {
  if (value?.removal_operation_id)
    return value.removal_operation_state === "failed" ? "卸载未完成" : "正在卸载服务";
  const error = value?.runtime_last_error || value?.last_error;
  return error
    ? runtimeErrorLabel(error)
    : (serviceLabels[value?.service_state] ?? value?.service_state ?? "未同步");
}
function serviceStateHint(value: ResourceItem | null) {
  if (value?.removal_operation_id)
    return value.removal_operation_state === "failed"
      ? "实例保持锁定；模型资产保留，可在后台任务中重试卸载"
      : "正在确认容器退出并移除；模型资产保留";
  const error = value?.runtime_last_error || value?.last_error;
  return error
    ? runtimeErrorHint(error)
    : value?.accepting_tasks
      ? "新任务可以进入调度"
      : "当前拒绝新任务";
}
function residencySupported(value: string) {
  return (
    (userSettings.value ? settingsProfile.value?.residency_modes : deployment.value?.instance_settings?.residency_modes) ?? [
      "on_demand",
      "idle",
      "resident",
    ]
  ).includes(value);
}
function modelRuntimeLabel(value: ResourceItem | null) {
  if (value?.actual_state !== "loaded")
    return (
      runtimeLabels[value?.actual_state] ?? value?.actual_state ?? "未同步"
    );
  const mode = value?.instance_settings?.residency;
  return mode === "resident"
    ? "显存常驻"
    : mode === "idle"
      ? "模型已保温"
      : "按需执行就绪";
}
function controlModel(item: CatalogModel, action: "start" | "stop") {
  const dep = deploymentFor(item);
  if (dep) void store.deploymentAction(dep, action);
}
function openModelLog(item: CatalogModel) {
  selectModel(item);
  tab.value = "history";
}
function openModelWorkspace(item: CatalogModel) {
  if (["image", "video", "speech", "music"].includes(item.kind))
    store.navigate(item.kind as AppView);
}
</script>

<template>
  <section class="workbench-view model-center">
    <header class="model-commandbar">
      <nav>
        <button
          type="button"
          :class="{ active: mode === 'services' }"
          @click="mode = 'services'"
        >
          服务模型 <span>{{ installedModels.length }}</span></button
        ><button
          type="button"
          :class="{ active: mode === 'assets' }"
          @click="mode = 'assets'"
        >
          模型资产 <span>{{ store.assets.length }}</span>
        </button>
      </nav>
      <label
        ><AppIcon name="search" :size="15" /><input
          v-model="query"
          :placeholder="
            mode === 'services' ? '搜索已安装模型' : '搜索物理模型资产'
          "
      /></label>
      <div>
        <ActionButton
          icon="package"
          label="导入并部署"
          tone="primary"
          compact
          @click="importWizardOpen = true"
        /><ActionButton
          icon="download"
          label="安装中心"
          compact
          @click="installOpen = true"
        /><ActionButton
          icon="refresh"
          title="刷新"
          compact
          @click="store.refreshSnapshot(true)"
        />
      </div>
    </header>
    <div
      v-if="mode === 'services'"
      class="model-body"
      :class="{ 'settings-open': settingsOpen }"
    >
      <aside class="model-catalog">
        <nav>
          <button
            v-for="value in [
              'all',
              'image',
              'video',
              'speech',
              'music',
            ] as Category[]"
            :key="value"
            type="button"
            :class="{ active: category === value }"
            @click="category = value"
          >
            {{ value === "all" ? "全部" : mediaLabels[value] }}
          </button>
          <i class="model-filter-divider" aria-hidden="true" />
          <button
            v-for="value in ['all', 'running', 'stopped'] as const"
            :key="`status-${value}`"
            type="button"
            :class="{ active: statusFilter === value }"
            @click="statusFilter = value"
          >
            {{ value === "all" ? "全态" : value === "running" ? "运行" : "停止" }}
          </button>
        </nav>
        <div class="model-list">
          <section
            v-for="group in [
              { label: '已启动', hint: '容器在线并按策略接单', rows: running },
              { label: '已停止', hint: '容器离线且不接新任务', rows: stopped },
            ]"
            :key="group.label"
            v-show="group.rows.length"
          >
            <header>
              <span
                ><b>{{ group.label }}</b
                ><small>{{ group.hint }}</small></span
              ><em>{{ group.rows.length }}</em>
            </header>
            <article
              v-for="item in group.rows"
              :key="item.key"
              class="model-row"
              :class="{ active: selected?.key === item.key }"
              role="button"
              tabindex="0"
              @click="selectModel(item)"
              @keydown.enter.prevent="selectModel(item)"
              @keydown.space.prevent="selectModel(item)"
            >
              <span class="model-kind"
                ><AppIcon :name="(item.kind || 'models') as any" :size="14"
              /></span>
              <div>
                <b>{{ item.label }}</b
                ><small :title="groupRow(item).summary"
                  >{{ groupRow(item).summary }}</small
                ><StateBadge
                  :label="groupRow(item).state.label"
                  :tone="groupRow(item).state.tone"
                />
              </div>
              <div class="row-actions">
                <ActionButton
                  v-if="groupRow(item).dep && isRunning(item)"
                  class="model-action-stop"
                  :action-key="`deployment:${groupRow(item).dep?.id}:stop`"
                  icon="power"
                  title="停止容器服务"
                  compact
                  @click.stop="controlModel(item, 'stop')"
                /><ActionButton
                  v-else-if="groupRow(item).dep"
                  class="model-action-start"
                  :action-key="`deployment:${groupRow(item).dep?.id}:start`"
                  icon="play"
                  title="启动容器服务"
                  :disabled="Boolean(groupRow(item).dep?.removal_operation_id)"
                  compact
                  @click.stop="controlModel(item, 'start')"
                /><ActionButton
                  class="model-action-settings"
                  icon="sliders"
                  title="配置显存策略"
                  :disabled="Boolean(groupRow(item).dep?.removal_operation_id)"
                  compact
                  @click.stop="openSettings(item)"
                />
              </div>
            </article>
          </section>
          <p v-if="!filtered.length" class="empty-state">
            当前筛选没有已安装模型
          </p>
        </div>
      </aside>
      <main v-if="selected" class="model-detail">
        <header>
          <span class="model-hero"
            ><AppIcon :name="(selected.kind || 'models') as any" :size="25"
          /></span>
          <div>
            <small>{{ mediaLabels[selected.kind] }}模型</small>
            <h1>{{ selected.label }}</h1>
            <p>{{ selected.model_id }} · {{ short(selected.revision) }}</p>
          </div>
          <div class="model-detail-actions">
            <StateBadge
              :label="stateLabel(selected).label"
              :tone="stateLabel(selected).tone"
            /><ActionButton
              v-if="deployment"
              icon="sliders"
              title="打开实例设置"
              :disabled="Boolean(deployment.removal_operation_id)"
              compact
              @click="selected && openSettings(selected, deployment.id)"
            /><ActionButton
              icon="audit"
              title="查看安装日志"
              compact
              @click="openModelLog(selected)"
            /><ActionButton
              :icon="(selected.kind || 'models') as any"
              title="打开生成工作台"
              compact
              @click="openModelWorkspace(selected)"
            />
          </div>
        </header>
        <nav>
          <button
            v-for="value in ['overview', 'assets', 'history'] as ModelTab[]"
            :key="value"
            type="button"
            :class="{ active: tab === value }"
            @click="tab = value"
          >
            {{
              {
                overview: "运行概览",
                assets: "资产与版本",
                history: "安装记录",
              }[value]
            }}
          </button>
        </nav>
        <div class="model-detail-scroll">
          <template v-if="tab === 'overview'"
            ><section class="model-runtime-summary">
              <header>
                <span class="model-runtime-mark"
                  ><AppIcon name="services" :size="18"
                /></span>
                <div>
                  <small>当前运行状态</small
                  ><b>{{ serviceStateLabel(deployment) }}</b>
                  <p>{{ serviceStateHint(deployment) }}</p>
                </div>
                <StateBadge
                  :label="stateLabel(selected).label"
                  :tone="stateLabel(selected).tone"
                />
              </header>
              <dl>
                <div>
                  <dt><AppIcon name="hardware" :size="14" />模型显存</dt>
                  <dd>{{ modelRuntimeLabel(deployment) }}</dd>
                  <small
                    >{{
                      residencyLabels[
                        deployment?.instance_settings?.residency
                      ] ?? "未配置"
                    }}策略</small
                  >
                </div>
                <div>
                  <dt><AppIcon name="models" :size="14" />运行容器</dt>
                  <dd>
                    {{
                      containerLabels[deployment?.container_state] ??
                      deployment?.container_state ??
                      "未同步"
                    }}
                  </dd>
                  <small>{{
                    deployment?.container_state === "running"
                      ? "Worker 在线"
                      : "当前不接任务"
                  }}</small>
                </div>
                <div>
                  <dt><AppIcon name="settings" :size="14" />配置状态</dt>
                  <dd>
                    {{
                      configurationLabels[deployment?.configuration_state] ??
                      deployment?.configuration_state ??
                      "未同步"
                    }}
                  </dd>
                  <small
                    >策略版本
                    {{
                      deployment?.instance_settings?.policy_version ?? "—"
                    }}</small
                  >
                </div>
              </dl>
            </section>
            <section class="deployment-facts">
              <header>
                <h2>当前部署</h2>
                <code>{{ deployment?.id ?? "实例未同步" }}</code>
              </header>
              <div>
                <span
                  ><small>计算位置</small
                  ><b>{{
                    deployment?.instance_settings?.gpu_uuids
                      ?.map((v: string) => short(v, 4))
                      .join(" · ") || "等待配置 GPU"
                  }}</b></span
                ><span
                  ><small>资源方式</small
                  ><b>{{
                    deployment?.instance_settings?.sharing_mode === "exclusive"
                      ? "独占 GPU"
                      : "共享 GPU"
                  }}</b></span
                ><span
                  ><small>模型预算</small
                  ><b
                    >{{
                      Math.round(
                        Number(deployment?.instance_settings?.base_mib ?? 0) /
                          1024,
                      )
                    }}
                    +
                    {{
                      Math.round(
                        Number(deployment?.instance_settings?.task_mib ?? 0) /
                          1024,
                      )
                    }}
                    GiB</b
                  ></span
                ><span
                  ><small>外部预留</small
                  ><b
                    >{{
                      Math.round(
                        Number(
                          deployment?.instance_settings?.external_reserve_mib ??
                            0,
                        ) / 1024,
                      )
                    }}
                    GiB</b
                  ></span
                ><span
                  ><small>运行环境</small
                  ><b>{{
                    selected.runtime_description || "固定容器制品"
                  }}</b></span
                ><span
                  ><small>绑定资产</small
                  ><b>{{
                    selected.assets[0]?.display_name || "未同步"
                  }}</b></span
                >
              </div>
            </section>
            <p
              v-if="deployment?.runtime_last_error || deployment?.last_error"
              class="runtime-error"
            >
              <b>{{
                runtimeErrorLabel(
                  deployment.runtime_last_error || deployment.last_error,
                )
              }}</b
              ><span>{{
                runtimeErrorHint(
                  deployment.runtime_last_error || deployment.last_error,
                )
              }}</span>
            </p></template
          >
          <template v-else-if="tab === 'assets'"
            ><div class="bound-assets">
              <article v-for="asset in selected.assets" :key="asset.id">
                <AppIcon name="models" />
                <div>
                  <b>{{ asset.display_name }}</b
                  ><small
                    >{{ asset.format }} · {{ asset.role }} ·
                    {{ humanBytes(asset.total_bytes) }}</small
                  ><code>{{ short(asset.revision) }}</code>
                </div>
                <StateBadge
                  :label="asset.state"
                  :tone="asset.state === 'ready' ? 'ready' : 'neutral'"
                />
              </article>
              <p v-if="!selected.assets.length" class="empty-state">
                绑定资产尚未同步。
              </p>
            </div></template
          >
          <template v-else
            ><div class="installation-history">
              <article v-for="item in selected.installations" :key="item.id">
                <header>
                  <StateBadge
                    :label="installationLabels[item.state] ?? item.state"
                    :tone="
                      item.state === 'failed'
                        ? 'error'
                        : activeInstallationStates.has(item.state)
                          ? 'busy'
                          : item.state === 'ready'
                            ? 'ready'
                            : 'neutral'
                    "
                  /><time>{{ formatDate(item.updated_at) }}</time>
                </header>
                <code>{{ item.id }}</code
                ><i
                  ><em
                    :style="{
                      width: `${Math.round(Number(item.progress ?? 0) * 100)}%`,
                    }"
                /></i>
              </article>
              <p v-if="!selected.installations.length" class="empty-state">
                暂无安装记录。
              </p>
            </div></template
          >
        </div>
      </main>
      <Transition name="inspector"
        ><aside v-if="deployment && settingsOpen" class="model-settings">
          <header>
            <div>
              <small>INSTANCE SETTINGS</small>
              <h2>{{ deployment.label || selected?.label }}</h2>
              <code>{{ deployment.id }}</code>
            </div>
            <button
              type="button"
              class="model-settings-close"
              aria-label="关闭实例设置"
              @click="settingsOpen = false"
            >
              <AppIcon name="close" :size="15" />
            </button>
          </header>
          <div class="settings-scroll">
            <p v-if="settingsStale && !settingsSubmitted" class="configuration-notice" role="status">服务器配置已变化，当前编辑内容未被覆盖。</p>
            <section v-if="currentSettingsOperation" class="configuration-progress" role="status">
              <b>{{ currentSettingsOperation.state === 'ready' ? '配置已生效' : (deploymentOperationLabels[currentSettingsOperation.state] ?? currentSettingsOperation.state) }}</b>
              <p v-if="currentSettingsOperation.error_message">{{ currentSettingsOperation.error_message }}</p>
              <button type="button" class="link-button" @click="store.toggleStatusCenter('task')">查看后台进度</button>
            </section>
            <fieldset class="settings-controls" :disabled="settingsLocked">
            <section v-if="userSettings" class="configuration-model-fields" aria-label="模型运行配置">
              <header><b>模型与运行环境</b><small>当前 r{{ settingsSnapshot?.configuration_binding?.config_revision ?? '—' }}</small></header>
              <label class="settings-field">基础模型<select v-model="settings.base_asset_id" @change="markDirty">
                <option v-if="settings.base_asset_id && !settingsBaseAssets.some(item => item.id === settings.base_asset_id)" :value="settings.base_asset_id" disabled>当前绑定资产未同步 · {{ settings.base_asset_id }}</option>
                <option v-for="asset in settingsBaseAssets" :key="asset.id" :value="asset.id" :disabled="asset.state !== 'ready'">{{ asset.display_name || asset.id }}{{ asset.state === 'ready' ? '' : ' · 未就绪' }}</option>
              </select></label>
              <label class="settings-field">运行环境<select v-model="settings.runtime_profile_key" @change="markDirty">
                <option v-if="!settingsProfile" :value="settings.runtime_profile_key" disabled>当前运行环境不可用 · {{ settings.runtime_profile_key }}</option>
                <option v-for="profile in store.runtimeProfiles" :key="`${profile.profile_id}/${profile.revision}`" :value="`${profile.profile_id}/${profile.revision}`" :disabled="Boolean(profileDisabledReason(profile))">{{ profile.label }} · r{{ profile.revision }}{{ profileDisabledReason(profile) ? ' · 不兼容' : '' }}</option>
              </select></label>
              <label class="settings-field">VAE<select v-model="settings.vae_asset_id" @change="markDirty">
                <option value="">使用模型内置 VAE</option>
                <option v-if="settings.vae_asset_id && !settingsVaeAssets.some(item => item.id === settings.vae_asset_id)" :value="settings.vae_asset_id" disabled>当前绑定 VAE 未同步 · {{ settings.vae_asset_id }}</option>
                <option v-for="asset in settingsVaeAssets" :key="asset.id" :value="asset.id" :disabled="asset.state !== 'ready' || !settingsProfile?.optional_deployment_roles.includes('vae')">{{ asset.display_name || asset.id }}{{ asset.state === 'ready' ? '' : ' · 未就绪' }}</option>
              </select></label>
              <label class="settings-field">显存预算<input v-model.number="settings.required_vram_mib" type="number" :min="settingsProfile?.required_vram_mib ?? 1" max="196608" step="1024" @input="markDirty" /><span>MiB · 最低 {{ settingsProfile?.required_vram_mib ?? '—' }}</span></label>
            </section>
            <fieldset>
              <legend>部署 GPU</legend>
              <label v-for="gpu in gpus" :key="gpu.uuid" class="gpu-choice"
                ><input
                  v-model="settings.gpu_uuids"
                  type="checkbox"
                  :value="gpu.uuid"
                  @change="markDirty"
                /><span
                  ><b>GPU {{ gpu.index }} · {{ gpu.name }}</b
                  ><small
                    >{{
                      Math.round(Number(gpu.memory_free_mib ?? 0) / 1024)
                    }}
                    GiB 空闲 · {{ short(gpu.uuid, 6) }}</small
                  ></span
                ></label
              >
              <p v-if="!gpus.length">服务器未提供可分配 GPU</p>
            </fieldset>
            <label class="settings-field"
              >资源方式<select
                v-model="settings.sharing_mode"
                @change="markDirty"
              >
                <option value="shared">共享</option>
                <option value="exclusive">独占</option>
              </select></label
            ><label class="settings-field"
              >外部显存预留<select
                v-model.number="settings.external_reserve_mib"
                @change="markDirty"
              >
                <option
                  v-for="value in [
                    2048, 4096, 8192, 12288, 16384, 24576, 32768,
                  ]"
                  :key="value"
                  :value="value"
                >
                  {{ value / 1024 }} GiB
                </option>
              </select></label
            >
            <fieldset class="residency-field">
              <legend>显存策略</legend>
              <div
                class="residency-options"
                role="radiogroup"
                aria-label="显存策略"
              >
                <label
                  v-for="item in residencyOptions"
                  :key="item.value"
                  class="residency-choice"
                  :class="{ disabled: !residencySupported(item.value) }"
                  ><input
                    v-model="settings.residency"
                    type="radio"
                    name="residency"
                    :value="item.value"
                    :disabled="!residencySupported(item.value)"
                    @change="markDirty"
                  /><span
                    ><b>{{ item.label }}</b
                    ><small>{{
                      residencySupported(item.value)
                        ? item.detail
                        : "当前运行器不支持"
                    }}</small></span
                  ></label
                >
              </div>
            </fieldset>
            <p
              v-if="!residencySupported(settings.residency)"
              class="runtime-error"
            >
              当前配置来自旧语义；请选择按需后保存。
            </p>
            <label class="settings-field"
              >保温时长<input
                v-model.number="settings.idle_minutes"
                type="number"
                min="1"
                max="1440"
                :disabled="settings.residency !== 'idle'"
                @input="markDirty"
              /><span>分钟</span></label
            ><label class="check-field"
              ><input
                v-model="settings.restart_recovery"
                type="checkbox"
                @change="markDirty"
              />容器异常后自动恢复</label
            >
            <section v-if="userSettings" class="configuration-confirmations">
              <p>许可：{{ settingsBaseAssets.find(item => item.id === settings.base_asset_id)?.license_declared || 'unknown' }}<template v-if="settings.vae_asset_id"> · VAE {{ settingsVaeAssets.find(item => item.id === settings.vae_asset_id)?.license_declared || 'unknown' }}</template></p>
              <label class="check-field"><input v-model="settings.license_accepted" type="checkbox" @change="markDirty" />我已核实所选资产的使用许可；未知不代表获准。</label>
              <label class="check-field"><input v-model="settings.experimental_compatibility_accepted" type="checkbox" @change="markDirty" />允许未经实测的实验性 VAE 组合</label>
            </section>
            </fieldset>
            <section class="apply-summary">
              <b>{{
                settingsPlan?.effects.updates_existing ? (settingsPlan.effects.requires_restart ? '需安全重启容器' : '无需重启容器') : settingsDirty
                  ? "待提交变更"
                  : (configurationLabels[deployment.configuration_state] ??
                    deployment.configuration_state)
              }}</b>
              <p>
                {{ userSettings ? '保留模型文件与当前启停状态；新配置健康检查通过后生效，失败回退。' : '显存策略可热更新；GPU、共享方式等结构设置需要安全重启容器。' }}
              </p>
              <p v-if="settingsPlan && !settingsPlan.capacity.schedulable" class="configuration-error">当前配置不可调度：{{ runtimeErrorLabel(settingsPlan.capacity.reason) }}</p>
              <p v-if="settingsPlan?.compatibility">VAE 兼容结论：{{ settingsPlan.compatibility.verdict }}</p>
            </section>
            <p v-if="settingsBlock" class="configuration-notice" role="status">{{ settingsBlock }}</p>
          </div>
          <footer>
            <button
              type="button"
              class="flat-button"
              :disabled="settingsBusy || settingsApplying || (!settingsDirty && !settingsStale && !settingsSubmitted)"
              @click="resetSettings"
            >
              {{ settingsStale || settingsSubmitted ? '读取最新配置' : '还原' }}</button
            ><ActionButton v-if="userSettings && !settingsPlan"
              action-key="deployment:plan" icon="search" label="检查变更" tone="primary"
              :disabled="!settingsDirty || Boolean(settingsBlock) || settingsBusy" @click="previewSettings" />
            <ActionButton v-else
              :action-key="userSettings ? `deployment:create:${deployment.id}` : `deployment:${deployment.id}:save`"
              icon="save"
              :label="userSettings ? '应用配置' : '保存设置'"
              tone="primary"
              :disabled="
                !settingsDirty || Boolean(settingsBlock) || settingsBusy || (userSettings && !settingsPlan?.capacity.schedulable)
              "
              @click="saveSettings"
            />
          </footer></aside
      ></Transition>
      <div v-if="!selected" class="empty-state model-empty">
        还没有已安装的模型服务。打开安装中心开始部署。
      </div>
    </div>
    <div v-else class="asset-library">
      <header>
        <div>
          <small>PHYSICAL MODEL ASSETS</small>
          <h1>模型物理资源</h1>
          <p>资产与服务配置分开管理；归档不会操作运行容器。</p>
        </div>
        <span
          ><ActionButton
            icon="download"
            label="受控下载"
            compact
            @click="downloadOpen = true" /><ActionButton
            icon="package"
            label="导入并部署"
            tone="primary"
            compact
            @click="importWizardOpen = true"
        /></span>
      </header>
      <div class="asset-grid">
        <article
          v-for="asset in store.assets.filter((a) =>
            `${a.display_name} ${a.id}`
              .toLowerCase()
              .includes(query.toLowerCase()),
          )"
          :key="asset.id"
          class="surface"
        >
          <header>
            <span
              ><AppIcon name="models" /><b>{{ asset.display_name }}</b></span
            ><StateBadge
              :label="asset.state"
              :tone="
                asset.state === 'ready'
                  ? 'ready'
                  : asset.state === 'failed'
                    ? 'error'
                    : 'neutral'
              "
            />
          </header>
          <dl>
            <div>
              <dt>分类</dt>
              <dd>
                {{ mediaLabels[asset.media_kind] ?? asset.media_kind }} ·
                {{ asset.role }}
              </dd>
            </div>
            <div>
              <dt>格式</dt>
              <dd>{{ asset.format }}</dd>
            </div>
            <div>
              <dt>大小</dt>
              <dd>{{ humanBytes(asset.total_bytes) }}</dd>
            </div>
            <div>
              <dt>版本</dt>
              <dd>{{ short(asset.revision) }}</dd>
            </div>
          </dl>
          <footer>
            <code>{{ asset.id }}</code
            ><button
              type="button"
              class="link-button"
              @click="
                assetAction(
                  asset,
                  asset.state === 'archived' ? 'restore' : 'archive',
                )
              "
            >
              {{ asset.state === "archived" ? "恢复" : "归档" }}
            </button>
          </footer>
        </article>
      </div>
      <section class="surface transfer-section">
        <header>
          <h2>传输任务</h2>
          <small>切换页面不影响服务器后台任务</small>
        </header>
        <article v-for="item in store.transfers" :key="item.id">
          <span
            ><b>{{ item.display_name }}</b
            ><small>{{ item.current_file || item.direction }}</small></span
          ><StateBadge
            :label="item.state"
            :tone="
              ['queued', 'transferring', 'verifying'].includes(item.state)
                ? 'busy'
                : item.state === 'failed'
                  ? 'error'
                  : 'neutral'
            "
          /><em
            >{{ humanBytes(item.received_bytes) }} /
            {{ humanBytes(item.expected_bytes) }}</em
          >
        </article>
        <p v-if="!store.transfers.length" class="empty-state">暂无传输任务</p>
      </section>
    </div>

    <Teleport to="body"
      ><Transition name="dialog"
        ><div
          v-if="installOpen"
          class="dialog-backdrop"
          @pointerdown.self="installOpen = false"
        >
          <section class="install-center">
            <header>
              <div>
                <small>ONE-CLICK DEPLOYMENT</small>
                <h1>安装中心</h1>
              </div>
              <button type="button" @click="installOpen = false">
                <AppIcon name="close" />
              </button>
            </header>
            <div class="install-toolbar">
              <div>
                <button
                  v-for="value in [
                    'all',
                    'installed',
                    'available',
                  ] as InstallFilter[]"
                  :key="value"
                  type="button"
                  :class="{ active: installFilter === value }"
                  @click="installFilter = value"
                >
                  {{
                    value === "all"
                      ? "全部"
                      : value === "installed"
                        ? "已安装"
                        : "未安装"
                  }}
                </button>
              </div>
              <label
                ><AppIcon name="search" :size="14" /><input
                  v-model="query"
                  placeholder="搜索安装配方" /></label
              ><span
                ><ActionButton
                  icon="download"
                  title="受控下载资产"
                  compact
                  @click="downloadOpen = true" /><ActionButton
                  icon="package"
                  title="导入并部署本地模型"
                  compact
                  @click="importWizardOpen = true"
              /></span>
            </div>
            <div class="install-body">
              <aside>
                <nav>
                  <button
                    v-for="value in [
                      'all',
                      'image',
                      'video',
                      'speech',
                      'music',
                    ] as Category[]"
                    :key="value"
                    type="button"
                    :class="{ active: category === value }"
                    @click="category = value"
                  >
                    {{ value === "all" ? "全部" : mediaLabels[value] }}
                  </button>
                </nav>
                <div>
                  <button
                    v-for="item in installModels"
                    :key="item.key"
                    type="button"
                    :class="{ active: installSelected?.key === item.key }"
                    @click="installSelectedKey = item.key"
                  >
                    <span class="model-kind"
                      ><AppIcon :name="(item.kind || 'models') as any" /></span
                    ><span
                      ><b>{{ item.label }}</b
                      ><small
                        >{{
                          item.required_vram_mib
                            ? `${Math.round(item.required_vram_mib / 1024)} GiB 显存`
                            : "显存未提供"
                        }}
                        · {{ item.file_count ?? "—" }} 个文件</small
                      ></span
                    ><StateBadge
                      :label="stateLabel(item).label"
                      :tone="stateLabel(item).tone"
                    />
                  </button>
                  <p v-if="!installModels.length" class="empty-state">
                    没有符合条件的配方
                  </p>
                </div>
              </aside>
              <main v-if="installSelected">
                <header>
                  <span class="model-hero"
                    ><AppIcon
                      :name="(installSelected.kind || 'models') as any"
                      :size="25"
                  /></span>
                  <div>
                    <small>{{ mediaLabels[installSelected.kind] }}模型</small>
                    <h2>{{ installSelected.label }}</h2>
                    <p>
                      {{ installSelected.model_id }} ·
                      {{ short(installSelected.revision) }}
                    </p>
                  </div>
                  <StateBadge
                    :label="stateLabel(installSelected).label"
                    :tone="stateLabel(installSelected).tone"
                  />
                </header>
                <div class="install-detail-scroll">
                  <p>
                    {{
                      installSelected.description ||
                      "由服务器固定配方安装运行环境、模型资产和服务实例。"
                    }}
                  </p>
                  <div class="install-metrics">
                    <span
                      ><small>模型制品</small
                      ><b>{{ humanBytes(installSelected.download_bytes) }}</b
                      ><em
                        >{{ installSelected.file_count ?? "—" }} 个固定文件</em
                      ></span
                    ><span
                      ><small>显存要求</small
                      ><b>{{
                        installSelected.required_vram_mib
                          ? `${Math.round(installSelected.required_vram_mib / 1024)} GiB`
                          : "未提供"
                      }}</b
                      ><em
                        >{{ installSelected.min_gpus ?? 1 }}–{{
                          installSelected.max_gpus ??
                          installSelected.min_gpus ??
                          1
                        }}
                        张 GPU</em
                      ></span
                    ><span
                      ><small>许可证</small><b>{{ installSelected.license }}</b
                      ><em>安装前明确接受</em></span
                    >
                  </div>
                  <div class="install-composition">
                    <span
                      ><small>运行框架</small
                      ><b>{{
                        installSelected.runtime_description || "固定容器制品"
                      }}</b></span
                    ><span
                      ><small>模型资产</small
                      ><b>{{
                        isInstalled(installSelected)
                          ? "已校验"
                          : installSelected.retained_deployment_id
                            ? "可直接复用"
                            : "安装时准备"
                      }}</b></span
                    ><span
                      ><small>服务实例</small
                      ><b>{{
                        installSelected.deployment_id ||
                        installSelected.retained_deployment_id ||
                        "安装后创建"
                      }}</b></span
                    >
                  </div>
                  <section v-if="activeInstall" class="install-progress">
                    <header>
                      <StateBadge
                        :label="
                          installationLabels[activeInstall.state] ??
                          activeInstall.state
                        "
                        :tone="
                          activeInstallationStates.has(activeInstall.state)
                            ? 'busy'
                            : activeInstall.state === 'failed'
                              ? 'error'
                              : 'neutral'
                        "
                      /><span
                        >{{
                          Math.round(Number(activeInstall.progress ?? 0) * 100)
                        }}%</span
                      >
                    </header>
                    <i
                      ><em
                        :style="{
                          width: `${Math.round(Number(activeInstall.progress ?? 0) * 100)}%`,
                        }"
                    /></i>
                    <ol>
                      <li
                        v-for="step in activeInstall.steps ?? []"
                        :key="step.key || step.label"
                        :class="step.state"
                      >
                        {{ step.label }}
                      </li>
                    </ol>
                    <footer>
                      <ActionButton
                        v-for="action in installationActions(activeInstall)"
                        :key="action"
                        :action-key="`installation:${activeInstall.id}:${action}`"
                        :label="
                          (
                            {
                              pause: '暂停',
                              resume: '继续',
                              cancel: '取消',
                              retry: '重试',
                            } as any
                          )[action]
                        "
                        compact
                        @click="
                          store.controlInstallation(activeInstall, action)
                        "
                      />
                    </footer>
                  </section>
                </div>
              </main>
              <aside v-if="installSelected" class="install-settings">
                <template v-if="isInstalled(installSelected)"
                  ><header>
                    <small>INSTALLED SERVICE</small>
                    <h2>{{ installSelected.label }}</h2>
                  </header>
                  <div class="settings-scroll">
                    <p class="install-ready">服务、环境与模型资产已经绑定。</p>
                    <p v-if="installDeployment?.removal_operation_state === 'failed'" class="runtime-error">
                      {{ installDeployment.removal_error || '容器移除尚未确认；实例保持锁定，可重试卸载。' }}
                    </p>
                    <p>
                      卸载只移除服务配置与运行容器；模型权重、镜像缓存和安装历史保留。
                    </p>
                    <p
                      v-if="!canUninstall(installDeployment)"
                      class="runtime-error"
                    >
                      {{ installDeployment?.removal_operation_id ? '卸载正在后台进行，实例已锁定。' : '请先停止服务，并等待任务与运行容器退出。' }}
                    </p>
                  </div>
                  <footer>
                    <ActionButton
                      :action-key="`uninstall:${installSelected.key}`"
                      icon="trash"
                      :label="installDeployment?.removal_operation_state === 'failed' ? '重试卸载' : '卸载服务'"
                      tone="danger"
                      :disabled="!canUninstall(installDeployment)"
                      @click="uninstall(installSelected)"
                    /></footer></template
                ><template v-else
                  ><header>
                    <small>INSTALL SETTINGS</small>
                    <h2>安装配置</h2>
                  </header>
                  <div class="settings-scroll">
                    <fieldset>
                      <legend>部署 GPU</legend>
                      <label
                        v-for="gpu in gpus"
                        :key="gpu.uuid"
                        class="gpu-choice"
                        ><input
                          v-model="currentInstallDraft.gpus"
                          type="checkbox"
                          :value="gpu.index"
                        /><span
                          ><b>GPU {{ gpu.index }} · {{ gpu.name }}</b
                          ><small
                            >{{
                              Math.round(
                                Number(gpu.memory_free_mib ?? 0) / 1024,
                              )
                            }}
                            GiB 空闲</small
                          ></span
                        ></label
                      >
                    </fieldset>
                    <section
                      v-if="installSelected.authentication"
                      class="source-auth"
                    >
                      <template v-if="installSelected.authentication.configured"
                        ><StateBadge
                          label="来源授权已就绪"
                          tone="ready"
                        /><button
                          type="button"
                          class="link-button"
                          @click="clearAuthorization(installSelected)"
                        >
                          清除授权
                        </button></template
                      ><template v-else
                        ><b>授权读取官方模型</b
                        ><button
                          type="button"
                          class="link-button"
                          @click="
                            desktopBridge().openSourceTerms(
                              installSelected.authentication.terms_url,
                            )
                          "
                        >
                          打开许可页面</button
                        ><input
                          v-model="currentInstallDraft.token"
                          type="password"
                          placeholder="hf_…" /><ActionButton
                          :action-key="`source:${installSelected.authentication.provider}`"
                          label="保存并授权"
                          compact
                          :disabled="
                            !currentInstallDraft.token.trim() ||
                            !store.activeProfile?.sourceAuthorizationAllowed
                          "
                          @click="authorize(installSelected)"
                      /></template>
                    </section>
                    <label class="check-field license"
                      ><input
                        v-model="currentInstallDraft.license"
                        type="checkbox"
                      />我已阅读并接受
                      {{ installSelected.license }} 许可证</label
                    >
                    <p
                      v-if="installSelected.retained_deployment_id"
                      class="cache-note"
                    >
                      将校验并复用现有资产，不重复下载基础制品。
                    </p>
                    <p
                      v-if="installBlocked(installSelected)"
                      class="install-block"
                    >
                      {{ installBlocked(installSelected) }}
                    </p>
                  </div>
                  <footer>
                    <ActionButton
                      :action-key="`install:${installSelected.key}`"
                      icon="download"
                      label="安装模型"
                      tone="primary"
                      :disabled="Boolean(installBlocked(installSelected))"
                      @click="install(installSelected)"
                    /></footer
                ></template>
              </aside>
            </div>
          </section></div></Transition
    ></Teleport>

    <ModelImportWizard
      :open="importWizardOpen"
      @close="importWizardOpen = false"
      @completed="mode = 'services'"
    />
    <Teleport to="body"
      ><div
        v-if="downloadOpen"
        class="dialog-backdrop"
        @pointerdown.self="downloadOpen = false"
      >
        <form class="asset-dialog" @submit.prevent="downloadModel">
          <header>
            <div>
              <small>CONTROLLED DOWNLOAD</small>
              <h2>受控下载模型</h2>
            </div>
            <button type="button" @click="downloadOpen = false">
              <AppIcon name="close" />
            </button>
          </header>
          <div class="asset-form">
            <label
              >显示名称<input
                v-model.trim="download.display_name"
                required /></label
            ><label
              >HTTPS 地址<input
                v-model.trim="download.url"
                type="url"
                required /></label
            ><label
              >文件名<input v-model.trim="download.filename" required /></label
            ><label
              >固定版本<input
                v-model.trim="download.revision"
                required /></label
            ><label
              >文件大小（字节）<input
                v-model.number="download.expected_bytes"
                type="number"
                min="1"
                required /></label
            ><label
              >SHA-256<input
                v-model.trim="download.expected_sha256"
                minlength="64"
                maxlength="64"
                required /></label
            ><label
              >媒体分类<select v-model="download.media_kind">
                <option
                  v-for="value in [
                    'image',
                    'video',
                    'speech',
                    'music',
                    'general',
                  ]"
                  :key="value"
                  :value="value"
                >
                  {{ mediaLabels[value] }}
                </option>
              </select></label
            ><label
              >模型角色<select v-model="download.role">
                <option value="checkpoint">基础模型</option>
                <option value="lora">LoRA</option>
                <option value="adapter">Adapter</option>
                <option value="vae">VAE</option>
                <option value="encoder">编码器</option>
                <option value="upscaler">超分</option>
                <option value="control">条件模型</option>
              </select></label
            ><label
              >格式<select v-model="download.format">
                <option value="safetensors">Safetensors</option>
                <option value="gguf">GGUF</option>
              </select></label
            ><label
              >许可证声明<input
                v-model.trim="download.license_declared"
                required
            /></label>
          </div>
          <p>服务器会按固定大小和 SHA-256 校验下载结果。</p>
          <footer>
            <button
              type="button"
              class="flat-button"
              @click="downloadOpen = false"
            >
              取消</button
            ><ActionButton
              action-key="model:download"
              icon="download"
              label="创建下载"
              tone="primary"
              @click="downloadModel"
            />
          </footer>
        </form></div
    ></Teleport>
  </section>
</template>
