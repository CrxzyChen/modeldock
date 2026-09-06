#!/usr/bin/env python3
"""Execute the frozen MC-044 OCI build matrix on the authorized Linux host.

The command does not pull model weights, delete build state, prune the Engine,
or retry an unknown build.  Every archive is exclusive and retained on error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time


BASE = re.compile(r"ubuntu@sha256:[0-9a-f]{64}")
ARG = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
VALUE = re.compile(r"[A-Za-z0-9_.:,/-]{1,1024}")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def inside(root: Path, relative: str, *, existing: bool) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("build_path_invalid")
    root = root.resolve(strict=True)
    path = root.joinpath(relative)
    resolved = path.resolve(strict=existing)
    if root != resolved and root not in resolved.parents:
        raise ValueError("build_path_outside_root")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("build_symlink_rejected")
    return resolved


def load_matrix(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (value.get("schema") != "mc.production-image-build-matrix/1"
            or value.get("platform") != "linux/amd64"
            or value.get("base_repository") != "ubuntu"
            or value.get("base_tag") != "24.04"):
        raise ValueError("build_matrix_invalid")
    images = value.get("images")
    if not isinstance(images, list) or len(images) != 11:
        raise ValueError("build_matrix_images_invalid")
    ids = [item.get("id") for item in images]
    archives = [item.get("archive") for item in images]
    if len(set(ids)) != len(ids) or len(set(archives)) != len(archives):
        raise ValueError("build_matrix_duplicate")
    if set(value.get("model_bindings", {})) != {
        "sdxl-base-1.0", "illustrious-xl-v2.0", "z-image-turbo",
        "qwen-image-2512", "realesrgan-x2plus", "realesrgan-x4plus",
        "realesrgan-x4plus-anime-6b", "wan2.1-t2v-1.3b",
        "minimax-h3-ref2va", "ltx-2.3-distilled", "wan2.2-i2v-a14b",
        "hunyuanvideo-1.5-720p-t2v", "musicgen-small", "cosyvoice2-0.5b",
    }:
        raise ValueError("build_model_bindings_invalid")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--contexts-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--maximum-seconds", type=int, default=43200)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    if not BASE.fullmatch(args.base_image):
        raise ValueError("base_image_not_digest_pinned")
    if not 60 <= args.maximum_seconds <= 43200:
        raise ValueError("build_duration_invalid")

    matrix = load_matrix(args.matrix.resolve(strict=True))
    contexts = args.contexts_root.resolve(strict=True)
    output = args.output_root.resolve(strict=True)
    evidence_path = args.evidence.resolve(strict=False)
    if evidence_path.exists() or evidence_path.parent.resolve(strict=True) != output:
        raise ValueError("build_evidence_not_exclusive")

    prepared = []
    for item in matrix["images"]:
        context = inside(contexts, item["context"], existing=True)
        dockerfile = inside(context, item["dockerfile"], existing=True)
        archive = inside(output, item["archive"], existing=False)
        if archive.exists():
            raise ValueError("build_archive_not_exclusive")
        build_args = item.get("build_args", {})
        if (not isinstance(build_args, dict)
                or any(not ARG.fullmatch(key) or not isinstance(value, str)
                       or not VALUE.fullmatch(value)
                       for key, value in build_args.items())):
            raise ValueError("build_args_invalid")
        prepared.append((item, context, dockerfile, archive, build_args))

    started = time.monotonic()
    report = {"schema": "mc.production-image-build-evidence/1",
              "base_image": args.base_image, "matrix_sha256": sha256(args.matrix),
              "platform": matrix["platform"], "status": "running", "images": []}
    evidence_path.write_text(canonical(report) + "\n", encoding="utf-8")
    os.chmod(evidence_path, 0o600)

    for item, context, dockerfile, archive, build_args in prepared:
        remaining = args.maximum_seconds - int(time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("build_matrix_timeout")
        metadata = output / (item["id"] + ".metadata.json")
        log = output / (item["id"] + ".build.log")
        if metadata.exists() or log.exists():
            raise ValueError("build_sidecar_not_exclusive")
        command = ["docker", "buildx", "build", "--progress=plain",
                   "--platform", matrix["platform"], "--network=none",
                   "--provenance=false", "--sbom=false",
                   "--build-arg", "BASE_IMAGE=" + args.base_image]
        for key in sorted(build_args):
            command.extend(("--build-arg", key + "=" + build_args[key]))
        command.extend(("--metadata-file", str(metadata), "--output",
                        "type=oci,dest=" + str(archive), "-f", str(dockerfile),
                        str(context)))
        row = {"id": item["id"], "group": item["group"],
               "context": str(context), "dockerfile_sha256": sha256(dockerfile),
               "archive": str(archive), "command": command,
               "started_unix_ns": time.time_ns(), "status": "running"}
        report["images"].append(row)
        evidence_path.write_text(canonical(report) + "\n", encoding="utf-8")
        with log.open("xb") as stream:
            try:
                completed = subprocess.run(command, stdin=subprocess.DEVNULL,
                                           stdout=stream, stderr=subprocess.STDOUT,
                                           timeout=remaining, check=False)
            except subprocess.TimeoutExpired:
                row.update(status="outcome_unknown", error="build_timeout")
                evidence_path.write_text(canonical(report) + "\n", encoding="utf-8")
                raise
        row["exit_code"] = completed.returncode
        row["finished_unix_ns"] = time.time_ns()
        row["log_sha256"] = sha256(log)
        if completed.returncode != 0 or not archive.is_file():
            row.update(status="failed", error="build_failed")
            evidence_path.write_text(canonical(report) + "\n", encoding="utf-8")
            raise RuntimeError("build_failed:" + item["id"])
        row.update(status="built", archive_bytes=archive.stat().st_size,
                   archive_sha256=sha256(archive), metadata_sha256=sha256(metadata))
        evidence_path.write_text(canonical(report) + "\n", encoding="utf-8")

    report.update(status="complete", finished_unix_ns=time.time_ns(),
                  elapsed_seconds=round(time.monotonic() - started, 3),
                  model_bindings=matrix["model_bindings"])
    evidence_path.write_text(canonical(report) + "\n", encoding="utf-8")
    print(canonical(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
