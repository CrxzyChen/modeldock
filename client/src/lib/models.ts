import type { AssetCompatibility, DeploymentOperation, ExpectedConfiguration, MediaTask, ResourceItem, RuntimeProfile } from "@/types/contracts";

// Public DeploymentOperation transitions; terminal states cannot be reopened.
const operationTransitions: Record<string, string[]> = {
  accepted: ['preparing_runtime', 'canceling', 'rollback'],
  preparing_runtime: ['creating_container', 'canceling', 'rollback'],
  creating_container: ['starting_worker', 'canceling', 'rollback'],
  starting_worker: ['loading_model', 'health_check', 'canceling', 'rollback'],
  loading_model: ['health_check', 'canceling', 'rollback'],
  health_check: ['ready', 'canceling', 'rollback'], canceling: ['canceled', 'rollback'],
  rollback: ['failed', 'canceled'], ready: [], failed: [], canceled: [],
};
function operationCanAdvance(from: string, to: string): boolean {
  return from === to || (operationTransitions[from] ?? []).some(next => operationCanAdvance(next, to));
}
export function mergeDeploymentOperations(current: DeploymentOperation[], incoming: DeploymentOperation[], focusedDeploymentId: string | null = null) {
  const items = new Map(current.map(item => [item.id, item]));
  for (const item of incoming) {
    const previous = items.get(item.id);
    if (previous && (!operationCanAdvance(previous.state, item.state) ||
      (previous.state === item.state && previous.updated_at && item.updated_at && previous.updated_at > item.updated_at))) continue;
    items.set(item.id, item);
  }
  const active = (item: DeploymentOperation) => !['ready', 'failed', 'canceled'].includes(item.state);
  const sorted = [...items.values()].sort((a, b) => Number(active(b)) - Number(active(a)) ||
    String(b.updated_at ?? b.created_at ?? '').localeCompare(String(a.updated_at ?? a.created_at ?? '')));
  const recent = sorted.slice(0, 100);
  const focused = sorted.find(item => item.deployment_id === focusedDeploymentId);
  // One open settings inspector may retain one extra result beyond the list window.
  if (focused && !recent.some(item => item.id === focused.id)) recent.push(focused);
  return recent;
}

export function deploymentSettingsDraft(value: ResourceItem | null) {
  const source = value?.instance_settings ?? {};
  const binding = value?.configuration_binding;
  return {
    gpu_uuids: [...(source.gpu_uuids ?? [])] as string[],
    sharing_mode: source.sharing_mode ?? "shared",
    external_reserve_mib: source.external_reserve_mib ?? 8192,
    residency: source.residency ?? "on_demand",
    idle_minutes: source.idle_minutes ?? 15,
    restart_recovery: source.restart_recovery === true,
    base_asset_id: binding?.base_asset_id ?? "",
    runtime_profile_key: binding ? `${binding.runtime_profile_id}/${binding.runtime_profile_revision}` : "",
    vae_asset_id: binding?.vae_asset_id ?? "",
    required_vram_mib: value?.required_vram_mib ?? 16384,
    license_accepted: false,
    experimental_compatibility_accepted: false,
  };
}

export function expectedConfiguration(value: ResourceItem | null): ExpectedConfiguration | null {
  const binding = value?.configuration_binding;
  const version = value?.instance_settings?.policy_version;
  if (!binding || !Number.isInteger(binding.config_revision) || binding.config_revision < 1 ||
      !/^[0-9a-f]{64}$/.test(binding.config_digest ?? "") || !Number.isInteger(version) || version < 1)
    return null;
  return { config_revision: binding.config_revision, config_digest: binding.config_digest, policy_version: version };
}

export function userConfigurationRequest(snapshot: ResourceItem, draft: ReturnType<typeof deploymentSettingsDraft>, profile: RuntimeProfile) {
  const expected = expectedConfiguration(snapshot);
  if (!expected) throw new Error("配置版本不可用，请重新同步实例");
  return {
    deployment_id: snapshot.id,
    expected_configuration: expected,
    base_asset_id: draft.base_asset_id,
    runtime_profile_id: profile.profile_id,
    runtime_profile_revision: profile.revision,
    vae_asset_id: draft.vae_asset_id || null,
    gpu_uuids: [...draft.gpu_uuids],
    residency: draft.residency,
    sharing_mode: draft.sharing_mode,
    required_vram_mib: draft.required_vram_mib,
    license_accepted: draft.license_accepted,
    experimental_compatibility_accepted: draft.experimental_compatibility_accepted,
    policy_options: {
      external_reserve_mib: draft.external_reserve_mib,
      idle_seconds: draft.idle_minutes * 60,
      restart_recovery: draft.restart_recovery,
    },
  };
}

export interface ImageGenerationDraft {
  prompt: string;
  options: Record<string, string | number>;
  configuration: MediaTask['configuration_binding'] | null;
  execution: MediaTask['execution_binding'] | null;
  lora: NonNullable<MediaTask['loras']>[number] | null;
}

export function imageDraftKey(profile: string, model: string) {
  return JSON.stringify([profile, model]);
}

export function sameModelIdentity(left: unknown, right: unknown): boolean {
  const normalize = (value: any): any => {
    if (Array.isArray(value)) return value.map(normalize);
    if (value && typeof value === 'object') return Object.fromEntries(
      Object.keys(value).sort().map(key => [key, normalize(value[key])]));
    return value ?? null;
  };
  return JSON.stringify(normalize(left)) === JSON.stringify(normalize(right));
}

export function loraCompatibility(records: AssetCompatibility[], asset: ResourceItem,
  base: { asset_id: string; revision: string } | null) {
  if (!base || asset.state !== 'ready') return null;
  return records.find(item => item.subject_asset_id === asset.id &&
    item.subject_revision === asset.revision && item.base_asset_id === base.asset_id &&
    item.base_revision === base.revision && item.detector_version === 'mc-sdxl-2') ?? null;
}

export const mediaLabels: Record<string, string> = {
  image: "图片",
  video: "视频",
  speech: "语音",
  music: "音乐",
  general: "通用",
};
export const serviceLabels: Record<string, string> = {
  installing: "安装中",
  configuring: "应用配置中",
  stopping: "正在停止容器",
  stopped: "容器已停止",
  starting: "正在启动容器",
  running: "容器在线",
  ready: "容器在线",
  error: "服务异常",
};
export const runtimeLabels: Record<string, string> = {
  unloaded: "模型未载入显存",
  waiting_runtime: "等待容器就绪",
  loading: "正在载入显存",
  loaded: "显存已就绪",
  unloading: "正在释放显存",
  online_unloaded: "模型未载入显存",
  stopping: "正在释放显存",
  error: "运行异常",
};
export const containerLabels: Record<string, string> = {
  configured: "已配置",
  created: "已创建",
  starting: "启动中",
  running: "运行中",
  stopping: "停止中",
  stopped: "已停止",
  removed: "已移除",
  error: "异常",
};
export const configurationLabels: Record<string, string> = {
  applied: "配置已生效",
  restart_pending: "等待重启生效",
  replace_pending: "等待替换容器",
  applying: "正在应用配置",
  failed: "配置应用失败",
};
export const installationLabels: Record<string, string> = {
  available: "未安装",
  unavailable: "暂不可用",
  installed: "已安装",
  ready: "安装完成",
  preflight: "预检中",
  downloading: "下载中",
  paused: "已暂停",
  verifying: "校验中",
  preparing: "准备环境",
  checking: "健康检查",
  failed: "安装失败",
  canceled: "已取消",
};
export const residencyLabels: Record<string, string> = {
  on_demand: "按需",
  idle: "保温",
  resident: "常驻",
};
export const deploymentOperationLabels: Record<string, string> = {
  accepted: "已接收", preparing_runtime: "准备 Runtime", creating_container: "创建容器",
  starting_worker: "启动 Worker", loading_model: "加载模型", health_check: "健康检查",
  ready: "部署完成", canceling: "正在取消", rollback: "正在回滚", canceled: "已取消", failed: "部署失败",
};
export const runtimeErrorLabels: Record<string, string> = {
  gpu_capacity_unavailable: "GPU 容量不足",
  gpu_unavailable: "GPU 不可用",
  residency_mode_unsupported: "当前运行器不支持该显存策略",
  runtime_artifact_unavailable: "运行环境不可用",
  container_start_failed: "容器启动失败",
};
export const activeInstallationStates = new Set([
  "preflight",
  "downloading",
  "verifying",
  "preparing",
  "checking",
]);
const activeRuntimeStates = new Set([
  "loaded",
  "loading",
  "unloading",
  "stopping",
]);
const startedServiceStates = new Set(["ready", "starting", "running"]);

export interface CatalogModel extends ResourceItem {
  key: string;
  deployments: ResourceItem[];
  installations: ResourceItem[];
  assets: ResourceItem[];
}

export function isUserDeployment(item: ResourceItem) {
  return String(item.model_id ?? "").startsWith("user/") || Number.isInteger(item.current_config_revision);
}

export function buildCatalogModels(
  recipes: ResourceItem[],
  deployments: ResourceItem[],
  installations: ResourceItem[],
  assets: ResourceItem[],
): CatalogModel[] {
  const catalog = recipes.map((recipe) => {
    const key = String(
      recipe.recipe_key || recipe.catalog_key || recipe.key || recipe.id,
    );
    const linked = deployments.filter(
      (item) =>
        !isUserDeployment(item) &&
        (item.catalog_key === key ||
          item.recipe_key === key ||
          item.model_id === recipe.model_id),
    );
    const assetIds = new Set(
      linked.map((item) => item.asset_id).filter(Boolean),
    );
    const history = installations
      .filter((item) => item.recipe_key === key)
      .sort((a, b) =>
        String(b.updated_at || "").localeCompare(String(a.updated_at || "")),
      );
    return {
      ...recipe,
      key,
      deployments: linked,
      installations: history,
      assets: assets.filter((item) => assetIds.has(item.id)),
    } as CatalogModel;
  });
  const imported = deployments.filter(item => isUserDeployment(item) && item.install_state === "ready").map((deployment) => {
    const assetIds = new Set(
      [deployment.asset_id, deployment.vae_asset_id].filter(Boolean),
    );
    return {
      id: deployment.id,
      key: `deployment:${deployment.id}`,
      recipe_key: deployment.catalog_key,
      catalog_key: deployment.catalog_key,
      state: "installed",
      kind: deployment.kind || "image",
      label: deployment.label || deployment.model_id || deployment.id,
      model_id: deployment.model_id,
      revision: deployment.revision,
      license: deployment.license,
      required_vram_mib: deployment.required_vram_mib,
      runtime_description:
        deployment.runtime_profile_label || "用户模型 Runtime",
      deployments: [deployment],
      installations: [],
      assets: assets.filter((item) => assetIds.has(item.id)),
      user_imported: true,
    } as CatalogModel;
  });
  return [...imported, ...catalog];
}

export function deploymentFor(
  item: CatalogModel | null,
  selectedId?: string | null,
) {
  return (
    item?.deployments.find((value) => value.id === selectedId) ??
    item?.deployments.find(
      (value) =>
        value.accepting_tasks || startedServiceStates.has(value.service_state),
    ) ??
    item?.deployments[0] ??
    null
  );
}

export function isInstalled(item: CatalogModel) {
  return (
    item.state === "installed" &&
    item.deployments.some((value) => value.install_state === "ready")
  );
}
export function isRunning(item: CatalogModel) {
  return item.deployments.some(
    (value) =>
      value.accepting_tasks || startedServiceStates.has(value.service_state),
  );
}
export function canUninstall(deployment: ResourceItem | null) {
  if (deployment?.removal_operation_id) return deployment.removal_operation_state === "failed";
  return Boolean(
    deployment &&
      deployment.service_state === "stopped" &&
      deployment.configuration_state === "applied" &&
      !activeRuntimeStates.has(deployment.actual_state),
  );
}
export function runtimeErrorLabel(value: unknown) {
  const key = String(value ?? "");
  return runtimeErrorLabels[key] ?? (key.replaceAll("_", " ") || "运行异常");
}
export function runtimeErrorHint(value: unknown) {
  if (value === "gpu_capacity_unavailable")
    return "容器服务在线，但没有足够的可调度显存加载模型；任务会继续排队。";
  if (value === "gpu_unavailable")
    return "已配置的 GPU 当前不可用于 MediaCenter 调度。";
  if (value === "residency_mode_unsupported")
    return "该模型当前采用权重流式卸载运行，只能使用按需策略；切换后容器无需重建。";
  if (value === "runtime_artifact_unavailable")
    return "对应的运行环境制品尚未就绪。";
  if (value === "container_start_failed")
    return "运行容器启动失败，请检查最近一次运行日志。";
  return String(value ?? "运行域出现异常，请检查部署状态。");
}
export function modelState(item: CatalogModel): {
  label: string;
  tone: "ready" | "busy" | "error" | "neutral" | "warn";
} {
  const removing = item.deployments.find(value => value.removal_operation_id);
  if (removing) return removing.removal_operation_state === 'failed'
    ? { label: '卸载未完成', tone: 'error' } : { label: '正在卸载', tone: 'busy' };
  const task = item.installations[0];
  if (task && activeInstallationStates.has(task.state))
    return { label: installationLabels[task.state], tone: "busy" };
  if (isInstalled(item)) {
    const failed = item.deployments.find(
      (value) =>
        value.service_state === "error" ||
        value.configuration_state === "failed" ||
        value.runtime_last_error ||
        value.last_error,
    );
    if (failed)
      return {
        label: runtimeErrorLabel(
          failed.runtime_last_error ||
            failed.last_error ||
            failed.service_state,
        ),
        tone: "error",
      };
    const service =
      item.deployments.find(
        (value) =>
          value.accepting_tasks ||
          startedServiceStates.has(value.service_state),
      ) ?? item.deployments[0];
    if (
      ["configuring", "stopping", "starting"].includes(service?.service_state)
    )
      return { label: serviceLabels[service.service_state], tone: "busy" };
    if (service?.accepting_tasks)
      return {
        label: serviceLabels[service.service_state] || "可接任务",
        tone: "ready",
      };
    return {
      label: serviceLabels[service?.service_state] || "已停止",
      tone: "neutral",
    };
  }
  if (task?.state === "failed") return { label: "安装失败", tone: "error" };
  return {
    label: installationLabels[item.state] || "未安装",
    tone: item.state === "unavailable" ? "warn" : "neutral",
  };
}

export function installationActions(
  task: ResourceItem | null,
): Array<"pause" | "resume" | "cancel" | "retry"> {
  if (!task) return [];
  if (task.state === "paused") return ["resume", "cancel"];
  if (task.state === "failed") return ["retry"];
  if (activeInstallationStates.has(task.state))
    return task.state === "downloading" && task.transfer_id
      ? ["pause", "cancel"]
      : ["cancel"];
  return [];
}
