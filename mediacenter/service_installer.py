from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .model_assets import ModelAssetError, ModelAssetManager
from .model_deployments import DeploymentError, ModelDeploymentManager
from .repository import InstallationOwner, InstallationOwnershipError, Repository
from .container_releases import RuntimeContractError
from .task_state import TaskStateError


ACTIVE_STATES = {"preflight", "downloading", "verifying", "preparing", "checking"}
INSTALL_ID = re.compile(r"[a-z0-9][a-z0-9.-]{2,63}")


class ServiceInstallerError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ServiceInstaller:
    """Orchestrates a fixed recipe into an immutable asset and runnable deployment."""

    def __init__(self, repository: Repository, catalog_path: str | Path,
                 assets: ModelAssetManager, deployments: ModelDeploymentManager,
                 health_check: Callable[[str, str], tuple[bool, str]] | None = None,
                 *, poll_seconds: float = 0.25, prepare_timeout_seconds: int = 7200,
                 installation_runtime=None, runtime_importer=None, on_installed=None,
                 on_uninstall=None):
        self.repository = repository
        self.assets = assets
        self.deployments = deployments
        self.health_check = health_check
        self.installation_runtime = installation_runtime
        self.runtime_importer = runtime_importer
        self.on_installed = on_installed
        self.on_uninstall = on_uninstall
        self.poll_seconds = poll_seconds
        self.prepare_timeout_seconds = prepare_timeout_seconds
        self._lock = threading.Lock()
        self._running: set[str] = set()
        self._threads: dict[str, threading.Thread] = {}
        payload = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
        self.recipes = {
            item["catalog_key"]: item for item in payload.get("models", [])
            if isinstance(item, dict) and isinstance(item.get("service_recipe"), dict)
        }
        self._validate_recipes()
        self._recover_interrupted()

    def catalog(self) -> list[dict[str, Any]]:
        installations = self.repository.list_service_installations(500, catalog_only=True)
        deployments = self.repository.list_deployments()
        installed_by_key = {
            key: self._installed_deployment(entry, deployments)
            for key, entry in self.recipes.items()
        }
        result = []
        for key, entry in self.recipes.items():
            recipe = entry["service_recipe"]
            latest = next((item for item in installations if item["recipe_key"] == key), None)
            retained = next((item for item in deployments
                             if item["catalog_key"] == key
                             and item["model_id"] == entry["model_id"]
                             and item["revision"] == entry["recommended_revision"]
                             and item["install_state"] == "configured"), None)
            prerequisites = list(recipe.get("prerequisites", ()))
            missing = [dependency for dependency in prerequisites
                       if installed_by_key[dependency] is None]
            installed = installed_by_key[key] if not missing else None
            reusable_asset = self._matching_asset(entry) is not None
            authentication = recipe.get("authentication")
            authentication_state = None
            if authentication is not None:
                configured = self.assets.has_download_credential(authentication["host"])
                authentication_state = {
                    "provider": authentication["provider"],
                    "required": authentication["required"],
                    "configured": configured,
                    "needed": bool(authentication["required"] and not configured
                                   and not reusable_asset),
                    "terms_url": authentication["terms_url"],
                }
            runtime_error = None
            try: self._runtime_release(entry)
            except (RuntimeContractError, ServiceInstallerError) as exc: runtime_error = exc.code
            state = ("installed" if installed else "unavailable" if runtime_error else
                     latest["state"] if latest and latest["state"] not in {"ready", "canceled"}
                     else "available")
            result.append({
                "recipe_key": key, "kind": entry["kind"], "label": entry["label"],
                "description": recipe["description"], "model_id": entry["model_id"],
                "revision": entry["recommended_revision"], "license": entry["license"],
                "download_bytes": sum(int(item["byte_size"]) for item in recipe["files"]),
                "file_count": len(recipe["files"]),
                "required_vram_mib": entry["required_vram_mib"],
                "min_gpus": entry["min_gpus"], "max_gpus": entry["max_gpus"],
                "recommended_gpus": entry.get("recommended_gpus", []),
                "state": state, "installation_id": latest["id"] if latest else None,
                "runtime_available": runtime_error is None, "runtime_error": runtime_error,
                "levels": self.installation_runtime.levels(installed['id']) if installed else
                    {'installed': False, 'env_checked': False, 'model_ready': False, 'generated_tested': False},
                "runtime_description": "固定容器制品；环境检查与生成测试独立记录",
                "authentication": authentication_state,
                "deployment_id": installed["id"] if installed else None,
                "retained_deployment_id": retained["id"] if retained else None,
                "prerequisites": [{
                    "recipe_key": dependency,
                    "label": self.recipes[dependency]["label"],
                    "installed": installed_by_key[dependency] is not None,
                } for dependency in prerequisites],
                "prerequisites_ready": not missing,
            })
        return result

    def uninstall(self, recipe_key: str) -> dict[str, Any]:
        entry = self.recipes.get(recipe_key) if isinstance(recipe_key, str) else None
        if entry is None:
            raise ServiceInstallerError("service_recipe_not_found", "服务安装配方不存在", 404)
        installed = self._installed_deployment(entry, self.repository.list_deployments())
        if installed is None:
            raise ServiceInstallerError("service_not_installed", "该模型服务尚未安装", 409)
        messages = {
            "service_installation_busy": "服务安装状态仍在变化，请稍后重试",
            "service_tasks_active": "该模型仍有排队或执行中的任务，请先处理任务",
            "service_dependency_in_use": "其他已安装模型依赖该服务，不能卸载",
            "service_runtime_active": "该模型仍在运行，请先停止并等待容器退出",
            "deployment_identity_changed": "模型实例身份已经变化，请刷新后重试",
            "service_not_installed": "该模型服务尚未安装",
        }
        try:
            if self.on_uninstall is not None:
                self.on_uninstall(installed["id"])
            self.repository.assert_service_uninstallable(
                installed["id"], installed["incarnation"])
            self.repository.uninstall_service_binding(
                installed["id"], installed["incarnation"], utc_now())
        except (InstallationOwnershipError, RuntimeContractError, TaskStateError) as exc:
            raise ServiceInstallerError(
                exc.code, messages.get(exc.code, "模型服务运行容器尚未安全移除"), 409) from None
        if self.deployments.on_change is not None:
            try:
                self.deployments.on_change()
            except Exception:
                logging.getLogger(__name__).error(
                    "service_uninstall_registry_refresh_failed deployment_id=%s", installed["id"])
        return next(item for item in self.catalog() if item["recipe_key"] == recipe_key)

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        return [self.get(item['id']) for item in
                self.repository.list_service_installations(max(1, min(limit, 500)), catalog_only=True)]

    def get(self, installation_id: str) -> dict[str, Any]:
        item = self.repository.get_service_installation(installation_id)
        if item is None or installation_id.startswith('dop_'):
            raise ServiceInstallerError("service_installation_not_found", "服务安装任务不存在", 404)
        result = {key: value for key, value in item.items() if key != "recipe_snapshot"}
        with self.repository._connect() as db:
            image = db.execute('SELECT transfer_id,release_digest,image_digest,phase,received_bytes,error_code FROM runtime_image_transfers WHERE operation_id=? AND attempt_id=?', (installation_id, item['current_attempt_id'])).fetchone()
        result['runtime_image'] = dict(image) if image else None
        return result

    def _runtime_release(self, entry):
        if self.installation_runtime is None or self.runtime_importer is None:
            raise ServiceInstallerError('runtime_release_unavailable', '尚无批准且可用的运行镜像及入口', 409)
        try: return self.installation_runtime.resolve(entry)
        except RuntimeContractError as exc:
            raise ServiceInstallerError(exc.code, exc.code, 409) from None

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"recipe_key", "gpu_indices", "license_accepted", "deployment_id",
                   "label", "startup_policy", "gpu_sharing_mode", "external_reserve_mib",
                   "adopt_existing"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ServiceInstallerError("invalid_service_installation", "服务安装字段不受支持")
        recipe_key = payload.get("recipe_key")
        entry = self.recipes.get(recipe_key) if isinstance(recipe_key, str) else None
        if entry is None:
            raise ServiceInstallerError("service_recipe_not_found", "服务安装配方不存在", 404)
        deployments = self.repository.list_deployments()
        missing_prerequisites = [dependency for dependency in
                                 entry["service_recipe"].get("prerequisites", ())
                                 if self._installed_deployment(self.recipes[dependency], deployments) is None]
        if missing_prerequisites:
            labels = "、".join(self.recipes[key]["label"] for key in missing_prerequisites)
            raise ServiceInstallerError("service_prerequisite_missing",
                                        f"请先安装前置服务：{labels}", 409)
        if payload.get("license_accepted") is not True:
            raise ServiceInstallerError("license_not_accepted", "必须明确接受模型许可证后才能安装", 409)
        authentication = entry["service_recipe"].get("authentication")
        if (authentication is not None and authentication["required"]
                and self._matching_asset(entry) is None
                and not self.assets.has_download_credential(authentication["host"])):
            raise ServiceInstallerError(
                "source_authentication_required",
                "该模型仓库受限；请先接受官方许可并在客户端完成 Hugging Face 来源授权",
                409,
            )
        gpu_indices = payload.get("gpu_indices")
        if (not isinstance(gpu_indices, list) or not gpu_indices or any(
                not isinstance(index, int) or isinstance(index, bool) for index in gpu_indices)):
            raise ServiceInstallerError("invalid_gpu_indices", "必须选择有效 GPU")
        if len(set(gpu_indices)) != len(gpu_indices):
            raise ServiceInstallerError("invalid_gpu_indices", "GPU 不能重复选择")
        if not entry["min_gpus"] <= len(gpu_indices) <= entry["max_gpus"]:
            raise ServiceInstallerError(
                "invalid_gpu_count", f"该服务需要 {entry['min_gpus']} 到 {entry['max_gpus']} 张 GPU")
        if any(index not in self.deployments.allowed_gpus for index in gpu_indices):
            raise ServiceInstallerError("gpu_out_of_pool", "GPU 不在当前服务器可分配池")
        deployment_id = payload.get("deployment_id") or recipe_key
        if not isinstance(deployment_id, str) or not INSTALL_ID.fullmatch(deployment_id):
            raise ServiceInstallerError("invalid_deployment_id", "部署标识必须是小写安全标识")
        existing = self.repository.get_deployment(deployment_id)
        adopt_existing = payload.get("adopt_existing") is True
        if payload.get("adopt_existing") not in (None, True):
            raise ServiceInstallerError("invalid_service_installation", "接管标记无效")
        if existing is not None:
            if not adopt_existing:
                raise ServiceInstallerError("deployment_exists", "同名服务已经部署", 409)
            try:
                current_binding = self.installation_runtime.get(deployment_id)
            except RuntimeContractError as exc:
                if exc.code not in {"runtime_template_changed", "runtime_image_binding_changed"}:
                    raise ServiceInstallerError(exc.code, "现有运行绑定校验失败，不能自动接管", 409) from None
                current_binding = None
            if current_binding is not None:
                raise ServiceInstallerError("runtime_adoption_conflict", "现有部署不需要或不能执行运行环境接管", 409)
            if (existing["catalog_key"] != entry["catalog_key"] or existing["model_id"] != entry["model_id"]
                    or existing["revision"] != entry["recommended_revision"]):
                raise ServiceInstallerError("runtime_adoption_identity_changed", "现有部署与固定模型配方不一致", 409)
        elif adopt_existing:
            raise ServiceInstallerError("runtime_adoption_target_missing", "没有可接管的现有部署", 409)
        if any(item["recipe_key"] == recipe_key and item["state"] in ACTIVE_STATES | {"paused"}
               for item in self.repository.list_service_installations(500)):
            raise ServiceInstallerError("service_installation_busy", "该服务已有安装任务", 409)
        self._runtime_release(entry)
        template_reserve = self.installation_runtime.templates[recipe_key].data['resources']['external_reserve_mib']
        external_reserve = payload.get('external_reserve_mib', template_reserve)
        try:
            self.deployments._validate_startup_policy(payload.get('startup_policy', 'manual'))
            self.deployments._validate_sharing_mode(payload.get('gpu_sharing_mode', 'exclusive'))
            self.deployments._validate_external_reserve(external_reserve)
        except DeploymentError as exc:
            raise ServiceInstallerError(exc.code, str(exc), 400) from None
        total = sum(int(item["byte_size"]) for item in entry["service_recipe"]["files"])
        if self._matching_asset(entry) is None and self.assets.storage_summary()["free_bytes"] < total:
            raise ServiceInstallerError("insufficient_storage", "模型存储空间不足", 507)
        now = utc_now()
        installation_id = f"sin_{secrets.token_hex(8)}"
        steps = self._new_steps()
        options = {
            "gpu_indices": sorted(gpu_indices), "license_accepted": True,
            "deployment_id": deployment_id, "label": payload.get("label") or entry["label"],
            "startup_policy": payload.get("startup_policy", "manual"),
            "gpu_sharing_mode": payload.get("gpu_sharing_mode", "exclusive"),
            "external_reserve_mib": external_reserve,
            "adopt_existing": adopt_existing,
        }
        installation = {
            "id": installation_id, "recipe_key": recipe_key, "state": "preflight",
            "current_step": "preflight", "progress": 0.02, "deployment_id": None,
            "transfer_id": None, "asset_id": None, "options": options, "steps": steps,
            "error_code": None, "error_message": None, "created_at": now, "updated_at": now,
            "recipe_snapshot": entry,
        }
        try:
            self.repository.insert_service_installation(installation)
        except InstallationOwnershipError as exc:
            raise ServiceInstallerError(exc.code, "已有安装操作或资源归属发生变化", 409) from None
        self._spawn(installation_id)
        return self.get(installation_id)

    def control(self, installation_id: str, action: str) -> dict[str, Any]:
        item = self.get(installation_id)
        owner = (installation_id, item["current_attempt_id"], None)
        if action not in {"cancel", "pause", "resume", "retry"}:
            raise ServiceInstallerError("invalid_service_installation_action", "安装控制动作无效")
        try:
            if action == "retry":
                # A retry is a new persistent attempt, never an old resource cleanup.
                self.repository.retry_service_installation(installation_id, item["current_attempt_id"],
                                                           utc_now(), self._new_steps())
            else:
                self.assets.control_installation_transfer(owner, action)
        except InstallationOwnershipError as exc:
            raise ServiceInstallerError(exc.code, "安装状态或资源归属已变化；未修改其他操作的资源", 409) from None
        if action in {"resume", "retry"}:
            self._spawn(installation_id)
        return self.get(installation_id)

    def _spawn(self, installation_id: str) -> None:
        attempt_id = self.get(installation_id)["current_attempt_id"]
        with self._lock:
            for finished_id, finished in list(self._threads.items()):
                if finished.ident is not None and not finished.is_alive():
                    self._threads.pop(finished_id, None)
            if attempt_id in self._running:
                return
            self._running.add(attempt_id)
        thread = threading.Thread(target=self._run_guarded, args=(installation_id, attempt_id),
                                  name=f"service-install-{installation_id}", daemon=True)
        with self._lock:
            self._threads[attempt_id] = thread
        try:
            thread.start()
        except Exception:
            with self._lock:
                self._running.discard(attempt_id)
                self._threads.pop(attempt_id, None)
            raise

    def wait_runner(self, attempt_id: str, timeout: float) -> bool:
        """Wait for actual local execution cleanup, not just its public terminal state."""
        with self._lock:
            thread = self._threads.get(attempt_id)
        if thread is None:
            return True
        if thread is threading.current_thread() or thread.ident is None:
            return False
        thread.join(max(0.0, timeout))
        if thread.is_alive():
            return False
        with self._lock:
            if self._threads.get(attempt_id) is thread:
                self._threads.pop(attempt_id, None)
        return True

    def _run_guarded(self, installation_id: str, attempt_id: str | None = None) -> None:
        attempt_id = attempt_id or self.get(installation_id)["current_attempt_id"]
        owner = self.repository.claim_installation_runner(installation_id, attempt_id)
        if owner is None:
            with self._lock:
                self._running.discard(attempt_id)
            return
        try:
            self._run(installation_id, owner)
        except Exception as exc:
            error = exc if isinstance(exc, (ServiceInstallerError, ModelAssetError, DeploymentError, InstallationOwnershipError, RuntimeContractError)) else None
            code = getattr(error, "code", "service_installation_failed")
            message = getattr(error, "message", f"{type(exc).__name__}: {exc}")
            current = self.repository.get_service_installation(installation_id)
            try:
                self.repository.assert_installation_owner(owner)
                if current and current["deployment_id"]:
                    self.deployments.rollback_created(current["deployment_id"], owner=owner)
                if current:
                    self._set(installation_id, owner=owner, state="failed", step="failed",
                              progress=current["progress"], error_code=code,
                              error_message=str(message)[-1000:])
            except InstallationOwnershipError:
                pass
        finally:
            self.repository.release_installation_runner(owner)
            with self._lock:
                self._running.discard(attempt_id)
            current = self.repository.get_service_installation(installation_id)
            if current and current["current_attempt_id"] == attempt_id and current["state"] in ACTIVE_STATES:
                self._spawn(installation_id)  # Resume racing a paused runner's exit.

    def _run(self, installation_id: str, owner: InstallationOwner) -> None:
        item = self.repository.get_service_installation(installation_id)
        self.repository.assert_installation_owner(owner)
        entry = item["recipe_snapshot"]
        if not entry or entry != self.recipes.get(item["recipe_key"]):
            raise ServiceInstallerError("installation_recipe_changed", "固定安装配方已变化，不能继续旧安装")
        _, release = self._runtime_release(entry)
        images = self.installation_runtime.images
        image = images.begin(owner, release.digest)
        self._set(installation_id, owner=owner, state='downloading', step='environment', progress=0.03)
        while image['phase'] in {'queued', 'downloading'}:
            image = images.download(owner, image['transfer_id'])
            self.repository.assert_installation_owner(owner)
        if image['phase'] in {'paused', 'canceled'}: return
        if image['phase'] != 'ready':
            images.import_image(owner, image['transfer_id'], self.runtime_importer)
        self.assets.wake_installation_download(owner)
        recipe = entry["service_recipe"]
        self._step(installation_id, "preflight", "succeeded", owner=owner, state="preflight", progress=0.08)
        asset = self._matching_asset(entry)
        if asset is None:
            transfer = self.assets.get_transfer(item["transfer_id"]) if item["transfer_id"] else None
            if transfer is None:
                transfer = self.assets.create_catalog_download({
                    "display_name": entry["label"], "media_kind": entry["kind"],
                    "role": recipe["role"], "format": recipe["format"],
                    "source_type": recipe["source_type"], "source_ref": entry["model_id"],
                    "revision": entry["recommended_revision"],
                    "license_declared": entry["license"], "files": recipe["files"],
                }, owner=owner)
                self._set(installation_id, owner=owner, state="downloading", step="download", progress=0.1,
                          transfer_id=transfer["id"])
            transfer = self._wait_transfer(installation_id, transfer["id"], owner)
            if transfer["state"] in {"paused", "canceled"}:
                return
            if transfer["state"] != "succeeded" or not transfer["asset_id"]:
                raise ServiceInstallerError(transfer.get("error_code") or "model_download_failed",
                                            transfer.get("error_message") or "模型下载失败")
            asset = self.repository.get_model_asset(transfer["asset_id"])
        if asset is None:
            raise ServiceInstallerError("model_asset_missing", "模型资产发布后不可用")
        self.repository.record_installation_resource(owner, "asset", asset["id"], asset["manifest_digest"], False, utc_now())
        self._step(installation_id, "verify", "succeeded", owner=owner, state="preparing", progress=0.72,
                   asset_id=asset["id"])
        item = self.get(installation_id)
        deployment_id = item["options"]["deployment_id"]
        existing = self.repository.get_deployment(deployment_id)
        if existing is not None and not item["options"].get("adopt_existing"):
            raise ServiceInstallerError("installation_deployment_conflict", "同名部署已由其他操作创建，不能接管")
        if existing is None and item["options"].get("adopt_existing"):
            raise ServiceInstallerError("runtime_adoption_target_missing", "接管目标已不存在")
        if existing is None:
            defaults = [row for row in self.repository.list_deployments()
                        if row["kind"] == entry["kind"] and row["is_default"]]
            self.deployments.create({
                "deployment_id": deployment_id, "asset_id": asset["id"],
                "catalog_key": entry["catalog_key"], "label": item["options"]["label"],
                "gpu_indices": item["options"]["gpu_indices"], "enabled": True,
                "default": not defaults, "startup_policy": item["options"]["startup_policy"],
                "gpu_sharing_mode": item["options"]["gpu_sharing_mode"],
                "external_reserve_mib": item["options"]["external_reserve_mib"],
            }, owner=owner)
        else:
            self.repository.adopt_deployment_for_installation(owner, deployment_id, existing["incarnation"],
                entry["catalog_key"], asset["id"], utc_now())
        self._set(installation_id, owner=owner, state="preparing", step="environment", progress=0.76,
                  deployment_id=deployment_id)
        self.installation_runtime.commit(owner, deployment_id, image['transfer_id'], entry)
        if self.on_installed is not None:
            try: self.on_installed(deployment_id)
            except Exception:
                logging.getLogger(__name__).error('installation_runtime_projection_pending installation_id=%s', installation_id)
        if self.deployments.on_change is not None:
            try:
                self.deployments.on_change()  # Publish the committed activation, never enable before health.
            except Exception:
                # The database is authoritative; rebuilding the registry recovers this notification.
                # A post-commit observer failure must never roll back the completed installation.
                logging.getLogger(__name__).error("installation_registry_refresh_failed installation_id=%s", installation_id)

    def _wait_transfer(self, installation_id: str, transfer_id: str, owner: InstallationOwner) -> dict[str, Any]:
        while True:
            self.repository.assert_installation_owner(owner)
            transfer = self.assets.get_transfer(transfer_id)
            expected = int(transfer.get("expected_bytes") or 0)
            ratio = (int(transfer["received_bytes"]) / expected) if expected else 0
            state = "verifying" if transfer["state"] == "verifying" else "downloading"
            step = "verify" if state == "verifying" else "download"
            progress = 0.1 + min(1, ratio) * 0.55 if state == "downloading" else 0.68
            self._set(installation_id, owner=owner, state=state, step=step, progress=progress)
            if transfer["state"] in {"succeeded", "failed", "paused", "canceled"}:
                if transfer["state"] == "paused":
                    self._set(installation_id, owner=owner, state="paused", step="download", progress=progress)
                elif transfer["state"] == "canceled":
                    self._set(installation_id, owner=owner, state="canceled", step="canceled", progress=progress,
                              error_code="user_canceled", error_message="用户取消安装")
                return transfer
            time.sleep(self.poll_seconds)

    def _matching_asset(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        recipe = entry["service_recipe"]
        expected = {item["relative_path"]: item for item in recipe["files"]}
        for summary in self.repository.list_model_assets(state="ready", limit=100_000):
            if (summary["revision"] == entry["recommended_revision"] and
                    summary["media_kind"] == entry["kind"] and
                    summary["role"] == recipe["role"] and
                    summary["format"] == recipe["format"] and summary['license_declared'] == entry['license']):
                full = self.repository.get_model_asset(summary["id"])
                actual = ({item["relative_path"]: item for item in full["files"]}
                          if full else {})
                if full and len(actual) == len(expected) and all(
                        path in actual and
                        actual[path]["byte_size"] == manifest["byte_size"] and
                        actual[path]["sha256"].lower() == manifest["sha256"].lower()
                        for path, manifest in expected.items()) and self._asset_files_present(full, expected):
                    return full
        return None

    def _asset_files_present(self, asset: dict[str, Any], expected: dict[str, Any]) -> bool:
        # Full hashes are verified on publication, not rehashed on every UI poll.
        # Still reject lost/truncated shards instead of trusting database rows alone.
        try:
            root = (self.assets.storage_root / asset["storage_relpath"]).resolve()
            if self.assets.storage_root not in root.parents:
                return False
            for relative, manifest in expected.items():
                target = (root / relative).resolve()
                if root not in target.parents or not target.is_file() or target.stat().st_size != manifest["byte_size"]:
                    return False
            return True
        except (KeyError, OSError, ValueError):
            return False

    def _installed_deployment(self, entry: dict[str, Any],
                              deployments: list[dict[str, Any]],
                              visiting: frozenset[str] = frozenset(),
                              required_deployment_id: str | None = None) -> dict[str, Any] | None:
        key = entry["catalog_key"]
        if key in visiting:
            return None
        matching_asset = self._matching_asset(entry)
        if matching_asset is None:
            return None
        installed = next((item for item in deployments
                          if item["catalog_key"] == key
                          and (required_deployment_id is None or item["id"] == required_deployment_id)
                          and item["model_id"] == entry["model_id"]
                          and item["revision"] == entry["recommended_revision"]
                          and item["install_state"] == "ready"
                          and self.repository.deployment_installation_committed(item["id"], item["incarnation"])
                          and self._runtime_installed(entry, item)
                          and item.get("asset_id") == matching_asset["id"]), None)
        if installed is None:
            return None
        bindings = {item["dependency_key"]: item
                    for item in installed.get("dependencies", ())}
        for dependency_key in entry["service_recipe"].get("prerequisites", ()):
            binding = bindings.get(dependency_key)
            dependency = (self._installed_deployment(
                self.recipes[dependency_key], deployments, visiting | {key},
                binding["deployment_id"]) if binding is not None else None)
            if (dependency is None or binding is None or
                    binding["asset_id"] != dependency.get("asset_id") or
                    binding["revision"] != dependency["revision"]):
                return None
        return installed

    def _runtime_installed(self, entry: dict[str, Any], deployment: dict[str, Any]) -> bool:
        if self.installation_runtime is None: return False
        try: return self.installation_runtime.get(deployment['id']) is not None
        except RuntimeContractError: return False

    def _validate_recipes(self) -> None:
        for key, entry in self.recipes.items():
            recipe = entry["service_recipe"]
            authentication = recipe.get("authentication")
            if authentication is not None:
                valid_authentication = (isinstance(authentication, dict)
                    and set(authentication) == {"provider", "host", "required", "terms_url"}
                    and authentication["provider"] == "huggingface"
                    and authentication["required"] is True
                    and authentication["host"] == "huggingface.co")
                try:
                    terms = urlsplit(authentication["terms_url"])
                    terms_port = terms.port
                except (KeyError, TypeError, ValueError):
                    valid_authentication = False
                else:
                    valid_authentication = (valid_authentication and terms.scheme == "https"
                        and terms.hostname == authentication["host"]
                        and terms.username is None and terms.password is None
                        and terms_port in (None, 443))
                if not valid_authentication:
                    raise ValueError(f"service recipe has invalid authentication contract: {key}")
            prerequisites = recipe.get("prerequisites", [])
            if (not isinstance(prerequisites, list) or len(prerequisites) != len(set(prerequisites))
                    or any(not isinstance(item, str) or item == key or item not in self.recipes
                           for item in prerequisites)):
                raise ValueError(f"service recipe has invalid prerequisites: {key}")
            worker = entry.get("worker_contract")
            if worker is not None and (not isinstance(worker, dict) or set(worker) != {
                    "schema", "release", "target", "adapter_id", "module", "class",
                    "operation", "lora_families", "dependencies"}
                    or worker["schema"] != 1
                    or worker["dependencies"] != prerequisites
                    or worker["operation"] not in {"image.generate", "image.upscale", "video.generate",
                                                   "speech.generate", "music.generate"}
                    or not all(isinstance(worker[name], str) and worker[name] for name in
                               ("release", "target", "adapter_id", "module", "class"))
                    or not isinstance(worker["lora_families"], list)
                    or len(worker["lora_families"]) != len(set(worker["lora_families"]))):
                raise ValueError(f"service recipe has invalid Worker contract: {key}")
            files = recipe.get("files")
            if not isinstance(files, list) or not files:
                raise ValueError(f"service recipe has no immutable file manifest: {key}")
            paths = {item.get("relative_path") for item in files if isinstance(item, dict)}
            if len(paths) != len(files) or not set(entry.get("required_files", ())) <= paths:
                raise ValueError(f"service recipe does not cover required files: {key}")
            for item in files:
                if (not isinstance(item.get("url"), str) or
                        not isinstance(item.get("byte_size"), int) or item["byte_size"] <= 0 or
                        not isinstance(item.get("sha256"), str) or len(item["sha256"]) != 64):
                    raise ValueError(f"service recipe file is not immutable: {key}")
            if authentication is not None and any(
                    urlsplit(item["url"]).hostname != authentication["host"] for item in files):
                raise ValueError(f"service recipe authentication host does not cover files: {key}")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise ValueError("service recipe prerequisites contain a cycle")
            if key in visited:
                return
            visiting.add(key)
            for dependency in self.recipes[key]["service_recipe"].get("prerequisites", ()):
                visit(dependency)
            visiting.remove(key)
            visited.add(key)

        for key in self.recipes:
            visit(key)

    def _recover_interrupted(self) -> None:
        for item in self.repository.list_service_installations(10_000, catalog_only=True):
            if item["state"] in ACTIVE_STATES and item["current_attempt_id"]:
                if self.repository.recover_service_installation(item["id"], item["current_attempt_id"], utc_now()):
                    if item["deployment_id"]:
                        self.deployments.rollback_created(item["deployment_id"],
                            owner=(item["id"], item["current_attempt_id"], None))

    @staticmethod
    def _new_steps() -> list[dict[str, str]]:
        return [{"id": key, "label": label, "state": "pending"} for key, label in (
            ("preflight", "固定制品许可"), ("download", "模型下载"), ("verify", "文件校验"),
            ("environment", "镜像导入"), ("health", "安装绑定提交（非运行检查）"))]

    def _step(self, installation_id: str, step: str, step_state: str, *,
              owner: InstallationOwner, state: str, progress: float, **values: Any) -> None:
        current = self.get(installation_id)
        steps = []
        for item in current["steps"]:
            updated = dict(item)
            if item["id"] == step:
                updated["state"] = step_state
            elif step_state == "succeeded" and item["state"] == "pending":
                order = ["preflight", "download", "verify", "environment", "health"]
                if order.index(item["id"]) < order.index(step):
                    updated["state"] = "succeeded"
            steps.append(updated)
        self._set(installation_id, owner=owner, state=state, step=step, progress=progress,
                  steps=steps, **values)

    def _set(self, installation_id: str, *, state: str, step: str, progress: float,
             owner: InstallationOwner, reset_steps: bool = False, **values: Any) -> dict[str, Any]:
        update = {"state": state, "current_step": step,
                  "progress": max(0.0, min(1.0, progress)), "updated_at": utc_now(), **values}
        if reset_steps:
            update["steps"] = self._new_steps()
        stored = self.repository.update_service_installation(installation_id, update, owner=owner)
        if stored is None:
            raise ServiceInstallerError("service_installation_not_found", "服务安装任务不存在", 404)
        return stored
