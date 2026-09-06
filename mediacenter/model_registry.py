"""Authoritative model registry for container-backed deployments."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .capabilities import capability_for
from .domain import ServiceKind

if TYPE_CHECKING:
    from .model_deployments import ModelDeploymentManager


def asset_file_manifest_digest(files: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda value: value["relative_path"]):
        digest.update(item["relative_path"].encode("utf-8")); digest.update(b"\0")
        digest.update(item["sha256"].encode("ascii")); digest.update(b"\0")
        digest.update(str(item["byte_size"]).encode("ascii"))
    return digest.hexdigest()


@dataclass
class ModelSpec:
    model_key: str
    kind: ServiceKind
    model_id: str
    label: str
    model_path: Path
    license: str
    revision: str
    manifest_digest: str
    required_files: tuple[str, ...]
    gpu_indices: tuple[int, ...]
    required_vram_mib: int
    gpu_sharing_mode: str
    external_reserve_mib: int
    default: bool
    catalog_key: str
    enabled: bool
    install_state: str
    asset_id: str | None
    asset_state: str | None
    asset_manifest_digest: str | None
    warm_ttl_seconds: int = 0
    desired_state: str = "unloaded"
    startup_policy: str = "manual"
    dependency_bindings: tuple[dict[str, str], ...] = ()
    installation_levels: Any = None
    _dependency_specs: dict[str, "ModelSpec"] = field(default_factory=dict, init=False, repr=False)

    def health(self, *, allow_disabled: bool = False) -> tuple[bool, str]:
        if not self.enabled and not allow_disabled:
            return False, "部署已停用"
        if self.install_state != "ready":
            return False, ("模型尚未完成安装" if self.install_state == "configured"
                           else f"安装状态: {self.install_state}")
        if self.installation_levels is None:
            return False, "容器安装权威不可用"
        try:
            levels = self.installation_levels(self.model_key)
        except Exception as exc:
            return False, getattr(exc, "code", "runtime_installation_unavailable")
        if levels is None or not levels["installed"]:
            return False, "runtime_installation_unavailable"
        intact, reason = self._asset_integrity()
        if not intact:
            return False, reason
        for binding in self.dependency_bindings:
            key = binding["dependency_key"]
            dependency = self._dependency_specs.get(key)
            if dependency is None:
                return False, f"前置部署缺失: {key}"
            if (dependency.model_key != binding["deployment_id"] or dependency.catalog_key != key
                    or dependency.asset_id != binding["asset_id"]
                    or dependency.revision != binding["revision"]):
                return False, f"前置部署绑定已漂移: {key}"
            # Runtime dependencies bind immutable assets/configuration. The
            # dependency service does not have to accept its own tasks.
            healthy, detail = dependency.health(allow_disabled=True)
            if not healthy:
                return False, f"前置部署不可用: {key} · {detail}"
        return True, "installed; runtime checks are separately reported"

    def _asset_integrity(self) -> tuple[bool, str]:
        if self.asset_state != "ready":
            return False, f"模型资产状态不可用: {self.asset_state or 'missing'}"
        manifest_path = self.model_path / "manifest.json"
        if not self.model_path.is_dir() or not manifest_path.is_file():
            return False, f"模型资产清单缺失: {self.model_path}"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            files = manifest["files"]
            if not isinstance(files, list) or not files or any(not isinstance(item, dict) for item in files):
                raise ValueError
            normalized = [{"relative_path": str(item["relative_path"]),
                           "sha256": str(item["sha256"]),
                           "byte_size": int(item["byte_size"])} for item in files]
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False, f"模型资产清单无效: {self.model_path}"
        expected = self.asset_manifest_digest
        if (manifest.get("asset_id") != self.asset_id or not expected
                or manifest.get("manifest_digest") != expected
                or asset_file_manifest_digest(normalized) != expected):
            return False, f"模型资产身份或清单摘要不匹配: {self.model_path}"
        declared = {item["relative_path"]: item for item in normalized}
        for relative in self.required_files:
            entry = declared.get(relative); target = (self.model_path / relative).resolve()
            if (entry is None or self.model_path not in target.parents or not target.is_file()
                    or target.stat().st_size != entry["byte_size"] or entry["byte_size"] <= 0):
                return False, f"模型必需文件缺失或大小不符: {relative}"
        return True, "ready"

    def runtime_dependencies(self) -> dict[str, dict[str, str]]:
        return {binding["dependency_key"]: {
                    "deployment_id": dependency.model_key,
                    "asset_id": str(dependency.asset_id), "revision": dependency.revision,
                    "model_path": str(dependency.model_path)}
                for binding in self.dependency_bindings
                if (dependency := self._dependency_specs.get(binding["dependency_key"])) is not None}

    def public(self) -> dict[str, Any]:
        healthy, reason = self.health()
        try:
            levels = self.installation_levels(self.model_key) if self.installation_levels else None
        except Exception:
            levels = {"installed": False, "env_checked": False,
                      "model_ready": False, "generated_tested": False}
        return {"kind": self.kind.value, "model_key": self.model_key, "runtime_levels": levels,
                "model_id": self.model_id, "label": self.label, "default": self.default,
                "provider": "mediacenter-kernel", "healthy": healthy, "health_reason": reason,
                "license": self.license, "revision": self.revision,
                "catalog_key": self.catalog_key, "gpu_indices": list(self.gpu_indices),
                "required_vram_mib": self.required_vram_mib,
                "gpu_sharing_mode": self.gpu_sharing_mode,
                "external_reserve_mib": self.external_reserve_mib,
                "enabled": self.enabled, "install_state": self.install_state,
                "asset_id": self.asset_id, "asset_state": self.asset_state,
                "warm_ttl_seconds": self.warm_ttl_seconds,
                "capabilities": capability_for(self.catalog_key, self.kind)}


class ModelRegistry:
    def __init__(self, deployment_manager: "ModelDeploymentManager",
                 installation_runtime=None):
        if deployment_manager is None:
            raise ValueError("container deployment manager is required")
        self.deployment_manager = deployment_manager
        self.installation_runtime = installation_runtime
        self.refresh()

    def refresh(self) -> None:
        specs: dict[str, ModelSpec] = {}; defaults: dict[ServiceKind, str] = {}
        for row in self.deployment_manager.raw_specs():
            kind, model_key = ServiceKind(row["kind"]), row["id"]
            if (not isinstance(model_key, str) or not model_key
                    or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-." for c in model_key)
                    or model_key in specs):
                raise ValueError("deployment registry requires unique lowercase ids")
            is_default = bool(row["is_default"])
            if is_default and kind in defaults:
                raise ValueError(f"multiple default models for {kind.value}")
            if is_default: defaults[kind] = model_key
            gpus = tuple(row["gpu_indices"]); vram = row["required_vram_mib"]
            if not gpus or any(not isinstance(index, int) or index < 0 for index in gpus):
                raise ValueError("model deployment requires valid gpu_indices")
            if not isinstance(vram, int) or isinstance(vram, bool) or vram <= 0:
                raise ValueError("model deployment has invalid required_vram_mib")
            if row["gpu_sharing_mode"] not in {"exclusive", "shared"}:
                raise ValueError("model deployment has invalid gpu_sharing_mode")
            if row["external_reserve_mib"] not in {2048,4096,8192,12288,16384,24576,32768}:
                raise ValueError("model deployment has invalid external_reserve_mib")
            ttl = row.get("warm_ttl_seconds", 0)
            if (not isinstance(ttl, int) or isinstance(ttl, bool) or ttl not in {0,300,900,1800}
                    or (kind != ServiceKind.VIDEO and ttl != 0)):
                raise ValueError("warm_ttl_seconds is only supported for video deployments")
            specs[model_key] = ModelSpec(
                model_key, kind, row["model_id"], row["label"], Path(row["model_path"]).resolve(),
                row["license"], row["revision"], row["manifest_digest"], tuple(row["required_files"]),
                gpus, vram, row["gpu_sharing_mode"], row["external_reserve_mib"], is_default,
                row["catalog_key"], bool(row["enabled"]), row["install_state"], row.get("asset_id"),
                row.get("asset_state"), row.get("asset_manifest_digest"), ttl,
                row.get("desired_state", "unloaded"), row.get("startup_policy", "manual"),
                tuple(row.get("dependencies", ())), self._runtime_levels)
        self._bind_dependencies(specs)
        for kind in {spec.kind for spec in specs.values()}:
            matches = [spec.model_key for spec in specs.values() if spec.kind == kind]
            if kind not in defaults:
                # Several installed/stopped user models need no implicit default.
                # Explicit task selection remains valid; an ambiguous implicit
                # request returns no model instead of breaking the whole registry.
                if len(matches) == 1:
                    defaults[kind] = matches[0]; specs[matches[0]].default = True
        self.specs, self.defaults, self.strict_gpu = specs, defaults, False

    def _runtime_levels(self, instance):
        return self.installation_runtime.levels(instance) if self.installation_runtime else None

    @staticmethod
    def _bind_dependencies(specs: dict[str, ModelSpec]) -> None:
        for spec in specs.values():
            keys: set[str] = set()
            for binding in spec.dependency_bindings:
                if (not isinstance(binding, dict)
                        or set(binding) != {"dependency_key", "deployment_id", "asset_id", "revision"}
                        or not all(isinstance(value, str) and value for value in binding.values())
                        or binding["dependency_key"] in keys):
                    raise ValueError(f"model deployment has invalid dependencies: {spec.model_key}")
                keys.add(binding["dependency_key"])
                if binding["deployment_id"] in specs:
                    spec._dependency_specs[binding["dependency_key"]] = specs[binding["deployment_id"]]
        visiting: set[str] = set(); visited: set[str] = set()
        def visit(key: str) -> None:
            if key in visiting: raise ValueError("model deployment dependencies contain a cycle")
            if key in visited: return
            visiting.add(key)
            for dependency in specs[key]._dependency_specs.values(): visit(dependency.model_key)
            visiting.remove(key); visited.add(key)
        for key in specs: visit(key)

    def get(self, kind: ServiceKind, model_key: str | None = None) -> ModelSpec | None:
        key = model_key or self.defaults.get(kind); spec = self.specs.get(key) if key else None
        return spec if spec and spec.kind == kind else None

    def for_kind(self, kind: ServiceKind) -> list[ModelSpec]:
        return [spec for spec in self.specs.values() if spec.kind == kind]

    def public(self, kind: ServiceKind | None = None) -> list[dict[str, Any]]:
        return [spec.public() for spec in (self.for_kind(kind) if kind else self.specs.values())]
