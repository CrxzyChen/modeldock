from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .domain import ServiceKind
from .repository import InstallationOwner, Repository
from .runtime_profiles import RuntimeProfileError, RuntimeProfileManager
from .asset_compatibility import AssetCompatibilityManager, AssetCompatibilityError


DEPLOYMENT_ID = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
USER_PLAN_FIELDS = frozenset({
    "deployment_id", "base_asset_id", "vae_asset_id", "runtime_profile_id",
    "runtime_profile_revision", "gpu_uuids", "residency", "sharing_mode",
    "required_vram_mib", "license_accepted", "experimental_compatibility_accepted",
})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def deployment_manifest_digest(value: dict[str, Any]) -> str:
    contract = {key: value[key] for key in (
        "catalog_key", "kind", "model_id", "revision", "license",
        "required_files", "dependencies",
    )}
    data = json.dumps(contract, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def user_deployment_plan_digest(value: dict[str, Any]) -> str:
    contract = {key: value[key] for key in sorted(value) if key != "plan_digest"}
    data = json.dumps(contract, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def deployment_config_digest(value: dict[str, Any]) -> str:
    contract = {key: value[key] for key in sorted(value)
                if key not in {"config_digest", "created_at"}}
    data = json.dumps(contract, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


class DeploymentError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CatalogEntry:
    catalog_key: str
    kind: ServiceKind
    label: str
    model_id: str
    license: str
    recommended_revision: str
    required_files: tuple[str, ...]
    download_profile: str
    download_allow_patterns: tuple[str, ...]
    download_urls: tuple[str, ...]
    required_vram_mib: int
    min_gpus: int
    max_gpus: int
    recommended_gpus: tuple[int, ...]
    prerequisites: tuple[str, ...]

    def public(self) -> dict[str, Any]:
        return {
            "catalog_key": self.catalog_key,
            "kind": self.kind.value,
            "label": self.label,
            "model_id": self.model_id,
            "license": self.license,
            "recommended_revision": self.recommended_revision,
            "required_vram_mib": self.required_vram_mib,
            "min_gpus": self.min_gpus,
            "max_gpus": self.max_gpus,
            "recommended_gpus": list(self.recommended_gpus),
        }


class ModelDeploymentManager:
    def __init__(self, repository: Repository, catalog_path: str | Path,
                 data_root: str | Path, allowed_gpus: tuple[int, ...],
                 model_store_root: str | Path | None = None):
        self.repository = repository
        self.data_root = Path(data_root).resolve()
        self.model_store_root = Path(model_store_root or self.data_root / "model-store").resolve()
        self.allowed_gpus = tuple(sorted(set(allowed_gpus)))
        if not self.allowed_gpus or any(index < 0 for index in self.allowed_gpus):
            raise ValueError("allowed GPU pool must contain non-negative indices")
        self.catalog = self._load_catalog(catalog_path)
        self.on_change = None
        self._reconcile_resource_contracts()
        self._reconcile_dependencies()

    @staticmethod
    def _load_catalog(path: str | Path) -> dict[str, CatalogEntry]:
        catalog_path = Path(path).resolve()
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
        entries: dict[str, CatalogEntry] = {}
        for raw in payload.get("models", []):
            key = raw.get("catalog_key")
            retired = {"environment", "environment_packages", "module", "probe", "runtime_recipe"}
            if retired & set(raw):
                raise ValueError(f"catalog {key or '<unknown>'} contains retired host runtime fields")
            if not isinstance(key, str) or not DEPLOYMENT_ID.fullmatch(key) or key in entries:
                raise ValueError("model catalog requires unique lowercase catalog_key")
            revision = raw.get("recommended_revision")
            if not isinstance(revision, str) or not REVISION.fullmatch(revision):
                raise ValueError(f"catalog {key} requires a 40-character immutable revision")
            required = tuple(raw.get("required_files", ()))
            download_urls = tuple(raw.get("download_urls", ()))
            recommended = tuple(raw.get("recommended_gpus", ()))
            required_vram_mib = raw.get("required_vram_mib")
            min_gpus = int(raw.get("min_gpus", 1))
            max_gpus = int(raw.get("max_gpus", min_gpus))
            if (not required or not isinstance(required_vram_mib, int) or
                    isinstance(required_vram_mib, bool) or required_vram_mib <= 0 or
                    min_gpus < 1 or max_gpus < min_gpus or
                    (recommended and not min_gpus <= len(recommended) <= max_gpus)):
                raise ValueError(f"catalog {key} has an invalid deployment contract")
            if (any(not isinstance(url, str) or not url.startswith("https://github.com/xinntao/Real-ESRGAN/releases/download/")
                    for url in download_urls) or
                    (download_urls and {url.rsplit('/', 1)[-1] for url in download_urls} != set(required))):
                raise ValueError(f"catalog {key} has invalid direct download URLs")
            entries[key] = CatalogEntry(
                key, ServiceKind(raw["kind"]), raw["label"], raw["model_id"], raw["license"],
                revision, required,
                raw["download_profile"], tuple(raw.get("download_allow_patterns", ())),
                download_urls,
                required_vram_mib, min_gpus, max_gpus, recommended,
                tuple(raw.get("service_recipe", {}).get("prerequisites", ())),
            )
        for key, entry in entries.items():
            if (len(entry.prerequisites) != len(set(entry.prerequisites)) or
                    any(dependency == key or dependency not in entries
                        for dependency in entry.prerequisites)):
                raise ValueError(f"catalog {key} has invalid deployment prerequisites")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise ValueError("model catalog deployment prerequisites contain a cycle")
            if key in visited:
                return
            visiting.add(key)
            for dependency in entries[key].prerequisites:
                visit(dependency)
            visiting.remove(key)
            visited.add(key)

        for key in entries:
            visit(key)
        return entries

    def _reconcile_resource_contracts(self) -> None:
        declared: dict[str, int] = {
            key: entry.required_vram_mib for key, entry in self.catalog.items()
        }
        for row in self.repository.list_deployments():
            required = declared.get(row["catalog_key"], declared.get(row["id"]))
            if required is not None and row["required_vram_mib"] != required:
                self.repository.update_deployment(row["id"], {
                    "required_vram_mib": required, "updated_at": utc_now(),
                })

    def _reconcile_dependencies(self) -> None:
        for row in self.repository.list_deployments():
            entry = self.catalog.get(row["catalog_key"])
            if entry is None:
                continue
            actual_keys = {item["dependency_key"] for item in row["dependencies"]}
            expected_keys = set(entry.prerequisites)
            if actual_keys:
                if actual_keys != expected_keys:
                    raise ValueError(f"deployment {row['id']} dependency contract does not match catalog")
                continue
            if not expected_keys:
                continue
            try:
                dependencies = self._resolve_dependencies(entry, row["id"])
            except DeploymentError:
                continue
            contract = dict(row)
            contract["dependencies"] = dependencies
            now = utc_now()
            self.repository.set_deployment_dependencies(
                row["id"], dependencies, deployment_manifest_digest(contract), now)

    def public_catalog(self) -> list[dict[str, Any]]:
        return [entry.public() for entry in self.catalog.values()]

    def list(self) -> list[dict[str, Any]]:
        return [self._public(item) for item in self.repository.list_deployments()]

    def get(self, deployment_id: str) -> dict[str, Any] | None:
        item = self.repository.get_deployment(deployment_id)
        return self._public(item) if item else None

    def rollback_created(self, deployment_id: str, *, owner: InstallationOwner | None = None) -> bool:
        """Remove only a never-loaded deployment created by a failed install transaction."""
        removed = self.repository.delete_deployment_if_unloaded(deployment_id, owner=owner)
        if removed and self.on_change is not None:
            self.on_change()
        return removed

    def raw_specs(self) -> list[dict[str, Any]]:
        specs = []
        for row in self.repository.list_deployments():
            spec = dict(row)
            asset = (self.repository.get_model_asset(str(row["asset_id"]))
                     if row.get("asset_id") else None)
            spec["asset_state"] = asset["state"] if asset is not None else "migration_required"
            spec["asset_manifest_digest"] = asset["manifest_digest"] if asset is not None else None
            specs.append(spec)
        return specs

    def plan_user(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._plan_user(payload)

    def _plan_user(self, payload: dict[str, Any], *, existing=None) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != USER_PLAN_FIELDS:
            raise DeploymentError("invalid_user_deployment_plan", "用户模型部署计划字段无效")
        deployment_id = payload["deployment_id"]
        if not isinstance(deployment_id, str) or not DEPLOYMENT_ID.fullmatch(deployment_id):
            raise DeploymentError("invalid_deployment_id", "实例标识必须使用小写字母、数字、点或连字符")
        if existing is None and self.repository.get_deployment(deployment_id):
            raise DeploymentError("deployment_exists", "部署实例已存在")
        base = self._ready_user_asset(payload["base_asset_id"], "checkpoint")
        try:
            profile = RuntimeProfileManager(self.repository).get(
                payload["runtime_profile_id"], payload["runtime_profile_revision"])
        except RuntimeProfileError as exc:
            raise DeploymentError(exc.code, str(exc)) from None
        if (base["architecture_family"] not in profile["architecture_families"]
                or base["format"] not in profile["main_formats"]):
            raise DeploymentError("runtime_asset_incompatible", "Runtime 与基础模型架构或格式不兼容")
        residency = payload["residency"]
        if residency not in profile["residency_modes"]:
            raise DeploymentError("residency_mode_unsupported", "Runtime 不支持所选驻留策略")
        if payload["sharing_mode"] not in {"exclusive", "shared"}:
            raise DeploymentError("invalid_gpu_sharing_mode", "GPU 共享策略无效")
        required_vram = payload["required_vram_mib"]
        if (not isinstance(required_vram, int) or isinstance(required_vram, bool)
                or required_vram < profile["required_vram_mib"] or required_vram > 196608):
            raise DeploymentError("invalid_required_vram", "显存预算低于 Runtime 最低要求或超过上限")
        gpu_uuids = payload["gpu_uuids"]
        if (not isinstance(gpu_uuids, list) or not 1 <= len(gpu_uuids) <= 8
                or len(set(gpu_uuids)) != len(gpu_uuids)
                or any(not isinstance(value, str) or not value.startswith("GPU-") for value in gpu_uuids)):
            raise DeploymentError("invalid_gpu_binding", "GPU UUID 选择无效")
        if type(payload["license_accepted"]) is not bool or not payload["license_accepted"]:
            raise DeploymentError("license_confirmation_required", "必须确认模型许可证事实")
        if type(payload["experimental_compatibility_accepted"]) is not bool:
            raise DeploymentError("invalid_compatibility_confirmation", "兼容性确认无效")
        vae = None
        compatibility = None
        if payload["vae_asset_id"] is not None:
            if "vae" not in profile["optional_deployment_roles"]:
                raise DeploymentError("runtime_vae_unsupported", "所选运行环境不支持外部 VAE")
            vae = self._ready_user_asset(payload["vae_asset_id"], "vae")
            if vae["format"] != "safetensors":
                raise DeploymentError("runtime_vae_format_unsupported", "外部 VAE 必须为 Safetensors")
            try:
                compatibility = AssetCompatibilityManager(self.repository).assess(
                    vae["id"], base["id"], persist=False)
            except AssetCompatibilityError as exc:
                raise DeploymentError(exc.code, str(exc)) from None
            if compatibility["verdict"] not in {"exact", "compatible"}:
                if not (compatibility["verdict"] == "experimental"
                        and payload["experimental_compatibility_accepted"]):
                    raise DeploymentError("asset_incompatible", "所选 VAE 未通过基础模型兼容检查")
        operation = {
            "deployment_id": deployment_id,
            "plan_digest": "",
            "base_asset": self._asset_reference(base),
            "runtime_profile": {
                "profile_id": profile["profile_id"], "revision": profile["revision"],
                "image_digest": profile["image_digest"],
                "profile_digest": profile["profile_digest"],
            },
            "vae_asset": self._asset_reference(vae) if vae else None,
            "gpu_uuids": list(gpu_uuids), "residency": residency,
            "sharing_mode": payload["sharing_mode"], "required_vram_mib": required_vram,
            "confirmations": {
                "license_accepted": True,
                "experimental_compatibility_accepted": payload["experimental_compatibility_accepted"],
            },
        }
        operation["plan_digest"] = user_deployment_plan_digest(operation)
        return {"operation": operation, "base_asset": base, "vae_asset": vae,
                "runtime_profile": profile, "compatibility": compatibility,
                "effects": {"creates_container": True, "uploads_bytes": 0,
                            "deletes_assets": False, "starts_service": False}}

    def plan_user_configuration(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Pure revision proposal for the existing deployment operation controller.

        Expected configuration and policy versions are mandatory. Execution must
        revalidate them at the atomic admission boundary; preview owns nothing.
        """
        from .instance_policy import InstancePolicy
        if (type(payload) is not dict or set(payload) != USER_PLAN_FIELDS | {
                "expected_configuration", "policy_options"}):
            raise DeploymentError("invalid_user_configuration_plan", "模型配置计划字段无效")
        expected = payload["expected_configuration"]
        options = payload["policy_options"]
        if (type(expected) is not dict or set(expected) != {
                "config_revision", "config_digest", "policy_version"}
                or type(expected["config_revision"]) is not int or expected["config_revision"] < 1
                or type(expected["policy_version"]) is not int or expected["policy_version"] < 1
                or type(expected["config_digest"]) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", expected["config_digest"])):
            raise DeploymentError("invalid_expected_configuration", "必须提供当前配置与策略版本")
        if (type(options) is not dict or set(options) != {
                "external_reserve_mib", "idle_seconds", "restart_recovery"}
                or type(options["external_reserve_mib"]) is not int
                or options["external_reserve_mib"] not in {2048, 4096, 8192, 12288, 16384, 24576, 32768}
                or type(options["idle_seconds"]) is not int or not 0 <= options["idle_seconds"] <= 86400
                or type(options["restart_recovery"]) is not bool):
            raise DeploymentError("invalid_instance_settings", "配置策略选项无效")
        deployment_id = payload["deployment_id"]
        if type(deployment_id) is not str or not DEPLOYMENT_ID.fullmatch(deployment_id):
            raise DeploymentError("invalid_deployment_id", "实例标识无效")
        with self.repository._connect() as db:
            # One read snapshot; no compatibility publication, lease or head mutation.
            db.execute("BEGIN")
            deployment = db.execute("SELECT * FROM model_deployments WHERE id=?", (deployment_id,)).fetchone()
            raw_policy = db.execute("SELECT * FROM instance_policies WHERE instance_id=?", (deployment_id,)).fetchone()
            current = db.execute("SELECT * FROM model_deployment_revisions WHERE deployment_id=? AND config_revision=?",
                                 (deployment_id, expected["config_revision"])).fetchone()
            if (deployment is None or deployment["catalog_key"] != "sdxl-single-file"
                    or deployment["install_state"] != "ready" or current is None or raw_policy is None):
                raise DeploymentError("deployment_revision_unavailable", "实例尚无就绪的用户模型配置")
            policy = InstancePolicy._policy(raw_policy)
            if (deployment["pending_config_revision"] is not None
                    or policy["configuration_state"] != "applied" or policy["pending_policy"] is not None):
                raise DeploymentError("deployment_configuration_pending", "已有配置正在应用")
            if (deployment["current_config_revision"] != expected["config_revision"]
                    or current["config_digest"] != expected["config_digest"]
                    or policy["version"] != expected["policy_version"]):
                raise DeploymentError("deployment_config_revision_conflict", "配置已变化，请刷新后重新确认")
            if policy["policy"] is None:
                raise DeploymentError("instance_policy_required", "实例运行策略尚未就绪")
            current = self.repository._model_deployment_revision(current)
            previous = policy["policy"]
            if InstancePolicy.deployment_binding(db, deployment) != previous["binding"]:
                raise DeploymentError("deployment_binding_mismatch", "当前实例绑定身份不一致")
            next_revision = int(db.execute(
                "SELECT MAX(config_revision)+1 FROM model_deployment_revisions WHERE deployment_id=?",
                (deployment_id,)).fetchone()[0])
        plan = self._plan_user({key: payload[key] for key in USER_PLAN_FIELDS}, existing=deployment)
        base, profile, vae = plan["base_asset"], plan["runtime_profile"], plan["vae_asset"]
        candidate = dict(current, config_revision=next_revision,
            runtime_profile_id=profile["profile_id"], runtime_profile_revision=profile["revision"],
            runtime_profile_digest=profile["profile_digest"], runtime_image_digest=profile["image_digest"],
            base_asset_id=base["id"], base_asset_revision=base["revision"],
            base_asset_manifest_digest=base["manifest_digest"],
            vae_asset_id=vae["id"] if vae else None, vae_asset_revision=vae["revision"] if vae else None,
            vae_asset_manifest_digest=vae["manifest_digest"] if vae else None,
            gpu_uuids=list(payload["gpu_uuids"]), required_vram_mib=payload["required_vram_mib"],
            sharing_mode=payload["sharing_mode"], residency=payload["residency"],
            external_reserve_mib=options["external_reserve_mib"], idle_seconds=options["idle_seconds"],
            license_confirmation={"accepted": True, "licenses": sorted({base["license_declared"],
                *([vae["license_declared"]] if vae else [])})},
            experimental_compatibility_accepted=payload["experimental_compatibility_accepted"],
            desired_state="running" if deployment["enabled"] else "stopped", created_at=utc_now())
        candidate["config_digest"] = deployment_config_digest(candidate)
        compared = set(candidate) - {"config_revision", "config_digest", "created_at", "desired_state"}
        changed = sorted(key for key in compared if candidate[key] != current[key])
        if not changed and options["restart_recovery"] == policy["restart_recovery"]:
            raise DeploymentError("deployment_configuration_unchanged", "配置没有变化")
        structural = set(changed) - {"residency", "idle_seconds", "license_confirmation",
                                     "experimental_compatibility_accepted"}
        plan["operation"].update(expected_configuration=dict(expected), policy_options=dict(options))
        plan["operation"]["plan_digest"] = user_deployment_plan_digest(plan["operation"])
        plan["configuration_revision"] = candidate
        model_path = (self.model_store_root / base["storage_relpath"]).resolve()
        if self.model_store_root not in model_path.parents:
            raise DeploymentError("path_escape", "部署资产路径越界")
        target = {key: deployment[key] for key in self.repository.CONFIGURATION_DEPLOYMENT_FIELDS}
        target.update(asset_id=base["id"], model_id=f"user/{base['id']}", revision=base["revision"],
                      license=base["license_declared"], model_path=str(model_path),
                      required_files_json=json.dumps([item["relative_path"] for item in base["files"]]),
                      required_vram_mib=candidate["required_vram_mib"],
                      gpu_sharing_mode=candidate["sharing_mode"],
                      external_reserve_mib=candidate["external_reserve_mib"])
        target["manifest_digest"] = deployment_manifest_digest({**dict(deployment), **target,
            "required_files": json.loads(target["required_files_json"]), "dependencies": []})
        # Private plan data; the HTTP preview exposes only operation/effects.
        plan["configuration_deployment"] = target
        plan["effects"] = {
            "updates_existing": True, "changed_fields": changed, "requires_restart": bool(structural),
            "preserves_desired_state": True, "deletes_assets": False, "uploads_bytes": 0,
            "rebuilds_runtime_image": False,
        }
        return plan

    def create_user(self, operation: dict[str, Any], gpu_indices: list[int]) -> dict[str, Any]:
        if operation.get("plan_digest") != user_deployment_plan_digest(operation):
            raise DeploymentError("deployment_plan_changed", "部署计划摘要不匹配")
        replay = self.plan_user({
            "deployment_id": operation["deployment_id"],
            "base_asset_id": operation["base_asset"]["asset_id"],
            "vae_asset_id": operation["vae_asset"]["asset_id"] if operation["vae_asset"] else None,
            "runtime_profile_id": operation["runtime_profile"]["profile_id"],
            "runtime_profile_revision": operation["runtime_profile"]["revision"],
            "gpu_uuids": operation["gpu_uuids"], "residency": operation["residency"],
            "sharing_mode": operation["sharing_mode"],
            "required_vram_mib": operation["required_vram_mib"],
            "license_accepted": operation["confirmations"]["license_accepted"],
            "experimental_compatibility_accepted": operation["confirmations"]["experimental_compatibility_accepted"],
        })
        if replay["operation"] != operation:
            raise DeploymentError("deployment_plan_changed", "资产、Runtime 或兼容关系已变化")
        if (not isinstance(gpu_indices, list) or len(gpu_indices) != len(operation["gpu_uuids"])
                or any(index not in self.allowed_gpus for index in gpu_indices)):
            raise DeploymentError("gpu_out_of_pool", "GPU 不在服务器显式资源池")
        base, profile, vae = replay["base_asset"], replay["runtime_profile"], replay["vae_asset"]
        if vae is not None:
            # Preview is read-only; the admitted installation records evidence.
            AssetCompatibilityManager(self.repository).assess(vae["id"], base["id"])
        now = utc_now()
        model_path = (self.model_store_root / base["storage_relpath"]).resolve()
        if self.model_store_root not in model_path.parents:
            raise DeploymentError("path_escape", "部署资产路径越界")
        deployment = {
            "id": operation["deployment_id"], "asset_id": base["id"],
            "catalog_key": "sdxl-single-file", "kind": "image",
            "label": base["display_name"], "model_id": f"user/{base['id']}",
            "revision": base["revision"], "enabled": False, "is_default": False,
            "gpu_indices": list(gpu_indices), "model_path": str(model_path),
            "license": base["license_declared"],
            "required_files": [item["relative_path"] for item in base["files"]],
            "dependencies": [], "required_vram_mib": operation["required_vram_mib"],
            "gpu_sharing_mode": operation["sharing_mode"], "external_reserve_mib": 8192,
            "warm_ttl_seconds": 0, "desired_state": "unloaded",
            "startup_policy": "manual", "actual_state": "unloaded",
            "runtime_updated_at": now, "install_state": "configured",
            "created_at": now, "updated_at": now,
        }
        deployment["manifest_digest"] = deployment_manifest_digest(deployment)
        revision = {
            "deployment_id": deployment["id"], "config_revision": 1,
            "runtime_profile_id": profile["profile_id"],
            "runtime_profile_revision": profile["revision"],
            "runtime_profile_digest": profile["profile_digest"],
            "runtime_image_digest": profile["image_digest"],
            "base_asset_id": base["id"], "base_asset_revision": base["revision"],
            "base_asset_manifest_digest": base["manifest_digest"],
            "vae_asset_id": vae["id"] if vae else None,
            "vae_asset_revision": vae["revision"] if vae else None,
            "vae_asset_manifest_digest": vae["manifest_digest"] if vae else None,
            "gpu_uuids": operation["gpu_uuids"],
            "required_vram_mib": operation["required_vram_mib"],
            "sharing_mode": operation["sharing_mode"], "residency": operation["residency"],
            "external_reserve_mib": deployment["external_reserve_mib"],
            "idle_seconds": 900 if operation["residency"] == "idle" else 0,
            "license_confirmation": {"accepted": True, "licenses": sorted({
                base["license_declared"], *([vae["license_declared"]] if vae else [])})},
            "experimental_compatibility_accepted": operation["confirmations"]["experimental_compatibility_accepted"],
            "desired_state": "stopped", "config_digest": "",
            "created_at": now,
        }
        revision["config_digest"] = deployment_config_digest(revision)
        return self._public(self.repository.insert_user_deployment(deployment, revision))

    def resume_user(self, operation: dict[str, Any], gpu_indices: list[int]) -> dict[str, Any]:
        """Verify that a stopped partial deployment is exactly reusable.

        A failed/canceled DeploymentOperation may leave the user's immutable
        instance configuration behind while its runtime binding and container
        are rolled back.  A retry is allowed to reuse only that exact
        configuration; it never adopts an arbitrary existing deployment.
        """
        if operation.get("plan_digest") != user_deployment_plan_digest(operation):
            raise DeploymentError("deployment_plan_changed", "部署计划摘要不匹配")
        deployment = self.repository.get_deployment(operation.get("deployment_id"))
        if (deployment is None or deployment["catalog_key"] != "sdxl-single-file"
                or deployment["enabled"] or deployment["install_state"] not in {"configured", "failed"}
                or deployment["desired_state"] != "unloaded"
                or deployment["actual_state"] not in {"unloaded", "error"}
                or deployment["current_config_revision"] is None
                or deployment["pending_config_revision"] is not None):
            raise DeploymentError("deployment_exists", "部署标识已由其他实例占用")
        revision = self.repository.get_model_deployment_revision(
            deployment["id"], deployment["current_config_revision"])
        if revision is None or revision["config_revision"] != 1:
            raise DeploymentError("deployment_retry_identity_changed", "待重试实例配置身份已变化")
        base = self._ready_user_asset(operation["base_asset"]["asset_id"], "checkpoint")
        if self._asset_reference(base) != operation["base_asset"]:
            raise DeploymentError("deployment_retry_identity_changed", "基础模型身份已变化")
        try:
            profile = RuntimeProfileManager(self.repository).get(
                operation["runtime_profile"]["profile_id"],
                operation["runtime_profile"]["revision"])
        except RuntimeProfileError as exc:
            raise DeploymentError(exc.code, str(exc)) from None
        profile_ref = {
            "profile_id": profile["profile_id"], "revision": profile["revision"],
            "image_digest": profile["image_digest"],
            "profile_digest": profile["profile_digest"],
        }
        if profile_ref != operation["runtime_profile"]:
            raise DeploymentError("deployment_retry_identity_changed", "Runtime 身份已变化")
        vae = None
        if operation["vae_asset"] is not None:
            if "vae" not in profile["optional_deployment_roles"]:
                raise DeploymentError("runtime_vae_unsupported", "所选运行环境不支持外部 VAE")
            vae = self._ready_user_asset(operation["vae_asset"]["asset_id"], "vae")
            if vae["format"] != "safetensors":
                raise DeploymentError("runtime_vae_format_unsupported", "外部 VAE 必须为 Safetensors")
            if self._asset_reference(vae) != operation["vae_asset"]:
                raise DeploymentError("deployment_retry_identity_changed", "VAE 身份已变化")
            try:
                compatibility = AssetCompatibilityManager(self.repository).assess(
                    vae["id"], base["id"])
            except AssetCompatibilityError as exc:
                raise DeploymentError(exc.code, str(exc)) from None
            allowed = {"exact", "compatible"}
            if operation["confirmations"]["experimental_compatibility_accepted"]:
                allowed.add("experimental")
            if compatibility["verdict"] not in allowed:
                raise DeploymentError("asset_incompatible", "所选 VAE 未通过基础模型兼容检查")
        expected_indices = list(gpu_indices)
        expected_revision = {
            "runtime_profile_id": profile["profile_id"],
            "runtime_profile_revision": profile["revision"],
            "runtime_profile_digest": profile["profile_digest"],
            "runtime_image_digest": profile["image_digest"],
            "base_asset_id": base["id"],
            "base_asset_revision": base["revision"],
            "base_asset_manifest_digest": base["manifest_digest"],
            "vae_asset_id": vae["id"] if vae else None,
            "vae_asset_revision": vae["revision"] if vae else None,
            "vae_asset_manifest_digest": vae["manifest_digest"] if vae else None,
            "gpu_uuids": list(operation["gpu_uuids"]),
            "required_vram_mib": operation["required_vram_mib"],
            "sharing_mode": operation["sharing_mode"],
            "residency": operation["residency"],
            "experimental_compatibility_accepted": operation["confirmations"][
                "experimental_compatibility_accepted"],
        }
        if (any(revision[key] != value for key, value in expected_revision.items())
                or deployment["asset_id"] != base["id"]
                or deployment["revision"] != base["revision"]
                or deployment["gpu_indices"] != expected_indices
                or deployment["required_vram_mib"] != operation["required_vram_mib"]
                or deployment["gpu_sharing_mode"] != operation["sharing_mode"]):
            raise DeploymentError("deployment_retry_identity_changed", "待重试实例与原部署计划不一致")
        return self._public(deployment)

    def next_policy_revision(self, deployment_id: str, policy: dict[str, Any]) -> dict[str, Any]:
        """Build the next immutable user deployment revision from one policy edit."""
        deployment = self.repository.get_deployment(deployment_id)
        if (deployment is None or deployment["catalog_key"] != "sdxl-single-file"
                or deployment["current_config_revision"] is None):
            raise DeploymentError("deployment_revision_unavailable",
                                  "该实例没有可版本化的用户模型配置")
        if deployment["pending_config_revision"] is not None:
            raise DeploymentError("deployment_configuration_pending", "已有配置正在应用")
        current = self.repository.get_model_deployment_revision(
            deployment_id, deployment["current_config_revision"])
        if current is None:
            raise DeploymentError("deployment_revision_unavailable", "当前配置版本不存在")
        value = dict(current)
        value.update(
            config_revision=self.repository.next_model_deployment_revision(deployment_id),
            gpu_uuids=list(policy["gpus"]), sharing_mode=policy["sharing_mode"],
            external_reserve_mib=policy["external_reserve_mib"],
            residency=policy["residency"], idle_seconds=policy["idle_seconds"],
            desired_state="running" if deployment["enabled"] else "stopped",
            created_at=utc_now(), config_digest="",
        )
        value["config_digest"] = deployment_config_digest(value)
        return value

    def _ready_user_asset(self, asset_id: Any, role: str) -> dict[str, Any]:
        if not isinstance(asset_id, str) or not asset_id.startswith("mdl_"):
            raise DeploymentError("model_asset_required", "模型资产引用无效")
        asset = self.repository.get_model_asset(asset_id)
        if asset is None or asset["state"] != "ready":
            raise DeploymentError("model_asset_not_ready", "模型资产尚未就绪")
        if (asset["role"] != role or asset["media_kind"] != "image"
                or asset["architecture_family"] != "sdxl"):
            raise DeploymentError("model_asset_incompatible", "模型资产角色或架构不兼容")
        return asset

    @staticmethod
    def _asset_reference(asset: dict[str, Any]) -> dict[str, Any]:
        return {"asset_id": asset["id"], "revision": asset["revision"],
                "manifest_digest": asset["manifest_digest"]}

    def create(self, payload: dict[str, Any], *, owner: InstallationOwner | None = None) -> dict[str, Any]:
        allowed = {"deployment_id", "asset_id", "catalog_key", "label", "gpu_indices",
                   "enabled", "default", "warm_ttl_seconds", "startup_policy",
                   "gpu_sharing_mode", "external_reserve_mib"}
        if not payload or set(payload) - allowed:
            raise DeploymentError("invalid_deployment", "部署字段不受支持")
        deployment_id = payload.get("deployment_id")
        catalog_key = payload.get("catalog_key")
        if not isinstance(deployment_id, str) or not DEPLOYMENT_ID.fullmatch(deployment_id):
            raise DeploymentError("invalid_deployment_id", "deployment_id 必须是小写安全标识")
        if self.repository.get_deployment(deployment_id):
            raise DeploymentError("deployment_exists", "部署实例已存在")
        entry = self.catalog.get(catalog_key) if isinstance(catalog_key, str) else None
        if entry is None:
            raise DeploymentError("catalog_not_found", "模型目录项不存在")
        asset_id = payload.get("asset_id")
        if not isinstance(asset_id, str) or not asset_id.startswith("mdl_"):
            raise DeploymentError("model_asset_required", "创建部署必须选择已就绪模型资产")
        asset = self.repository.get_model_asset(asset_id)
        if asset is None:
            raise DeploymentError("model_asset_not_found", "模型资产不存在")
        if asset["state"] != "ready":
            raise DeploymentError("model_asset_not_ready", "模型资产尚未就绪或已归档")
        if asset["media_kind"] != entry.kind.value:
            raise DeploymentError("model_asset_incompatible", "模型资产媒体分类与运行目录不兼容")
        if asset["revision"] != entry.recommended_revision:
            raise DeploymentError("model_asset_revision_mismatch", "模型资产版本与运行目录固定版本不一致")
        revision = asset["revision"]
        gpu_indices = payload.get("gpu_indices", list(entry.recommended_gpus))
        if not gpu_indices and "gpu_indices" not in payload:
            raise DeploymentError("gpu_selection_required", "该模型没有默认 GPU，必须从服务器资源池显式选择")
        indices = self._validate_gpus(gpu_indices, entry)
        label = payload.get("label", entry.label)
        if not isinstance(label, str) or not label.strip() or len(label) > 100:
            raise DeploymentError("invalid_label", "label 必须为1到100个字符")
        model_path = (self.model_store_root / asset["storage_relpath"]).resolve()
        if self.model_store_root not in model_path.parents:
            raise DeploymentError("path_escape", "部署路径越界")
        dependencies = self._resolve_dependencies(entry, deployment_id)
        now = utc_now()
        deployment = {
            "id": deployment_id, "asset_id": asset_id,
            "catalog_key": entry.catalog_key, "kind": entry.kind.value,
            "label": label.strip(), "model_id": entry.model_id, "revision": revision,
            "enabled": bool(payload.get("enabled", False)),
            "is_default": bool(payload.get("default", False)), "gpu_indices": indices,
            "model_path": str(model_path), "license": entry.license,
            "required_files": list(entry.required_files),
            "dependencies": dependencies,
            "required_vram_mib": entry.required_vram_mib,
            "gpu_sharing_mode": self._validate_sharing_mode(payload.get("gpu_sharing_mode", "exclusive")),
            "external_reserve_mib": self._validate_external_reserve(payload.get("external_reserve_mib", 8192)),
            "warm_ttl_seconds": self._validate_warm_ttl(payload.get("warm_ttl_seconds", 0)),
            "desired_state": "unloaded",
            "startup_policy": self._validate_startup_policy(payload.get("startup_policy", "manual")),
            "actual_state": "unloaded", "runtime_last_error": None, "runtime_updated_at": now,
            "install_state": "configured", "last_error": None,
            "created_at": now, "updated_at": now,
        }
        deployment["manifest_digest"] = deployment_manifest_digest(deployment)
        if owner is not None:
            # Do not expose an environment-ready but unverified installation to
            # the scheduler. Activation commits with installation success.
            deployment.update(enabled=False, is_default=False, startup_policy="manual")
        self.repository.insert_deployment(deployment, owner=owner)
        return self.get(deployment_id) or {}

    def update(self, deployment_id: str, payload: dict[str, Any], active_tasks: int = 0,
               *, validate_only: bool = False) -> dict[str, Any]:
        allowed = {"label", "enabled", "default", "gpu_indices", "warm_ttl_seconds", "startup_policy",
                   "gpu_sharing_mode", "external_reserve_mib"}
        if not payload or set(payload) - allowed:
            raise DeploymentError("invalid_deployment_update", "部署更新字段不受支持")
        current = self.repository.get_deployment(deployment_id)
        if current is None:
            raise DeploymentError("deployment_not_found", "部署实例不存在")
        if (current['current_config_revision'] is not None
                and {'enabled', 'gpu_indices', 'warm_ttl_seconds', 'startup_policy',
                     'gpu_sharing_mode', 'external_reserve_mib'}.intersection(payload)):
            from .task_state import TaskStateError
            raise TaskStateError('use_versioned_instance_policy')
        if {"gpu_indices", "warm_ttl_seconds", "startup_policy", "gpu_sharing_mode", "external_reserve_mib"}.intersection(payload):
            with self.repository._connect() as db:
                configured = db.execute("SELECT 1 FROM instance_policies WHERE instance_id=? AND policy_json!='null'", (deployment_id,)).fetchone()
            if configured:
                from .task_state import TaskStateError
                raise TaskStateError("use_versioned_instance_policy")
        if active_tasks and ({"enabled", "gpu_indices", "warm_ttl_seconds", "startup_policy",
                              "gpu_sharing_mode", "external_reserve_mib"} & set(payload)):
            raise DeploymentError("deployment_busy", "部署有运行中任务，不能修改启停、GPU或常驻时间")
        if not self.repository.deployment_installation_committed(deployment_id, current["incarnation"]):
            raise DeploymentError("installation_uncommitted", "安装事务尚未完成，不能修改该部署")
        values: dict[str, Any] = {"updated_at": utc_now()}
        if "label" in payload:
            label = payload["label"]
            if not isinstance(label, str) or not label.strip() or len(label) > 100:
                raise DeploymentError("invalid_label", "label 必须为1到100个字符")
            values["label"] = label.strip()
        if "enabled" in payload:
            if not isinstance(payload["enabled"], bool):
                raise DeploymentError("invalid_enabled", "enabled 必须是布尔值")
            if current["is_default"] and not payload["enabled"] and payload.get("default") is not False:
                raise DeploymentError("default_disabled", "停用默认部署时必须同时取消默认")
            if not payload["enabled"] and self.repository.deployment_dependency_references(deployment_id):
                raise DeploymentError("deployment_dependency_in_use", "部署仍被其他模型作为前置依赖，不能停用")
            values["enabled"] = payload["enabled"]
        if "default" in payload:
            if not isinstance(payload["default"], bool):
                raise DeploymentError("invalid_default", "default 必须是布尔值")
            if payload["default"] and not payload.get("enabled", current["enabled"]):
                raise DeploymentError("default_disabled", "默认部署必须启用")
            values["is_default"] = payload["default"]
        if "gpu_indices" in payload:
            entry = self.catalog.get(current["catalog_key"])
            if entry is not None:
                values["gpu_indices"] = self._validate_gpus(payload["gpu_indices"], entry)
            else:
                values["gpu_indices"] = self._validate_manifest_gpus(
                    payload["gpu_indices"], current["gpu_indices"])
        if "warm_ttl_seconds" in payload:
            values["warm_ttl_seconds"] = self._validate_warm_ttl(payload["warm_ttl_seconds"])
        if "startup_policy" in payload:
            values["startup_policy"] = self._validate_startup_policy(payload["startup_policy"])
        if "gpu_sharing_mode" in payload:
            values["gpu_sharing_mode"] = self._validate_sharing_mode(payload["gpu_sharing_mode"])
        if "external_reserve_mib" in payload:
            values["external_reserve_mib"] = self._validate_external_reserve(payload["external_reserve_mib"])
        if validate_only:
            return self._public(current)
        updated = self.repository.update_deployment(deployment_id, values)
        if updated is None:
            raise DeploymentError("deployment_not_found", "部署实例不存在")
        return self._public(updated)

    def set_desired_state(self, deployment_id: str, desired_state: str) -> dict[str, Any]:
        from .instance_policy import InstancePolicy
        from .task_state import TaskStateError
        try:
            row = InstancePolicy(self.repository).desire(deployment_id, desired_state)
        except TaskStateError as exc:
            raise DeploymentError(exc.code, "持久运行策略未就绪") from None
        current = self.get(deployment_id)
        return {**current, "desired_state": row["desired_state"], "actual_state": row["status"]}

    def mark_install_state(self, deployment_id: str, state: str,
                           error: str | None = None, *, incarnation: str | None = None,
                           owner: InstallationOwner | None = None) -> dict[str, Any]:
        if state not in {"configured", "installing", "ready", "failed"}:
            raise DeploymentError("invalid_install_state", "安装状态无效")
        updated = self.repository.update_deployment(deployment_id, {
            "install_state": state, "last_error": error, "updated_at": utc_now(),
        }, incarnation=incarnation, owner=owner)
        if updated is None:
            raise DeploymentError("deployment_not_found", "部署实例不存在")
        return self._public(updated)

    def _validate_gpus(self, value: Any, entry: CatalogEntry) -> list[int]:
        if (not isinstance(value, list) or not value or any(
                not isinstance(index, int) or isinstance(index, bool) for index in value)):
            raise DeploymentError("invalid_gpu_indices", "gpu_indices 必须是非空整数数组")
        indices = sorted(set(value))
        if len(indices) != len(value) or any(index not in self.allowed_gpus for index in indices):
            raise DeploymentError("gpu_out_of_pool", "GPU不在MediaCenter资源池或存在重复")
        if not entry.min_gpus <= len(indices) <= entry.max_gpus:
            raise DeploymentError("invalid_gpu_count",
                                  f"该模型需要 {entry.min_gpus} 到 {entry.max_gpus} 张GPU")
        return indices

    def _validate_manifest_gpus(self, value: Any, current: list[int]) -> list[int]:
        if (not isinstance(value, list) or not value or any(
                not isinstance(index, int) or isinstance(index, bool) for index in value)):
            raise DeploymentError("invalid_gpu_indices", "gpu_indices 必须是非空整数数组")
        indices = sorted(set(value))
        if len(indices) != len(value) or any(index not in self.allowed_gpus for index in indices):
            raise DeploymentError("gpu_out_of_pool", "GPU不在MediaCenter资源池或存在重复")
        if len(indices) != len(current):
            raise DeploymentError("invalid_gpu_count", "基础清单部署只能在保持GPU卡数时迁移")
        return indices

    def _resolve_dependencies(self, entry: CatalogEntry,
                              deployment_id: str) -> list[dict[str, Any]]:
        bindings: list[dict[str, Any]] = []
        deployments = self.repository.list_deployments()
        for dependency_key in entry.prerequisites:
            dependency_entry = self.catalog[dependency_key]
            candidates = []
            for candidate in deployments:
                if (candidate["id"] == deployment_id or
                        candidate["catalog_key"] != dependency_key or
                        candidate["revision"] != dependency_entry.recommended_revision or
                        candidate["install_state"] != "ready" or
                        not candidate.get("asset_id")):
                    continue
                asset = self.repository.get_model_asset(candidate["asset_id"])
                if asset is not None and asset["state"] == "ready" and asset["id"] == candidate["asset_id"]:
                    candidates.append(candidate)
            preferred = [candidate for candidate in candidates if candidate["is_default"]]
            if len(preferred) == 1:
                target = preferred[0]
            elif len(candidates) == 1:
                target = candidates[0]
            elif not candidates:
                raise DeploymentError("deployment_dependency_missing",
                                      f"缺少可用前置部署：{dependency_entry.label}")
            else:
                raise DeploymentError("deployment_dependency_ambiguous",
                                      f"前置部署不唯一，请先设置默认部署：{dependency_entry.label}")
            bindings.append({
                "dependency_key": dependency_key,
                "deployment_id": target["id"],
                "asset_id": target["asset_id"],
                "revision": target["revision"],
            })
        return bindings

    @staticmethod
    def _public(item: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in item.items()
                if key not in {"model_path", "required_files"}}

    @staticmethod
    def _validate_warm_ttl(value: Any) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value not in {0, 300, 900, 1800}:
            raise DeploymentError("invalid_warm_ttl", "warm_ttl_seconds 仅支持 0、300、900 或 1800")
        return value

    @staticmethod
    def _validate_startup_policy(value: Any) -> str:
        if value not in {"manual", "auto"}:
            raise DeploymentError("invalid_startup_policy", "startup_policy 必须是 manual 或 auto")
        return str(value)

    @staticmethod
    def _validate_sharing_mode(value: Any) -> str:
        if value not in {"exclusive", "shared"}:
            raise DeploymentError("invalid_gpu_sharing_mode", "gpu_sharing_mode 必须是 exclusive 或 shared")
        return str(value)

    @staticmethod
    def _validate_external_reserve(value: Any) -> int:
        allowed = {2048, 4096, 8192, 12288, 16384, 24576, 32768}
        if not isinstance(value, int) or isinstance(value, bool) or value not in allowed:
            raise DeploymentError("invalid_external_reserve", "external_reserve_mib 必须使用 2–32 GiB 预留档位")
        return value
