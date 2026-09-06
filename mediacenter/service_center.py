from __future__ import annotations

import hashlib
import logging
import math
import os
import time
import uuid
import io
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .domain import ServiceKind, TaskStatus, ACTIVE_TASK_STATUSES
from .task_state import TaskState, TaskStateError
from .worker_common import digest
from .capabilities import capability_for, validate_options, validate_worker_request, worker_capability_for
from .residency import residency_modes_for
from .model_registry import ModelRegistry
from .model_deployments import DeploymentError, ModelDeploymentManager
from .deployment_operations import (
    DeploymentOperationError, DeploymentOperationManager,
)
from .model_assets import ModelAssetError, ModelAssetManager
from .asset_compatibility import AssetCompatibilityError, AssetCompatibilityManager, CURRENT_DETECTOR_VERSION
from .service_installer import ServiceInstaller, ServiceInstallerError
from .repository import Repository
from .gpu_scheduler import GPUBusyError, GPULeaseScheduler
from .hardware import HardwareProbe
from .artifacts import ArtifactStore, AuthorizedFile, open_regular, copy_snapshot, file_hash
from .runtime_artifacts import RuntimeLockBusy


class ServiceCenterError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class _DeploymentWorkerStopping(Exception):
    """Leave the durable operation resumable at the next safe boundary."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ServiceCenter:
    def __init__(self, repository: Repository, registry: ModelRegistry, artifact_root: str | Path,
                 deployments: ModelDeploymentManager | None = None,
                 gpu_scheduler: GPULeaseScheduler | None = None,
                 retire_worker: Callable[[str], None] | None = None,
                 hardware_probe: HardwareProbe | None = None,
                 model_assets: ModelAssetManager | None = None,
                 service_installer: ServiceInstaller | None = None, *, runtime=None,
                 runtime_importer=None):
        self.repository = repository
        self.task_state = runtime.authority.tasks if runtime else TaskState(repository)
        self.runtime = runtime
        self.registry = registry
        self.artifact_root = Path(artifact_root).resolve()
        self.artifacts = runtime.artifacts if runtime and runtime.artifacts else ArtifactStore(self.task_state, artifact_root)
        self.deployments = deployments
        self.gpu_scheduler = gpu_scheduler
        self.retire_worker = retire_worker
        self.hardware_probe = hardware_probe
        self.model_assets = model_assets
        self.service_installer = service_installer
        self.runtime_importer = runtime_importer
        self._deployment_stop = threading.Event()
        self._deployment_wake = threading.Event()
        self._deployment_guard = threading.Lock()
        self._deployment_thread = None
        self.started_monotonic = time.monotonic()
        self.input_root = self.artifact_root.parent / "inputs"
        self.input_root.mkdir(parents=True, exist_ok=True)

    def start_deployment_worker(self) -> None:
        if self.runtime_importer is None:
            return
        with self._deployment_guard:
            if self._deployment_thread is not None and self._deployment_thread.is_alive():
                return
            self._deployment_stop.clear()
            self._deployment_thread = threading.Thread(
                target=self._deployment_loop, name="user-deployment-runner", daemon=True)
            self._deployment_thread.start()

    def stop_deployment_worker(self, timeout: float = 5.0) -> bool:
        self._deployment_stop.set()
        self._deployment_wake.set()
        with self._deployment_guard:
            thread = self._deployment_thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _deployment_loop(self) -> None:
        while not self._deployment_stop.is_set():
            self._deployment_wake.clear()
            try:
                # Filter before limiting: completed history must not hide old work.
                with self.repository._connect() as db:
                    ids = [row['id'] for row in db.execute(
                        "SELECT id FROM deployment_operations WHERE state IN "
                        "('accepted','preparing_runtime','creating_container',"
                        "'starting_worker','loading_model','health_check','canceling','rollback') "
                        "ORDER BY created_at,id LIMIT 32")]
                for operation_id in ids:
                    if self._deployment_stop.is_set():
                        break
                    try:
                        self._execute_user_deployment(operation_id)
                    except RuntimeLockBusy:
                        # Another process owns the lock. Do not fail or roll it back.
                        pass
            except Exception:
                logging.getLogger(__name__).exception("user_deployment_runner_failed")
            self._deployment_wake.wait(1.0)

    def list_services(self) -> list[dict[str, Any]]:
        # Reconciliation can atomically replace an installation without this
        # process receiving a local callback (including after a restart).
        self.registry.refresh()
        items = []
        for stored in self.repository.list_services():
            kind = ServiceKind(stored["kind"])
            models = self.registry.public(kind)
            healthy_models = [model for model in models if model["healthy"]]
            available = bool(stored["enabled"] and healthy_models)
            reason = "模型未登记" if not models else "; ".join(
                f"{model['label']}: {model['health_reason']}" for model in models if not model["healthy"]
            )
            items.append({**stored, "provider": "mediacenter-kernel", "models": models,
                          "default_model": next((model["model_key"] for model in models if model["default"]), None),
                          "health_reason": "ready" if healthy_models else reason,
                          "enabled": bool(stored["enabled"]),
                          "available": available, "status": "online" if available else "offline",
                          "concurrency": 1, "active_tasks": self.repository.active_count(kind)})
        return items

    def configure_service(self, raw_kind: str, changes: dict[str, Any]) -> dict[str, Any]:
        kind = self._kind(raw_kind)
        if not changes or set(changes) - {"enabled", "timeout_seconds"}:
            raise ServiceCenterError("invalid_configuration", "仅支持 enabled 和 timeout_seconds")
        if "enabled" in changes and not isinstance(changes["enabled"], bool):
            raise ServiceCenterError("invalid_enabled", "enabled 必须是布尔值")
        if "timeout_seconds" in changes:
            self._bounded_int(changes["timeout_seconds"], "timeout_seconds", 10, 7200)
        self.repository.update_service(kind, changes)
        self.repository.add_audit(utc_now(), "service.configure", kind.value, changes)
        return next(item for item in self.list_services() if item["kind"] == kind.value)

    def create_task(self, payload: dict[str, Any], *, idempotency_key: str | None = None,
                    identity_scope: str = "server-admin") -> dict[str, Any]:
        try:
            return self._create_task(payload, idempotency_key, identity_scope)
        except TaskStateError as exc:
            raise ServiceCenterError(exc.code, exc.code, exc.status) from None

    def _create_task(self, payload, idempotency_key, identity_scope, *, validation=None):
        if not isinstance(payload, dict):
            raise ServiceCenterError("invalid_body", "请求体必须是 JSON 对象")
        if set(payload) - {"service", "prompt", "model", "options", "inputs", "loras", "expected_binding", "expected_configuration"}:
            raise ServiceCenterError("unknown_task_field", "请求包含未知字段")
        kind = self._kind(payload.get("service"))
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
            raise ServiceCenterError("invalid_prompt", "prompt 必须为 1 到 4000 个字符")
        options = payload.get("options", {})
        if not isinstance(options, dict):
            raise ServiceCenterError("invalid_options", "options 必须是 JSON 对象")
        self._validate_finite(options)
        model_key = payload.get("model")
        if not isinstance(model_key, str) or not model_key:
            raise ServiceCenterError("invalid_model", "model 必须指定已登记的模型")
        request = {"service": kind.value, "prompt": prompt.strip(), "model": model_key,
                   "options": options, "inputs": payload.get("inputs", [])}
        for key in ('expected_binding', 'expected_configuration'):
            if key in payload:
                if type(payload[key]) is not dict:
                    raise ServiceCenterError('invalid_expected_configuration', '预期运行绑定必须为对象')
                request[key] = payload[key]
        if payload.get('loras') != [] and 'loras' in payload:
            request['loras'] = payload['loras']
        if validation is not None: request['validation_id'] = validation['validation_id']
        prior = self.task_state.lookup(identity_scope, idempotency_key, request)
        if prior is not None:
            return self.get_task(prior["id"])
        if model_key == "mediacenter-client-image-edit":
            if 'expected_binding' in request or 'expected_configuration' in request:
                raise ServiceCenterError('invalid_expected_configuration', '本地编辑没有模型配置')
            if request.get('loras'): raise ServiceCenterError('unsupported_lora', '本地编辑不支持LoRA')
            return self._create_client_image_edit(kind, prompt.strip(), options, request["inputs"],
                                                  request, identity_scope, idempotency_key)
        self.registry.refresh()
        spec = self.registry.get(kind, model_key)
        if spec is None:
            raise ServiceCenterError("invalid_model", "所选模型不存在或不属于该服务")
        healthy, reason = spec.health()
        if not healthy:
            raise ServiceCenterError("service_unavailable", reason, 503)
        try:
            validate_options(spec.catalog_key, kind, options)
        except ValueError as exc:
            raise ServiceCenterError("invalid_options", str(exc)) from None
        inputs = payload.get("inputs", [])
        if not isinstance(inputs, list) or any(not isinstance(item, str) for item in inputs):
            raise ServiceCenterError("invalid_inputs", "inputs 必须是素材 ID 数组")
        contract = capability_for(spec.catalog_key, kind).get(
            "input_contract", {"minimum": 0, "maximum": 0, "accept": []})
        if not contract["minimum"] <= len(inputs) <= contract["maximum"]:
            raise ServiceCenterError("invalid_inputs",
                                     f"该模型需要 {contract['minimum']} 到 {contract['maximum']} 个输入素材")
        assets = []
        input_bindings = []
        for asset_id in inputs:
            asset = self.repository.get_asset(asset_id)
            if asset is None:
                raise ServiceCenterError("asset_not_found", f"输入素材不存在: {asset_id}", 404)
            if not any(asset["media_type"].startswith(prefix) for prefix in contract["accept"]):
                raise ServiceCenterError("invalid_asset_type", f"模型不支持素材类型: {asset['media_type']}")
            source=Path(asset['storage_path']).absolute()
            if self.input_root not in source.parents:raise ServiceCenterError('asset_path_escape','素材路径越界')
            try:
                with open_regular(source) as stream:
                    if file_hash(stream,asset['byte_size'])!=(asset['sha256'],asset['byte_size']):
                        raise TaskStateError('input_identity_changed')
            except (OSError,TaskStateError):
                raise ServiceCenterError('input_identity_changed','素材身份已变化',409) from None
            assets.append(asset_id)
            input_bindings.append({"asset_id": asset_id, "revision": asset["sha256"], "media_type": asset["media_type"]})
        binding = None
        deployment_id = None
        deployment_config_revision = None
        configuration = None
        if spec.asset_id is not None:
            if self.runtime is None:
                raise ServiceCenterError("runtime_unavailable", "容器运行控制未配置", 503)
            try:
                with self.repository._connect() as db:
                    deployment = db.execute(
                        "SELECT * FROM model_deployments WHERE id=?", (spec.model_key,)).fetchone()
                    if deployment is None:
                        raise TaskStateError("deployment_not_ready")
                    binding = self.runtime.authority.deployment_binding(db, deployment)
                    if deployment["current_config_revision"] is not None:
                        revision = db.execute(
                            """SELECT config_digest FROM model_deployment_revisions
                               WHERE deployment_id=? AND config_revision=?""",
                            (deployment["id"], deployment["current_config_revision"]),
                        ).fetchone()
                        if revision is None:
                            raise TaskStateError("deployment_revision_unavailable")
                        deployment_id = deployment["id"]
                        deployment_config_revision = deployment["current_config_revision"]
                        configuration = dict(deployment_id=deployment_id,
                            config_revision=deployment_config_revision, config_digest=revision['config_digest'])
            except TaskStateError as exc:
                raise ServiceCenterError(exc.code, "部署运行绑定不可用", 409) from None
        if ('expected_binding' in request and request['expected_binding'] != binding
                or 'expected_configuration' in request and request['expected_configuration'] != configuration):
            raise ServiceCenterError('deployment_configuration_changed',
                '模型或 VAE 配置已变化，请确认新配置后再提交；草稿参数没有被替换。', 409)
        lora_bindings = []
        if request.get('loras') is not None:
            try:
                capability = worker_capability_for(spec.catalog_key)
                validate_worker_request(spec.catalog_key, capability['operation'], {'prompt':prompt.strip(), **options},
                                        input_bindings, request['loras'])
                installation = getattr(self.registry, 'installation_runtime', None)
                if installation is None or installation.lora_authority is None:
                    raise ValueError('lora_authority_unavailable')
                current = installation.get(model_key)
                if current is None: raise ValueError('runtime_installation_binding_required')
                lora_bindings = [installation.lora_authority.bind(
                                 item,
                                 base_asset_id=current['asset_id'],
                                 base_revision=current['asset_revision'],
                                 runtime_profile_digest=current['recipe_digest'],
                                 assets=self.model_assets)
                                 for item in request['loras']]
            except (ValueError, RuntimeError) as exc:
                raise ServiceCenterError(getattr(exc, 'code', 'invalid_lora'), 'LoRA未获批准或身份已变化', 409) from None
        task, _ = self.task_state.accept(request, scope=identity_scope, key=idempotency_key,
                                         binding=binding, input_bindings=input_bindings,
                                         lora_bindings=lora_bindings, validation=validation,
                                         deployment_id=deployment_id,
                                         deployment_config_revision=deployment_config_revision)
        return self.get_task(task["id"])

    def _create_client_image_edit(self, kind: ServiceKind, prompt: str,
                                  options: dict[str, Any], inputs: Any, request: dict,
                                  scope: str, key: str | None, *, retry: tuple[str, int] | None = None) -> dict[str, Any]:
        """Persist a deterministic Canvas edit as an audited image task.

        The renderer performs crop/mosaic/composite pixels locally; the server verifies the
        encoded PNG and promotes exactly that managed upload into immutable history.
        This path never impersonates a model task and never enters the GPU queue.
        """
        if kind != ServiceKind.IMAGE:
            raise ServiceCenterError("invalid_model", "客户端图片编辑仅属于 image 服务")
        if set(options) != {"operation", "width", "height"} or options.get("operation") not in {"crop", "mosaic", "composite"}:
            raise ServiceCenterError("invalid_options", "客户端图片编辑参数无效")
        width = self._bounded_int(options.get("width"), "width", 1, 8192)
        height = self._bounded_int(options.get("height"), "height", 1, 8192)
        if width * height > 64_000_000:
            raise ServiceCenterError("invalid_options", "编辑产物总像素不得超过 64MP")
        if not isinstance(inputs, list) or len(inputs) != 1 or not isinstance(inputs[0], str):
            raise ServiceCenterError("invalid_inputs", "客户端图片编辑必须包含一个受管 PNG 素材")
        asset = self.repository.get_asset(inputs[0])
        if asset is None:
            raise ServiceCenterError("asset_not_found", "编辑产物素材不存在", 404)
        if asset["media_type"] != "image/png":
            raise ServiceCenterError("invalid_asset_type", "客户端图片编辑产物必须为 PNG")
        source = Path(asset["storage_path"]).absolute()
        if self.input_root.resolve() not in source.parents:
            raise ServiceCenterError("asset_path_escape", "编辑素材路径越界")
        with open_regular(source) as stream:
            header = stream.read(24)
        if len(header) != 24 or not header.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ServiceCenterError("invalid_asset_signature", "编辑产物不是有效 PNG")
        encoded_width = int.from_bytes(header[16:20], "big")
        encoded_height = int.from_bytes(header[20:24], "big")
        if (encoded_width, encoded_height) != (width, height):
            raise ServiceCenterError("invalid_options", "声明尺寸与 PNG 像素尺寸不一致")
        references = [{"asset_id": asset["id"], "revision": asset["sha256"], "media_type": asset["media_type"]}]
        if retry is None:
            task, created = self.task_state.accept(request, scope=scope, key=key, mode="local", input_bindings=references)
        else:
            if self.get_task(retry[0])["input_bindings"] != references:
                raise ServiceCenterError("input_identity_changed", "原编辑素材身份已变化，不能重试", 409)
            task, created = self.task_state.retry(*retry, local=True), True
        if not created:
            return self.get_task(task["id"])
        task_id = task["id"]
        attempt = task["current_attempt_id"]
        try:
            self.artifacts.local(task,source,asset,{"provider":"client-canvas","operation":options["operation"],
                "effective_options":options,"duration_seconds":0.0})
        except Exception as exc:
            current = self.repository.get_task(task_id)
            publication = (current or {}).get("publication") or {}
            if (isinstance(exc, OSError) and publication.get("error_code") == "publication_io_error"
                    and current["current_attempt_id"] == attempt and not current["cancel_requested"]):
                # The local computation is already represented by a durable
                # immutable input/publication. Retry its bounded copy, never
                # replace the attempt merely because storage is temporarily full.
                return self.get_task(task_id)
            if current and current["current_attempt_id"] == attempt and current["status"] in {"running", "cancel_requested"}:
                self.task_state.finish_local(task_id, attempt, error="local_edit_failed")
            current = self.repository.get_task(task_id)
            if current and current["status"] in {"canceled", "succeeded"}:
                return self.get_task(task_id)
            if isinstance(exc, TaskStateError):
                raise
            raise ServiceCenterError("artifact_write_failed", "编辑产物保存失败", 500) from exc
        return self.get_task(task_id)

    def create_asset(self, filename: str, media_type: str, data: bytes) -> dict[str, Any]:
        if (not isinstance(filename, str) or not filename or len(filename) > 255 or
                Path(filename).name != filename or not isinstance(media_type, str)):
            raise ServiceCenterError("invalid_asset", "素材文件名或类型无效")
        allowed = {
            "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
            "video/mp4": ".mp4", "video/webm": ".webm", "audio/wav": ".wav",
            "audio/mpeg": ".mp3", "audio/flac": ".flac", "audio/mp4": ".m4a",
        }
        extension = allowed.get(media_type)
        if extension is None or not data or len(data) > 256 * 1024 * 1024:
            raise ServiceCenterError("invalid_asset", "仅支持指定图片、视频和音频，单文件最大 256MiB")
        signatures = {
            "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/jpeg": data.startswith(b"\xff\xd8\xff"),
            "image/webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
            "video/mp4": data[4:8] == b"ftyp",
            "video/webm": data.startswith(b"\x1aE\xdf\xa3"),
            "audio/wav": data.startswith(b"RIFF") and data[8:12] == b"WAVE",
            "audio/mpeg": data.startswith(b"ID3") or data.startswith((b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")),
            "audio/flac": data.startswith(b"fLaC"),
            "audio/mp4": data[4:8] == b"ftyp",
        }
        if not signatures[media_type]:
            raise ServiceCenterError("invalid_asset_signature", "素材内容与声明的媒体类型不匹配")
        asset_id = f"ast_{uuid.uuid4().hex}"
        target = (self.input_root / f"{asset_id}{extension}").resolve()
        if self.input_root.resolve() not in target.parents:
            raise ServiceCenterError("asset_path_escape", "素材路径越界")
        copy_snapshot(io.BytesIO(data),target,sha256=hashlib.sha256(data).hexdigest(),byte_size=len(data),media_type=media_type)
        asset = {
            "id": asset_id, "filename": filename, "media_type": media_type,
            "sha256": hashlib.sha256(data).hexdigest(), "byte_size": len(data),
            "storage_path": str(target), "created_at": utc_now(),
        }
        self.repository.insert_asset(asset)
        self.repository.add_audit(asset["created_at"], "asset.create", asset_id,
                                  {"filename": filename, "media_type": media_type,
                                   "byte_size": len(data), "sha256": asset["sha256"]})
        return {key: value for key, value in asset.items() if key != "storage_path"}

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        asset = self.repository.get_asset(asset_id)
        if asset is None:
            raise ServiceCenterError("asset_not_found", "素材不存在", 404)
        return {key: value for key, value in asset.items() if key != "storage_path"}

    def model_catalog(self) -> list[dict[str, Any]]:
        return self.deployments.public_catalog() if self.deployments else []

    def service_catalog(self) -> list[dict[str, Any]]:
        return self.service_installer.catalog() if self.service_installer else []

    def source_authorizations(self) -> list[dict[str, Any]]:
        manager = self._model_assets()
        return [{
            "provider": "huggingface",
            "configured": manager.has_download_credential("huggingface.co"),
            "persistence": "memory",
        }]

    def configure_source_authorization(self, provider: str,
                                       payload: dict[str, Any]) -> dict[str, Any]:
        if provider != "huggingface":
            raise ServiceCenterError(
                "source_authorization_provider_unsupported", "模型来源授权提供方不受支持", 404)
        if not isinstance(payload, dict) or set(payload) - {"token", "action"}:
            raise ServiceCenterError(
                "invalid_source_authorization", "模型来源授权字段不受支持")
        manager = self._model_assets()
        try:
            if payload.get("action") == "clear":
                if set(payload) != {"action"}:
                    raise ServiceCenterError(
                        "invalid_source_authorization", "清除来源授权不能同时提交令牌")
                manager.clear_download_credential("huggingface.co")
            elif payload.get("action") in (None, "configure"):
                if set(payload) not in ({"token"}, {"token", "action"}):
                    raise ServiceCenterError(
                        "invalid_source_authorization", "必须提供模型来源令牌")
                manager.configure_download_credential("huggingface.co", payload.get("token"))
            else:
                raise ServiceCenterError(
                    "invalid_source_authorization", "模型来源授权动作不受支持")
        except ModelAssetError as exc:
            raise ServiceCenterError(exc.code, exc.message, exc.status) from None
        return self.source_authorizations()[0]

    def list_service_installations(self, limit: int = 100) -> list[dict[str, Any]]:
        if self.service_installer is None:
            return []
        return self.service_installer.list(limit)

    def get_service_installation(self, installation_id: str) -> dict[str, Any]:
        if self.service_installer is None:
            raise ServiceCenterError("service_installer_unavailable", "服务安装中心未启用", 503)
        try:
            return self.service_installer.get(installation_id)
        except ServiceInstallerError as exc:
            raise ServiceCenterError(exc.code, exc.message, exc.status) from None

    def install_service(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.service_installer is None:
            raise ServiceCenterError("service_installer_unavailable", "服务安装中心未启用", 503)
        try:
            installation = self.service_installer.start(payload)
        except ServiceInstallerError as exc:
            raise ServiceCenterError(exc.code, exc.message, exc.status) from None
        self.repository.add_audit(utc_now(), "service_installation.create", installation["id"], {
            "recipe_key": installation["recipe_key"],
            "gpu_indices": installation["options"]["gpu_indices"],
        })
        return installation

    def uninstall_service(self, recipe_key: str) -> dict[str, Any]:
        if self.service_installer is None:
            raise ServiceCenterError("service_installer_unavailable", "服务安装中心未启用", 503)
        try:
            service = self.service_installer.uninstall(recipe_key)
        except ServiceInstallerError as exc:
            raise ServiceCenterError(exc.code, exc.message, exc.status) from None
        self.repository.add_audit(utc_now(), "service_installation.uninstall", recipe_key, {
            "retained_deployment_id": service.get("retained_deployment_id"),
            "assets_preserved": True,
            "runtime_cache_preserved": True,
        })
        return service

    def control_service_installation(self, installation_id: str, action: str) -> dict[str, Any]:
        if self.service_installer is None:
            raise ServiceCenterError("service_installer_unavailable", "服务安装中心未启用", 503)
        try:
            installation = self.service_installer.control(installation_id, action)
        except (ServiceInstallerError, ModelAssetError) as exc:
            raise ServiceCenterError(exc.code, exc.message, exc.status) from None
        self.repository.add_audit(utc_now(), f"service_installation.{action}", installation_id, {})
        return installation

    def list_model_assets(self, *, media_kind: str | None = None, role: str | None = None,
                          state: str | None = None, query: str | None = None,
                          limit: int = 100) -> list[dict[str, Any]]:
        manager = self._model_assets()
        return manager.list_assets(media_kind=media_kind, role=role, state=state,
                                   query=query, limit=max(1, min(limit, 500)))

    def get_model_asset(self, asset_id: str) -> dict[str, Any]:
        try:
            return self._model_assets().get_asset(asset_id)
        except ModelAssetError as exc:
            raise ServiceCenterError(exc.code, exc.message, exc.status) from None

    def list_asset_compatibility(self, *, subject_asset_id: str | None = None,
                                 base_asset_id: str | None = None) -> list[dict[str, Any]]:
        """Expose immutable server-produced evidence for desktop filtering."""
        return [item for item in self.repository.list_asset_compatibility(
            subject_asset_id=subject_asset_id, base_asset_id=base_asset_id)
                if item['detector_version'] == CURRENT_DETECTOR_VERSION]

    def assess_asset_compatibility(self, payload: dict[str, Any]) -> dict[str, Any]:
        if (type(payload) is not dict
                or set(payload) != {"subject_asset_id", "base_asset_id"}
                or any(type(payload[key]) is not str or not payload[key]
                       for key in payload)):
            raise ServiceCenterError(
                "invalid_compatibility_request", "兼容检查必须指定两个模型资产", 400)
        try:
            result = AssetCompatibilityManager(self.repository).assess(
                payload["subject_asset_id"], payload["base_asset_id"])
        except AssetCompatibilityError as exc:
            raise ServiceCenterError(exc.code, str(exc), exc.status) from None
        if result['disposition'] == 'created':
            self.repository.add_audit(utc_now(), "asset.compatibility.assess",
                                      payload["subject_asset_id"], {
                "base_asset_id": payload["base_asset_id"],
                "verdict": result["verdict"],
                "evidence_digest": result["evidence_digest"],
            })
        return result

    def model_storage(self) -> dict[str, Any]:
        return self._model_assets().storage_summary()

    def list_model_transfers(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._model_assets().list_transfers(max(1, min(limit, 500)))

    def list_runtime_profiles(self) -> list[dict[str, Any]]:
        """Expose immutable loader capabilities for the desktop deployment planner."""
        return self.repository.list_runtime_profiles()

    def get_model_transfer(self, transfer_id: str) -> dict[str, Any]:
        try:
            return self._model_assets().get_transfer(transfer_id)
        except ModelAssetError as exc:
            raise ServiceCenterError(exc.code, exc.message, exc.status) from None

    def create_model_transfer(self, payload: dict[str, Any]) -> dict[str, Any]:
        direction = payload.get("direction")
        request = dict(payload)
        request.pop("direction", None)
        try:
            if direction == "upload":
                transfer = self._model_assets().create_upload(request)
            elif direction == "download" and request.pop("source_type", None) == "https":
                transfer = self._model_assets().create_https_download(request)
            else:
                raise ModelAssetError("invalid_transfer_source", "模型传输来源不受支持")
        except ModelAssetError as exc:
            raise ServiceCenterError(exc.code, exc.message, exc.status) from None
        self.repository.add_audit(utc_now(), "model_transfer.create", transfer["id"], {
            "direction": transfer["direction"], "display_name": transfer["display_name"],
            "expected_bytes": transfer["expected_bytes"],
        })
        return transfer

    def preflight_model_import(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._model_assets().preflight_import(payload)
        except ModelAssetError as exc:
            raise ServiceCenterError(exc.code, exc.message, exc.status) from None

    def _user_deployment_components(self):
        installation = getattr(self.registry, "installation_runtime", None)
        if (self.deployments is None or self.gpu_scheduler is None or self.runtime is None
                or installation is None
                or getattr(self.runtime, "package_provider", None) is None):
            raise ServiceCenterError(
                "runtime_unavailable", "用户模型 Runtime 尚未在服务器启用", 503)
        return installation

    def _user_gpu_indices(self, gpu_uuids: list[str]) -> list[int]:
        mapping = dict(zip(self.gpu_scheduler.allowed_uuids,
                           self.gpu_scheduler.allowed_indices))
        if any(value not in mapping for value in gpu_uuids):
            raise ServiceCenterError(
                "invalid_gpu_binding", "GPU 必须来自服务器显式资源池", 400)
        return [mapping[value] for value in gpu_uuids]

    @staticmethod
    def _user_budget(operation: dict[str, Any]) -> dict[str, Any]:
        required = operation["required_vram_mib"]
        task = 1024 if required <= 8192 else 2048 if required <= 20480 else 4096
        return {
            "gpus": list(operation["gpu_uuids"]),
            "base_mib": required - task, "task_mib": task,
            "external_reserve_mib": operation.get('policy_options', {}).get('external_reserve_mib', 8192),
            "sharing_mode": operation["sharing_mode"],
        }

    def plan_user_deployment(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._user_deployment_components()
        try:
            plan = (self.deployments.plan_user_configuration(payload)
                    if isinstance(payload, dict) and 'expected_configuration' in payload
                    else self.deployments.plan_user(payload))
        except DeploymentError as exc:
            status = 409 if exc.code in {"deployment_exists", "asset_incompatible"} else 400
            raise ServiceCenterError(exc.code, str(exc), status) from None
        self._user_gpu_indices(plan["operation"]["gpu_uuids"])
        try:
            self.gpu_scheduler.validate_budget(self._user_budget(plan["operation"]))
            capacity = {"schedulable": True, "reason": "capacity_available"}
        except GPUBusyError as exc:
            capacity = {"schedulable": False, "reason": str(exc)}
        return {
            "operation": plan["operation"],
            "compatibility": plan["compatibility"],
            "runtime_profile": {key: plan["runtime_profile"][key] for key in (
                "profile_id", "revision", "label", "image_digest", "profile_digest",
                "required_vram_mib", "residency_modes")},
            "capacity": capacity,
            "effects": plan["effects"],
        }

    @staticmethod
    def _plan_payload(operation: dict[str, Any]) -> dict[str, Any]:
        result = {
            "deployment_id": operation["deployment_id"],
            "base_asset_id": operation["base_asset"]["asset_id"],
            "vae_asset_id": operation["vae_asset"]["asset_id"]
            if operation["vae_asset"] else None,
            "runtime_profile_id": operation["runtime_profile"]["profile_id"],
            "runtime_profile_revision": operation["runtime_profile"]["revision"],
            "gpu_uuids": list(operation["gpu_uuids"]),
            "residency": operation["residency"],
            "sharing_mode": operation["sharing_mode"],
            "required_vram_mib": operation["required_vram_mib"],
            "license_accepted": operation["confirmations"]["license_accepted"],
            "experimental_compatibility_accepted": operation["confirmations"][
            "experimental_compatibility_accepted"],
        }
        if 'expected_configuration' in operation:
            result.update(expected_configuration=operation['expected_configuration'],
                          policy_options=operation['policy_options'])
        return result

    @staticmethod
    def _deployment_operation_error(exc: Exception) -> tuple[str, str, str]:
        code = getattr(exc, "code", None) or (
            str(exc) if isinstance(exc, (GPUBusyError, TaskStateError)) else
            "user_deployment_failed")
        non_recoverable = {
            "deployment_plan_changed", "deployment_retry_identity_changed",
            "idempotency_conflict", "runtime_asset_binding_changed",
            "runtime_optional_asset_changed", "runtime_profile_changed",
            "runtime_import_outcome_unknown", "runtime_image_import_unresolved",
        }
        error_class = "non_recoverable" if code in non_recoverable else "recoverable"
        if isinstance(exc, GPUBusyError) or code in {
                "gpu_policy_unschedulable", "container_materialization_unconfirmed"}:
            message = "GPU 或容器资源暂不可用；释放容量或修复容器运行时后重试。"
        elif code in {"runtime_import_outcome_unknown", "runtime_image_import_unresolved"}:
            message = "镜像导入结果尚不确定；已暂停自动重试，请核对容器引擎中的导入状态后恢复。"
        elif str(code).startswith("runtime_"):
            message = "运行环境绑定未通过；请在安装中心修复 Runtime 制品后重试。"
        elif isinstance(exc, (DeploymentError, DeploymentOperationError)):
            message = str(exc)
        else:
            message = "部署未完成；已保留模型资产，请检查运行环境后重试。"
        return error_class, str(code)[:160], message[:1000]

    def _rollback_user_deployment_operation(
            self, operations: DeploymentOperationManager,
            installation: Any, operation_id: str, *,
            error_class: str, error_code: str, error_message: str,
            canceled: bool = False) -> dict[str, Any]:
        """Settle one operation without touching shared model/runtime assets."""
        current = operations.get(operation_id)
        if current["state"] in {"ready", "failed", "canceled"}:
            return current
        if current["state"] != "rollback":
            try:
                current = operations.transition(
                    operation_id, "rollback",
                    recovery_cursor={"step": "runtime_cleanup"},
                    error_class="canceled" if canceled else error_class,
                    error_code="user_canceled" if canceled else error_code,
                    error_message="用户取消部署" if canceled else error_message,
                )
            except DeploymentOperationError:
                current = operations.get(operation_id)
                if current["state"] in {"ready", "failed", "canceled"}:
                    return current
                if current["state"] != "rollback":
                    raise
        try:
            context = self.repository.get_configuration_context(operation_id)
            if context is not None:
                # A configuration operation never owns the pre-existing
                # installation. Its rollback must not uninstall that service.
                self.runtime.authority.abort_configuration_operation(
                    operation_id, 'user_canceled' if canceled else error_code)
                return operations.get(operation_id)
            deployment = self.repository.get_deployment(current["deployment_id"])
            binding = installation.get(current["deployment_id"]) if deployment else None
            # Ownership is exact. Another operation's binding is observation,
            # never authority to stop or detach that service.
            if binding is not None and binding.get("operation_id") == operation_id:
                retire = getattr(self.runtime, "retire_instance_containers", None)
                if callable(retire):
                    retire(current["deployment_id"])
                deployment = self.repository.get_deployment(current["deployment_id"])
                if deployment is None:
                    raise TaskStateError("deployment_identity_changed")
                self.repository.uninstall_service_binding(
                    deployment["id"], deployment["incarnation"], utc_now())
            installation.settle_user_operation(
                operation_id, "canceled" if canceled else "failed",
                "user_canceled" if canceled else error_code)
        except Exception as cleanup_exc:
            cleanup_code = (getattr(cleanup_exc, "code", None)
                            or f"deployment_rollback_{type(cleanup_exc).__name__}")
            cursor = {"step": "runtime_cleanup_required", "code": str(cleanup_code)[:160]}
            if current.get('recovery_cursor') != cursor:
                logging.getLogger(__name__).exception(
                    "deployment_operation_rollback_failed operation_id=%s", operation_id)
                self.repository.transition_deployment_operation(operation_id, {'rollback'}, {
                    'recovery_cursor': cursor, 'updated_at': utc_now()})
            # A transient cleanup failure is not terminal. The same locked dop
            # retries cleanup; unchanged failures do not write on every poll.
            return operations.get(operation_id)
        try:
            self.registry.refresh()
        except Exception:
            # Runtime ownership has already been released. A registry projection
            # fault is observable but must not falsify the cleanup outcome.
            logging.getLogger(__name__).exception(
                "deployment_operation_registry_refresh_failed operation_id=%s",
                operation_id)
        if canceled:
            return operations.transition(
                operation_id, "canceled",
                recovery_cursor={"step": "runtime_cleanup_complete"})
        return operations.transition(
            operation_id, "failed", error_class=error_class,
            error_code=error_code, error_message=error_message,
            recovery_cursor={"step": "runtime_cleanup_complete"})

    def _deployment_operation_checkpoint(
            self, operations: DeploymentOperationManager,
            installation: Any, operation_id: str) -> dict[str, Any]:
        if self._deployment_stop.is_set():
            raise _DeploymentWorkerStopping()
        current = operations.get(operation_id)
        if current["state"] == "canceling":
            return self._rollback_user_deployment_operation(
                operations, installation, operation_id,
                error_class="canceled", error_code="user_canceled",
                error_message="用户取消部署", canceled=True)
        return current

    def create_user_deployment(self, payload: dict[str, Any], *,
                               idempotency_key: str | None,
                               identity_scope: str = "server-admin") -> dict[str, Any]:
        self._user_deployment_components()
        if self.runtime_importer is None:
            raise ServiceCenterError("runtime_unavailable", "运行镜像导入器尚未配置", 503)
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ServiceCenterError(
                "idempotency_key_required", "部署命令必须提供 Idempotency-Key", 400)
        operations = DeploymentOperationManager(self.repository)
        def configuration_plan():
            result = self.deployments.plan_user_configuration(self._plan_payload(payload))
            self._user_gpu_indices(result['operation']['gpu_uuids'])
            return result
        try:
            operation = operations.create(
                payload, server_profile_id=self.task_state.server_id,
                authenticated_principal=identity_scope,
                idempotency_key=idempotency_key,
                configuration_plan=configuration_plan)
        except DeploymentOperationError as exc:
            raise ServiceCenterError(exc.code, str(exc), exc.status) from None
        except DeploymentError as exc:
            raise ServiceCenterError(exc.code, str(exc), 409) from None
        except ValueError as exc:
            raise ServiceCenterError(str(exc), '配置已变化，请刷新后重试。', 409) from None
        self.start_deployment_worker()
        self._deployment_wake.set()
        return operation

    def uninstall_user_deployment(self, instance, payload, *, idempotency_key,
                                  identity_scope="server-admin"):
        from contextlib import nullcontext
        installation = self._user_deployment_components()
        if not callable(getattr(self.runtime, "retire_instance_containers", None)):
            raise ServiceCenterError("runtime_unavailable", "容器生命周期尚未配置", 503)
        operations = DeploymentOperationManager(self.repository)
        # Transfer ownership only after the failed executor has released its
        # cross-process lock. A delayed old executor then sees its terminal DOP.
        retry = payload.get("retry_of") if isinstance(payload, dict) else None
        try:
            with (installation.user_operation_lock(retry) if retry else nullcontext()):
                operation = operations.uninstall(instance, payload,
                    server_profile_id=self.task_state.server_id,
                    authenticated_principal=identity_scope, idempotency_key=idempotency_key)
        except RuntimeLockBusy:
            raise ServiceCenterError("deployment_removal_executor_busy", "旧卸载操作正在收尾，请稍后重试。", 409) from None
        except DeploymentOperationError as exc:
            raise ServiceCenterError(exc.code, str(exc), exc.status) from None
        except (ValueError, TaskStateError) as exc:
            raise ServiceCenterError(getattr(exc, 'code', str(exc)),
                                     "实例状态已变化或尚未停止，请刷新后重试卸载。", 409) from None
        self.start_deployment_worker()
        self._deployment_wake.set()
        return operation

    def _execute_user_removal_locked(self, operation):
        self.repository.check_removal_owner(operation)
        try:
            retire = getattr(self.runtime, "retire_instance_containers", None)
            if not callable(retire):
                raise TaskStateError("runtime_unavailable")
            retire(operation["deployment_id"])
            result = self.repository.finish_removal(operation, utc_now())
        except Exception as exc:
            code = getattr(exc, "code", None) or f"container_removal_{type(exc).__name__}"
            return self.repository.finish_removal(operation, utc_now(), error_code=str(code)[:160])
        try:
            self.registry.refresh()
        except Exception:
            logging.getLogger(__name__).exception("registry_refresh_after_removal_failed")
        return result

    def _execute_user_deployment(self, operation_id: str) -> dict[str, Any]:
        installation = self._user_deployment_components()
        with installation.user_operation_lock(operation_id):
            return self._execute_user_deployment_locked(operation_id, installation)

    def _execute_user_deployment_locked(self, operation_id, installation):
        operations = DeploymentOperationManager(self.repository)
        operation = operations.get(operation_id)
        if operation["payload"].get("action") == "uninstall":
            if operation["state"] in {"ready", "failed", "canceled"}:
                return operation
            if self._deployment_stop.is_set():
                raise _DeploymentWorkerStopping()
            return self._execute_user_removal_locked(operation)
        try:
            if operation["state"] in {"ready", "failed", "canceled"}:
                return operation
            if operation["state"] == "rollback":
                return self._rollback_user_deployment_operation(
                    operations, installation, operation_id,
                    error_class=operation["error_class"] or "recoverable",
                    error_code=operation["error_code"] or "deployment_interrupted",
                    error_message=operation["error_message"] or "部署中断",
                    canceled=operation["error_class"] == "canceled")
            operation = self._deployment_operation_checkpoint(
                operations, installation, operation["id"])
            if operation["state"] in {"failed", "canceled", "rollback"}:
                return operation
            if self.repository.get_configuration_context(operation_id) is not None:
                return self._execute_user_configuration_locked(operation, installation, operations)
            existing = self.repository.get_deployment(operation["deployment_id"])
            gpu_indices = self._user_gpu_indices(operation["payload"]["gpu_uuids"])
            if existing is None:
                plan = self.plan_user_deployment(self._plan_payload(operation["payload"]))
                if plan["operation"] != operation["payload"]:
                    raise DeploymentError(
                        "deployment_plan_changed", "资产、Runtime 或兼容关系已变化")
                if not plan["capacity"]["schedulable"]:
                    raise GPUBusyError(plan["capacity"]["reason"])
            else:
                binding = installation.get(operation["deployment_id"])
                if binding is not None and binding.get("operation_id") != operation["id"]:
                    raise DeploymentError("deployment_exists", "部署标识已有活动运行绑定")
                if binding is None:
                    self.deployments.resume_user(operation["payload"], gpu_indices)
            if operation["state"] == "accepted":
                operation = operations.transition(
                    operation["id"], "preparing_runtime",
                    recovery_cursor={"step": "runtime_preparation"})
            operation = self._deployment_operation_checkpoint(
                operations, installation, operation["id"])
            if operation["state"] in {"failed", "canceled", "rollback"}:
                return operation
            if operation["state"] == "preparing_runtime":
                def checkpoint():
                    if self._deployment_stop.is_set():
                        raise _DeploymentWorkerStopping()
                    if operations.get(operation_id)["state"] != "preparing_runtime":
                        raise DeploymentOperationError(
                            "deployment_operation_changed", "部署已取消或状态已变化", 409)
                installation.prepare_user_runtime(
                    operation_id, self.runtime_importer, checkpoint)
            operations.record_resource(
                operation["id"], kind="runtime-image",
                resource_id=operation["payload"]["runtime_profile"]["image_digest"],
                identity=operation["payload"]["runtime_profile"]["profile_digest"],
                created=False)
            for reference in (operation["payload"]["base_asset"],
                              operation["payload"]["vae_asset"]):
                if reference is not None:
                    operations.record_resource(
                        operation["id"], kind="asset",
                        resource_id=reference["asset_id"],
                        identity=reference["manifest_digest"], created=False)
            if existing is None:
                deployment = self.deployments.create_user(
                    operation["payload"], gpu_indices)
                operations.record_resource(
                    operation["id"], kind="deployment",
                    resource_id=deployment["id"], identity=deployment["incarnation"],
                    created=True)
            else:
                operations.record_resource(
                    operation["id"], kind="deployment",
                    resource_id=existing["id"], identity=existing["incarnation"],
                    created=False)
            operation = self._deployment_operation_checkpoint(
                operations, installation, operation["id"])
            if operation["state"] in {"failed", "canceled", "rollback"}:
                return operation
            if operation["state"] == "preparing_runtime":
                operation = operations.transition(
                    operation["id"], "creating_container",
                    recovery_cursor={"step": "runtime_verified_deployment_created"})
            binding = installation.commit_user(operation["id"])
            operation = self._deployment_operation_checkpoint(
                operations, installation, operation["id"])
            if operation["state"] in {"failed", "canceled", "rollback"}:
                return operation
            operations.record_resource(
                operation["id"], kind="container",
                resource_id=operation["deployment_id"],
                identity=binding["image_digest"], created=False)
            policy = self.runtime.install_instance(operation["deployment_id"])
            operation = self._deployment_operation_checkpoint(
                operations, installation, operation["id"])
            if operation["state"] in {"failed", "canceled", "rollback"}:
                return operation
            if operation["state"] == "creating_container":
                operation = operations.transition(
                    operation["id"], "starting_worker",
                    recovery_cursor={"step": "instance_policy_created"})
            if operation["state"] == "starting_worker":
                operation = operations.transition(
                    operation["id"], "health_check",
                    recovery_cursor={"step": "stopped_instance_ready"})
            self.registry.refresh()
            if operation["state"] == "health_check":
                instance_status = self._runtime_status(operation["deployment_id"])
                if instance_status.get("container_state") != "created":
                    raise TaskStateError("container_materialization_unconfirmed")
                result = {
                    "deployment": self.deployments.get(operation["deployment_id"]),
                    "instance": instance_status,
                    "service_started": False,
                    "container_materialized": True,
                    "policy_version": policy["version"],
                }
                operation = operations.transition(
                    operation["id"], "ready", result=result,
                    recovery_cursor={"step": "ready"})
            self.repository.add_audit(
                utc_now(), "user_deployment.create", operation["deployment_id"],
                {"operation_id": operation["id"], "plan_digest": operation["plan_digest"]})
            return operation
        except (_DeploymentWorkerStopping, RuntimeLockBusy):
            return operations.get(operation_id)
        except Exception as exc:
            current = operations.get(operation["id"])
            if current["state"] in {"ready", "failed", "canceled"}:
                return current
            canceled = current["state"] == "canceling"
            error_class, error_code, error_message = self._deployment_operation_error(exc)
            return self._rollback_user_deployment_operation(
                operations, installation, operation["id"],
                error_class=error_class, error_code=error_code,
                error_message=error_message, canceled=canceled)

    def _execute_user_configuration_locked(self, operation, installation, operations):
        """One bounded pass of the same locked DeploymentOperation runner.

        Reconciliation owns drain/launch/health; the runner never waits for a
        GPU or stops another installation. Durable context drives crash replay.
        """
        operation_id = operation['id']
        if operation['state'] == 'accepted':
            operation = operations.transition(operation_id, 'preparing_runtime',
                recovery_cursor={'step': 'configuration_runtime_preparation'})
        if operation['state'] == 'preparing_runtime':
            def checkpoint():
                if self._deployment_stop.is_set():
                    raise _DeploymentWorkerStopping()
                if operations.get(operation_id)['state'] != 'preparing_runtime':
                    raise DeploymentOperationError('deployment_operation_changed', '配置操作已变化', 409)
            installation.prepare_user_runtime(operation_id, self.runtime_importer, checkpoint)
            operation = operations.transition(operation_id, 'creating_container',
                recovery_cursor={'step': 'configuration_runtime_verified'})
        if operation['state'] == 'creating_container':
            reference = operation['payload']['vae_asset']
            if reference is not None:
                from .asset_compatibility import AssetCompatibilityManager
                AssetCompatibilityManager(self.repository).assess(
                    reference['asset_id'], operation['payload']['base_asset']['asset_id'])
            installation.commit_user(operation_id,
                gpu_indices=self._user_gpu_indices(operation['payload']['gpu_uuids']))
            operation = operations.transition(operation_id, 'starting_worker',
                recovery_cursor={'step': 'configuration_candidate_prepared'})
        operation = self._deployment_operation_checkpoint(operations, installation, operation_id)
        if operation['state'] in {'failed','canceled','rollback'}:
            return operation
        if operation['state'] == 'starting_worker':
            operation = operations.transition(operation_id, 'health_check',
                recovery_cursor={'step': 'configuration_converging'})
        if operation['state'] == 'health_check':
            self.runtime.authority.begin_configuration_operation(operation_id)
        return operations.get(operation_id)

    def get_deployment_operation(self, operation_id: str) -> dict[str, Any]:
        try:
            return DeploymentOperationManager(self.repository).get(operation_id)
        except DeploymentOperationError as exc:
            raise ServiceCenterError(exc.code, str(exc), exc.status) from None

    def list_deployment_operations(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            return DeploymentOperationManager(self.repository).list(max(1, min(limit, 500)))
        except DeploymentOperationError as exc:
            raise ServiceCenterError(exc.code, str(exc), exc.status) from None

    def cancel_deployment_operation(self, operation_id: str) -> dict[str, Any]:
        try:
            operation = DeploymentOperationManager(self.repository).cancel(operation_id)
            self._deployment_wake.set()
            return operation
        except DeploymentOperationError as exc:
            raise ServiceCenterError(exc.code, str(exc), exc.status) from None

    def append_model_transfer_chunk(self, transfer_id: str, file_id: str,
                                    offset: int, data: bytes) -> dict[str, Any]:
        try:
            return self._model_assets().append_upload_chunk(transfer_id, file_id, offset, data)
        except ModelAssetError as exc:
            raise ServiceCenterError(exc.code, exc.message, exc.status) from None

    def complete_model_transfer(self, transfer_id: str) -> dict[str, Any]:
        try:
            transfer = self._model_assets().complete_upload(transfer_id)
        except ModelAssetError as exc:
            raise ServiceCenterError(exc.code, exc.message, exc.status) from None
        self.repository.add_audit(utc_now(), "model_transfer.complete", transfer_id, {
            "asset_id": transfer["asset_id"]})
        return transfer

    def control_model_transfer(self, transfer_id: str, action: str) -> dict[str, Any]:
        manager = self._model_assets()
        method = {"pause": manager.pause, "resume": manager.resume,
                  "cancel": manager.cancel, "retry": manager.retry}.get(action)
        if method is None:
            raise ServiceCenterError("invalid_transfer_action", "模型传输动作无效")
        try:
            transfer = method(transfer_id)
        except ModelAssetError as exc:
            raise ServiceCenterError(exc.code, exc.message, exc.status) from None
        self.repository.add_audit(utc_now(), f"model_transfer.{action}", transfer_id, {})
        return transfer

    def archive_model_asset(self, asset_id: str, restore: bool = False) -> dict[str, Any]:
        try:
            asset = self._model_assets().restore(asset_id) if restore else self._model_assets().archive(asset_id)
        except ModelAssetError as exc:
            raise ServiceCenterError(exc.code, exc.message, exc.status) from None
        self.repository.add_audit(utc_now(), "model_asset.restore" if restore else "model_asset.archive",
                                  asset_id, {})
        return asset

    def _model_assets(self) -> ModelAssetManager:
        if self.model_assets is None:
            raise ServiceCenterError("model_asset_manager_unavailable", "模型资产库未启用", 503)
        return self.model_assets

    def list_deployments(self) -> list[dict[str, Any]]:
        rows = self.deployments.list() if self.deployments else []
        if self.runtime:
            projected = []
            installation = getattr(self.registry, "installation_runtime", None)
            for row in rows:
                status = self._runtime_status(row["id"])
                current_revision = (
                    self.repository.get_model_deployment_revision(
                        row["id"], row["current_config_revision"])
                    if row.get("current_config_revision") is not None else None)
                status["configuration_binding"] = ({key: current_revision[key]
                    for key in (
                        "deployment_id", "config_revision", "config_digest",
                        "runtime_profile_id", "runtime_profile_revision",
                        "runtime_profile_digest", "runtime_image_digest",
                        "base_asset_id", "base_asset_revision",
                        "base_asset_manifest_digest", "vae_asset_id",
                        "vae_asset_revision", "vae_asset_manifest_digest")}
                    if current_revision else None)
                policy = status.get("policy") or {}
                status["execution_binding"] = policy.get("binding")
                status["instance_settings"] = ({
                    "gpu_uuids": list(policy["gpus"]),
                    "sharing_mode": policy["sharing_mode"],
                    "external_reserve_mib": policy["external_reserve_mib"],
                    "base_mib": policy["base_mib"],
                    "task_mib": policy["task_mib"],
                    "residency": policy["residency"],
                    "residency_modes": list(residency_modes_for(
                        policy["binding"]["model_key"])),
                    "idle_minutes": policy["idle_seconds"] // 60,
                    "restart_recovery": bool(status.get("restart_recovery")),
                    "policy_version": status["version"],
                } if policy else None)
                if installation is not None:
                    try:
                        status["runtime_levels"] = installation.levels(row["id"])
                        status["runtime_level_error"] = None
                    except Exception as exc:
                        status["runtime_levels"] = {"installed": False, "env_checked": False,
                                                    "model_ready": False, "generated_tested": False}
                        status["runtime_level_error"] = getattr(exc, "code", "runtime_status_unavailable")
                if row.get("removal_operation_id"):
                    removal = self.repository.get_deployment_operation(row["removal_operation_id"])
                    status["removal_operation_state"] = removal["state"] if removal else "unknown"
                    status["removal_error"] = removal.get("error_message") if removal else "卸载记录不可用"
                    status["accepting_tasks"] = False
                projected.append({**row, **status})
            rows = projected
        return rows

    def _runtime_status(self, deployment_id):
        row = self.runtime.authority.get(deployment_id)
        deployment = self.repository.get_deployment(deployment_id)
        if row is None:
            return {"actual_state": "waiting_runtime", "runtime_last_error": "instance_policy_required",
                    "version": None, "restart_recovery": False,
                    "service_state": "installing", "accepting_tasks": False,
                    "desired_service_state": "stopped", "configuration_state": "applied",
                    "configuration_apply_mode": None, "container_state": "configured"}
        configuration = row.get("configuration_state", "applied")
        pending = row.get("pending_policy") is not None
        enabled = bool(deployment and deployment["enabled"])
        resume = bool(row.get("resume_after_apply"))
        accepting = enabled and not pending and configuration == "applied"
        with self.repository._connect() as db:
            intent = db.execute("""SELECT r.state,r.claim_id,r.epoch,c.state AS removal_state
                                 FROM runtime_intents r
                                 LEFT JOIN runtime_container_removals c USING(intent_id)
                                 WHERE r.instance_id=? ORDER BY r.generation DESC LIMIT 1""",
                                (deployment_id,)).fetchone()
        intent_state = intent[0] if intent else None
        if intent and intent["removal_state"] == "removed":
            container_state = "removed"
        elif intent_state is None:
            container_state = "configured"
        elif intent_state == "running":
            container_state = "running"
        elif intent_state in {"start_pending", "start_unknown"}:
            container_state = "starting"
        elif intent_state in {"stop_pending", "stop_unknown", "exit_unconfirmed"}:
            container_state = "stopping"
        elif intent_state in {"prepared", "domain_ready", "created", "created_unverified"}:
            container_state = "created"
        elif intent_state == "exited":
            container_state = "stopped"
        else:
            container_state = "error"
        if pending:
            service_state = "configuring"
        elif not enabled:
            claim = row["claim"]
            # Installed, stopped services retain their created container and
            # claim. Only its confirmed inactive identity is a stopped state;
            # a running/unconfirmed worker must remain visibly transitional.
            stopped_claim = (claim is not None and intent is not None
                and claim["state"] == "container_stopped" and not claim["registered"]
                and not claim.get("stop_reason") and row["status"] == "unloaded"
                and intent_state == "created" and container_state == "created"
                and intent["claim_id"] == claim["claim_id"] and intent["epoch"] == claim["epoch"])
            service_state = "stopping" if (
                claim is not None and not stopped_claim
                or container_state in {"starting", "running", "stopping"}) else "stopped"
        elif row["status"] in {"quarantined", "error", "legacy_unreconciled"} or container_state == "error":
            service_state = "error"
        elif container_state == "running" and row["claim"] is not None and row["claim"]["registered"]:
            service_state = "running"
        else:
            service_state = "starting"
        apply_mode = ({"restart_pending": "restart", "replace_pending": "replace",
                       "applying": "apply"}.get(configuration))
        return {"actual_state": row["status"], "desired_state": row["desired_state"],
                "runtime_last_error": row["error_code"], "policy_version": row["version"],
                "policy": row["policy"], "backend": row["claim"]["backend"] if row["claim"] else None,
                "version": row["version"], "restart_recovery": bool(row["restart_recovery"]),
                "service_state": service_state, "accepting_tasks": accepting,
                "desired_service_state": "started" if enabled or resume else "stopped",
                "configuration_state": configuration, "configuration_apply_mode": apply_mode,
                "configuration_error": row.get("configuration_error"),
                "container_state": container_state}

    def configure_instance_policy(self, deployment_id, payload):
        if self.runtime is None:
            raise ServiceCenterError("runtime_unavailable", "运行控制未配置", 503)
        fields = {"version", "gpu_uuids", "sharing_mode", "external_reserve_mib",
                  "residency", "idle_minutes", "restart_recovery"}
        if type(payload) is not dict or set(payload) != fields:
            raise ServiceCenterError("invalid_instance_settings", "只允许实例运行设置和当前版本", 400)
        version, gpus = payload["version"], payload["gpu_uuids"]
        if (type(version) is not int or version < 1 or type(gpus) is not list or not 1 <= len(gpus) <= 16
                or any(type(item) is not str for item in gpus) or len(set(gpus)) != len(gpus)
                or type(payload["sharing_mode"]) is not str or payload["sharing_mode"] not in {"shared", "exclusive"}
                or type(payload["external_reserve_mib"]) is not int
                or payload["external_reserve_mib"] not in {2048, 4096, 8192, 12288, 16384, 24576, 32768}
                or type(payload["residency"]) is not str or payload["residency"] not in {"on_demand", "idle", "resident"}
                or type(payload["idle_minutes"]) is not int or not 0 <= payload["idle_minutes"] <= 1440
                or type(payload["restart_recovery"]) is not bool):
            raise ServiceCenterError("invalid_instance_settings", "实例运行设置无效", 400)
        allowed = set(getattr(self.runtime.scheduler, "allowed_uuids", ()))
        if any(gpu not in allowed for gpu in gpus):
            raise ServiceCenterError("invalid_gpu_binding", "GPU 必须来自服务器显式资源池", 400)
        try:
            current = self.runtime.authority.get(deployment_id)
            if current is None or current["policy"] is None:
                raise TaskStateError("instance_policy_required")
            if payload["residency"] not in residency_modes_for(
                    current["policy"]["binding"]["model_key"]):
                raise TaskStateError("residency_mode_unsupported", 409)
            policy = dict(current["policy"])
            policy.update(gpus=list(gpus), sharing_mode=payload["sharing_mode"],
                          external_reserve_mib=payload["external_reserve_mib"],
                          residency=payload["residency"], idle_seconds=payload["idle_minutes"] * 60)
            self.gpu_scheduler.validate_budget(policy)
            deployment_revision = None
            if policy != current["policy"]:
                deployment = self.repository.get_deployment(deployment_id)
                if (deployment is not None
                        and deployment["current_config_revision"] is not None):
                    if self.deployments is None:
                        raise TaskStateError("deployment_manager_unavailable")
                    try:
                        deployment_revision = self.deployments.next_policy_revision(
                            deployment_id, policy)
                    except DeploymentError as exc:
                        raise TaskStateError(exc.code) from None
            applied = self.runtime.authority.configure(
                deployment_id, policy, expected_version=version,
                restart_recovery=payload["restart_recovery"],
                deployment_revision=deployment_revision)
        except TaskStateError as exc:
            raise ServiceCenterError(exc.code, "实例设置未提交", exc.status) from None
        except Exception as exc:
            code = str(exc) if str(exc) == "gpu_policy_unschedulable" else "gpu_telemetry_unavailable"
            raise ServiceCenterError(code, "实例预算无法在所选 GPU 上调度", 409) from None
        self.repository.add_audit(utc_now(), "instance_policy.configure", deployment_id, {
            "gpu_uuids": list(gpus), "sharing_mode": payload["sharing_mode"],
            "external_reserve_mib": payload["external_reserve_mib"],
            "residency": payload["residency"], "idle_minutes": payload["idle_minutes"],
            "restart_recovery": payload["restart_recovery"],
        })
        self.registry.refresh()
        status = self._runtime_status(deployment_id)
        status["configuration_apply_mode"] = status.get("configuration_apply_mode") or (
            "hot" if applied.get("configuration_state") == "applied" else None)
        return status

    def start_deployment(self, deployment_id, payload):
        if self.runtime is None:
            raise ServiceCenterError("runtime_unavailable", "运行控制未配置", 503)
        if type(payload) is not dict or set(payload) != {"version"} or type(payload["version"]) is not int:
            raise ServiceCenterError("invalid_instance_version", "启动需要当前实例版本", 400)
        try:
            self.runtime.authority.set_service(
                deployment_id, True, expected_version=payload["version"])
        except TaskStateError as exc:
            raise ServiceCenterError(exc.code, "服务启动意图未提交", exc.status) from None
        self.registry.refresh()
        self.repository.add_audit(utc_now(), "deployment.start", deployment_id, {})
        return self._runtime_status(deployment_id)

    def stop_deployment(self, deployment_id, payload):
        if self.runtime is None:
            raise ServiceCenterError("runtime_unavailable", "运行控制未配置", 503)
        if type(payload) is not dict or set(payload) != {"version"} or type(payload["version"]) is not int:
            raise ServiceCenterError("invalid_instance_version", "停止需要当前实例版本", 400)
        try:
            self.runtime.authority.set_service(
                deployment_id, False, expected_version=payload["version"])
        except TaskStateError as exc:
            raise ServiceCenterError(exc.code, "服务停止意图未提交", exc.status) from None
        self.registry.refresh()
        self.repository.add_audit(utc_now(), "deployment.stop", deployment_id, {})
        return self._runtime_status(deployment_id)

    def create_deployment(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.deployments is None:
            raise ServiceCenterError("deployment_manager_unavailable", "部署管理未启用", 503)
        try:
            deployment = self.deployments.create(payload)
            self.registry.refresh()
        except DeploymentError as exc:
            status = 409 if exc.code in {"deployment_exists", "deployment_busy"} else 400
            raise ServiceCenterError(exc.code, str(exc), status) from None
        self.repository.add_audit(utc_now(), "deployment.create", deployment["id"], {
            "catalog_key": deployment["catalog_key"], "revision": deployment["revision"],
            "gpu_indices": deployment["gpu_indices"],
        })
        return deployment

    def update_deployment(self, deployment_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.deployments is None:
            raise ServiceCenterError("deployment_manager_unavailable", "部署管理未启用", 503)
        active_tasks = self.repository.active_deployment_count(deployment_id)
        try:
            current = self.deployments.get(deployment_id)
            if current is None:
                raise DeploymentError("deployment_not_found", "部署实例不存在")
            runtime_change = {"enabled", "gpu_indices", "gpu_sharing_mode", "external_reserve_mib"} & set(payload)
            self.deployments.update(deployment_id, payload, active_tasks=active_tasks,
                                    validate_only=True)
            if runtime_change and self.retire_worker:
                self.retire_worker(deployment_id)
            deployment = self.deployments.update(
                deployment_id, payload,
                active_tasks=active_tasks,
            )
            self.registry.refresh()
            deployment = self.deployments.get(deployment_id) or deployment
        except TaskStateError as exc:
            raise ServiceCenterError(exc.code, "须先卸载并确认执行域退出", exc.status) from None
        except DeploymentError as exc:
            status = 404 if exc.code == "deployment_not_found" else (
                409 if exc.code in {"deployment_busy", "default_disabled"} else 400)
            raise ServiceCenterError(exc.code, str(exc), status) from None
        self.repository.add_audit(utc_now(), "deployment.configure", deployment_id, payload)
        return deployment

    def gpu_resources(self) -> dict[str, Any]:
        pool = list(self.deployments.allowed_gpus) if self.deployments else []
        observed = self.hardware_probe.gpu_snapshot() if self.hardware_probe else {
            "available": False, "error": "GPU probe unavailable", "items": [],
        }
        schedule = (self.gpu_scheduler.resource_snapshot(observation=observed) if self.gpu_scheduler else {
            "telemetry_available": False,
            "telemetry_error": "GPU scheduler unavailable",
            "gpus": [],
        })
        scheduled = {item["uuid"]: item for item in schedule["gpus"]}
        gpus = []
        for item in observed["items"]:
            index = item["index"]
            if item["uuid"] in scheduled:
                gpus.append({**item, **scheduled[item["uuid"]], "configured_for_mediacenter": True})
            else:
                gpus.append({**item, "configured_for_mediacenter": False,
                             "managed_task_active": False, "managed_model_warm": False,
                             "available_for_new_task": False,
                             "availability_reason": "not_configured_for_mediacenter",
                             "shared_capacity_mib": None, "system_reserve_mib": None})
        if not gpus:
            gpus = [{**item, "configured_for_mediacenter": True} for item in schedule["gpus"]]
        return {"configured_gpu_indices": pool,
                "configured_gpu_uuids": list(self.gpu_scheduler.allowed_uuids) if self.gpu_scheduler else [],
                "policy": "durable-base-and-task-reservations",
                "system_reserve_mib": GPULeaseScheduler.SYSTEM_RESERVE_MIB,
                "telemetry_available": bool(observed["available"] and schedule["telemetry_available"]),
                "telemetry_error": observed["error"] or schedule["telemetry_error"],
                "gpus": gpus}

    def hardware(self) -> dict[str, Any]:
        snapshot = self.hardware_probe.snapshot() if self.hardware_probe else {
            "captured_at": utc_now(), "status": "unavailable", "host": {},
            "cpu": {"available": False, "error": "Hardware probe unavailable"},
            "memory": {"available": False, "error": "Hardware probe unavailable"},
            "storage": {"available": False, "items": [], "error": "Hardware probe unavailable"},
            "gpu": {"available": False, "items": [], "error": "Hardware probe unavailable"},
        }
        services = self.list_services()
        snapshot["mediacenter"] = {
            "pid": os.getpid(),
            "process_uptime_seconds": int(time.monotonic() - self.started_monotonic),
            "configured_gpu_indices": list(self.deployments.allowed_gpus) if self.deployments else [],
            "services": [{"kind": item["kind"], "status": item["status"],
                          "enabled": item["enabled"],
                          "healthy_models": sum(1 for model in item["models"] if model["healthy"]),
                          "total_models": len(item["models"])} for item in services],
        }
        return snapshot

    def get_task(self, task_id: str) -> dict[str, Any]:
        task = self.repository.get_task(task_id)
        if task is None:
            raise ServiceCenterError("task_not_found", "任务不存在", 404)
        task.pop("cancel_requested", None)
        return task

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = self._bounded_int(limit, "limit", 1, 200)
        return [self.get_task(task["id"]) for task in self.repository.list_tasks(limit)]

    def _installation_runtime(self):
        value = getattr(self.registry, 'installation_runtime', None)
        if value is None or self.runtime is None:
            raise ServiceCenterError('runtime_validation_unavailable', '受管验证入口不可用', 409)
        return value

    def validate_instance(self, instance, payload, *, key):
        """Explicit action. Environment validation loads the model on its GPU."""
        from .container_releases import RuntimeContractError
        try:
            runtime = self._installation_runtime()
            if type(payload) is not dict or type(payload.get('kind')) is not str or payload.get('kind') not in {'env_checked','generated_tested'}:
                raise ServiceCenterError('validation_request_invalid','验证请求无效')
            payload=dict(payload)
            expected_binding=payload.pop('binding_digest',None)
            timeout_seconds=payload.pop('timeout_seconds',600)
            if type(timeout_seconds) is not int or not 10 <= timeout_seconds <= 3600:
                raise ServiceCenterError('validation_budget_invalid','验证时间预算无效')
            if expected_binding is not None and expected_binding != digest(runtime.get(instance)):
                raise ServiceCenterError('validation_binding_changed','验证对象已变更',409)
            if payload['kind']=='env_checked':
                if set(payload) != {'kind','version'}:
                    raise ServiceCenterError('validation_request_invalid','加载检查需当前实例版本')
                intent = runtime.validation_intent(instance,'env_checked',key,timeout_seconds=timeout_seconds)
                if expected_binding is not None and expected_binding != intent['binding_digest']:
                    raise ServiceCenterError('validation_binding_changed','验证对象已变更',409)
                self.runtime.authority.desire(instance,'loaded',expected_version=payload['version'],validation=intent)
            else:
                if set(payload) != {'kind','task'} or type(payload['task']) is not dict or payload['task'].get('model') != instance:
                    raise ServiceCenterError('validation_request_invalid','生成测试必须绑定当前实例任务')
                intent = runtime.validation_intent(instance,'generated_tested',key,timeout_seconds=timeout_seconds,
                                                   expected={'request_digest':digest(payload['task'])})
                if expected_binding is not None and expected_binding != intent['binding_digest']:
                    raise ServiceCenterError('validation_binding_changed','验证对象已变更',409)
                self._create_task(payload['task'],key,'validation',validation=intent)
            return runtime.validation(intent['validation_id'])
        except (RuntimeContractError,TaskStateError) as error:
            raise ServiceCenterError(error.code,error.code,409) from None

    def validation_target(self, instance):
        from .container_releases import RuntimeContractError
        try:
            runtime=self._installation_runtime();binding=runtime.get(instance)
            if binding is None:raise RuntimeContractError('runtime_installation_binding_required')
            state=self.runtime.authority.get(instance)
            if state is None:raise RuntimeContractError('instance_policy_required')
            return {'instance_id':instance,'binding_digest':digest(binding),'release_digest':binding['release_digest'],
                'image_digest':binding['image_digest'],'model_asset_id':binding['asset_id'],'model_asset_revision':binding['asset_revision'],
                'policy_version':state['version'],'policy':state['policy'],'actual_state':state['status'],
                'restart_recovery':bool(state['restart_recovery']),
                'claim_id':state['claim']['claim_id'] if state['claim'] else None}
        except RuntimeContractError as error:raise ServiceCenterError(error.code,error.code,409) from None

    def get_validation(self, identifier):
        from .container_releases import RuntimeContractError
        try: return self._installation_runtime().validation(identifier)
        except RuntimeContractError as error: raise ServiceCenterError(error.code,error.code,404) from None

    def cancel_validation(self, identifier, payload):
        from .container_releases import RuntimeContractError
        if type(payload) is not dict or set(payload) != {'version'}: raise ServiceCenterError('validation_request_invalid','需验证记录版本')
        try: return self._installation_runtime().cancel_validation(identifier,payload['version'])
        except RuntimeContractError as error: raise ServiceCenterError(error.code,error.code,409) from None

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        try:
            self.task_state.cancel(task_id)
        except TaskStateError as exc:
            raise ServiceCenterError(exc.code, exc.code, exc.status) from None
        return self.get_task(task_id)

    def retry_task(self, task_id: str, expected_version: int) -> dict[str, Any]:
        if type(expected_version) is not int or expected_version < 1:
            raise ServiceCenterError("invalid_task_version", "重试必须提供当前任务版本")
        try:
            task = self.get_task(task_id)
            if task["execution_mode"] == "local":
                return self._create_client_image_edit(ServiceKind(task["service"]), task["prompt"],
                    task["options"], task["inputs"], {}, "server-admin", None, retry=(task_id, expected_version))
            self.task_state.retry(task_id, expected_version)
        except TaskStateError as exc:
            raise ServiceCenterError(exc.code, exc.code, exc.status) from None
        return self.get_task(task_id)

    def overview(self) -> dict[str, Any]:
        services = self.list_services()
        counts = {status.value: 0 for status in TaskStatus}
        counts.update(self.repository.task_counts())
        return {"services_total": len(services),
                "services_online": sum(1 for service in services if service["available"]),
                "active_tasks": sum(counts[state.value] for state in ACTIVE_TASK_STATUSES),
                "completed_tasks": counts["succeeded"], "failed_tasks": counts["failed"],
                "task_counts": counts, "mode": "real", "kernel": "mediacenter"}

    def audit(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.repository.list_audit(self._bounded_int(limit, "limit", 1, 200))

    def artifact(self, relative: str) -> AuthorizedFile:
        try:
            return self.artifacts.authorize(relative)
        except (TaskStateError,OSError):
            raise ServiceCenterError("artifact_not_found", "产物不存在", 404) from None

    @staticmethod
    def _kind(value: Any) -> ServiceKind:
        try:
            return ServiceKind(value)
        except (TypeError, ValueError):
            raise ServiceCenterError("invalid_service", "service 必须是 image、video、speech 或 music") from None

    @staticmethod
    def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise ServiceCenterError(f"invalid_{name}", f"{name} 必须在 {minimum} 到 {maximum} 之间")
        return value

    @classmethod
    def _validate_finite(cls, value: Any) -> None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ServiceCenterError("invalid_options", "options 不能包含非有限数值")
        if isinstance(value, dict):
            for item in value.values():
                cls._validate_finite(item)
        elif isinstance(value, list):
            for item in value:
                cls._validate_finite(item)
