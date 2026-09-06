#!/usr/bin/env python3
"""Build a deterministic, secret-free MediaCenter Linux server bundle."""
from __future__ import annotations

import argparse
import ast
import gzip
import io
import json
import os
import tarfile
from pathlib import Path

from verify_release_bundle import (
    ReleaseValidationError,
    canonical,
    digest_bytes,
    inspect_server_bundle,
    scan_payload,
)


ROOT_FILES = ("README.md", "pyproject.toml")
DEPLOY_FILES = (
    "deploy/container-security.json",
    "deploy/mediacenter-redis.service",
    "deploy/mediacenter.env.example",
    "deploy/mediacenter.service",
    "deploy/model_catalog.json",
    "deploy/nginx-mediacenter-sse.conf",
    "deploy/redis-acl.template",
    "deploy/redis-runtime.json",
    "deploy/redis.conf",
    "deploy/release.schema.json",
)
SCRIPT_FILES = (
    "scripts/adopt_legacy_server.py",
    "scripts/bootstrap_server.sh",
    "scripts/convert_oci_to_docker_archive.py",
    "scripts/install_server_release.py",
    "scripts/migrate_task_kernel.py",
    "scripts/verify_release_bundle.py",
)


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ReleaseValidationError(code)


def project_version(root: Path) -> str:
    marker = 'version = "'
    for line in (root / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith(marker):
            return line[len(marker):].removesuffix('"')
    raise ReleaseValidationError("server_project_version_missing")


def verify_health_version(root: Path, version: str) -> None:
    tree = ast.parse((root / 'mediacenter/release_version.py').read_text(encoding='utf-8'))
    values = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == 'RELEASE_VERSION' for target in node.targets)
              and isinstance(node.value, ast.Constant)]
    require(values == [version], 'server_health_version_mismatch')


def _walk_files(root: Path, relative_root: str) -> list[str]:
    base = root / relative_root
    require(base.is_dir() and not base.is_symlink(), "server_source_root_missing_or_unsafe")
    result = []
    for path in sorted(base.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        require(path.is_file() and not path.is_symlink(), "server_source_file_unsafe")
        result.append(relative)
    return result


def gather_payload(root: Path) -> list[dict]:
    candidates = [*ROOT_FILES, *DEPLOY_FILES, *SCRIPT_FILES]
    candidates += _walk_files(root, "mediacenter")
    candidates += _walk_files(root, "containers")
    candidates += _walk_files(root, "deploy/production-images")
    candidates = sorted(set(candidates))
    records = []
    for relative in candidates:
        path = root / relative
        require(path.is_file() and not path.is_symlink(), "server_source_file_missing_or_unsafe")
        try:
            path.resolve(strict=True).relative_to(root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise ReleaseValidationError("server_source_file_outside_root") from exc
        content = path.read_bytes()
        scan_payload(relative, content)
        mode = 0o755 if relative.startswith("scripts/") else 0o644
        records.append({
            "path": relative,
            "bytes": len(content),
            "sha256": digest_bytes(content),
            "mode": mode,
        })
    return records


def _tar_info(name: str, content: bytes, mode: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mode = mode
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.pax_headers = {}
    return info


def build(root: Path, output: Path, version: str, *, force: bool = False) -> dict:
    root = root.resolve(strict=True)
    require(version == project_version(root), "server_release_version_mismatch")
    verify_health_version(root, version)
    output = output.resolve()
    require(output.parent.is_dir(), "server_release_output_parent_missing")
    try:
        output.relative_to(root)
    except ValueError as exc:
        raise ReleaseValidationError("server_release_output_outside_root") from exc
    require(force or not output.exists(), "server_release_output_exists")
    require(output.name == f"MediaCenter-server-{version}.tar.gz", "server_release_output_name_invalid")
    records = gather_payload(root)
    bundle_root = f"MediaCenter-server-{version}"
    manifest = {
        "schema": "mc.server-bundle/1",
        "product": "MediaCenter",
        "version": version,
        "root": bundle_root,
        "files": records,
        "security": {
            "credentials_embedded": False,
            "model_weights_embedded": False,
            "database_embedded": False,
            "media_embedded": False,
        },
    }
    manifest_bytes = canonical(manifest)
    temporary = output.with_name(output.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as zipped:
                with tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as archive:
                    manifest_name = f"{bundle_root}/release/server-manifest.json"
                    archive.addfile(_tar_info(manifest_name, manifest_bytes, 0o644), io.BytesIO(manifest_bytes))
                    for item in records:
                        content = (root / item["path"]).read_bytes()
                        archive.addfile(
                            _tar_info(f"{bundle_root}/{item['path']}", content, item["mode"]),
                            io.BytesIO(content),
                        )
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    inspected = inspect_server_bundle(output)
    return {
        "schema": "mc.server-bundle-build/1",
        "version": version,
        "path": output.relative_to(root).as_posix(),
        "bytes": output.stat().st_size,
        "sha256": digest_bytes(output.read_bytes()),
        "payload_manifest_sha256": inspected["manifest_sha256"],
        "files": inspected["files"],
        "status": "passed",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    output = args.output or args.root / "dist" / f"MediaCenter-server-{args.version}.tar.gz"
    try:
        result = build(args.root, output, args.version, force=args.force)
    except (OSError, ReleaseValidationError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
