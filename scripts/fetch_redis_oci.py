#!/usr/bin/env python3
"""Fetch the frozen Redis 7.2.16 amd64 image as a verified OCI archive."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tarfile
from urllib.parse import urlencode

from assemble_oci_images import Registry, canonical, exclusive, safe_root, sha_file


MANIFEST = "sha256:e17e3a1993da428251cbd88dbdb3de8c8d4007f840d7350eb17a2d8695fa705f"
CONFIG = "sha256:82d6cb5ce52178d45aaa066f657b784548355c5a1377445221d451a763956490"
MEDIA = {"application/vnd.oci.image.layer.v1.tar+gzip",
         "application/vnd.docker.image.rootfs.diff.tar.gzip"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--maximum-download-bytes", type=int, default=128 * 1024**2)
    args = parser.parse_args()
    output = safe_root(args.output, existing=False)
    if output.exists():
        raise ValueError("redis_oci_output_not_exclusive")
    output.mkdir(mode=0o700)
    registry = Registry(output / "registry", args.maximum_download_bytes)
    token, _ = registry._json("https://auth.docker.io/token?" + urlencode(
        {"service": "registry.docker.io", "scope": "repository:library/redis:pull"}))
    accept = ", ".join(("application/vnd.oci.image.manifest.v1+json",
                        "application/vnd.docker.distribution.manifest.v2+json"))
    manifest, declared = registry._json(
        "https://registry-1.docker.io/v2/library/redis/manifests/" + MANIFEST,
        token=token["token"], accept=accept)
    if declared != MANIFEST or manifest.get("schemaVersion") != 2 \
            or manifest.get("config", {}).get("digest") != CONFIG:
        raise ValueError("redis_manifest_identity_changed")
    layers = manifest.get("layers")
    if type(layers) is not list or len(layers) != 7:
        raise ValueError("redis_manifest_layers_changed")
    descriptors = [manifest["config"], *layers]
    for row in descriptors:
        if (type(row) is not dict or not isinstance(row.get("size"), int)
                or row.get("size") <= 0 or not row.get("digest", "").startswith("sha256:")):
            raise ValueError("redis_descriptor_invalid")
        path = registry.blob(token["token"], row["digest"], "redis")
        actual, size = sha_file(path)
        if actual != row["digest"].split(":", 1)[1] or size != row["size"]:
            raise ValueError("redis_blob_changed")
    if any(row.get("mediaType") not in MEDIA for row in layers):
        raise ValueError("redis_layer_media_changed")
    config = json.loads((output / "registry" / CONFIG.split(":", 1)[1]).read_bytes())
    if config.get("architecture") != "amd64" or config.get("os") != "linux":
        raise ValueError("redis_platform_changed")
    layout = output / "layout"; blobs = layout / "blobs/sha256"
    blobs.mkdir(parents=True)
    for row in descriptors:
        os.link(output / "registry" / row["digest"].split(":", 1)[1],
                blobs / row["digest"].split(":", 1)[1])
    raw = canonical(manifest)
    local_manifest = "sha256:" + hashlib.sha256(raw).hexdigest()
    exclusive(blobs / local_manifest.split(":", 1)[1], raw)
    index = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
             "manifests": [{"mediaType": manifest["mediaType"], "digest": local_manifest,
                             "size": len(raw), "platform": {"architecture": "amd64", "os": "linux"}}]}
    exclusive(layout / "oci-layout", canonical({"imageLayoutVersion": "1.0.0"}))
    exclusive(layout / "index.json", canonical(index))
    archive = output / "redis-7.2.16.oci.tar"
    with tarfile.open(archive, "x:") as stream:
        for name in ("oci-layout", "index.json"):
            stream.add(layout / name, arcname=name, recursive=False)
        stream.add(layout / "blobs", arcname="blobs", recursive=True)
    archive_sha, archive_size = sha_file(archive)
    evidence = {"schema": "mc.redis-oci-evidence/1", "source_manifest": MANIFEST,
                "manifest_digest": local_manifest, "config_digest": CONFIG,
                "archive": str(archive), "archive_sha256": archive_sha,
                "archive_bytes": archive_size, "downloaded_bytes": registry.downloaded}
    exclusive(output / "evidence.json", canonical(evidence) + b"\n")
    print(canonical(evidence).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
