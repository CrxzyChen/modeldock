#!/usr/bin/env python3
"""One-time, explicit adoption of a running pre-release server as rollback baseline."""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

from install_server_release import (
    InstallError,
    atomic_write,
    current_version,
    lexical_absolute,
    render_service,
    require,
    switch_current,
    systemctl,
    utc_now,
    verify_installed_release,
    wait_health,
    write_receipt,
    write_state,
)
from verify_release_bundle import (
    canonical,
    digest_bytes,
    scan_payload,
    validate_server_manifest,
)


ENVIRONMENT_REQUIRED = {
    "MEDIACENTER_HOST", "MEDIACENTER_PORT", "MEDIACENTER_API_KEY_FILE",
    "MEDIACENTER_DATA_ROOT", "MEDIACENTER_DB", "MEDIACENTER_GPU_POOL",
    "MEDIACENTER_MODEL_CATALOG", "MEDIACENTER_RUNTIME_CONFIG",
}
ENVIRONMENT_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
LEGACY_RUNTIME_DEPLOY_FILES = (
    "deploy/container-release-plan.json",
    "deploy/container-security.json",
    "deploy/redis-acl.template",
    "deploy/redis-runtime.json",
    "deploy/redis.conf",
)
LEGACY_OPTIONAL_DEPLOY_FILES = (
    "deploy/mediacenter-redis.service",
    "deploy/nginx-mediacenter-sse.conf",
)


def under(path: Path, root: Path, code: str) -> Path:
    path = path.resolve(strict=True)
    try:
        path.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise InstallError(code) from exc
    return path


def environment_values(path: Path) -> tuple[list[str], dict[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise InstallError("legacy_environment_invalid") from exc
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        require("=" in line, "legacy_environment_invalid")
        key, value = line.split("=", 1)
        require(ENVIRONMENT_KEY.fullmatch(key) is not None and key not in values,
                "legacy_environment_invalid")
        require("\r" not in value and "\n" not in value, "legacy_environment_invalid")
        values[key] = value
    require(ENVIRONMENT_REQUIRED <= set(values), "legacy_environment_incomplete")
    require("MEDIACENTER_API_KEY" not in values, "legacy_environment_inline_key_forbidden")
    return lines, values


def legacy_payload(legacy_release: Path, model_catalog: Path, service_template: Path,
                   environment_template: Path) -> tuple[list[dict], dict[str, bytes]]:
    candidates: dict[str, Path] = {}
    for relative_root, required in (("mediacenter", True), ("containers", False),
                                    ("deploy/production-images", False)):
        base = legacy_release / relative_root
        if not base.exists() and not required:
            continue
        require(base.is_dir() and not base.is_symlink(), "legacy_release_runtime_missing")
        for path in sorted(base.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            relative = path.relative_to(legacy_release).as_posix()
            candidates[relative] = path
    for relative in ("README.md", "pyproject.toml"):
        path = legacy_release / relative
        if path.is_file():
            candidates[relative] = path
    require(model_catalog.is_file() and not model_catalog.is_symlink(),
            "legacy_model_catalog_missing")
    candidates["deploy/model_catalog.json"] = model_catalog
    for relative in LEGACY_RUNTIME_DEPLOY_FILES:
        path = legacy_release / relative
        require(path.is_file() and not path.is_symlink(), "legacy_runtime_support_missing")
        candidates[relative] = path
    for relative in LEGACY_OPTIONAL_DEPLOY_FILES:
        path = legacy_release / relative
        if path.is_file() and not path.is_symlink():
            candidates[relative] = path
    candidates["deploy/mediacenter.service"] = service_template.resolve(strict=True)
    candidates["deploy/mediacenter.env.example"] = environment_template.resolve(strict=True)
    records, payload = [], {}
    for relative, path in sorted(candidates.items()):
        require(path.is_file() and not path.is_symlink(), "legacy_release_file_unsafe")
        content = path.read_bytes()
        scan_payload(relative, content)
        mode = 0o755 if os.access(path, os.X_OK) else 0o644
        records.append({"path": relative, "bytes": len(content),
                        "sha256": digest_bytes(content), "mode": mode})
        payload[relative] = content
    require(records, "legacy_release_payload_empty")
    return records, payload


def render_stable_environment(lines: list[str], data_root: Path) -> bytes:
    replacement = f"MEDIACENTER_MODEL_CATALOG={data_root.as_posix()}/current/deploy/model_catalog.json"
    rendered, replacements = [], 0
    for line in lines:
        if line.startswith("MEDIACENTER_MODEL_CATALOG="):
            rendered.append(replacement)
            replacements += 1
        else:
            rendered.append(line)
    require(replacements == 1, "legacy_model_catalog_binding_invalid")
    return ("\n".join(rendered) + "\n").encode("utf-8")


def adoption_manifest(version: str, records: list[dict]) -> dict:
    return {
        "schema": "mc.server-bundle/1", "product": "MediaCenter", "version": version,
        "root": f"MediaCenter-server-{version}", "files": records,
        "security": {"credentials_embedded": False, "model_weights_embedded": False,
                     "database_embedded": False, "media_embedded": False},
    }


def inspect(action: str, data_root: Path, legacy_release: Path, legacy_environment: Path,
            version: str, service_file: Path, service_template: Path,
            environment_template: Path, health_url: str) -> dict:
    require(VERSION.fullmatch(version) is not None, "legacy_version_invalid")
    require(data_root.is_dir() and not data_root.is_symlink(), "data_root_invalid")
    require(current_version(data_root) is None, "formal_release_state_already_exists")
    legacy_release = under(legacy_release, data_root / "releases", "legacy_release_outside_data_root")
    require(legacy_release.is_dir() and not legacy_release.is_symlink(), "legacy_release_invalid")
    legacy_environment = under(legacy_environment, data_root / "config",
                               "legacy_environment_outside_data_root")
    require(legacy_environment.is_file() and not legacy_environment.is_symlink(),
            "legacy_environment_invalid")
    require(service_file.is_file() and not service_file.is_symlink(), "legacy_service_file_invalid")
    try:
        service_body = service_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InstallError("legacy_service_file_invalid") from exc
    require(f"WorkingDirectory={legacy_release.as_posix()}" in service_body,
            "legacy_service_release_binding_mismatch")
    require(f"EnvironmentFile={legacy_environment.as_posix()}" in service_body,
            "legacy_service_environment_binding_mismatch")
    lines, values = environment_values(legacy_environment)
    require(Path(values["MEDIACENTER_DATA_ROOT"]).resolve() == data_root,
            "legacy_environment_data_root_mismatch")
    for key in ("MEDIACENTER_API_KEY_FILE", "MEDIACENTER_DB", "MEDIACENTER_RUNTIME_CONFIG"):
        path = under(Path(values[key]), data_root, f"legacy_{key.lower()}_outside_data_root")
        require(path.is_file() and not path.is_symlink(), f"legacy_{key.lower()}_missing")
    model_catalog = under(Path(values["MEDIACENTER_MODEL_CATALOG"]), data_root / "releases",
                          "legacy_model_catalog_outside_releases")
    records, payload = legacy_payload(
        legacy_release, model_catalog, service_template, environment_template,
    )
    manifest = adoption_manifest(version, records)
    target = data_root / "releases" / version
    target_reused = False
    if target.exists():
        manifest_path = target / "release/server-manifest.json"
        require(manifest_path.is_file() and not manifest_path.is_symlink(),
                "legacy_target_release_unverified")
        try:
            existing_manifest = validate_server_manifest(
                json.loads(manifest_path.read_text(encoding="utf-8")),
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise InstallError("legacy_target_release_unverified") from exc
        require(existing_manifest == manifest, "legacy_target_release_conflict")
        verify_installed_release(target, manifest)
        target_reused = True
    stable_path = data_root / "config/mediacenter.env"
    stable_environment_before = None
    if stable_path.exists():
        require(stable_path.is_file() and not stable_path.is_symlink(),
                "stable_environment_invalid")
        stable_environment_before = {
            "bytes": stable_path.stat().st_size,
            "sha256": digest_bytes(stable_path.read_bytes()),
        }
    try:
        with urlopen(health_url.rstrip("/") + "/healthz", timeout=5) as response:
            health = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise InstallError("legacy_health_unavailable") from exc
    require(response.status == 200 and health.get("status") == "ok"
            and health.get("version") == version, "legacy_health_identity_mismatch")
    return {
        "schema": "mc.legacy-server-adoption-preflight/1",
        "action": action,
        "status": "passed",
        "version": version,
        "data_root": str(data_root),
        "legacy_release": str(legacy_release),
        "legacy_environment": str(legacy_environment),
        "service_file": str(service_file),
        "health": {"status": health["status"], "version": health["version"]},
        "files": len(records),
        "payload_bytes": sum(item["bytes"] for item in records),
        "source_digest": digest_bytes(canonical(records)),
        "target_reused": target_reused,
        "stable_environment_before": stable_environment_before,
        "stable_environment": render_stable_environment(lines, data_root),
        "records": records,
        "payload": payload,
        "mutations_performed": [],
    }


def materialize(data_root: Path, version: str, records: list[dict],
                payload: dict[str, bytes]) -> tuple[Path, dict, str]:
    releases = data_root / "releases"
    staging = releases / f".adopt-{version}-{secrets.token_hex(6)}"
    target = releases / version
    manifest = adoption_manifest(version, records)
    manifest_bytes = canonical(manifest)
    if target.exists():
        verify_installed_release(target, manifest)
        return target, manifest, digest_bytes(manifest_bytes)
    staging.mkdir(mode=0o700)
    for item in records:
        path = staging / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(payload[item["path"]])
        os.chmod(path, item["mode"])
    atomic_write(staging / "release/server-manifest.json", manifest_bytes, 0o644)
    verify_installed_release(staging, manifest)
    os.replace(staging, target)
    return target, manifest, digest_bytes(manifest_bytes)


def apply_adoption(plan: dict, data_root: Path, service_file: Path,
                   service_template: Path, control_python: Path, health_url: str) -> dict:
    require(os.name == "posix", "legacy_adoption_requires_linux")
    version = plan["version"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = data_root / "backups" / f"release-adoption-{stamp}"
    backup.mkdir(mode=0o700)
    atomic_write(backup / "mediacenter.service", service_file.read_bytes(), 0o600)
    atomic_write(backup / "legacy-environment", Path(plan["legacy_environment"]).read_bytes(), 0o600)
    if plan["stable_environment_before"] is not None:
        atomic_write(backup / "previous-mediacenter.env",
                     (data_root / "config/mediacenter.env").read_bytes(), 0o600)
    receipt = {
        "schema": "mc.legacy-server-adoption-receipt/1", "action": "adopt",
        "status": "running", "started_at": utc_now(), "finished_at": None,
        "version": version, "legacy_release": plan["legacy_release"],
        "legacy_environment": plan["legacy_environment"], "backup": str(backup),
        "source_digest": plan["source_digest"], "manifest_sha256": None,
        "health": None, "release_directory_reused": plan["target_reused"],
        "stable_environment_replaced": plan["stable_environment_before"] is not None,
        "rollback_performed": False, "recovery_error": None,
        "error": None,
    }
    switched = env_written = service_written = cutover_started = False
    try:
        target, manifest, manifest_sha = materialize(
            data_root, version, plan["records"], plan["payload"],
        )
        receipt["manifest_sha256"] = manifest_sha
        atomic_write(data_root / "config/mediacenter.env", plan["stable_environment"], 0o600)
        env_written = True
        systemctl("stop")
        cutover_started = True
        atomic_write(service_file, render_service(service_template, data_root, control_python), 0o600)
        service_written = True
        switch_current(data_root, version)
        switched = True
        write_state(data_root, version, None, manifest_sha)
        systemctl("daemon-reload")
        systemctl("start")
        receipt["health"] = wait_health(
            health_url, Path(environment_values(Path(plan["legacy_environment"]))[1]["MEDIACENTER_API_KEY_FILE"]),
            version,
        )
        verify_installed_release(target, manifest)
        receipt["status"] = "succeeded"
    except Exception as exc:
        receipt["error"] = f"{type(exc).__name__}:{exc}"
        try:
            if cutover_started:
                systemctl("stop", allow_missing=True)
            current = data_root / "current"
            if switched and current.is_symlink():
                current.unlink()
            state_path = data_root / "state/release-state.json"
            if switched and state_path.is_file() and not state_path.is_symlink():
                state_path.unlink()
            if env_written:
                stable = data_root / "config/mediacenter.env"
                if plan["stable_environment_before"] is not None:
                    atomic_write(stable, (backup / "previous-mediacenter.env").read_bytes(), 0o600)
                elif stable.is_file() and not stable.is_symlink():
                    stable.unlink()
            if service_written:
                atomic_write(service_file, (backup / "mediacenter.service").read_bytes(), 0o600)
            if cutover_started:
                systemctl("daemon-reload")
                systemctl("start")
                wait_health(
                    health_url,
                    Path(environment_values(Path(plan["legacy_environment"]))[1]["MEDIACENTER_API_KEY_FILE"]),
                    version,
                )
                receipt["rollback_performed"] = True
        except Exception as recovery_exc:
            receipt["recovery_error"] = f"{type(recovery_exc).__name__}:{recovery_exc}"
        receipt["status"] = "failed"
        receipt["finished_at"] = utc_now()
        path = write_receipt(data_root, receipt)
        raise InstallError(f"legacy_adoption_failed receipt={path}") from exc
    receipt["finished_at"] = utc_now()
    path = write_receipt(data_root, receipt)
    return {"receipt": str(path), **receipt}


def public_plan(value: dict) -> dict:
    return {key: item for key, item in value.items()
            if key not in {"stable_environment", "records", "payload"}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "apply"))
    parser.add_argument("--data-root", type=Path, default=Path("/srv/mediacenter"))
    parser.add_argument("--legacy-release", type=Path, required=True)
    parser.add_argument("--legacy-environment", type=Path, required=True)
    parser.add_argument("--version", default="1.0.0")
    parser.add_argument("--service-file", type=Path,
                        default=Path.home() / ".config/systemd/user/mediacenter.service")
    parser.add_argument("--service-template", type=Path,
                        default=Path(__file__).resolve().parents[1] / "deploy/mediacenter.service")
    parser.add_argument("--environment-template", type=Path,
                        default=Path(__file__).resolve().parents[1]
                        / "deploy/mediacenter.env.example")
    parser.add_argument("--control-python", type=Path,
                        default=Path("/srv/mediacenter/control-runtime-v2/bin/python"))
    parser.add_argument("--health-url", default="http://127.0.0.1:8787")
    args = parser.parse_args()
    try:
        plan = inspect(args.action, args.data_root.resolve(), args.legacy_release,
                       args.legacy_environment, args.version, args.service_file.resolve(),
                       args.service_template.resolve(strict=True),
                       args.environment_template.resolve(strict=True), args.health_url)
        result = public_plan(plan) if args.action == "preflight" else apply_adoption(
            plan, args.data_root.resolve(), args.service_file.resolve(),
            args.service_template.resolve(strict=True), lexical_absolute(args.control_python),
            args.health_url,
        )
    except (OSError, subprocess.SubprocessError, InstallError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
