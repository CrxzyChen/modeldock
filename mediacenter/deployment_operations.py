from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Iterable

from .repository import Repository


SAFE_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
RESIDENCY = {"on_demand", "idle", "resident"}
SHARING = {"exclusive", "shared"}
OPERATION_FIELDS = {
    "deployment_id", "plan_digest", "base_asset", "runtime_profile", "vae_asset",
    "gpu_uuids", "residency", "sharing_mode", "required_vram_mib", "confirmations",
}
TRANSITIONS = {
    "planned": {"accepted", "canceled"},
    "accepted": {"preparing_runtime", "canceling", "rollback"},
    "preparing_runtime": {"creating_container", "canceling", "rollback"},
    "creating_container": {"starting_worker", "canceling", "rollback"},
    "starting_worker": {"loading_model", "health_check", "canceling", "rollback"},
    "loading_model": {"health_check", "canceling", "rollback"},
    "health_check": {"ready", "canceling", "rollback"},
    "canceling": {"canceled", "rollback"},
    "rollback": {"failed", "canceled"},
    "ready": set(), "canceled": set(), "failed": set(),
}
ERROR_CLASSES = {"recoverable", "non_recoverable", "canceled"}
RESOURCE_KINDS = {"runtime-image", "asset", "deployment", "container", "gpu-lease"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class DeploymentOperationError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


class DeploymentOperationManager:
    """Persistent import-to-deployment command identity and monotonic state."""

    route = "/api/v1/deployment-operations"

    def __init__(self, repository: Repository):
        self.repository = repository

    def create(self, payload: dict[str, Any], *, server_profile_id: str,
               authenticated_principal: str, idempotency_key: str,
               configuration_plan=None) -> dict[str, Any]:
        configuration_fields = {"expected_configuration", "policy_options"}
        updating = isinstance(payload, dict) and set(payload) == OPERATION_FIELDS | configuration_fields
        if not isinstance(payload, dict) or set(payload) != OPERATION_FIELDS and not updating:
            raise DeploymentOperationError("invalid_deployment_operation",
                                           "DeploymentOperation 字段必须精确匹配合同")
        for value, code in ((server_profile_id, "invalid_server_profile"),
                            (authenticated_principal, "invalid_principal"),
                            (idempotency_key, "invalid_idempotency_key")):
            if not isinstance(value, str) or not SAFE_TEXT.fullmatch(value):
                raise DeploymentOperationError(code, "命令身份字段无效")
        deployment_id = payload["deployment_id"]
        if not isinstance(deployment_id, str) or not SAFE_TEXT.fullmatch(deployment_id):
            raise DeploymentOperationError("invalid_deployment_id", "deployment_id 无效")
        if not isinstance(payload["plan_digest"], str) or not SHA256.fullmatch(payload["plan_digest"]):
            raise DeploymentOperationError("invalid_plan_digest", "部署计划摘要无效")
        base = self._asset_ref(payload["base_asset"], "base_asset")
        vae = self._asset_ref(payload["vae_asset"], "vae_asset", optional=True)
        runtime = self._runtime_ref(payload["runtime_profile"])
        gpu_uuids = payload["gpu_uuids"]
        if (not isinstance(gpu_uuids, list) or not gpu_uuids or len(gpu_uuids) > 8
                or any(not isinstance(item, str) or not SAFE_TEXT.fullmatch(item) for item in gpu_uuids)
                or len(set(gpu_uuids)) != len(gpu_uuids)):
            raise DeploymentOperationError("invalid_gpu_selection", "GPU UUID 选择无效")
        if payload["residency"] not in RESIDENCY:
            raise DeploymentOperationError("invalid_residency", "驻留策略无效")
        if payload["sharing_mode"] not in SHARING:
            raise DeploymentOperationError("invalid_sharing_mode", "GPU 共享策略无效")
        required_vram = payload["required_vram_mib"]
        if (not isinstance(required_vram, int) or isinstance(required_vram, bool)
                or not 1024 <= required_vram <= 196608):
            raise DeploymentOperationError("invalid_required_vram", "显存预算无效")
        confirmations = payload["confirmations"]
        if (not isinstance(confirmations, dict)
                or set(confirmations) != {"license_accepted", "experimental_compatibility_accepted"}
                or any(type(value) is not bool for value in confirmations.values())
                or not confirmations["license_accepted"]):
            raise DeploymentOperationError("confirmation_required", "许可证确认缺失")
        normalized = {
            "deployment_id": deployment_id,
            "plan_digest": payload["plan_digest"],
            "base_asset": base,
            "runtime_profile": runtime,
            "vae_asset": vae,
            "gpu_uuids": list(gpu_uuids),
            "residency": payload["residency"],
            "sharing_mode": payload["sharing_mode"],
            "required_vram_mib": required_vram,
            "confirmations": dict(confirmations),
        }
        if updating:
            if configuration_plan is None:
                raise DeploymentOperationError("configuration_handler_required", "配置更新执行器尚未配置", 503)
            expected, options = payload['expected_configuration'], payload['policy_options']
            if (type(expected) is not dict or set(expected) != {'config_revision', 'config_digest', 'policy_version'}
                    or type(expected['config_revision']) is not int or expected['config_revision'] < 1
                    or type(expected['policy_version']) is not int or expected['policy_version'] < 1
                    or type(expected['config_digest']) is not str or not SHA256.fullmatch(expected['config_digest'])
                    or type(options) is not dict or set(options) != {'external_reserve_mib','idle_seconds','restart_recovery'}
                    or type(options['external_reserve_mib']) is not int
                    or options['external_reserve_mib'] not in {2048,4096,8192,12288,16384,24576,32768}
                    or type(options['idle_seconds']) is not int or not 0 <= options['idle_seconds'] <= 86400
                    or type(options['restart_recovery']) is not bool):
                raise DeploymentOperationError('invalid_expected_configuration', '配置版本或策略选项无效')
            normalized.update(expected_configuration=dict(expected), policy_options=dict(options))
        now = utc_now()
        operation = {
            "id": f"dop_{secrets.token_hex(16)}",
            "server_profile_id": server_profile_id,
            "authenticated_principal": authenticated_principal,
            "route": self.route,
            "idempotency_key": idempotency_key,
            "request_digest": canonical_digest(normalized),
            "deployment_id": deployment_id,
            "state": "accepted",
            "payload": normalized,
            "plan_digest": normalized["plan_digest"],
            "confirmations": confirmations,
            "milestone": "accepted",
            "recovery_cursor": None,
            "created_at": now,
            "updated_at": now,
        }
        try:
            disposition, stored = self.repository.create_deployment_operation(
                operation, configuration_plan=configuration_plan if updating else None)
        except ValueError as exc:
            if str(exc) == "idempotency_conflict":
                raise DeploymentOperationError("idempotency_conflict",
                                               "同一幂等键已绑定不同请求", 409) from None
            raise
        return {**stored, "disposition": disposition}

    def get(self, operation_id: str) -> dict[str, Any]:
        value = self.repository.get_deployment_operation(operation_id)
        if value is None:
            raise DeploymentOperationError("deployment_operation_not_found", "部署操作不存在", 404)
        return value

    def uninstall(self, instance, payload, *, server_profile_id,
                  authenticated_principal, idempotency_key):
        fields = {"incarnation", "config_revision", "config_digest", "policy_version", "retry_of"}
        if (type(payload) is not dict or set(payload) != fields
                or type(payload["incarnation"]) is not str or not SAFE_TEXT.fullmatch(payload["incarnation"])
                or type(payload["config_revision"]) is not int or payload["config_revision"] < 1
                or type(payload["policy_version"]) is not int or payload["policy_version"] < 1
                or type(payload["config_digest"]) is not str or not SHA256.fullmatch(payload["config_digest"])
                or payload["retry_of"] is not None and (type(payload["retry_of"]) is not str
                    or not re.fullmatch(r"dop_[0-9a-f]{32}", payload["retry_of"]))):
            raise DeploymentOperationError("invalid_uninstall_request", "卸载请求必须包含准确的实例与配置版本")
        if any(type(value) is not str or not SAFE_TEXT.fullmatch(value)
               for value in (instance, server_profile_id, authenticated_principal, idempotency_key)):
            raise DeploymentOperationError("invalid_command_identity", "卸载命令身份无效")
        normalized = {**payload, "action": "uninstall", "deployment_id": instance}
        stamp = utc_now()
        operation = {"id": "dop_" + secrets.token_hex(16), "server_profile_id": server_profile_id,
                     "authenticated_principal": authenticated_principal,
                     "route": f"/api/v1/deployments/{instance}/uninstall", "idempotency_key": idempotency_key,
                     "request_digest": canonical_digest(normalized), "deployment_id": instance,
                     "state": "accepted", "payload": normalized, "plan_digest": payload["config_digest"],
                     "confirmations": {}, "milestone": "removing_containers", "recovery_cursor": None,
                     "created_at": stamp, "updated_at": stamp}
        disposition, stored = self.repository.create_deployment_operation(operation)
        return {**stored, "disposition": disposition}

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise DeploymentOperationError("invalid_operation_limit", "部署操作数量无效")
        return self.repository.list_deployment_operations(limit)

    def transition(self, operation_id: str, target: str, *, milestone: str | None = None,
                   recovery_cursor: dict[str, Any] | None = None,
                   error_class: str | None = None, error_code: str | None = None,
                   error_message: str | None = None,
                   result: dict[str, Any] | None = None) -> dict[str, Any]:
        current = self.get(operation_id)
        if current["payload"].get("action") == "uninstall":
            raise DeploymentOperationError("deployment_removal_owned_transition", "卸载状态只能由移除确认事务推进", 409)
        if target not in TRANSITIONS.get(current["state"], set()):
            raise DeploymentOperationError("invalid_deployment_operation_transition",
                                           "部署操作状态转换无效", 409)
        if target in {"rollback", "failed"}:
            if error_class not in ERROR_CLASSES or not error_code:
                raise DeploymentOperationError("deployment_operation_error_required",
                                               "失败转换必须包含错误分类和错误码")
        elif any(value is not None for value in (error_class, error_code, error_message)):
            raise DeploymentOperationError("unexpected_deployment_operation_error",
                                           "非失败转换不能携带错误")
        updated = self.repository.transition_deployment_operation(
            operation_id, {current["state"]}, {
                "state": target,
                "milestone": milestone or target,
                "recovery_cursor": recovery_cursor,
                "error_class": error_class,
                "error_code": error_code,
                "error_message": error_message,
                "result": result,
                "updated_at": utc_now(),
            })
        if updated is None:
            raise DeploymentOperationError("deployment_operation_changed",
                                           "部署操作已被其他执行器推进", 409)
        return updated

    def cancel(self, operation_id: str) -> dict[str, Any]:
        current = self.get(operation_id)
        if current["payload"].get("action") == "uninstall":
            raise DeploymentOperationError("deployment_removal_not_cancelable", "已受理的卸载不能取消，失败后可重试", 409)
        if current["state"] in {"ready", "canceled", "failed"}:
            raise DeploymentOperationError("deployment_operation_terminal", "终态操作不能取消", 409)
        target = "canceled" if current["state"] == "planned" else "canceling"
        return self.transition(operation_id, target)

    def record_resource(self, operation_id: str, *, kind: str, resource_id: str,
                        identity: str, created: bool) -> dict[str, Any]:
        self.get(operation_id)
        if kind not in RESOURCE_KINDS:
            raise DeploymentOperationError("invalid_operation_resource_kind", "资源类型无效")
        if (not isinstance(resource_id, str) or not SAFE_TEXT.fullmatch(resource_id)
                or not isinstance(identity, str) or not SAFE_TEXT.fullmatch(identity)
                or type(created) is not bool):
            raise DeploymentOperationError("invalid_operation_resource", "操作资源身份无效")
        self.repository.add_deployment_operation_resource(operation_id, {
            "kind": kind, "resource_id": resource_id, "identity": identity,
            "created": created, "created_at": utc_now(),
        })
        return self.get(operation_id)

    @staticmethod
    def _asset_ref(value: Any, name: str, *, optional: bool = False) -> dict[str, Any] | None:
        if value is None and optional:
            return None
        if (not isinstance(value, dict)
                or set(value) != {"asset_id", "revision", "manifest_digest"}
                or not isinstance(value["asset_id"], str)
                or not SAFE_TEXT.fullmatch(value["asset_id"])
                or not isinstance(value["revision"], str) or not value["revision"]
                or len(value["revision"]) > 160
                or not isinstance(value["manifest_digest"], str)
                or not SHA256.fullmatch(value["manifest_digest"])):
            raise DeploymentOperationError("invalid_asset_reference", f"{name} 不可变引用无效")
        return dict(value)

    @staticmethod
    def _runtime_ref(value: Any) -> dict[str, Any]:
        if (not isinstance(value, dict)
                or set(value) != {"profile_id", "revision", "image_digest", "profile_digest"}
                or not isinstance(value["profile_id"], str)
                or not SAFE_TEXT.fullmatch(value["profile_id"])
                or not isinstance(value["revision"], int) or isinstance(value["revision"], bool)
                or value["revision"] <= 0
                or not isinstance(value["image_digest"], str)
                or not IMAGE_DIGEST.fullmatch(value["image_digest"])
                or not isinstance(value["profile_digest"], str)
                or not SHA256.fullmatch(value["profile_digest"])):
            raise DeploymentOperationError("invalid_runtime_profile_reference", "Runtime Profile 引用无效")
        return dict(value)
