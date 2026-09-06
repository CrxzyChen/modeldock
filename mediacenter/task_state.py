"""Durable task authority. No transport, process launch or implicit recovery.

Only trusted controller code may register instances, attest execution exit or
record sealed artifacts. Worker messages cannot provide paths or grant those
authorities. MC033 supplies the filesystem sealing implementation; this module
commits its verified metadata, never infers success from a file's existence.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Callable

from .capabilities import worker_capability_for
from .protocol import PROTOCOL, ProtocolError, validate_envelope
from .worker_common import TaskStateError, canonical, digest

SCHEMA_VERSION = 14
TERMINAL = {"succeeded", "failed", "canceled", "interrupted"}
ACTIVE = {"queued", "assigned", "running", "cancel_requested"}
CANCEL_GRACE_SECONDS = 60
TASK_COLUMNS = {
    "version": "INTEGER NOT NULL DEFAULT 1",
    "current_attempt_id": "TEXT",
    "cancel_revision": "INTEGER NOT NULL DEFAULT 0",
    "execution_mode": "TEXT NOT NULL DEFAULT 'worker'",
    "request_digest": "TEXT",
    "request_scope": "TEXT",
    "idempotency_key": "TEXT",
    "binding_json": "TEXT",
    "input_bindings_json": "TEXT NOT NULL DEFAULT '[]'",
    "lora_bindings_json": "TEXT NOT NULL DEFAULT '[]'",
    "deployment_id": "TEXT",
    "deployment_config_revision": "INTEGER",
    "migration_source": "TEXT",
}
VALIDATION_COLUMNS = {
    'policy_revision': 'INTEGER', 'operation_id': 'TEXT REFERENCES model_operations(operation_id)',
    'claim_id': 'TEXT REFERENCES instance_claims(claim_id)', 'epoch': 'TEXT', 'command_digest': 'TEXT',
    'expires_at': 'TEXT', 'cancel_revision': 'INTEGER', 'version': 'INTEGER NOT NULL DEFAULT 1',
    'expected_json': "TEXT NOT NULL DEFAULT '{}'",
}
LORA_SCHEMA = """
CREATE TABLE runtime_lora_permits (
 permit_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, revision TEXT NOT NULL,
 scope_digest TEXT NOT NULL,
 descriptor_json TEXT NOT NULL, active INTEGER NOT NULL CHECK(active IN (0,1)),
 UNIQUE(asset_id,revision,scope_digest));
"""
TRANSFER_CLEANUP_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_transfer_writes (
 transfer_id TEXT PRIMARY KEY REFERENCES model_transfers(id),
 file_id TEXT NOT NULL REFERENCES model_transfer_files(id),
 write_offset INTEGER NOT NULL, byte_count INTEGER NOT NULL,
 object_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_transfer_cleanup (
 transfer_id TEXT PRIMARY KEY REFERENCES model_transfers(id),
 asset_id TEXT NOT NULL REFERENCES model_assets(id), revision TEXT NOT NULL,
 manifest_digest TEXT NOT NULL, files_json TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','blocked','done')),
 error_code TEXT, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS model_transfer_cleanup_pending
 ON model_transfer_cleanup(state,transfer_id);
"""
SCHEMA = """
CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=14));
INSERT INTO task_kernel_metadata VALUES(14);
CREATE UNIQUE INDEX task_idempotency ON tasks(request_scope,idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE TABLE task_instances (
 instance_id TEXT NOT NULL, epoch TEXT NOT NULL, binding_json TEXT NOT NULL,
 gpu_limits_json TEXT NOT NULL, active INTEGER NOT NULL CHECK(active IN (0,1)),
 PRIMARY KEY(instance_id,epoch));
CREATE UNIQUE INDEX task_instance_active ON task_instances(instance_id) WHERE active=1;
CREATE TABLE task_attempts (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), generation INTEGER NOT NULL,
 mode TEXT NOT NULL CHECK(mode IN ('worker','local')), instance_id TEXT, epoch TEXT,
 status TEXT NOT NULL, next_event_seq INTEGER NOT NULL DEFAULT 1,
 exit_confirmed INTEGER NOT NULL DEFAULT 0, exit_evidence TEXT,
 recovery_evidence TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 execution_deadline_at TEXT, termination_reason TEXT,
 UNIQUE(task_id,generation), FOREIGN KEY(instance_id,epoch) REFERENCES task_instances(instance_id,epoch));
CREATE UNIQUE INDEX task_one_active_attempt ON task_attempts(task_id) WHERE status IN ('assigned','running','cancel_requested');
CREATE UNIQUE INDEX task_instance_single_flight ON task_attempts(instance_id) WHERE mode='worker' AND exit_confirmed=0;
CREATE TABLE task_reservations (
 reservation_id TEXT NOT NULL, generation INTEGER NOT NULL, gpu_uuid TEXT NOT NULL,
 instance_id TEXT NOT NULL, epoch TEXT NOT NULL, attempt_id TEXT REFERENCES task_attempts(id),
 kind TEXT NOT NULL CHECK(kind IN ('base','task')), mib INTEGER NOT NULL CHECK(mib>0),
 released INTEGER NOT NULL DEFAULT 0 CHECK(released IN (0,1)),
 PRIMARY KEY(reservation_id,generation,gpu_uuid),
 FOREIGN KEY(instance_id,epoch) REFERENCES task_instances(instance_id,epoch));
CREATE TABLE task_outbox (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL UNIQUE,
 task_id TEXT REFERENCES tasks(id), attempt_id TEXT REFERENCES task_attempts(id),
 envelope_json TEXT NOT NULL, digest TEXT NOT NULL, delivered_at TEXT, acknowledged_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE task_inbox (
 message_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
 attempt_id TEXT NOT NULL REFERENCES task_attempts(id), event_seq INTEGER NOT NULL,
 envelope_json TEXT NOT NULL, digest TEXT NOT NULL, result TEXT NOT NULL,
 received_at TEXT NOT NULL, UNIQUE(attempt_id,event_seq));
CREATE TABLE task_seals (
 asset_id TEXT NOT NULL, revision TEXT NOT NULL, sha256 TEXT NOT NULL,
 task_id TEXT NOT NULL REFERENCES tasks(id), attempt_id TEXT NOT NULL REFERENCES task_attempts(id),
 cancel_revision INTEGER NOT NULL, relative_path TEXT NOT NULL UNIQUE,
 byte_size INTEGER NOT NULL CHECK(byte_size>=0), execution_json TEXT NOT NULL,
 PRIMARY KEY(asset_id,revision));
CREATE TABLE task_artifacts (
 task_id TEXT PRIMARY KEY REFERENCES tasks(id), attempt_id TEXT REFERENCES task_attempts(id),
 asset_id TEXT, revision TEXT, relative_path TEXT NOT NULL UNIQUE,
 byte_size INTEGER NOT NULL CHECK(byte_size>=0), sha256 TEXT, origin TEXT NOT NULL,
 FOREIGN KEY(asset_id,revision) REFERENCES task_seals(asset_id,revision));
"""

# The runtime lifecycle belongs to the same authoritative metadata database.
# It does not turn container observations into task outcomes or release GPUs.
RUNTIME_SCHEMA_V2 = """
CREATE TABLE runtime_intents (
 intent_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, epoch TEXT NOT NULL,
 engine_id TEXT NOT NULL, container_name TEXT NOT NULL, desired_revision INTEGER NOT NULL CHECK(desired_revision>0),
 spec_json TEXT NOT NULL, spec_digest TEXT NOT NULL, image_id TEXT NOT NULL,
 mount_grants_json TEXT NOT NULL, mount_grants_digest TEXT NOT NULL, intent_digest TEXT NOT NULL,
 state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, effect_token TEXT,
 container_id TEXT, domain_json TEXT, domain_digest TEXT, exit_evidence_json TEXT, exit_evidence_digest TEXT, error_code TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(instance_id,epoch), UNIQUE(instance_id,desired_revision), UNIQUE(engine_id,container_name), UNIQUE(engine_id,container_id));
CREATE UNIQUE INDEX runtime_one_unresolved ON runtime_intents(instance_id) WHERE state!='exited';
CREATE TABLE runtime_effects (
 token TEXT PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES runtime_intents(intent_id),
 action TEXT NOT NULL CHECK(action IN ('domain','create','start','stop')),
 state TEXT NOT NULL, request_digest TEXT NOT NULL, result_digest TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(intent_id,action));
"""

# The old representation remains the explicit v1->v2 migration target. Its
# pending effect hashes are immutable history, not recomputed in schema5.
RUNTIME_SCHEMA = RUNTIME_SCHEMA_V2.replace(
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL,\n UNIQUE(instance_id,epoch), UNIQUE(instance_id,desired_revision)",
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL,\n generation INTEGER NOT NULL CHECK(generation>0), identity_version INTEGER NOT NULL CHECK(identity_version IN (1,2)), claim_id TEXT,\n UNIQUE(instance_id,epoch), UNIQUE(instance_id,generation)")

RUNTIME_REMOVAL_SCHEMA = """
CREATE TABLE runtime_container_removals (
 intent_id TEXT PRIMARY KEY REFERENCES runtime_intents(intent_id),
 engine_id TEXT NOT NULL, container_id TEXT NOT NULL, request_digest TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','unknown','removed')),
 result_digest TEXT, error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
"""

INSTANCE_SCHEMA_V3 = """
CREATE TABLE instance_policies (
 instance_id TEXT PRIMARY KEY REFERENCES model_deployments(id), incarnation TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>0), desired_state TEXT NOT NULL,
 policy_json TEXT NOT NULL, policy_digest TEXT NOT NULL, status TEXT NOT NULL,
 error_code TEXT, last_activity TEXT, version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE instance_claims (
 claim_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL REFERENCES instance_policies(instance_id),
 epoch TEXT NOT NULL, backend TEXT NOT NULL CHECK(backend IN ('container','legacy')),
 incarnation TEXT NOT NULL, revision INTEGER NOT NULL, policy_json TEXT NOT NULL, policy_digest TEXT NOT NULL,
 execution_json TEXT, execution_digest TEXT, state TEXT NOT NULL,
 registered INTEGER NOT NULL DEFAULT 0, exit_json TEXT, exit_digest TEXT,
 heartbeat_seq INTEGER NOT NULL DEFAULT 0, heartbeat_at TEXT, stop_reason TEXT, recovery_evidence TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(instance_id,epoch));
CREATE UNIQUE INDEX instance_one_backend ON instance_claims(instance_id) WHERE state!='exited';
CREATE TABLE model_operations (
 operation_id TEXT PRIMARY KEY, claim_id TEXT NOT NULL REFERENCES instance_claims(claim_id),
 desired_revision INTEGER NOT NULL, action TEXT NOT NULL CHECK(action IN ('load','unload','snapshot')),
 command_id TEXT NOT NULL UNIQUE REFERENCES task_outbox(message_id), command_digest TEXT NOT NULL,
 state TEXT NOT NULL, next_event_seq INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(claim_id,desired_revision));
CREATE TABLE instance_inbox (
 message_id TEXT PRIMARY KEY, claim_id TEXT NOT NULL REFERENCES instance_claims(claim_id),
 scope_id TEXT NOT NULL, event_seq INTEGER NOT NULL, envelope_json TEXT NOT NULL,
 digest TEXT NOT NULL, result TEXT NOT NULL, received_at TEXT NOT NULL,
 UNIQUE(claim_id,scope_id,event_seq));
CREATE TABLE instance_cancel_deadlines (
 attempt_id TEXT PRIMARY KEY REFERENCES task_attempts(id), claim_id TEXT NOT NULL REFERENCES instance_claims(claim_id),
 cancel_revision INTEGER NOT NULL, due_at TEXT NOT NULL, state TEXT NOT NULL, updated_at TEXT NOT NULL);
"""
INSTANCE_SCHEMA = INSTANCE_SCHEMA_V3.replace(
    "error_code TEXT, last_activity TEXT, version INTEGER NOT NULL DEFAULT 1);",
    """error_code TEXT, last_activity TEXT, version INTEGER NOT NULL DEFAULT 1,
 restart_recovery INTEGER NOT NULL DEFAULT 0 CHECK(restart_recovery IN (0,1)),
 configuration_state TEXT NOT NULL DEFAULT 'applied'
  CHECK(configuration_state IN ('applied','restart_pending','replace_pending','applying','failed')),
 pending_policy_json TEXT, pending_policy_digest TEXT,
 pending_restart_recovery INTEGER CHECK(pending_restart_recovery IN (0,1)),
 resume_after_apply INTEGER NOT NULL DEFAULT 0 CHECK(resume_after_apply IN (0,1)),
 configuration_error TEXT);""")


PUBLICATION_SCHEMA = """
CREATE TABLE artifact_publications (
 publication_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
 attempt_id TEXT NOT NULL REFERENCES task_attempts(id), asset_id TEXT NOT NULL, revision TEXT NOT NULL,
 descriptor_json TEXT NOT NULL, descriptor_digest TEXT NOT NULL,
 phase TEXT NOT NULL CHECK(phase IN ('intent','staged','published','committed','canceled')),
 temporary_path TEXT, object_json TEXT, error_code TEXT, copy_attempts INTEGER NOT NULL DEFAULT 0,
 retry_after REAL NOT NULL DEFAULT 0, io_failures INTEGER NOT NULL DEFAULT 0,
 UNIQUE(asset_id,revision), UNIQUE(task_id,attempt_id));
"""

# Installation, image acquisition and epoch publication share the task DB. A
# prepared image/package is not evidence that a model has loaded or generated.
INSTALLATION_RUNTIME_SCHEMA = """
CREATE TABLE runtime_release_records (
 release_digest TEXT PRIMARY KEY, release_id TEXT NOT NULL, contract_json TEXT NOT NULL,
 approved_at TEXT NOT NULL);
CREATE TABLE runtime_image_transfers (
 transfer_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES service_installations(id),
 attempt_id TEXT NOT NULL REFERENCES installation_attempts(id),
 release_digest TEXT NOT NULL REFERENCES runtime_release_records(release_digest), image_digest TEXT NOT NULL,
 phase TEXT NOT NULL CHECK(phase IN ('queued','downloading','paused','verified','import_pending','import_unknown','ready','failed','canceled')),
 received_bytes INTEGER NOT NULL DEFAULT 0 CHECK(received_bytes>=0),
 object_json TEXT, local_path TEXT NOT NULL, engine_id TEXT, result_json TEXT,
 result_digest TEXT, error_code TEXT, version INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(operation_id,release_digest));
CREATE UNIQUE INDEX runtime_image_one_unresolved ON runtime_image_transfers(engine_id,image_digest)
 WHERE engine_id IS NOT NULL AND phase IN ('import_pending','import_unknown');
CREATE TABLE runtime_image_bindings (
 engine_id TEXT NOT NULL, image_digest TEXT NOT NULL,
 release_digest TEXT NOT NULL REFERENCES runtime_release_records(release_digest),
 image_id TEXT NOT NULL, verification_json TEXT NOT NULL, verification_digest TEXT NOT NULL,
 transfer_id TEXT NOT NULL REFERENCES runtime_image_transfers(transfer_id),
 PRIMARY KEY(engine_id,image_digest));
CREATE TABLE instance_installation_bindings (
 instance_id TEXT PRIMARY KEY REFERENCES model_deployments(id), incarnation TEXT NOT NULL,
 operation_id TEXT NOT NULL REFERENCES service_installations(id),
 attempt_id TEXT NOT NULL REFERENCES installation_attempts(id),
 release_digest TEXT NOT NULL REFERENCES runtime_release_records(release_digest),
 recipe_digest TEXT NOT NULL, asset_id TEXT NOT NULL REFERENCES model_assets(id),
 asset_revision TEXT NOT NULL, asset_manifest_digest TEXT NOT NULL,
 binding_json TEXT NOT NULL, binding_digest TEXT NOT NULL,
 template_json TEXT NOT NULL, template_digest TEXT NOT NULL, installed_at TEXT NOT NULL);
CREATE TABLE runtime_epoch_packages (
 package_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL REFERENCES model_deployments(id),
 incarnation TEXT NOT NULL, epoch TEXT NOT NULL, desired_revision INTEGER NOT NULL,
 generation INTEGER NOT NULL CHECK(generation>0),
 template_digest TEXT NOT NULL, authority_digest TEXT NOT NULL,
 phase TEXT NOT NULL CHECK(phase IN ('intent','files_ready','acl_pending','acl_unknown','ready','failed')),
 record_json TEXT NOT NULL, record_digest TEXT NOT NULL, error_code TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(instance_id,epoch), UNIQUE(instance_id,incarnation,generation));
CREATE TABLE runtime_boundaries (
 boundary_id TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('sealed','outputs','journal')),
 source_path TEXT NOT NULL, object_json TEXT NOT NULL, object_digest TEXT NOT NULL,
 package_id TEXT REFERENCES runtime_epoch_packages(package_id), created_at TEXT NOT NULL,
 UNIQUE(role,source_path));
CREATE TABLE runtime_validation_records (
 validation_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL REFERENCES model_deployments(id),
 incarnation TEXT NOT NULL, binding_digest TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('env_checked','generated_tested')),
 state TEXT NOT NULL CHECK(state IN ('pending','passed','failed','unknown')),
 task_id TEXT REFERENCES tasks(id), attempt_id TEXT REFERENCES task_attempts(id),
 evidence_json TEXT, evidence_digest TEXT, error_code TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def token(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", value):
        raise TaskStateError("invalid_identity", 400)
    return value


def create_schema(db) -> None:
    for key, declaration in TASK_COLUMNS.items():
        db.execute(f"ALTER TABLE tasks ADD COLUMN {key} {declaration}")
    # Do not executescript: it silently commits an existing sqlite transaction.
    for statement in (SCHEMA + RUNTIME_SCHEMA + RUNTIME_REMOVAL_SCHEMA + INSTANCE_SCHEMA + PUBLICATION_SCHEMA + INSTALLATION_RUNTIME_SCHEMA + LORA_SCHEMA + TRANSFER_CLEANUP_SCHEMA).split(";"):
        if statement.strip():
            db.execute(statement)
    for key, declaration in VALIDATION_COLUMNS.items():
        db.execute(f'ALTER TABLE runtime_validation_records ADD COLUMN {key} {declaration}')


def upgrade_schema_v1(db) -> None:
    """Explicit offline migration only. Never called by normal startup."""
    if [row[0] for row in db.execute("SELECT version FROM task_kernel_metadata")] != [1]:
        raise TaskStateError("task_schema_upgrade_source_invalid")
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = set(re.findall(r"CREATE TABLE (\w+)", SCHEMA))
    if not expected <= tables or not (TASK_COLUMNS.keys() - {"lora_bindings_json"}) <= {row[1] for row in db.execute("PRAGMA table_info(tasks)")}:
        raise TaskStateError("task_schema_upgrade_source_invalid")
    db.execute("DROP TABLE task_kernel_metadata")
    db.execute("CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=2))")
    db.execute("INSERT INTO task_kernel_metadata VALUES(2)")
    for statement in RUNTIME_SCHEMA_V2.split(";"):
        if statement.strip():
            db.execute(statement)


def upgrade_schema_v2(db) -> None:
    """Offline v2 to v3 only. Never infer legacy process exit from metadata."""
    if [row[0] for row in db.execute("SELECT version FROM task_kernel_metadata")] != [2]:
        raise TaskStateError("task_schema_upgrade_source_invalid")
    db.execute("DROP TABLE task_kernel_metadata")
    db.execute("CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=3))")
    db.execute("INSERT INTO task_kernel_metadata VALUES(3)")
    for statement in INSTANCE_SCHEMA_V3.split(";"):
        if statement.strip():
            db.execute(statement)
    # Unknown historical loaders have no trustworthy UUID/domain mapping. A
    # durable global gate is safer than inventing a zero-byte reservation.
    for row in db.execute("SELECT id,incarnation,actual_state,desired_state FROM model_deployments"):
        status = "legacy_unreconciled" if row[2] != "unloaded" else "waiting_runtime"
        db.execute("INSERT INTO instance_policies(instance_id,incarnation,revision,desired_state,policy_json,policy_digest,status) VALUES(?,?,1,?,'null',?,?)",
                   (row[0], row[1], row[3], digest(None), status))


def upgrade_schema_v3(db) -> None:
    """Explicit offline v3 to v4. Existing works and execution are untouched."""
    if [row[0] for row in db.execute("SELECT version FROM task_kernel_metadata")] != [3]:
        raise TaskStateError("task_schema_upgrade_source_invalid")
    db.execute("DROP TABLE task_kernel_metadata")
    db.execute("CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=4))")
    db.execute("INSERT INTO task_kernel_metadata VALUES(4)")
    for statement in PUBLICATION_SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)


def upgrade_schema_v4(db) -> None:
    """Offline v4 to v5; no implicit image approval or legacy installation claim."""
    if [row[0] for row in db.execute("SELECT version FROM task_kernel_metadata")] != [4]:
        raise TaskStateError("task_schema_upgrade_source_invalid")
    db.execute("DROP TABLE task_kernel_metadata")
    db.execute("CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=5))")
    db.execute("INSERT INTO task_kernel_metadata VALUES(5)")
    # Rebuild the two related tables while FK checking stays enabled. Copy all
    # original columns verbatim, including unknown request/result/exit hashes.
    columns = [row[1] for row in db.execute("PRAGMA table_info(runtime_intents)")]
    if "generation" in columns:
        raise TaskStateError("task_schema_upgrade_source_invalid")
    db.execute("CREATE TEMP TABLE runtime_effects_history AS SELECT * FROM runtime_effects")
    definition = RUNTIME_SCHEMA.split(";")[0].replace("CREATE TABLE runtime_intents (", "CREATE TABLE runtime_intents_v5 (")
    db.execute(definition)
    names = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
    db.execute(f"INSERT INTO runtime_intents_v5({names},generation,identity_version,claim_id) SELECT {names},desired_revision,1,NULL FROM runtime_intents")
    db.execute("DROP TABLE runtime_effects")
    db.execute("DROP TABLE runtime_intents")
    db.execute("ALTER TABLE runtime_intents_v5 RENAME TO runtime_intents")
    db.execute("CREATE UNIQUE INDEX runtime_one_unresolved ON runtime_intents(instance_id) WHERE state!='exited'")
    db.execute(next(statement for statement in RUNTIME_SCHEMA.split(";") if "CREATE TABLE runtime_effects" in statement))
    db.execute("INSERT INTO runtime_effects SELECT * FROM runtime_effects_history")
    db.execute("DROP TABLE runtime_effects_history")
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='installation_resources'").fetchone():
        db.execute("""CREATE TABLE installation_resources_v5 (
            attempt_id TEXT NOT NULL REFERENCES installation_attempts(id),
            kind TEXT NOT NULL CHECK(kind IN ('deployment','transfer','asset','environment','runtime-transfer','runtime-image','runtime-binding')),
            resource_id TEXT NOT NULL, identity TEXT NOT NULL,
            created INTEGER NOT NULL CHECK(created IN (0,1)), created_at TEXT NOT NULL,
            successor_attempt_id TEXT, PRIMARY KEY(attempt_id,kind,resource_id))""")
        db.execute("INSERT INTO installation_resources_v5 SELECT attempt_id,kind,resource_id,identity,created,created_at,successor_attempt_id FROM installation_resources")
        db.execute("DROP TABLE installation_resources")
        db.execute("ALTER TABLE installation_resources_v5 RENAME TO installation_resources")
    for statement in INSTALLATION_RUNTIME_SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)


def upgrade_schema_v5(db) -> None:
    """Explicit offline upgrade; original request/attempt/effect digests stay intact."""
    if [row[0] for row in db.execute("SELECT version FROM task_kernel_metadata")] != [5]:
        raise TaskStateError("task_schema_upgrade_source_invalid")
    db.execute("ALTER TABLE tasks ADD COLUMN lora_bindings_json TEXT NOT NULL DEFAULT '[]'")
    db.execute("DROP TABLE task_kernel_metadata")
    db.execute("CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=6))")
    db.execute("INSERT INTO task_kernel_metadata VALUES(6)")
    for statement in LORA_SCHEMA.split(';'):
        if statement.strip(): db.execute(statement)
    for key, declaration in VALIDATION_COLUMNS.items():
        db.execute(f'ALTER TABLE runtime_validation_records ADD COLUMN {key} {declaration}')


def upgrade_schema_v6(db) -> None:
    """Add an operator recovery preference without rewriting policy or claim evidence."""
    if [row[0] for row in db.execute("SELECT version FROM task_kernel_metadata")] != [6]:
        raise TaskStateError("task_schema_upgrade_source_invalid")
    db.execute("ALTER TABLE instance_policies ADD COLUMN restart_recovery INTEGER NOT NULL DEFAULT 0 CHECK(restart_recovery IN (0,1))")
    db.execute("DROP TABLE task_kernel_metadata")
    db.execute("CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=7))")
    db.execute("INSERT INTO task_kernel_metadata VALUES(7)")


def upgrade_schema_v7(db) -> None:
    """Add durable, non-blocking instance configuration application state.

    Existing policies remain applied verbatim. No running claim, task, model
    asset or runtime intent is rewritten or treated as stopped by this upgrade.
    """
    if [row[0] for row in db.execute("SELECT version FROM task_kernel_metadata")] != [7]:
        raise TaskStateError("task_schema_upgrade_source_invalid")
    for statement in (
        "ALTER TABLE instance_policies ADD COLUMN configuration_state TEXT NOT NULL DEFAULT 'applied' CHECK(configuration_state IN ('applied','restart_pending','replace_pending','applying','failed'))",
        "ALTER TABLE instance_policies ADD COLUMN pending_policy_json TEXT",
        "ALTER TABLE instance_policies ADD COLUMN pending_policy_digest TEXT",
        "ALTER TABLE instance_policies ADD COLUMN pending_restart_recovery INTEGER CHECK(pending_restart_recovery IN (0,1))",
        "ALTER TABLE instance_policies ADD COLUMN resume_after_apply INTEGER NOT NULL DEFAULT 0 CHECK(resume_after_apply IN (0,1))",
        "ALTER TABLE instance_policies ADD COLUMN configuration_error TEXT",
    ):
        db.execute(statement)
    for statement in RUNTIME_REMOVAL_SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)
    db.execute("DROP TABLE task_kernel_metadata")
    db.execute("CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=8))")
    db.execute("INSERT INTO task_kernel_metadata VALUES(8)")


def upgrade_schema_v8(db) -> None:
    """Bind new tasks to an immutable deployment configuration revision.

    Historical task, attempt and execution evidence is left byte-for-byte
    unchanged.  A legacy task has no trustworthy deployment revision and
    therefore remains NULL rather than receiving an inferred identity.
    """
    if [row[0] for row in db.execute("SELECT version FROM task_kernel_metadata")] != [8]:
        raise TaskStateError("task_schema_upgrade_source_invalid")
    columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
    if "deployment_id" not in columns:
        db.execute("ALTER TABLE tasks ADD COLUMN deployment_id TEXT")
    if "deployment_config_revision" not in columns:
        db.execute("ALTER TABLE tasks ADD COLUMN deployment_config_revision INTEGER")
    db.execute("DROP TABLE task_kernel_metadata")
    db.execute("CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=9))")
    db.execute("INSERT INTO task_kernel_metadata VALUES(9)")


def upgrade_schema_v9(db) -> None:
    """Scope LoRA permits to an immutable base identity.

    Version 9 permits were global to one LoRA revision.  They cannot prove the
    base checkpoint against which compatibility was assessed, so the offline
    migration retains them as inactive audit rows and gives every future
    base-scoped permit its own unique slot.  No model asset is changed.
    """
    if [row[0] for row in db.execute("SELECT version FROM task_kernel_metadata")] != [9]:
        raise TaskStateError("task_schema_upgrade_source_invalid")
    rows = db.execute("SELECT permit_id,asset_id,revision,descriptor_json FROM runtime_lora_permits ORDER BY permit_id").fetchall()
    db.execute("""CREATE TABLE runtime_lora_permits_v10 (
        permit_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, revision TEXT NOT NULL,
        scope_digest TEXT NOT NULL, descriptor_json TEXT NOT NULL,
        active INTEGER NOT NULL CHECK(active IN (0,1)),
        UNIQUE(asset_id,revision,scope_digest))""")
    for row in rows:
        db.execute("INSERT INTO runtime_lora_permits_v10 VALUES(?,?,?,?,?,0)",
                   (row[0], row[1], row[2],
                    digest(["legacy-unscoped", row[0]]), row[3]))
    db.execute("DROP TABLE runtime_lora_permits")
    db.execute("ALTER TABLE runtime_lora_permits_v10 RENAME TO runtime_lora_permits")
    db.execute("DROP TABLE task_kernel_metadata")
    db.execute("CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=10))")
    db.execute("INSERT INTO task_kernel_metadata VALUES(10)")


def upgrade_schema_v10(db) -> None:
    """Offline copy migration: private, durable configuration rollback context."""
    if [row[0] for row in db.execute("SELECT version FROM task_kernel_metadata")] != [10]:
        raise TaskStateError("task_schema_upgrade_source_invalid")
    # Older task-only snapshots do not have PH-8 tables. Repository creates
    # those tables after migration; never synthesize an installation here.
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='deployment_operations'").fetchone():
        columns = {row[1] for row in db.execute("PRAGMA table_info(deployment_operations)")}
        for column in ("configuration_context_json", "configuration_context_digest"):
            if column not in columns:
                db.execute(f"ALTER TABLE deployment_operations ADD COLUMN {column} TEXT")
    db.execute("DROP TABLE task_kernel_metadata")
    db.execute("CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=11))")
    db.execute("INSERT INTO task_kernel_metadata VALUES(11)")


def upgrade_schema_v11(db):
    """Offline copy migration: durable per-instance removal ownership."""
    if [row[0] for row in db.execute("SELECT version FROM task_kernel_metadata")] != [11]:
        raise TaskStateError("task_schema_upgrade_source_invalid")
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='model_deployments'").fetchone():
        columns = {row[1] for row in db.execute("PRAGMA table_info(model_deployments)")}
        if "removal_operation_id" not in columns:
            db.execute("ALTER TABLE model_deployments ADD COLUMN removal_operation_id TEXT")
    db.execute("DROP TABLE task_kernel_metadata")
    db.execute("CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=12))")
    db.execute("INSERT INTO task_kernel_metadata VALUES(12)")


def upgrade_schema_v12(db):
    """Offline copy migration; never invent deadlines for historical attempts."""
    if [row[0] for row in db.execute("SELECT version FROM task_kernel_metadata")] != [12]:
        raise TaskStateError("task_schema_upgrade_source_invalid")
    columns = {row[1] for row in db.execute("PRAGMA table_info(task_attempts)")}
    for column in ("execution_deadline_at", "termination_reason"):
        if column not in columns:
            db.execute(f"ALTER TABLE task_attempts ADD COLUMN {column} TEXT")
    db.execute("DROP TABLE task_kernel_metadata")
    db.execute("CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=13))")
    db.execute("INSERT INTO task_kernel_metadata VALUES(13)")


def upgrade_schema_v13(db):
    """Explicit copy migration. Historical transfers receive NO cleanup grant."""
    if [row[0] for row in db.execute("SELECT version FROM task_kernel_metadata")] != [13]:
        raise TaskStateError("task_schema_upgrade_source_invalid")
    for statement in TRANSFER_CLEANUP_SCHEMA.split(';'):
        if statement.strip():
            db.execute(statement)
    db.execute("DROP TABLE task_kernel_metadata")
    db.execute("CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=14))")
    db.execute("INSERT INTO task_kernel_metadata VALUES(14)")


class TaskState:
    def __init__(self, repository, *, server_id: str = "mediacenter", capabilities=None,
                 fault: Callable[[str], None] | None = None,
                 cancel_grace_seconds: int = CANCEL_GRACE_SECONDS):
        if type(cancel_grace_seconds) is not int or not 1 <= cancel_grace_seconds <= 300:
            raise TaskStateError("invalid_cancel_grace")
        self.repository = repository
        self.server_id = token(server_id)
        self.capabilities = capabilities
        self.cancel_grace_seconds = cancel_grace_seconds
        self.fault = fault or (lambda _point: None)
        with repository._connect() as db:
            if db.execute("SELECT version FROM task_kernel_metadata").fetchone()[0] != SCHEMA_VERSION:
                raise TaskStateError("task_schema_unsupported")

    @staticmethod
    def _audit(db, action, task_id, detail=None):
        db.execute("INSERT INTO audit_events(occurred_at,action,target,detail_json) VALUES(?,?,?,?)",
                   (now(), action, task_id, canonical(detail or {})))

    @staticmethod
    def _task(db, task_id, version=None):
        if version is not None and (type(version) is not int or version < 1):
            raise TaskStateError("invalid_task_version", 400)
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            raise TaskStateError("task_not_found", 404)
        if version is not None and task["version"] != version:
            raise TaskStateError("task_version_conflict")
        if task["migration_source"] and task["status"] not in TERMINAL:
            raise TaskStateError("legacy_execution_unreconciled")
        return task

    def lookup(self, scope: str, key: str | None, request: dict) -> dict | None:
        token(scope)
        if key is None:
            return None
        token(key)
        with self.repository._connect() as db:
            row = db.execute("SELECT id,request_digest FROM tasks WHERE request_scope=? AND idempotency_key=?",
                             (scope, key)).fetchone()
        if row and row["request_digest"] != digest(request):
            raise TaskStateError("idempotency_conflict")
        return self.repository.get_task(row["id"]) if row else None

    def accept(self, request: dict, *, scope: str, key: str | None = None,
               binding: dict | None = None, mode: str = "worker", input_bindings: list | None = None,
               lora_bindings: list | None = None, validation: dict | None = None,
               deployment_id: str | None = None,
               deployment_config_revision: int | None = None) -> tuple[dict, bool]:
        token(scope)
        if key is not None:
            token(key)
        if (deployment_id is None) != (deployment_config_revision is None):
            raise TaskStateError("deployment_revision_binding_invalid", 400)
        if deployment_id is not None:
            token(deployment_id)
            if (type(deployment_config_revision) is not int
                    or deployment_config_revision < 1):
                raise TaskStateError("deployment_revision_binding_invalid", 400)
        if mode not in {"worker", "local"}:
            raise TaskStateError("invalid_execution_mode", 400)
        fingerprint = digest(request)
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_loras(db, lora_bindings or [], binding=binding)
            if [{k: item[k] for k in ('asset_id', 'revision', 'family', 'weight')} for item in (lora_bindings or [])] != request.get('loras', []):
                raise TaskStateError('lora_binding_mismatch')
            previous = db.execute("SELECT id,request_digest FROM tasks WHERE request_scope=? AND idempotency_key=?",
                                  (scope, key)).fetchone() if key is not None else None
            if previous:
                if previous["request_digest"] != fingerprint:
                    raise TaskStateError("idempotency_conflict")
                task_id, created = previous["id"], False
            else:
                if mode == "worker":
                    self.repository.assert_not_removing_tx(db, request["model"])
                service = db.execute("SELECT enabled FROM services WHERE kind=?", (request["service"],)).fetchone()
                if not service or not service["enabled"]:
                    raise TaskStateError("service_disabled")
                if deployment_id is not None:
                    deployment = db.execute(
                        """SELECT enabled,install_state,current_config_revision,
                                  pending_config_revision
                           FROM model_deployments WHERE id=?""",
                        (deployment_id,),
                    ).fetchone()
                    policy = db.execute(
                        """SELECT configuration_state FROM instance_policies
                           WHERE instance_id=?""", (deployment_id,),
                    ).fetchone()
                    if (deployment is None or not deployment["enabled"]
                            or deployment["install_state"] != "ready"
                            or deployment["current_config_revision"]
                                != deployment_config_revision
                            or deployment["pending_config_revision"] is not None
                            or policy is None
                            or policy["configuration_state"] != "applied"):
                        raise TaskStateError("deployment_not_ready")
                task_id, timestamp = "tsk_" + secrets.token_hex(16), now()
                db.execute("""INSERT INTO tasks(id,service,model_key,status,prompt,options_json,inputs_json,
                 created_at,updated_at,request_scope,idempotency_key,request_digest,binding_json,execution_mode,
                 input_bindings_json,lora_bindings_json,deployment_id,deployment_config_revision)
                 VALUES(?,?,?,'queued',?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (task_id, request["service"], request["model"], request["prompt"], canonical(request.get("options", {})),
                  canonical(request.get("inputs", [])), timestamp, timestamp, scope, key, fingerprint,
                  canonical(binding) if binding else None, mode, canonical(input_bindings or []),
                  canonical(lora_bindings or []), deployment_id, deployment_config_revision))
                self.fault("accept.task")
                if validation is not None:
                    from .runtime_provisioning import register_task_validation
                    register_task_validation(db, task_id, validation)
                self._audit(db, "task.create", task_id, {"service": request["service"], "model": request["model"]})
                self.fault("accept.audit")
                if mode == "local":
                    self._begin_local(db, task_id, 1)
                created = True
        return self.repository.get_task(task_id), created

    @staticmethod
    def _check_loras(db, values, *, binding=None):
        from .asset_compatibility import AssetCompatibilityError, AssetCompatibilityManager, CURRENT_DETECTOR_VERSION
        if type(values) is not list or len(values) > 1:
            raise TaskStateError('invalid_lora_bindings')
        for item in values:
            if type(item) is not dict or set(item) != {'asset_id', 'revision', 'family', 'weight', 'permit_id'}:
                raise TaskStateError('invalid_lora_bindings')
            if type(item['weight']) not in (int,float) or not math.isfinite(item['weight']) or not -2 <= item['weight'] <= 2:
                raise TaskStateError('invalid_lora_weight')
            row = db.execute('SELECT * FROM runtime_lora_permits WHERE permit_id=? AND active=1', (item['permit_id'],)).fetchone()
            if not row:
                raise TaskStateError('lora_not_approved')
            value = json.loads(row['descriptor_json'])
            asset = db.execute('SELECT * FROM model_assets WHERE id=?', (item['asset_id'],)).fetchone()
            files = [dict(r) for r in db.execute('SELECT relative_path,sha256,byte_size FROM model_asset_files WHERE asset_id=? ORDER BY relative_path', (item['asset_id'],))]
            try:
                compatibility = AssetCompatibilityManager.require_in_transaction(
                    db, item['asset_id'], item['revision'], value.get('base_asset_id'),
                    value.get('base_revision'))
            except AssetCompatibilityError as exc:
                raise TaskStateError(exc.code) from None
            base_matches = (binding is not None and
                binding.get('model_asset_id') == value.get('base_asset_id') and
                binding.get('model_asset_revision') == value.get('base_revision') and
                binding.get('recipe_revision') == value.get('runtime_profile_digest'))
            if (digest(value) != row['permit_id'] or any(value[k] != item[k] for k in ('asset_id', 'revision', 'family'))
                    or not asset or asset['state'] != 'ready' or asset['role'] != 'lora' or asset['format'] != 'safetensors'
                    or asset['media_kind'] != 'image' or asset['revision'] != value['revision']
                    or asset['manifest_digest'] != value['manifest_digest'] or digest(asset['license_declared']) != value['license_digest']
                    or row['scope_digest'] != digest([
                        value.get('base_asset_id'), value.get('base_revision'),
                        value.get('runtime_profile_digest')])
                    or value.get('compatibility_detector_version') != CURRENT_DETECTOR_VERSION
                    or not base_matches or compatibility is None
                    or compatibility['verdict'] not in {'exact', 'compatible'}
                    or compatibility['evidence_digest'] != value.get('compatibility_evidence_digest')
                    or files != sorted(value['files'],key=lambda v:v['relative_path'])):
                raise TaskStateError('lora_asset_changed')

    def register_instance(self, instance_id: str, epoch: str, binding: dict, gpu_limits: dict[str, int]) -> None:
        """Internal attestation from the future runtime controller, not an HTTP endpoint."""
        token(instance_id); token(epoch)
        if type(binding) is not dict or set(binding) != {"model_key", "recipe_revision", "model_asset_id", "model_asset_revision", "dependencies"}:
            raise TaskStateError("invalid_instance_binding", 400)
        for field in ("model_key", "recipe_revision", "model_asset_id", "model_asset_revision"):
            token(binding.get(field))
        if type(binding["dependencies"]) is not list:
            raise TaskStateError("invalid_dependency_binding", 400)
        for dependency in binding["dependencies"]:
            if type(dependency) is not dict or set(dependency) != {"dependency_key", "deployment_id", "asset_id", "revision"}:
                raise TaskStateError("invalid_dependency_binding", 400)
            for value in dependency.values():
                token(value)
        if type(gpu_limits) is not dict or not gpu_limits or any(type(key) is not str or not re.fullmatch(r"GPU-[A-Za-z0-9-]+", key)
                                 or type(value) is not int or value <= 0 for key, value in gpu_limits.items()):
            raise TaskStateError("invalid_gpu_capacity", 400)
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            from .instance_policy import InstancePolicy
            claim = InstancePolicy.assert_claim(db, instance_id, epoch)
            if claim["policy"]["binding"] != binding or set(claim["policy"]["gpus"]) != set(gpu_limits):
                raise TaskStateError("instance_binding_conflict")
            old = db.execute("SELECT * FROM task_instances WHERE instance_id=? AND epoch=?", (instance_id, epoch)).fetchone()
            if old:
                if old["binding_json"] != canonical(binding) or old["gpu_limits_json"] != canonical(gpu_limits):
                    raise TaskStateError("instance_binding_conflict")
                return
            db.execute("UPDATE task_instances SET active=0 WHERE instance_id=?", (instance_id,))
            db.execute("INSERT INTO task_instances VALUES(?,?,?,?,1)",
                       (instance_id, epoch, canonical(binding), canonical(gpu_limits)))

    def _envelope(self, kind, instance, epoch, payload, *, task_id=None, attempt_id=None):
        timestamp = datetime.now(timezone.utc)
        message = {"protocol": PROTOCOL, "type": kind, "message_id": "msg_" + secrets.token_hex(16),
                   "server_id": self.server_id, "instance_id": instance, "worker_epoch": epoch,
                   "correlation_id": task_id or "instance", "created_at": timestamp.isoformat().replace("+00:00", "Z"),
                   "expires_at": (timestamp + timedelta(hours=1)).isoformat().replace("+00:00", "Z"), "payload": payload}
        if kind in {"task.execute", "task.cancel"}:
            message.update(task_id=task_id, attempt_id=attempt_id)
        return validate_envelope(message, capabilities=self.capabilities)

    def _outbox(self, db, message, task_id, attempt_id):
        db.execute("INSERT INTO task_outbox(message_id,task_id,attempt_id,envelope_json,digest,delivered_at,acknowledged_at,created_at) VALUES(?,?,?,?,?,NULL,NULL,?)",
                   (message["message_id"], task_id, attempt_id, canonical(message), digest(message), now()))

    def _reserve(self, db, instance, epoch, reservation_id, generation, gpus, *, attempt_id=None):
        record = db.execute("SELECT * FROM task_instances WHERE instance_id=? AND epoch=? AND active=1",
                            (instance, epoch)).fetchone()
        if record is None:
            raise TaskStateError("instance_not_current")
        limits = json.loads(record["gpu_limits_json"])
        if not gpus or set(gpus) != set(limits):
            raise TaskStateError("invalid_gpu_reservation")
        for gpu, mib in gpus.items():
            if type(mib) is not int or mib <= 0:
                raise TaskStateError("invalid_gpu_reservation")
            reserved = db.execute("SELECT COALESCE(SUM(mib),0) FROM task_reservations WHERE gpu_uuid=? AND released=0", (gpu,)).fetchone()[0]
            if reserved + mib > limits[gpu]:
                raise TaskStateError("gpu_capacity_unavailable")
            db.execute("INSERT INTO task_reservations VALUES(?,?,?,?,?,?,?,?,0)",
                       (reservation_id, generation, gpu, instance, epoch, attempt_id, "task" if attempt_id else "base", mib))
            self.fault("dispatch.reservation")
        return record

    def reserve_base(self, instance, epoch, reservation_id, generation, gpus):
        token(reservation_id)
        if type(generation) is not int or generation < 1:
            raise TaskStateError("invalid_generation")
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            from .instance_policy import InstancePolicy
            InstancePolicy.assert_claim(db, instance, epoch)
            existing = db.execute("SELECT gpu_uuid,mib FROM task_reservations WHERE instance_id=? AND epoch=? AND reservation_id=? AND generation=? AND kind='base' AND released=0", (instance, epoch, reservation_id, generation)).fetchall()
            if {row[0]: row[1] for row in existing} != gpus:
                raise TaskStateError("base_reservation_requires_claim_transaction")

    def dispatch(self, task_id: str, version: int, instance: str, epoch: str, gpus: dict[str, int], *, observe_capacity) -> dict:
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            from .instance_policy import InstancePolicy
            self.repository.assert_not_removing_tx(db, instance)
            claim = InstancePolicy.assert_claim(db, instance, epoch, backend="container", loaded=True)
            policy = db.execute("SELECT * FROM instance_policies WHERE instance_id=?", (instance,)).fetchone()
            if policy["configuration_state"] != "applied":
                raise TaskStateError("deployment_configuration_pending")
            if policy["desired_state"] != "loaded" or policy["status"] != "loaded":
                raise TaskStateError("instance_not_loaded")
            if gpus != {gpu: claim["policy"]["task_mib"] for gpu in claim["policy"]["gpus"]}:
                raise TaskStateError("task_budget_mismatch")
            fresh_limits = InstancePolicy.admit_capacity(db, instance, claim["policy"], observe_capacity, task=True)
            task = self._task(db, task_id, version)
            if task["status"] != "queued" or task["execution_mode"] != "worker":
                raise TaskStateError("task_not_dispatchable")
            if db.execute("SELECT 1 FROM task_attempts WHERE task_id=? AND exit_confirmed=0", (task_id,)).fetchone():
                raise TaskStateError("execution_exit_unconfirmed")
            if db.execute("SELECT 1 FROM task_attempts WHERE instance_id=? AND mode='worker' AND exit_confirmed=0", (instance,)).fetchone():
                raise TaskStateError("instance_execution_unconfirmed")
            binding = json.loads(task["binding_json"] or "null")
            record = db.execute("SELECT * FROM task_instances WHERE instance_id=? AND epoch=? AND active=1", (instance, epoch)).fetchone()
            if record is None or binding is None or record["binding_json"] != canonical(binding):
                raise TaskStateError("instance_binding_conflict")
            if task["deployment_id"] is not None:
                deployment = db.execute(
                    "SELECT current_config_revision,pending_config_revision FROM model_deployments WHERE id=?",
                    (task["deployment_id"],),
                ).fetchone()
                revision = db.execute(
                    """SELECT 1 FROM model_deployment_revisions
                       WHERE deployment_id=? AND config_revision=?""",
                    (task["deployment_id"], task["deployment_config_revision"]),
                ).fetchone()
                if (task["deployment_id"] != instance or deployment is None
                        or deployment["pending_config_revision"] is not None
                        or deployment["current_config_revision"] != task["deployment_config_revision"]
                        or revision is None):
                    raise TaskStateError("deployment_config_revision_changed")
            if any(value > fresh_limits[gpu] for gpu, value in json.loads(record["gpu_limits_json"]).items()):
                raise TaskStateError("gpu_capacity_contract_changed")
            generation = db.execute("SELECT COALESCE(MAX(generation),0)+1 FROM task_attempts WHERE task_id=?", (task_id,)).fetchone()[0]
            attempt, reservation, timestamp = "att_" + secrets.token_hex(16), "res_" + secrets.token_hex(16), now()
            service = db.execute("SELECT timeout_seconds FROM services WHERE kind=?", (task['service'],)).fetchone()
            if service is None or type(service['timeout_seconds']) is not int or not 10 <= service['timeout_seconds'] <= 7200:
                raise TaskStateError('invalid_execution_timeout')
            deadline = (datetime.fromisoformat(timestamp) + timedelta(seconds=service['timeout_seconds'])).isoformat()
            db.execute("INSERT INTO task_attempts(id,task_id,generation,mode,instance_id,epoch,status,created_at,updated_at,execution_deadline_at) VALUES(?,?,?,'worker',?,?,'assigned',?,?,?)",
                       (attempt, task_id, generation, instance, epoch, timestamp, timestamp, deadline))
            self.fault("dispatch.attempt")
            self._reserve(db, instance, epoch, reservation, generation, gpus, attempt_id=attempt)
            model = {key: binding[key] for key in ("model_key", "recipe_revision", "model_asset_id", "model_asset_revision")}
            capability = worker_capability_for(model["model_key"]) if self.capabilities is None else self.capabilities[model["model_key"]]
            loras = json.loads(task['lora_bindings_json'])
            self._check_loras(db, loras, binding=binding)
            payload = {**model, "reservation_id": reservation, "reservation_generation": generation,
                       "operation": capability["operation"],
                       "parameters": {**json.loads(task["options_json"]), "prompt": task["prompt"]},
                       "inputs": json.loads(task["input_bindings_json"]),
                       "loras": [{k: item[k] for k in ('asset_id','revision','family','weight')} for item in loras]}
            message = self._envelope("task.execute", instance, epoch, payload, task_id=task_id, attempt_id=attempt)
            db.execute("UPDATE runtime_validation_records SET attempt_id=?,claim_id=?,epoch=?,command_digest=?,cancel_revision=?,updated_at=? WHERE task_id=? AND state='pending' AND attempt_id IS NULL",
                       (attempt, claim['claim_id'], epoch, digest(message), task['cancel_revision'], timestamp, task_id))
            db.execute("UPDATE tasks SET status='assigned',stage='assigned',current_attempt_id=?,version=version+1,updated_at=? WHERE id=?",
                       (attempt, timestamp, task_id))
            self.fault("dispatch.task")
            self._outbox(db, message, task_id, attempt)
            self.fault("dispatch.outbox")
            self._audit(db, "task.dispatch", task_id, {"attempt_id": attempt})
        return message

    def cancel(self, task_id: str, version: int | None = None) -> dict:
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._task(db, task_id, version)
            attempt = db.execute('SELECT * FROM task_attempts WHERE id=?', (task['current_attempt_id'],)).fetchone()
            reason = 'task_timed_out' if self._deadline_due(attempt) else 'user_canceled'
            self._cancel_tx(db, task, reason)
        return self.repository.get_task(task_id)

    @staticmethod
    def _deadline_due(attempt, at=None):
        if (attempt is None or attempt['mode'] != 'worker' or attempt['status'] in TERMINAL
                or attempt['execution_deadline_at'] is None or attempt['termination_reason'] is not None):
            return False
        current = at if at is not None else datetime.fromisoformat(now())
        return datetime.fromisoformat(attempt['execution_deadline_at']) <= current

    def _cancel_tx(self, db, task, reason):
        """First termination intent wins in the same transaction as its outbox."""
        if task['status'] in TERMINAL or task['status'] == 'cancel_requested':
            return False
        revision, timestamp = task['cancel_revision'] + 1, now()
        state = 'canceled' if task['status'] == 'queued' else 'cancel_requested'
        timeout = reason == 'task_timed_out'
        db.execute("UPDATE tasks SET status=?,stage=?,error=?,cancel_requested=1,cancel_revision=?,version=version+1,updated_at=? WHERE id=?",
                   (state, 'timeout_stopping' if timeout else state, reason if timeout else task['error'], revision, timestamp, task['id']))
        self.fault('cancel.task')
        if state == 'cancel_requested':
            attempt = db.execute('SELECT * FROM task_attempts WHERE id=?', (task['current_attempt_id'],)).fetchone()
            db.execute("UPDATE task_attempts SET status='cancel_requested',termination_reason=?,updated_at=? WHERE id=?",
                       (reason, timestamp, attempt['id']))
            if attempt['mode'] == 'worker':
                message = self._envelope('task.cancel', attempt['instance_id'], attempt['epoch'],
                    {'cancel_revision': revision}, task_id=task['id'], attempt_id=attempt['id'])
                self._outbox(db, message, task['id'], attempt['id'])
                from .instance_policy import InstancePolicy
                claim = InstancePolicy.assert_claim(db, attempt['instance_id'], attempt['epoch'])
                due = (datetime.fromisoformat(timestamp) + timedelta(seconds=self.cancel_grace_seconds)).isoformat()
                db.execute("INSERT INTO instance_cancel_deadlines VALUES(?,?,?,?,'waiting',?)",
                           (attempt['id'], claim['claim_id'], revision, due, timestamp))
        self.fault('cancel.outbox')
        self._audit(db, 'task.timeout' if timeout else 'task.cancel', task['id'], {'cancel_revision':revision})
        return True

    def _expire_tx(self, db, task, attempt, at=None):
        if task['current_attempt_id'] == attempt['id'] and self._deadline_due(attempt, at):
            return self._cancel_tx(db, task, 'task_timed_out')
        return False

    def expire_due_tasks(self, *, at=None):
        """Bounded durable scan, no broker/Engine calls or steady-state writes."""
        at = at if at is not None else datetime.fromisoformat(now())
        if not isinstance(at, datetime) or at.tzinfo is None:
            raise TaskStateError('invalid_deadline_clock')
        with self.repository._connect() as db:
            rows = db.execute("""SELECT a.id,a.task_id FROM task_attempts a JOIN tasks t
                ON t.current_attempt_id=a.id AND t.id=a.task_id
                WHERE a.mode='worker' AND a.status IN ('assigned','running','cancel_requested')
                AND t.status IN ('assigned','running') AND a.termination_reason IS NULL
                AND julianday(a.execution_deadline_at)<=julianday(?)
                ORDER BY a.execution_deadline_at,a.id LIMIT 100""", (at.isoformat(),)).fetchall()
        expired = []
        for row in rows:
            with self.repository._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                task = self._task(db, row['task_id'])
                attempt = db.execute('SELECT * FROM task_attempts WHERE id=?', (row['id'],)).fetchone()
                if self._expire_tx(db, task, attempt, at):
                    expired.append(task['id'])
        return expired

    def authorize_recovery(self, task_id: str, attempt_id: str, epoch: str, evidence: str) -> None:
        """Trusted reconciler authorizes original-journal delivery; never creates an attempt."""
        token(evidence)
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._task(db, task_id)
            row = db.execute("SELECT * FROM task_attempts WHERE id=? AND task_id=? AND epoch=?", (attempt_id, task_id, epoch)).fetchone()
            if row is None or task["current_attempt_id"] != attempt_id or row["exit_confirmed"]:
                raise TaskStateError("recovery_not_eligible")
            if row["recovery_evidence"] not in {None, evidence}:
                raise TaskStateError("recovery_evidence_conflict")
            db.execute("UPDATE task_attempts SET recovery_evidence=? WHERE id=?", (evidence, attempt_id))
            self._drain(db, attempt_id)

    def receive(self, envelope: dict) -> str:
        message = validate_envelope(envelope, capabilities=self.capabilities)
        if message["type"] not in {"task.accepted", "phase.changed", "task.terminal"} or "task_id" not in message:
            raise TaskStateError("unsupported_task_event", 400)
        if message["server_id"] != self.server_id:
            raise TaskStateError("wrong_server")
        identity, fingerprint = message["message_id"], digest(message)
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM task_inbox WHERE message_id=?", (identity,)).fetchone()
            if prior:
                if prior["digest"] != fingerprint:
                    raise TaskStateError("event_identity_conflict")
                self._drain(db, prior["attempt_id"])
                return db.execute("SELECT result FROM task_inbox WHERE message_id=?", (identity,)).fetchone()[0]
            attempt = db.execute("SELECT * FROM task_attempts WHERE id=? AND task_id=?", (message["attempt_id"], message["task_id"])).fetchone()
            if (attempt is None or attempt["mode"] != "worker" or attempt["instance_id"] != message["instance_id"]
                    or attempt["epoch"] != message["worker_epoch"]):
                raise TaskStateError("event_attempt_mismatch")
            instance = db.execute("SELECT active FROM task_instances WHERE instance_id=? AND epoch=?", (attempt["instance_id"], attempt["epoch"])).fetchone()
            if not instance["active"] and not attempt["recovery_evidence"]:
                raise TaskStateError("old_epoch_unreconciled")
            self._accepted_reservation(db, attempt, message)
            if db.execute("SELECT 1 FROM task_inbox WHERE attempt_id=? AND event_seq=?", (attempt["id"], message["event_seq"])).fetchone():
                raise TaskStateError("event_sequence_conflict")
            db.execute("INSERT INTO task_inbox VALUES(?,?,?,?,?,?,?,?)", (identity, message["task_id"], attempt["id"], message["event_seq"], canonical(message), fingerprint, "pending", now()))
            self.fault("event.inbox")
            self._drain(db, attempt["id"])
            return db.execute("SELECT result FROM task_inbox WHERE message_id=?", (identity,)).fetchone()[0]

    def _accepted_reservation(self, db, attempt, message):
        # This binding is independent of event order. Do not persist/ACK a
        # poison seq-N event and discover its bad reservation only after N-1.
        if message["type"] == "task.accepted":
            reserved = db.execute("SELECT 1 FROM task_reservations WHERE attempt_id=? AND kind='task' AND reservation_id=? AND generation=?",
                                  (attempt["id"], message["payload"]["reservation_id"], message["payload"]["reservation_generation"])).fetchone()
            if not reserved:
                raise TaskStateError("reservation_identity_conflict")

    def _drain(self, db, attempt_id):
        try:
            self._drain_pending(db, attempt_id)
        except (TaskStateError, ProtocolError, json.JSONDecodeError, KeyError, TypeError):
            # Any failure here belongs to stored inbox/state, not necessarily
            # the delivery which triggered draining (receive/seal/recovery).
            # Never let transport quarantine/ACK the innocent current event.
            raise TaskStateError("inbox_integrity_error") from None

    def _drain_pending(self, db, attempt_id):
        while True:
            attempt = db.execute("SELECT * FROM task_attempts WHERE id=?", (attempt_id,)).fetchone()
            if attempt["mode"] == "worker":
                instance = db.execute("SELECT active FROM task_instances WHERE instance_id=? AND epoch=?", (attempt["instance_id"], attempt["epoch"])).fetchone()
                if not instance or not instance["active"] and not attempt["recovery_evidence"]:
                    return  # Re-check on every drain, including seal completion and duplicate delivery.
            row = db.execute("SELECT * FROM task_inbox WHERE attempt_id=? AND event_seq=? AND result='pending'",
                             (attempt_id, attempt["next_event_seq"])).fetchone()
            if row is None:
                return
            message = validate_envelope(json.loads(row["envelope_json"]), capabilities=self.capabilities)
            if (digest(message) != row["digest"] or message["message_id"] != row["message_id"]
                    or message["event_seq"] != row["event_seq"] or message["task_id"] != attempt["task_id"]
                    or message["attempt_id"] != attempt_id or message["instance_id"] != attempt["instance_id"]
                    or message["worker_epoch"] != attempt["epoch"] or message["server_id"] != self.server_id):
                raise TaskStateError("stored_event_identity_conflict")
            self._accepted_reservation(db, attempt, message)
            task = self._task(db, attempt["task_id"])
            outcome = "applied"
            if task["current_attempt_id"] != attempt_id or attempt["status"] in TERMINAL:
                outcome = "stale"
            else:
                if self._expire_tx(db, task, attempt):
                    task = self._task(db, task['id'])
                    attempt = db.execute('SELECT * FROM task_attempts WHERE id=?', (attempt_id,)).fetchone()
            if outcome == 'applied' and message["type"] == "task.terminal":
                if (attempt['termination_reason'] == 'task_timed_out'
                        and not message.get('extensions', {}).get('execution_quiescence')
                        and not attempt['recovery_evidence']):
                    return  # Keep the event pending until original-domain exit is proved.
                payload = message["payload"]
                state = "canceled" if task["cancel_requested"] else payload["status"]
                seal = None
                if state == "succeeded":
                    manifest = payload["manifest"]
                    seal = db.execute("SELECT * FROM task_seals WHERE asset_id=? AND revision=? AND sha256=? AND task_id=? AND attempt_id=? AND cancel_revision=?",
                                      (manifest["asset_id"], manifest["revision"], manifest["sha256"], task["id"], attempt_id, task["cancel_revision"])).fetchone()
                    if seal is None:
                        return  # Journal remains pending until trusted sealing, no premature receipt.
                self._terminal(db, task, attempt, state, seal, payload["error_code"])
                self._quiescence(db, task, attempt, message)
                self.fault("event.terminal")
            elif outcome == 'applied':
                state = "cancel_requested" if task["cancel_requested"] else "running"
                stage = (task['stage'] if task['cancel_requested'] else message["payload"].get("phase", "accepted"))
                db.execute("UPDATE tasks SET status=?,stage=?,version=version+1,updated_at=? WHERE id=?", (state, stage, now(), task["id"]))
                db.execute("UPDATE task_attempts SET status=?,updated_at=? WHERE id=?", (state, now(), attempt_id))
            if outcome == "applied" and message["type"] in {"task.accepted", "task.terminal"}:
                for command in db.execute("SELECT message_id,envelope_json FROM task_outbox WHERE attempt_id=? AND acknowledged_at IS NULL", (attempt_id,)).fetchall():
                    kind = json.loads(command["envelope_json"])["type"]
                    if kind == "task.execute" or kind == "task.cancel" and message["type"] == "task.terminal":
                        db.execute("UPDATE task_outbox SET acknowledged_at=? WHERE message_id=?", (now(), command["message_id"]))
            receipt = self._envelope("event.receipt", attempt["instance_id"], attempt["epoch"],
                                     {"subject": "task", "task_id": task["id"], "attempt_id": attempt_id,
                                      "event_message_id": row["message_id"], "event_seq": row["event_seq"]},
                                     task_id=task["id"], attempt_id=attempt_id)
            receipt["message_id"] = self._receipt_id(row["message_id"])
            self._outbox(db, receipt, task["id"], attempt_id)
            self.fault("event.receipt")
            db.execute("UPDATE task_inbox SET result=? WHERE message_id=?", (outcome, row["message_id"]))
            db.execute("UPDATE task_attempts SET next_event_seq=next_event_seq+1 WHERE id=?", (attempt_id,))
            self.fault("event.applied")

    def _terminal(self, db, task, attempt, state, seal=None, error=None, *, audit_action="task.terminal"):
        if attempt['termination_reason'] == 'task_timed_out':
            state, seal, error = 'failed', None, 'task_timed_out'
        output = None
        execution = None
        if state == "succeeded":
            if seal is None:
                raise TaskStateError("trusted_manifest_required")
            output = {"artifact_path": seal["relative_path"], "artifact_url": "/api/v1/artifacts/" + seal["relative_path"],
                      "bytes": seal["byte_size"], "asset_id": seal["asset_id"], "revision": seal["revision"], "sha256": seal["sha256"]}
            execution = seal["execution_json"]
            db.execute("INSERT INTO task_artifacts VALUES(?,?,?,?,?,?,?,?)",
                       (task["id"], attempt["id"], seal["asset_id"], seal["revision"], seal["relative_path"], seal["byte_size"], seal["sha256"], "sealed"))
            self.fault("event.manifest")
        db.execute("""UPDATE tasks SET status=?,stage=?,version=version+1,progress=?,updated_at=?,error=?,
         output_json=?,execution_json=?,artifact_path=?,artifact_bytes=? WHERE id=?""",
         (state, "completed" if state == "succeeded" else state, 1.0 if state == "succeeded" else task["progress"], now(),
          error if state != "succeeded" else None, canonical(output) if output else None, execution,
          output["artifact_path"] if output else None, output["bytes"] if output else None, task["id"]))
        db.execute("UPDATE task_attempts SET status=?,updated_at=? WHERE id=?", (state, now(), attempt["id"]))
        self._audit(db, audit_action, task["id"], {"attempt_id": attempt["id"], "status": state})

    def _quiescence(self, db, task, attempt, message):
        proof = message.get("extensions", {}).get("execution_quiescence")
        if proof is None:
            return
        command = db.execute("SELECT * FROM task_outbox WHERE message_id=? AND task_id=? AND attempt_id=?",
                             (proof["command_message_id"], task["id"], attempt["id"])).fetchone()
        if not command or command["digest"] != proof["command_digest"] or digest(json.loads(command["envelope_json"])) != command["digest"]:
            raise TaskStateError("quiescence_command_mismatch")
        original = validate_envelope(json.loads(command["envelope_json"]), capabilities=self.capabilities)
        if original["type"] != "task.execute" or original["instance_id"] != attempt["instance_id"] or original["worker_epoch"] != attempt["epoch"]:
            raise TaskStateError("quiescence_command_mismatch")
        from .instance_policy import InstancePolicy
        InstancePolicy.assert_claim(db, attempt["instance_id"], attempt["epoch"], backend="container")
        db.execute("UPDATE task_attempts SET exit_confirmed=1,exit_evidence=? WHERE id=?", ("quiescent-" + digest(proof), attempt["id"]))
        db.execute("UPDATE task_reservations SET released=1 WHERE attempt_id=? AND kind='task'", (attempt["id"],))
        db.execute("UPDATE instance_cancel_deadlines SET state='completed',updated_at=? WHERE attempt_id=?", (now(), attempt["id"]))
        db.execute("UPDATE instance_policies SET last_activity=? WHERE instance_id=?", (now(), attempt["instance_id"]))
        self.fault("quiescence.release")

    def record_sealed_artifact(self, task_id, attempt_id, *, cancel_revision, asset_id, revision,
                               sha256, relative_path, byte_size, execution=None, _connection=None):
        """Accept only controller-verified sealing metadata; never exposed to Worker/HTTP."""
        token(asset_id); token(revision)
        if type(relative_path) is not str:
            raise TaskStateError("invalid_artifact_path", 400)
        path = PurePosixPath(relative_path)
        if (path.is_absolute() or ".." in path.parts or "\\" in relative_path
                or ":" in relative_path or str(path) != relative_path or not path.parts):
            raise TaskStateError("invalid_artifact_path", 400)
        if type(byte_size) is not int or byte_size < 0 or type(sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise TaskStateError("invalid_manifest", 400)
        values = (asset_id, revision, sha256, task_id, attempt_id, cancel_revision, relative_path, byte_size, canonical(execution or {}))
        from contextlib import nullcontext
        with (nullcontext(_connection) if _connection is not None else self.repository._connect()) as db:
            if _connection is None:
                db.execute("BEGIN IMMEDIATE")
            task = self._task(db, task_id)
            attempt = db.execute('SELECT * FROM task_attempts WHERE id=?', (attempt_id,)).fetchone()
            if self._deadline_due(attempt):
                raise TaskStateError('seal_attempt_timed_out')
            if task["current_attempt_id"] != attempt_id or task["cancel_revision"] != cancel_revision or task["cancel_requested"]:
                raise TaskStateError("seal_attempt_canceled_or_stale")
            old = db.execute("SELECT * FROM task_seals WHERE asset_id=? AND revision=?", (asset_id, revision)).fetchone()
            if old:
                if tuple(old) != values:
                    raise TaskStateError("manifest_identity_conflict")
                return
            if task["status"] in TERMINAL:
                raise TaskStateError("task_terminal")
            db.execute("INSERT INTO task_seals VALUES(?,?,?,?,?,?,?,?,?)", values)
            self.fault("seal.record")
            self._drain(db, attempt_id)

    def _record_exit_confirmation(self, db, task_id, attempt_id, evidence):
        changed = db.execute("UPDATE task_attempts SET exit_confirmed=1,exit_evidence=? WHERE id=? AND task_id=? AND exit_confirmed=0",
                             (evidence, attempt_id, task_id)).rowcount
        if changed:
            # Exit proof is a task-resource change: it unlocks retry in clients.
            # Increment once per proof, never on repeated observations or heartbeats.
            db.execute("UPDATE tasks SET version=version+1,updated_at=? WHERE id=? AND current_attempt_id=?",
                       (now(), task_id, attempt_id))

    def confirm_exit(self, task_id, attempt_id, *, instance_id, epoch, evidence):
        """Trusted controller exit proof, not a terminal event or heartbeat timeout."""
        token(evidence)
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._task(db, task_id)
            attempt = db.execute("SELECT * FROM task_attempts WHERE id=? AND task_id=?", (attempt_id, task_id)).fetchone()
            if attempt is None or attempt["mode"] != "worker" or (attempt["instance_id"], attempt["epoch"]) != (instance_id, epoch):
                raise TaskStateError("exit_identity_conflict")
            if attempt["exit_confirmed"]:
                if attempt["exit_evidence"] != evidence:
                    raise TaskStateError("exit_evidence_conflict")
                return
            if attempt["status"] not in TERMINAL:
                if task["current_attempt_id"] != attempt_id:
                    raise TaskStateError("exit_identity_conflict")
                if self._expire_tx(db, task, attempt):
                    task = self._task(db, task_id)
                    attempt = db.execute('SELECT * FROM task_attempts WHERE id=?', (attempt_id,)).fetchone()
                self._terminal(db, task, attempt, "canceled" if task["cancel_requested"] else "interrupted",
                               error="execution_exit_confirmed_without_terminal")
            self._record_exit_confirmation(db, task_id, attempt_id, evidence)
            db.execute("UPDATE task_reservations SET released=1 WHERE attempt_id=? AND kind='task'", (attempt_id,))
            db.execute("UPDATE instance_cancel_deadlines SET state='completed',updated_at=? WHERE attempt_id=?", (now(), attempt_id))
            self.fault("exit.release")
            self._audit(db, "task.exit_confirmed", task_id, {"attempt_id": attempt_id})

    def retry(self, task_id, version, *, local=False):
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._task(db, task_id, version)
            if task["migration_source"]:
                raise TaskStateError("legacy_execution_unreconciled")
            if (task["execution_mode"] == "local") != local:
                raise TaskStateError("retry_execution_mode_mismatch")
            if not local:
                self.repository.assert_not_removing_tx(db, task["model_key"])
                if task["deployment_id"] is not None:
                    deployment = db.execute("SELECT enabled,install_state,current_config_revision,pending_config_revision "
                                            "FROM model_deployments WHERE id=?", (task["deployment_id"],)).fetchone()
                    policy = db.execute("SELECT configuration_state FROM instance_policies WHERE instance_id=?",
                                        (task["deployment_id"],)).fetchone()
                    installed = db.execute("SELECT 1 FROM instance_installation_bindings b JOIN model_deployments d "
                                           "ON d.id=b.instance_id AND d.incarnation=b.incarnation WHERE d.id=?",
                                           (task["deployment_id"],)).fetchone()
                    if (deployment is None or not deployment["enabled"] or deployment["install_state"] != "ready"
                            or deployment["current_config_revision"] != task["deployment_config_revision"]
                            or deployment["pending_config_revision"] is not None
                            or installed is None or policy is None or policy["configuration_state"] != "applied"):
                        raise TaskStateError("deployment_not_ready")
            if task["status"] not in {"failed", "canceled", "interrupted"}:
                raise TaskStateError("task_not_retryable")
            if db.execute("SELECT 1 FROM task_attempts WHERE task_id=? AND exit_confirmed=0", (task_id,)).fetchone():
                raise TaskStateError("execution_exit_unconfirmed")
            db.execute("UPDATE tasks SET status='queued',stage='queued',version=version+1,cancel_requested=0,error=NULL,progress=0,updated_at=? WHERE id=?", (now(), task_id))
            self._audit(db, "task.retry", task_id)
            if local:
                self._begin_local(db, task_id, version + 1)
            self.fault("retry.task")
        return self.repository.get_task(task_id)

    def begin_local(self, task_id, version):
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._begin_local(db, task_id, version)

    def _begin_local(self, db, task_id, version):
        task = self._task(db, task_id, version)
        if task["status"] != "queued" or task["execution_mode"] != "local":
            raise TaskStateError("local_task_not_startable")
        if db.execute("SELECT 1 FROM task_attempts WHERE task_id=? AND exit_confirmed=0", (task_id,)).fetchone():
            raise TaskStateError("execution_exit_unconfirmed")
        attempt = "local_" + secrets.token_hex(16)
        generation = db.execute("SELECT COALESCE(MAX(generation),0)+1 FROM task_attempts WHERE task_id=?", (task_id,)).fetchone()[0]
        db.execute("INSERT INTO task_attempts(id,task_id,generation,mode,status,created_at,updated_at) VALUES(?,?,?,'local','running',?,?)", (attempt, task_id, generation, now(), now()))
        db.execute("UPDATE tasks SET status='running',stage='local_edit',current_attempt_id=?,version=version+1,updated_at=? WHERE id=?", (attempt, now(), task_id))
        self.fault("local.start")
        return attempt

    def finish_local(self, task_id, attempt_id, *, asset_id=None, revision=None, error=None, _connection=None):
        from contextlib import nullcontext
        with (nullcontext(_connection) if _connection is not None else self.repository._connect()) as db:
            if _connection is None:
                db.execute("BEGIN IMMEDIATE")
            task = self._task(db, task_id)
            attempt = db.execute("SELECT * FROM task_attempts WHERE id=? AND task_id=? AND mode='local'", (attempt_id, task_id)).fetchone()
            if attempt is None or task["current_attempt_id"] != attempt_id or task["status"] not in {"running", "cancel_requested"}:
                raise TaskStateError("local_attempt_stale")
            state = "canceled" if task["cancel_requested"] else "failed" if error else "succeeded"
            seal = db.execute("SELECT * FROM task_seals WHERE asset_id=? AND revision=? AND task_id=? AND attempt_id=? AND cancel_revision=?",
                              (asset_id, revision, task_id, attempt_id, task["cancel_revision"])).fetchone() if state == "succeeded" else None
            self._terminal(db, task, attempt, state, seal, error, audit_action="task.client_image_edit")
            db.execute("UPDATE task_attempts SET exit_confirmed=1,exit_evidence='local-return' WHERE id=?", (attempt_id,))
            self.fault("local.finish")
        return self.repository.get_task(task_id) if _connection is None else None

    def outbox_page(self, limit=100, *, after_sequence=0, replay=False, identity=None):
        """Transport delivery is not business acknowledgement.

        replay=True reconstructs immutable envelopes after broker rollback,
        including already sent messages. Journals must deduplicate message_id;
        neither reading nor retransmission creates a new attempt.
        """
        if type(limit) is not int or not 1 <= limit <= 1000 or type(after_sequence) is not int or after_sequence < 0:
            raise TaskStateError("invalid_outbox_cursor", 400)
        identity_values = None
        if identity is not None:
            if (type(identity) is not dict or set(identity) != {"server_id", "instance_id", "worker_epoch"}
                    or any(type(identity[key]) is not str or not identity[key] for key in identity)):
                raise TaskStateError("invalid_outbox_identity", 400)
            identity_values = tuple(identity[key] for key in ("server_id", "instance_id", "worker_epoch"))
        with self.repository._connect() as db:
            where = "" if replay else "AND delivered_at IS NULL AND acknowledged_at IS NULL"
            identity_where = ""
            values = [after_sequence]
            if identity_values is not None:
                identity_where = ("AND json_extract(envelope_json,'$.server_id')=? "
                                  "AND json_extract(envelope_json,'$.instance_id')=? "
                                  "AND json_extract(envelope_json,'$.worker_epoch')=?")
                values.extend(identity_values)
            values.append(limit + 1)
            rows = db.execute(
                f"SELECT sequence,message_id,envelope_json,digest FROM task_outbox WHERE sequence>? {where} {identity_where} ORDER BY sequence LIMIT ?",
                tuple(values)).fetchall()
            selected = rows[:limit]
            items = []
            for row in selected:
                message = json.loads(row["envelope_json"])
                if digest(message) != row["digest"] or message.get("message_id") != row["message_id"]:
                    raise TaskStateError("outbox_integrity_error")
                if self._completed_model_command(db, message, row["digest"]):
                    continue
                items.append(validate_envelope(message, capabilities=self.capabilities))
        return {"items": items,
                "next_cursor": selected[-1]["sequence"] if selected else after_sequence, "has_more": len(rows) > limit}

    def _completed_model_command(self, db, message, expected_digest):
        """Completed model work is history, not a fresh execution request.

        Verify the original command and committed terminal evidence without
        reinterpreting old payloads through today's execution schema. Receipts
        remain replayable, and incomplete commands still get strict validation.
        """
        if message.get("type") not in {"model.load", "model.unload", "worker.snapshot.request"}:
            return False
        operation = db.execute("""SELECT m.*,c.instance_id,c.epoch FROM model_operations m
            JOIN instance_claims c USING(claim_id) WHERE m.command_id=?""",
            (message["message_id"],)).fetchone()
        if operation is None or operation["state"] not in {"succeeded", "failed", "canceled"}:
            return False
        payload = message.get("payload")
        expected_type = ("worker.snapshot.request" if operation["action"] == "snapshot"
                         else "model." + operation["action"])
        if (operation["command_digest"] != expected_digest or type(payload) is not dict
                or message["type"] != expected_type or message.get("server_id") != self.server_id
                or message.get("instance_id") != operation["instance_id"]
                or message.get("worker_epoch") != operation["epoch"]
                or payload.get("operation_id") != operation["operation_id"]
                or payload.get("desired_revision") != operation["desired_revision"]):
            raise TaskStateError("outbox_integrity_error")
        terminal = db.execute("""SELECT * FROM instance_inbox WHERE claim_id=? AND scope_id=?
            AND result IN ('applied','stale') AND json_extract(envelope_json,'$.type')='model.terminal'
            ORDER BY event_seq DESC LIMIT 1""", (operation["claim_id"], operation["operation_id"])).fetchone()
        # Do not infer completion from a transport ACK or a terminal-looking
        # operation without its committed event. Such work stays fail-closed.
        if terminal is None:
            return False
        event = json.loads(terminal["envelope_json"])
        end = event.get("payload")
        if (digest(event) != terminal["digest"] or event.get("message_id") != terminal["message_id"]
                or event.get("event_seq") != terminal["event_seq"] or type(end) is not dict
                or any(event.get(key) != message.get(key) for key in ("server_id", "instance_id", "worker_epoch"))
                or end.get("operation_id") != operation["operation_id"]
                or end.get("desired_revision") != operation["desired_revision"]
                or end.get("action") != operation["action"] or end.get("status") != operation["state"]):
            raise TaskStateError("outbox_integrity_error")
        return True

    def _receipt_id(self, event_message_id):
        return "receipt_" + hashlib.sha256(canonical([self.server_id, event_message_id]).encode("utf-8")).hexdigest()

    def receipt_for_event(self, envelope):
        """Two indexed lookups, independent of outbox size or concurrent append.

        This reads an already committed receipt; pending events cannot acquire
        an acknowledgement through a lookup. Verify the entire original event
        identity, not a caller-supplied ID alone.
        """
        message = validate_envelope(envelope, capabilities=self.capabilities)
        with self.repository._connect() as db:
            event = db.execute("SELECT digest,result FROM task_inbox WHERE message_id=?", (message["message_id"],)).fetchone()
            if event is None:
                return None
            if event["digest"] != digest(message):
                raise TaskStateError("event_identity_conflict")
            if event["result"] == "pending":
                return None
            row = db.execute("SELECT envelope_json,digest FROM task_outbox WHERE message_id=?", (self._receipt_id(message["message_id"]),)).fetchone()
        if row is None:
            raise TaskStateError("receipt_integrity_error")
        receipt = json.loads(row["envelope_json"])
        if (digest(receipt) != row["digest"] or receipt["type"] != "event.receipt"
                or any(receipt[key] != message[key] for key in ("server_id", "instance_id", "worker_epoch"))
                or receipt["payload"] != {"subject": "task", "task_id": message["task_id"],
                    "attempt_id": message["attempt_id"], "event_message_id": message["message_id"], "event_seq": message["event_seq"]}):
            raise TaskStateError("receipt_integrity_error")
        return receipt

    def outbox(self, limit=100, *, replay=False):
        return self.outbox_page(limit, replay=replay)["items"]

    def mark_delivered(self, message_id, expected_digest):
        with self.repository._connect() as db:
            changed = db.execute(
                "UPDATE task_outbox SET delivered_at=? "
                "WHERE message_id=? AND digest=? AND delivered_at IS NULL",
                (now(), message_id, expected_digest),
            ).rowcount
            if changed == 1:
                return True
            return db.execute(
                "SELECT 1 FROM task_outbox WHERE message_id=? AND digest=? "
                "AND delivered_at IS NOT NULL",
                (message_id, expected_digest),
            ).fetchone() is not None
