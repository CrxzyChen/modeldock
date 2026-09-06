from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import shutil
import socket
import stat
import struct
import threading
import time
from contextlib import contextmanager, nullcontext, ExitStack
from functools import wraps
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .model_imports import ModelInspectionError, canonical_digest, inspect_safetensors
from .repository import Repository


MEDIA_KINDS = {"image", "video", "speech", "music", "general"}
MODEL_ROLES = {"checkpoint", "lora", "adapter", "vae", "encoder", "upscaler", "control"}
MODEL_FORMATS = {"safetensors", "gguf", "diffusers", "transformers", "trusted-bundle"}
TRANSFER_STATES = {"queued", "transferring", "paused", "verifying", "succeeded", "failed", "canceled"}
MAX_CHUNK_BYTES = 8 * 1024 * 1024
MAX_FILE_COUNT = 20_000


class ModelAssetError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def serialized_transfer(method):
    @wraps(method)
    def call(self, transfer_id, *args, **kwargs):
        with self._transfer_guard(transfer_id):
            return method(self, transfer_id, *args, **kwargs)
    return call


def normalize_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ModelAssetError("invalid_model_path", "模型文件路径无效")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ModelAssetError("invalid_model_path", "模型文件路径必须是规范化相对路径")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ModelAssetError("invalid_model_path", "模型文件路径必须是规范化相对路径")
    normalized = path.as_posix()
    if normalized.startswith("/") or ":" in path.parts[0]:
        raise ModelAssetError("invalid_model_path", "模型文件路径不得是绝对路径")
    return normalized


def validate_https_url(url: str, allowed_hosts: Iterable[str]) -> tuple[str, list[str]]:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ModelAssetError("source_url_invalid", "下载地址无效") from None
    hosts = {host.strip().lower().rstrip(".") for host in allowed_hosts if host.strip()}
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
        raise ModelAssetError("source_url_invalid", "只允许不含凭据的 HTTPS 地址")
    if hostname not in hosts:
        raise ModelAssetError("source_host_not_allowed", "下载主机不在服务器允许列表")
    if port not in {None, 443}:
        raise ModelAssetError("source_port_not_allowed", "下载地址只允许 HTTPS 默认端口")
    try:
        addresses = sorted({item[4][0] for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)})
    except socket.gaierror:
        raise ModelAssetError("source_dns_failed", "下载主机 DNS 解析失败") from None
    if not addresses:
        raise ModelAssetError("source_dns_failed", "下载主机没有可用地址")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ModelAssetError("source_address_blocked", "下载主机解析到非公网地址")
    return hostname, addresses


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


class ModelAssetManager:
    def __init__(self, repository: Repository, storage_root: str | Path,
                 allowed_download_hosts: Iterable[str] = (), *,
                 download_bearer_tokens: Mapping[str, str] | None = None,
                 download_proxy: str | None = None, recover_interrupted: bool = True):
        self.repository = repository
        self.storage_root = Path(storage_root).resolve()
        self.allowed_download_hosts = tuple(
            host.strip().lower().rstrip(".") for host in allowed_download_hosts if host.strip())
        self.download_proxy = self._validated_proxy(download_proxy)
        allowed = set(self.allowed_download_hosts)
        credentials: dict[str, str] = {}
        for raw_host, token in (download_bearer_tokens or {}).items():
            host = raw_host.strip().lower().rstrip(".") if isinstance(raw_host, str) else ""
            if host not in allowed or not isinstance(token, str) or not token \
                    or len(token) > 4096 or any(character.isspace() for character in token):
                raise ValueError("download bearer credential is invalid")
            credentials[host] = token
        self._download_bearer_tokens = credentials
        self._credential_lock = threading.RLock()
        self._download_lock = threading.Lock()
        self._downloading: set[str] = set()
        self._maintenance_stop = threading.Event()
        self._maintenance_wake = threading.Event()
        self._maintenance_thread = None
        self._maintenance_closed = False
        self._cleanup_cursor = ''
        self._cleanup_retries = {}
        self._maintenance_lock = threading.Lock()
        self._inventory_lock = threading.Lock()
        self._inventory = {'quarantine_bytes': None, 'quarantine_file_count': None,
                           'inventory_complete': False, 'inventory_updated_at': None,
                           'inventory_error': None}
        if not self.storage_root.is_absolute():
            raise ValueError("model storage root must be absolute")
        for name in ("quarantine", "blobs", "assets", "rejected", "transfer-locks"):
            (self.storage_root / name).mkdir(parents=True, exist_ok=True)
        if recover_interrupted:
            self.recover_interrupted_transfers()

    @contextmanager
    def _transfer_guard(self, transfer_id, *, wait=False):
        """Fail fast for HTTP callers; all file/state mutations share this lock.

        Lock names are permanent coordination objects, never cleanup candidates.
        Separate managers and processes must not create a second writer.
        """
        from .artifacts import checked_path
        from .runtime_artifacts import exclusive_lock, RuntimeLockBusy
        if not isinstance(transfer_id, str) or not re.fullmatch(r'mtr_[0-9a-f]{16}', transfer_id):
            raise ModelAssetError('model_transfer_not_found', '模型传输不存在', 404)
        lock_path = self.storage_root / 'transfer-locks' / (transfer_id + '.lock')
        deadline = time.monotonic() + (5.0 if wait else 0)
        with ExitStack() as stack:
            while True:
                try:
                    stack.enter_context(exclusive_lock(lock_path))
                    break
                except RuntimeLockBusy:
                    if time.monotonic() >= deadline:
                        raise ModelAssetError('model_transfer_busy', '模型传输正在处理，请稍后重试', 409) from None
                    time.sleep(0.025)
            checked_path(lock_path)
            yield

    @staticmethod
    def _validated_proxy(value: str | None) -> str | None:
        if value in (None, ""):
            return None
        if not isinstance(value, str) or value != value.strip() or any(
                ord(character) < 33 or ord(character) > 126 for character in value):
            raise ValueError("download proxy is invalid")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise ValueError("download proxy is invalid") from None
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"} or port is not None and not 1 <= port <= 65535):
            raise ValueError("download proxy is invalid")
        return value.removesuffix("/")

    def has_download_credential(self, host: str) -> bool:
        normalized = host.strip().lower().rstrip(".") if isinstance(host, str) else ""
        with self._credential_lock:
            return normalized in self._download_bearer_tokens

    def configure_download_credential(self, host: str, token: str) -> None:
        normalized = host.strip().lower().rstrip(".") if isinstance(host, str) else ""
        if normalized not in self.allowed_download_hosts:
            raise ModelAssetError(
                "source_credential_host_forbidden", "模型来源凭据主机不在允许列表", 400)
        if (not isinstance(token, str) or not token or len(token) > 4096
                or any(character.isspace() for character in token)):
            raise ModelAssetError("source_credential_invalid", "模型来源令牌格式无效", 400)
        with self._credential_lock:
            self._download_bearer_tokens[normalized] = token

    def clear_download_credential(self, host: str) -> None:
        normalized = host.strip().lower().rstrip(".") if isinstance(host, str) else ""
        if normalized not in self.allowed_download_hosts:
            raise ModelAssetError(
                "source_credential_host_forbidden", "模型来源凭据主机不在允许列表", 400)
        with self._credential_lock:
            self._download_bearer_tokens.pop(normalized, None)

    def create_https_download(self, payload: dict[str, Any]) -> dict[str, Any]:
        display_name = self._text(payload.get("display_name"), "display_name", 120)
        media_kind = self._choice(payload.get("media_kind"), MEDIA_KINDS, "media_kind")
        role = self._choice(payload.get("role"), MODEL_ROLES, "role")
        model_format = self._choice(payload.get("format"), MODEL_FORMATS - {"trusted-bundle"}, "format")
        revision = self._text(payload.get("revision"), "revision", 160)
        license_declared = self._text(payload.get("license_declared", "unknown"),
                                      "license_declared", 160)
        url = payload.get("url")
        filename = normalize_relative_path(payload.get("filename"))
        if "/" in filename:
            raise ModelAssetError("invalid_model_path", "HTTPS 单文件下载名不能包含目录")
        if not isinstance(url, str):
            raise ModelAssetError("source_url_invalid", "下载地址无效")
        validate_https_url(url, self.allowed_download_hosts)
        expected_bytes = payload.get("expected_bytes")
        expected_sha256 = payload.get("expected_sha256")
        if (not isinstance(expected_bytes, int) or isinstance(expected_bytes, bool) or
                expected_bytes <= 0):
            raise ModelAssetError("invalid_file_size", "下载必须提供大于零的固定文件大小")
        if not isinstance(expected_sha256, str) or not self._is_sha256(expected_sha256):
            raise ModelAssetError("invalid_sha256", "下载必须提供固定 SHA-256")
        if expected_bytes > shutil.disk_usage(self.storage_root).free:
            raise ModelAssetError("insufficient_storage", "模型存储空间不足", 507)
        transfer_id = f"mtr_{secrets.token_hex(8)}"
        quarantine_relpath = f"quarantine/{transfer_id}"
        self._inside(quarantine_relpath).mkdir(parents=False, exist_ok=False)
        now = utc_now()
        transfer = {"id": transfer_id, "direction": "download", "state": "queued",
                    "display_name": display_name, "media_kind": media_kind, "role": role,
                    "format": model_format, "source_type": "https", "source_ref": url,
                    "revision": revision, "license_declared": license_declared,
                    "expected_bytes": expected_bytes, "received_bytes": 0,
                    "quarantine_relpath": quarantine_relpath, "created_at": now, "updated_at": now}
        files = [{"id": f"mfl_{secrets.token_hex(8)}", "relative_path": filename,
                  "expected_bytes": expected_bytes, "expected_sha256": expected_sha256.lower(),
                  "source_url": url}]
        self.repository.insert_model_transfer(transfer, files)
        self._start_download(transfer_id)
        return self.get_transfer(transfer_id)

    def create_catalog_download(self, payload: dict[str, Any], *, owner=None) -> dict[str, Any]:
        """Create a fixed, resumable multi-file download from a trusted service recipe."""
        display_name = self._text(payload.get("display_name"), "display_name", 120)
        media_kind = self._choice(payload.get("media_kind"), MEDIA_KINDS, "media_kind")
        role = self._choice(payload.get("role"), MODEL_ROLES, "role")
        # Fixed service recipes may install trusted bundles such as an official .pth
        # release because every file is pinned by URL, size and SHA-256. Manual URL
        # downloads and client uploads keep their stricter format restrictions.
        model_format = self._choice(payload.get("format"), MODEL_FORMATS, "format")
        source_type = self._choice(payload.get("source_type"),
                                   {"huggingface", "github-release", "https"}, "source_type")
        source_ref = self._text(payload.get("source_ref"), "source_ref", 300)
        revision = self._text(payload.get("revision"), "revision", 160)
        license_declared = self._text(payload.get("license_declared"),
                                      "license_declared", 160)
        raw_files = payload.get("files")
        if not isinstance(raw_files, list) or not raw_files or len(raw_files) > MAX_FILE_COUNT:
            raise ModelAssetError("invalid_file_manifest", "服务配方文件清单为空或数量超限")
        files: list[dict[str, Any]] = []
        folded: set[str] = set()
        total_bytes = 0
        for raw in raw_files:
            if not isinstance(raw, dict):
                raise ModelAssetError("invalid_file_manifest", "服务配方文件清单项无效")
            relative_path = normalize_relative_path(raw.get("relative_path"))
            key = relative_path.casefold()
            if key in folded:
                raise ModelAssetError("path_case_collision", "服务配方文件路径存在大小写冲突")
            folded.add(key)
            source_url = raw.get("url")
            if not isinstance(source_url, str):
                raise ModelAssetError("source_url_invalid", "服务配方下载地址无效")
            validate_https_url(source_url, self.allowed_download_hosts)
            expected_bytes = raw.get("byte_size")
            expected_sha256 = raw.get("sha256")
            if (not isinstance(expected_bytes, int) or isinstance(expected_bytes, bool) or
                    expected_bytes <= 0):
                raise ModelAssetError("invalid_file_size", "服务配方必须固定每个文件大小")
            if not isinstance(expected_sha256, str) or not self._is_sha256(expected_sha256):
                raise ModelAssetError("invalid_sha256", "服务配方必须固定每个文件 SHA-256")
            total_bytes += expected_bytes
            files.append({"id": f"mfl_{secrets.token_hex(8)}",
                          "relative_path": relative_path, "expected_bytes": expected_bytes,
                          "expected_sha256": expected_sha256.lower(), "source_url": source_url})
        if total_bytes > shutil.disk_usage(self.storage_root).free:
            raise ModelAssetError("insufficient_storage", "模型存储空间不足", 507)
        transfer_id = f"mtr_{secrets.token_hex(8)}"
        quarantine_relpath = f"quarantine/{transfer_id}"
        self._inside(quarantine_relpath).mkdir(parents=False, exist_ok=False)
        now = utc_now()
        transfer = {"id": transfer_id, "direction": "download", "state": "queued",
                    "display_name": display_name, "media_kind": media_kind, "role": role,
                    "format": model_format, "source_type": source_type,
                    "source_ref": source_ref, "revision": revision,
                    "license_declared": license_declared, "expected_bytes": total_bytes,
                    "received_bytes": 0, "quarantine_relpath": quarantine_relpath,
                    "created_at": now, "updated_at": now}
        self.repository.insert_model_transfer(transfer, files, owner=owner)
        self._start_download(transfer_id)
        return self.get_transfer(transfer_id)

    def control_installation_transfer(self, owner, action: str) -> None:
        """Internal installation control; ownership and both states commit together."""
        operation = self.repository.get_service_installation(owner[0])
        expected_id = operation['transfer_id'] if operation else None
        with self._transfer_guard(expected_id) if expected_id else nullcontext():
            transfer_id = self.repository.control_service_installation(
                owner, action, utc_now(), expected_transfer_id=expected_id)
        if transfer_id is not None:
            self._start_download(transfer_id)

    def create_upload(self, payload: dict[str, Any]) -> dict[str, Any]:
        display_name = self._text(payload.get("display_name"), "display_name", 120)
        media_kind = self._choice(payload.get("media_kind"), MEDIA_KINDS, "media_kind")
        role = self._choice(payload.get("role"), MODEL_ROLES, "role")
        model_format = self._choice(payload.get("format"), MODEL_FORMATS, "format")
        if model_format == "trusted-bundle":
            raise ModelAssetError("unsafe_upload_format", "上传文件不能声明为受信任混合模型包")
        revision = self._text(payload.get("revision"), "revision", 160)
        license_declared = self._text(payload.get("license_declared", "unknown"),
                                      "license_declared", 160)
        raw_files = payload.get("files")
        if not isinstance(raw_files, list) or not raw_files or len(raw_files) > MAX_FILE_COUNT:
            raise ModelAssetError("invalid_file_manifest", "模型文件清单为空或数量超限")
        files: list[dict[str, Any]] = []
        folded: set[str] = set()
        total_bytes = 0
        for raw in raw_files:
            if not isinstance(raw, dict):
                raise ModelAssetError("invalid_file_manifest", "模型文件清单项无效")
            relative_path = normalize_relative_path(raw.get("relative_path"))
            if Path(relative_path).suffix.lower() in {".pth", ".pt", ".pkl", ".ckpt", ".bin", ".py"}:
                raise ModelAssetError("unsafe_upload_format", "上传清单包含不可执行或 Pickle 风险文件")
            key = relative_path.casefold()
            if key in folded:
                raise ModelAssetError("path_case_collision", "模型文件路径存在大小写冲突")
            folded.add(key)
            size = raw.get("byte_size")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise ModelAssetError("invalid_file_size", "模型文件大小无效")
            digest = raw.get("sha256")
            if digest is not None and (not isinstance(digest, str) or not self._is_sha256(digest)):
                raise ModelAssetError("invalid_sha256", "模型文件 SHA-256 无效")
            total_bytes += size
            files.append({"id": f"mfl_{secrets.token_hex(8)}", "relative_path": relative_path,
                          "expected_bytes": size, "expected_sha256": digest.lower() if digest else None})
        free_bytes = shutil.disk_usage(self.storage_root).free
        if total_bytes > free_bytes:
            raise ModelAssetError("insufficient_storage", "模型存储空间不足", 507)
        transfer_id = f"mtr_{secrets.token_hex(8)}"
        quarantine_relpath = f"quarantine/{transfer_id}"
        quarantine = self._inside(quarantine_relpath)
        quarantine.mkdir(parents=False, exist_ok=False)
        now = utc_now()
        transfer = {
            "id": transfer_id, "direction": "upload", "state": "queued",
            "display_name": display_name, "media_kind": media_kind, "role": role,
            "format": model_format, "source_type": "upload", "source_ref": "client-upload",
            "revision": revision, "license_declared": license_declared,
            "expected_bytes": total_bytes, "received_bytes": 0,
            "quarantine_relpath": quarantine_relpath, "created_at": now, "updated_at": now,
        }
        try:
            self.repository.insert_model_transfer(transfer, files)
        except Exception:
            quarantine.rmdir()
            raise
        return self.get_transfer(transfer_id)

    def preflight_import(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Resolve content reuse from a client-computed manifest without trusting it for publish."""
        raw_files = payload.get("files")
        if not isinstance(raw_files, list) or not raw_files or len(raw_files) > MAX_FILE_COUNT:
            raise ModelAssetError("invalid_file_manifest", "模型文件清单为空或数量超限")
        files: list[dict[str, Any]] = []
        folded: set[str] = set()
        for raw in raw_files:
            if not isinstance(raw, dict):
                raise ModelAssetError("invalid_file_manifest", "模型文件清单项无效")
            relative_path = normalize_relative_path(raw.get("relative_path"))
            key = relative_path.casefold()
            if key in folded:
                raise ModelAssetError("path_case_collision", "模型文件路径存在大小写冲突")
            folded.add(key)
            size, digest = raw.get("byte_size"), raw.get("sha256")
            if (not isinstance(size, int) or isinstance(size, bool) or size < 0
                    or not isinstance(digest, str) or not self._is_sha256(digest)):
                raise ModelAssetError("invalid_file_manifest", "模型预检文件身份无效")
            files.append({"relative_path": relative_path, "byte_size": size,
                          "sha256": digest.lower()})
        manifest_digest = self._manifest_digest(files)
        duplicate = next((item for item in self.repository.list_model_assets(limit=100_000)
                          if item["manifest_digest"] == manifest_digest
                          and item["state"] in {"ready", "archived"}), None)
        return {
            "manifest_digest": manifest_digest,
            "disposition": "reuse" if duplicate else "upload",
            "asset": self.get_asset(duplicate["id"]) if duplicate else None,
        }

    @serialized_transfer
    def append_upload_chunk(self, transfer_id: str, file_id: str, offset: int, data: bytes) -> dict[str, Any]:
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ModelAssetError("invalid_offset", "上传偏移量无效")
        if not data or len(data) > MAX_CHUNK_BYTES:
            raise ModelAssetError("invalid_chunk_size", "上传分块必须在 1 字节到 8 MiB 之间", 413)
        return self._append_chunk_locked(transfer_id, file_id, offset, data, 'upload')

    def _append_chunk_locked(self, transfer_id, file_id, offset, data, direction, token=None):
        from .artifacts import checked_path, identity
        transfer = self._transfer(transfer_id)
        if transfer['direction'] != direction:
            raise ModelAssetError('invalid_transfer_direction', '传输方向不匹配')
        # A rejected late request must not mkdir/open/write/truncate anything.
        if transfer['state'] not in {'queued', 'transferring'}:
            raise ModelAssetError('invalid_transfer_state', '当前传输状态不接受分块', 409)
        self._check_download_token(transfer_id, token)
        self._recover_pending_chunk(transfer_id)
        file = next((item for item in transfer["files"] if item["id"] == file_id), None)
        if file is None:
            raise ModelAssetError("model_transfer_file_not_found", "上传文件不存在", 404)
        if int(file["received_bytes"]) != offset:
            raise ModelAssetError("upload_offset_mismatch", "上传偏移量与服务器续传位置不一致", 409)
        if offset + len(data) > int(file['expected_bytes']):
            raise ModelAssetError('upload_size_exceeded', '上传分块超过已声明文件大小', 413)
        if transfer['quarantine_relpath'] != 'quarantine/' + transfer_id:
            raise ModelAssetError('invalid_model_path', '隔离区归属无效')
        parent = checked_path(self.storage_root / 'quarantine' / transfer_id)
        relative = PurePosixPath(normalize_relative_path(file['relative_path']))
        for part in relative.parts[:-1]:
            parent = parent / part
            parent.mkdir(exist_ok=True)
            checked_path(parent)
        target = parent / relative.name
        exists = target.exists()
        if exists:
            checked_path(target)
        flags = os.O_RDWR | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        if not exists:
            flags |= os.O_CREAT | os.O_EXCL
        fd = os.open(target, flags, 0o600)
        with os.fdopen(fd, 'r+b') as handle:
            opened = os.fstat(handle.fileno())
            if (not stat.S_ISREG(opened.st_mode)
                    or identity(opened) != identity(checked_path(target).stat())
                    or opened.st_size != offset):
                raise ModelAssetError('upload_file_changed', '上传临时文件与续传记录不一致', 409)
            self.repository.begin_model_transfer_write(
                transfer_id, file_id, offset, len(data), self._copy_identity(opened))
            handle.seek(offset)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            result = self.repository.append_model_transfer_bytes(
                transfer_id, file_id, offset, len(data), utc_now())
            if result != 'updated':
                # Roll back this owned descriptor only, never reopen a changed path.
                handle.truncate(offset)
                handle.flush()
                os.fsync(handle.fileno())
                pending = self.repository.get_model_transfer_write(transfer_id)
                if pending:
                    self.repository.clear_model_transfer_write(pending)
                raise ModelAssetError('transfer_state_conflict', '分块提交状态冲突', 409)
        return self.get_transfer(transfer_id)

    def _recover_pending_chunk(self, transfer_id):
        """Rollback ONLY a journaled, uncommitted append on the original inode.

        One bounded intent per transfer; committing progress removes it in the
        same DB transaction. A crash never authorizes truncating an unknown file.
        Caller holds the cross-process transfer lock.
        """
        from .artifacts import checked_path
        record = self.repository.get_model_transfer_write(transfer_id)
        if record is None:
            return
        transfer = self._transfer(transfer_id)
        file = next((item for item in transfer['files'] if item['id'] == record['file_id']), None)
        offset, count = record['write_offset'], record['byte_count']
        if (transfer['state'] not in {'queued','transferring','paused','failed','canceled'}
                or transfer['quarantine_relpath'] != 'quarantine/' + transfer_id or not file
                or file['received_bytes'] != offset or offset < 0 or not 1 <= count <= MAX_CHUNK_BYTES
                or offset + count > file['expected_bytes']):
            raise ModelAssetError('transfer_write_recovery_conflict', '未提交分块归属冲突，保留文件', 409)
        path = checked_path(self.storage_root/'quarantine'/transfer_id/normalize_relative_path(file['relative_path']))
        expected = json.loads(record['object_json'])
        fd = os.open(path, os.O_RDWR | getattr(os,'O_BINARY',0) | getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(fd, 'r+b') as stream:
            info = self._copy_identity(os.fstat(stream.fileno()))
            if (not all(info[key] == expected[key] for key in ('device','inode','mode','links'))
                    or info['mode'] != stat.S_IFREG or info['links'] != 1 or expected['size'] != offset
                    or not offset <= info['size'] <= offset + count
                    or info != self._copy_identity(checked_path(path).stat())):
                raise ModelAssetError('transfer_write_recovery_conflict', '未提交分块文件身份已变化，保留文件', 409)
            stream.truncate(offset)
            stream.flush()
            os.fsync(stream.fileno())
        self.repository.clear_model_transfer_write(record)

    def _check_download_token(self, transfer_id, token):
        if token is None:
            return
        with self.repository._connect() as db:
            run = db.execute('SELECT token,owner_pid,state FROM transfer_download_runs WHERE transfer_id=?',
                             (transfer_id,)).fetchone()
        if (run is None or run['token'] != token or run['owner_pid'] != os.getpid()
                or run['state'] != 'running'):
            raise ModelAssetError('download_owner_changed', '下载执行权已改变', 409)

    @serialized_transfer
    def complete_upload(self, transfer_id: str) -> dict[str, Any]:
        transfer = self._transfer(transfer_id)
        if transfer["direction"] != "upload":
            raise ModelAssetError("invalid_transfer_direction", "该传输不是上传任务")
        if any(int(item["received_bytes"]) != int(item["expected_bytes"])
               for item in transfer["files"]):
            raise ModelAssetError("upload_incomplete", "模型文件尚未全部上传", 409)
        if not self.repository.set_model_transfer_state(
                transfer_id, {"queued", "transferring", "paused"}, "verifying", utc_now()):
            raise ModelAssetError("invalid_transfer_state", "当前传输状态不能完成", 409)
        try:
            return self._verify_and_publish(self._transfer(transfer_id))
        except ModelAssetError as exc:
            self.repository.set_model_transfer_state(
                transfer_id, {"verifying"}, "failed", utc_now(),
                error_code=exc.code, error_message=exc.message)
            raise

    def pause(self, transfer_id: str) -> dict[str, Any]:
        self._transition(transfer_id, {"queued", "transferring"}, "paused")
        return self.get_transfer(transfer_id)

    def resume(self, transfer_id: str) -> dict[str, Any]:
        self._transition(transfer_id, {"paused"}, "queued")
        if self._transfer(transfer_id)["direction"] == "download":
            self._start_download(transfer_id)
        return self.get_transfer(transfer_id)

    def retry(self, transfer_id: str) -> dict[str, Any]:
        transfer = self._transfer(transfer_id)
        if transfer["direction"] != "download":
            raise ModelAssetError("invalid_transfer_direction", "上传失败后请按服务器续传位置继续上传")
        self._transition(transfer_id, {"failed"}, "queued")
        self._start_download(transfer_id)
        return self.get_transfer(transfer_id)

    def cancel(self, transfer_id: str) -> dict[str, Any]:
        self._transition(transfer_id, {"queued", "transferring", "paused"}, "canceled")
        return self.get_transfer(transfer_id)

    def get_transfer(self, transfer_id: str) -> dict[str, Any]:
        return self._public_transfer(self._transfer(transfer_id))

    def list_transfers(self, limit: int = 100) -> list[dict[str, Any]]:
        return [self._public_transfer(item) for item in self.repository.list_model_transfers(limit)]

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        item = self.repository.get_model_asset(asset_id)
        if item is None:
            raise ModelAssetError("model_asset_not_found", "模型资产不存在", 404)
        public = dict(item)
        public.pop("storage_relpath", None)
        for file in public["files"]:
            file.pop("storage_relpath", None)
        public["allowed_actions"] = (["archive"] if item["state"] == "ready" and
                                      item["deployment_references"] == 0 else
                                     ["restore"] if item["state"] == "archived" else [])
        return public

    def readonly_asset_path(self, asset_id: str, revision: str) -> Path:
        """Validate the registered revision and physical read-only asset tree.

        This does not re-hash multi-GiB weights or claim a container was tested.
        Publication owns their recorded hashes; paths/files cannot be symlinks.
        """
        from .artifacts import checked_path, open_regular
        item=self.repository.get_model_asset(asset_id)
        if item is None or item['state']!='ready' or item['revision']!=revision:
            raise ModelAssetError('model_asset_revision_mismatch','模型资产版本不匹配',409)
        relative=normalize_relative_path(item['storage_relpath'])
        root=checked_path(self.storage_root/relative)
        if self.storage_root not in root.parents:
            raise ModelAssetError('asset_path_escape','模型路径越界',409)
        with open_regular(root/'manifest.json') as stream:
            raw=stream.read(1024*1024+1)
        if len(raw)>1024*1024:raise ModelAssetError('model_manifest_invalid','模型清单超限',409)
        manifest=json.loads(raw)
        expected=[{key:file[key] for key in ('relative_path','sha256','byte_size')} for file in item['files']]
        if (manifest.get('asset_id')!=asset_id or manifest.get('manifest_digest')!=item['manifest_digest']
                or sorted(manifest.get('files',[]),key=lambda value:value['relative_path'])!=expected):
            raise ModelAssetError('model_manifest_invalid','模型清单与登记不符',409)
        for file in item['files']:
            with open_regular(root/normalize_relative_path(file['relative_path'])) as stream:
                if os.fstat(stream.fileno()).st_size!=file['byte_size']:
                    raise ModelAssetError('model_size_mismatch','模型文件大小变化',409)
        return root

    def list_assets(self, **filters: Any) -> list[dict[str, Any]]:
        items = self.repository.list_model_assets(**filters)
        return [{key: value for key, value in item.items() if key != "storage_relpath"}
                for item in items]

    def archive(self, asset_id: str) -> dict[str, Any]:
        result = self.repository.set_model_asset_archived(asset_id, True, utc_now())
        self._asset_action_result(result)
        return self.get_asset(asset_id)

    def restore(self, asset_id: str) -> dict[str, Any]:
        result = self.repository.set_model_asset_archived(asset_id, False, utc_now())
        self._asset_action_result(result)
        return self.get_asset(asset_id)

    def storage_summary(self) -> dict[str, Any]:
        usage = shutil.disk_usage(self.storage_root)
        with self._inventory_lock:
            inventory = dict(self._inventory)
        root_id = hashlib.sha256(str(self.storage_root).encode("utf-8")).hexdigest()[:16]
        return {"root_id": root_id, "capacity_bytes": usage.total, "free_bytes": usage.free,
                **self.repository.model_storage_totals(), **inventory,
                "max_chunk_bytes": MAX_CHUNK_BYTES}

    def start_maintenance(self):
        """Exactly one cancellable model-store maintenance loop per server."""
        with self._maintenance_lock:
            if self._maintenance_closed:
                raise RuntimeError('model store maintenance is closed')
            if self._maintenance_thread is not None and self._maintenance_thread.is_alive():
                return
            self._maintenance_stop.clear()
            thread = threading.Thread(
                target=self._maintenance_loop, name='model-store-maintenance', daemon=True)
            thread.start()
            self._maintenance_thread = thread

    def stop_maintenance(self, timeout=5):
        with self._maintenance_lock:
            self._maintenance_closed = True
            self._maintenance_stop.set()
            self._maintenance_wake.set()
            thread = self._maintenance_thread
            if thread is not None:
                thread.join(timeout)
            return thread is None or not thread.is_alive()

    def _maintenance_loop(self):
        while not self._maintenance_stop.is_set():
            self._maintenance_wake.clear()
            try:
                self.refresh_storage_inventory()
                self.cleanup_completed_transfers()
                self.refresh_storage_inventory()
            except InterruptedError:
                return
            except Exception:
                # A filesystem/DB outage cannot kill the server or busy-loop.
                with self._inventory_lock:
                    self._inventory['inventory_complete'] = False
                    self._inventory['inventory_error'] = 'model_store_maintenance_failed'
            self._maintenance_wake.wait(30)

    def _check_maintenance_stop(self):
        if self._maintenance_stop.is_set():
            raise InterruptedError('model_store_maintenance_stopped')

    def refresh_storage_inventory(self):
        """Read-only metadata scan, NEVER a cleanup candidate enumerator.

        Count all states and unowned entries. No content reads or persistent
        sampling writes. Publish one bounded snapshot; incomplete is not zero.
        """
        from .artifacts import checked_path
        total, count, visited, error = 0, 0, 0, None
        pending = [self.storage_root / 'quarantine']
        deadline = time.monotonic() + 5
        try:
            while pending:
                self._check_maintenance_stop()
                with os.scandir(checked_path(pending.pop())) as entries:
                    for entry in entries:
                        self._check_maintenance_stop()
                        visited += 1
                        if visited > 100_000 or time.monotonic() > deadline:
                            raise ValueError('model_inventory_limit')
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                            error = 'model_inventory_unresolved_entry'
                        elif stat.S_ISDIR(info.st_mode):
                            pending.append(Path(entry.path))
                        elif stat.S_ISREG(info.st_mode):
                            total += info.st_size
                            count += 1
                        else:
                            error = 'model_inventory_unresolved_entry'
        except InterruptedError:
            raise
        except Exception:
            error = 'model_inventory_incomplete'
        with self._inventory_lock:
            self._inventory = {'quarantine_bytes': total if error is None else None,
                               'quarantine_file_count': count if error is None else None,
                               'inventory_complete': error is None,
                               'inventory_updated_at': utc_now(), 'inventory_error': error}
        return dict(self._inventory)

    @staticmethod
    def _copy_identity(info):
        return {'device': info.st_dev, 'inode': info.st_ino, 'size': info.st_size,
                'mode': stat.S_IFMT(info.st_mode), 'mtime_ns': info.st_mtime_ns,
                'ctime_ns': info.st_ctime_ns, 'links': info.st_nlink}

    def _verified_file(self, path, *, expected=None, cancellable=False):
        from .artifacts import open_regular, checked_path
        with open_regular(path) as stream:
            before = self._copy_identity(os.fstat(stream.fileno()))
            if expected is not None and before != expected:
                raise ModelAssetError('cleanup_file_changed', '隔离副本身份已变化，保留文件', 409)
            digest = hashlib.sha256()
            while True:
                if cancellable:
                    self._check_maintenance_stop()
                block = stream.read(4 * 1024 * 1024)
                if not block:
                    break
                digest.update(block)
            if (before != self._copy_identity(os.fstat(stream.fileno()))
                    or before != self._copy_identity(checked_path(path).stat())):
                raise ModelAssetError('cleanup_file_changed', '模型文件在校验期间发生变化，保留副本', 409)
        return digest.hexdigest(), before

    def cleanup_completed_transfers(self, limit=8):
        """Only transaction-registered candidates; never discover old files."""
        completed = 0
        rows = self.repository.pending_transfer_cleanups(limit, after=self._cleanup_cursor)
        if not rows:
            self._cleanup_cursor = ''
            rows = self.repository.pending_transfer_cleanups(limit)
        for row in rows:
            self._check_maintenance_stop()
            self._cleanup_cursor = row['transfer_id']
            retry_at = self._cleanup_retries.get(row['transfer_id'], 0)
            if time.monotonic() < retry_at:
                continue
            self._cleanup_retries.pop(row['transfer_id'], None)
            try:
                with self._transfer_guard(row['transfer_id']):
                    current = self.repository.get_transfer_cleanup(row['transfer_id'])
                    if current is None or current['state'] != 'pending':
                        continue
                    self._cleanup_transfer(current)
                    self.repository.finish_transfer_cleanup(row['transfer_id'], 'done', None, utc_now())
                    completed += 1
            except InterruptedError:
                raise
            except (OSError, sqlite3.OperationalError):
                # Transient IO/DB failures retain their durable grant and retry
                # without a persistent error write on every sample.
                if len(self._cleanup_retries) >= 32:
                    self._cleanup_retries.pop(next(iter(self._cleanup_retries)))
                self._cleanup_retries[row['transfer_id']] = time.monotonic() + 120
            except ModelAssetError as exc:
                if exc.code != 'model_transfer_busy':
                    self.repository.finish_transfer_cleanup(row['transfer_id'], 'blocked', exc.code, utc_now())
            except Exception:
                self.repository.finish_transfer_cleanup(row['transfer_id'], 'blocked', 'cleanup_verification_failed', utc_now())
        return completed

    def _cleanup_transfer(self, row):
        from .artifacts import checked_path, open_regular
        transfer = self._transfer(row['transfer_id'])
        asset = self.repository.get_model_asset(row['asset_id'])
        files = json.loads(row['files_json'])
        fields = ('relative_path', 'sha256', 'byte_size', 'storage_relpath')
        if (transfer['state'] != 'succeeded' or transfer['asset_id'] != row['asset_id']
                or transfer['quarantine_relpath'] != 'quarantine/' + row['transfer_id']
                or not asset or asset['state'] not in {'ready', 'archived'}
                or asset['revision'] != row['revision'] or asset['manifest_digest'] != row['manifest_digest']
                or asset['storage_relpath'] != 'assets/' + row['asset_id']
                or not isinstance(files, list) or not 1 <= len(files) <= MAX_FILE_COUNT
                or [{key: file[key] for key in fields} for file in files]
                != [{key: file[key] for key in fields} for file in asset['files']]
                or [(file['relative_path'], file['byte_size'], file['byte_size']) for file in files]
                != [(file['relative_path'], file['expected_bytes'], file['received_bytes']) for file in transfer['files']]):
            raise ModelAssetError('cleanup_binding_mismatch', '副本归属与正式资产不一致，保留文件', 409)
        root = checked_path(self.storage_root / asset['storage_relpath'])
        with open_regular(root / 'manifest.json') as stream:
            raw = stream.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ModelAssetError('cleanup_manifest_invalid', '正式模型清单超限', 409)
        manifest = json.loads(raw)
        expected = [{key: file[key] for key in ('relative_path', 'sha256', 'byte_size')} for file in files]
        if (manifest != {'asset_id': row['asset_id'], 'manifest_digest': row['manifest_digest'], 'files': expected}
                or self._manifest_digest(files) != row['manifest_digest']):
            raise ModelAssetError('cleanup_manifest_invalid', '正式模型清单与副本归属不符', 409)
        for file in files:
            self._check_maintenance_stop()
            relative = normalize_relative_path(file['relative_path'])
            path = self.storage_root / 'quarantine' / row['transfer_id'] / relative
            checked_path(path.parent)
            try:
                path.lstat()
            except FileNotFoundError:
                # Unlink may have happened immediately before process exit.
                continue
            if file['identity']['links'] != 1:
                raise ModelAssetError('cleanup_copy_not_independent', '隔离副本不是独立文件，保留文件', 409)
            if file['storage_relpath'] != f"blobs/sha256/{file['sha256'][:2]}/{file['sha256']}":
                raise ModelAssetError('cleanup_blob_invalid', '内容库位置与摘要不符', 409)
            formal_paths = (root / relative, self.storage_root / file['storage_relpath'])
            formal_identities = []
            for formal in formal_paths:
                digest, info = self._verified_file(formal, cancellable=True)
                if digest != file['sha256'] or info['size'] != file['byte_size']:
                    raise ModelAssetError('cleanup_formal_copy_invalid', '正式模型副本校验失败，保留隔离副本', 409)
                formal_identities.append(info)
            digest, info = self._verified_file(path, expected=file['identity'], cancellable=True)
            if digest != file['sha256'] or info['size'] != file['byte_size']:
                raise ModelAssetError('cleanup_copy_hash_mismatch', '隔离副本摘要不符，保留文件', 409)
            self._check_maintenance_stop()
            if any(self._copy_identity(checked_path(formal).stat()) != expected_info
                   for formal, expected_info in zip(formal_paths, formal_identities)):
                raise ModelAssetError('cleanup_formal_copy_changed', '正式模型在回收前发生变化，保留副本', 409)
            self._unlink_owned_copy(path, info)

    def _unlink_owned_copy(self, path, expected):
        from .artifacts import checked_path, identity, fsync_directory
        # Exact precomputed file only. Never recurse, delete parents, or alter
        # formal assets. POSIX pins every directory component using NOFOLLOW.
        parents = [(part, identity(checked_path(part).stat())) for part in path.parents]
        directory = None
        try:
            if os.name == 'posix':
                directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
                for name in path.parts[1:-1]:
                    next_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    os.close(directory)
                    directory = next_fd
                info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            else:
                info = checked_path(path).lstat()
            if (self._copy_identity(info) != expected
                    or any(identity(checked_path(part).stat()) != old for part, old in parents)):
                raise ModelAssetError('cleanup_file_changed', '隔离副本或父目录已变化，保留文件', 409)
            self._check_maintenance_stop()
            if directory is None:
                path.unlink()
                fsync_directory(path.parent)
            else:
                os.unlink(path.name, dir_fd=directory)
                os.fsync(directory)
        finally:
            if directory is not None:
                os.close(directory)

    def recover_interrupted_transfers(self) -> int:
        recovered = 0
        for item in self.repository.list_model_transfers(100_000):
            if item["state"] in {"transferring", "verifying"}:
                try:
                    with self._transfer_guard(item['id']):
                        if self.repository.recover_model_transfer(item["id"], utc_now()):
                            recovered += 1
                except ModelAssetError as exc:
                    if exc.code != 'model_transfer_busy':
                        raise
        return recovered

    def _start_download(self, transfer_id: str) -> None:
        with self._download_lock:
            if transfer_id in self._downloading:
                return
            self._downloading.add(transfer_id)
        try:
            with self._transfer_guard(transfer_id):
                token = self.repository.claim_transfer_download(transfer_id)
        except BaseException:
            with self._download_lock:
                self._downloading.discard(transfer_id)
            raise
        if token is None:
            with self._download_lock:
                self._downloading.discard(transfer_id)
            return
        try:
            threading.Thread(target=self._download_worker, args=(transfer_id, token),
                             name=f"model-download-{transfer_id}", daemon=True).start()
        except Exception:
            self.repository.finish_transfer_download(transfer_id, token)
            with self._download_lock:
                self._downloading.discard(transfer_id)
            raise

    def _download_worker(self, transfer_id: str, token: str | None = None) -> None:
        if token is None:
            with self._transfer_guard(transfer_id, wait=True):
                token = self.repository.claim_transfer_download(transfer_id)
        if token is None:
            return
        try:
            transfer = self._transfer(transfer_id)
            for file in transfer["files"]:
                if int(file["received_bytes"]) == int(file["expected_bytes"]):
                    continue
                if self._download_file(transfer_id, transfer, file, token=token) is False:
                    return
            with self._transfer_guard(transfer_id, wait=True):
                self._check_download_token(transfer_id, token)
                completed = self._transfer(transfer_id)
                if int(completed["received_bytes"]) != int(completed["expected_bytes"]):
                    raise ModelAssetError("model_size_mismatch", "下载文件总大小与固定清单不一致")
                if not self.repository.set_model_transfer_state(
                        transfer_id, {"transferring", "queued"}, "verifying", utc_now()):
                    raise ModelAssetError("download_state_conflict", "下载完成状态发生并发冲突", 409)
                self._verify_and_publish(self._transfer(transfer_id))
        except Exception as exc:
            error = exc if isinstance(exc, ModelAssetError) else ModelAssetError(
                "download_failed", f"{type(exc).__name__}: {exc}")
            try:
                with self._transfer_guard(transfer_id, wait=True):
                    self._check_download_token(transfer_id, token)
                    self.repository.set_model_transfer_state(
                        transfer_id, {"queued", "transferring", "verifying"}, "failed", utc_now(),
                        error_code=error.code, error_message=error.message[-1000:])
            except ModelAssetError as owner_error:
                if owner_error.code not in {'download_owner_changed', 'model_transfer_busy'}:
                    raise
        finally:
            self.repository.finish_transfer_download(transfer_id, token)
            with self._download_lock:
                self._downloading.discard(transfer_id)
            current = self.repository.get_model_transfer(transfer_id)
            if current and current["state"] == "queued":
                self._start_download(transfer_id)  # Resume may race the paused worker's exit.

    def wake_installation_download(self, owner) -> None:
        self.repository.assert_installation_owner(owner)
        item = self.repository.get_service_installation(owner[0])
        if item and item["transfer_id"]:
            self._start_download(item["transfer_id"])

    def _download_file(self, transfer_id: str, transfer: dict[str, Any],
                       file: dict[str, Any], *, token: str | None = None) -> bool:
        offset = int(file['received_bytes'])
        source_url = file.get("source_url") or (
            transfer["source_ref"] if len(transfer["files"]) == 1 else None)
        if not isinstance(source_url, str):
            raise ModelAssetError("source_url_missing", "服务配方文件缺少固定下载地址")
        current_url = source_url
        redirects = 0
        while True:
            hostname, _addresses = validate_https_url(current_url, self.allowed_download_hosts)
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            with self._credential_lock:
                bearer_token = self._download_bearer_tokens.get(hostname)
            if bearer_token is not None:
                headers["Authorization"] = f"Bearer {bearer_token}"
            try:
                proxies = ({"http": self.download_proxy, "https": self.download_proxy}
                           if self.download_proxy else {})
                response = build_opener(ProxyHandler(proxies), _NoRedirect).open(
                    Request(current_url, headers=headers), timeout=60)
            except Exception as exc:
                if getattr(exc, "code", None) in {301, 302, 303, 307, 308}:
                    location = exc.headers.get("Location")
                    if not location or redirects >= 5:
                        raise ModelAssetError(
                            "source_redirect_blocked", "模型下载重定向无效或过多") from None
                    from urllib.parse import urljoin
                    current_url = urljoin(current_url, location)
                    redirects += 1
                    continue
                if getattr(exc, "code", None) in {401, 403}:
                    raise ModelAssetError(
                        "source_authentication_rejected",
                        "模型来源拒绝了凭据，请确认已接受模型许可且令牌具有读取权限",
                        409,
                    ) from None
                raise
            break
        status = int(getattr(response, "status", response.getcode()))
        if offset and status != 206:
            response.close()
            raise ModelAssetError("download_range_unsupported", "来源不支持安全续传，请重试安装")
        with response:
            while True:
                current = self._transfer(transfer_id)
                if current["state"] in {"paused", "canceled"}:
                    return False
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                # A network read may take60s. Never hold the transfer lock across it.
                with self._transfer_guard(transfer_id, wait=True):
                    if self._transfer(transfer_id)['state'] in {'paused', 'canceled'}:
                        return False
                    self._append_chunk_locked(transfer_id, file['id'], offset, chunk, 'download', token)
                offset += len(chunk)
        if offset != int(file["expected_bytes"]):
            raise ModelAssetError("model_size_mismatch", f"下载文件大小不符: {file['relative_path']}")
        return True

    def _verify_and_publish(self, transfer: dict[str, Any]) -> dict[str, Any]:
        verified: list[dict[str, Any]] = []
        cleanup_files = []
        from .artifacts import checked_path
        if transfer['quarantine_relpath'] != 'quarantine/' + transfer['id']:
            raise ModelAssetError('invalid_model_path', '隔离区归属无效')
        quarantine = checked_path(self.storage_root / transfer['quarantine_relpath'])
        for item in transfer["files"]:
            path = quarantine / normalize_relative_path(item['relative_path'])
            digest, identity = self._verified_file(path)
            size = identity['size']
            if size != int(item["expected_bytes"]):
                raise ModelAssetError("model_size_mismatch", "模型文件大小校验失败")
            if item["expected_sha256"] and digest != item["expected_sha256"].lower():
                raise ModelAssetError("model_hash_mismatch", "模型文件 SHA-256 校验失败")
            verified.append({"relative_path": item["relative_path"], "sha256": digest,
                             "byte_size": size,
                             "storage_relpath": f"blobs/sha256/{digest[:2]}/{digest}"})
            cleanup_files.append({**verified[-1], 'identity': identity})
        inspection = self._validate_format(
            transfer["format"], quarantine, verified, transfer["role"])
        manifest_digest = self._manifest_digest(verified)
        duplicate = next((item for item in self.repository.list_model_assets(limit=100_000)
                          if item["manifest_digest"] == manifest_digest), None)
        if duplicate is not None:
            self.repository.publish_model_asset(
                {**duplicate, 'updated_at': utc_now()}, verified, transfer['id'], cleanup_files=cleanup_files)
            self._maintenance_wake.set()
            return self.get_transfer(transfer["id"])
        asset_id = f"mdl_{secrets.token_hex(8)}"
        staging = self._inside(f"quarantine/{transfer['id']}/.asset-staging")
        staging.mkdir()
        for item in verified:
            source = quarantine / Path(item["relative_path"])
            blob = self._inside(item["storage_relpath"])
            blob.parent.mkdir(parents=True, exist_ok=True)
            if not blob.exists():
                shutil.copyfile(source, blob)
                blob.chmod(0o444)
            target = staging / Path(item["relative_path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(blob, target)
            except OSError:
                shutil.copyfile(blob, target)
                target.chmod(0o444)
        manifest = {"asset_id": asset_id, "manifest_digest": manifest_digest,
                    "files": [{key: item[key] for key in ("relative_path", "sha256", "byte_size")}
                              for item in verified]}
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        published = self._inside(f"assets/{asset_id}")
        staging.replace(published)
        now = utc_now()
        asset = {
            "id": asset_id, "display_name": transfer["display_name"],
            "media_kind": transfer["media_kind"], "role": transfer["role"],
            "format": transfer["format"], "source_type": transfer["source_type"],
            "source_ref": transfer["source_ref"], "revision": transfer["revision"],
            "license_declared": transfer["license_declared"],
            "manifest_digest": manifest_digest, "state": "ready",
            "total_bytes": sum(item["byte_size"] for item in verified),
            "file_count": len(verified), "storage_relpath": f"assets/{asset_id}",
            "architecture_family": inspection["architecture_family"],
            "tensor_precision": inspection.get("tensor_precision"),
            "parameter_summary": inspection["parameter_summary"],
            "metadata": inspection["metadata"],
            "detector_version": inspection["detector_version"],
            # Unknown legacy-style files retain their detector summary but may be
            # enriched once a later detector can prove an architecture identity.
            "metadata_digest": (inspection["metadata_digest"]
                                if inspection["architecture_family"] != "unknown" else None),
            "created_at": transfer["created_at"], "verified_at": now, "updated_at": now,
        }
        try:
            status, stored = self.repository.publish_model_asset(
                asset, verified, transfer["id"], cleanup_files=cleanup_files)
        except Exception:
            raise
        if status == "duplicate" and stored["id"] != asset_id:
            raise ModelAssetError("asset_publish_race", "相同模型资产已由另一传输发布", 409)
        self._maintenance_wake.set()
        return self.get_transfer(transfer["id"])

    def _validate_format(self, model_format: str, root: Path,
                         files: list[dict[str, Any]], role_hint: str) -> dict[str, Any]:
        paths = {item["relative_path"] for item in files}
        if model_format == "safetensors":
            candidates = [root / Path(path) for path in paths if path.endswith(".safetensors")]
            if len(candidates) != 1 or len(paths) != 1:
                raise ModelAssetError("model_format_mismatch", "Safetensors 单文件资产必须只包含一个权重文件")
            return self._inspect_safetensors(candidates[0], role_hint)
        elif model_format == "gguf":
            candidates = [root / Path(path) for path in paths if path.endswith(".gguf")]
            if not candidates:
                raise ModelAssetError("model_format_mismatch", "GGUF 资产缺少 .gguf 文件")
            for path in candidates:
                with path.open("rb") as handle:
                    if handle.read(4) != b"GGUF":
                        raise ModelAssetError("model_format_mismatch", "GGUF magic 无效")
                    version = struct.unpack("<I", handle.read(4))[0]
                    if version not in {2, 3}:
                        raise ModelAssetError("model_format_mismatch", "GGUF 版本不受支持")
            result = {"architecture_family": "unknown", "tensor_precision": "quantized",
                      "parameter_summary": {"file_count": len(candidates)}, "metadata": {},
                      "detector_version": "mc-gguf-1"}
        elif model_format == "diffusers":
            if "model_index.json" not in paths:
                raise ModelAssetError("model_format_mismatch", "Diffusers 资产缺少 model_index.json")
            index = self._validate_json_object(root / "model_index.json")
            class_name = str(index.get("_class_name", ""))
            family = "sdxl" if class_name in {
                "StableDiffusionXLPipeline", "StableDiffusionXLImg2ImgPipeline",
                "StableDiffusionXLInpaintPipeline",
            } else "unknown"
            candidates = [root / Path(path) for path in paths if path.endswith(".safetensors")]
            if not candidates:
                raise ModelAssetError("model_format_mismatch", "Diffusers 资产必须使用 Safetensors 权重")
            summaries = [self._inspect_safetensors(path, None) for path in candidates]
            result = {"architecture_family": family,
                      "tensor_precision": self._combined_precision(summaries),
                      "parameter_summary": {
                          "file_count": len(candidates),
                          "tensor_count": sum(item["parameter_summary"]["tensor_count"] for item in summaries),
                          "parameter_count": sum(item["parameter_summary"]["parameter_count"] for item in summaries),
                      },
                      "metadata": {"pipeline_class": class_name},
                      "detector_version": "mc-diffusers-1"}
        elif model_format == "transformers":
            if "config.json" not in paths:
                raise ModelAssetError("model_format_mismatch", "Transformers 资产缺少 config.json")
            self._validate_json_object(root / "config.json")
            if not any(path.endswith(".safetensors") for path in paths):
                raise ModelAssetError("model_format_mismatch", "Transformers 资产必须使用 Safetensors 权重")
            result = {"architecture_family": "unknown", "tensor_precision": None,
                      "parameter_summary": {"file_count": len(paths)}, "metadata": {},
                      "detector_version": "mc-transformers-1"}
        elif model_format == "trusted-bundle":
            result = {"architecture_family": "unknown", "tensor_precision": None,
                      "parameter_summary": {"file_count": len(paths)}, "metadata": {},
                      "detector_version": "mc-trusted-bundle-1"}
        else:
            raise ModelAssetError("unsafe_upload_format", "受信任混合模型包不能从上传提升")
        result["metadata_digest"] = canonical_digest(result)
        return result

    @staticmethod
    def _inspect_safetensors(path: Path, role_hint: str | None) -> dict[str, Any]:
        try:
            return inspect_safetensors(path, role_hint=role_hint)
        except ModelInspectionError as exc:
            raise ModelAssetError(exc.code, str(exc)) from None

    @staticmethod
    def _combined_precision(items: list[dict[str, Any]]) -> str | None:
        values = {item.get("tensor_precision") for item in items if item.get("tensor_precision")}
        return next(iter(values)) if len(values) == 1 else "mixed" if values else None

    @staticmethod
    def _validate_json_object(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            raise ModelAssetError("model_format_mismatch", f"{path.name} 不是有效 JSON") from None
        if not isinstance(value, dict):
            raise ModelAssetError("model_format_mismatch", f"{path.name} 必须是 JSON 对象")
        return value

    def _inside(self, relative_path: str) -> Path:
        candidate = (self.storage_root / Path(relative_path)).resolve(strict=False)
        try:
            candidate.relative_to(self.storage_root)
        except ValueError:
            raise ModelAssetError("model_path_escape", "模型存储路径越界") from None
        return candidate

    @serialized_transfer
    def _transition(self, transfer_id: str, expected: set[str], target: str) -> None:
        if target not in TRANSFER_STATES or not self.repository.set_model_transfer_state(
                transfer_id, expected, target, utc_now()):
            if self.repository.get_model_transfer(transfer_id) is None:
                raise ModelAssetError("model_transfer_not_found", "模型传输不存在", 404)
            raise ModelAssetError("invalid_transfer_state", "当前传输状态不允许该操作", 409)

    def _transfer(self, transfer_id: str) -> dict[str, Any]:
        if not isinstance(transfer_id, str) or not transfer_id.startswith("mtr_"):
            raise ModelAssetError("model_transfer_not_found", "模型传输不存在", 404)
        item = self.repository.get_model_transfer(transfer_id)
        if item is None:
            raise ModelAssetError("model_transfer_not_found", "模型传输不存在", 404)
        return item

    @staticmethod
    def _public_transfer(item: dict[str, Any]) -> dict[str, Any]:
        public = dict(item)
        public.pop("quarantine_relpath", None)
        public["files"] = [{key: value for key, value in file.items()
                            if key not in {"transfer_id", "source_url"}}
                           for file in item["files"]]
        state = item["state"]
        public["allowed_actions"] = ({"queued": ["cancel"],
                                      "transferring": ["pause", "cancel"],
                                      "paused": ["resume", "cancel"],
                                      "failed": []}.get(state, []))
        return public

    @staticmethod
    def _manifest_digest(files: list[dict[str, Any]]) -> str:
        digest = hashlib.sha256()
        for item in sorted(files, key=lambda value: value["relative_path"]):
            digest.update(item["relative_path"].encode("utf-8"))
            digest.update(b"\0")
            digest.update(item["sha256"].encode("ascii"))
            digest.update(b"\0")
            digest.update(str(item["byte_size"]).encode("ascii"))
        return digest.hexdigest()

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _is_sha256(value: str) -> bool:
        return len(value) == 64 and all(character in "0123456789abcdefABCDEF" for character in value)

    @staticmethod
    def _text(value: Any, name: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
            raise ModelAssetError(f"invalid_{name}", f"{name} 无效")
        return value.strip()

    @staticmethod
    def _choice(value: Any, allowed: set[str], name: str) -> str:
        if value not in allowed:
            raise ModelAssetError(f"invalid_{name}", f"{name} 不受支持")
        return str(value)

    @staticmethod
    def _asset_action_result(result: str) -> None:
        if result == "updated":
            return
        if result == "not_found":
            raise ModelAssetError("model_asset_not_found", "模型资产不存在", 404)
        if result == "referenced":
            raise ModelAssetError("model_asset_referenced", "模型资产仍被部署实例引用", 409)
        raise ModelAssetError("invalid_model_asset_state", "当前模型资产状态不允许该操作", 409)
