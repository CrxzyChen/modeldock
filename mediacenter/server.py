from __future__ import annotations

import argparse
import ipaddress
import json
import mimetypes
import os
import secrets
import shutil
import ssl
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .release_version import RELEASE_VERSION
from .kernel import DeploymentLifecycle
from .hardware import HardwareProbe
from .model_registry import ModelRegistry
from .model_deployments import ModelDeploymentManager
from .model_assets import MAX_CHUNK_BYTES, ModelAssetManager
from .service_installer import ServiceInstaller
from .repository import Repository
from .service_center import ServiceCenter, ServiceCenterError
from .client_events import GPUEventObserver, MemoryEventBroker, StateEventObserver

class MediaCenterHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    center: ServiceCenter
    lifecycle: DeploymentLifecycle
    api_key: str
    client_events: MemoryEventBroker
    event_observer: StateEventObserver
    gpu_event_observer: GPUEventObserver

    def server_close(self) -> None:
        model_assets = getattr(getattr(self, 'center', None), 'model_assets', None)
        if model_assets is not None and not model_assets.stop_maintenance():
            raise RuntimeError('model store maintenance is still stopping')
        if hasattr(self, "center") and not self.center.stop_deployment_worker():
            # Do not tear down lifecycle/ownership underneath a live OCI import.
            raise RuntimeError("user deployment worker is still finishing its bounded step")
        if hasattr(self, "event_observer"):
            self.event_observer.stop()
        if hasattr(self, "gpu_event_observer"):
            self.gpu_event_observer.stop()
        if hasattr(self, "client_events"):
            self.client_events.close()
        if hasattr(self, "lifecycle"):
            self.lifecycle.stop()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server: MediaCenterHTTPServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/healthz":
                services = self.server.center.list_services()
                self._json(200, {"status": "ok", "mode": "real", "version": RELEASE_VERSION,
                                 "services_ready": sum(1 for item in services if item["available"]),
                                 "services_total": len(services)})
                return
            if parsed.path.startswith("/api/v1/"):
                self._authorize()
                query = parse_qs(parsed.query)
                if parsed.path == "/api/v1/events":
                    self._events()
                elif parsed.path == "/api/v1/overview":
                    self._json(200, self.server.center.overview())
                elif parsed.path == "/api/v1/services":
                    self._json(200, {"items": self.server.center.list_services()})
                elif parsed.path.startswith('/api/v1/deployments/') and parsed.path.endswith('/validation-target'):
                    identifier=parsed.path.removeprefix('/api/v1/deployments/').removesuffix('/validation-target')
                    if not identifier or '/' in identifier:raise ServiceCenterError('not_found','实例不存在',404)
                    self._json(200,self.server.center.validation_target(identifier))
                elif parsed.path.startswith('/api/v1/runtime-validations/'):
                    identifier = parsed.path.removeprefix('/api/v1/runtime-validations/')
                    if '/' in identifier: raise ServiceCenterError('not_found','验证记录不存在',404)
                    self._json(200,self.server.center.get_validation(identifier))
                elif parsed.path == "/api/v1/tasks":
                    self._json(200, {"items": self.server.center.list_tasks(self._query_int(query, "limit", 50))})
                elif parsed.path.startswith("/api/v1/tasks/"):
                    self._json(200, self.server.center.get_task(parsed.path.removeprefix("/api/v1/tasks/")))
                elif parsed.path == "/api/v1/audit":
                    self._json(200, {"items": self.server.center.audit(self._query_int(query, "limit", 100))})
                elif parsed.path == "/api/v1/model-catalog":
                    self._json(200, {"items": self.server.center.model_catalog()})
                elif parsed.path == "/api/v1/service-catalog":
                    self._json(200, {"items": self.server.center.service_catalog()})
                elif parsed.path == "/api/v1/source-authorizations":
                    self._json(200, {"items": self.server.center.source_authorizations()})
                elif parsed.path == "/api/v1/service-installations":
                    self._json(200, {"items": self.server.center.list_service_installations(
                        self._query_int(query, "limit", 100))})
                elif parsed.path.startswith("/api/v1/service-installations/"):
                    installation_id = parsed.path.removeprefix("/api/v1/service-installations/")
                    if not installation_id or "/" in installation_id:
                        raise ServiceCenterError("service_installation_not_found",
                                                 "服务安装任务不存在", 404)
                    self._json(200, self.server.center.get_service_installation(installation_id))
                elif parsed.path == "/api/v1/model-assets":
                    self._json(200, {"items": self.server.center.list_model_assets(
                        media_kind=self._query_text(query, "media_kind"),
                        role=self._query_text(query, "role"), state=self._query_text(query, "state"),
                        query=self._query_text(query, "q"), limit=self._query_int(query, "limit", 100))})
                elif parsed.path == "/api/v1/asset-compatibility":
                    self._json(200, {"items": self.server.center.list_asset_compatibility(
                        subject_asset_id=self._query_text(query, "subject_asset_id"),
                        base_asset_id=self._query_text(query, "base_asset_id"))})
                elif parsed.path.startswith("/api/v1/model-assets/"):
                    asset_id = parsed.path.removeprefix("/api/v1/model-assets/")
                    if not asset_id or "/" in asset_id:
                        raise ServiceCenterError("model_asset_not_found", "模型资产不存在", 404)
                    self._json(200, self.server.center.get_model_asset(asset_id))
                elif parsed.path == "/api/v1/model-storage":
                    self._json(200, self.server.center.model_storage())
                elif parsed.path == "/api/v1/model-transfers":
                    self._json(200, {"items": self.server.center.list_model_transfers(
                        self._query_int(query, "limit", 100))})
                elif parsed.path == "/api/v1/runtime-profiles":
                    self._json(200, {"items": self.server.center.list_runtime_profiles()})
                elif parsed.path.startswith("/api/v1/model-transfers/"):
                    transfer_id = parsed.path.removeprefix("/api/v1/model-transfers/")
                    if not transfer_id or "/" in transfer_id:
                        raise ServiceCenterError("model_transfer_not_found", "模型传输不存在", 404)
                    self._json(200, self.server.center.get_model_transfer(transfer_id))
                elif parsed.path == "/api/v1/deployments":
                    self._json(200, {"items": self.server.center.list_deployments()})
                elif parsed.path == "/api/v1/deployment-operations":
                    self._json(200, {"items": self.server.center.list_deployment_operations(
                        self._query_int(query, "limit", 100))})
                elif parsed.path.startswith("/api/v1/deployment-operations/"):
                    operation_id = parsed.path.removeprefix(
                        "/api/v1/deployment-operations/")
                    if not operation_id or "/" in operation_id:
                        raise ServiceCenterError(
                            "deployment_operation_not_found", "部署操作不存在", 404)
                    self._json(200, self.server.center.get_deployment_operation(operation_id))
                elif parsed.path == "/api/v1/resources/gpus":
                    self._json(200, self.server.center.gpu_resources())
                elif parsed.path == "/api/v1/hardware":
                    self._json(200, self.server.center.hardware())
                elif parsed.path.startswith("/api/v1/assets/"):
                    asset_id = parsed.path.removeprefix("/api/v1/assets/")
                    if not asset_id or "/" in asset_id:
                        raise ServiceCenterError("asset_not_found", "素材不存在", 404)
                    self._json(200, self.server.center.get_asset(asset_id))
                elif parsed.path.startswith("/api/v1/artifacts/"):
                    self._file(self.server.center.artifact(unquote(parsed.path.removeprefix("/api/v1/artifacts/"))))
                else:
                    raise ServiceCenterError("not_found", "接口不存在", 404)
                return
            raise ServiceCenterError(
                "desktop_client_required",
                "MediaCenter Server 仅提供 API，请使用 MediaCenter PC 客户端连接",
                404,
            )
        except ServiceCenterError as exc:
            self._error(exc)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            self._authorize()
            # Parse every JSON POST exactly once, including fieldless actions.
            # Leaving an ignored ``{}`` in a persistent connection makes those
            # bytes become the next request method (for example ``{}GET``).
            # Asset creation is the sole binary POST contract and consumes its
            # body below through ``_asset_body``.
            body = None if parsed.path == "/api/v1/assets" else self._body()
            if parsed.path == "/api/v1/tasks":
                self._json(201, self.server.center.create_task(body,
                    idempotency_key=self.headers.get("Idempotency-Key"), identity_scope="server-admin"))
            elif parsed.path == "/api/v1/assets":
                query = parse_qs(parsed.query)
                filename = query.get("filename", [""])[0]
                media_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                self._json(201, self.server.center.create_asset(filename, media_type, self._asset_body()))
            elif parsed.path == "/api/v1/deployments":
                self._json(201, self.server.center.create_deployment(body))
            elif parsed.path == "/api/v1/model-transfers":
                self._json(201, self.server.center.create_model_transfer(body))
            elif parsed.path == "/api/v1/model-imports/preflight":
                self._json(200, self.server.center.preflight_model_import(body))
            elif parsed.path == "/api/v1/asset-compatibility":
                self._json(200, self.server.center.assess_asset_compatibility(body))
            elif parsed.path == "/api/v1/deployment-plans":
                self._json(200, self.server.center.plan_user_deployment(body))
            elif parsed.path == "/api/v1/deployment-operations":
                self._json(202, self.server.center.create_user_deployment(
                    body, idempotency_key=self.headers.get("Idempotency-Key"),
                    identity_scope="server-admin"))
            elif (parsed.path.startswith("/api/v1/deployment-operations/")
                  and parsed.path.endswith("/cancel")):
                operation_id = parsed.path.removeprefix(
                    "/api/v1/deployment-operations/").removesuffix("/cancel")
                if not operation_id or "/" in operation_id:
                    raise ServiceCenterError(
                        "deployment_operation_not_found", "部署操作不存在", 404)
                self._json(200, self.server.center.cancel_deployment_operation(operation_id))
            elif parsed.path == "/api/v1/service-installations":
                self._json(202, self.server.center.install_service(body))
            elif parsed.path.startswith("/api/v1/source-authorizations/"):
                if not self._source_authorization_transport_allowed():
                    raise ServiceCenterError(
                        "source_authorization_secure_transport_required",
                        "模型来源令牌只能通过 HTTPS、服务器本机回环连接，或客户端显式确认的可信局域网 HTTP 连接提交", 426)
                provider = parsed.path.removeprefix("/api/v1/source-authorizations/")
                if not provider or "/" in provider:
                    raise ServiceCenterError(
                        "source_authorization_provider_unsupported",
                        "模型来源授权提供方不受支持", 404)
                self._json(200, self.server.center.configure_source_authorization(
                    provider, body))
            elif (parsed.path.startswith("/api/v1/service-catalog/")
                  and parsed.path.endswith("/uninstall")):
                recipe_key = parsed.path.removeprefix(
                    "/api/v1/service-catalog/").removesuffix("/uninstall")
                if not recipe_key or "/" in recipe_key:
                    raise ServiceCenterError("service_recipe_not_found", "服务安装配方不存在", 404)
                self._json(200, self.server.center.uninstall_service(recipe_key))
            elif parsed.path.startswith("/api/v1/service-installations/") and any(
                    parsed.path.endswith(f"/{action}")
                    for action in ("pause", "resume", "cancel", "retry")):
                action = parsed.path.rsplit("/", 1)[-1]
                installation_id = parsed.path.removeprefix(
                    "/api/v1/service-installations/").rsplit("/", 1)[0]
                self._json(200, self.server.center.control_service_installation(
                    installation_id, action))
            elif parsed.path.startswith("/api/v1/model-transfers/") and parsed.path.endswith("/complete"):
                transfer_id = parsed.path.removeprefix("/api/v1/model-transfers/").removesuffix("/complete")
                self._json(200, self.server.center.complete_model_transfer(transfer_id))
            elif parsed.path.startswith("/api/v1/model-transfers/") and any(
                    parsed.path.endswith(f"/{action}") for action in ("pause", "resume", "cancel", "retry")):
                action = parsed.path.rsplit("/", 1)[-1]
                transfer_id = parsed.path.removeprefix("/api/v1/model-transfers/").rsplit("/", 1)[0]
                self._json(200, self.server.center.control_model_transfer(transfer_id, action))
            elif parsed.path.startswith("/api/v1/model-assets/") and parsed.path.endswith("/archive"):
                asset_id = parsed.path.removeprefix("/api/v1/model-assets/").removesuffix("/archive")
                self._json(200, self.server.center.archive_model_asset(asset_id))
            elif parsed.path.startswith("/api/v1/model-assets/") and parsed.path.endswith("/restore"):
                asset_id = parsed.path.removeprefix("/api/v1/model-assets/").removesuffix("/restore")
                self._json(200, self.server.center.archive_model_asset(asset_id, restore=True))
            elif parsed.path.startswith("/api/v1/tasks/") and parsed.path.endswith("/cancel"):
                task_id = parsed.path.removeprefix("/api/v1/tasks/").removesuffix("/cancel")
                self._json(200, self.server.center.cancel_task(task_id))
            elif parsed.path.startswith("/api/v1/tasks/") and parsed.path.endswith("/retry"):
                task_id = parsed.path.removeprefix("/api/v1/tasks/").removesuffix("/retry")
                self._json(200, self.server.center.retry_task(task_id, body.get("version")))
            elif parsed.path.startswith('/api/v1/runtime-validations/') and parsed.path.endswith('/cancel'):
                identifier = parsed.path.removeprefix('/api/v1/runtime-validations/').removesuffix('/cancel')
                if '/' in identifier: raise ServiceCenterError('not_found','验证记录不存在',404)
                self._json(200,self.server.center.cancel_validation(identifier,body))
            elif parsed.path.startswith('/api/v1/deployments/') and parsed.path.endswith('/uninstall'):
                identifier = parsed.path.removeprefix('/api/v1/deployments/').removesuffix('/uninstall')
                if not identifier or '/' in identifier:
                    raise ServiceCenterError('not_found', '实例不存在', 404)
                self._json(202, self.server.center.uninstall_user_deployment(
                    identifier, body, idempotency_key=self.headers.get('Idempotency-Key'),
                    identity_scope='server-admin'))
            elif parsed.path.startswith('/api/v1/deployments/') and parsed.path.endswith('/validate'):
                identifier = parsed.path.removeprefix('/api/v1/deployments/').removesuffix('/validate')
                if '/' in identifier: raise ServiceCenterError('not_found','实例不存在',404)
                self._json(202,self.server.center.validate_instance(identifier,body,key=self.headers.get('Idempotency-Key')))
            elif parsed.path.startswith("/api/v1/deployments/") and parsed.path.endswith("/start"):
                deployment_id = parsed.path.removeprefix("/api/v1/deployments/").removesuffix("/start")
                if not deployment_id or "/" in deployment_id:
                    raise ServiceCenterError("not_found", "部署实例不存在", 404)
                self._json(202, self.server.center.start_deployment(deployment_id, body))
            elif parsed.path.startswith("/api/v1/deployments/") and parsed.path.endswith("/policy"):
                deployment_id = parsed.path.removeprefix("/api/v1/deployments/").removesuffix("/policy")
                self._json(200, self.server.center.configure_instance_policy(deployment_id, body))
            elif parsed.path.startswith("/api/v1/deployments/") and parsed.path.endswith("/stop"):
                deployment_id = parsed.path.removeprefix("/api/v1/deployments/").removesuffix("/stop")
                if not deployment_id or "/" in deployment_id:
                    raise ServiceCenterError("not_found", "部署实例不存在", 404)
                self._json(202, self.server.center.stop_deployment(deployment_id, body))
            else:
                raise ServiceCenterError("not_found", "接口不存在", 404)
        except ServiceCenterError as exc:
            self._error(exc)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        try:
            self._authorize()
            parts = parsed.path.removeprefix("/api/v1/model-transfers/").split("/")
            if (not parsed.path.startswith("/api/v1/model-transfers/") or len(parts) != 3 or
                    parts[1] != "files" or not parts[0] or not parts[2]):
                raise ServiceCenterError("not_found", "接口不存在", 404)
            query = parse_qs(parsed.query)
            try:
                offset = int(query.get("offset", [""])[0])
            except ValueError:
                raise ServiceCenterError("invalid_offset", "上传偏移量无效") from None
            self._json(200, self.server.center.append_model_transfer_chunk(
                parts[0], parts[2], offset, self._chunk_body()))
        except ServiceCenterError as exc:
            self._error(exc)

    def do_PATCH(self) -> None:
        try:
            self._authorize()
            path = urlparse(self.path).path
            if path.startswith("/api/v1/services/"):
                self._json(200, self.server.center.configure_service(path.removeprefix("/api/v1/services/"), self._body()))
            elif path.startswith("/api/v1/deployments/"):
                deployment_id = path.removeprefix("/api/v1/deployments/")
                if not deployment_id or "/" in deployment_id:
                    raise ServiceCenterError("not_found", "部署实例不存在", 404)
                self._json(200, self.server.center.update_deployment(deployment_id, self._body()))
            else:
                raise ServiceCenterError("not_found", "接口不存在", 404)
        except ServiceCenterError as exc:
            self._error(exc)

    def _authorize(self) -> None:
        candidate = self.headers.get("X-API-Key", "")
        if not candidate or not secrets.compare_digest(candidate, self.server.api_key):
            # Do not leave an unread request body attached to a persistent
            # connection. The body is untrusted and deliberately not parsed.
            self.close_connection = True
            raise ServiceCenterError("unauthorized", "API Key 无效", 401)

    def _source_authorization_transport_allowed(self) -> bool:
        if isinstance(self.connection, ssl.SSLSocket):
            return True
        try:
            if ipaddress.ip_address(self.client_address[0]).is_loopback:
                return True
        except ValueError:
            pass
        return self.headers.get("X-MediaCenter-Insecure-Transport-Accepted") == "1"

    @staticmethod
    def _event_bytes(event: dict[str, Any], *, name: str | None = None) -> bytes:
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False)
        lines = [f"id: {event['id']}"]
        if name:
            lines.append(f"event: {name}")
        lines.extend(f"data: {line}" for line in payload.splitlines() or [""])
        return ("\n".join(lines) + "\n\n").encode("utf-8")

    def _events(self) -> None:
        broker = self.server.client_events
        if not broker.acquire_client():
            raise ServiceCenterError("event_client_limit", "SSE 连接数已达到上限", 503)
        try:
            cursor, reset = broker.open(self.headers.get("Last-Event-ID"))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.connection.settimeout(10.0)
            hello = {"protocol": "mc.client/1", "id": f"{cursor.stream_id}:{cursor.sequence}",
                     "type": "hello", "resource_id": "", "version": None,
                     "occurred_at": time.time(),
                     "data": broker.memory_contract}
            self.wfile.write(b"retry: 2000\n")
            self.wfile.write(self._event_bytes(hello, name="hello"))
            if reset:
                event = {**hello, "type": "snapshot.required", "data": {"reason": "cursor_unavailable"}}
                self.wfile.write(self._event_bytes(event, name="snapshot.required"))
            if not self.headers.get("Last-Event-ID"):
                latest_gpu = broker.latest("gpu.telemetry", "gpus")
                if latest_gpu is not None:
                    latest_gpu = {**latest_gpu, "id": hello["id"]}
                    self.wfile.write(self._event_bytes(latest_gpu, name="gpu.telemetry"))
            self.wfile.flush()
            while not broker.closed:
                events, cursor, reset = broker.wait(cursor, timeout=15.0)
                if reset:
                    event = {**hello, "id": f"{cursor.stream_id}:{cursor.sequence}",
                             "type": "snapshot.required", "occurred_at": time.time(),
                             "data": {"reason": "cursor_expired"}}
                    self.wfile.write(self._event_bytes(event, name="snapshot.required"))
                elif events:
                    for event in events:
                        self.wfile.write(self._event_bytes(event, name=event["type"]))
                else:
                    self.wfile.write(f": heartbeat {int(time.time())}\n\n".encode("ascii"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            self.close_connection = True
        finally:
            self.close_connection = True
            broker.release_client()

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                raise ValueError
            value = json.loads(self.rfile.read(length), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            raise ServiceCenterError("invalid_json", "请求体不是有效 JSON") from None
        if not isinstance(value, dict):
            raise ServiceCenterError("invalid_body", "请求体必须是 JSON 对象")
        self._validate_unicode(value)
        return value

    def _asset_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 256 * 1024 * 1024:
            raise ServiceCenterError("invalid_asset", "素材大小必须在 1 字节到 256MiB 之间")
        data = self.rfile.read(length)
        if len(data) != length:
            raise ServiceCenterError("invalid_asset", "素材上传不完整")
        return data

    def _chunk_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            raise ServiceCenterError("chunked_encoding_forbidden", "模型上传分块必须提供 Content-Length", 411)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_CHUNK_BYTES:
            raise ServiceCenterError("invalid_chunk_size", "模型上传分块必须在 1 字节到 8 MiB 之间", 413)
        data = self.rfile.read(length)
        if len(data) != length:
            raise ServiceCenterError("upload_incomplete", "模型上传分块不完整")
        return data

    @classmethod
    def _validate_unicode(cls, value: Any) -> None:
        if isinstance(value, str):
            try:
                value.encode("utf-8", "strict")
            except UnicodeEncodeError:
                raise ServiceCenterError("invalid_unicode", "JSON 字符串包含无效 Unicode") from None
        elif isinstance(value, dict):
            for key, item in value.items():
                cls._validate_unicode(key); cls._validate_unicode(item)
        elif isinstance(value, list):
            for item in value: cls._validate_unicode(item)

    @staticmethod
    def _query_int(query: dict[str, list[str]], name: str, default: int) -> int:
        try: return int(query.get(name, [str(default)])[0])
        except ValueError: raise ServiceCenterError(f"invalid_{name}", f"{name} 必须是整数") from None

    @staticmethod
    def _query_text(query: dict[str, list[str]], name: str) -> str | None:
        value = query.get(name, [None])[0]
        return value if value else None

    def _file(self, path: Path, cache: str = "private, max-age=3600") -> None:
        from .artifacts import AuthorizedFile
        if isinstance(path, AuthorizedFile):
            with path:
                self.connection.settimeout(10.0)
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(path.size))
                self.send_header("Cache-Control", cache)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                remaining = path.size
                while remaining:
                    block = path.stream.read(min(1024*1024,remaining))
                    if not block:
                        self.close_connection = True
                        break
                    try:self.wfile.write(block)
                    except OSError:
                        self.close_connection = True
                        break
                    remaining -= len(block)
            return
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        with path.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile, length=1024 * 1024)

    def _error(self, exc: ServiceCenterError) -> None:
        self._json(exc.status, {"error": {"code": exc.code, "message": exc.message}})

    def _json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("X-Frame-Options", "DENY")
        self.end_headers(); self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")


def create_server(host: str, port: int, db_path: str | Path, api_key: str, *,
                  artifact_root: str | Path | None = None,
                  catalog_path: str | Path | None = None,
                  model_store_root: str | Path | None = None, gpu_index: int | None = None,
                  gpu_indices: tuple[int, ...] | None = None,
                   gpu_uuids: tuple[str, ...] = (), runtime_config_path=None, api_key_file=None,
                   start_kernel: bool = True) -> MediaCenterHTTPServer:
    if not api_key: raise ValueError("api_key cannot be empty")
    if not _is_loopback(host) and api_key == "mc_dev_key":
        raise ValueError("MEDIACENTER_API_KEY must be explicitly set for non-loopback binding")
    data_root = Path(os.environ.get("MEDIACENTER_DATA_ROOT", "data")).resolve()
    artifacts = Path(artifact_root or data_root / "artifacts")
    repository = Repository(db_path)
    if gpu_indices is None:
        if gpu_index is None:
            raise ValueError("MediaCenter GPU pool must be explicitly configured")
        gpu_indices = (gpu_index,)
    pool = tuple(sorted(set(gpu_indices)))
    if not pool or len(pool) != len(gpu_indices) or any(index < 0 for index in pool):
        raise ValueError("MediaCenter GPU pool must contain unique non-negative indices")
    catalog = Path(catalog_path or data_root / "model_catalog.json")
    model_store = Path(model_store_root or os.environ.get(
        "MEDIACENTER_MODEL_STORE", str(data_root / "model-store"))).resolve()
    download_hosts = tuple(host.strip() for host in os.environ.get(
        "MEDIACENTER_MODEL_DOWNLOAD_HOSTS", "").split(",") if host.strip())
    catalog_payload = json.loads(catalog.read_text(encoding="utf-8")) if catalog.is_file() else {}
    recipe_hosts = {
        urlparse(file.get("url", "")).hostname
        for entry in catalog_payload.get("models", []) if isinstance(entry, dict)
        for file in (entry.get("service_recipe") or {}).get("files", []) if isinstance(file, dict)
    }
    recipe_hosts.update(
        host for entry in catalog_payload.get("models", []) if isinstance(entry, dict)
        for host in (entry.get("service_recipe") or {}).get("allowed_redirect_hosts", [])
        if isinstance(host, str)
    )
    download_hosts = tuple(sorted(set(download_hosts) | {host for host in recipe_hosts if host}))
    model_assets = ModelAssetManager(
        repository, model_store, download_hosts,
        download_proxy=os.environ.get("MEDIACENTER_DOWNLOAD_PROXY"),
    )
    deployments = ModelDeploymentManager(repository, catalog, data_root, pool,
                                         model_store) if catalog.is_file() else None
    if deployments is None:
        raise ValueError("container model catalog is required")
    registry = ModelRegistry(deployments)
    server_id = "mediacenter"
    installations = importer = provisioner = None
    if runtime_config_path is not None:
        from .config import load_runtime_configuration, installation_components
        server_id, gpu_uuids, installation_config = load_runtime_configuration(runtime_config_path,
            database=repository.path, api_key_file=api_key_file, include_installation=True)
        if Path(api_key_file).read_text(encoding="utf-8").strip() != api_key:
            raise ValueError("API Key file does not match the Server authentication source")
        if installation_config[1] is not None:
            if len(gpu_uuids) != len(pool): raise ValueError('explicit UUID pool required for installation')
            inputs = artifacts.parent / 'inputs'; inputs.mkdir(parents=True, exist_ok=True)
            installations, importer, provisioner = installation_components(repository, server_id, *installation_config,
                models_root=model_store, inputs_root=inputs, api_key_file=api_key_file)
    registry.installation_runtime = installations
    service_installer = (ServiceInstaller(repository, catalog, model_assets, deployments,
                            installation_runtime=installations, runtime_importer=importer)
                         if deployments is not None else None)
    hardware_probe = HardwareProbe(pool, storage_paths=("/", data_root))
    server = MediaCenterHTTPServer((host, port), Handler)
    server.lifecycle = DeploymentLifecycle(repository, registry, artifacts,
                           gpu_indices=pool, gpu_uuids=gpu_uuids,
                           hardware=hardware_probe, server_id=server_id, model_assets=model_assets, package_provider=provisioner)
    if service_installer is not None and provisioner is not None:
        service_installer.on_installed = server.lifecycle.install_instance
        service_installer.on_uninstall = server.lifecycle.retire_instance_containers
    if deployments is not None:
        deployments.on_change = registry.refresh
    server.center = ServiceCenter(
        repository, registry, artifacts,
        deployments=deployments,
        gpu_scheduler=server.lifecycle.scheduler,
        retire_worker=server.lifecycle.retire,
        hardware_probe=hardware_probe,
        model_assets=model_assets,
        service_installer=service_installer,
        runtime=server.lifecycle,
        runtime_importer=importer,
    )
    server.api_key = api_key
    server.client_events = MemoryEventBroker()
    server.event_observer = StateEventObserver(repository, server.client_events)
    server.gpu_event_observer = GPUEventObserver(server.center.gpu_resources, server.client_events)
    if start_kernel:
        server.lifecycle.start()
        server.center.start_deployment_worker()
        server.event_observer.start()
        server.gpu_event_observer.start()
        model_assets.start_maintenance()
    return server


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost": return True
    try: return ipaddress.ip_address(host).is_loopback
    except ValueError: return False


def parse_gpu_pool(raw_pool: str | None) -> tuple[int, ...]:
    if not raw_pool:
        raise ValueError("MEDIACENTER_GPU_POOL must be explicitly set")
    try:
        values = [int(value.strip()) for value in raw_pool.split(",")]
    except ValueError:
        raise ValueError("MEDIACENTER_GPU_POOL must be a comma-separated integer list") from None
    pool = tuple(sorted(set(values)))
    if not pool or len(pool) != len(values) or any(index < 0 for index in pool):
        raise ValueError("MEDIACENTER_GPU_POOL must contain unique non-negative indices")
    return pool


def main() -> None:
    parser = argparse.ArgumentParser(description="MediaCenter server control plane")
    parser.add_argument("--host", default=os.environ.get("MEDIACENTER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MEDIACENTER_PORT", "8787")))
    parser.add_argument("--db", default=os.environ.get("MEDIACENTER_DB", "data/mediacenter.db"))
    args = parser.parse_args()
    pool = parse_gpu_pool(os.environ.get("MEDIACENTER_GPU_POOL"))
    gpu_inventory = HardwareProbe(pool).gpu_snapshot()
    if not gpu_inventory["available"]:
        raise ValueError(f"GPU inventory unavailable: {gpu_inventory['error']}")
    present = {item["index"] for item in gpu_inventory["items"]}
    missing = sorted(set(pool) - present)
    if missing:
        raise ValueError("MEDIACENTER_GPU_POOL contains missing GPU indices: "
                         + ", ".join(map(str, missing)))
    key_file = os.environ.get("MEDIACENTER_API_KEY_FILE")
    key = Path(key_file).read_text(encoding="utf-8").strip() if key_file else os.environ.get("MEDIACENTER_API_KEY", "mc_dev_key")
    server = create_server(args.host, args.port, args.db, key, gpu_indices=pool,
                           gpu_uuids=tuple(value.strip() for value in os.environ.get("MEDIACENTER_GPU_UUID_POOL", "").split(",") if value.strip()),
                           runtime_config_path=os.environ.get("MEDIACENTER_RUNTIME_CONFIG"), api_key_file=key_file,
                           catalog_path=os.environ.get("MEDIACENTER_MODEL_CATALOG"))
    print(f"MediaCenter running at http://{args.host}:{server.server_address[1]} (real model mode)")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
