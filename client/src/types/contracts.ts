export type MediaKind = "image" | "video" | "speech" | "music";
export type TaskStatus =
  | "queued"
  | "assigned"
  | "running"
  | "cancel_requested"
  | "succeeded"
  | "failed"
  | "canceled"
  | "interrupted";
export type JsonObject = Record<string, any>;

export interface ConnectionProfile {
  id: string;
  name: string;
  baseUrl: string;
  hasCredential: boolean;
  sourceAuthorizationAllowed?: boolean;
  allowInsecureSourceAuthorization?: boolean;
  insecure?: boolean;
  createdAt?: string;
  lastUsedAt?: string;
}

export interface ConnectionState {
  activeProfileId: string | null;
  revision: number;
  profiles: ConnectionProfile[];
  requiresLogin?: boolean;
  selectedProfileId?: string;
}

export interface MediaAsset {
  id: string;
  display_name?: string;
  media_type?: string;
  content_type?: string;
  artifact_url?: string;
  [key: string]: unknown;
}

export interface MediaTask {
  execution_binding?: {
    model_key: string;
    recipe_revision: string;
    model_asset_id: string;
    model_asset_revision: string;
    dependencies: Array<Record<string, unknown>>;
  } | null;
  id: string;
  version?: number;
  service: MediaKind;
  model: string;
  prompt: string;
  status: TaskStatus;
  stage?: string;
  progress?: number;
  created_at?: string;
  updated_at?: string;
  started_at?: string;
  error?: string | null;
  attempt?: {
    id: string;
    instance_id: string | null;
    epoch: number | null;
    status: string;
    exit_confirmed: 0 | 1;
    exit_evidence: string | null;
    execution_deadline_at: string | null;
    termination_reason: "user_canceled" | "task_timed_out" | null;
  } | null;
  options?: Record<string, unknown>;
  inputs?: string[];
  output?: { artifact_url?: string; [key: string]: unknown };
  execution?: Record<string, unknown>;
  stage_detail?: Record<string, unknown>;
  publication?: Record<string, unknown>;
  loras?: Array<{
    asset_id: string;
    revision: string;
    family: string;
    weight: number;
  }>;
  deployment_id?: string | null;
  deployment_config_revision?: number | null;
  configuration_binding?: {
    deployment_id: string;
    config_revision: number;
    config_digest: string;
    runtime_profile_id: string;
    runtime_profile_revision: number;
    runtime_profile_digest: string;
    runtime_image_digest: string;
    base_asset_id: string;
    base_asset_revision: string;
    base_asset_manifest_digest: string;
    vae_asset_id: string | null;
    vae_asset_revision: string | null;
    vae_asset_manifest_digest: string | null;
  } | null;
}

export interface AssetCompatibility {
  subject_asset_id: string;
  subject_revision: string;
  base_asset_id: string;
  base_revision: string;
  detector_version: string;
  verdict: "exact" | "compatible" | "experimental" | "incompatible" | "unknown";
  reason_codes: string[];
  evidence_digest: string;
  created_at: string;
}

export interface ServiceModel {
  model_key: string;
  label: string;
  healthy: boolean;
  enabled?: boolean;
  install_state?: string;
  gpu_indices?: number[];
  capabilities?: JsonObject;
  [key: string]: any;
}

export interface MediaService {
  kind: MediaKind;
  name: string;
  description?: string;
  enabled: boolean;
  available: boolean;
  health_reason?: string;
  timeout_seconds?: number;
  active_tasks?: number;
  default_model?: string;
  models: ServiceModel[];
  [key: string]: any;
}

export interface ResourceItem {
  id: string;
  version?: number;
  [key: string]: any;
}

export interface Overview {
  services_online: number;
  active_tasks: number;
  completed_tasks: number;
  failed_tasks: number;
  [key: string]: any;
}

export interface ListResponse<T> {
  items: T[];
}

export interface StatusMessage {
  id: string;
  title: string;
  detail: string;
  type: "info" | "error";
  createdAt: string;
  read: boolean;
  serverId: string;
  serverName: string;
  taskId: string | null;
  service: MediaKind | null;
}

export interface ToastMessage {
  id: string;
  title: string;
  detail: string;
  type: "success" | "error";
  persistent: boolean;
}

export interface ActionState {
  phase: "pending" | "success" | "error";
  label: string;
  error?: string;
}

export interface ServerEvent {
  id?: string;
  protocol: string;
  type: string;
  resource_id?: string;
  version?: number;
  occurred_at?: number;
  data?: Record<string, unknown>;
}

export interface ServerEventEnvelope {
  profileId: string;
  revision: number;
  event: ServerEvent;
}

export interface ServerEventStatus {
  profileId: string;
  revision: number;
  state: string;
  detail?: string;
}

export interface ModelTransferProgress {
  profileId: string;
  revision: number;
  sessionId?: string;
  transferId?: string | null;
  stage?: "hashing" | "uploading" | "server-verifying" | string;
  relativePath: string;
  receivedBytes: number;
  totalBytes: number;
}

export interface ModelImportPreview {
  role: "checkpoint" | "lora" | "vae" | "unknown";
  architectureFamily: string;
  precision: string;
  tensorCount: number | null;
  suggestedResolution?: string | null;
  loraRank?: number | number[] | null;
  loraAlpha?: string | null;
  baseIdentity?: string | null;
  sourceModelName?: string | null;
  title?: string | null;
  pipelineClass?: string | null;
}

export interface ModelUploadSession {
  id: string;
  profileId: string;
  state: string;
  selection: "file" | "directory";
  format: "safetensors" | "diffusers";
  displayName: string;
  createdAt: string;
  updatedAt: string;
  files: Array<{ relativePath: string; size: number; sha256: string | null }>;
  totalBytes: number;
  preview: ModelImportPreview;
  transferId: string | null;
  reusedAsset: ResourceItem | null;
  error: string | null;
}

export interface RuntimeProfile {
  [key: string]: any;
  profile_id: string;
  revision: number;
  label: string;
  image_digest: string;
  profile_digest: string;
  architecture_families: string[];
  main_formats: string[];
  optional_deployment_roles: string[];
  task_roles: string[];
  residency_modes: string[];
  required_vram_mib: number;
}

export interface ExpectedConfiguration {
  config_revision: number;
  config_digest: string;
  policy_version: number;
}

export interface DeploymentPlan {
  [key: string]: any;
  operation: Record<string, any> & { expected_configuration?: ExpectedConfiguration };
  compatibility: Record<string, any> | null;
  runtime_profile: RuntimeProfile;
  capacity: { schedulable: boolean; reason: string };
  effects: {
    creates_container: boolean;
    uploads_bytes: number;
    deletes_assets: boolean;
    starts_service: boolean;
    updates_existing?: false;
  } | {
    updates_existing: true;
    changed_fields: string[];
    requires_restart: boolean;
    preserves_desired_state: true;
    deletes_assets: false;
    uploads_bytes: 0;
    rebuilds_runtime_image: false;
  };
}

export interface DeploymentOperation extends ResourceItem {
  id: string;
  deployment_id: string;
  state:
    | "accepted"
    | "preparing_runtime"
    | "creating_container"
    | "starting_worker"
    | "loading_model"
    | "health_check"
    | "ready"
    | "failed"
    | "canceling"
    | "rollback"
    | "canceled";
  plan_digest: string;
  payload: Record<string, any>;
  error_class?: "recoverable" | "non_recoverable" | "canceled" | null;
  error_code?: string | null;
  error_message?: string | null;
  recovery_cursor?: ResourceItem | null;
  result?: ResourceItem | null;
}
