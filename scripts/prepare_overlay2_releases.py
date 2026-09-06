#!/usr/bin/env python3
"""Derive overlay2 runtime declarations from verified OCI assembly evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mediacenter.container_releases import RuntimeRelease


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def write_new(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    except BaseException:
        if path.exists():
            path.unlink()
        raise


def resolve_image(image_contract, images):
    images_by_config = {row["config_digest"]: row for row in images}
    images_by_manifest = {row["manifest_digest"]: row for row in images}
    image = (images_by_config.get(image_contract["image_id"])
             or images_by_manifest.get(image_contract["image_id"]))
    if image is None:
        raise ValueError("release/assembly image mismatch")
    valid_references = {
        image["manifest_digest"],
        "mediacenter.local/" + image["id"] + "@" + image["manifest_digest"],
    }
    if image_contract["reference"] not in valid_references:
        raise ValueError("release/assembly image mismatch")
    return image


def prepare(assembly_path, output_root):
    assembly_path, output_root = Path(assembly_path).resolve(), Path(output_root).resolve()
    assembly = json.loads(assembly_path.read_text(encoding="utf-8"))
    if assembly.get("status") != "complete" or len(assembly.get("releases", [])) != 14:
        raise ValueError("verified 14-release assembly required")
    result = []
    for row in sorted(assembly["releases"], key=lambda value: value["model_key"]):
        source = Path(row["path"]).resolve()
        declaration = json.loads(source.read_text(encoding="utf-8"))
        image = resolve_image(declaration["image"], assembly["images"])
        declaration["image"]["reference"] = ("mediacenter.local/" + image["id"] + "@"
                                                   + image["manifest_digest"])
        declaration["image"]["image_id"] = image["config_digest"]
        release = RuntimeRelease(declaration)
        target = output_root / (row["model_key"] + ".json")
        raw = (canonical(declaration) + "\n").encode()
        write_new(target, raw)
        result.append({"model_key": row["model_key"], "path": str(target),
                       "release_digest": release.digest,
                       "manifest_digest": image["manifest_digest"],
                       "image_id": image["config_digest"],
                       "sha256": hashlib.sha256(raw).hexdigest()})
    return {"schema": "mc.overlay2-releases/1", "status": "passed",
            "assembly": str(assembly_path), "release_count": len(result), "releases": result}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assembly", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args()
    result = prepare(args.assembly, args.output_root)
    raw = (canonical(result) + "\n").encode()
    write_new(Path(args.evidence).resolve(), raw)
    print(raw.decode(), end="")


if __name__ == "__main__":
    main()
