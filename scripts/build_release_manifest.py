#!/usr/bin/env python3
"""Build the deterministic cross-platform MediaCenter mc.release/1 manifest."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from verify_release_bundle import (
    ReleaseValidationError,
    canonical,
    digest_bytes,
    digest_file,
    inspect_server_bundle,
    safe_repository_file,
    validate_release_manifest,
)


DESKTOP_ROOTS = ("electron", "client/src", "client/dist")
DESKTOP_FILES = ("package.json", "package-lock.json", "pyproject.toml")
WINDOWS_PACKAGE_SCOPE = ["electron/**/*", "client/dist/**/*", "package.json"]


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ReleaseValidationError(code)


def _version_from_pyproject(root: Path) -> str:
    prefix = 'version = "'
    for line in (root / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].removesuffix('"')
    raise ReleaseValidationError("release_python_version_missing")


def _tool_version(command: list[str], fallback: str) -> str:
    try:
        result = subprocess.run(command, check=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return fallback
    return result.stdout.strip() or result.stderr.strip() or fallback


def _desktop_source_paths(root: Path) -> list[str]:
    paths = list(DESKTOP_FILES)
    for relative_root in DESKTOP_ROOTS:
        base = root / relative_root
        require(base.is_dir() and not base.is_symlink(), "release_desktop_source_root_missing")
        for path in sorted(base.rglob("*")):
            if path.is_dir() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo", ".map"}:
                continue
            require(path.is_file() and not path.is_symlink(), "release_desktop_source_unsafe")
            paths.append(path.relative_to(root).as_posix())
    return sorted(set(paths))


def _file_record(root: Path, relative: str) -> dict:
    path = safe_repository_file(root, relative)
    size, sha = digest_file(path)
    return {"path": relative, "bytes": size, "sha256": sha}


def build(root: Path, version: str, windows: Path, linux: Path, output: Path,
          created_at: str, *, force: bool = False) -> dict:
    root = root.resolve(strict=True)
    require(datetime.fromisoformat(created_at.replace("Z", "+00:00")).tzinfo is not None,
            "release_created_at_timezone_required")
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    require(package.get("version") == version == _version_from_pyproject(root),
            "release_product_version_mismatch")
    require(package.get("build", {}).get("files") == WINDOWS_PACKAGE_SCOPE,
            "release_windows_package_scope_invalid")
    require(package.get("build", {}).get("win", {}).get("target") == ["nsis"],
            "release_windows_target_invalid")
    windows = windows.resolve(strict=True)
    linux = linux.resolve(strict=True)
    output = output.resolve()
    for artifact in (windows, linux):
        try:
            artifact.relative_to(root)
        except ValueError as exc:
            raise ReleaseValidationError("release_artifact_outside_root") from exc
        require(artifact.is_file() and not artifact.is_symlink(), "release_artifact_missing_or_unsafe")
    require(windows.name == f"MediaCenter-Setup-{version}.exe", "release_windows_name_invalid")
    require(linux.name == f"MediaCenter-server-{version}.tar.gz", "release_linux_name_invalid")
    require(output.parent.is_dir(), "release_manifest_output_parent_missing")
    try:
        output.relative_to(root)
    except ValueError as exc:
        raise ReleaseValidationError("release_manifest_output_outside_root") from exc
    require(output.name == f"MediaCenter-{version}-release.json", "release_manifest_output_name_invalid")
    sidecar = output.with_name(output.name + ".sha256")
    require(force or (not output.exists() and not sidecar.exists()), "release_manifest_output_exists")

    server = inspect_server_bundle(linux)
    require(server["manifest"]["version"] == version, "release_server_version_mismatch")
    source_paths = set(_desktop_source_paths(root))
    source_paths.update(item["path"] for item in server["manifest"]["files"])
    source_files = [_file_record(root, relative) for relative in sorted(source_paths)]
    source_digest = digest_bytes(canonical(source_files))
    windows_size, windows_sha = digest_file(windows)
    linux_size, linux_sha = digest_file(linux)
    dependencies = package["devDependencies"]
    manifest = {
        "schema": "mc.release/1",
        "product": "MediaCenter",
        "version": version,
        "release_id": f"mediacenter-{version}",
        "created_at": created_at,
        "source_snapshot": {
            "kind": "filesystem_sha256",
            "git_repository": (root / ".git").is_dir(),
            "digest": source_digest,
            "files": source_files,
        },
        "toolchain": {
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "node": _tool_version(["node", "--version"], "unavailable"),
            "electron": dependencies["electron"],
            "electron_builder": dependencies["electron-builder"],
        },
        "artifacts": [
            {
                "id": "windows-installer",
                "platform": "windows-x64",
                "kind": "nsis",
                "path": windows.relative_to(root).as_posix(),
                "bytes": windows_size,
                "sha256": windows_sha,
                "signed": False,
                "payload_manifest_sha256": None,
            },
            {
                "id": "linux-server-bundle",
                "platform": "linux-x86_64",
                "kind": "tar.gz",
                "path": linux.relative_to(root).as_posix(),
                "bytes": linux_size,
                "sha256": linux_sha,
                "signed": False,
                "payload_manifest_sha256": server["manifest_sha256"],
            },
        ],
        "security": {
            "credentials_embedded": False,
            "model_weights_embedded": False,
            "database_embedded": False,
            "media_embedded": False,
            "windows_packaged_paths": WINDOWS_PACKAGE_SCOPE,
        },
        "verification": {
            "command": ["python", "scripts/verify_release_bundle.py", "--manifest",
                        f"dist/MediaCenter-{version}-release.json"],
            "manifest_sha256_file": f"dist/MediaCenter-{version}-release.json.sha256",
            "status": "self_verifiable",
        },
    }
    validate_release_manifest(manifest)
    body = canonical(manifest)
    temporary = output.with_name(output.name + ".tmp")
    temporary_sidecar = sidecar.with_name(sidecar.name + ".tmp")
    for path in (temporary, temporary_sidecar):
        if path.exists():
            path.unlink()
    try:
        temporary.write_bytes(body)
        temporary_sidecar.write_text(digest_bytes(body) + "\n", encoding="ascii")
        os.replace(temporary, output)
        os.replace(temporary_sidecar, sidecar)
    finally:
        for path in (temporary, temporary_sidecar):
            if path.exists():
                path.unlink()
    return {
        "schema": "mc.release-manifest-build/1",
        "release_id": manifest["release_id"],
        "path": output.relative_to(root).as_posix(),
        "sha256": digest_bytes(body),
        "source_files": len(source_files),
        "artifacts": 2,
        "status": "passed",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--version", required=True)
    parser.add_argument("--windows", type=Path, required=True)
    parser.add_argument("--linux", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--created-at", default=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    output = args.output or args.root / "dist" / f"MediaCenter-{args.version}-release.json"
    try:
        result = build(
            args.root,
            args.version,
            args.windows if args.windows.is_absolute() else args.root / args.windows,
            args.linux if args.linux.is_absolute() else args.root / args.linux,
            output,
            args.created_at,
            force=args.force,
        )
    except (OSError, json.JSONDecodeError, ReleaseValidationError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
