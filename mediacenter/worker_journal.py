"""Instance-local durable Worker journal; never the Server task database.

Trusted Controller admission/grants/exit attestations are explicit methods,
not messages or inferred from a receipt, heartbeat, PID absence or Redis claim.
Opening a journal never resumes execution or changes an old work state.
"""
from __future__ import annotations

import copy
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .capabilities import validate_worker_capability
from .protocol import PROTOCOL, COMMAND_TYPES, validate_envelope
from .worker_common import canonical, digest
from .transport import Identity, DurableResult, receipt_matches, token

TERMINAL = {"succeeded", "failed", "canceled", "interrupted"}
SCHEMA = """
CREATE TABLE journal_meta(version INTEGER NOT NULL CHECK(version=1),instance_id TEXT NOT NULL,highest_revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE journal_epochs(sequence INTEGER PRIMARY KEY AUTOINCREMENT,epoch TEXT UNIQUE NOT NULL,server_id TEXT NOT NULL,
 binding_json TEXT NOT NULL,admitted INTEGER NOT NULL DEFAULT 0,admission_evidence TEXT,clock_trusted INTEGER NOT NULL DEFAULT 0,
 quarantined INTEGER NOT NULL DEFAULT 0,child_state TEXT NOT NULL DEFAULT 'none',child_pid INTEGER,child_token TEXT,exit_evidence TEXT,
 resident_state TEXT NOT NULL DEFAULT 'online_unloaded');
CREATE TABLE journal_commands(message_id TEXT PRIMARY KEY,digest TEXT NOT NULL,envelope_json TEXT NOT NULL,
 epoch TEXT NOT NULL REFERENCES journal_epochs(epoch),work_key TEXT,received_at TEXT NOT NULL);
CREATE TABLE journal_work(sequence INTEGER PRIMARY KEY AUTOINCREMENT,work_key TEXT UNIQUE NOT NULL,
 epoch TEXT NOT NULL REFERENCES journal_epochs(epoch),kind TEXT NOT NULL,scope_id TEXT NOT NULL,
 task_id TEXT,desired_revision INTEGER,request_digest TEXT NOT NULL,envelope_json TEXT NOT NULL,
 state TEXT NOT NULL,execution_token TEXT,next_event_seq INTEGER NOT NULL DEFAULT 1,
 exit_confirmed INTEGER NOT NULL DEFAULT 0,error_code TEXT,quiescence_json TEXT,UNIQUE(epoch,kind,scope_id));
CREATE UNIQUE INDEX journal_one_execution ON journal_work((1)) WHERE state IN ('executing','cancel_requested');
CREATE TABLE journal_cancels(epoch TEXT NOT NULL REFERENCES journal_epochs(epoch),task_id TEXT NOT NULL,attempt_id TEXT NOT NULL,
 cancel_revision INTEGER NOT NULL,received_at TEXT NOT NULL,PRIMARY KEY(epoch,task_id,attempt_id));
CREATE TABLE journal_grants(epoch TEXT NOT NULL REFERENCES journal_epochs(epoch),reservation_id TEXT NOT NULL,generation INTEGER NOT NULL,
 kind TEXT NOT NULL,target_id TEXT NOT NULL,PRIMARY KEY(epoch,reservation_id));
CREATE TABLE journal_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,message_id TEXT UNIQUE NOT NULL,
 epoch TEXT NOT NULL REFERENCES journal_epochs(epoch),scope TEXT NOT NULL,event_seq INTEGER NOT NULL,
 envelope_json TEXT NOT NULL,digest TEXT NOT NULL,confirmed INTEGER NOT NULL DEFAULT 0,receipt_json TEXT,
 UNIQUE(epoch,scope,event_seq));
"""


class JournalError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CommandAuthority:
    """Trusted bootstrap + authenticated command channel, never wire input.

    Controller constructs this only after epoch recovery and credential binding.
    The runtime uses it exclusively on its authenticated commands connection.
    """
    identity: Identity
    binding_digest: str
    evidence: str


def utcnow():
    return datetime.now(timezone.utc)


class WorkerJournal:
    def __init__(self, path, instance_id, capabilities, *, clock=utcnow, fault=lambda _: None):
        self.path, self.instance_id = Path(path), token(instance_id)
        self.capabilities = copy.deepcopy(capabilities)
        for capability in self.capabilities.values():
            validate_worker_capability(capability)
        self.clock, self.fault = clock, fault
        if not self.path.is_absolute() or any(part.is_symlink() for part in (self.path, *self.path.parents)):
            raise JournalError("invalid_journal_path")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        if existed:
            # Detect unsupported/corrupt input read-only before enabling WAL.
            try:
                db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
                try:
                    row = db.execute("SELECT version,instance_id FROM journal_meta").fetchall()
                    if row != [(1, instance_id)] or db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise JournalError("journal_identity_or_schema_mismatch")
                finally:
                    db.close()
            except sqlite3.Error:
                raise JournalError("journal_corrupt") from None
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            if not existed:
                db.execute("BEGIN IMMEDIATE")
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute("INSERT INTO journal_meta(version,instance_id) VALUES(1,?)", (instance_id,))
        if os.name == "posix":
            self.path.chmod(0o600)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise JournalError("clock_untrusted")
        return value.astimezone(timezone.utc)

    def _epoch(self, db, identity):
        if identity.instance_id != self.instance_id:
            raise JournalError("wrong_instance")
        row = db.execute("SELECT * FROM journal_epochs WHERE epoch=? AND server_id=?", (identity.worker_epoch, identity.server_id)).fetchone()
        if row is None:
            raise JournalError("unknown_epoch")
        return row

    def _admitted(self, db, identity):
        row = self._epoch(db, identity)
        newest = db.execute("SELECT epoch FROM journal_epochs ORDER BY sequence DESC LIMIT 1").fetchone()[0]
        if (newest != identity.worker_epoch or not row["admitted"] or not row["clock_trusted"] or row["quarantined"]):
            raise JournalError("admission_required")
        return row

    def _event(self, db, identity, kind, payload, scope, sequence, *, task_id=None, attempt_id=None, extensions=None):
        message = {"protocol": PROTOCOL, "type": kind, "message_id": "evt_" + secrets.token_hex(16),
                   "server_id": identity.server_id, "instance_id": identity.instance_id, "worker_epoch": identity.worker_epoch,
                   "correlation_id": task_id or "instance", "created_at": self._now().isoformat().replace("+00:00", "Z"),
                   "event_seq": sequence, "payload": payload}
        if task_id is not None:
            message.update(task_id=task_id, attempt_id=attempt_id)
        if extensions:
            message["extensions"] = extensions
        message = validate_envelope(message, capabilities=self.capabilities)
        db.execute("INSERT INTO journal_events(message_id,epoch,scope,event_seq,envelope_json,digest) VALUES(?,?,?,?,?,?)",
                   (message["message_id"], identity.worker_epoch, scope, sequence, canonical(message), digest(message)))
        self.fault("event.insert")
        return message

    def register(self, identity, binding):
        required = {"model_key", "recipe_revision", "model_asset_id", "model_asset_revision", "image_digest", "gpu_uuids", "capability_digest"}
        if type(binding) is not dict or set(binding) != required:
            raise JournalError("invalid_binding")
        for name in ("model_key", "recipe_revision", "model_asset_id", "model_asset_revision"):
            token(binding[name])
        capability = self.capabilities.get(binding["model_key"])
        if capability is None or binding["capability_digest"] != digest(capability):
            raise JournalError("capability_binding_mismatch")
        if identity.instance_id != self.instance_id:
            raise JournalError("wrong_instance")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM journal_epochs WHERE epoch=?", (identity.worker_epoch,)).fetchone():
                raise JournalError("supervisor_epoch_reuse_forbidden")
            db.execute("INSERT INTO journal_epochs(epoch,server_id,binding_json) VALUES(?,?,?)",
                       (identity.worker_epoch, identity.server_id, canonical(binding)))
            payload = {key: binding[key] for key in ("model_key", "recipe_revision", "image_digest", "gpu_uuids", "capability_digest")}
            self._event(db, identity, "worker.registered", payload, "worker", 1)
            self.fault("register.epoch")

    def admit(self, identity, expected_binding, *, evidence, recovery_complete, clock_trusted, child_token=None):
        """Trusted Controller hook. A registered receipt cannot call this."""
        token(evidence)
        if recovery_complete is not True or clock_trusted is not True:
            raise JournalError("admission_required")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            epoch = self._admission_state(db, identity, expected_binding)
            if child_token is not None and (epoch["child_state"] != "alive" or epoch["child_token"] != child_token):
                raise JournalError("child_identity_conflict")
            db.execute("UPDATE journal_epochs SET admitted=1,clock_trusted=1,admission_evidence=? WHERE epoch=?", (evidence, identity.worker_epoch))
            self.fault("admission.commit")

    def _admission_state(self, db, identity, binding):
        epoch = self._epoch(db, identity)
        if canonical(binding) != epoch["binding_json"]:
            raise JournalError("binding_mismatch")
        newest = db.execute("SELECT epoch FROM journal_epochs ORDER BY sequence DESC LIMIT 1").fetchone()[0]
        if newest != identity.worker_epoch or epoch["quarantined"] or epoch["child_state"] == "exited":
            raise JournalError("recovery_required")
        if db.execute("SELECT 1 FROM journal_epochs WHERE epoch!=? AND child_state IN ('starting','alive')", (identity.worker_epoch,)).fetchone():
            raise JournalError("recovery_required")
        if db.execute("SELECT 1 FROM journal_work WHERE epoch!=? AND exit_confirmed=0", (identity.worker_epoch,)).fetchone():
            raise JournalError("recovery_required")
        return epoch

    def check_admission(self, identity, binding, *, evidence, recovery_complete, clock_trusted):
        """Read-only bootstrap gate before a lightweight capability child."""
        token(evidence)
        if recovery_complete is not True or clock_trusted is not True:
            raise JournalError("admission_required")
        with self._connect() as db:
            epoch = self._admission_state(db, identity, binding)
            if epoch["admitted"]:
                raise JournalError("admission_already_completed")

    def grant(self, identity, reservation_id, generation, *, kind, target_id):
        """Persist an explicit Controller grant; does not allocate/inspect GPUs."""
        token(reservation_id); token(target_id)
        if type(generation) is not int or generation < 1 or kind not in {"task", "load"}:
            raise JournalError("invalid_resource_grant")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._admitted(db, identity)
            old = db.execute("SELECT * FROM journal_grants WHERE epoch=? AND reservation_id=?", (identity.worker_epoch, reservation_id)).fetchone()
            if old and (generation < old["generation"] or generation == old["generation"] and (kind, target_id) != (old["kind"], old["target_id"])):
                raise JournalError("resource_generation_conflict")
            db.execute("INSERT INTO journal_grants VALUES(?,?,?,?,?) ON CONFLICT(epoch,reservation_id) DO UPDATE SET generation=excluded.generation,kind=excluded.kind,target_id=excluded.target_id",
                       (identity.worker_epoch, reservation_id, generation, kind, target_id))

    def _resource(self, db, identity, message):
        if message["type"] not in {"task.execute", "model.load"}:
            return
        payload = message["payload"]
        grant = db.execute("SELECT * FROM journal_grants WHERE epoch=? AND reservation_id=?", (identity.worker_epoch, payload["reservation_id"])).fetchone()
        kind, target = ("task", message["attempt_id"]) if message["type"] == "task.execute" else ("load", payload["operation_id"])
        if not grant or (grant["generation"], grant["kind"], grant["target_id"]) != (payload["reservation_generation"], kind, target):
            raise JournalError("resource_grant_required")

    def _command(self, db, message, work_key=None):
        db.execute("INSERT INTO journal_commands VALUES(?,?,?,?,?,?)", (message["message_id"], digest(message), canonical(message),
                   message["worker_epoch"], work_key, self._now().isoformat()))
        self.fault("receive.command")

    def _checked_work(self, db, work):
        try:
            message = validate_envelope(json.loads(work["envelope_json"]), capabilities=self.capabilities)
            identity = Identity(message["server_id"], message["instance_id"], message["worker_epoch"])
            epoch = self._epoch(db, identity)
            kind = "task" if message["type"] == "task.execute" else "operation"
            scope = message["attempt_id"] if kind == "task" else message["payload"]["operation_id"]
            expected = digest({"type": message["type"], "payload": message["payload"], "task_id": message.get("task_id")})
            command = db.execute("SELECT * FROM journal_commands WHERE message_id=?", (message["message_id"],)).fetchone()
            if (message["type"] not in {"task.execute", "model.load", "model.unload"}
                    or work["request_digest"] != expected or work["kind"] != kind or work["scope_id"] != scope
                    or work["epoch"] != message["worker_epoch"] or work["task_id"] != message.get("task_id")
                    or work["desired_revision"] != message["payload"].get("desired_revision")
                    or work["work_key"] != "work_" + digest([work["epoch"], kind, scope])
                    or not command or command["digest"] != digest(message) or command["envelope_json"] != canonical(message)
                    or command["work_key"] != work["work_key"] or command["epoch"] != work["epoch"]):
                raise ValueError()
            binding = json.loads(epoch["binding_json"])
            if message["type"] != "model.unload" and any(message["payload"][key] != binding[key] for key in
                    ("model_key", "recipe_revision", "model_asset_id", "model_asset_revision")):
                raise ValueError()
            return message
        except (ValueError, KeyError, TypeError, JournalError):
            raise JournalError("journal_work_corrupt") from None

    def _work_event(self, db, work, kind, payload, *, extensions=None):
        message = self._checked_work(db, work)
        identity = Identity(message["server_id"], self.instance_id, work["epoch"])
        event = self._event(db, identity, kind, payload, work["work_key"], work["next_event_seq"],
                            task_id=work["task_id"] if work["kind"] == "task" else None,
                            attempt_id=work["scope_id"] if work["kind"] == "task" else None, extensions=extensions)
        db.execute("UPDATE journal_work SET next_event_seq=next_event_seq+1 WHERE work_key=?", (work["work_key"],))
        return event

    def _checked_event(self, db, row):
        try:
            value = validate_envelope(json.loads(row["envelope_json"]), capabilities=self.capabilities)
            self._epoch(db, Identity(value["server_id"], value["instance_id"], value["worker_epoch"]))
            if (digest(value) != row["digest"] or value["message_id"] != row["message_id"]
                    or value["worker_epoch"] != row["epoch"] or value["event_seq"] != row["event_seq"]):
                raise ValueError()
            return value
        except (ValueError, KeyError, TypeError, JournalError):
            raise JournalError("journal_event_corrupt") from None

    def _finish(self, db, work, status, error_code, manifest=None, *, clean=True, controller_exit=False):
        canceled = work["kind"] == "task" and db.execute("SELECT 1 FROM journal_cancels WHERE epoch=? AND task_id=? AND attempt_id=?",
                    (work["epoch"], work["task_id"], work["scope_id"])).fetchone()
        if canceled:
            status, error_code, manifest = "canceled", "canceled" if clean else "adapter_reset_failed", None
        request = self._checked_work(db, work)
        payload = {"status": status, "error_code": error_code}
        if work["kind"] == "task":
            payload["manifest"] = manifest if status == "succeeded" else None
            kind = "task.terminal"
        else:
            payload.update(operation_id=work["scope_id"], desired_revision=work["desired_revision"],
                           action="load" if request["type"] == "model.load" else "unload")
            if request["type"] == "model.load":
                payload.update({key: request["payload"][key] for key in ("reservation_id", "reservation_generation")})
            kind = "model.terminal"
        epoch = db.execute("SELECT * FROM journal_epochs WHERE epoch=?", (work["epoch"],)).fetchone()
        extensions = None
        if clean and not controller_exit and work["kind"] == "task":
            if work["execution_token"] and (not epoch["child_token"] or epoch["child_state"] != "alive"):
                raise JournalError("child_quiescence_unbound")
            assertion = {"kind": "quiescent" if work["execution_token"] else "never_started",
                         "command_message_id": request["message_id"], "command_digest": digest(request),
                         **{key: request[key] for key in ("server_id", "instance_id", "worker_epoch", "task_id", "attempt_id")}}
            if work["execution_token"]:
                assertion.update(execution_token=work["execution_token"], child_token=epoch["child_token"])
            extensions = {"execution_quiescence": assertion}
        self._work_event(db, work, kind, payload, extensions=extensions)
        proof = {"instance_id": self.instance_id, "worker_epoch": work["epoch"], "work_key": work["work_key"],
                 "scope_id": work["scope_id"], "task_id": work["task_id"], "execution_token": work["execution_token"],
                 "child_token": epoch["child_token"], "kind": "adapter_quiescent" if work["execution_token"] else "not_dispatched"}
        db.execute("UPDATE journal_work SET state=?,error_code=?,exit_confirmed=?,quiescence_json=? WHERE work_key=?",
                   (status, error_code, int(clean), canonical(proof) if clean else None, work["work_key"]))
        if not clean:
            db.execute("UPDATE journal_epochs SET quarantined=1,admitted=0 WHERE epoch=?", (work["epoch"],))
        elif work["kind"] == "operation" and status == "succeeded" and not controller_exit:
            db.execute("UPDATE journal_epochs SET resident_state=? WHERE epoch=?",
                       ("ready" if request["type"] == "model.load" else "online_unloaded", work["epoch"]))
        self.fault("finish.state")

    def receive(self, envelope, *, authority=None):
        message = validate_envelope(envelope, capabilities=self.capabilities)
        if message["type"] not in COMMAND_TYPES:
            raise JournalError("command_required")
        identity = Identity(message["server_id"], message["instance_id"], message["worker_epoch"])
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            epoch = self._epoch(db, identity)
            prior = db.execute("SELECT digest FROM journal_commands WHERE message_id=?", (message["message_id"],)).fetchone()
            if prior:
                if prior["digest"] != digest(message):
                    raise JournalError("command_identity_conflict")
                return DurableResult("journaled")
            kind, payload = message["type"], message["payload"]
            if kind == "event.receipt":
                event = db.execute("SELECT * FROM journal_events WHERE message_id=?", (payload["event_message_id"],)).fetchone()
                if not event or not receipt_matches(message, self._checked_event(db, event)):
                    raise JournalError("receipt_identity_conflict")
                db.execute("UPDATE journal_events SET confirmed=1,receipt_json=? WHERE message_id=?", (canonical(message), payload["event_message_id"]))
                self._command(db, message)
                self.fault("receipt.confirm")
                return DurableResult("journaled")
            if kind == "task.cancel":
                self._command(db, message)
                db.execute("INSERT INTO journal_cancels VALUES(?,?,?,?,?) ON CONFLICT(epoch,task_id,attempt_id) DO UPDATE SET cancel_revision=MAX(cancel_revision,excluded.cancel_revision)",
                           (identity.worker_epoch, message["task_id"], message["attempt_id"], payload["cancel_revision"], self._now().isoformat()))
                work = db.execute("SELECT * FROM journal_work WHERE epoch=? AND kind='task' AND scope_id=? AND task_id=?",
                                  (identity.worker_epoch, message["attempt_id"], message["task_id"])).fetchone()
                if work and work["state"] == "accepted":
                    self._finish(db, work, "canceled", "canceled")
                elif work and work["state"] == "executing":
                    db.execute("UPDATE journal_work SET state='cancel_requested' WHERE work_key=?", (work["work_key"],))
                self.fault("cancel.tombstone")
                return DurableResult("journaled")
            if kind == "worker.snapshot.request":
                existing_snapshot = db.execute("SELECT * FROM journal_events WHERE epoch=? AND scope=?",
                                               (identity.worker_epoch, "snapshot-" + payload["operation_id"])).fetchone()
                if existing_snapshot:
                    saved = self._checked_event(db, existing_snapshot)
                    if saved["payload"]["desired_revision"] != payload["desired_revision"]:
                        raise JournalError("work_identity_conflict")
                    self._command(db, message)
                    return DurableResult("journaled")
                snapshot = self._snapshot(db)
                self._event(db, identity, "worker.snapshot", dict(payload, **snapshot), "snapshot-" + payload["operation_id"], 1)
                self._command(db, message)
                return DurableResult("journaled")
            work_kind = "task" if kind == "task.execute" else "operation"
            scope_id = message["attempt_id"] if work_kind == "task" else payload["operation_id"]
            work_key = "work_" + digest([identity.worker_epoch, work_kind, scope_id])
            request_digest = digest({"type": kind, "payload": payload, "task_id": message.get("task_id")})
            existing = db.execute("SELECT * FROM journal_work WHERE work_key=?", (work_key,)).fetchone()
            if existing:
                self._checked_work(db, existing)
                if existing["request_digest"] != request_digest:
                    raise JournalError("work_identity_conflict")
                self._command(db, message, work_key)
                return DurableResult("journaled")
            epoch = self._admitted(db, identity)
            if kind in {"task.execute", "model.load"}:
                if datetime.fromisoformat(message["expires_at"].replace("Z", "+00:00")) <= self._now():
                    raise JournalError("execution_permission_expired")
                binding = json.loads(epoch["binding_json"])
                if any(payload[key] != binding[key] for key in ("model_key", "recipe_revision", "model_asset_id", "model_asset_revision")):
                    raise JournalError("model_binding_mismatch")
                if authority is not None:
                    if (not isinstance(authority, CommandAuthority) or authority.identity != identity
                            or authority.binding_digest != digest(binding)):
                        raise JournalError("command_authority_mismatch")
                    token(authority.evidence)
                    prior_grant = db.execute("SELECT * FROM journal_grants WHERE epoch=? AND reservation_id=?",
                                             (identity.worker_epoch, payload["reservation_id"])).fetchone()
                    grant_kind = "task" if kind == "task.execute" else "load"
                    if prior_grant and (payload["reservation_generation"] < prior_grant["generation"] or
                            payload["reservation_generation"] == prior_grant["generation"] and
                            (grant_kind, scope_id) != (prior_grant["kind"], prior_grant["target_id"])):
                        raise JournalError("resource_generation_conflict")
                    db.execute("INSERT INTO journal_grants VALUES(?,?,?,?,?) ON CONFLICT(epoch,reservation_id) DO UPDATE SET generation=excluded.generation,kind=excluded.kind,target_id=excluded.target_id",
                               (identity.worker_epoch, payload["reservation_id"], payload["reservation_generation"], grant_kind, scope_id))
                self._resource(db, identity, message)
            if work_kind == "task" and db.execute("SELECT 1 FROM journal_work WHERE state IN ('accepted','executing','cancel_requested')").fetchone():
                raise JournalError("worker_busy")
            if db.execute("SELECT COUNT(*) FROM journal_work WHERE state='accepted'").fetchone()[0] >= 64:
                raise JournalError("worker_queue_full")
            desired = payload.get("desired_revision")
            if desired is not None and db.execute("SELECT 1 FROM journal_work WHERE epoch=? AND kind='operation' AND desired_revision=?",
                                                 (identity.worker_epoch, desired)).fetchone():
                raise JournalError("desired_revision_conflict")
            highest = db.execute("SELECT highest_revision FROM journal_meta").fetchone()[0]
            if desired is not None and desired > highest:
                db.execute("UPDATE journal_meta SET highest_revision=?", (desired,))
            self._command(db, message, work_key)
            db.execute("INSERT INTO journal_work(work_key,epoch,kind,scope_id,task_id,desired_revision,request_digest,envelope_json,state) VALUES(?,?,?,?,?,?,?,?,?)",
                       (work_key, identity.worker_epoch, work_kind, scope_id, message.get("task_id"), desired, request_digest, canonical(message), "accepted"))
            work = db.execute("SELECT * FROM journal_work WHERE work_key=?", (work_key,)).fetchone()
            canceled = work_kind == "task" and db.execute("SELECT 1 FROM journal_cancels WHERE epoch=? AND task_id=? AND attempt_id=?",
                        (identity.worker_epoch, message["task_id"], scope_id)).fetchone()
            if canceled or desired is not None and desired < highest:
                self._finish(db, work, "canceled" if canceled else "failed", "canceled" if canceled else "stale_desired_revision")
            else:
                accepted_payload = {key: payload[key] for key in ("reservation_id", "reservation_generation") if key in payload}
                if work_kind == "operation":
                    accepted_payload.update(operation_id=scope_id, desired_revision=desired, action="load" if kind == "model.load" else "unload")
                self._work_event(db, work, "task.accepted" if work_kind == "task" else "model.accepted", accepted_payload)
            self.fault("receive.work")
        return DurableResult("journaled")

    def claim(self, identity):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._admitted(db, identity)
            if db.execute("SELECT 1 FROM journal_work WHERE state IN ('executing','cancel_requested')").fetchone():
                return None
            work = db.execute("SELECT * FROM journal_work WHERE epoch=? AND state='accepted' ORDER BY sequence LIMIT 1", (identity.worker_epoch,)).fetchone()
            if work is None:
                return None
            message = self._checked_work(db, work)
            highest = db.execute("SELECT highest_revision FROM journal_meta").fetchone()[0]
            if work["desired_revision"] is not None and work["desired_revision"] != highest:
                self._finish(db, work, "failed", "stale_desired_revision")
                return None
            try:
                self._resource(db, identity, message)
            except JournalError:
                # No execution took place. Keep the work queryable but close
                # admission durably instead of repeatedly retrying the head.
                db.execute("UPDATE journal_work SET error_code='resource_grant_changed' WHERE work_key=?", (work["work_key"],))
                db.execute("UPDATE journal_epochs SET quarantined=1,admitted=0 WHERE epoch=?", (identity.worker_epoch,))
                return None
            execution_token = "run_" + secrets.token_hex(16)
            db.execute("UPDATE journal_work SET state='executing',execution_token=? WHERE work_key=? AND state='accepted'", (execution_token, work["work_key"]))
            self.fault("claim.executing")
            return {"work_key": work["work_key"], "execution_token": execution_token, "envelope": message}

    def plan_child(self, identity):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            epoch = self._epoch(db, identity)
            # Capability description must precede admission, but never bypass
            # old execution recovery or permit model effects before admission.
            epoch = self._admission_state(db, identity, json.loads(epoch["binding_json"]))
            if epoch["child_state"] != "none":
                raise JournalError("child_recovery_required")
            child_token = "child_" + secrets.token_hex(16)
            db.execute("UPDATE journal_epochs SET child_state='starting',child_token=? WHERE epoch=?", (child_token, identity.worker_epoch))
            self.fault("child.intent")
            return child_token

    def child_started(self, identity, child_token, pid):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("UPDATE journal_epochs SET child_state='alive',child_pid=? WHERE epoch=? AND child_state='starting' AND child_token=?",
                          (pid, identity.worker_epoch, child_token)).rowcount != 1:
                raise JournalError("child_identity_conflict")
            self.fault("child.started")

    def finish(self, identity, work_key, execution_token, *, status, error_code=None, manifest=None, clean=True):
        if type(clean) is not bool:
            raise JournalError("invalid_reset_confirmation")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._epoch(db, identity)
            work = db.execute("SELECT * FROM journal_work WHERE work_key=? AND epoch=? AND execution_token=?", (work_key, identity.worker_epoch, execution_token)).fetchone()
            if not work or work["state"] not in {"executing", "cancel_requested"}:
                raise JournalError("execution_identity_conflict")
            if not clean:
                status, error_code, manifest = "failed", "adapter_reset_failed" if work["kind"] == "task" else "model_cleanup_unconfirmed", None
            self._finish(db, work, status, error_code, manifest, clean=clean)

    def phase(self, identity, work_key, execution_token, phase):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._epoch(db, identity)
            work = db.execute("SELECT * FROM journal_work WHERE work_key=? AND epoch=? AND execution_token=?",
                              (work_key, identity.worker_epoch, execution_token)).fetchone()
            if not work or work["state"] not in {"executing", "cancel_requested"}:
                raise JournalError("execution_identity_conflict")
            payload = {"phase": phase}
            if work["kind"] != "task":
                payload.update(operation_id=work["scope_id"], desired_revision=work["desired_revision"])
            self._work_event(db, work, "phase.changed", payload)

    def quarantine(self, identity):
        with self._connect() as db:
            db.execute("UPDATE journal_epochs SET quarantined=1,admitted=0 WHERE epoch=? AND server_id=?", (identity.worker_epoch, identity.server_id))

    def confirm_epoch_exit(self, identity, evidence):
        """Controller-only proof of the original process AND descendants exit.

        Never called just because the Supervisor/one child PID disappeared.
        Preserve old epoch envelopes; recovery does not execute them again.
        """
        token(evidence)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            epoch = self._epoch(db, identity)
            if epoch["exit_evidence"] not in {None, evidence}:
                raise JournalError("exit_evidence_conflict")
            for work in db.execute("SELECT * FROM journal_work WHERE epoch=? AND exit_confirmed=0", (identity.worker_epoch,)).fetchall():
                self._checked_work(db, work)
                if work["state"] not in TERMINAL:
                    self._finish(db, work, "interrupted", "controller_confirmed_exit", controller_exit=True)
                proof = {"kind": "controller_epoch_exit", "instance_id": self.instance_id, "worker_epoch": identity.worker_epoch,
                         "work_key": work["work_key"], "scope_id": work["scope_id"], "task_id": work["task_id"],
                         "execution_token": work["execution_token"], "child_token": epoch["child_token"], "evidence": evidence}
                db.execute("UPDATE journal_work SET exit_confirmed=1,quiescence_json=? WHERE work_key=?", (canonical(proof), work["work_key"]))
            db.execute("UPDATE journal_epochs SET child_state='exited',exit_evidence=?,admitted=0 WHERE epoch=?", (evidence, identity.worker_epoch))
            self.fault("exit.confirm")

    def canceled(self, identity, task_id, attempt_id):
        with self._connect() as db:
            return bool(db.execute("SELECT 1 FROM journal_cancels WHERE epoch=? AND task_id=? AND attempt_id=?", (identity.worker_epoch, task_id, attempt_id)).fetchone())

    def pending_events(self, *, epoch=None, after_sequence=0, limit=100):
        if type(after_sequence) is not int or after_sequence < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise JournalError("invalid_journal_cursor")
        with self._connect() as db:
            rows = db.execute("SELECT * FROM journal_events WHERE sequence>? AND confirmed=0" + (" AND epoch=?" if epoch else "") + " ORDER BY sequence LIMIT ?",
                              (after_sequence, epoch, limit + 1) if epoch else (after_sequence, limit + 1)).fetchall()
            selected = rows[:limit]
            events = [self._checked_event(db, row) for row in selected]
        return {"items": events, "next_cursor": selected[-1]["sequence"] if selected else after_sequence, "has_more": len(rows) > limit}

    def recovery(self, *, after_sequence=0, limit=100):
        if type(after_sequence) is not int or after_sequence < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise JournalError("invalid_journal_cursor")
        with self._connect() as db:
            rows = db.execute("SELECT * FROM journal_work WHERE sequence>? ORDER BY sequence LIMIT ?", (after_sequence, limit + 1)).fetchall()
            for row in rows[:limit]:
                self._checked_work(db, row)
        selected = rows[:limit]
        return {"work": [dict(row, uncertain=row["state"] != "accepted" and not row["exit_confirmed"]) for row in selected],
                "next_cursor": selected[-1]["sequence"] if selected else after_sequence, "has_more": len(rows) > limit}

    def epoch_state(self, identity):
        with self._connect() as db:
            return dict(self._epoch(db, identity))

    def _snapshot(self, db):
        rows = db.execute("SELECT * FROM journal_work WHERE exit_confirmed=0 ORDER BY sequence LIMIT 129").fetchall()
        if len(rows) > 128:
            raise JournalError("snapshot_requires_paged_recovery")
        tasks, operations = [], []
        for row in rows:
            self._checked_work(db, row)
            state = "running" if row["state"] == "executing" else row["state"]
            base = {"worker_epoch": row["epoch"], "state": state, "event_seq": max(1, row["next_event_seq"] - 1)}
            if row["kind"] == "task":
                tasks.append(dict(base, task_id=row["task_id"], attempt_id=row["scope_id"]))
            else:
                if state in {"running", "cancel_requested"}:
                    state = "loading" if json.loads(row["envelope_json"])["type"] == "model.load" else "unloading"
                operations.append(dict(base, state=state, operation_id=row["scope_id"], desired_revision=row["desired_revision"]))
        epoch = db.execute("SELECT * FROM journal_epochs ORDER BY sequence DESC LIMIT 1").fetchone()
        return {"state": "error" if rows or epoch["quarantined"] else epoch["resident_state"], "tasks": tasks, "operations": operations}
