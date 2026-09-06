from __future__ import annotations

import json
import hashlib
import os
import secrets
import sqlite3
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .domain import SERVICE_DEFAULTS, ServiceKind
from .task_state import SCHEMA_VERSION, TASK_COLUMNS, TaskStateError, create_schema


INSTALL_ACTIVE = {"preflight", "downloading", "verifying", "preparing", "checking"}
InstallationOwner = tuple[str, str, str | None]  # operation, attempt, runner token


class InstallationOwnershipError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _startup_file(path, limit):
    """Read-only source evidence. A schema probe must not create WAL/SHM."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode):
        raise TaskStateError("task_schema_probe_source_invalid")
    if before.st_size > limit:
        raise TaskStateError("task_schema_probe_limit_exceeded")
    result = hashlib.sha256()
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        consumed = 0
        for block in iter(lambda: stream.read(min(1024 * 1024, limit - consumed + 1)), b""):
            consumed += len(block)
            if consumed > limit:
                raise TaskStateError("task_schema_probe_limit_exceeded")
            result.update(block)
    after = path.stat()
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    if identity(before) != identity(opened) or identity(before) != identity(after):
        raise TaskStateError("task_schema_probe_source_changed")
    return (*identity(before), result.hexdigest())


@contextmanager
def _startup_schema_connection(path):
    # A fixed-size private inspection copy is not another task authority. It
    # performs recovery solely to see a WAL-origin schema without writing even
    # SHM read marks in the source directory. Retries are fixed and bounded.
    paths = [(path, 256 * 1024 * 1024), (Path(str(path) + "-wal"), 64 * 1024 * 1024),
             (Path(str(path) + "-journal"), 64 * 1024 * 1024)]
    with tempfile.TemporaryDirectory(prefix="mediacenter-schema-probe-") as scratch:
        for attempt in range(5):
            try:
                before = [_startup_file(value, limit) for value, limit in paths]
                target = Path(scratch) / f"metadata-{attempt}.db"
                for (source, limit), evidence, suffix in zip(paths, before, ("", "-wal", "-journal")):
                    if evidence is None:
                        continue
                    copy_path = Path(str(target) + suffix)
                    with source.open("rb") as stream, copy_path.open("xb") as output:
                        os.chmod(copy_path, 0o600)
                        copied = 0
                        for block in iter(lambda: stream.read(1024 * 1024), b""):
                            copied += len(block)
                            if copied > limit:
                                raise TaskStateError("task_schema_probe_limit_exceeded")
                            output.write(block)
                    if _startup_file(copy_path, limit)[-1] != evidence[-1]:
                        raise TaskStateError("task_schema_probe_source_changed")
                if [_startup_file(value, limit) for value, limit in paths] == before:
                    break
            except FileNotFoundError:
                error = TaskStateError("task_schema_probe_source_changed")
            except TaskStateError as error:
                if error.code not in {"task_schema_probe_source_changed"}:
                    raise
            if attempt == 4:
                raise TaskStateError("task_schema_probe_source_changed")
            # Let a normal short concurrent transaction finish; five immediate
            # byte copies otherwise exhaust the bound before its commit. This
            # is still a fixed 150ms aggregate backoff, never a retry loop.
            time.sleep(0.01 * (2 ** attempt))
        # Even the immutable fast path uses the verified private main-file copy:
        # a concurrent writer may create WAL after the initial existence probe.
        db = (sqlite3.connect(target) if before[1] is not None or before[2] is not None else
              sqlite3.connect(target.resolve().as_uri() + "?mode=ro&immutable=1", uri=True))
        try:
            db.execute("PRAGMA query_only=ON")
            yield db
        finally:
            db.close()


def _pid_alive(pid: int | None) -> bool:
    """Conservative liveness only. PID reuse blocks recovery rather than stealing."""
    if pid is None:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # Access denied is not proof of exit.
        try:
            return kernel.WaitForSingleObject(handle, 0) != 0
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Repository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Existing task metadata upgrades are explicit offline operations. Check
        # before WAL negotiation or any schema/data writes.
        if self.path.exists() and self.path.stat().st_size:
            with _startup_schema_connection(self.path) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "tasks" in tables and "task_kernel_metadata" not in tables:
                    raise TaskStateError("task_schema_migration_required")
                if "task_kernel_metadata" in tables:
                    versions = [row[0] for row in db.execute("SELECT version FROM task_kernel_metadata")]
                    columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
                    if versions != [SCHEMA_VERSION] or not TASK_COLUMNS.keys() <= columns:
                        raise TaskStateError("task_schema_unsupported")
                    attempt_columns = {row[1] for row in db.execute('PRAGMA table_info(task_attempts)')}
                    if not {'execution_deadline_at', 'termination_reason'} <= attempt_columns:
                        raise TaskStateError('task_schema_unsupported')
                    if not {"runtime_intents", "runtime_effects", "runtime_container_removals",
                            "instance_policies", "instance_claims",
                            "model_operations", "instance_inbox", "instance_cancel_deadlines",
                            "model_transfer_cleanup", "model_transfer_writes"} <= tables:
                        raise TaskStateError("task_schema_unsupported")
                    policy_columns = {row[1] for row in db.execute("PRAGMA table_info(instance_policies)")}
                    if not {"configuration_state", "pending_policy_json", "pending_policy_digest",
                            "pending_restart_recovery", "resume_after_apply",
                            "configuration_error"} <= policy_columns:
                        raise TaskStateError("task_schema_unsupported")
                    if "deployment_operations" in tables:
                        operation_columns = {row[1] for row in db.execute("PRAGMA table_info(deployment_operations)")}
                        if not {"configuration_context_json", "configuration_context_digest"} <= operation_columns:
                            raise TaskStateError("task_schema_unsupported")
                    if "model_deployments" in tables:
                        deployment_columns = {row[1] for row in db.execute("PRAGMA table_info(model_deployments)")}
                        if "removal_operation_id" not in deployment_columns:
                            raise TaskStateError("task_schema_unsupported")
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            # WAL is a persistent database setting. Negotiating it on every
            # short-lived connection can block concurrent readers and writers.
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS services (
                    kind TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
                    timeout_seconds INTEGER NOT NULL CHECK (timeout_seconds BETWEEN 10 AND 7200)
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, service TEXT NOT NULL REFERENCES services(kind),
                    model_key TEXT NOT NULL,
                    status TEXT NOT NULL, prompt TEXT NOT NULL, options_json TEXT NOT NULL,
                    inputs_json TEXT NOT NULL DEFAULT '[]',
                    output_json TEXT, execution_json TEXT, artifact_path TEXT,
                    artifact_bytes INTEGER, error TEXT, progress REAL NOT NULL DEFAULT 0,
                    stage TEXT NOT NULL DEFAULT 'queued', stage_detail_json TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tasks_created_idx ON tasks(created_at DESC);
                CREATE INDEX IF NOT EXISTS tasks_status_idx ON tasks(status, created_at);
                CREATE TABLE IF NOT EXISTS input_assets (
                    id TEXT PRIMARY KEY, filename TEXT NOT NULL, media_type TEXT NOT NULL,
                    sha256 TEXT NOT NULL, byte_size INTEGER NOT NULL, storage_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at TEXT NOT NULL,
                    action TEXT NOT NULL, target TEXT NOT NULL, detail_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_assets (
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    media_kind TEXT NOT NULL CHECK (media_kind IN ('image','video','speech','music','general')),
                    role TEXT NOT NULL CHECK (role IN ('checkpoint','lora','adapter','vae','encoder','upscaler','control')),
                    format TEXT NOT NULL CHECK (format IN ('safetensors','gguf','diffusers','transformers','trusted-bundle')),
                    source_type TEXT NOT NULL CHECK (source_type IN ('huggingface','github-release','https','upload')),
                    source_ref TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    license_declared TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK (state IN ('quarantined','verifying','ready','rejected','archived')),
                    total_bytes INTEGER NOT NULL CHECK (total_bytes >= 0),
                    file_count INTEGER NOT NULL CHECK (file_count > 0),
                    storage_relpath TEXT NOT NULL UNIQUE,
                    architecture_family TEXT NOT NULL DEFAULT 'unknown',
                    tensor_precision TEXT,
                    parameter_summary_json TEXT NOT NULL DEFAULT '{}',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    detector_version TEXT NOT NULL DEFAULT 'legacy',
                    metadata_digest TEXT,
                    created_at TEXT NOT NULL,
                    verified_at TEXT,
                    updated_at TEXT NOT NULL,
                    archived_at TEXT
                );
                CREATE TABLE IF NOT EXISTS model_asset_files (
                    asset_id TEXT NOT NULL REFERENCES model_assets(id),
                    relative_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
                    storage_relpath TEXT NOT NULL,
                    PRIMARY KEY(asset_id, relative_path)
                );
                CREATE TABLE IF NOT EXISTS runtime_profiles (
                    profile_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision > 0),
                    label TEXT NOT NULL,
                    image_digest TEXT NOT NULL,
                    worker_protocol TEXT NOT NULL,
                    architecture_families_json TEXT NOT NULL,
                    main_formats_json TEXT NOT NULL,
                    optional_deployment_roles_json TEXT NOT NULL,
                    task_roles_json TEXT NOT NULL,
                    loader TEXT NOT NULL,
                    trust_remote_code INTEGER NOT NULL DEFAULT 0 CHECK (trust_remote_code=0),
                    residency_modes_json TEXT NOT NULL,
                    required_vram_mib INTEGER NOT NULL CHECK (required_vram_mib > 0),
                    profile_digest TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(profile_id, revision)
                );
                CREATE TABLE IF NOT EXISTS asset_compatibility (
                    subject_asset_id TEXT NOT NULL REFERENCES model_assets(id) ON DELETE RESTRICT,
                    subject_revision TEXT NOT NULL,
                    base_asset_id TEXT NOT NULL REFERENCES model_assets(id) ON DELETE RESTRICT,
                    base_revision TEXT NOT NULL,
                    detector_version TEXT NOT NULL,
                    verdict TEXT NOT NULL CHECK (verdict IN (
                        'exact','compatible','experimental','incompatible','unknown')),
                    reason_codes_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    evidence_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(subject_asset_id,subject_revision,base_asset_id,base_revision,detector_version)
                );
                CREATE TABLE IF NOT EXISTS model_transfers (
                    id TEXT PRIMARY KEY,
                    direction TEXT NOT NULL CHECK (direction IN ('download','upload')),
                    state TEXT NOT NULL CHECK (state IN ('queued','transferring','paused','verifying','succeeded','failed','canceled')),
                    display_name TEXT NOT NULL,
                    media_kind TEXT NOT NULL,
                    role TEXT NOT NULL,
                    format TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    license_declared TEXT NOT NULL,
                    expected_bytes INTEGER,
                    received_bytes INTEGER NOT NULL DEFAULT 0,
                    quarantine_relpath TEXT NOT NULL UNIQUE,
                    asset_id TEXT REFERENCES model_assets(id),
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_transfer_files (
                    id TEXT PRIMARY KEY,
                    transfer_id TEXT NOT NULL REFERENCES model_transfers(id),
                    relative_path TEXT NOT NULL,
                    expected_bytes INTEGER NOT NULL CHECK (expected_bytes >= 0),
                    expected_sha256 TEXT,
                    source_url TEXT,
                    received_bytes INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(transfer_id, relative_path)
                );
                CREATE TABLE IF NOT EXISTS model_deployments (
                    id TEXT PRIMARY KEY,
                    asset_id TEXT REFERENCES model_assets(id),
                    catalog_key TEXT NOT NULL,
                    kind TEXT NOT NULL REFERENCES services(kind),
                    label TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
                    is_default INTEGER NOT NULL CHECK (is_default IN (0,1)),
                    gpu_indices_json TEXT NOT NULL,
                    model_path TEXT NOT NULL,
                    license TEXT NOT NULL,
                    required_files_json TEXT NOT NULL,
                    required_vram_mib INTEGER NOT NULL CHECK (required_vram_mib > 0),
                    gpu_sharing_mode TEXT NOT NULL DEFAULT 'exclusive' CHECK (gpu_sharing_mode IN ('exclusive','shared')),
                    external_reserve_mib INTEGER NOT NULL DEFAULT 8192 CHECK (external_reserve_mib IN (2048,4096,8192,12288,16384,24576,32768)),
                    warm_ttl_seconds INTEGER NOT NULL DEFAULT 0 CHECK (warm_ttl_seconds IN (0,300,900,1800)),
                    desired_state TEXT NOT NULL DEFAULT 'unloaded' CHECK (desired_state IN ('unloaded','loaded')),
                    startup_policy TEXT NOT NULL DEFAULT 'manual' CHECK (startup_policy IN ('manual','auto')),
                    actual_state TEXT NOT NULL DEFAULT 'unloaded' CHECK (actual_state IN ('unloaded','loading','loaded','unloading','error')),
                    current_config_revision INTEGER,
                    pending_config_revision INTEGER,
                    removal_operation_id TEXT,
                    service_desired_state TEXT NOT NULL DEFAULT 'stopped'
                        CHECK (service_desired_state IN ('stopped','running')),
                    service_observed_state TEXT NOT NULL DEFAULT 'stopped'
                        CHECK (service_observed_state IN (
                            'absent','created','starting','running','ready','draining','stopped','error')),
                    runtime_last_error TEXT,
                    runtime_updated_at TEXT,
                    install_state TEXT NOT NULL CHECK (install_state IN ('configured','installing','ready','failed')),
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_deployment_revisions (
                    deployment_id TEXT NOT NULL REFERENCES model_deployments(id) ON DELETE CASCADE,
                    config_revision INTEGER NOT NULL CHECK (config_revision > 0),
                    runtime_profile_id TEXT NOT NULL,
                    runtime_profile_revision INTEGER NOT NULL CHECK (runtime_profile_revision > 0),
                    runtime_profile_digest TEXT NOT NULL,
                    runtime_image_digest TEXT NOT NULL,
                    base_asset_id TEXT NOT NULL REFERENCES model_assets(id) ON DELETE RESTRICT,
                    base_asset_revision TEXT NOT NULL,
                    base_asset_manifest_digest TEXT NOT NULL,
                    vae_asset_id TEXT REFERENCES model_assets(id) ON DELETE RESTRICT,
                    vae_asset_revision TEXT,
                    vae_asset_manifest_digest TEXT,
                    gpu_uuids_json TEXT NOT NULL,
                    required_vram_mib INTEGER NOT NULL CHECK (required_vram_mib > 0),
                    sharing_mode TEXT NOT NULL CHECK (sharing_mode IN ('exclusive','shared')),
                    residency TEXT NOT NULL CHECK (residency IN ('on_demand','idle','resident')),
                    external_reserve_mib INTEGER NOT NULL DEFAULT 8192
                        CHECK (external_reserve_mib IN (2048,4096,8192,12288,16384,24576,32768)),
                    idle_seconds INTEGER NOT NULL DEFAULT 0 CHECK (idle_seconds BETWEEN 0 AND 86400),
                    license_confirmation_json TEXT NOT NULL,
                    experimental_compatibility_accepted INTEGER NOT NULL DEFAULT 0
                        CHECK (experimental_compatibility_accepted IN (0,1)),
                    desired_state TEXT NOT NULL CHECK (desired_state IN ('stopped','running')),
                    config_digest TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(deployment_id,config_revision),
                    FOREIGN KEY(runtime_profile_id,runtime_profile_revision)
                        REFERENCES runtime_profiles(profile_id,revision),
                    CHECK ((vae_asset_id IS NULL AND vae_asset_revision IS NULL
                            AND vae_asset_manifest_digest IS NULL)
                        OR (vae_asset_id IS NOT NULL AND vae_asset_revision IS NOT NULL
                            AND vae_asset_manifest_digest IS NOT NULL))
                );
                CREATE TABLE IF NOT EXISTS deployment_operations (
                    id TEXT PRIMARY KEY,
                    server_profile_id TEXT NOT NULL,
                    authenticated_principal TEXT NOT NULL,
                    route TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    deployment_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN (
                        'planned','accepted','preparing_runtime','creating_container',
                        'starting_worker','loading_model','health_check','ready',
                        'canceling','canceled','rollback','failed')),
                    payload_json TEXT NOT NULL,
                    plan_digest TEXT NOT NULL,
                    confirmations_json TEXT NOT NULL,
                    milestone TEXT NOT NULL,
                    recovery_cursor_json TEXT,
                    error_class TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    result_json TEXT,
                    configuration_context_json TEXT,
                    configuration_context_digest TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(server_profile_id,authenticated_principal,route,idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS deployment_operations_deployment_idx
                    ON deployment_operations(deployment_id,created_at DESC);
                CREATE TABLE IF NOT EXISTS deployment_operation_resources (
                    operation_id TEXT NOT NULL REFERENCES deployment_operations(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK (kind IN (
                        'runtime-image','asset','deployment','container','gpu-lease')),
                    resource_id TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    created INTEGER NOT NULL CHECK (created IN (0,1)),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(operation_id,kind,resource_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS model_deployments_default_idx
                    ON model_deployments(kind) WHERE is_default=1;
                CREATE TABLE IF NOT EXISTS model_deployment_dependencies (
                    deployment_id TEXT NOT NULL REFERENCES model_deployments(id) ON DELETE CASCADE,
                    dependency_key TEXT NOT NULL,
                    dependency_deployment_id TEXT NOT NULL REFERENCES model_deployments(id) ON DELETE RESTRICT,
                    dependency_asset_id TEXT NOT NULL REFERENCES model_assets(id) ON DELETE RESTRICT,
                    dependency_revision TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(deployment_id, dependency_key),
                    CHECK(deployment_id != dependency_deployment_id)
                );
                CREATE INDEX IF NOT EXISTS model_deployment_dependencies_target_idx
                    ON model_deployment_dependencies(dependency_deployment_id);
                CREATE TABLE IF NOT EXISTS service_installations (
                    id TEXT PRIMARY KEY,
                    recipe_key TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN (
                        'preflight','downloading','paused','verifying','preparing','checking',
                        'ready','failed','canceled')),
                    current_step TEXT NOT NULL,
                    progress REAL NOT NULL CHECK (progress BETWEEN 0 AND 1),
                    deployment_id TEXT,
                    transfer_id TEXT REFERENCES model_transfers(id),
                    asset_id TEXT REFERENCES model_assets(id),
                    options_json TEXT NOT NULL,
                    steps_json TEXT NOT NULL,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS service_installations_created_idx
                    ON service_installations(created_at DESC);
                CREATE TABLE IF NOT EXISTS installation_attempts (
                    id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL REFERENCES service_installations(id),
                    generation INTEGER NOT NULL CHECK(generation > 0),
                    state TEXT NOT NULL,
                    runner_token TEXT,
                    owner_pid INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(operation_id,generation)
                );
                CREATE TABLE IF NOT EXISTS installation_resources (
                    attempt_id TEXT NOT NULL REFERENCES installation_attempts(id),
                    kind TEXT NOT NULL CHECK(kind IN ('deployment','transfer','asset','runtime-transfer','runtime-image','runtime-binding')),
                    resource_id TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    created INTEGER NOT NULL CHECK(created IN (0,1)),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(attempt_id,kind,resource_id)
                );
                CREATE TABLE IF NOT EXISTS transfer_download_runs (
                    transfer_id TEXT PRIMARY KEY REFERENCES model_transfers(id),
                    token TEXT NOT NULL, owner_pid INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('running','stopped'))
                );
                """
            )
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_kernel_metadata'").fetchone():
                db.execute("BEGIN IMMEDIATE")
                create_schema(db)
            deployment_columns = {row[1] for row in db.execute("PRAGMA table_info(model_deployments)")}
            retired_columns = {"python_path", "module", "extension", "probe_enabled"}
            if deployment_columns & retired_columns:
                db.execute("DROP TABLE IF EXISTS model_deployment_runtimes")
                for column in sorted(deployment_columns & retired_columns):
                    db.execute(f"ALTER TABLE model_deployments DROP COLUMN {column}")
                deployment_columns -= retired_columns
            if "incarnation" not in deployment_columns:
                db.execute("ALTER TABLE model_deployments ADD COLUMN incarnation TEXT")
                db.execute("UPDATE model_deployments SET incarnation=lower(hex(randomblob(16))) WHERE incarnation IS NULL")
            installation_columns = {row[1] for row in db.execute("PRAGMA table_info(service_installations)")}
            resource_columns = {row[1] for row in db.execute("PRAGMA table_info(installation_resources)")}
            if "successor_attempt_id" not in resource_columns:
                db.execute("ALTER TABLE installation_resources ADD COLUMN successor_attempt_id TEXT")
            resource_sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='installation_resources'"
            ).fetchone()[0]
            if "'environment'" in resource_sql:
                db.executescript("""
                    CREATE TABLE installation_resources_v2 (
                        attempt_id TEXT NOT NULL REFERENCES installation_attempts(id),
                        kind TEXT NOT NULL CHECK(kind IN ('deployment','transfer','asset','runtime-transfer','runtime-image','runtime-binding')),
                        resource_id TEXT NOT NULL,
                        identity TEXT NOT NULL,
                        created INTEGER NOT NULL CHECK(created IN (0,1)),
                        created_at TEXT NOT NULL,
                        successor_attempt_id TEXT,
                        PRIMARY KEY(attempt_id,kind,resource_id)
                    );
                    INSERT INTO installation_resources_v2
                        SELECT attempt_id,kind,resource_id,identity,created,created_at,successor_attempt_id
                        FROM installation_resources WHERE kind!='environment';
                    DROP TABLE installation_resources;
                    ALTER TABLE installation_resources_v2 RENAME TO installation_resources;
                    DROP TABLE IF EXISTS environment_preparations;
                """)
            else:
                db.execute("DROP TABLE IF EXISTS environment_preparations")
            if "current_attempt_id" not in installation_columns:
                db.execute("ALTER TABLE service_installations ADD COLUMN current_attempt_id TEXT")
            if "recipe_json" not in installation_columns:
                db.execute("ALTER TABLE service_installations ADD COLUMN recipe_json TEXT")
            transfer_file_columns = {row[1] for row in db.execute(
                "PRAGMA table_info(model_transfer_files)")}
            if "source_url" not in transfer_file_columns:
                db.execute("ALTER TABLE model_transfer_files ADD COLUMN source_url TEXT")
            asset_columns = {row[1] for row in db.execute("PRAGMA table_info(model_assets)")}
            asset_column_migrations = {
                "architecture_family": "TEXT NOT NULL DEFAULT 'unknown'",
                "tensor_precision": "TEXT",
                "parameter_summary_json": "TEXT NOT NULL DEFAULT '{}'",
                "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
                "detector_version": "TEXT NOT NULL DEFAULT 'legacy'",
                "metadata_digest": "TEXT",
            }
            for column, declaration in asset_column_migrations.items():
                if column not in asset_columns:
                    db.execute(f"ALTER TABLE model_assets ADD COLUMN {column} {declaration}")
            revision_columns = {row[1] for row in db.execute(
                "PRAGMA table_info(model_deployment_revisions)")}
            for column, declaration in {
                "external_reserve_mib": "INTEGER NOT NULL DEFAULT 8192",
                "idle_seconds": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if column not in revision_columns:
                    db.execute(
                        f"ALTER TABLE model_deployment_revisions ADD COLUMN {column} {declaration}")
            if "asset_id" not in deployment_columns:
                db.execute("ALTER TABLE model_deployments ADD COLUMN asset_id TEXT REFERENCES model_assets(id)")
            if "warm_ttl_seconds" not in deployment_columns:
                db.execute("ALTER TABLE model_deployments ADD COLUMN warm_ttl_seconds INTEGER NOT NULL DEFAULT 0")
            if "desired_state" not in deployment_columns:
                db.execute("ALTER TABLE model_deployments ADD COLUMN desired_state TEXT NOT NULL DEFAULT 'unloaded'")
            if "startup_policy" not in deployment_columns:
                db.execute("ALTER TABLE model_deployments ADD COLUMN startup_policy TEXT NOT NULL DEFAULT 'manual'")
            if "actual_state" not in deployment_columns:
                db.execute("ALTER TABLE model_deployments ADD COLUMN actual_state TEXT NOT NULL DEFAULT 'unloaded'")
            if "runtime_last_error" not in deployment_columns:
                db.execute("ALTER TABLE model_deployments ADD COLUMN runtime_last_error TEXT")
            if "runtime_updated_at" not in deployment_columns:
                db.execute("ALTER TABLE model_deployments ADD COLUMN runtime_updated_at TEXT")
            if "required_vram_mib" not in deployment_columns:
                db.execute("ALTER TABLE model_deployments ADD COLUMN required_vram_mib INTEGER NOT NULL DEFAULT 0")
            if "gpu_sharing_mode" not in deployment_columns:
                db.execute("ALTER TABLE model_deployments ADD COLUMN gpu_sharing_mode TEXT NOT NULL DEFAULT 'exclusive'")
            if "external_reserve_mib" not in deployment_columns:
                db.execute("ALTER TABLE model_deployments ADD COLUMN external_reserve_mib INTEGER NOT NULL DEFAULT 8192")
            if "current_config_revision" not in deployment_columns:
                db.execute("ALTER TABLE model_deployments ADD COLUMN current_config_revision INTEGER")
            if "pending_config_revision" not in deployment_columns:
                db.execute("ALTER TABLE model_deployments ADD COLUMN pending_config_revision INTEGER")
            if "service_desired_state" not in deployment_columns:
                db.execute("ALTER TABLE model_deployments ADD COLUMN service_desired_state TEXT NOT NULL DEFAULT 'stopped'")
            if "service_observed_state" not in deployment_columns:
                db.execute("ALTER TABLE model_deployments ADD COLUMN service_observed_state TEXT NOT NULL DEFAULT 'stopped'")
            db.execute(
                """UPDATE model_deployments
                   SET actual_state='unloaded', runtime_updated_at=COALESCE(runtime_updated_at, updated_at)
                   WHERE actual_state IN ('loading','loaded','unloading')"""
            )
            for kind, (name, description, timeout) in SERVICE_DEFAULTS.items():
                db.execute(
                    "INSERT OR IGNORE INTO services(kind,name,description,enabled,timeout_seconds) VALUES(?,?,?,?,?)",
                    (kind.value, name, description, 1, timeout),
                )

    def list_services(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM services ORDER BY kind").fetchall()
        return [dict(row) for row in rows]

    def get_service(self, kind: ServiceKind) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM services WHERE kind=?", (kind.value,)).fetchone()
        return dict(row) if row else None

    def update_service(self, kind: ServiceKind, values: dict[str, Any]) -> dict[str, Any]:
        columns = sorted({"enabled", "timeout_seconds"} & values.keys())
        if columns:
            assignments = ", ".join(f"{column}=?" for column in columns)
            params = [int(values[column]) if column == "enabled" else values[column] for column in columns]
            with self._connect() as db:
                db.execute(f"UPDATE services SET {assignments} WHERE kind=?", (*params, kind.value))
        service = self.get_service(kind)
        if service is None:
            raise KeyError(kind.value)
        return service

    def deployment_count(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM model_deployments").fetchone()[0])

    def insert_deployment(self, deployment: dict[str, Any], *, owner: InstallationOwner | None = None) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if owner is not None:
                self._assert_installation_owner(db, owner, INSTALL_ACTIVE)
            if deployment["is_default"]:
                db.execute("UPDATE model_deployments SET is_default=0 WHERE kind=?",
                           (deployment["kind"],))
            db.execute(
                """INSERT INTO model_deployments(
                       id,asset_id,catalog_key,kind,label,model_id,revision,manifest_digest,enabled,is_default,
                       gpu_indices_json,model_path,license,required_files_json,
                       required_vram_mib,gpu_sharing_mode,external_reserve_mib,
                       warm_ttl_seconds,desired_state,startup_policy,
                       actual_state,runtime_last_error,runtime_updated_at,install_state,last_error,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (deployment["id"], deployment.get("asset_id"), deployment["catalog_key"], deployment["kind"],
                 deployment["label"], deployment["model_id"], deployment["revision"],
                 deployment["manifest_digest"],
                 int(deployment["enabled"]), int(deployment["is_default"]),
                 json.dumps(deployment["gpu_indices"]), deployment["model_path"],
                 deployment["license"], json.dumps(deployment["required_files"]),
                 int(deployment["required_vram_mib"]),
                 deployment.get("gpu_sharing_mode", "exclusive"),
                 int(deployment.get("external_reserve_mib", 8192)),
                 int(deployment.get("warm_ttl_seconds", 0)),
                 deployment.get("desired_state", "unloaded"), deployment.get("startup_policy", "manual"),
                 deployment.get("actual_state", "unloaded"), deployment.get("runtime_last_error"),
                 deployment.get("runtime_updated_at", deployment["updated_at"]), deployment["install_state"],
                 deployment.get("last_error"), deployment["created_at"], deployment["updated_at"]),
            )
            for dependency in deployment.get("dependencies", []):
                db.execute(
                    """INSERT INTO model_deployment_dependencies(
                           deployment_id,dependency_key,dependency_deployment_id,
                           dependency_asset_id,dependency_revision,created_at
                       ) VALUES(?,?,?,?,?,?)""",
                    (deployment["id"], dependency["dependency_key"],
                     dependency["deployment_id"], dependency["asset_id"],
                     dependency["revision"], deployment["created_at"]),
                )
            incarnation = secrets.token_hex(16)
            db.execute("UPDATE model_deployments SET incarnation=? WHERE id=?", (incarnation, deployment["id"]))
            if owner is not None:
                self._record_installation_resource(db, owner, "deployment", deployment["id"],
                                                   incarnation, True, deployment["created_at"])
                db.execute("UPDATE service_installations SET deployment_id=? WHERE id=?",
                           (deployment["id"], owner[0]))

    def insert_user_deployment(self, deployment: dict[str, Any],
                               revision: dict[str, Any]) -> dict[str, Any]:
        """Atomically create the user instance and its first immutable configuration."""
        incarnation = secrets.token_hex(16)
        if revision["deployment_id"] != deployment["id"] or revision["config_revision"] != 1:
            raise ValueError("deployment_initial_revision_invalid")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if deployment["is_default"]:
                db.execute("UPDATE model_deployments SET is_default=0 WHERE kind=?",
                           (deployment["kind"],))
            db.execute(
                """INSERT INTO model_deployments(
                       id,asset_id,catalog_key,kind,label,model_id,revision,manifest_digest,
                       enabled,is_default,gpu_indices_json,model_path,license,required_files_json,
                       required_vram_mib,gpu_sharing_mode,external_reserve_mib,
                       warm_ttl_seconds,desired_state,startup_policy,actual_state,
                       current_config_revision,pending_config_revision,
                       service_desired_state,service_observed_state,runtime_last_error,
                       runtime_updated_at,install_state,last_error,created_at,updated_at,incarnation
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (deployment["id"], deployment["asset_id"], deployment["catalog_key"],
                 deployment["kind"], deployment["label"], deployment["model_id"],
                 deployment["revision"], deployment["manifest_digest"],
                 int(deployment["enabled"]), int(deployment["is_default"]),
                 json.dumps(deployment["gpu_indices"]), deployment["model_path"],
                 deployment["license"], json.dumps(deployment["required_files"]),
                 deployment["required_vram_mib"], deployment["gpu_sharing_mode"],
                 deployment["external_reserve_mib"], deployment["warm_ttl_seconds"],
                 deployment["desired_state"], deployment["startup_policy"],
                 deployment["actual_state"], 1, None, "stopped", "created", None,
                 deployment["runtime_updated_at"], deployment["install_state"], None,
                 deployment["created_at"], deployment["updated_at"], incarnation),
            )
            db.execute(
                """INSERT INTO model_deployment_revisions(
                       deployment_id,config_revision,runtime_profile_id,
                       runtime_profile_revision,runtime_profile_digest,runtime_image_digest,
                       base_asset_id,base_asset_revision,base_asset_manifest_digest,
                       vae_asset_id,vae_asset_revision,vae_asset_manifest_digest,
                       gpu_uuids_json,required_vram_mib,sharing_mode,residency,
                       external_reserve_mib,idle_seconds,
                       license_confirmation_json,experimental_compatibility_accepted,
                       desired_state,config_digest,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (revision["deployment_id"], 1, revision["runtime_profile_id"],
                 revision["runtime_profile_revision"], revision["runtime_profile_digest"],
                 revision["runtime_image_digest"], revision["base_asset_id"],
                 revision["base_asset_revision"], revision["base_asset_manifest_digest"],
                 revision.get("vae_asset_id"), revision.get("vae_asset_revision"),
                 revision.get("vae_asset_manifest_digest"),
                 json.dumps(revision["gpu_uuids"], separators=(",", ":")),
                 revision["required_vram_mib"], revision["sharing_mode"],
                 revision["residency"], revision["external_reserve_mib"],
                 revision["idle_seconds"], json.dumps(revision["license_confirmation"],
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False),
                 int(revision["experimental_compatibility_accepted"]),
                 revision["desired_state"], revision["config_digest"], revision["created_at"]),
            )
        return self.get_deployment(deployment["id"]) or {}

    def get_deployment(self, deployment_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM model_deployments WHERE id=?", (deployment_id,)).fetchone()
            dependencies = self._deployment_dependencies(db, [deployment_id])
        return self._deployment(row, dependencies.get(deployment_id, [])) if row else None

    def list_deployments(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM model_deployments ORDER BY kind,is_default DESC,id").fetchall()
            dependencies = self._deployment_dependencies(db, [row["id"] for row in rows])
        return [self._deployment(row, dependencies.get(row["id"], [])) for row in rows]

    def set_deployment_dependencies(self, deployment_id: str,
                                    dependencies: list[dict[str, Any]],
                                    manifest_digest: str, updated_at: str) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM model_deployments WHERE id=?", (deployment_id,)).fetchone() is None:
                raise KeyError(deployment_id)
            db.execute("DELETE FROM model_deployment_dependencies WHERE deployment_id=?",
                       (deployment_id,))
            for dependency in dependencies:
                db.execute(
                    """INSERT INTO model_deployment_dependencies(
                           deployment_id,dependency_key,dependency_deployment_id,
                           dependency_asset_id,dependency_revision,created_at
                       ) VALUES(?,?,?,?,?,?)""",
                    (deployment_id, dependency["dependency_key"], dependency["deployment_id"],
                     dependency["asset_id"], dependency["revision"], updated_at),
                )
            db.execute("UPDATE model_deployments SET manifest_digest=?,updated_at=? WHERE id=?",
                       (manifest_digest, updated_at, deployment_id))

    def deployment_dependency_references(self, deployment_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT deployment_id,dependency_key,dependency_asset_id,dependency_revision
                   FROM model_deployment_dependencies
                   WHERE dependency_deployment_id=? ORDER BY deployment_id,dependency_key""",
                (deployment_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_deployment(self, deployment_id: str, values: dict[str, Any], *,
                          incarnation: str | None = None, owner: InstallationOwner | None = None) -> dict[str, Any] | None:
        allowed = {"label", "enabled", "is_default", "gpu_indices", "install_state", "last_error",
                   "updated_at", "license", "required_files",
                   "manifest_digest", "warm_ttl_seconds", "desired_state", "startup_policy",
                   "actual_state", "runtime_last_error", "runtime_updated_at",
                   "required_vram_mib", "gpu_sharing_mode", "external_reserve_mib"}
        columns = sorted(set(values) & allowed)
        if not columns:
            return self.get_deployment(deployment_id)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if owner is not None:
                self._assert_installation_owner(db, owner, INSTALL_ACTIVE)
            current = db.execute("SELECT kind,incarnation FROM model_deployments WHERE id=?",
                                 (deployment_id,)).fetchone()
            if current is None:
                return None
            if (set(columns) - {'label', 'is_default', 'updated_at'}
                    and self.active_configuration_context_tx(db, deployment_id) is not None):
                from .task_state import TaskStateError
                raise TaskStateError('deployment_configuration_pending')
            if set(columns) - {'label', 'updated_at'}:
                self.assert_not_removing_tx(db, deployment_id)
            runtime_fields = {"enabled", "gpu_indices", "module", "manifest_digest", "warm_ttl_seconds",
                              "required_vram_mib", "gpu_sharing_mode", "external_reserve_mib", "startup_policy"}
            resource_fields = runtime_fields - {"enabled"}
            if resource_fields.intersection(columns) and db.execute("SELECT 1 FROM instance_policies WHERE instance_id=? AND policy_json!='null'", (deployment_id,)).fetchone():
                from .task_state import TaskStateError
                raise TaskStateError("use_versioned_instance_policy")
            if runtime_fields.intersection(columns) and db.execute("SELECT 1 FROM instance_claims WHERE instance_id=? AND state!='exited'", (deployment_id,)).fetchone():
                from .task_state import TaskStateError
                raise TaskStateError("unload_and_confirm_exit_before_configuration")
            if owner is not None:
                self._assert_deployment_owner(db, owner, deployment_id, current["incarnation"])
            if incarnation is not None and db.execute(
                    "SELECT 1 FROM model_deployments WHERE id=? AND incarnation=?",
                    (deployment_id, incarnation)).fetchone() is None:
                raise InstallationOwnershipError("deployment_identity_changed")
            if values.get("is_default"):
                db.execute("UPDATE model_deployments SET is_default=0 WHERE kind=?",
                           (current["kind"],))
            mapped = {"gpu_indices": "gpu_indices_json", "required_files": "required_files_json"}
            assignments = ", ".join(f"{mapped.get(column, column)}=?" for column in columns)
            params: list[Any] = []
            for column in columns:
                value = values[column]
                if column in {"enabled", "is_default"}:
                    value = int(value)
                elif column in {"gpu_indices", "required_files"}:
                    value = json.dumps(value)
                params.append(value)
            db.execute(f"UPDATE model_deployments SET {assignments} WHERE id=?",
                       (*params, deployment_id))
            if values.get("enabled") is False:
                db.execute("UPDATE instance_policies SET desired_state='unloaded',revision=revision+1,version=version+1 WHERE instance_id=? AND desired_state!='unloaded'", (deployment_id,))
        return self.get_deployment(deployment_id)

    def delete_deployment_if_unloaded(self, deployment_id: str, *, owner: InstallationOwner | None = None) -> bool:
        # This is installation rollback, never an administrative delete-by-name.
        if owner is None:
            return False
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_installation_owner(db, owner, INSTALL_ACTIVE | {"failed"})
            owned = db.execute(
                """SELECT r.identity FROM installation_resources r JOIN model_deployments d
                   ON d.id=r.resource_id AND d.incarnation=r.identity
                   WHERE r.attempt_id=? AND r.kind='deployment' AND r.resource_id=? AND r.created=1""",
                (owner[1], deployment_id)).fetchone()
            if owned is None:
                return False
            if db.execute("SELECT 1 FROM instance_claims WHERE instance_id=? AND state!='exited'", (deployment_id,)).fetchone():
                return False
            if db.execute("""SELECT 1 FROM installation_resources WHERE kind='deployment'
                           AND resource_id=? AND identity=? AND attempt_id!=?""",
                          (deployment_id, owned["identity"], owner[1])).fetchone():
                return False
            if db.execute(
                    "SELECT 1 FROM model_deployment_dependencies WHERE dependency_deployment_id=? LIMIT 1",
                    (deployment_id,)).fetchone() is not None:
                return False
            active = int(db.execute(
                "SELECT COUNT(*) FROM tasks WHERE model_key=? AND status IN ('queued','assigned','running','cancel_requested')",
                (deployment_id,),
            ).fetchone()[0])
            if active:
                return False
            return db.execute(
                """DELETE FROM model_deployments
                   WHERE id=? AND desired_state='unloaded'
                     AND actual_state IN ('unloaded','error')""",
                (deployment_id,),
            ).rowcount == 1

    def assert_service_uninstallable(self, deployment_id: str, incarnation: str) -> None:
        """Read-only preflight; the final uninstall repeats every condition atomically."""
        with self._connect() as db:
            deployment = db.execute(
                "SELECT * FROM model_deployments WHERE id=?", (deployment_id,),
            ).fetchone()
            if deployment is None:
                raise InstallationOwnershipError("service_not_installed")
            if deployment["incarnation"] != incarnation:
                raise InstallationOwnershipError("deployment_identity_changed")
            binding = db.execute(
                "SELECT * FROM instance_installation_bindings WHERE instance_id=?",
                (deployment_id,),
            ).fetchone()
            if binding is None:
                raise InstallationOwnershipError("service_not_installed")
            if binding["incarnation"] != incarnation:
                raise InstallationOwnershipError("deployment_identity_changed")
            installation = db.execute(
                "SELECT state FROM service_installations WHERE id=?",
                (binding["operation_id"],),
            ).fetchone()
            if installation is None or installation["state"] != "ready":
                raise InstallationOwnershipError("service_installation_busy")
            if db.execute(
                    """SELECT 1 FROM tasks WHERE model_key=?
                       AND status IN ('queued','assigned','running','cancel_requested') LIMIT 1""",
                    (deployment_id,),
            ).fetchone():
                raise InstallationOwnershipError("service_tasks_active")
            if db.execute(
                    """SELECT 1 FROM model_deployment_dependencies d
                       JOIN instance_installation_bindings b ON b.instance_id=d.deployment_id
                       WHERE d.dependency_deployment_id=? LIMIT 1""",
                    (deployment_id,),
            ).fetchone():
                raise InstallationOwnershipError("service_dependency_in_use")
            policy = db.execute(
                "SELECT desired_state,configuration_state FROM instance_policies WHERE instance_id=?",
                (deployment_id,),
            ).fetchone()
            has_active_runtime = any((
                db.execute(
                    "SELECT 1 FROM instance_claims WHERE instance_id=? AND state!='exited' LIMIT 1",
                    (deployment_id,),
                ).fetchone(),
                db.execute(
                    "SELECT 1 FROM runtime_intents WHERE instance_id=? AND state!='exited' LIMIT 1",
                    (deployment_id,),
                ).fetchone(),
                db.execute(
                    "SELECT 1 FROM task_attempts WHERE instance_id=? AND exit_confirmed=0 LIMIT 1",
                    (deployment_id,),
                ).fetchone(),
            ))
            if (has_active_runtime
                    or deployment["enabled"]
                    or deployment["desired_state"] != "unloaded"
                    or deployment["actual_state"] not in {"unloaded", "error"}
                    or policy is not None and (policy["desired_state"] != "unloaded"
                                               or policy["configuration_state"] != "applied")):
                raise InstallationOwnershipError("service_runtime_active")

    def uninstall_service_binding(self, deployment_id: str, incarnation: str, now: str,
                                  *, _connection=None, _removal_operation_id=None) -> None:
        """Detach one installed service without deleting shared assets or cached images."""
        from contextlib import nullcontext
        with (nullcontext(_connection) if _connection is not None else self._connect()) as db:
            if _connection is None:
                db.execute("BEGIN IMMEDIATE")
            self.assert_not_removing_tx(db, deployment_id, owner=_removal_operation_id)
            if self.active_configuration_context_tx(db, deployment_id) is not None:
                raise InstallationOwnershipError('deployment_configuration_pending')
            deployment = db.execute(
                "SELECT * FROM model_deployments WHERE id=?", (deployment_id,),
            ).fetchone()
            if deployment is None:
                raise InstallationOwnershipError("service_not_installed")
            if deployment["incarnation"] != incarnation:
                raise InstallationOwnershipError("deployment_identity_changed")
            binding = db.execute(
                "SELECT * FROM instance_installation_bindings WHERE instance_id=?",
                (deployment_id,),
            ).fetchone()
            if binding is None:
                raise InstallationOwnershipError("service_not_installed")
            if binding["incarnation"] != incarnation:
                raise InstallationOwnershipError("deployment_identity_changed")
            installation = db.execute(
                "SELECT state FROM service_installations WHERE id=?",
                (binding["operation_id"],),
            ).fetchone()
            if installation is None or installation["state"] != "ready":
                raise InstallationOwnershipError("service_installation_busy")
            if db.execute(
                    """SELECT 1 FROM tasks WHERE model_key=?
                       AND status IN ('queued','assigned','running','cancel_requested') LIMIT 1""",
                    (deployment_id,),
            ).fetchone():
                raise InstallationOwnershipError("service_tasks_active")
            if db.execute(
                    """SELECT 1 FROM model_deployment_dependencies d
                       JOIN instance_installation_bindings b ON b.instance_id=d.deployment_id
                       WHERE d.dependency_deployment_id=? LIMIT 1""",
                    (deployment_id,),
            ).fetchone():
                raise InstallationOwnershipError("service_dependency_in_use")
            policy = db.execute(
                "SELECT desired_state,configuration_state FROM instance_policies WHERE instance_id=?",
                (deployment_id,),
            ).fetchone()
            has_active_runtime = any((
                db.execute(
                    "SELECT 1 FROM instance_claims WHERE instance_id=? AND state!='exited' LIMIT 1",
                    (deployment_id,),
                ).fetchone(),
                db.execute(
                    "SELECT 1 FROM runtime_intents WHERE instance_id=? AND state!='exited' LIMIT 1",
                    (deployment_id,),
                ).fetchone(),
                db.execute(
                    "SELECT 1 FROM task_attempts WHERE instance_id=? AND exit_confirmed=0 LIMIT 1",
                    (deployment_id,),
                ).fetchone(),
            ))
            if (has_active_runtime
                    or deployment["enabled"]
                    or deployment["desired_state"] != "unloaded"
                    or deployment["actual_state"] not in {"unloaded", "error"}
                    or policy is not None and (policy["desired_state"] != "unloaded"
                                               or policy["configuration_state"] != "applied")):
                raise InstallationOwnershipError("service_runtime_active")

            db.execute(
                "DELETE FROM instance_installation_bindings WHERE instance_id=? AND incarnation=?",
                (deployment_id, incarnation),
            )
            db.execute(
                """UPDATE instance_policies
                   SET desired_state='unloaded',status='waiting_runtime',
                       error_code='service_uninstalled',restart_recovery=0,
                       revision=revision+1,version=version+1
                   WHERE instance_id=?""",
                (deployment_id,),
            )
            changed = db.execute(
                """UPDATE model_deployments
                   SET enabled=0,is_default=0,install_state='configured',
                       desired_state='unloaded',actual_state='unloaded',
                       runtime_last_error=NULL,last_error=NULL,updated_at=?
                   WHERE id=? AND incarnation=?""",
                (now, deployment_id, incarnation),
            ).rowcount
            if changed != 1:
                raise InstallationOwnershipError("deployment_identity_changed")

    def put_runtime_profile(self, profile: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT * FROM runtime_profiles WHERE profile_id=? AND revision=?",
                (profile["profile_id"], profile["revision"]),
            ).fetchone()
            if current is not None:
                if current["profile_digest"] != profile["profile_digest"]:
                    raise ValueError("runtime_profile_revision_conflict")
                return "existing", self._runtime_profile(current)
            db.execute(
                """INSERT INTO runtime_profiles(
                       profile_id,revision,label,image_digest,worker_protocol,
                       architecture_families_json,main_formats_json,
                       optional_deployment_roles_json,task_roles_json,loader,
                       trust_remote_code,residency_modes_json,required_vram_mib,
                       profile_digest,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (profile["profile_id"], profile["revision"], profile["label"],
                 profile["image_digest"], profile["worker_protocol"],
                 json.dumps(profile["architecture_families"], separators=(",", ":")),
                 json.dumps(profile["main_formats"], separators=(",", ":")),
                 json.dumps(profile["optional_deployment_roles"], separators=(",", ":")),
                 json.dumps(profile["task_roles"], separators=(",", ":")),
                 profile["loader"], 0,
                 json.dumps(profile["residency_modes"], separators=(",", ":")),
                 profile["required_vram_mib"], profile["profile_digest"],
                 profile["created_at"]),
            )
        return "created", self.get_runtime_profile(profile["profile_id"], profile["revision"]) or {}

    def get_runtime_profile(self, profile_id: str, revision: int) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM runtime_profiles WHERE profile_id=? AND revision=?",
                (profile_id, revision),
            ).fetchone()
        return self._runtime_profile(row) if row is not None else None

    def list_runtime_profiles(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM runtime_profiles ORDER BY profile_id,revision DESC"
            ).fetchall()
        return [self._runtime_profile(row) for row in rows]

    def put_model_deployment_revision(self, revision: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        key = (revision["deployment_id"], revision["config_revision"])
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                """SELECT * FROM model_deployment_revisions
                   WHERE deployment_id=? AND config_revision=?""", key,
            ).fetchone()
            if current is not None:
                if current["config_digest"] != revision["config_digest"]:
                    raise ValueError("deployment_config_revision_conflict")
                return "existing", self._model_deployment_revision(current)
            db.execute(
                """INSERT INTO model_deployment_revisions(
                       deployment_id,config_revision,runtime_profile_id,
                       runtime_profile_revision,runtime_profile_digest,runtime_image_digest,
                       base_asset_id,base_asset_revision,base_asset_manifest_digest,
                       vae_asset_id,vae_asset_revision,vae_asset_manifest_digest,
                       gpu_uuids_json,required_vram_mib,sharing_mode,residency,
                       external_reserve_mib,idle_seconds,
                       license_confirmation_json,experimental_compatibility_accepted,
                       desired_state,config_digest,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (revision["deployment_id"], revision["config_revision"],
                 revision["runtime_profile_id"], revision["runtime_profile_revision"],
                 revision["runtime_profile_digest"], revision["runtime_image_digest"],
                 revision["base_asset_id"], revision["base_asset_revision"],
                 revision["base_asset_manifest_digest"], revision.get("vae_asset_id"),
                 revision.get("vae_asset_revision"), revision.get("vae_asset_manifest_digest"),
                 json.dumps(revision["gpu_uuids"], separators=(",", ":")),
                 revision["required_vram_mib"], revision["sharing_mode"], revision["residency"],
                 revision["external_reserve_mib"], revision["idle_seconds"],
                 json.dumps(revision["license_confirmation"], ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False),
                 int(revision["experimental_compatibility_accepted"]),
                 revision["desired_state"], revision["config_digest"], revision["created_at"]),
            )
        return "created", self.get_model_deployment_revision(*key) or {}

    @staticmethod
    def _insert_model_deployment_revision_tx(db: sqlite3.Connection,
                                             revision: dict[str, Any]) -> None:
        db.execute(
            """INSERT INTO model_deployment_revisions(
                   deployment_id,config_revision,runtime_profile_id,
                   runtime_profile_revision,runtime_profile_digest,runtime_image_digest,
                   base_asset_id,base_asset_revision,base_asset_manifest_digest,
                   vae_asset_id,vae_asset_revision,vae_asset_manifest_digest,
                   gpu_uuids_json,required_vram_mib,sharing_mode,residency,
                   external_reserve_mib,idle_seconds,
                   license_confirmation_json,experimental_compatibility_accepted,
                   desired_state,config_digest,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (revision["deployment_id"], revision["config_revision"],
             revision["runtime_profile_id"], revision["runtime_profile_revision"],
             revision["runtime_profile_digest"], revision["runtime_image_digest"],
             revision["base_asset_id"], revision["base_asset_revision"],
             revision["base_asset_manifest_digest"], revision.get("vae_asset_id"),
             revision.get("vae_asset_revision"), revision.get("vae_asset_manifest_digest"),
             json.dumps(revision["gpu_uuids"], separators=(",", ":")),
             revision["required_vram_mib"], revision["sharing_mode"], revision["residency"],
             revision["external_reserve_mib"], revision["idle_seconds"],
             json.dumps(revision["license_confirmation"], ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"), allow_nan=False),
             int(revision["experimental_compatibility_accepted"]), revision["desired_state"],
             revision["config_digest"], revision["created_at"]),
        )

    def stage_model_deployment_revision_tx(self, db: sqlite3.Connection,
                                           revision: dict[str, Any],
                                           *, expected_current: int) -> None:
        """Insert one immutable pending revision inside the caller's transaction."""
        from .model_deployments import deployment_config_digest
        self.assert_not_removing_tx(db, revision["deployment_id"])
        deployment = db.execute(
            """SELECT current_config_revision,pending_config_revision
               FROM model_deployments WHERE id=?""",
            (revision["deployment_id"],),
        ).fetchone()
        if deployment is None:
            raise ValueError("deployment_not_found")
        # Failed candidates remain immutable history. The current (healthy)
        # head can move backwards on rollback, but revision identities cannot.
        next_revision = db.execute(
            "SELECT COALESCE(MAX(config_revision),0)+1 FROM model_deployment_revisions "
            "WHERE deployment_id=?", (revision["deployment_id"],),
        ).fetchone()[0]
        if (deployment["current_config_revision"] != expected_current
                or deployment["pending_config_revision"] is not None
                or type(revision["config_revision"]) is not int
                or revision["config_revision"] != next_revision):
            raise ValueError("deployment_config_revision_conflict")
        if revision["config_digest"] != deployment_config_digest(revision):
            raise ValueError("deployment_config_digest_invalid")
        self._insert_model_deployment_revision_tx(db, revision)
        if db.execute(
                """UPDATE model_deployments
                   SET pending_config_revision=?,updated_at=?
                   WHERE id=? AND current_config_revision=?
                     AND pending_config_revision IS NULL""",
                (revision["config_revision"], revision["created_at"],
                 revision["deployment_id"], expected_current),
        ).rowcount != 1:
            raise ValueError("deployment_config_revision_conflict")

    @staticmethod
    def commit_model_deployment_revision_tx(db: sqlite3.Connection,
                                            deployment_id: str, updated_at: str) -> int | None:
        deployment = db.execute(
            """SELECT current_config_revision,pending_config_revision
               FROM model_deployments WHERE id=?""", (deployment_id,),
        ).fetchone()
        if deployment is None:
            raise ValueError("deployment_not_found")
        pending = deployment["pending_config_revision"]
        if pending is None:
            return None
        revision = db.execute(
            """SELECT * FROM model_deployment_revisions
               WHERE deployment_id=? AND config_revision=?""",
            (deployment_id, pending),
        ).fetchone()
        if revision is None:
            raise ValueError("deployment_config_revision_not_found")
        if db.execute(
                """UPDATE model_deployments
                   SET current_config_revision=?,pending_config_revision=NULL,
                       required_vram_mib=?,gpu_sharing_mode=?,external_reserve_mib=?,
                       updated_at=?
                   WHERE id=? AND current_config_revision=? AND pending_config_revision=?""",
                (pending, revision["required_vram_mib"], revision["sharing_mode"],
                 revision["external_reserve_mib"], updated_at,
                 deployment_id, deployment["current_config_revision"], pending),
        ).rowcount != 1:
            raise ValueError("deployment_config_revision_conflict")
        return int(pending)

    @staticmethod
    def rollback_model_deployment_revision_tx(db: sqlite3.Connection,
                                              deployment_id: str, updated_at: str) -> int | None:
        deployment = db.execute(
            "SELECT pending_config_revision FROM model_deployments WHERE id=?",
            (deployment_id,),
        ).fetchone()
        if deployment is None:
            raise ValueError("deployment_not_found")
        pending = deployment["pending_config_revision"]
        if pending is None:
            return None
        if db.execute(
                """UPDATE model_deployments SET pending_config_revision=NULL,updated_at=?
                   WHERE id=? AND pending_config_revision=?""",
                (updated_at, deployment_id, pending),
        ).rowcount != 1:
            raise ValueError("deployment_config_revision_conflict")
        return int(pending)

    def get_model_deployment_revision(self, deployment_id: str,
                                      config_revision: int) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT * FROM model_deployment_revisions
                   WHERE deployment_id=? AND config_revision=?""",
                (deployment_id, config_revision),
            ).fetchone()
        return self._model_deployment_revision(row) if row is not None else None

    def list_model_deployment_revisions(self, deployment_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM model_deployment_revisions WHERE deployment_id=?
                   ORDER BY config_revision DESC""", (deployment_id,),
            ).fetchall()
        return [self._model_deployment_revision(row) for row in rows]

    def next_model_deployment_revision(self, deployment_id: str) -> int:
        """Read-only proposal; stage performs the atomic sequence/CAS check."""
        with self._connect() as db:
            return int(db.execute(
                "SELECT COALESCE(MAX(config_revision),0)+1 FROM model_deployment_revisions "
                "WHERE deployment_id=?", (deployment_id,),
            ).fetchone()[0])

    def set_model_deployment_revision_head(self, deployment_id: str, config_revision: int,
                                           *, expected_current: int | None,
                                           pending: bool, updated_at: str) -> bool:
        column = "pending_config_revision" if pending else "current_config_revision"
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                    """SELECT 1 FROM model_deployment_revisions
                       WHERE deployment_id=? AND config_revision=?""",
                    (deployment_id, config_revision)).fetchone() is None:
                raise ValueError("deployment_config_revision_not_found")
            condition = "current_config_revision IS NULL" if expected_current is None else "current_config_revision=?"
            params: list[Any] = [config_revision, updated_at, deployment_id]
            if expected_current is not None:
                params.append(expected_current)
            return db.execute(
                f"UPDATE model_deployments SET {column}=?,updated_at=? WHERE id=? AND {condition}",
                params,
            ).rowcount == 1

    def set_model_asset_metadata(self, asset_id: str, revision: str,
                                 metadata: dict[str, Any], updated_at: str) -> dict[str, Any]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM model_assets WHERE id=?", (asset_id,)).fetchone()
            if row is None:
                raise KeyError(asset_id)
            if row["revision"] != revision:
                raise ValueError("model_asset_revision_mismatch")
            if row["state"] not in {"verifying", "ready"}:
                raise ValueError("model_asset_not_metadata_writable")
            existing_digest = row["metadata_digest"]
            if existing_digest is not None and existing_digest != metadata["metadata_digest"]:
                raise ValueError("model_asset_metadata_immutable")
            db.execute(
                """UPDATE model_assets
                   SET architecture_family=?,tensor_precision=?,parameter_summary_json=?,
                       metadata_json=?,detector_version=?,metadata_digest=?,updated_at=?
                   WHERE id=? AND revision=?""",
                (metadata["architecture_family"], metadata.get("tensor_precision"),
                 json.dumps(metadata["parameter_summary"], ensure_ascii=False,
                            sort_keys=True, separators=(",", ":"), allow_nan=False),
                 json.dumps(metadata["metadata"], ensure_ascii=False,
                            sort_keys=True, separators=(",", ":"), allow_nan=False),
                 metadata["detector_version"], metadata["metadata_digest"],
                 updated_at, asset_id, revision),
            )
        return self.get_model_asset(asset_id) or {}

    def put_asset_compatibility(self, compatibility: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        key = (compatibility["subject_asset_id"], compatibility["subject_revision"],
               compatibility["base_asset_id"], compatibility["base_revision"],
               compatibility["detector_version"])
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                """SELECT * FROM asset_compatibility
                   WHERE subject_asset_id=? AND subject_revision=? AND base_asset_id=?
                     AND base_revision=? AND detector_version=?""", key,
            ).fetchone()
            if current is not None:
                if current["evidence_digest"] != compatibility["evidence_digest"]:
                    raise ValueError("asset_compatibility_conflict")
                return "existing", self._asset_compatibility(current)
            db.execute(
                """INSERT INTO asset_compatibility(
                       subject_asset_id,subject_revision,base_asset_id,base_revision,
                       detector_version,verdict,reason_codes_json,evidence_json,
                       evidence_digest,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (*key, compatibility["verdict"],
                 json.dumps(compatibility["reason_codes"], separators=(",", ":")),
                 json.dumps(compatibility["evidence"], ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False),
                 compatibility["evidence_digest"], compatibility["created_at"]),
            )
        return "created", self.get_asset_compatibility(*key) or {}

    def get_asset_compatibility(self, subject_asset_id: str, subject_revision: str,
                                base_asset_id: str, base_revision: str,
                                detector_version: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT * FROM asset_compatibility
                   WHERE subject_asset_id=? AND subject_revision=? AND base_asset_id=?
                     AND base_revision=? AND detector_version=?""",
                (subject_asset_id, subject_revision, base_asset_id, base_revision,
                 detector_version),
            ).fetchone()
        return self._asset_compatibility(row) if row is not None else None

    def list_asset_compatibility(self, *, subject_asset_id: str | None = None,
                                 base_asset_id: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if subject_asset_id is not None:
            clauses.append("subject_asset_id=?")
            params.append(subject_asset_id)
        if base_asset_id is not None:
            clauses.append("base_asset_id=?")
            params.append(base_asset_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as db:
            rows = db.execute(
                f"SELECT * FROM asset_compatibility{where} ORDER BY created_at DESC",
                params,
            ).fetchall()
        return [self._asset_compatibility(row) for row in rows]

    def create_deployment_operation(self, operation: dict[str, Any], *,
                                    configuration_plan=None) -> tuple[str, dict[str, Any]]:
        scope = (operation["server_profile_id"], operation["authenticated_principal"],
                 operation["route"], operation["idempotency_key"])
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                """SELECT * FROM deployment_operations
                   WHERE server_profile_id=? AND authenticated_principal=?
                     AND route=? AND idempotency_key=?""", scope,
            ).fetchone()
            if current is not None:
                if current["request_digest"] != operation["request_digest"]:
                    raise ValueError("idempotency_conflict")
                return "replayed", self._deployment_operation(current)
            db.execute(
                """INSERT INTO deployment_operations(
                       id,server_profile_id,authenticated_principal,route,idempotency_key,
                       request_digest,deployment_id,state,payload_json,plan_digest,
                       confirmations_json,milestone,recovery_cursor_json,error_class,
                       error_code,error_message,result_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (operation["id"], *scope, operation["request_digest"],
                 operation["deployment_id"], operation["state"],
                 json.dumps(operation["payload"], ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False),
                 operation["plan_digest"],
                 json.dumps(operation["confirmations"], ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False),
                 operation["milestone"],
                 json.dumps(operation.get("recovery_cursor"), ensure_ascii=False,
                            sort_keys=True, separators=(",", ":"), allow_nan=False)
                 if operation.get("recovery_cursor") is not None else None,
                 operation.get("error_class"), operation.get("error_code"),
                 operation.get("error_message"),
                 json.dumps(operation.get("result"), ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False)
                 if operation.get("result") is not None else None,
                 operation["created_at"], operation["updated_at"]),
            )
            if configuration_plan is not None:
                # Called only for a new command. A replay must retain its
                # original snapshot even after the head or policy has moved.
                plan = configuration_plan() if callable(configuration_plan) else configuration_plan
                self._admit_configuration_operation_tx(db, operation, plan)
            if operation["payload"].get("action") == "uninstall":
                self._admit_removal_tx(db, operation)
        return "created", self.get_deployment_operation(operation["id"]) or {}

    @staticmethod
    def assert_not_removing_tx(db, instance, *, owner=None):
        row = db.execute("SELECT removal_operation_id FROM model_deployments WHERE id=?", (instance,)).fetchone()
        if row and row[0] is not None and row[0] != owner:
            raise TaskStateError("deployment_removal_pending")

    def _removal_identity_tx(self, db, operation):
        instance, expected = operation["deployment_id"], operation["payload"]
        deployment = db.execute("SELECT * FROM model_deployments WHERE id=?", (instance,)).fetchone()
        policy = db.execute("SELECT * FROM instance_policies WHERE instance_id=?", (instance,)).fetchone()
        installed = db.execute("SELECT * FROM instance_installation_bindings WHERE instance_id=?", (instance,)).fetchone()
        head = db.execute("SELECT * FROM model_deployment_revisions WHERE deployment_id=? AND config_revision=?",
                          (instance, expected["config_revision"])).fetchone()
        if (deployment is None or policy is None or installed is None or head is None
                or deployment["catalog_key"] != "sdxl-single-file" or deployment["install_state"] != "ready"
                or deployment["incarnation"] != expected["incarnation"]
                or installed["incarnation"] != expected["incarnation"]
                or deployment["current_config_revision"] != expected["config_revision"]
                or head["config_digest"] != expected["config_digest"]):
            raise TaskStateError("deployment_removal_identity_changed")
        return deployment, policy

    @staticmethod
    def assert_removal_exit_recoverable_tx(db, instance, intent_id):
        row = db.execute("SELECT r.* FROM runtime_intents r JOIN model_deployments d ON d.id=r.instance_id "
                         "JOIN deployment_operations o ON o.id=d.removal_operation_id "
                         "WHERE r.instance_id=? AND r.intent_id=? AND o.deployment_id=d.id "
                         "AND json_extract(o.payload_json,'$.action')='uninstall'",
                         (instance, intent_id)).fetchone()
        if (row is None or row["state"] not in {"stop_pending", "exit_unconfirmed"}
                or not db.execute("SELECT 1 FROM runtime_effects WHERE token=? AND intent_id=? AND action='stop'",
                                  (row["effect_token"], intent_id)).fetchone()
                or db.execute("SELECT 1 FROM runtime_effects WHERE intent_id=? AND (action='start' "
                              "OR (action!='stop' AND (state LIKE '%pending' OR state LIKE '%unknown')))",
                              (intent_id,)).fetchone()
                or not db.execute("SELECT 1 FROM instance_claims WHERE instance_id=? AND epoch=? "
                                  "AND state='container_stopped'", (instance, row["epoch"])).fetchone()):
            raise TaskStateError("service_runtime_active")

    @staticmethod
    def _removal_idle_tx(db, instance, deployment, policy):
        if (deployment["enabled"] or deployment["desired_state"] != "unloaded"
                or deployment["actual_state"] not in {"unloaded", "error"}
                or deployment["pending_config_revision"] is not None
                or policy["desired_state"] != "unloaded" or policy["configuration_state"] != "applied"
                or policy["pending_policy_json"] is not None):
            raise TaskStateError("service_runtime_active")
        checks = (
            ("service_tasks_active", "SELECT 1 FROM tasks WHERE model_key=? AND status IN ('queued','assigned','running','cancel_requested')"),
            ("execution_exit_unconfirmed", "SELECT 1 FROM task_attempts WHERE instance_id=? AND exit_confirmed=0"),
            ("service_reservations_active", "SELECT 1 FROM task_reservations WHERE instance_id=? AND released=0"),
            ("service_dependency_in_use", "SELECT 1 FROM model_deployment_dependencies d JOIN instance_installation_bindings b ON b.instance_id=d.deployment_id WHERE d.dependency_deployment_id=?"),
            ("service_runtime_active", "SELECT 1 FROM instance_claims WHERE instance_id=? AND state NOT IN ('exited','container_stopped')"),
            ("service_model_operation_active", "SELECT 1 FROM model_operations m JOIN instance_claims c USING(claim_id) WHERE c.instance_id=? AND c.state!='exited' AND m.state IN ('pending','accepted')"),
        )
        for code, query in checks:
            if db.execute(query + " LIMIT 1", (instance,)).fetchone():
                raise TaskStateError(code)
        # Only our already-fenced, never-started stop confirmation may recover.
        # Neither an arbitrary unknown create/start nor another service's stop
        # is made eligible by a user pressing retry.
        for row in db.execute("SELECT intent_id,state FROM runtime_intents WHERE instance_id=?", (instance,)):
            if row["state"] not in {"exited", "prepared", "domain_ready", "created"}:
                Repository.assert_removal_exit_recoverable_tx(db, instance, row["intent_id"])
            elif db.execute("SELECT 1 FROM runtime_effects WHERE intent_id=? "
                            "AND (state LIKE '%pending' OR state LIKE '%unknown')", (row["intent_id"],)).fetchone():
                raise TaskStateError("service_runtime_effect_unconfirmed")

    def _admit_removal_tx(self, db, operation):
        instance, payload = operation["deployment_id"], operation["payload"]
        deployment, policy = self._removal_identity_tx(db, operation)
        if policy["version"] != payload["policy_version"]:
            raise TaskStateError("instance_version_conflict")
        previous = deployment["removal_operation_id"]
        if previous != payload["retry_of"]:
            raise TaskStateError("deployment_removal_pending")
        if previous is not None:
            old = db.execute("SELECT * FROM deployment_operations WHERE id=?", (previous,)).fetchone()
            if (old is None or old["state"] != "failed" or old["deployment_id"] != instance
                    or json.loads(old["payload_json"]).get("action") != "uninstall"):
                raise TaskStateError("deployment_removal_retry_invalid")
        self._removal_idle_tx(db, instance, deployment, policy)
        if self.active_configuration_context_tx(db, instance) is not None or db.execute(
                "SELECT 1 FROM deployment_operations WHERE deployment_id=? AND id!=? "
                "AND state NOT IN ('ready','failed','canceled')", (instance, operation["id"])).fetchone():
            raise TaskStateError("deployment_configuration_pending")
        binding = db.execute("SELECT s.state FROM service_installations s JOIN instance_installation_bindings b "
                             "ON b.operation_id=s.id WHERE b.instance_id=?", (instance,)).fetchone()
        if binding is None or binding[0] != "ready":
            raise TaskStateError("service_installation_busy")
        db.execute("UPDATE model_deployments SET removal_operation_id=?,updated_at=? WHERE id=?",
                   (operation["id"], operation["created_at"], instance))

    def check_removal_owner(self, operation):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_removal_owner_tx(db, operation)

    def _check_removal_owner_tx(self, db, operation):
        deployment, policy = self._removal_identity_tx(db, operation)
        current = db.execute("SELECT state FROM deployment_operations WHERE id=?", (operation["id"],)).fetchone()
        if (deployment["removal_operation_id"] != operation["id"] or current is None
                or current[0] != "accepted" or operation["payload"].get("action") != "uninstall"):
            raise TaskStateError("deployment_removal_owner_changed")
        return deployment, policy

    def finish_removal(self, operation, timestamp, *, error_code=None):
        """Commit the removal receipt and binding detach at one DB boundary."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            deployment, policy = self._check_removal_owner_tx(db, operation)
            instance = operation["deployment_id"]
            if error_code is None:
                self._removal_idle_tx(db, instance, deployment, policy)
                if db.execute("SELECT 1 FROM runtime_intents r LEFT JOIN runtime_container_removals m USING(intent_id) "
                              "WHERE r.instance_id=? AND (r.state!='exited' OR r.exit_evidence_json IS NULL "
                              "OR (r.container_id IS NOT NULL AND (m.state IS NULL OR m.state!='removed'))) LIMIT 1",
                              (instance,)).fetchone():
                    raise TaskStateError("container_removal_unconfirmed")
                self.uninstall_service_binding(instance, deployment["incarnation"], timestamp,
                                               _connection=db, _removal_operation_id=operation["id"])
                db.execute("UPDATE model_deployments SET removal_operation_id=NULL WHERE id=?", (instance,))
            result = json.dumps({"deployment_id": instance, "uninstalled": True, "assets_preserved": True})
            db.execute("UPDATE deployment_operations SET state=?,milestone=?,error_class=?,error_code=?,"
                       "error_message=?,result_json=?,updated_at=? WHERE id=?",
                       ("failed" if error_code else "ready", "removal_failed" if error_code else "uninstalled",
                        "recoverable" if error_code else None, error_code,
                        "容器移除尚未确认；实例保持锁定，可重试卸载。" if error_code else None,
                        None if error_code else result, timestamp, operation["id"]))
        return self.get_deployment_operation(operation["id"])

    CONFIGURATION_DEPLOYMENT_FIELDS = (
        "asset_id", "model_id", "revision", "license", "model_path",
        "required_files_json", "manifest_digest", "gpu_indices_json",
        "required_vram_mib", "gpu_sharing_mode", "external_reserve_mib",
    )

    def _admit_configuration_operation_tx(self, db, operation, plan):
        """Reserve one immutable candidate and its rollback base atomically.

        This is server-internal input, never an HTTP context supplied by a
        client. No Runtime/binding/desired-state change occurs at admission.
        """
        from .instance_policy import InstancePolicy
        from .worker_common import canonical, digest
        if (type(plan) is not dict or plan.get("operation") != operation["payload"]
                or not plan.get("effects", {}).get("updates_existing")):
            raise ValueError("deployment_configuration_plan_invalid")
        instance = operation["deployment_id"]
        self.assert_not_removing_tx(db, instance)
        expected = operation["payload"]["expected_configuration"]
        deployment = db.execute("SELECT * FROM model_deployments WHERE id=?", (instance,)).fetchone()
        policy = db.execute("SELECT * FROM instance_policies WHERE instance_id=?", (instance,)).fetchone()
        installed = db.execute("SELECT * FROM instance_installation_bindings WHERE instance_id=?", (instance,)).fetchone()
        head = db.execute("SELECT * FROM model_deployment_revisions WHERE deployment_id=? AND config_revision=?",
                          (instance, expected["config_revision"])).fetchone()
        if (deployment is None or policy is None or installed is None or head is None
                or deployment["catalog_key"] != "sdxl-single-file" or deployment["install_state"] != "ready"
                or deployment["current_config_revision"] != expected["config_revision"]
                or head["config_digest"] != expected["config_digest"]
                or policy["version"] != expected["policy_version"]
                or deployment["incarnation"] != installed["incarnation"]):
            raise ValueError("deployment_config_revision_conflict")
        current = InstancePolicy._policy(policy)
        if (deployment["pending_config_revision"] is not None
                or current["configuration_state"] != "applied" or current["pending_policy"] is not None):
            raise ValueError("deployment_configuration_pending")
        if (current["policy"] is None
                or current["policy"]["binding"] != InstancePolicy.deployment_binding(db, deployment)
                or digest(json.loads(installed["template_json"])) != installed["template_digest"]):
            raise ValueError("deployment_binding_mismatch")
        if db.execute("SELECT 1 FROM tasks WHERE model_key=? AND status='queued' LIMIT 1", (instance,)).fetchone():
            raise ValueError("deployment_queue_not_empty")
        if db.execute("SELECT 1 FROM deployment_operations WHERE deployment_id=? AND id!=? "
                      "AND state NOT IN ('ready','failed','canceled') LIMIT 1",
                      (instance, operation["id"])).fetchone():
            raise ValueError("deployment_configuration_pending")
        revision = plan["configuration_revision"]
        if revision["deployment_id"] != instance:
            raise ValueError("deployment_configuration_plan_invalid")
        self.stage_model_deployment_revision_tx(db, revision, expected_current=expected["config_revision"])
        context = {
            "schema": 1, "operation_id": operation["id"], "instance_id": instance,
            "incarnation": deployment["incarnation"], "phase": "admitted",
            "expected_configuration": dict(expected), "revision": revision,
            "previous": {
                "installation": dict(installed),
                "deployment": {key: deployment[key] for key in self.CONFIGURATION_DEPLOYMENT_FIELDS},
                "policy": current["policy"], "restart_recovery": current["restart_recovery"],
                "enabled": bool(deployment["enabled"]),
            },
            "candidate": None, "target_deployment": plan["configuration_deployment"],
            "requires_restart": plan["effects"]["requires_restart"],
        }
        db.execute("UPDATE deployment_operations SET configuration_context_json=?,configuration_context_digest=? WHERE id=?",
                   (canonical(context), digest(context), operation["id"]))

    @staticmethod
    def configuration_context_tx(db, operation_id):
        """Private authority; do not return this from public projections."""
        from .worker_common import digest
        row = db.execute("SELECT * FROM deployment_operations WHERE id=?", (operation_id,)).fetchone()
        if row is None:
            raise ValueError("deployment_operation_not_found")
        raw, checksum = row["configuration_context_json"], row["configuration_context_digest"]
        if raw is None and checksum is None:
            return None
        try:
            context = json.loads(raw)
            if (digest(context) != checksum or context["schema"] != 1
                    or context["operation_id"] != operation_id or context["instance_id"] != row["deployment_id"]):
                raise ValueError()
        except (TypeError, KeyError, ValueError):
            raise ValueError("deployment_configuration_context_corrupt") from None
        return context

    def get_configuration_context(self, operation_id):
        with self._connect() as db:
            return self.configuration_context_tx(db, operation_id)

    @staticmethod
    def store_configuration_context_tx(db, context, *, expected_digest):
        from .worker_common import canonical, digest
        if db.execute("UPDATE deployment_operations SET configuration_context_json=?,configuration_context_digest=? "
                      "WHERE id=? AND configuration_context_digest=?",
                      (canonical(context), digest(context), context["operation_id"], expected_digest)).rowcount != 1:
            raise ValueError("deployment_configuration_context_changed")

    def active_configuration_context_tx(self, db, instance):
        rows = db.execute("SELECT id FROM deployment_operations WHERE deployment_id=? "
                          "AND (configuration_context_json IS NOT NULL OR configuration_context_digest IS NOT NULL) "
                          "AND state NOT IN ('ready','failed','canceled')", (instance,)).fetchall()
        if len(rows) > 1:
            raise ValueError("deployment_configuration_ownership_conflict")
        return self.configuration_context_tx(db, rows[0]["id"]) if rows else None

    def switch_configuration_binding_tx(self, db, context, *, restore=False):
        """CAS the entire installation identity; caller also swaps policy/head."""
        from .worker_common import canonical, digest
        source, target = ((context['candidate'], context['previous']) if restore else
                          (context['previous'], context['candidate']))
        if source is None or target is None:
            raise ValueError('deployment_configuration_candidate_required')
        installed = db.execute('SELECT * FROM instance_installation_bindings WHERE instance_id=?',
                               (context['instance_id'],)).fetchone()
        deployment = db.execute('SELECT * FROM model_deployments WHERE id=?', (context['instance_id'],)).fetchone()
        if (deployment is None or deployment['incarnation'] != context['incarnation']
                or installed is None or dict(installed) != source['installation']
                or any(deployment[key] != source['deployment'][key] for key in self.CONFIGURATION_DEPLOYMENT_FIELDS)):
            raise ValueError('deployment_configuration_binding_changed')
        binding = json.loads(target['installation']['binding_json'])
        if (digest(binding) != target['installation']['binding_digest']
                or digest(json.loads(target['installation']['template_json'])) != target['installation']['template_digest']):
            raise ValueError('deployment_configuration_context_corrupt')
        references = [{'asset_id': binding['asset_id'], 'revision': binding['asset_revision'],
                       'manifest_digest': binding['asset_manifest_digest']},
                      *binding.get('optional_assets', {}).values()]
        for reference in references:
            asset = db.execute('SELECT * FROM model_assets WHERE id=?', (reference['asset_id'],)).fetchone()
            if (asset is None or asset['state'] != 'ready' or asset['revision'] != reference['revision']
                    or asset['manifest_digest'] != reference['manifest_digest']):
                raise ValueError('deployment_configuration_asset_unavailable')
        imported = db.execute('SELECT * FROM runtime_image_bindings WHERE engine_id=? AND image_digest=?',
                              (binding['engine_id'], binding['image_digest'])).fetchone()
        if (imported is None or imported['release_digest'] != binding['release_digest']
                or digest(json.loads(imported['verification_json'])) != imported['verification_digest']):
            raise ValueError('deployment_configuration_runtime_unavailable')
        fields = tuple(target['installation'])
        if set(fields) != set(installed.keys()):
            raise ValueError('deployment_configuration_context_corrupt')
        db.execute('UPDATE instance_installation_bindings SET ' + ','.join(key+'=?' for key in fields)
                   + ' WHERE instance_id=? AND binding_digest=?',
                   (*[target['installation'][key] for key in fields], context['instance_id'], installed['binding_digest']))
        db.execute('UPDATE model_deployments SET ' + ','.join(key+'=?' for key in self.CONFIGURATION_DEPLOYMENT_FIELDS)
                   + ' WHERE id=? AND incarnation=?',
                   (*[target['deployment'][key] for key in self.CONFIGURATION_DEPLOYMENT_FIELDS],
                    context['instance_id'], context['incarnation']))

    def get_deployment_operation(self, operation_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM deployment_operations WHERE id=?", (operation_id,),
            ).fetchone()
            if row is None:
                return None
            resources = db.execute(
                """SELECT kind,resource_id,identity,created,created_at
                   FROM deployment_operation_resources WHERE operation_id=?
                   ORDER BY kind,resource_id""", (operation_id,),
            ).fetchall()
        value = self._deployment_operation(row)
        value["resources"] = [dict(item, created=bool(item["created"])) for item in resources]
        return value

    def list_deployment_operations(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM deployment_operations ORDER BY updated_at DESC,id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._deployment_operation(row) for row in rows]

    def transition_deployment_operation(self, operation_id: str, expected_states: Iterable[str],
                                        values: dict[str, Any]) -> dict[str, Any] | None:
        expected = tuple(expected_states)
        if not expected:
            raise ValueError("deployment_operation_expected_state_required")
        allowed = {"state", "milestone", "recovery_cursor", "error_class", "error_code",
                   "error_message", "result", "updated_at"}
        if not values or set(values) - allowed:
            raise ValueError("deployment_operation_update_invalid")
        mapped = {"recovery_cursor": "recovery_cursor_json", "result": "result_json"}
        columns = list(values)
        assignments = ",".join(f"{mapped.get(column, column)}=?" for column in columns)
        params: list[Any] = []
        for column in columns:
            value = values[column]
            if column in {"recovery_cursor", "result"} and value is not None:
                value = json.dumps(value, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), allow_nan=False)
            params.append(value)
        placeholders = ",".join("?" for _ in expected)
        with self._connect() as db:
            changed = db.execute(
                f"UPDATE deployment_operations SET {assignments} WHERE id=? AND state IN ({placeholders})",
                (*params, operation_id, *expected),
            ).rowcount
        return self.get_deployment_operation(operation_id) if changed == 1 else None

    def add_deployment_operation_resource(self, operation_id: str, resource: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute(
                'SELECT identity,created FROM deployment_operation_resources '
                'WHERE operation_id=? AND kind=? AND resource_id=?',
                (operation_id, resource['kind'], resource['resource_id'])).fetchone()
            if previous is not None:
                if previous['identity'] != resource['identity']:
                    raise ValueError('deployment_operation_resource_changed')
                # Replaying an observation must not erase the original ownership.
                return
            db.execute(
                """INSERT INTO deployment_operation_resources(
                       operation_id,kind,resource_id,identity,created,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (operation_id, resource["kind"], resource["resource_id"],
                 resource["identity"], int(resource["created"]), resource["created_at"]),
            )

    def insert_model_transfer(self, transfer: dict[str, Any], files: list[dict[str, Any]], *,
                              owner: InstallationOwner | None = None) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if owner is not None:
                self._assert_installation_owner(db, owner, INSTALL_ACTIVE)
            db.execute(
                """INSERT INTO model_transfers(
                       id,direction,state,display_name,media_kind,role,format,source_type,
                       source_ref,revision,license_declared,expected_bytes,received_bytes,
                       quarantine_relpath,asset_id,error_code,error_message,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (transfer["id"], transfer["direction"], transfer["state"],
                 transfer["display_name"], transfer["media_kind"], transfer["role"],
                 transfer["format"], transfer["source_type"], transfer["source_ref"],
                 transfer["revision"], transfer["license_declared"], transfer.get("expected_bytes"),
                 transfer.get("received_bytes", 0), transfer["quarantine_relpath"],
                 transfer.get("asset_id"), transfer.get("error_code"), transfer.get("error_message"),
                 transfer["created_at"], transfer["updated_at"]),
            )
            for item in files:
                db.execute(
                    """INSERT INTO model_transfer_files(
                           id,transfer_id,relative_path,expected_bytes,expected_sha256,source_url,
                           received_bytes
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (item["id"], transfer["id"], item["relative_path"],
                     item["expected_bytes"], item.get("expected_sha256"),
                    item.get("source_url"), 0),
                )
            if owner is not None:
                self._record_installation_resource(db, owner, "transfer", transfer["id"],
                                                   transfer["id"], True, transfer["created_at"])
                db.execute("UPDATE service_installations SET transfer_id=? WHERE id=?", (transfer["id"], owner[0]))

    def get_model_transfer(self, transfer_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM model_transfers WHERE id=?", (transfer_id,)).fetchone()
            if row is None:
                return None
            files = db.execute(
                "SELECT * FROM model_transfer_files WHERE transfer_id=? ORDER BY relative_path",
                (transfer_id,),
            ).fetchall()
        item = dict(row)
        item["files"] = [dict(file) for file in files]
        return item

    def list_model_transfers(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id FROM model_transfers ORDER BY created_at DESC LIMIT ?", (limit,),
            ).fetchall()
        return [item for row in rows if (item := self.get_model_transfer(str(row["id"]))) is not None]

    def append_model_transfer_bytes(self, transfer_id: str, file_id: str, offset: int,
                                    byte_count: int, updated_at: str) -> str:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT f.received_bytes,f.expected_bytes,t.state
                   FROM model_transfer_files f JOIN model_transfers t ON t.id=f.transfer_id
                   WHERE f.id=? AND f.transfer_id=?""", (file_id, transfer_id),
            ).fetchone()
            if row is None:
                return "not_found"
            if row["state"] not in {"queued", "transferring"}:
                return "invalid_state"
            if int(row["received_bytes"]) != offset:
                return "offset_mismatch"
            if offset + byte_count > int(row["expected_bytes"]):
                return "size_exceeded"
            changed = db.execute(
                """UPDATE model_transfer_files SET received_bytes=received_bytes+?
                   WHERE id=? AND transfer_id=? AND received_bytes=?""",
                (byte_count, file_id, transfer_id, offset),
            ).rowcount
            if changed != 1:
                return "offset_mismatch"
            db.execute(
                """UPDATE model_transfers SET state='transferring',received_bytes=received_bytes+?,updated_at=?
                   WHERE id=?""", (byte_count, updated_at, transfer_id),
            )
            db.execute('''DELETE FROM model_transfer_writes
                WHERE transfer_id=? AND file_id=? AND write_offset=? AND byte_count=?''',
                (transfer_id, file_id, offset, byte_count))
        return "updated"

    def get_model_transfer_write(self, transfer_id):
        with self._connect() as db:
            row = db.execute('SELECT * FROM model_transfer_writes WHERE transfer_id=?', (transfer_id,)).fetchone()
            return dict(row) if row else None

    def begin_model_transfer_write(self, transfer_id, file_id, offset, byte_count, identity):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT t.state,f.received_bytes FROM model_transfers t JOIN model_transfer_files f
                ON f.transfer_id=t.id WHERE t.id=? AND f.id=?''', (transfer_id, file_id)).fetchone()
            if not row or row['state'] not in {'queued', 'transferring'} or row['received_bytes'] != offset:
                raise ValueError('transfer_write_state_changed')
            db.execute('INSERT INTO model_transfer_writes VALUES(?,?,?,?,?)',
                (transfer_id, file_id, offset, byte_count,
                 json.dumps(identity, sort_keys=True, separators=(',', ':'))))

    def clear_model_transfer_write(self, record):
        with self._connect() as db:
            db.execute('''DELETE FROM model_transfer_writes WHERE transfer_id=? AND file_id=?
                AND write_offset=? AND byte_count=? AND object_json=?''',
                tuple(record[key] for key in ('transfer_id','file_id','write_offset','byte_count','object_json')))

    def set_model_transfer_state(self, transfer_id: str, expected: Iterable[str], state: str,
                                 updated_at: str, *, asset_id: str | None = None,
                                 error_code: str | None = None,
                                 error_message: str | None = None) -> bool:
        states = tuple(expected)
        placeholders = ",".join("?" for _ in states)
        with self._connect() as db:
            changed = db.execute(
                f"""UPDATE model_transfers SET state=?,asset_id=?,error_code=?,error_message=?,updated_at=?
                    WHERE id=? AND state IN ({placeholders})""",
                (state, asset_id, error_code, error_message, updated_at, transfer_id, *states),
            ).rowcount
        return changed == 1

    def publish_model_asset(self, asset: dict[str, Any], files: list[dict[str, Any]],
                            transfer_id: str, *, cleanup_files: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            duplicate = db.execute(
                "SELECT * FROM model_assets WHERE manifest_digest=?",
                (asset["manifest_digest"],),
            ).fetchone()
            if duplicate is not None:
                changed = db.execute(
                    """UPDATE model_transfers SET state='succeeded',asset_id=?,updated_at=?
                       WHERE id=? AND state='verifying'""",
                    (duplicate["id"], asset["updated_at"], transfer_id),
                ).rowcount
                if changed != 1:
                    raise RuntimeError("model transfer left verifying state during publication")
                self._register_transfer_cleanup(db, transfer_id, dict(duplicate), cleanup_files)
                return "duplicate", dict(duplicate)
            db.execute(
                """INSERT INTO model_assets(
                       id,display_name,media_kind,role,format,source_type,source_ref,revision,
                       license_declared,manifest_digest,state,total_bytes,file_count,storage_relpath,
                       architecture_family,tensor_precision,parameter_summary_json,metadata_json,
                       detector_version,metadata_digest,created_at,verified_at,updated_at,archived_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (asset["id"], asset["display_name"], asset["media_kind"], asset["role"],
                 asset["format"], asset["source_type"], asset["source_ref"], asset["revision"],
                 asset["license_declared"], asset["manifest_digest"], asset["state"],
                 asset["total_bytes"], asset["file_count"], asset["storage_relpath"],
                 asset.get("architecture_family", "unknown"), asset.get("tensor_precision"),
                 json.dumps(asset.get("parameter_summary", {}), ensure_ascii=False,
                            sort_keys=True, separators=(",", ":"), allow_nan=False),
                 json.dumps(asset.get("metadata", {}), ensure_ascii=False,
                            sort_keys=True, separators=(",", ":"), allow_nan=False),
                 asset.get("detector_version", "legacy"), asset.get("metadata_digest"),
                 asset["created_at"], asset["verified_at"], asset["updated_at"], None),
            )
            for item in files:
                db.execute(
                    """INSERT INTO model_asset_files(
                           asset_id,relative_path,sha256,byte_size,storage_relpath
                       ) VALUES(?,?,?,?,?)""",
                    (asset["id"], item["relative_path"], item["sha256"],
                     item["byte_size"], item["storage_relpath"]),
                )
            changed = db.execute(
                """UPDATE model_transfers SET state='succeeded',asset_id=?,updated_at=?
                   WHERE id=? AND state='verifying'""",
                (asset["id"], asset["updated_at"], transfer_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError("model transfer left verifying state during publication")
            self._register_transfer_cleanup(db, transfer_id, asset, cleanup_files)
        return "created", asset

    @staticmethod
    def _register_transfer_cleanup(db, transfer_id, asset, files):
        # Called only inside the publication transaction. Terminal state alone
        # is never a deletion authority (especially for pre-upgrade records).
        transfer = db.execute('SELECT * FROM model_transfers WHERE id=?', (transfer_id,)).fetchone()
        source = db.execute('SELECT relative_path,expected_bytes,received_bytes FROM model_transfer_files '
                            'WHERE transfer_id=? ORDER BY relative_path', (transfer_id,)).fetchall()
        formal = db.execute('SELECT relative_path,sha256,byte_size,storage_relpath FROM model_asset_files '
                            'WHERE asset_id=? ORDER BY relative_path', (asset['id'],)).fetchall()
        ordered = sorted(files, key=lambda item: item['relative_path'])
        fields = ('relative_path', 'sha256', 'byte_size', 'storage_relpath')
        if (not files or transfer is None or transfer['state'] != 'succeeded'
                or db.execute('SELECT 1 FROM model_transfer_writes WHERE transfer_id=?', (transfer_id,)).fetchone()
                or transfer['asset_id'] != asset['id']
                or transfer['quarantine_relpath'] != 'quarantine/' + transfer_id
                or [tuple(item[key] for key in fields) for item in ordered] != [tuple(row) for row in formal]
                or [(item['relative_path'], item['byte_size'], item['byte_size']) for item in ordered]
                   != [tuple(row) for row in source]):
            raise ValueError('transfer_cleanup_binding_mismatch')
        db.execute('''INSERT INTO model_transfer_cleanup
            (transfer_id,asset_id,revision,manifest_digest,files_json,state,updated_at)
            VALUES(?,?,?,?,?,'pending',?)''',
            (transfer_id, asset['id'], asset['revision'], asset['manifest_digest'],
             json.dumps(ordered, sort_keys=True, separators=(',', ':'), allow_nan=False), asset['updated_at']))

    def pending_transfer_cleanups(self, limit=8, *, after=''):
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM model_transfer_cleanup WHERE state='pending' AND transfer_id>? ORDER BY transfer_id LIMIT ?",
                (after, max(1, min(32, limit))))]

    def get_transfer_cleanup(self, transfer_id):
        with self._connect() as db:
            row = db.execute('SELECT * FROM model_transfer_cleanup WHERE transfer_id=?', (transfer_id,)).fetchone()
            return dict(row) if row else None

    def finish_transfer_cleanup(self, transfer_id, state, error_code, updated_at):
        if state not in {'done', 'blocked'}:
            raise ValueError('invalid_transfer_cleanup_state')
        with self._connect() as db:
            return db.execute('''UPDATE model_transfer_cleanup SET state=?,error_code=?,updated_at=?
                WHERE transfer_id=? AND state='pending' ''',
                (state, error_code, updated_at, transfer_id)).rowcount == 1

    def model_storage_totals(self):
        with self._connect() as db:
            assets = db.execute('SELECT coalesce(sum(total_bytes),0) FROM model_assets').fetchone()[0]
            received = db.execute('SELECT coalesce(sum(received_bytes),0) FROM model_transfers').fetchone()[0]
            cleanup = {row[0]: row[1] for row in db.execute(
                'SELECT state,count(*) FROM model_transfer_cleanup GROUP BY state')}
            blocked = [dict(row) for row in db.execute('''SELECT transfer_id,error_code
                FROM model_transfer_cleanup WHERE state='blocked' ORDER BY transfer_id LIMIT 8''')]
        return {'asset_bytes': assets, 'transfer_received_bytes': received,
                'cleanup_counts': cleanup, 'cleanup_blocked': blocked}

    def publish_migrated_model_assets(self, assets: list[dict[str, Any]],
                                      bindings: list[dict[str, Any]],
                                      occurred_at: str) -> None:
        """Publish an offline migration as one database transaction.

        Asset directories must already be fully staged at their final immutable
        paths. This method deliberately does not move or delete model files.
        """
        asset_ids = {asset["id"] for asset in assets}
        if len(asset_ids) != len(assets) or not assets or not bindings:
            raise ValueError("migration requires unique assets and deployment bindings")
        if any(binding["asset_id"] not in asset_ids for binding in bindings):
            raise ValueError("migration binding references an unknown asset")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for asset in assets:
                if db.execute(
                        "SELECT 1 FROM model_assets WHERE id=? OR manifest_digest=?",
                        (asset["id"], asset["manifest_digest"])).fetchone() is not None:
                    raise ValueError(f"model asset already exists: {asset['id']}")
            for binding in bindings:
                deployment = db.execute(
                    "SELECT asset_id,desired_state,actual_state FROM model_deployments WHERE id=?",
                    (binding["deployment_id"],),
                ).fetchone()
                if deployment is None:
                    raise ValueError(f"deployment not found: {binding['deployment_id']}")
                if (deployment["asset_id"] is not None or
                        deployment["desired_state"] != "unloaded" or
                        deployment["actual_state"] != "unloaded"):
                    raise ValueError(
                        f"deployment is not migration-safe: {binding['deployment_id']}")
            for asset in assets:
                db.execute(
                    """INSERT INTO model_assets(
                           id,display_name,media_kind,role,format,source_type,source_ref,revision,
                           license_declared,manifest_digest,state,total_bytes,file_count,storage_relpath,
                           architecture_family,tensor_precision,parameter_summary_json,metadata_json,
                           detector_version,metadata_digest,created_at,verified_at,updated_at,archived_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (asset["id"], asset["display_name"], asset["media_kind"], asset["role"],
                     asset["format"], asset["source_type"], asset["source_ref"], asset["revision"],
                     asset["license_declared"], asset["manifest_digest"], "ready",
                     asset["total_bytes"], asset["file_count"], asset["storage_relpath"],
                     asset.get("architecture_family", "unknown"), asset.get("tensor_precision"),
                     json.dumps(asset.get("parameter_summary", {}), ensure_ascii=False,
                                sort_keys=True, separators=(",", ":"), allow_nan=False),
                     json.dumps(asset.get("metadata", {}), ensure_ascii=False,
                                sort_keys=True, separators=(",", ":"), allow_nan=False),
                     asset.get("detector_version", "legacy"), asset.get("metadata_digest"),
                     occurred_at, occurred_at, occurred_at, None),
                )
                for item in asset["files"]:
                    db.execute(
                        """INSERT INTO model_asset_files(
                               asset_id,relative_path,sha256,byte_size,storage_relpath
                           ) VALUES(?,?,?,?,?)""",
                        (asset["id"], item["relative_path"], item["sha256"],
                         item["byte_size"], item["storage_relpath"]),
                    )
            for binding in bindings:
                changed = db.execute(
                    """UPDATE model_deployments
                       SET asset_id=?,model_path=?,updated_at=?
                       WHERE id=? AND asset_id IS NULL
                         AND desired_state='unloaded' AND actual_state='unloaded'""",
                    (binding["asset_id"], binding["model_path"], occurred_at,
                     binding["deployment_id"]),
                ).rowcount
                if changed != 1:
                    raise RuntimeError(
                        f"deployment changed during migration: {binding['deployment_id']}")
                db.execute(
                    """INSERT INTO audit_events(occurred_at,action,target,detail_json)
                       VALUES(?,?,?,?)""",
                    (occurred_at, "model_asset.migrate", binding["deployment_id"],
                     json.dumps({"asset_id": binding["asset_id"],
                                 "model_path": binding["model_path"]},
                                ensure_ascii=False, sort_keys=True)),
                )

    def get_model_asset(self, asset_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM model_assets WHERE id=?", (asset_id,)).fetchone()
            if row is None:
                return None
            files = db.execute(
                "SELECT * FROM model_asset_files WHERE asset_id=? ORDER BY relative_path",
                (asset_id,),
            ).fetchall()
            references = int(db.execute(
                "SELECT COUNT(*) FROM model_deployments WHERE asset_id=?", (asset_id,),
            ).fetchone()[0])
        item = self._model_asset(row)
        item["files"] = [dict(file) for file in files]
        item["deployment_references"] = references
        return item

    def list_model_assets(self, *, media_kind: str | None = None, role: str | None = None,
                          state: str | None = None, query: str | None = None,
                          limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (("media_kind", media_kind), ("role", role), ("state", state)):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        if query:
            clauses.append("LOWER(display_name) LIKE ?")
            params.append(f"%{query.lower()}%")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as db:
            rows = db.execute(
                f"SELECT * FROM model_assets{where} ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [self._model_asset(row) for row in rows]

    def set_model_asset_archived(self, asset_id: str, archived: bool, updated_at: str) -> str:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM model_assets WHERE id=?", (asset_id,)).fetchone()
            if row is None:
                return "not_found"
            target = "archived" if archived else "ready"
            expected = "ready" if archived else "archived"
            if row["state"] != expected:
                return "invalid_state"
            if archived and int(db.execute(
                    "SELECT COUNT(*) FROM model_deployments WHERE asset_id=?", (asset_id,),
            ).fetchone()[0]):
                return "referenced"
            db.execute(
                "UPDATE model_assets SET state=?,archived_at=?,updated_at=? WHERE id=?",
                (target, updated_at if archived else None, updated_at, asset_id),
            )
        return "updated"

    def insert_service_installation(self, installation: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = tuple(INSTALL_ACTIVE | {"paused"})
            placeholders = ",".join("?" for _ in active)
            if db.execute(f"SELECT 1 FROM service_installations WHERE recipe_key=? AND state IN ({placeholders})",
                          (installation["recipe_key"], *active)).fetchone():
                raise InstallationOwnershipError("service_installation_busy")
            db.execute(
                """INSERT INTO service_installations(
                       id,recipe_key,state,current_step,progress,deployment_id,transfer_id,
                       asset_id,options_json,steps_json,error_code,error_message,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (installation["id"], installation["recipe_key"], installation["state"],
                 installation["current_step"], installation["progress"],
                 installation.get("deployment_id"), installation.get("transfer_id"),
                 installation.get("asset_id"),
                 json.dumps(installation["options"], ensure_ascii=False, allow_nan=False),
                 json.dumps(installation["steps"], ensure_ascii=False, allow_nan=False),
                 installation.get("error_code"), installation.get("error_message"),
                 installation["created_at"], installation["updated_at"]),
            )
            attempt = "sia_" + secrets.token_hex(16)
            db.execute("INSERT INTO installation_attempts VALUES(?,?,1,?,NULL,NULL,?,?)",
                       (attempt, installation["id"], installation["state"], installation["created_at"], installation["updated_at"]))
            db.execute("UPDATE service_installations SET current_attempt_id=?,recipe_json=? WHERE id=?",
                       (attempt, json.dumps(installation.get("recipe_snapshot"), ensure_ascii=False), installation["id"]))

    def get_service_installation(self, installation_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM service_installations WHERE id=?", (installation_id,),
            ).fetchone()
        return self._service_installation(row) if row else None

    def list_service_installations(self, limit: int = 100, *, catalog_only: bool = False) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM service_installations "
                + ("WHERE substr(id,1,4)!='dop_' " if catalog_only else "")
                + "ORDER BY created_at DESC LIMIT ?", (limit,),
            ).fetchall()
        return [self._service_installation(row) for row in rows]

    def update_service_installation(self, installation_id: str,
                                    values: dict[str, Any], *, owner: InstallationOwner | None = None) -> dict[str, Any] | None:
        allowed = {"state", "current_step", "progress", "deployment_id", "transfer_id",
                   "asset_id", "steps", "error_code", "error_message", "updated_at"}
        columns = sorted(set(values) & allowed)
        if not columns:
            return self.get_service_installation(installation_id)
        mapped = {"steps": "steps_json"}
        assignments = ", ".join(f"{mapped.get(column, column)}=?" for column in columns)
        params: list[Any] = []
        for column in columns:
            value = values[column]
            if column == "steps":
                value = json.dumps(value, ensure_ascii=False, allow_nan=False)
            params.append(value)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if owner is None or owner[0] != installation_id:
                raise InstallationOwnershipError("installation_attempt_required")
            self._assert_installation_owner(db, owner, INSTALL_ACTIVE)
            if values.get("state") == "ready":
                row = db.execute("SELECT * FROM service_installations WHERE id=?", (installation_id,)).fetchone()
                deployment = db.execute(
                    """SELECT d.* FROM model_deployments d JOIN installation_resources r
                       ON r.resource_id=d.id AND r.identity=d.incarnation
                       WHERE r.kind='deployment' AND r.created=1 AND r.attempt_id=?
                         AND d.id=? AND d.install_state='ready'""", (owner[1], row["deployment_id"])).fetchone()
                if deployment is None:
                    raise InstallationOwnershipError("installation_deployment_not_ready")
                options = json.loads(row["options_json"])
                has_default = db.execute("SELECT 1 FROM model_deployments WHERE kind=? AND is_default=1",
                                         (deployment["kind"],)).fetchone() is not None
                db.execute("UPDATE model_deployments SET enabled=1,is_default=?,startup_policy=? WHERE id=?",
                           (int(not has_default), options["startup_policy"], deployment["id"]))
            changed = db.execute(
                f"UPDATE service_installations SET {assignments} WHERE id=?",
                (*params, installation_id),
            ).rowcount
            db.execute("UPDATE installation_attempts SET state=?,updated_at=? WHERE id=?",
                       (values.get("state", "preflight"), values["updated_at"], owner[1]))
        return self.get_service_installation(installation_id) if changed else None

    @staticmethod
    def _assert_installation_owner(db: sqlite3.Connection, owner: InstallationOwner,
                                   states: set[str]) -> sqlite3.Row:
        row = db.execute(
            """SELECT s.*,a.runner_token,a.owner_pid FROM service_installations s
               JOIN installation_attempts a ON a.id=s.current_attempt_id
               WHERE s.id=? AND a.id=?""", owner[:2]).fetchone()
        if row is None or row["state"] not in states or (owner[2] is not None and row["runner_token"] != owner[2]):
            raise InstallationOwnershipError("installation_attempt_stale")
        return row

    @staticmethod
    def _assert_deployment_owner(db: sqlite3.Connection, owner: InstallationOwner,
                                  deployment_id: str, incarnation: str) -> None:
        if db.execute("""SELECT 1 FROM installation_resources WHERE attempt_id=? AND kind='deployment'
                       AND resource_id=? AND identity=?""",
                      (owner[1], deployment_id, incarnation)).fetchone() is None:
            raise InstallationOwnershipError("deployment_identity_changed")

    def adopt_deployment_for_installation(self, owner: InstallationOwner, deployment_id: str,
                                          incarnation: str, catalog_key: str, asset_id: str,
                                          now: str) -> None:
        """Fence an existing deployment for a non-destructive runtime adoption."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_installation_owner(db, owner, INSTALL_ACTIVE)
            row = db.execute("SELECT * FROM model_deployments WHERE id=? AND incarnation=?",
                             (deployment_id, incarnation)).fetchone()
            if (not row or row["catalog_key"] != catalog_key or row["asset_id"] != asset_id):
                raise InstallationOwnershipError("deployment_identity_changed")
            if db.execute("SELECT 1 FROM instance_claims WHERE instance_id=? AND state!='exited'",
                          (deployment_id,)).fetchone():
                raise InstallationOwnershipError("deployment_active_during_adoption")
            self._record_installation_resource(db, owner, "deployment",
                                               deployment_id, incarnation, False, now)

    @staticmethod
    def _record_installation_resource(db: sqlite3.Connection, owner: InstallationOwner,
                                      kind: str, resource_id: str, identity: str,
                                      created: bool, now: str) -> None:
        existing = db.execute("SELECT identity,created FROM installation_resources WHERE attempt_id=? AND kind=? AND resource_id=?",
                              (owner[1], kind, resource_id)).fetchone()
        if existing and (existing["identity"] != identity or bool(existing["created"]) != created):
            raise InstallationOwnershipError("installation_resource_changed")
        db.execute("INSERT OR IGNORE INTO installation_resources(attempt_id,kind,resource_id,identity,created,created_at) VALUES(?,?,?,?,?,?)",
                   (owner[1], kind, resource_id, identity, int(created), now))

    def record_installation_resource(self, owner: InstallationOwner, kind: str, resource_id: str,
                                     identity: str, created: bool, now: str) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_installation_owner(db, owner, INSTALL_ACTIVE)
            self._record_installation_resource(db, owner, kind, resource_id, identity, created, now)

    @staticmethod
    def _assert_resource_exclusive(db: sqlite3.Connection, operation_id: str,
                                   kind: str, resource_id: str) -> None:
        other = db.execute(
            """SELECT 1 FROM installation_resources r
               JOIN installation_attempts a ON a.id=r.attempt_id
               WHERE r.kind=? AND r.resource_id=?
                 AND r.successor_attempt_id IS NULL AND a.operation_id!=?""",
            (kind, resource_id, operation_id),
        ).fetchone()
        if other:
            raise InstallationOwnershipError("installation_resource_shared")
        if kind == "transfer" and db.execute(
                "SELECT 1 FROM service_installations WHERE transfer_id=? AND id!=?",
                (resource_id, operation_id)).fetchone():
            raise InstallationOwnershipError("installation_resource_shared")

    def assert_installation_owner(self, owner: InstallationOwner) -> None:
        with self._connect() as db:
            self._assert_installation_owner(db, owner, INSTALL_ACTIVE)

    def claim_installation_runner(self, operation_id: str, attempt_id: str) -> InstallationOwner | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = (operation_id, attempt_id, None)
            try:
                row = self._assert_installation_owner(db, owner, INSTALL_ACTIVE)
            except InstallationOwnershipError:
                return None
            if row["runner_token"] is not None:
                return None
            token = secrets.token_hex(16)
            db.execute("UPDATE installation_attempts SET runner_token=?,owner_pid=? WHERE id=?",
                       (token, os.getpid(), attempt_id))
            return operation_id, attempt_id, token

    def release_installation_runner(self, owner: InstallationOwner) -> None:
        with self._connect() as db:
            db.execute("UPDATE installation_attempts SET runner_token=NULL,owner_pid=NULL WHERE id=? AND runner_token=?",
                       (owner[1], owner[2]))

    def installation_attempts(self, operation_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM installation_attempts WHERE operation_id=? ORDER BY generation", (operation_id,))]

    def installation_resources(self, attempt_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM installation_resources WHERE attempt_id=?", (attempt_id,))]

    def retry_service_installation(self, operation_id: str, attempt_id: str, now: str,
                                   steps: list[dict[str, Any]]) -> dict[str, Any]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._assert_installation_owner(db, (operation_id, attempt_id, None), {"failed"})
            options = json.loads(row["options_json"])
            # Retry never cleans by a historical name or takes over a deployment.
            if db.execute("SELECT 1 FROM model_deployments WHERE id=?", (options["deployment_id"],)).fetchone():
                raise InstallationOwnershipError("installation_deployment_conflict")
            active = tuple(INSTALL_ACTIVE | {"paused"})
            if db.execute(f"SELECT 1 FROM service_installations WHERE recipe_key=? AND id!=? AND state IN ({','.join('?' for _ in active)})",
                          (row["recipe_key"], operation_id, *active)).fetchone():
                raise InstallationOwnershipError("service_installation_busy")
            generation = db.execute("SELECT generation FROM installation_attempts WHERE id=?", (attempt_id,)).fetchone()[0] + 1
            new_attempt = "sia_" + secrets.token_hex(16)
            resources = db.execute("SELECT * FROM installation_resources WHERE attempt_id=? AND created=1 AND successor_attempt_id IS NULL AND kind='transfer'",
                                   (attempt_id,)).fetchall()
            transferable = []
            for resource in resources:
                self._assert_resource_exclusive(db, operation_id, resource["kind"], resource["resource_id"])
                run = db.execute("SELECT * FROM transfer_download_runs WHERE transfer_id=?", (resource["resource_id"],)).fetchone()
                if run and run["state"] == "running" and _pid_alive(run["owner_pid"]):
                    raise InstallationOwnershipError("installation_transfer_running")
                transfer = db.execute("SELECT state FROM model_transfers WHERE id=?", (resource["resource_id"],)).fetchone()
                if transfer is None or transfer["state"] not in {"failed", "paused", "succeeded", "queued"}:
                    raise InstallationOwnershipError("installation_transfer_state_changed")
                transferable.append(resource)
            db.execute("INSERT INTO installation_attempts VALUES(?,?,?,'preflight',NULL,NULL,?,?)",
                       (new_attempt, operation_id, generation, now, now))
            for resource in transferable:
                self._record_installation_resource(db, (operation_id, new_attempt, None), resource["kind"],
                                                   resource["resource_id"], resource["identity"], True, now)
                db.execute("UPDATE installation_resources SET successor_attempt_id=? WHERE attempt_id=? AND kind=? AND resource_id=?",
                           (new_attempt, attempt_id, resource["kind"], resource["resource_id"]))
                if resource["kind"] == "transfer":
                    db.execute("UPDATE model_transfers SET state=CASE WHEN state='succeeded' THEN state ELSE 'queued' END,error_code=NULL,error_message=NULL,updated_at=? WHERE id=?",
                               (now, resource["resource_id"]))
            transfer_id = row["transfer_id"] if any(r["kind"] == "transfer" and r["resource_id"] == row["transfer_id"] for r in transferable) else None
            db.execute("""UPDATE service_installations SET current_attempt_id=?,state='preflight',current_step='preflight',
                       progress=0.02,deployment_id=NULL,transfer_id=?,asset_id=NULL,error_code=NULL,error_message=NULL,
                       steps_json=?,updated_at=? WHERE id=?""",
                       (new_attempt, transfer_id, json.dumps(steps), now, operation_id))
        return self.get_service_installation(operation_id) or {}

    def control_service_installation(self, owner: InstallationOwner, action: str, now: str,
                                     *, expected_transfer_id: str | None) -> str | None:
        """Atomically change installation and only its exclusively owned transfer.

        Cancel may detach from a shared transfer. Pause/resume may not mutate it.
        Return a transfer id only when a resumed downloader should be awakened.
        """
        expected = {"cancel": {"preflight", "downloading", "paused", "verifying"},
                    "pause": {"downloading"}, "resume": {"paused"}}[action]
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._assert_installation_owner(db, owner, expected)
            if row['transfer_id'] != expected_transfer_id:
                raise InstallationOwnershipError('installation_transfer_changed')
            image = db.execute('SELECT * FROM runtime_image_transfers WHERE operation_id=? AND attempt_id=?', owner[:2]).fetchone()
            image_controlled = False
            if image:
                resource = db.execute("SELECT 1 FROM installation_resources WHERE attempt_id=? AND kind='runtime-transfer' AND resource_id=? AND created=1 AND successor_attempt_id IS NULL",
                    (owner[1], image['transfer_id'])).fetchone()
                if not resource: raise InstallationOwnershipError('runtime_transfer_owner_changed')
                image_states = {'pause': {'queued','downloading'}, 'resume': {'paused'}, 'cancel': {'queued','downloading','paused','failed'}}
                if image['phase'] in image_states[action]:
                    db.execute('UPDATE runtime_image_transfers SET phase=?,updated_at=? WHERE transfer_id=?',
                        ({'pause':'paused','resume':'queued','cancel':'canceled'}[action], now, image['transfer_id']))
                    image_controlled = True
            transfer_id = row["transfer_id"]
            exclusive = False
            transfer = None
            if transfer_id:
                transfer = db.execute("SELECT * FROM model_transfers WHERE id=?", (transfer_id,)).fetchone()
                claim = db.execute("SELECT 1 FROM installation_resources WHERE attempt_id=? AND kind='transfer' AND resource_id=? AND created=1",
                                   (owner[1], transfer_id)).fetchone()
                other = db.execute("SELECT 1 FROM installation_resources WHERE kind='transfer' AND resource_id=? AND attempt_id!=? AND successor_attempt_id IS NULL",
                                   (transfer_id, owner[1])).fetchone()
                referenced = db.execute("SELECT 1 FROM service_installations WHERE transfer_id=? AND id!=?",
                                        (transfer_id, owner[0])).fetchone()
                exclusive = bool(claim and not other and not referenced)
            if action != "cancel" and not image_controlled and (not exclusive or transfer is None):
                raise InstallationOwnershipError("installation_transfer_shared")
            transfer_states = {"cancel": ({"queued", "transferring", "paused"}, "canceled"),
                               "pause": ({"queued", "transferring"}, "paused"), "resume": ({"paused"}, "queued")}
            allowed, target = transfer_states[action]
            if transfer is not None and exclusive:
                if transfer["state"] in allowed:
                    db.execute("UPDATE model_transfers SET state=?,updated_at=? WHERE id=?", (target, now, transfer_id))
                elif action != "cancel":
                    raise InstallationOwnershipError("installation_transfer_state_changed")
            state = {"cancel": "canceled", "pause": "paused", "resume": "downloading"}[action]
            db.execute("UPDATE service_installations SET state=?,current_step=?,updated_at=?,error_code=?,error_message=? WHERE id=?",
                       (state, "canceled" if action == "cancel" else "download", now,
                        "user_canceled" if action == "cancel" else None,
                        "用户取消安装" if action == "cancel" else None, owner[0]))
            db.execute("UPDATE installation_attempts SET state=?,updated_at=? WHERE id=?", (state, now, owner[1]))
            return transfer_id if action == "resume" else None

    def deployment_installation_committed(self, deployment_id: str, incarnation: str) -> bool:
        with self._connect() as db:
            rows = db.execute("""SELECT s.state,s.current_attempt_id,r.attempt_id FROM installation_resources r
                               JOIN installation_attempts a ON a.id=r.attempt_id
                               JOIN service_installations s ON s.id=a.operation_id
                               WHERE r.kind='deployment' AND r.resource_id=? AND r.identity=? AND r.created=1""",
                              (deployment_id, incarnation)).fetchall()
            return not rows or all(row["state"] == "ready" and row["current_attempt_id"] == row["attempt_id"] for row in rows)

    def recover_service_installation(self, operation_id: str, attempt_id: str, now: str) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._assert_installation_owner(db, (operation_id, attempt_id, None), INSTALL_ACTIVE)
            except InstallationOwnershipError:
                return False
            if row["runner_token"] and _pid_alive(row["owner_pid"]):
                return False
            db.execute("""UPDATE service_installations SET state='failed',current_step='failed',updated_at=?,
                       error_code='service_restarted',error_message='安装执行中断，请检查后重试' WHERE id=?""", (now, operation_id))
            db.execute("UPDATE installation_attempts SET state='failed',runner_token=NULL,owner_pid=NULL,updated_at=? WHERE id=?",
                       (now, attempt_id))
            return True

    def claim_transfer_download(self, transfer_id: str) -> str | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            transfer = db.execute("SELECT state FROM model_transfers WHERE id=?", (transfer_id,)).fetchone()
            previous = db.execute("SELECT * FROM transfer_download_runs WHERE transfer_id=?", (transfer_id,)).fetchone()
            if not transfer or transfer["state"] != "queued" or (previous and previous["state"] == "running" and _pid_alive(previous["owner_pid"])):
                return None
            token = secrets.token_hex(16)
            db.execute("""INSERT INTO transfer_download_runs VALUES(?,?,?,'running') ON CONFLICT(transfer_id)
                       DO UPDATE SET token=excluded.token,owner_pid=excluded.owner_pid,state='running'""",
                       (transfer_id, token, os.getpid()))
            return token

    def finish_transfer_download(self, transfer_id: str, token: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE transfer_download_runs SET state='stopped' WHERE transfer_id=? AND token=?", (transfer_id, token))

    def recover_model_transfer(self, transfer_id: str, now: str) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT * FROM transfer_download_runs WHERE transfer_id=?", (transfer_id,)).fetchone()
            if run and run["state"] == "running" and _pid_alive(run["owner_pid"]):
                return False
            return db.execute("""UPDATE model_transfers SET state='paused',updated_at=?,error_code='service_restarted',
                               error_message='服务重启后等待续传' WHERE id=? AND state IN ('transferring','verifying')""",
                              (now, transfer_id)).rowcount == 1

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:return None
            result=self._task(row)
            result['configuration_binding']=self._task_configuration(db, row)
            result['publication']=self._publication_status(db,row['current_attempt_id'])
            attempt=db.execute('SELECT id,instance_id,epoch,status,exit_confirmed,exit_evidence,execution_deadline_at,termination_reason FROM task_attempts WHERE id=? AND task_id=?',
                               (row['current_attempt_id'],task_id)).fetchone()
            result['attempt']=dict(attempt) if attempt else None
            return result

    @staticmethod
    def _publication_status(db,attempt_id):
        row=db.execute('SELECT publication_id,phase,error_code,retry_after FROM artifact_publications WHERE attempt_id=?',(attempt_id,)).fetchone()
        return dict(row) if row else None

    def insert_asset(self, asset: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO input_assets(id,filename,media_type,sha256,byte_size,storage_path,created_at) VALUES(?,?,?,?,?,?,?)",
                (asset["id"], asset["filename"], asset["media_type"], asset["sha256"],
                 asset["byte_size"], asset["storage_path"], asset["created_at"]),
            )

    def get_asset(self, asset_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM input_assets WHERE id=?", (asset_id,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(self._task(row),
                         configuration_binding=self._task_configuration(db, row),
                         publication=self._publication_status(db,row['current_attempt_id']))
                    for row in rows]

    @staticmethod
    def _task_configuration(db, row) -> dict[str, Any] | None:
        if row['deployment_id'] is None or row['deployment_config_revision'] is None:
            return None
        revision = db.execute(
            '''SELECT config_revision,config_digest,runtime_profile_id,
                      runtime_profile_revision,runtime_profile_digest,
                      runtime_image_digest,base_asset_id,base_asset_revision,
                      base_asset_manifest_digest,vae_asset_id,vae_asset_revision,
                      vae_asset_manifest_digest
               FROM model_deployment_revisions
               WHERE deployment_id=? AND config_revision=?''',
            (row['deployment_id'], row['deployment_config_revision']),
        ).fetchone()
        if revision is None:
            return None
        return {'deployment_id': row['deployment_id'], **dict(revision)}

    def active_count(self, kind: ServiceKind | None = None) -> int:
        sql = "SELECT COUNT(*) FROM tasks WHERE status IN ('queued','assigned','running','cancel_requested')"
        params: tuple[Any, ...] = ()
        if kind is not None:
            sql += " AND service=?"
            params = (kind.value,)
        with self._connect() as db:
            return int(db.execute(sql, params).fetchone()[0])

    def active_deployment_count(self, deployment_id: str) -> int:
        with self._connect() as db:
            return int(db.execute(
                "SELECT COUNT(*) FROM tasks WHERE model_key=? AND status IN ('queued','assigned','running','cancel_requested')",
                (deployment_id,),
            ).fetchone()[0])

    def task_counts(self) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute("SELECT status,COUNT(*) count FROM tasks GROUP BY status").fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def authorized_artifact(self, relative_path: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT a.task_id,a.byte_size,a.sha256,a.origin,t.output_json FROM task_artifacts a
                   JOIN tasks t ON t.id=a.task_id WHERE t.status='succeeded' AND a.relative_path=?""",
                (relative_path,),
            ).fetchone()
        if row is None:
            return None
        return {"task_id": row["task_id"], "bytes": int(row["byte_size"]), "sha256": row["sha256"],
                "origin":row["origin"], "output": json.loads(row["output_json"])}

    def add_audit(self, occurred_at: str, action: str, target: str, detail: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO audit_events(occurred_at,action,target,detail_json) VALUES(?,?,?,?)",
                       (occurred_at, action, target, json.dumps(detail, ensure_ascii=False, allow_nan=False)))

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": row["id"], "occurred_at": row["occurred_at"], "action": row["action"],
                 "target": row["target"], "detail": json.loads(row["detail_json"])} for row in rows]

    @staticmethod
    def _runtime_profile(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "profile_id": row["profile_id"], "revision": int(row["revision"]),
            "label": row["label"], "image_digest": row["image_digest"],
            "worker_protocol": row["worker_protocol"],
            "architecture_families": json.loads(row["architecture_families_json"]),
            "main_formats": json.loads(row["main_formats_json"]),
            "optional_deployment_roles": json.loads(row["optional_deployment_roles_json"]),
            "task_roles": json.loads(row["task_roles_json"]), "loader": row["loader"],
            "trust_remote_code": bool(row["trust_remote_code"]),
            "residency_modes": json.loads(row["residency_modes_json"]),
            "required_vram_mib": int(row["required_vram_mib"]),
            "profile_digest": row["profile_digest"], "created_at": row["created_at"],
        }

    @staticmethod
    def _model_asset(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["parameter_summary"] = json.loads(value.pop("parameter_summary_json"))
        value["metadata"] = json.loads(value.pop("metadata_json"))
        return value

    @staticmethod
    def _model_deployment_revision(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "deployment_id": row["deployment_id"],
            "config_revision": int(row["config_revision"]),
            "runtime_profile_id": row["runtime_profile_id"],
            "runtime_profile_revision": int(row["runtime_profile_revision"]),
            "runtime_profile_digest": row["runtime_profile_digest"],
            "runtime_image_digest": row["runtime_image_digest"],
            "base_asset_id": row["base_asset_id"],
            "base_asset_revision": row["base_asset_revision"],
            "base_asset_manifest_digest": row["base_asset_manifest_digest"],
            "vae_asset_id": row["vae_asset_id"],
            "vae_asset_revision": row["vae_asset_revision"],
            "vae_asset_manifest_digest": row["vae_asset_manifest_digest"],
            "gpu_uuids": json.loads(row["gpu_uuids_json"]),
            "required_vram_mib": int(row["required_vram_mib"]),
            "sharing_mode": row["sharing_mode"], "residency": row["residency"],
            "external_reserve_mib": int(row["external_reserve_mib"]),
            "idle_seconds": int(row["idle_seconds"]),
            "license_confirmation": json.loads(row["license_confirmation_json"]),
            "experimental_compatibility_accepted": bool(
                row["experimental_compatibility_accepted"]),
            "desired_state": row["desired_state"], "config_digest": row["config_digest"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _asset_compatibility(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "subject_asset_id": row["subject_asset_id"],
            "subject_revision": row["subject_revision"],
            "base_asset_id": row["base_asset_id"], "base_revision": row["base_revision"],
            "detector_version": row["detector_version"], "verdict": row["verdict"],
            "reason_codes": json.loads(row["reason_codes_json"]),
            "evidence": json.loads(row["evidence_json"]),
            "evidence_digest": row["evidence_digest"], "created_at": row["created_at"],
        }

    @staticmethod
    def _deployment_operation(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "server_profile_id": row["server_profile_id"],
            "authenticated_principal": row["authenticated_principal"],
            "route": row["route"], "idempotency_key": row["idempotency_key"],
            "request_digest": row["request_digest"], "deployment_id": row["deployment_id"],
            "state": row["state"], "payload": json.loads(row["payload_json"]),
            "plan_digest": row["plan_digest"],
            "confirmations": json.loads(row["confirmations_json"]),
            "milestone": row["milestone"],
            "recovery_cursor": json.loads(row["recovery_cursor_json"])
            if row["recovery_cursor_json"] else None,
            "error_class": row["error_class"], "error_code": row["error_code"],
            "error_message": row["error_message"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    @staticmethod
    def _task(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "service": row["service"], "model": row["model_key"],
                "status": row["status"], "version": row["version"],
                "current_attempt_id": row["current_attempt_id"], "cancel_revision": row["cancel_revision"],
                "execution_mode": row["execution_mode"], "migration_source": row["migration_source"],
                "deployment_id": row["deployment_id"],
                "deployment_config_revision": row["deployment_config_revision"],
                "execution_binding": json.loads(row["binding_json"]) if row["binding_json"] else None,
                "prompt": row["prompt"], "options": json.loads(row["options_json"]),
                "inputs": json.loads(row["inputs_json"] or "[]"),
                "input_bindings": json.loads(row["input_bindings_json"]),
                "loras": [{key: value[key] for key in ("asset_id", "revision", "family", "weight")}
                          for value in json.loads(row["lora_bindings_json"])],
                "output": json.loads(row["output_json"]) if row["output_json"] else None,
                "execution": json.loads(row["execution_json"]) if row["execution_json"] else None,
                "error": row["error"], "progress": float(row["progress"]),
                "stage": row["stage"] or row["status"],
                "stage_detail": json.loads(row["stage_detail_json"]) if row["stage_detail_json"] else None,
                "cancel_requested": bool(row["cancel_requested"]),
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    @staticmethod
    def _deployment_dependencies(db: sqlite3.Connection,
                                 deployment_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        result = {deployment_id: [] for deployment_id in deployment_ids}
        if not deployment_ids:
            return result
        placeholders = ",".join("?" for _ in deployment_ids)
        rows = db.execute(
            f"""SELECT deployment_id,dependency_key,dependency_deployment_id,
                       dependency_asset_id,dependency_revision
                FROM model_deployment_dependencies
                WHERE deployment_id IN ({placeholders})
                ORDER BY deployment_id,dependency_key""",
            deployment_ids,
        ).fetchall()
        for row in rows:
            result[row["deployment_id"]].append({
                "dependency_key": row["dependency_key"],
                "deployment_id": row["dependency_deployment_id"],
                "asset_id": row["dependency_asset_id"],
                "revision": row["dependency_revision"],
            })
        return result

    @staticmethod
    def _deployment(row: sqlite3.Row,
                    dependencies: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {
            "id": row["id"], "asset_id": row["asset_id"], "incarnation": row["incarnation"],
            "catalog_key": row["catalog_key"], "kind": row["kind"],
            "label": row["label"], "model_id": row["model_id"], "revision": row["revision"],
            "manifest_digest": row["manifest_digest"],
            "enabled": bool(row["enabled"]), "is_default": bool(row["is_default"]),
            "gpu_indices": json.loads(row["gpu_indices_json"]), "model_path": row["model_path"],
            "license": row["license"],
            "required_files": json.loads(row["required_files_json"]),
            "install_state": row["install_state"],
            "required_vram_mib": int(row["required_vram_mib"]),
            "gpu_sharing_mode": row["gpu_sharing_mode"],
            "external_reserve_mib": int(row["external_reserve_mib"]),
            "warm_ttl_seconds": int(row["warm_ttl_seconds"]),
            "desired_state": row["desired_state"], "startup_policy": row["startup_policy"],
            "actual_state": row["actual_state"], "runtime_last_error": row["runtime_last_error"],
            "current_config_revision": row["current_config_revision"],
            "pending_config_revision": row["pending_config_revision"],
            "removal_operation_id": row["removal_operation_id"],
            "service_desired_state": row["service_desired_state"],
            "service_observed_state": row["service_observed_state"],
            "runtime_updated_at": row["runtime_updated_at"],
            "dependencies": list(dependencies or []),
            "last_error": row["last_error"], "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _service_installation(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "recipe_key": row["recipe_key"], "state": row["state"],
            "current_attempt_id": row["current_attempt_id"],
            "recipe_snapshot": json.loads(row["recipe_json"]) if row["recipe_json"] else None,
            "current_step": row["current_step"], "progress": float(row["progress"]),
            "deployment_id": row["deployment_id"], "transfer_id": row["transfer_id"],
            "asset_id": row["asset_id"], "options": json.loads(row["options_json"]),
            "steps": json.loads(row["steps_json"]), "error_code": row["error_code"],
            "error_message": row["error_message"], "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
