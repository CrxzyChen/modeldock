"""Offline task metadata copy migration. Never edits the source or media bytes.

Usage: python -B scripts/migrate_task_kernel.py migrate --source OLD.db
       --output NEW.db --backup BACKUP.db --offline-confirmed
Rollback creates another new database: rollback --receipt NEW.db.receipt.json
       --output RESTORED.db --offline-confirmed
An operator must stop the owning service before invoking either operation.
All outputs are exclusive, retained on failure, and must not replace a live DB.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path, PurePosixPath

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mediacenter.task_state import (SCHEMA_VERSION, TaskStateError, canonical, create_schema,
                                    upgrade_schema_v1, upgrade_schema_v2, upgrade_schema_v3,
                                    upgrade_schema_v4, upgrade_schema_v5, upgrade_schema_v6,
                                    upgrade_schema_v7, upgrade_schema_v8,
                                    upgrade_schema_v9, upgrade_schema_v10, upgrade_schema_v11, upgrade_schema_v12,
                                    upgrade_schema_v13)


def checked(path, *, existing=False):
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise TaskStateError("migration_symlink_rejected")
    if existing and not path.is_file():
        raise TaskStateError("migration_source_missing")
    if not existing and (path.exists() or not path.parent.is_dir()):
        raise TaskStateError("migration_output_not_exclusive")
    return path


def sha(path):
    with path.open("rb") as stream:
        result = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def copy_database(source, target):
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.execute("PRAGMA query_only=ON")
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise TaskStateError("migration_integrity_failed")
    finally:
        dst.close(); src.close()
    with target.open("r+b") as stream:
        os.fsync(stream.fileno())


def migrate(source, output, backup, *, offline_confirmed=False, fault=lambda _: None):
    if not offline_confirmed:
        raise TaskStateError("offline_confirmation_required")
    source, output, backup = checked(source, existing=True), checked(output), checked(backup)
    if len({source, output, backup}) != 3:
        raise TaskStateError("migration_paths_overlap")
    receipt = checked(str(output) + ".receipt.json")
    copy_database(source, backup)
    copy_database(backup, output)
    db = sqlite3.connect(output)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        versioned = db.execute("SELECT 1 FROM sqlite_master WHERE name='task_kernel_metadata'").fetchone() is not None
        if versioned:
            versions = [row[0] for row in db.execute("SELECT version FROM task_kernel_metadata")]
            if versions not in ([1], [2], [3], [4], [5], [6], [7], [8], [9], [10], [11], [12], [13]):
                raise TaskStateError("task_schema_already_current" if versions == [SCHEMA_VERSION] else "task_schema_unsupported")
        db.execute("BEGIN IMMEDIATE")
        if versioned:
            # No task/attempt/installation/media rewriting on a v1 upgrade.
            db.row_factory = None
            if versions == [1]:
                upgrade_schema_v1(db)
            if versions in ([1], [2]):
                upgrade_schema_v2(db)
            if versions in ([1], [2], [3]):
                upgrade_schema_v3(db)
            if versions in ([1], [2], [3], [4]):
                upgrade_schema_v4(db)
            if versions in ([1], [2], [3], [4], [5]):
                upgrade_schema_v5(db)
            if versions in ([1], [2], [3], [4], [5], [6]):
                upgrade_schema_v6(db)
            if versions in ([1], [2], [3], [4], [5], [6], [7]):
                upgrade_schema_v7(db)
            if versions in ([1], [2], [3], [4], [5], [6], [7], [8]):
                upgrade_schema_v8(db)
            if versions[0] <= 9:
                upgrade_schema_v9(db)
            if versions[0] <= 10:
                upgrade_schema_v10(db)
            if versions[0] <= 11:
                upgrade_schema_v11(db)
            if versions[0] <= 12:
                upgrade_schema_v12(db)
            upgrade_schema_v13(db)
            db.row_factory = sqlite3.Row
            fault("migration.runtime_schema")
            rows = db.execute("SELECT * FROM tasks").fetchall()
        else:
            rows = _migrate_legacy(db)
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='model_deployments'").fetchone():
                columns = {row[1] for row in db.execute('PRAGMA table_info(model_deployments)')}
                if 'removal_operation_id' not in columns:
                    db.execute('ALTER TABLE model_deployments ADD COLUMN removal_operation_id TEXT')
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise TaskStateError("migration_foreign_key_failure")
        fault("migration.before_commit")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    with output.open("r+b") as stream:
        os.fsync(stream.fileno())
    evidence = {"schema": SCHEMA_VERSION, "source_schema": versions[0] if versioned else 0,
                "source": str(source), "backup": str(backup), "output": str(output),
                "backup_sha256": sha(backup), "output_sha256": sha(output), "tasks": len(rows),
                "media_modified": False, "legacy_execution_confirmed": False, "container_ownership_inferred": False}
    with receipt.open("x", encoding="utf-8") as stream:
        os.chmod(receipt, 0o600)
        stream.write(canonical(evidence)); stream.flush(); os.fsync(stream.fileno())
    return evidence


def _migrate_legacy(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
    if not {"id", "service", "status", "prompt", "options_json", "created_at", "updated_at"} <= columns:
        raise TaskStateError("legacy_task_schema_unknown")
    additions = {"model_key": "TEXT", "inputs_json": "TEXT DEFAULT '[]'", "output_json": "TEXT",
                 "execution_json": "TEXT", "artifact_path": "TEXT", "artifact_bytes": "INTEGER",
                 "error": "TEXT", "progress": "REAL DEFAULT 0", "stage": "TEXT DEFAULT 'queued'",
                 "stage_detail_json": "TEXT", "cancel_requested": "INTEGER DEFAULT 0"}
    for name, declaration in additions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {declaration}")
    create_schema(db)
    rows = db.execute("SELECT * FROM tasks").fetchall()
    defaults = {"image": "sdxl-base-1.0", "video": "wan2.1-t2v-1.3b", "speech": "cosyvoice2-0.5b", "music": "musicgen-small"}
    for row in rows:
        if row["status"] not in {"queued", "running", "succeeded", "failed", "canceled"}:
            raise TaskStateError("legacy_task_state_unknown")
        # Preserve uncertain running work as running. No forged epoch/attempt
        # or exit acknowledgement; TaskState refuses dispatch/cancel/retry.
        db.execute("UPDATE tasks SET migration_source='legacy-v0',model_key=?,stage=? WHERE id=?",
                   (row["model_key"] or defaults[row["service"]],
                    "completed" if row["status"] == "succeeded" else row["status"], row["id"]))
        if row["status"] != "succeeded" or not row["output_json"]:
            continue
        output_record = json.loads(row["output_json"])
        path = row["artifact_path"] or str(output_record.get("artifact_url", "")).removeprefix("/api/v1/artifacts/")
        size = row["artifact_bytes"] if row["artifact_bytes"] is not None else output_record.get("bytes")
        parsed = PurePosixPath(path)
        if (not path or parsed.is_absolute() or ".." in parsed.parts or "\\" in path or ":" in path
                or str(parsed) != path or type(size) is not int or size < 0):
            raise TaskStateError("legacy_artifact_contract_invalid")
        output_record.update(artifact_path=path, artifact_url="/api/v1/artifacts/" + path, bytes=size)
        db.execute("UPDATE tasks SET artifact_path=?,artifact_bytes=?,output_json=? WHERE id=?", (path, size, canonical(output_record), row["id"]))
        db.execute("INSERT INTO task_artifacts VALUES(?,NULL,NULL,NULL,?,?,NULL,'legacy-readonly')", (row["id"], path, size))
    return rows


def rollback(receipt, output, *, offline_confirmed=False):
    if not offline_confirmed:
        raise TaskStateError("offline_confirmation_required")
    receipt, output = checked(receipt, existing=True), checked(output)
    evidence = json.loads(receipt.read_text(encoding="utf-8"))
    backup = checked(evidence["backup"], existing=True)
    if sha(backup) != evidence["backup_sha256"]:
        raise TaskStateError("migration_backup_changed")
    if any(Path(str(backup) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise TaskStateError("migration_backup_not_self_contained")
    # The retained backup is already an offline, self-contained DELETE-mode
    # database. SQLite backup() changes header counters on a WAL-origin DB;
    # rollback promises the verified bytes, not another logical transformation.
    with backup.open("rb") as source, output.open("xb") as target:
        os.chmod(output, 0o600)
        for block in iter(lambda: source.read(1024 * 1024), b""):
            target.write(block)
        target.flush(); os.fsync(target.fileno())
    if sha(output) != evidence["backup_sha256"]:
        raise TaskStateError("migration_restore_mismatch")
    check = sqlite3.connect(output.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise TaskStateError("migration_integrity_failed")
    finally:
        check.close()
    return {"output": str(output), "sha256": sha(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("migrate", "rollback"):
        args = sub.add_parser(command)
        args.add_argument("--output", required=True)
        args.add_argument("--offline-confirmed", action="store_true")
        if command == "migrate":
            args.add_argument("--source", required=True); args.add_argument("--backup", required=True)
        else:
            args.add_argument("--receipt", required=True)
    args = vars(parser.parse_args())
    command = args.pop("command")
    result = migrate(**args) if command == "migrate" else rollback(**args)
    print(canonical(result))


if __name__ == "__main__":
    main()
