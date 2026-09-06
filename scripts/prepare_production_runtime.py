#!/usr/bin/env python3
"""Create the exact private runtime.json for one frozen MC-044 server.

The script is deliberately offline and read-only with respect to the source
database, OCI archives and release declarations.  It writes one new JSON file;
all runtime directories and secret files must already exist so an operator can
review the complete boundary before the Server is started.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mediacenter.runtime_provisioning import RuntimeTemplate
from mediacenter.container_releases import RuntimeRelease
from mediacenter.task_state import canonical, digest


def _existing(path: Path, kind: str) -> Path:
    path = path.resolve()
    if kind == "file" and not path.is_file():
        raise ValueError(f"required file is missing: {path}")
    if kind == "directory" and not path.is_dir():
        raise ValueError(f"required directory is missing: {path}")
    if any(value.is_symlink() for value in (path, *path.parents)):
        raise ValueError(f"symlinked production boundary: {path}")
    return path


def _load(path: Path) -> dict:
    return json.loads(_existing(path, "file").read_text(encoding="utf-8"))


def _limits(kind: str, required_vram_mib: int) -> dict:
    # Host RAM is a hard cgroup ceiling, not an estimate of GPU residency.
    gib = 1024 ** 3
    if required_vram_mib >= 40960:
        ram, cpus, pids, tmp = 120 * gib, 24, 2048, 8 * gib
    elif kind == "video" or required_vram_mib >= 32768:
        ram, cpus, pids, tmp = 96 * gib, 20, 1536, 8 * gib
    elif required_vram_mib >= 16384:
        ram, cpus, pids, tmp = 64 * gib, 16, 1024, 4 * gib
    else:
        ram, cpus, pids, tmp = 32 * gib, 12, 768, 2 * gib
    return {"uid": 1000, "gid": 1000, "memory_bytes": ram,
            "nano_cpus": cpus * 10 ** 9, "pids_limit": pids,
            "tmpfs_bytes": tmp}


def _resources(required_vram_mib: int, sharing_mode: str,
               external_reserve_mib: int, gpu_memory_mib: int) -> dict:
    task = 1024 if required_vram_mib <= 8192 else 2048 if required_vram_mib <= 20480 else 4096
    if required_vram_mib <= task:
        raise ValueError("model VRAM declaration cannot be split safely")
    allowed = (2048, 4096, 8192, 12288, 16384, 24576, 32768)
    headroom = gpu_memory_mib - 2048 - required_vram_mib
    compatible = [value for value in allowed if value <= external_reserve_mib and value <= headroom]
    if not compatible:
        raise ValueError("model VRAM budget cannot fit the target GPU")
    external_reserve_mib = max(compatible)
    return {"base_mib": required_vram_mib - task, "task_mib": task,
            "external_reserve_mib": external_reserve_mib,
            "sharing_mode": sharing_mode, "residency": "on_demand",
            "idle_seconds": 300}


def prepare(args) -> dict:
    if (not args.gpu_uuids or len(set(args.gpu_uuids)) != len(args.gpu_uuids)
            or any(not re.fullmatch(r'GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', value)
                   for value in args.gpu_uuids)):
        raise ValueError('explicit unique GPU UUIDs are required')
    catalog = _load(Path(args.catalog))
    assembly = _load(Path(args.assembly))
    overlay = _load(Path(args.overlay_evidence))
    if (assembly.get("status") != "complete" or overlay.get("status") != "passed"
            or len(catalog.get("models", [])) != 14
            or len(overlay.get("releases", [])) != 14):
        raise ValueError("the frozen 14-model release set is incomplete")
    entries = {row["catalog_key"]: row for row in catalog["models"]}
    if len(entries) != 14:
        raise ValueError("catalog keys are not unique")
    images = {row["manifest_digest"]: row for row in assembly["images"]}
    releases, templates, approved, local = [], {}, [], {}
    for row in sorted(overlay["releases"], key=lambda value: value["model_key"]):
        declaration = _load(Path(row["path"]))
        release = RuntimeRelease(declaration)
        model_key = row["model_key"]
        entry = entries.get(model_key)
        image = images.get(row["manifest_digest"])
        if (entry is None or release.digest != row["release_digest"]
                or release.data["image"]["image_id"] != row["image_id"]
                or image is None or image["config_digest"] != row["image_id"]):
            raise ValueError(f"release identity mismatch: {model_key}")
        archive = _existing(Path(image["archive"]), "file")
        releases.append(declaration)
        approved.append(release.digest)
        local[release.digest] = str(archive)

    database = _existing(Path(args.database), "file")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = {row["catalog_key"]: dict(row) for row in connection.execute(
            "SELECT catalog_key,kind,required_vram_mib,gpu_sharing_mode,"
            "external_reserve_mib FROM model_deployments")}
    finally:
        connection.close()
    if set(rows) != set(entries):
        raise ValueError("database deployment/catalog set differs from the release set")
    for key, entry in entries.items():
        row = rows[key]
        template = {"schema": 1, "recipe_key": key,
                    "recipe_digest": digest(entry),
                    "release_digest": next(item.digest for item in map(RuntimeRelease, releases)
                                             if item.data["release_id"].endswith(key)),
                    "limits": _limits(row["kind"], row["required_vram_mib"]),
                    "resources": _resources(row["required_vram_mib"],
                                              row["gpu_sharing_mode"],
                                              row["external_reserve_mib"], args.gpu_memory_mib)}
        RuntimeTemplate(template)
        templates[key] = template

    runtime_root = _existing(Path(args.runtime_root), "directory")
    redis_root = _existing(Path(args.redis_root), "directory")
    oci_root = _existing(Path(args.oci_root), "directory")
    value = {
        "server_id": args.server_id,
        "gpu_uuids": args.gpu_uuids,
        "engine": {"socket_path": "/run/docker.sock", "engine_id": args.engine_id,
                   "cgroup_mount": "/sys/fs/cgroup", "delegated_subtree": "/sys/fs/cgroup",
                   "platform": "linux/amd64", "api_version": "1.51",
                   "server_version": "28.3.2", "cgroup_driver": "systemd",
                   "image_store": "overlay2", "total_timeout": 10.0,
                   "io_timeout": 2.0, "import_timeout": 3600.0,
                   "import_io_timeout": 900.0, "response_limit": 1048576,
                   "stop_seconds": 3},
        "installation": {
            "releases": releases,
            "approved_release_digests": sorted(approved),
            "templates": templates,
            "image_store": str(_existing(runtime_root / "images", "directory")),
            "download_hosts": [],
            "local_artifact_roots": [str(oci_root)],
            "local_artifacts": local,
            "publisher": {
                "endpoint": {"username": "private-publisher",
                             "secret_file": str(_existing(redis_root / "secrets" / "server-manager.secret", "file")),
                             "unix_socket": str(redis_root / "run" / "redis.sock")},
                "seed_file": str(_existing(redis_root / "secrets" / "server-seed.secret", "file")),
                "state_root": str(_existing(runtime_root / "publisher", "directory"))},
            "package_root": str(_existing(runtime_root / "packages", "directory")),
            "lora": {"root": str(_existing(runtime_root / "lora-authority", "directory"))}}}
    # Canonical reparse proves the output contains JSON primitives only.
    return json.loads(canonical(value))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--assembly", required=True)
    parser.add_argument("--overlay-evidence", required=True)
    parser.add_argument("--oci-root", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--redis-root", required=True)
    parser.add_argument("--engine-id", required=True)
    parser.add_argument("--server-id", required=True)
    parser.add_argument("--gpu-memory-mib", required=True, type=int)
    parser.add_argument("--gpu-uuids", required=True, nargs='+',
                        help="Explicit GPU UUIDs from this host's nvidia-smi")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.exists() or not output.parent.is_dir():
        raise ValueError("runtime configuration output must be new")
    value = prepare(args)
    raw = (canonical(value) + "\n").encode()
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    print(canonical({"output": str(output), "release_count": len(value["installation"]["releases"]),
                     "template_count": len(value["installation"]["templates"]),
                     "gpu_uuids": value["gpu_uuids"]}))


if __name__ == "__main__":
    main()
