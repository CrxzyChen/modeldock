from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from .repository import Repository


PROFILE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?$")
IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
PROFILE_FIELDS = {
    "profile_id", "revision", "label", "image_digest", "worker_protocol",
    "architecture_families", "main_formats", "optional_deployment_roles",
    "task_roles", "loader", "trust_remote_code", "residency_modes",
    "required_vram_mib",
}
SUPPORTED_LOADERS = {
    "StableDiffusionXLPipeline.from_single_file": {
        "architecture_families": {"sdxl"},
        "main_formats": {"safetensors"},
    },
}
ALLOWED_RESIDENCY = {"on_demand", "idle", "resident"}
ALLOWED_DEPLOYMENT_ROLES = {"vae"}
ALLOWED_TASK_ROLES = {"lora"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_digest(value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class RuntimeProfileError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


class RuntimeProfileManager:
    """Versioned allow-list of immutable model loading capabilities."""

    def __init__(self, repository: Repository):
        self.repository = repository

    def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != PROFILE_FIELDS:
            raise RuntimeProfileError("invalid_runtime_profile", "Runtime Profile 字段必须精确匹配合同")
        profile_id = payload["profile_id"]
        revision = payload["revision"]
        label = payload["label"]
        image_digest = payload["image_digest"]
        worker_protocol = payload["worker_protocol"]
        loader = payload["loader"]
        required_vram_mib = payload["required_vram_mib"]
        if not isinstance(profile_id, str) or not PROFILE_ID.fullmatch(profile_id):
            raise RuntimeProfileError("invalid_runtime_profile_id", "Runtime Profile ID 无效")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
            raise RuntimeProfileError("invalid_runtime_profile_revision", "Runtime Profile revision 必须为正整数")
        if not isinstance(label, str) or not label.strip() or len(label.strip()) > 100:
            raise RuntimeProfileError("invalid_runtime_profile_label", "Runtime Profile 名称无效")
        if not isinstance(image_digest, str) or not IMAGE_DIGEST.fullmatch(image_digest):
            raise RuntimeProfileError("invalid_runtime_image_digest", "Runtime 镜像必须固定 sha256 digest")
        if worker_protocol != "mc.worker/1":
            raise RuntimeProfileError("unsupported_worker_protocol", "Worker 协议不受支持")
        if payload["trust_remote_code"] is not False:
            raise RuntimeProfileError("unsafe_runtime_profile", "Runtime 不允许 trust_remote_code")
        if loader not in SUPPORTED_LOADERS:
            raise RuntimeProfileError("unsupported_runtime_loader", "Runtime loader 不在受控列表")
        if (not isinstance(required_vram_mib, int) or isinstance(required_vram_mib, bool)
                or not 1024 <= required_vram_mib <= 196608):
            raise RuntimeProfileError("invalid_runtime_vram", "Runtime 显存预算无效")

        architecture_families = self._string_set(
            payload["architecture_families"], "architecture_families", 8)
        main_formats = self._string_set(payload["main_formats"], "main_formats", 8)
        optional_roles = self._string_set(
            payload["optional_deployment_roles"], "optional_deployment_roles", 8,
            allow_empty=True)
        task_roles = self._string_set(payload["task_roles"], "task_roles", 8, allow_empty=True)
        residency_modes = self._string_set(payload["residency_modes"], "residency_modes", 3)
        loader_contract = SUPPORTED_LOADERS[loader]
        if (set(architecture_families) != loader_contract["architecture_families"]
                or set(main_formats) != loader_contract["main_formats"]):
            raise RuntimeProfileError("runtime_loader_contract_mismatch",
                                      "Runtime loader 与资产家族或格式不匹配")
        if not set(optional_roles) <= ALLOWED_DEPLOYMENT_ROLES:
            raise RuntimeProfileError("unsupported_deployment_role", "Runtime 部署级资产角色不受支持")
        if not set(task_roles) <= ALLOWED_TASK_ROLES:
            raise RuntimeProfileError("unsupported_task_role", "Runtime 任务级资产角色不受支持")
        if set(residency_modes) != ALLOWED_RESIDENCY:
            raise RuntimeProfileError("invalid_residency_modes", "Runtime 必须明确支持三档驻留策略")

        contract = {
            "profile_id": profile_id,
            "revision": revision,
            "label": label.strip(),
            "image_digest": image_digest,
            "worker_protocol": worker_protocol,
            "architecture_families": architecture_families,
            "main_formats": main_formats,
            "optional_deployment_roles": optional_roles,
            "task_roles": task_roles,
            "loader": loader,
            "trust_remote_code": False,
            "residency_modes": residency_modes,
            "required_vram_mib": required_vram_mib,
        }
        stored = {**contract, "profile_digest": canonical_digest(contract), "created_at": utc_now()}
        try:
            disposition, value = self.repository.put_runtime_profile(stored)
        except ValueError as exc:
            if str(exc) == "runtime_profile_revision_conflict":
                raise RuntimeProfileError(str(exc), "相同 Runtime Profile revision 已存在不同合同", 409) from None
            raise
        return {**value, "disposition": disposition}

    def get(self, profile_id: str, revision: int) -> dict[str, Any]:
        value = self.repository.get_runtime_profile(profile_id, revision)
        if value is None:
            raise RuntimeProfileError("runtime_profile_not_found", "Runtime Profile 不存在", 404)
        return value

    def list(self) -> list[dict[str, Any]]:
        return self.repository.list_runtime_profiles()

    def compatible(self, asset: dict[str, Any]) -> list[dict[str, Any]]:
        if asset.get("state") != "ready":
            return []
        return [profile for profile in self.list()
                if asset.get("architecture_family") in profile["architecture_families"]
                and asset.get("format") in profile["main_formats"]]

    @staticmethod
    def sdxl_single_file(image_digest: str, *, required_vram_mib: int = 16384) -> dict[str, Any]:
        return {
            "profile_id": "sdxl-single-file",
            "revision": 1,
            "label": "SDXL 单文件 Runtime",
            "image_digest": image_digest,
            "worker_protocol": "mc.worker/1",
            "architecture_families": ["sdxl"],
            "main_formats": ["safetensors"],
            "optional_deployment_roles": ["vae"],
            "task_roles": ["lora"],
            "loader": "StableDiffusionXLPipeline.from_single_file",
            "trust_remote_code": False,
            "residency_modes": ["idle", "on_demand", "resident"],
            "required_vram_mib": required_vram_mib,
        }

    @staticmethod
    def _string_set(value: Any, field: str, maximum: int, *, allow_empty: bool = False) -> list[str]:
        if (not isinstance(value, list) or len(value) > maximum
                or (not value and not allow_empty)
                or any(not isinstance(item, str) or not item or len(item) > 80 for item in value)):
            raise RuntimeProfileError("invalid_runtime_profile", f"{field} 无效")
        normalized = sorted(set(value))
        if len(normalized) != len(value):
            raise RuntimeProfileError("invalid_runtime_profile", f"{field} 包含重复值")
        return normalized
