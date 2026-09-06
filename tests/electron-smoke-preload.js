"use strict";

const { contextBridge } = require("electron");
const refreshCallbacks = [];
const eventCallbacks = [];
let removalOperation = null;
let recoveryTask = null;
let recoveryVersion = 1;

const profile = {
  id: "smoke-server",
  name: "Electron Smoke Server",
  baseUrl: "http://10.0.0.10:8787",
  hasCredential: true,
  sourceAuthorizationAllowed: true,
  allowInsecureSourceAuthorization: true
};

const imageModels = [
  { model_key: "sdxl-smoke", model_id: "smoke/sdxl", label: "SDXL Smoke", healthy: true, gpu_indices: [0], revision: "test", capabilities: { workflow: "generate", prompt_label: "画面描述", options: [{ key: "width", label: "宽", type: "integer", default: 1024, minimum: 512, maximum: 2048 }, { key: "height", label: "高", type: "integer", default: 1024, minimum: 512, maximum: 2048 }], size_presets: [{ label: "方形", width: 1024, height: 1024 }] } },
  { model_key: "upscale-smoke", model_id: "smoke/upscale", label: "AI 超分", healthy: true, gpu_indices: [0], revision: "test", capabilities: { workflow: "upscale", options: [] } }
];
const services = [
  { kind: "image", name: "图片生成", enabled: true, available: true, timeout_seconds: 600, default_model: "sdxl-smoke", models: imageModels },
  { kind: "video", name: "视频生成", enabled: true, available: true, timeout_seconds: 3600, default_model: "video-smoke", models: [{ model_key: "video-smoke", model_id: "smoke/video", label: "Video Smoke", healthy: true, gpu_indices: [0], revision: "test", capabilities: { input_contract: { minimum: 0, maximum: 0, accept: [] }, options: [{ key: "num_frames", label: "帧数", type: "integer", default: 49, minimum: 9, maximum: 97 }, { key: "fps", label: "帧率", type: "integer", default: 24, minimum: 8, maximum: 30 }] } }] },
  { kind: "speech", name: "语音生成", enabled: true, available: true, timeout_seconds: 600, default_model: "speech-smoke", models: [{ model_key: "speech-smoke", model_id: "smoke/speech", label: "Speech Smoke", healthy: true, revision: "test", capabilities: { input_contract: { minimum: 0, maximum: 0, accept: [] }, options: [{ key: "speed", label: "语速", type: "number", default: 1, minimum: 0.5, maximum: 2 }] } }] },
  { kind: "music", name: "音乐合成", enabled: true, available: true, timeout_seconds: 600, default_model: "music-smoke", models: [{ model_key: "music-smoke", model_id: "smoke/music", label: "Music Smoke", healthy: true, revision: "test", capabilities: { input_contract: { minimum: 0, maximum: 0, accept: [] }, options: [{ key: "duration", label: "时长", type: "number", default: 8, minimum: 1, maximum: 30 }] } }] }
];
const deployment = { id: "sdxl-smoke", label: "SDXL Smoke", catalog_key: "sdxl-smoke", model_id: "smoke/sdxl", asset_id: "asset-smoke", install_state: "ready", actual_state: "loaded", service_state: "ready", desired_service_state: "started", accepting_tasks: true, configuration_state: "applied", container_state: "running", instance_settings: { policy_version: 1, gpu_uuids: ["GPU-smoke"], sharing_mode: "shared", external_reserve_mib: 8192, residency: "resident", idle_minutes: 15, restart_recovery: true } };
const catalog = { id: "sdxl-smoke", recipe_key: "sdxl-smoke", key: "sdxl-smoke", label: "SDXL Smoke", model_id: "smoke/sdxl", kind: "image", state: "installed", revision: "test", license: "test", runtime_available: true, prerequisites_ready: true, min_gpus: 1, max_gpus: 1, recommended_gpus: [0], required_vram_mib: 8192, deployment_id: deployment.id };
const blockedDeployment = { id: "upscale-smoke", label: "AI 超分", catalog_key: "upscale-smoke", model_id: "smoke/upscale", asset_id: "asset-upscale", install_state: "ready", actual_state: "waiting_runtime", service_state: "starting", desired_service_state: "started", accepting_tasks: true, configuration_state: "applied", container_state: "stopped", runtime_last_error: "gpu_capacity_unavailable", instance_settings: { policy_version: 2, gpu_uuids: ["GPU-smoke"], sharing_mode: "shared", external_reserve_mib: 8192, residency: "on_demand", residency_modes: ["on_demand"], idle_minutes: 15, restart_recovery: true } };
const blockedCatalog = { id: "upscale-smoke", recipe_key: "upscale-smoke", key: "upscale-smoke", label: "AI 超分", model_id: "smoke/upscale", kind: "image", state: "installed", revision: "test", license: "test", runtime_available: true, prerequisites_ready: true, min_gpus: 1, max_gpus: 1, recommended_gpus: [0], required_vram_mib: 6144, deployment_id: blockedDeployment.id };
const stoppedDeployment = { id: "video-stopped", label: "Video Stopped", catalog_key: "video-stopped", model_id: "smoke/video-stopped", asset_id: "asset-video", install_state: "ready", actual_state: "unloaded", service_state: "stopped", desired_service_state: "stopped", accepting_tasks: false, configuration_state: "applied", container_state: "stopped", instance_settings: { policy_version: 1, gpu_uuids: ["GPU-smoke"], sharing_mode: "shared", external_reserve_mib: 8192, residency: "on_demand", idle_minutes: 15, restart_recovery: false } };
const stoppedCatalog = { id: "video-stopped", recipe_key: "video-stopped", key: "video-stopped", label: "Video Stopped", model_id: "smoke/video-stopped", kind: "video", state: "installed", revision: "test", license: "test", runtime_available: true, prerequisites_ready: true, min_gpus: 1, max_gpus: 1, recommended_gpus: [0], required_vram_mib: 8192, deployment_id: stoppedDeployment.id };
const imageTask = { id: "image-history-smoke", service: "image", model: "sdxl-smoke", prompt: "淡蓝色控制中心", status: "succeeded", stage: "completed", progress: 1, created_at: "2026-09-03T20:00:00Z", updated_at: "2026-09-03T20:01:00Z", options: { width: 1024, height: 1024 }, output: { artifact_url: "/api/v1/artifacts/image-history-smoke.png" } };
const smokePng = Uint8Array.from(Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "base64"));
const runtimeProfile = { profile_id: "sdxl-single-file", revision: 1, label: "SDXL 单文件 Runtime", image_digest: "sha256:" + "a".repeat(64), profile_digest: "b".repeat(64), architecture_families: ["sdxl"], main_formats: ["safetensors"], optional_deployment_roles: ["vae"], task_roles: ["lora"], residency_modes: ["on_demand", "idle", "resident"], required_vram_mib: 16384 };
const userDeployment = { ...stoppedDeployment, id: 'ph8-config-smoke', label: 'Pony V6 XL', model_id: 'user/pony-smoke',
  catalog_key: 'sdxl-single-file', kind: 'image', asset_id: 'pony-smoke', required_vram_mib: 16384,
  current_config_revision: 1, pending_config_revision: null,
  configuration_binding: { deployment_id: 'ph8-config-smoke', config_revision: 1, config_digest: 'e'.repeat(64),
    runtime_profile_id: runtimeProfile.profile_id, runtime_profile_revision: 1, base_asset_id: 'pony-smoke',
    base_asset_revision: 'v6', vae_asset_id: null } };
const userAssets = [
  {id:'pony-smoke',display_name:'Pony Diffusion V6 XL',media_kind:'image',role:'checkpoint',format:'safetensors',architecture_family:'sdxl',revision:'v6',state:'ready',license_declared:'unknown'},
  {id:'vae-smoke',display_name:'SDXL VAE',media_kind:'image',role:'vae',format:'safetensors',architecture_family:'sdxl',revision:'v1',state:'ready',license_declared:'unknown'},
  {id:'vae-quarantined',display_name:'待校验 VAE',media_kind:'image',role:'vae',format:'safetensors',architecture_family:'sdxl',revision:'v1',state:'quarantined',license_declared:'unknown'}
];
imageModels[0].catalog_key = 'sdxl-single-file';
deployment.execution_binding = {model_key:'sdxl-single-file',recipe_revision:'b'.repeat(64),model_asset_id:'asset-smoke',model_asset_revision:'test',dependencies:[]};
imageTask.execution_binding = deployment.execution_binding;
const loraAssets = [
  {id:'lora-shadow',display_name:'huuoliv-shadow v1',media_kind:'image',role:'lora',format:'safetensors',architecture_family:'sdxl',revision:'v1',state:'ready'},
  {id:'lora-other',display_name:'Pony style',media_kind:'image',role:'lora',format:'safetensors',architecture_family:'sdxl',revision:'v1',state:'ready'}
];
const compatibility = loraAssets.map((asset,index)=>({subject_asset_id:asset.id,subject_revision:'v1',base_asset_id:'asset-smoke',base_revision:'test',detector_version:'mc-sdxl-2',verdict:index?'incompatible':'exact',reason_codes:index?['declared_base_identity_mismatch']:['declared_base_identity_exact'],evidence_digest:'c'.repeat(64),created_at:'2026-09-04T00:00:00Z'}));

function response(path, request) {
  const body = request?.body ? JSON.parse(request.body) : null;
  if (path.startsWith('/__smoke__/timeout/')) {
    // Renderer-only recovery fixture. This is not a real timeout/OOM/GPU receipt.
    const [, , , service, state] = path.split('/');
    if (!['image','video'].includes(service) || !['running','stopping','unconfirmed','confirmed','oom-unconfirmed','oom-confirmed','reset-unconfirmed','reset-confirmed'].includes(state)) throw new Error('invalid recovery fixture');
    const confirmed = ['confirmed','oom-confirmed','reset-confirmed'].includes(state);
    const error = state==='running'?null:state.startsWith('oom-')?'model_out_of_memory':state.startsWith('reset-')?'adapter_reset_failed':'task_timed_out';
    recoveryTask = {id:'timeout-'+service,service,model:service==='image'?'sdxl-smoke':'video-smoke',prompt:'庭院中的柔和蓝色灯光',
      version:++recoveryVersion,status:state==='running'?'running':state==='stopping'?'cancel_requested':'failed',
      stage:state==='running'?'采样中':state==='stopping'?'timeout_stopping':'failed',progress:0.3,
      error,created_at:'2026-09-05T00:00:00Z',updated_at:'2026-09-05T00:01:00Z',
      attempt:{id:'attempt-'+service,instance_id:'sdxl-smoke',epoch:1,status:state==='running'?'running':state==='stopping'?'cancel_requested':'failed',
        exit_confirmed:confirmed?1:0,exit_evidence:confirmed?'fixture-exit':null,
        execution_deadline_at:'2026-09-05T00:00:10Z',termination_reason:error==='task_timed_out'?'task_timed_out':null}};
    eventCallbacks.forEach(callback=>callback({profileId:profile.id,revision:1,event:{protocol:'mc.client/1',
      type:'task.changed',resource_id:recoveryTask.id,version:recoveryTask.version,data:{}}}));
    return {ok:true,status:200,data:{fixture:true,state,service}};
  }
  if (recoveryTask && path === '/api/v1/tasks/'+recoveryTask.id) return {ok:true,status:200,data:recoveryTask};
  if (path.startsWith('/__smoke__/removal/')) {
    const state = path.split('/').at(-1);
    if (!['accepted','failed','ready'].includes(state)) throw new Error('invalid smoke removal state');
    removalOperation = {id:state==='ready'?'dop-smoke-removal-retry':'dop-smoke-removal',deployment_id:userDeployment.id,state,
      payload:{action:'uninstall'},updated_at:'2026-09-05T09:00:00Z',
      error_message:state==='failed'?'容器退出尚未确认；模型资产保留，可重试卸载。':null};
    userDeployment.removal_operation_id = state==='ready'?null:removalOperation.id;
    userDeployment.removal_operation_state = state;
    userDeployment.service_state = state==='failed'?'stopping':'stopped';
    userDeployment.install_state = state==='ready'?'configured':'ready';
    refreshCallbacks.forEach(callback=>callback());
    return {ok:true,status:200,data:{fixture:true,state}};
  }
  if (path === '/api/v1/deployment-plans' && body?.expected_configuration) {
    // Renderer contract fixture only: no Engine, database, network or GPU.
    return { ok: true, status: 200, data: {
      operation: { deployment_id: body.deployment_id, expected_configuration: body.expected_configuration,
        gpu_uuids: body.gpu_uuids, residency: body.residency, plan_digest: 'f'.repeat(64) },
      runtime_profile: runtimeProfile, compatibility: {verdict:'experimental'},
      capacity: {schedulable:true,reason:''}, effects: { updates_existing:true, requires_restart:true,
        changed_fields:['vae_asset_id'], preserves_desired_state:true, deletes_assets:false, uploads_bytes:0, rebuilds_runtime_image:false }
    } };
  }
  const routes = {
    "/api/v1/overview": { services_online: 4, active_tasks: 0, completed_tasks: 1, failed_tasks: 0 },
    "/api/v1/services": { items: services },
    "/api/v1/tasks?limit=50": { items: recoveryTask ? [recoveryTask,imageTask] : [imageTask] },
    "/api/v1/deployments": { items: [deployment, blockedDeployment, stoppedDeployment, userDeployment] },
    "/api/v1/service-catalog": { items: [catalog, blockedCatalog, stoppedCatalog] },
    "/api/v1/service-installations?limit=100": { items: [] },
    "/api/v1/resources/gpus": { configured_gpu_indices: [0], gpus: [{ id: "gpu-0", index: 0, uuid: "GPU-smoke", name: "Smoke GPU", configured_for_mediacenter: true, memory_total_mib: 49140, memory_free_mib: 44000, utilization_gpu_percent: 0 }] },
    "/api/v1/model-assets?limit=500": { items: [{ id: "asset-smoke", display_name: "SDXL Smoke", media_kind: "image", role: "checkpoint", format: "safetensors", total_bytes: 1, revision: "test", state: "ready" }, { id: "asset-upscale", display_name: "AI 超分", media_kind: "image", role: "checkpoint", format: "safetensors", total_bytes: 1, revision: "test", state: "ready" }, { id: "asset-video", display_name: "Video Stopped", media_kind: "video", role: "checkpoint", format: "safetensors", total_bytes: 1, revision: "test", state: "ready" }] },
    "/api/v1/model-transfers?limit=100": { items: [] },
    "/api/v1/runtime-profiles": { items: [runtimeProfile] },
    "/api/v1/deployment-operations?limit=100": { items: [
      {id:'dop-smoke-running',deployment_id:'wai-local',state:'preparing_runtime',created_at:'2026-09-04T00:00:00Z'},
      {id:'dop-smoke-failed',deployment_id:'pony-local',state:'failed',error_class:'recoverable',error_message:'运行环境传输中断，可安全重试',payload:{deployment_id:'pony-local'},created_at:'2026-09-04T00:00:00Z'}
    ] },
    "/api/v1/asset-compatibility": { items: compatibility },
    "/api/v1/hardware": { hostname: "smoke", platform: "Windows", uptime_seconds: 100, cpu: { logical_count: 16, usage_percent: 5 }, memory: { total_bytes: 137438953472, available_bytes: 128849018880, usage_percent: 6 }, storage: [], gpus: [], processes: [] },
    "/api/v1/audit?limit=100": { items: [] }
  };
  if (!(path in routes)) return { ok: false, status: 404, data: { error: { message: `Smoke route missing: ${path}` } } };
  if (path === '/api/v1/deployment-operations?limit=100' && removalOperation) routes[path].items.push(removalOperation);
  if (path === '/api/v1/model-assets?limit=500') routes[path].items.push(...loraAssets, ...userAssets);
  return { ok: true, status: 200, data: routes[path] };
}

contextBridge.exposeInMainWorld("mediaCenterDesktop", Object.freeze({
  listConnections: async () => ({ activeProfileId: profile.id, revision: 1, profiles: [profile] }),
  connectServer: async () => ({ activeProfileId: profile.id, revision: 1, profiles: [profile] }),
  switchServer: async () => ({ activeProfileId: profile.id, revision: 1, profiles: [profile] }),
  logoutServer: async () => ({ activeProfileId: null, revision: 2, profiles: [profile] }),
  removeServer: async () => ({ activeProfileId: null, revision: 2, profiles: [] }),
  request: async (path, request) => response(path, request),
  configureSourceAuthorization: async () => ({}), openSourceTerms: async () => true,
  startEvents: async () => ({}), stopEvents: async () => undefined,
  onServerEvent: callback => { eventCallbacks.push(callback); return () => {}; }, onServerEventStatus: () => () => {}, onModelTransferProgress: () => () => {},
  fetchArtifact: async () => ({ bytes: smokePng, contentType: "image/png" }),
  saveArtifact: async () => ({ saved: false }), uploadAsset: async () => ({}), pickAssets: async () => [],
  pickAndUploadModel: async () => ({ canceled: true }),
  pickModelForImport: async () => ({ canceled: true }), listModelUploadSessions: async () => [],
  startModelUpload: async () => ({}), resumeModelUpload: async () => ({}), pauseModelUpload: async () => ({}), discardModelUpload: async () => true,
  notifyTask: async () => true,
  windowControl: async () => ({ maximized: false }), onWindowState: () => () => {},
  onNavigate: () => () => {}, onRefresh: callback => { refreshCallbacks.push(callback); return () => {}; }
}));
