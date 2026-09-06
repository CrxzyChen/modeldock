#!/usr/bin/env python3
"""Transactional Linux installer for versioned MediaCenter server bundles."""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from verify_release_bundle import (
    ReleaseValidationError,
    canonical,
    digest_bytes,
    inspect_server_bundle,
    safe_relative,
    validate_server_manifest,
)


STATE_SCHEMA = "mc.server-install-state/1"
RECEIPT_SCHEMA = "mc.server-install-receipt/1"
PATH_TOKEN = re.compile(r"^/[A-Za-z0-9._/@+-]+$")
GPU_POOL = re.compile(r"^[0-9]+(?:,[0-9]+)*$")
RELEASE_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class InstallError(RuntimeError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise InstallError(code)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def lexical_absolute(path: Path) -> Path:
    """Make a path absolute without resolving a virtualenv executable symlink."""
    expanded = path.expanduser()
    if expanded.as_posix().startswith("/"):
        return expanded
    return Path(os.path.abspath(os.fspath(expanded)))


def is_posix_runtime() -> bool:
    return os.name == "posix"


def atomic_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{secrets.token_hex(6)}")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_state(data_root: Path) -> dict | None:
    path = data_root / "state/release-state.json"
    if not path.is_file() or path.is_symlink():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError("release_state_invalid") from exc
    keys = {"schema", "current_version", "previous_version", "manifest_sha256", "updated_at"}
    require(isinstance(value, dict) and set(value) == keys and value["schema"] == STATE_SCHEMA,
            "release_state_invalid")
    for key in ("current_version", "manifest_sha256", "updated_at"):
        require(isinstance(value[key], str) and value[key], "release_state_invalid")
    require(value["previous_version"] is None or isinstance(value["previous_version"], str),
            "release_state_invalid")
    require(RELEASE_VERSION.fullmatch(value["current_version"]) is not None,
            "release_state_invalid")
    require(value["previous_version"] is None
            or RELEASE_VERSION.fullmatch(value["previous_version"]) is not None,
            "release_state_invalid")
    require(SHA256.fullmatch(value["manifest_sha256"]) is not None, "release_state_invalid")
    return value


def release_paths(data_root: Path, version: str) -> tuple[Path, Path]:
    release = data_root / "releases" / version
    current = data_root / "current"
    return release, current


def current_version(data_root: Path) -> str | None:
    state = read_state(data_root)
    if state is None:
        require(not (data_root / "current").exists(), "release_pointer_without_state")
        return None
    release, current = release_paths(data_root, state["current_version"])
    require(current.is_symlink(), "release_current_pointer_invalid")
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise InstallError("release_current_pointer_broken") from exc
    require(resolved == release.resolve(strict=True), "release_current_pointer_mismatch")
    return state["current_version"]


def preflight(bundle: Path, data_root: Path, control_python: Path) -> dict:
    inspected = inspect_server_bundle(bundle)
    require(sys.version_info >= (3, 10), "installer_python_too_old")
    # The lexical POSIX path check also keeps preflight reproducible on a Windows
    # release workstation, where pathlib does not classify /usr/bin as absolute.
    require(PATH_TOKEN.fullmatch(control_python.as_posix()) is not None,
            "control_python_path_invalid")
    existing = data_root if data_root.exists() else data_root.parent
    if data_root.exists():
        require(data_root.is_dir() and not data_root.is_symlink(), "data_root_invalid")
    require(existing.exists() and existing.is_dir(), "data_root_parent_missing")
    usage = shutil.disk_usage(existing)
    payload_bytes = sum(item["bytes"] for item in inspected["manifest"]["files"])
    required_bytes = bundle.stat().st_size + payload_bytes * 2 + 64 * 1024 * 1024
    require(usage.free >= required_bytes, "server_release_disk_space_insufficient")
    return {
        "schema": "mc.server-install-preflight/1",
        "version": inspected["manifest"]["version"],
        "bundle_manifest_sha256": inspected["manifest_sha256"],
        "files": inspected["files"],
        "required_bytes": required_bytes,
        "free_bytes": usage.free,
        "data_root": str(data_root),
        "control_python": str(control_python),
        "status": "passed",
        "mutations_performed": [],
    }


def verify_installed_release(release: Path, manifest: dict) -> None:
    require(release.is_dir() and not release.is_symlink(), "installed_release_missing_or_unsafe")
    expected = {item["path"]: item for item in manifest["files"]}
    actual = {
        path.relative_to(release).as_posix()
        for path in release.rglob("*") if path.is_file()
    }
    expected_with_manifest = set(expected) | {"release/server-manifest.json"}
    require(actual == expected_with_manifest, "installed_release_file_set_mismatch")
    for relative, item in expected.items():
        path = release / relative
        require(path.is_file() and not path.is_symlink(), "installed_release_file_unsafe")
        content = path.read_bytes()
        require(len(content) == item["bytes"] and digest_bytes(content) == item["sha256"],
                "installed_release_file_digest_mismatch")


def extract_release(bundle: Path, data_root: Path, inspected: dict) -> tuple[Path, bool]:
    manifest = inspected["manifest"]
    version = manifest["version"]
    release, _current = release_paths(data_root, version)
    if release.exists():
        verify_installed_release(release, manifest)
        return release, False
    releases = data_root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    require(releases.is_dir() and not releases.is_symlink(), "release_directory_invalid")
    staging = releases / f".staging-{version}-{secrets.token_hex(6)}"
    staging.mkdir(parents=False, exist_ok=False)
    try:
        with tarfile.open(bundle, "r:gz") as archive:
            root = manifest["root"] + "/"
            for member in archive.getmembers():
                require(member.isfile(), "server_bundle_members_invalid")
                name = safe_relative(member.name, code="server_bundle_path_invalid")
                require(name.startswith(root), "server_bundle_root_mismatch")
                relative = name.removeprefix(root)
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(member)
                require(stream is not None, "server_bundle_file_unreadable")
                with target.open("xb") as output:
                    shutil.copyfileobj(stream, output, length=1024 * 1024)
                os.chmod(target, member.mode)
        verify_installed_release(staging, manifest)
        os.replace(staging, release)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return release, True


def render_environment(template: Path, data_root: Path, gpu_pool: str | None,
                       gpu_uuids: str | None, host: str, port: int) -> bytes:
    require(gpu_pool is not None and GPU_POOL.fullmatch(gpu_pool) is not None,
            "clean_install_gpu_pool_required")
    values = [int(item) for item in gpu_pool.split(",")]
    require(len(values) == len(set(values)), "clean_install_gpu_pool_invalid")
    if gpu_uuids:
        uuids = [item.strip() for item in gpu_uuids.split(",")]
        require(len(uuids) == len(values) and all(item.startswith("GPU-") for item in uuids),
                "clean_install_gpu_uuid_pool_invalid")
    else:
        uuids = []
    require(host and "\n" not in host and "\r" not in host, "clean_install_host_invalid")
    require(1 <= port <= 65535, "clean_install_port_invalid")
    body = template.read_text(encoding="utf-8")
    replacements = {
        "@MEDIACENTER_DATA_ROOT@": data_root.as_posix(),
        "@MEDIACENTER_HOST@": host,
        "@MEDIACENTER_PORT@": str(port),
        "@MEDIACENTER_GPU_POOL@": gpu_pool,
        "@MEDIACENTER_GPU_UUID_POOL@": ",".join(uuids),
    }
    for key, value in replacements.items():
        body = body.replace(key, value)
    require("@MEDIACENTER_" not in body, "environment_template_unresolved")
    return body.encode("utf-8")


def render_service(template: Path, data_root: Path, control_python: Path) -> bytes:
    body = template.read_text(encoding="utf-8")
    body = body.replace("@MEDIACENTER_DATA_ROOT@", data_root.as_posix())
    body = body.replace("@MEDIACENTER_CONTROL_PYTHON@", control_python.as_posix())
    require("@MEDIACENTER_" not in body, "service_template_unresolved")
    return body.encode("utf-8")


def configured_api_key_file(environment_file: Path, data_root: Path) -> Path:
    try:
        lines = environment_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise InstallError("environment_file_invalid") from exc
    values = [line.split("=", 1)[1] for line in lines
              if line.startswith("MEDIACENTER_API_KEY_FILE=")]
    require(len(values) == 1 and values[0], "environment_api_key_file_invalid")
    key_file = Path(values[0]).resolve(strict=True)
    try:
        key_file.relative_to(data_root.resolve(strict=True))
    except ValueError as exc:
        raise InstallError("environment_api_key_file_outside_data_root") from exc
    require(key_file.is_file() and not key_file.is_symlink(), "api_key_file_invalid")
    return key_file


def ensure_runtime_layout(data_root: Path, release: Path, service_dir: Path,
                          control_python: Path, *, gpu_pool: str | None,
                          gpu_uuids: str | None, host: str, port: int) -> dict:
    for relative in ("config", "state", "artifacts", "models", "inputs", "runtime", "releases", "receipts"):
        directory = data_root / relative
        directory.mkdir(parents=True, exist_ok=True)
        require(directory.is_dir() and not directory.is_symlink(), "runtime_directory_invalid")
    env_file = data_root / "config/mediacenter.env"
    env_created = False
    key_created = False
    if env_file.exists():
        require(env_file.is_file() and not env_file.is_symlink(), "environment_file_invalid")
        key_file = configured_api_key_file(env_file, data_root)
    else:
        key_file = data_root / "config/api-key"
        if not key_file.exists():
            atomic_write(key_file, (secrets.token_urlsafe(32) + "\n").encode("ascii"), 0o600)
            key_created = True
        require(key_file.is_file() and not key_file.is_symlink(), "api_key_file_invalid")
        body = render_environment(release / "deploy/mediacenter.env.example", data_root,
                                  gpu_pool, gpu_uuids, host, port)
        atomic_write(env_file, body, 0o600)
        env_created = True
    service_dir.mkdir(parents=True, exist_ok=True)
    service_file = service_dir / "mediacenter.service"
    atomic_write(service_file, render_service(release / "deploy/mediacenter.service",
                                              data_root, control_python), 0o600)
    redis_service_file = service_dir / "mediacenter-redis.service"
    atomic_write(redis_service_file,
                 render_service(release / "deploy/mediacenter-redis.service",
                                data_root, control_python), 0o600)
    return {"api_key_created": key_created, "api_key_file": str(key_file),
            "environment_created": env_created, "service_file": str(service_file),
            "redis_service_file": str(redis_service_file)}


def switch_current(data_root: Path, version: str) -> None:
    require(os.name == "posix", "release_switch_requires_posix")
    release, current = release_paths(data_root, version)
    require(release.is_dir() and not release.is_symlink(), "release_switch_target_invalid")
    require(not current.exists() or current.is_symlink(), "release_current_path_not_symlink")
    temporary = data_root / f".current-{secrets.token_hex(6)}"
    try:
        os.symlink(release, temporary, target_is_directory=True)
        os.replace(temporary, current)
    finally:
        if temporary.is_symlink():
            temporary.unlink()


def systemctl(action: str, *, allow_missing: bool = False) -> None:
    require(action in {"stop", "start", "daemon-reload"}, "systemctl_action_invalid")
    command = (["systemctl", "--user", action, "mediacenter.service"] if action != "daemon-reload"
               else ["systemctl", "--user", "daemon-reload"])
    result = subprocess.run(command, check=False, timeout=120)
    require(result.returncode == 0 or (allow_missing and result.returncode == 5),
            f"systemctl_{action}_failed")


def wait_health(url: str, api_key_file: Path, version: str, timeout: int = 120) -> dict:
    deadline = time.monotonic() + timeout
    last_error = "health_timeout"
    while time.monotonic() < deadline:
        try:
            with urlopen(url.rstrip("/") + "/healthz", timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))
                require(response.status == 200 and body.get("status") == "ok"
                        and body.get("version") == version, "health_contract_invalid")
            key = api_key_file.read_text(encoding="utf-8").strip()
            request = Request(url.rstrip("/") + "/api/v1/overview", headers={"X-API-Key": key})
            with urlopen(request, timeout=5) as response:
                require(response.status == 200, "authenticated_health_invalid")
                response.read()
            return {"url": url, "version": version, "health": 200, "authenticated_overview": 200}
        except (OSError, HTTPError, URLError, json.JSONDecodeError, InstallError) as exc:
            last_error = str(exc)
            time.sleep(2)
    raise InstallError(f"health_timeout:{last_error}")


def write_state(data_root: Path, current: str, previous: str | None, manifest_sha: str) -> dict:
    value = {
        "schema": STATE_SCHEMA,
        "current_version": current,
        "previous_version": previous,
        "manifest_sha256": manifest_sha,
        "updated_at": utc_now(),
    }
    atomic_write(data_root / "state/release-state.json", canonical(value), 0o600)
    return value


def write_receipt(data_root: Path, value: dict) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = data_root / "receipts" / f"{stamp}-{value['action']}.json"
    atomic_write(path, canonical(value), 0o600)
    return path


def mutate(action: str, bundle: Path | None, data_root: Path, service_dir: Path,
           control_python: Path, *, service_control: bool, gpu_pool: str | None,
           gpu_uuids: str | None, host: str, port: int, health_url: str) -> dict:
    require(is_posix_runtime(), "server_install_mutation_requires_linux")
    require(control_python.is_file() and os.access(control_python, os.X_OK),
            "control_python_missing_or_not_executable")
    started_at = utc_now()
    before = current_version(data_root) if data_root.exists() else None
    state_before = read_state(data_root) if data_root.exists() else None
    if action in {"install", "upgrade"}:
        require(bundle is not None, "server_bundle_required")
        checked = preflight(bundle, data_root, control_python)
        inspected = inspect_server_bundle(bundle)
        target = inspected["manifest"]["version"]
        if action == "install":
            require(before is None, "install_requires_empty_release_state")
        else:
            require(before is not None and before != target, "upgrade_requires_different_current_release")
        manifest_sha = inspected["manifest_sha256"]
    else:
        require(action == "rollback" and state_before is not None
                and state_before["previous_version"] is not None, "rollback_point_missing")
        target = state_before["previous_version"]
        release, _current = release_paths(data_root, target)
        manifest_path = release / "release/server-manifest.json"
        require(manifest_path.is_file() and not manifest_path.is_symlink(), "rollback_manifest_missing")
        try:
            manifest_bytes = manifest_path.read_bytes()
            rollback_manifest = validate_server_manifest(json.loads(manifest_bytes.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InstallError("rollback_manifest_invalid") from exc
        require(rollback_manifest["version"] == target, "rollback_manifest_version_mismatch")
        verify_installed_release(release, rollback_manifest)
        manifest_sha = digest_bytes(manifest_bytes)
        checked = {"status": "passed", "version": target,
                   "bundle_manifest_sha256": manifest_sha, "mutations_performed": []}
        inspected = None
    created = False
    layout = None
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "action": action,
        "status": "running",
        "started_at": started_at,
        "finished_at": None,
        "from_version": before,
        "to_version": target,
        "manifest_sha256": manifest_sha,
        "release_directory_created": created,
        "layout": layout,
        "service_control": service_control,
        "health": None,
        "rollback_performed": False,
        "recovery_error": None,
        "error": None,
    }
    switched = False
    service_stopped = False
    try:
        if action in {"install", "upgrade"}:
            require(bundle is not None and inspected is not None, "server_bundle_required")
            release, created = extract_release(bundle, data_root, inspected)
        else:
            release, _current = release_paths(data_root, target)
        layout = ensure_runtime_layout(data_root, release, service_dir, control_python,
                                       gpu_pool=gpu_pool, gpu_uuids=gpu_uuids,
                                       host=host, port=port)
        receipt["release_directory_created"] = created
        receipt["layout"] = layout
        if service_control and before is not None:
            systemctl("stop")
            service_stopped = True
        switch_current(data_root, target)
        switched = True
        write_state(data_root, target, before, manifest_sha)
        if service_control:
            systemctl("daemon-reload")
            systemctl("start")
            require(layout is not None, "runtime_layout_missing")
            receipt["health"] = wait_health(health_url, Path(layout["api_key_file"]), target)
            service_stopped = False
        receipt["status"] = "succeeded"
    except Exception as exc:
        receipt["error"] = f"{type(exc).__name__}:{exc}"
        try:
            if switched:
                if service_control and service_stopped:
                    systemctl("stop", allow_missing=True)
                if before is not None:
                    switch_current(data_root, before)
                    require(state_before is not None, "rollback_state_missing")
                    atomic_write(data_root / "state/release-state.json", canonical(state_before), 0o600)
                    receipt["rollback_performed"] = True
                else:
                    current = data_root / "current"
                    if current.is_symlink():
                        current.unlink()
                    state_path = data_root / "state/release-state.json"
                    if state_path.is_file() and not state_path.is_symlink():
                        state_path.unlink()
            if service_control and service_stopped and before is not None:
                require(layout is not None, "runtime_layout_missing")
                systemctl("daemon-reload")
                systemctl("start")
                wait_health(health_url, Path(layout["api_key_file"]), before)
        except Exception as recovery_exc:
            receipt["recovery_error"] = f"{type(recovery_exc).__name__}:{recovery_exc}"
        receipt["status"] = "failed"
        receipt["finished_at"] = utc_now()
        path = write_receipt(data_root, receipt)
        raise InstallError(f"release_{action}_failed receipt={path}") from exc
    receipt["finished_at"] = utc_now()
    path = write_receipt(data_root, receipt)
    return {"receipt": str(path), "preflight": checked, **receipt}


def status(data_root: Path) -> dict:
    state = read_state(data_root) if data_root.exists() else None
    current = current_version(data_root) if state else None
    return {
        "schema": "mc.server-install-status/1",
        "data_root": str(data_root),
        "installed": state is not None,
        "current_version": current,
        "previous_version": state["previous_version"] if state else None,
        "manifest_sha256": state["manifest_sha256"] if state else None,
        "status": "installed" if state else "not_installed",
        "mutations_performed": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "install", "upgrade", "rollback", "status"))
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--data-root", type=Path, default=Path("/srv/mediacenter"))
    parser.add_argument("--service-dir", type=Path, default=Path.home() / ".config/systemd/user")
    parser.add_argument("--control-python", type=Path,
                        default=Path("/srv/mediacenter/control-runtime-v2/bin/python"))
    parser.add_argument("--gpu-pool")
    parser.add_argument("--gpu-uuids")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--health-url", default="http://127.0.0.1:8787")
    parser.add_argument("--no-service-control", action="store_true")
    args = parser.parse_args()
    bundle = args.bundle.resolve(strict=True) if args.bundle else None
    data_root = args.data_root.resolve()
    service_dir = args.service_dir.resolve()
    control_python = lexical_absolute(args.control_python)
    try:
        if args.action == "preflight":
            require(bundle is not None, "server_bundle_required")
            result = preflight(bundle, data_root, control_python)
        elif args.action == "status":
            result = status(data_root)
        else:
            result = mutate(args.action, bundle, data_root, service_dir, control_python,
                            service_control=not args.no_service_control,
                            gpu_pool=args.gpu_pool, gpu_uuids=args.gpu_uuids,
                            host=args.host, port=args.port, health_url=args.health_url)
    except (OSError, subprocess.SubprocessError, tarfile.TarError,
            ReleaseValidationError, InstallError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
