#!/usr/bin/env python3
"""Verify the complete MC-044 OCI assembly without importing or extracting it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

from mediacenter.runtime_artifacts import verify_oci_archive
from mediacenter.container_releases import RuntimeRelease


def image_contract(image):
    return {"reference": "mediacenter.local/" + image["id"] + "@" + image["manifest_digest"],
            "image_id": image["config_digest"],
            "platform": "linux/amd64", "entrypoint": image["entrypoint"],
            "command": image["command"], "environment": image["environment"]}
from mediacenter.worker_common import canonical


def exclusive(path: Path, value):
    raw = canonical(value).encode() + b"\n"
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def inside(root: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("production_oci_path_invalid")
    path = path.resolve(strict=True)
    if root not in path.parents:
        raise ValueError("production_oci_path_outside_root")
    return path


def verify(root: Path, matrix_path: Path):
    root = root.absolute().resolve(strict=True)
    assembly = json.loads((root / "assembly-evidence.json").read_bytes())
    matrix = json.loads(matrix_path.read_bytes())
    if (assembly.get("schema") != "mc.oci-assembly-evidence/1"
            or assembly.get("status") != "complete"
            or len(assembly.get("images", ())) != 11
            or len(assembly.get("releases", ())) != 14
            or matrix.get("schema") != "mc.production-image-build-matrix/1"):
        raise ValueError("production_oci_evidence_invalid")
    models = set(matrix["model_bindings"])
    if {row.get("model_key") for row in assembly["releases"]} != models:
        raise ValueError("production_oci_model_matrix_mismatch")
    images = {row["archive_sha256"]: row for row in assembly["images"]}
    if len(images) != 11:
        raise ValueError("production_oci_archive_identity_ambiguous")
    verified, releases = {}, []
    for image in assembly["images"]:
        archive = inside(root, image["archive"])
        artifact = {"format": "oci-layout-tar", "url": "https://unpublished.invalid/" + archive.name,
                    "sha256": image["archive_sha256"], "byte_size": image["archive_bytes"]}
        archive_contract = image_contract(image)
        release_view = SimpleNamespace(data={"artifact": artifact, "image": archive_contract},
                                       image_digest=image["manifest_digest"])
        with archive.open("rb") as stream:
            result = verify_oci_archive(stream, release_view)
        normalized = {"artifact": {key: artifact[key] for key in ("format", "sha256", "byte_size")},
                      "image": archive_contract}
        verified[artifact["sha256"]] = {"contract": normalized,
                                        "result": result, "path": str(archive)}
    for row in sorted(assembly["releases"], key=lambda item: item["model_key"]):
        path = inside(root, row["path"])
        raw = path.read_bytes()
        release = RuntimeRelease(json.loads(raw))
        if (release.digest != row["release_digest"]
                or release.image_digest != row["image_digest"]):
            raise ValueError("production_oci_release_changed")
        artifact = release.data["artifact"]
        image = images.get(artifact["sha256"])
        if image is None or image["archive_bytes"] != artifact["byte_size"]:
            raise ValueError("production_oci_archive_unbound")
        archive = inside(root, image["archive"])
        previous = verified.get(artifact["sha256"])
        contract = {"artifact": {key: artifact[key] for key in ("format", "sha256", "byte_size")},
                    "image": release.data["image"]}
        if previous is None or previous["contract"] != contract:
            raise ValueError("production_oci_shared_archive_contract_mismatch")
        releases.append({"model_key": row["model_key"], "release_digest": release.digest,
                         "image_digest": release.image_digest,
                         "archive_sha256": artifact["sha256"]})
    return {"schema": "mc.production-oci-verification/1", "status": "passed",
            "assembly_sha256": hashlib.sha256((root / "assembly-evidence.json").read_bytes()).hexdigest(),
            "image_count": len(verified), "release_count": len(releases),
            "archives": [{"sha256": key, **value["result"], "path": value["path"]}
                         for key, value in sorted(verified.items())],
            "releases": releases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.root, args.matrix)
    exclusive(args.evidence, result)
    print(canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
