#!/usr/bin/env python3
"""Import the frozen model release matrix into one pinned overlay2 engine."""
import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mediacenter.config import EngineConfig
from mediacenter.container_runtime import UnixEngine
from mediacenter.runtime_artifacts import UnixRuntimeImporter, verify_oci_archive
from mediacenter.container_releases import RuntimeRelease


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def checkpoint(path, value):
    raw = (canonical(value) + "\n").encode()
    temporary = path.with_name(path.name + ".partial")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def declared_release_paths(assembly, root):
    rows = assembly.get("releases")
    if type(rows) is not list or len(rows) != 14:
        raise ValueError("release declaration matrix invalid")
    model_keys = [row.get("model_key") for row in rows if type(row) is dict]
    if (len(model_keys) != 14 or len(set(model_keys)) != 14
            or any(type(key) is not str or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", key)
                   for key in model_keys)):
        raise ValueError("release declaration matrix invalid")
    root = Path(root).resolve(strict=True)
    paths = [root / (key + ".json") for key in sorted(model_keys)]
    if any(not path.is_file() or path.is_symlink() for path in paths):
        raise ValueError("declared release missing")
    return paths


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assembly", required=True)
    parser.add_argument("--releases", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--engine-id", required=True)
    args = parser.parse_args()
    evidence_path = Path(args.evidence).resolve()
    if evidence_path.exists():
        raise ValueError("evidence already exists")
    assembly = json.loads(Path(args.assembly).read_text(encoding="utf-8"))
    images = {row["manifest_digest"]: row for row in assembly["images"]}
    releases = {}
    for path in declared_release_paths(assembly, args.releases):
        release = RuntimeRelease(json.loads(path.read_text(encoding="utf-8")))
        releases.setdefault(release.image_digest, release)
    if not set(releases) <= set(images) or len(releases) != 9:
        raise ValueError("release/image matrix mismatch")
    images = {key: images[key] for key in releases}
    config = EngineConfig(args.socket, args.engine_id, "/sys/fs/cgroup", "/sys/fs/cgroup",
                          api_version="1.51", server_version="28.3.2",
                          cgroup_driver="systemd", image_store="overlay2",
                          import_timeout=3600.0, import_io_timeout=900.0)
    importer = UnixRuntimeImporter(UnixEngine(config))
    result = {"schema": "mc.overlay2-import-matrix/1", "status": "in_progress",
              "engine_id": args.engine_id, "image_count": len(images), "images": []}
    checkpoint(evidence_path, result)
    for manifest_digest, release in sorted(releases.items()):
        image = images[manifest_digest]
        with Path(image["archive"]).open("rb") as stream:
            verified = verify_oci_archive(stream, release)
            try:
                inspected = importer.inspect(release, verified)
                disposition = "already_present"
            except Exception as error:
                if getattr(error, "code", None) != "engine_object_missing":
                    raise
                importer.load(stream, release)
                inspected = importer.inspect(release, verified)
                disposition = "imported"
        result["images"].append({"id": image["id"], "manifest_digest": manifest_digest,
                                 "image_id": release.data["image"]["image_id"],
                                 "archive_sha256": verified["archive_sha256"],
                                 "disposition": disposition, "inspection": inspected})
        checkpoint(evidence_path, result)
    result["status"] = "passed"
    checkpoint(evidence_path, result)
    print(canonical(result))


if __name__ == "__main__":
    main()
