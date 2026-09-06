#!/usr/bin/env python3
"""One-time, offline migration of legacy deployment directories to model assets.

Preparation hashes files and constructs hard-linked immutable asset trees while
the service may remain online. Commit requires the service to be stopped,
revalidates every source inode/size/mtime, marks files read-only, and binds all
deployments in one SQLite transaction. No source file is moved or deleted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mediacenter.repository import Repository


IGNORED_NAMES = {".mediacenter-ready"}
IGNORED_DIRECTORIES = {".cache"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path, progress_label: str | None = None) -> str:
    digest = hashlib.sha256()
    processed = 0
    next_report = 8 * 1024 * 1024 * 1024
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
            processed += len(chunk)
            if progress_label and processed >= next_report:
                print(json.dumps({"stage": "hashing", "file": progress_label,
                                  "bytes": processed}, ensure_ascii=False),
                      file=sys.stderr, flush=True)
                next_report += 8 * 1024 * 1024 * 1024
    return digest.hexdigest()


def manifest_digest(files: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda value: value["relative_path"]):
        digest.update(item["relative_path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(item["sha256"].encode("ascii"))
        digest.update(b"\0")
        digest.update(str(item["byte_size"]).encode("ascii"))
    return digest.hexdigest()


def _inside(path: Path, root: Path, label: str) -> Path:
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        raise ValueError(f"{label} escapes approved root: {resolved}") from None
    return resolved


def _deployment_rows(db_path: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in connection.execute(
            "PRAGMA table_info(model_deployments)")}
        asset_expression = "asset_id" if "asset_id" in columns else "NULL AS asset_id"
        rows = connection.execute(
            f"""SELECT id,catalog_key,kind,label,revision,license,model_path,
                       required_files_json,desired_state,actual_state,{asset_expression}
                FROM model_deployments ORDER BY id""").fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _model_files(model_path: Path) -> list[Path]:
    if not model_path.is_dir():
        raise ValueError(f"model directory is missing: {model_path}")
    files: list[Path] = []
    for root, directories, names in os.walk(model_path, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in IGNORED_DIRECTORIES)
        current = Path(root)
        for directory in directories:
            if (current / directory).is_symlink():
                raise ValueError(f"symlinked model directory is not allowed: {current / directory}")
        for name in sorted(names):
            if name in IGNORED_NAMES:
                continue
            if name == "manifest.json":
                raise ValueError(f"legacy model conflicts with asset manifest: {current / name}")
            path = current / name
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"non-regular model file is not allowed: {path}")
            files.append(path)
    if not files:
        raise ValueError(f"model directory has no migratable files: {model_path}")
    if len(files) > 20_000:
        raise ValueError(f"model directory exceeds the 20000 file limit: {model_path}")
    return files


def _asset_format(paths: set[str]) -> str:
    if "model_index.json" in paths:
        return "diffusers"
    if "config.json" in paths and any(path.endswith(".safetensors") for path in paths):
        return "transformers"
    if any(path.endswith(".safetensors") for path in paths):
        return "safetensors"
    if any(path.endswith(".gguf") for path in paths):
        return "gguf"
    return "trusted-bundle"


def prepare_migration(db_path: Path, models_root: Path, storage_root: Path,
                      plan_path: Path) -> dict[str, Any]:
    db_path = db_path.resolve()
    models_root = models_root.resolve()
    storage_root = storage_root.resolve(strict=False)
    if not db_path.is_file() or not models_root.is_dir():
        raise ValueError("database or models root is missing")
    if plan_path.exists():
        raise ValueError(f"migration plan already exists: {plan_path}")
    rows = _deployment_rows(db_path)
    if not rows:
        raise ValueError("there are no deployments to migrate")
    if any(row["asset_id"] is not None for row in rows):
        raise ValueError("migration requires every deployment to be unbound")
    if any(row["desired_state"] != "unloaded" or row["actual_state"] != "unloaded"
           for row in rows):
        raise ValueError("all deployments must be unloaded before migration preparation")

    (storage_root / "blobs" / "sha256").mkdir(parents=True, exist_ok=True)
    (storage_root / "assets").mkdir(parents=True, exist_ok=True)
    assets_by_digest: dict[str, dict[str, Any]] = {}
    bindings: list[dict[str, Any]] = []
    for row in rows:
        model_path = _inside(Path(row["model_path"]), models_root, "model path")
        print(json.dumps({"stage": "deployment_start", "deployment_id": row["id"]},
                         ensure_ascii=False), file=sys.stderr, flush=True)
        files: list[dict[str, Any]] = []
        for source in _model_files(model_path):
            relative = source.relative_to(model_path).as_posix()
            info = source.stat()
            if info.st_size <= 0:
                raise ValueError(f"empty model file is not allowed: {source}")
            digest = sha256_file(source, f"{row['id']}:{relative}")
            files.append({
                "relative_path": relative, "sha256": digest, "byte_size": info.st_size,
                "storage_relpath": f"blobs/sha256/{digest[:2]}/{digest}",
                "source_path": str(source), "source_device": info.st_dev,
                "source_inode": info.st_ino, "source_mtime_ns": info.st_mtime_ns,
            })
        required = json.loads(row["required_files_json"])
        available = {item["relative_path"] for item in files}
        missing = [relative for relative in required if relative not in available]
        if missing:
            raise ValueError(f"deployment {row['id']} misses required files: {missing}")
        digest = manifest_digest(files)
        asset_id = f"mdl_{digest[:16]}"
        asset = assets_by_digest.get(digest)
        if asset is None:
            paths = {item["relative_path"] for item in files}
            asset = {
                "id": asset_id, "display_name": row["label"], "media_kind": row["kind"],
                "role": "upscaler" if row["catalog_key"].startswith("realesrgan-") else "checkpoint",
                "format": _asset_format(paths), "source_type": "upload",
                "source_ref": f"legacy-deployment:{row['id']}", "revision": row["revision"],
                "license_declared": row["license"], "manifest_digest": digest,
                "total_bytes": sum(item["byte_size"] for item in files),
                "file_count": len(files), "storage_relpath": f"assets/{asset_id}",
                "files": files,
            }
            assets_by_digest[digest] = asset
            _materialize_asset(storage_root, asset)
        elif (asset["media_kind"] != row["kind"] or
              asset["revision"] != row["revision"] or
              asset["license_declared"] != row["license"]):
            raise ValueError(
                f"identical model content has incompatible deployment metadata: {row['id']}")
        bindings.append({"deployment_id": row["id"], "asset_id": asset["id"],
                         "model_path": str(storage_root / asset["storage_relpath"])})
        print(json.dumps({"stage": "deployment_prepared", "deployment_id": row["id"],
                          "files": len(files), "bytes": sum(item["byte_size"] for item in files)},
                         ensure_ascii=False), file=sys.stderr, flush=True)
    plan = {
        "schema_version": 1, "created_at": utc_now(), "db_path": str(db_path),
        "models_root": str(models_root), "storage_root": str(storage_root),
        "assets": list(assets_by_digest.values()), "bindings": bindings,
    }
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    with plan_path.open("x", encoding="utf-8") as handle:
        json.dump(plan, handle, ensure_ascii=False, sort_keys=True)
    return plan


def _materialize_asset(storage_root: Path, asset: dict[str, Any]) -> None:
    final = _inside(storage_root / asset["storage_relpath"], storage_root, "asset path")
    if final.exists():
        raise ValueError(f"asset path already exists: {final}")
    staging = _inside(storage_root / ".migration-staging" / asset["id"],
                      storage_root, "staging path")
    if staging.exists():
        raise ValueError(f"migration staging path already exists: {staging}")
    staging.mkdir(parents=True)
    for item in asset["files"]:
        source = Path(item["source_path"])
        blob = _inside(storage_root / item["storage_relpath"], storage_root, "blob path")
        blob.parent.mkdir(parents=True, exist_ok=True)
        if blob.exists():
            if blob.stat().st_size != item["byte_size"] or sha256_file(blob) != item["sha256"]:
                raise ValueError(f"existing content blob is invalid: {blob}")
        else:
            os.link(source, blob)
        target = staging / Path(item["relative_path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(blob, target)
    public_files = [{key: item[key] for key in ("relative_path", "sha256", "byte_size")}
                    for item in asset["files"]]
    (staging / "manifest.json").write_text(json.dumps({
        "asset_id": asset["id"], "manifest_digest": asset["manifest_digest"],
        "files": public_files,
    }, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    staging.replace(final)


def commit_migration(plan_path: Path, *, require_stopped: bool = True) -> dict[str, Any]:
    if require_stopped:
        result = subprocess.run(["systemctl", "--user", "is-active", "mediacenter.service"],
                                capture_output=True, text=True)
        if result.stdout.strip() != "inactive":
            raise RuntimeError("mediacenter.service must be inactive before migration commit")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("schema_version") != 1:
        raise ValueError("unsupported migration plan")
    storage_root = Path(plan["storage_root"]).resolve()
    for asset in plan["assets"]:
        manifest = storage_root / asset["storage_relpath"] / "manifest.json"
        stored = json.loads(manifest.read_text(encoding="utf-8"))
        if stored.get("asset_id") != asset["id"] or stored.get("manifest_digest") != asset["manifest_digest"]:
            raise ValueError(f"prepared asset manifest changed: {asset['id']}")
        for item in asset["files"]:
            source = Path(item["source_path"])
            info = source.stat()
            current = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            expected = (item["source_device"], item["source_inode"],
                        item["byte_size"], item["source_mtime_ns"])
            if current != expected:
                raise ValueError(f"model file changed after preparation: {source}")
            blob = storage_root / item["storage_relpath"]
            target = storage_root / asset["storage_relpath"] / item["relative_path"]
            if (not blob.is_file() or not target.is_file() or
                    blob.stat().st_ino != target.stat().st_ino or
                    blob.stat().st_size != item["byte_size"]):
                raise ValueError(f"prepared hard link is missing: {blob}")
            source.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            blob.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        manifest.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    occurred_at = utc_now()
    repository = Repository(plan["db_path"])
    repository.publish_migrated_model_assets(plan["assets"], plan["bindings"], occurred_at)
    return {"asset_count": len(plan["assets"]), "deployment_count": len(plan["bindings"]),
            "committed_at": occurred_at}


def main() -> None:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare", action="store_true")
    action.add_argument("--commit", action="store_true")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--models-root", type=Path)
    parser.add_argument("--storage-root", type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    args = parser.parse_args()
    if args.prepare:
        if not all((args.db, args.models_root, args.storage_root)):
            parser.error("--prepare requires --db, --models-root and --storage-root")
        result = prepare_migration(args.db, args.models_root, args.storage_root, args.plan)
        output = {"asset_count": len(result["assets"]),
                  "deployment_count": len(result["bindings"]), "plan": str(args.plan)}
    else:
        result = commit_migration(args.plan)
        output = dict(result, plan=str(args.plan))
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
