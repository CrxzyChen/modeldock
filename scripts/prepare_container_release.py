#!/usr/bin/env python3
"""Read-only MC-044 release preflight.

This command never connects to a server, builds an image, migrates a database,
or changes a service. Production execution requires a separately frozen copy of
this plan with every resource field populated and explicitly authorized.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


EXPECTED_MODELS = {
    "sdxl-base-1.0", "illustrious-xl-v2.0", "z-image-turbo", "qwen-image-2512",
    "realesrgan-x2plus", "realesrgan-x4plus", "realesrgan-x4plus-anime-6b",
    "wan2.1-t2v-1.3b", "minimax-h3-ref2va", "ltx-2.3-distilled",
    "wan2.2-i2v-a14b", "hunyuanvideo-1.5-720p-t2v", "musicgen-small",
    "cosyvoice2-0.5b",
}
RESOURCE_FIELDS = {
    "authorization_id", "server_id", "maintenance_window", "database_identity",
    "backup_target", "rollback_deadline", "maximum_download_bytes",
    "maximum_duration_seconds",
}


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def preflight(root: Path, plan_path: Path, manifest_path: Path | None) -> dict:
    root = root.resolve(strict=True)
    plan_path = plan_path.resolve(strict=True)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("schema") != "mc.container-release/1":
        raise ValueError("release_plan_schema_invalid")
    models = plan.get("models")
    keys = [item.get("model_key") for item in models] if isinstance(models, list) else []
    if len(keys) != 14 or len(set(keys)) != 14 or set(keys) != EXPECTED_MODELS:
        raise ValueError("release_model_matrix_invalid")

    releases = []
    for item in models:
        path = (root / item["release"]).resolve(strict=True)
        if root not in path.parents:
            raise ValueError("release_path_outside_root")
        data = json.loads(path.read_text(encoding="utf-8"))
        releases.append({"model_key": item["model_key"], "path": item["release"],
                         "sha256": sha256(path), "status": data.get("status"),
                         "image_digest": data.get("image_digest")})

    manifest = None
    if manifest_path is not None:
        manifest_path = manifest_path.resolve(strict=True)
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = {"path": str(manifest_path), "sha256": sha256(manifest_path),
                    "declared_digest": manifest_data.get("manifest_digest")}

    authorization = plan.get("production_authorization", {})
    missing = sorted(key for key in RESOURCE_FIELDS if authorization.get(key) in (None, ""))
    if not authorization.get("gpu_uuids"):
        missing.append("gpu_uuids")
    candidate = plan.get("candidate", {})
    if candidate.get("sha256") in (None, ""):
        missing.append("candidate.sha256")
    unbuilt = sorted({row["path"] for row in releases if not row["image_digest"]})
    return {
        "schema": "mc.container-release-preflight/1",
        "release_id": plan["release_id"],
        "verify_only": True,
        "models": len(releases),
        "releases": releases,
        "candidate_manifest": manifest,
        "production_ready": not missing and not unbuilt,
        "missing_resource_inputs": missing,
        "unbuilt_release_metadata": unbuilt,
        "mutations_performed": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only MediaCenter container release preflight")
    parser.add_argument("--verify-only", action="store_true", required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    plan = args.plan or args.root / "deploy/container-release-plan.json"
    result = preflight(args.root, plan, args.manifest)
    body = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body, encoding="utf-8")
    print(body, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
