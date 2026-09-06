"""Single-database, CAS-owned container lifecycle; no task/GPU side effects.

Every external mutation has a durable unique intent/effect first. Pending or
unknown effects survive process loss and are never blindly reissued. Recovery
uses a fixed intent name only for discovering a lost create response, followed
by complete ownership/configuration checks before recording the full ID.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import ContainerError, READONLY_ROLES, canonical, digest, identifier
from .container_runtime import full_id, verify_inspection
from .instance_policy import InstancePolicy


def now():
    return datetime.now(timezone.utc).isoformat()


class RuntimeController:
    def __init__(self, repository, engine, observer, policy, *, fault=lambda _: None, launch_check=None,
                 historical_removal_check=None):
        self.repository, self.engine, self.observer, self.policy = repository, engine, observer, policy
        self.fault = fault
        self.launch_check = launch_check
        self.historical_removal_check = historical_removal_check
        if (self.policy.server_database != str(self.repository.path)
                or self.policy.engine_socket != self.engine.config.socket_path):
            raise ContainerError("container_authority_boundary_mismatch")

    def _grants(self):
        return [{"role": grant.role, "source": grant.source, "target": grant.target,
                 "readonly": grant.role in READONLY_ROLES, "identity": grant.identity} for grant in self.policy.mounts]

    @staticmethod
    def _request_digest(row):
        return row["intent_digest"]

    @staticmethod
    def _intent_digest(row):
        keys = ("intent_id", "instance_id", "epoch", "engine_id", "container_name",
                "desired_revision", "spec_digest", "image_id", "mount_grants_digest")
        if row.get("identity_version", 1) == 2:
            keys += ("identity_version", "generation", "claim_id")
        return digest({key: row[key] for key in keys})

    def get(self, intent_id):
        with self.repository._connect() as db:
            row = db.execute("SELECT * FROM runtime_intents WHERE intent_id=?", (identifier(intent_id),)).fetchone()
            if row is None:
                raise ContainerError("runtime_intent_missing")
            return self._checked(dict(row))

    def _checked(self, row, *, current_policy=True):
        try:
            if (row["identity_version"] not in (1, 2) or type(row["generation"]) is not int or row["generation"] <= 0
                    or row["identity_version"] == 2 and not row["claim_id"]
                    or row["identity_version"] == 1 and (row["claim_id"] is not None or row["generation"] != row["desired_revision"])):
                raise ContainerError("runtime_intent_integrity_error")
            if self._intent_digest(row) != row["intent_digest"]:
                raise ContainerError("runtime_intent_integrity_error")
            spec = json.loads(row["spec_json"])
            if digest(spec) != row["spec_digest"]:
                raise ContainerError("runtime_intent_integrity_error")
            grants = json.loads(row["mount_grants_json"])
            if digest(grants) != row["mount_grants_digest"]:
                raise ContainerError("runtime_mount_grant_changed")
            if current_policy:
                expected = self.policy.spec(name=row["container_name"], instance_id=row["instance_id"], epoch=row["epoch"],
                                             intent_id=row["intent_id"], cgroup_parent=self.observer.docker_parent(row["container_name"]))
                if spec != expected or row["engine_id"] != self.engine.config.engine_id or row["image_id"] != self.policy.image.image_id:
                    raise ContainerError("runtime_intent_integrity_error")
                if grants != self._grants():
                    raise ContainerError("runtime_mount_grant_changed")
            if row["container_id"]:
                full_id(row["container_id"])
            if row["domain_json"]:
                record = json.loads(row["domain_json"])
                if digest(record) != row["domain_digest"] or record["docker_parent"] != spec["HostConfig"]["CgroupParent"]:
                    raise ContainerError("runtime_domain_integrity_error")
            if row["exit_evidence_json"]:
                evidence = json.loads(row["exit_evidence_json"])
                if digest(evidence) != row["exit_evidence_digest"] or any(evidence[key] != row[key] for key in ("intent_id", "instance_id", "epoch", "engine_id", "container_id", "spec_digest")) or evidence["stop_effect"] != row["effect_token"]:
                    raise ContainerError("runtime_exit_evidence_invalid")
            if row["state"] == "exited" and not row["exit_evidence_json"]:
                raise ContainerError("runtime_exit_evidence_invalid")
        except (KeyError, ValueError, TypeError) as error:
            if isinstance(error, ContainerError):
                raise
            raise ContainerError("runtime_intent_integrity_error") from None
        return row

    def prepare(self, instance_id, epoch, desired_revision):
        identifier(instance_id); identifier(epoch)
        if type(desired_revision) is not int or desired_revision <= 0:
            raise ContainerError("runtime_revision_invalid")
        random = secrets.token_hex(16); intent, name = "runtime-" + random, "mc-" + random
        spec = self.policy.spec(name=name, instance_id=instance_id, epoch=epoch, intent_id=intent,
                                cgroup_parent=self.observer.docker_parent(name))
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            claim = InstancePolicy.assert_claim(db, instance_id, epoch, backend="container")
            InstancePolicy.assert_materialize(db, claim)
            if self.launch_check is not None: self.launch_check(db)
            package = json.loads(claim["execution_json"])
            if (claim["revision"] != desired_revision or set(claim["policy"]["gpus"]) != set(self.policy.gpu_uuids)
                    or package.get("image_digest") != self.policy.image.reference.split("@")[-1]):
                raise ContainerError("runtime_claim_binding_mismatch")
            previous = db.execute("SELECT * FROM runtime_intents WHERE instance_id=? ORDER BY generation DESC LIMIT 1", (instance_id,)).fetchone()
            generation = 1
            if previous:
                previous = self._checked(dict(previous), current_policy=False)
                if previous["epoch"] == epoch:
                    if previous["desired_revision"] != desired_revision or previous["identity_version"] == 2 and previous["claim_id"] != claim["claim_id"]:
                        raise ContainerError("runtime_epoch_revision_conflict")
                    return self._checked(previous)
                if previous["state"] != "exited" or not previous["exit_evidence_json"]:
                    raise ContainerError("previous_execution_unconfirmed")
                if desired_revision < previous["desired_revision"]:
                    raise ContainerError("runtime_revision_stale")
                generation = previous["generation"] + 1
                if db.execute("SELECT 1 FROM runtime_intents WHERE instance_id=? AND state!='exited'", (instance_id,)).fetchone():
                    raise ContainerError("previous_execution_unconfirmed")
                if db.execute("SELECT 1 FROM runtime_effects e JOIN runtime_intents i ON i.intent_id=e.intent_id WHERE i.instance_id=? AND (e.state LIKE '%_pending' OR e.state LIKE '%_unknown' OR e.state='pending')", (instance_id,)).fetchone():
                    raise ContainerError("previous_effect_unconfirmed")
            stamp = now()
            if 'generation' in package:
                if type(package['generation']) is not int or package['generation'] < generation:
                    raise ContainerError('runtime_generation_stale')
                generation = package['generation']
            immutable = {"intent_id": intent, "instance_id": instance_id, "epoch": epoch,
                         "engine_id": self.engine.config.engine_id, "container_name": name, "desired_revision": desired_revision,
                         "spec_digest": digest(spec), "image_id": self.policy.image.image_id, "mount_grants_digest": digest(self._grants()),
                         "identity_version": 2, "generation": generation, "claim_id": claim["claim_id"]}
            try:
                db.execute("INSERT INTO runtime_intents(intent_id,instance_id,epoch,engine_id,container_name,desired_revision,spec_json,spec_digest,image_id,mount_grants_json,mount_grants_digest,intent_digest,state,created_at,updated_at,generation,identity_version,claim_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'prepared',?,?,?,2,?)",
                           (intent, instance_id, epoch, self.engine.config.engine_id, name, desired_revision, canonical(spec), digest(spec), self.policy.image.image_id, canonical(self._grants()), digest(self._grants()), self._intent_digest(immutable), stamp, stamp, generation, claim["claim_id"]))
            except sqlite3.IntegrityError:
                raise ContainerError("runtime_intent_conflict") from None
            self.fault("intent.before_commit")
        return self.get(intent)

    def _begin(self, intent_id, expected_version, action, allowed):
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            raw = db.execute("SELECT * FROM runtime_intents WHERE intent_id=?", (intent_id,)).fetchone()
            if raw is None:
                raise ContainerError("runtime_intent_missing")
            row = self._checked(dict(raw))
            claim = InstancePolicy.assert_claim(db, row["instance_id"], row["epoch"], backend="container")
            if claim["revision"] != row["desired_revision"] or row["identity_version"] == 2 and claim["claim_id"] != row["claim_id"]:
                raise ContainerError("runtime_claim_binding_mismatch")
            if action in {"domain", "create", "start"} and claim["state"] not in {"claimed", "starting", "container_stopped"}:
                raise ContainerError("runtime_claim_fenced")
            if action in {"domain", "create"}:
                InstancePolicy.assert_materialize(db, claim)
                if self.launch_check is not None: self.launch_check(db)
            if action == "start":
                InstancePolicy.assert_launch(db, claim)
                if self.launch_check is not None: self.launch_check(db)
            if row["version"] != expected_version or row["state"] not in allowed:
                raise ContainerError("runtime_state_conflict")
            if db.execute("SELECT 1 FROM runtime_effects WHERE intent_id=? AND action=?", (intent_id, action)).fetchone():
                raise ContainerError("runtime_effect_already_issued")
            token, stamp = "effect-" + secrets.token_hex(16), now()
            db.execute("INSERT INTO runtime_effects VALUES(?,?,?,'pending',?,NULL,?,?)", (token, intent_id, action, self._request_digest(row), stamp, stamp))
            db.execute("UPDATE runtime_intents SET state=?,effect_token=?,version=version+1,error_code=NULL,updated_at=? WHERE intent_id=? AND version=?",
                       (action + "_pending", token, stamp, intent_id, expected_version))
            self.fault(action + ".before_intent_commit")
        self.fault(action + ".after_intent_commit")
        return self.get(intent_id)

    def _finish(self, row, state, *, container_id=None, domain=None, evidence=None, error=None):
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM runtime_intents WHERE intent_id=?", (row["intent_id"],)).fetchone()
            if not current or current["version"] != row["version"] or current["effect_token"] != row["effect_token"]:
                raise ContainerError("runtime_state_conflict")
            self._checked(dict(current))
            effect = db.execute("SELECT * FROM runtime_effects WHERE token=?", (row["effect_token"],)).fetchone()
            if not effect or effect["intent_id"] != row["intent_id"] or effect["request_digest"] != self._request_digest(row):
                raise ContainerError("runtime_effect_integrity_error")
            result = {"state": state, "container_id": container_id, "domain": domain, "evidence": evidence, "error": error}
            db.execute("UPDATE runtime_effects SET state=?,result_digest=?,updated_at=? WHERE token=?", (state, digest(result), now(), row["effect_token"]))
            db.execute("UPDATE runtime_intents SET state=?,version=version+1,container_id=COALESCE(?,container_id),domain_json=COALESCE(?,domain_json),domain_digest=COALESCE(?,domain_digest),exit_evidence_json=COALESCE(?,exit_evidence_json),exit_evidence_digest=COALESCE(?,exit_evidence_digest),error_code=?,updated_at=? WHERE intent_id=? AND version=?",
                       (state, container_id, canonical(domain) if domain is not None else None,
                        digest(domain) if domain is not None else None, canonical(evidence) if evidence is not None else None,
                        digest(evidence) if evidence is not None else None, error, now(), row["intent_id"], row["version"]))
            self.fault("result.before_commit")
        return self.get(row["intent_id"])

    def create_domain(self, intent_id, version):
        row = self._begin(intent_id, version, "domain", {"prepared"})
        try:
            self.engine.verify_engine()
            record = self.observer.create(row["container_name"])
            self.fault("domain.after_external")
        except ContainerError as error:
            return self._finish(row, "domain_unknown", error=error.code)
        return self._finish(row, "domain_ready", domain=record)

    def create(self, intent_id, version):
        row = self.get(intent_id)
        if row["version"] != version or row["state"] != "domain_ready":
            raise ContainerError("runtime_state_conflict")
        self.engine.verify_engine(); self.engine.verify_image(self.policy.image)
        self.observer.verify(json.loads(row["domain_json"]))
        row = self._begin(intent_id, version, "create", {"domain_ready"})
        try:
            container_id = self.engine.create(row["container_name"], json.loads(row["spec_json"]))
            self.fault("create.after_external")
        except ContainerError as error:
            # Even a server error must not grant a second create. A fixed-name
            # collision explicitly remains unowned, not a recovery candidate.
            state = "create_rejected" if error.code in {"engine_name_conflict", "engine_permission_denied"} else "create_unknown"
            return self._finish(row, state, error=error.code)
        row = self._finish(row, "created_unverified", container_id=container_id)
        return self.reconcile(row["intent_id"], row["version"])

    def _inspect(self, row):
        self.engine.verify_engine()
        self.observer.verify(json.loads(row["domain_json"]))
        inspected = self.engine.inspect(row["container_id"])
        verify_inspection(row, inspected)
        return inspected

    def _membership(self, row, inspected):
        if inspected["State"]["Running"] is not True or inspected["State"]["Pid"] <= 0:
            raise ContainerError("container_init_unobserved")
        record = json.loads(row["domain_json"])
        membership = self.observer.observe_member(record, inspected["State"]["Pid"])
        second = self._inspect(row)
        if second["State"] != inspected["State"]:
            raise ContainerError("container_state_changed")
        if self.observer.observe_member(record, second["State"]["Pid"]) != membership:
            raise ContainerError("container_pid_reused")
        record["membership"] = membership
        return record

    def reconcile(self, intent_id, version):
        row = self.get(intent_id)
        if row["version"] != version:
            raise ContainerError("runtime_state_conflict")
        if row["state"] in {"create_pending", "create_unknown"} and not row["container_id"]:
            self.engine.verify_engine()
            self.observer.verify(json.loads(row["domain_json"]))
            inspected = self.engine.inspect_intent_name(row["container_name"])
            container_id = verify_inspection(row, inspected)
            row = self._finish(row, "created_unverified", container_id=container_id)
        if row["state"] == "created_unverified":
            inspected = self._inspect(row)
            if inspected["State"]["Running"] or inspected["State"].get("Status") != "created":
                return self._finish(row, "quarantined", error="unexpected_container_execution")
            return self._finish(row, "created")
        if row["state"] in {"start_pending", "start_unknown"}:
            inspected = self._inspect(row)
            if not inspected["State"]["Running"]:
                # A timed-out start can still execute later. Exited/404 is not
                # proof of an unexecuted or safely retryable start request.
                return self._finish(row, "start_unknown", error="start_execution_unconfirmed")
            return self._finish(row, "running", domain=self._membership(row, inspected))
        if row["state"] in {"stop_pending", "stop_unknown", "exit_unconfirmed"}:
            with self.repository._connect() as db:
                started = db.execute("SELECT 1 FROM runtime_effects WHERE intent_id=? AND action='start'", (intent_id,)).fetchone()
            if not started:
                return self._confirm_never_started(row)
            return self._confirm_stopped(row)
        return row

    def start(self, intent_id, version):
        row = self.get(intent_id)
        if row["version"] != version or row["state"] != "created":
            raise ContainerError("runtime_state_conflict")
        inspected = self._inspect(row)
        if inspected["State"]["Running"] or inspected["State"].get("Status") != "created":
            raise ContainerError("container_start_state_untrusted")
        row = self._begin(intent_id, version, "start", {"created"})
        try:
            self.engine.start(row["container_id"])
            self.fault("start.after_external")
            inspected = self._inspect(row)
            domain = self._membership(row, inspected)
        except ContainerError as error:
            return self._finish(row, "start_unknown", error=error.code)
        return self._finish(row, "running", domain=domain)

    def stop(self, intent_id, version):
        row = self.get(intent_id)
        if row["version"] != version or row["state"] != "running":
            raise ContainerError("runtime_state_conflict")
        self._inspect(row)  # zero mutation if external identity/config drifted.
        row = self._begin(intent_id, version, "stop", {"running"})
        try:
            self.engine.stop(row["container_id"])
            self.fault("stop.after_external")
            self.engine.wait(row["container_id"])
        except ContainerError as error:
            return self._finish(row, "stop_unknown", error=error.code)
        return self._confirm_stopped(row)

    @staticmethod
    def _removal_request(row):
        return {
            "intent_id": row["intent_id"], "engine_id": row["engine_id"],
            "container_id": row["container_id"],
            "exit_evidence_digest": row["exit_evidence_digest"],
        }

    def _finish_removal(self, intent_id, request_digest, state, error=None):
        result = {"intent_id": intent_id, "state": state, "error": error}
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT * FROM runtime_container_removals WHERE intent_id=?",
                (intent_id,),
            ).fetchone()
            if current is None or current["request_digest"] != request_digest:
                raise ContainerError("container_removal_identity_changed")
            db.execute(
                """UPDATE runtime_container_removals
                   SET state=?,result_digest=?,error_code=?,updated_at=?
                   WHERE intent_id=? AND request_digest=?""",
                (state, digest(result) if state == "removed" else None,
                 error, now(), intent_id, request_digest),
            )
        return result

    def remove_exited(self, intent_id):
        """Remove one exact, proven-exited container without touching its evidence."""
        if self.historical_removal_check is None:
            row = self.get(intent_id)
        else:
            # Removal uses the immutable execution specification, not defaults
            # of today's launcher. Only a bound historical package grants this
            # path; ordinary get/create/start retain current-policy validation.
            with self.repository._connect() as db:
                raw = db.execute("SELECT * FROM runtime_intents WHERE intent_id=?",
                                 (identifier(intent_id),)).fetchone()
            if raw is None:
                raise ContainerError("runtime_intent_missing")
            row = self._checked(dict(raw), current_policy=False)
            self.historical_removal_check(row)
        if row["state"] != "exited" or not row["exit_evidence_json"]:
            raise ContainerError("container_exit_unconfirmed")
        if row["container_id"] is None:
            return {"intent_id": intent_id, "state": "removed", "error": None}
        request = self._removal_request(row)
        request_digest = digest(request)
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM runtime_container_removals WHERE intent_id=?",
                (intent_id,),
            ).fetchone()
            if existing is None:
                db.execute(
                    """INSERT INTO runtime_container_removals(
                       intent_id,engine_id,container_id,request_digest,state,
                       result_digest,error_code,created_at,updated_at)
                       VALUES(?,?,?,?,'pending',NULL,NULL,?,?)""",
                    (intent_id, row["engine_id"], row["container_id"],
                     request_digest, now(), now()),
                )
                self.fault("remove.before_intent_commit")
            elif (existing["engine_id"] != row["engine_id"] or
                  existing["container_id"] != row["container_id"] or
                  existing["request_digest"] != request_digest):
                raise ContainerError("container_removal_identity_changed")
            elif existing["state"] == "removed":
                return {"intent_id": intent_id, "state": "removed", "error": None}
            else:
                db.execute(
                    """UPDATE runtime_container_removals
                       SET state='pending',error_code=NULL,updated_at=?
                       WHERE intent_id=? AND request_digest=?""",
                    (now(), intent_id, request_digest),
                )
        self.fault("remove.after_intent_commit")

        try:
            self.engine.verify_engine()
            try:
                inspected = self.engine.inspect(row["container_id"])
            except ContainerError as exc:
                if exc.code == "engine_object_missing":
                    return self._finish_removal(intent_id, request_digest, "removed")
                raise
            verify_inspection(row, inspected)
            state = inspected["State"]
            if (state["Running"] or state["Pid"] != 0 or
                    state.get("Status") not in {"created", "exited"}):
                raise ContainerError("container_exit_unconfirmed")
            self.engine.remove(row["container_id"])
            self.fault("remove.after_external")
            try:
                self.engine.inspect(row["container_id"])
            except ContainerError as exc:
                if exc.code == "engine_object_missing":
                    return self._finish_removal(intent_id, request_digest, "removed")
                raise
            raise ContainerError("container_removal_unconfirmed", outcome_unknown=True)
        except ContainerError as exc:
            self._finish_removal(intent_id, request_digest, "unknown", exc.code)
            raise

    def recover_host_reboot(self, intent_id, version):
        """Close a proven old-boot execution without issuing any Docker mutation.

        Deliberately excludes ambiguous starts and missing containers. Callers
        must still recover the journal and confirm claim exit before release.
        """
        row = self.get(intent_id)
        if row["version"] != version:
            raise ContainerError("runtime_state_conflict")
        if row["state"] == "exited":
            return row
        if row["state"] not in {"running", "stop_pending", "stop_unknown", "exit_unconfirmed"}:
            return row
        record = json.loads(row["domain_json"])
        proof = self.observer.previous_boot(record)
        if proof is None:
            return row

        def stopped():
            self.engine.verify_engine()
            inspected = self.engine.inspect(row["container_id"])
            verify_inspection(row, inspected)
            state = inspected["State"]
            if state["Running"] or state["Pid"] != 0 or state.get("Status") != "exited":
                raise ContainerError("container_exit_unconfirmed")
            return state

        state = stopped()
        if row["state"] == "running":
            row = self._begin(intent_id, version, "stop", {"running"})
        if self.observer.previous_boot(record) != proof or stopped() != state:
            raise ContainerError("execution_domain_changed")
        evidence = {"schema": 1, "kind": "host_reboot", "engine_id": row["engine_id"],
                    "container_id": row["container_id"], "intent_id": row["intent_id"],
                    "instance_id": row["instance_id"], "epoch": row["epoch"],
                    "stop_effect": row["effect_token"], "spec_digest": row["spec_digest"],
                    "domain": proof, "container_state": state, "observed_at": now()}
        return self._finish(row, "exited", evidence=evidence)

    def observe_running(self, intent_id):
        row = self.get(intent_id)
        if row["state"] != "running":
            return False
        return bool(self._inspect(row)["State"]["Running"])

    def abandon_before_start(self, intent_id, version):
        """Close an intent that never issued start; unknown effects are ineligible."""
        with self.repository._connect() as db:
            if db.execute("SELECT 1 FROM runtime_effects WHERE intent_id=? AND (action='start' OR state='pending')", (intent_id,)).fetchone():
                raise ContainerError("runtime_start_or_effect_unconfirmed")
        row = self._begin(intent_id, version, "stop", {"prepared", "domain_ready", "created"})
        return self._confirm_never_started(row)

    def _confirm_never_started(self, row):
        if row["state"] not in {"stop_pending", "exit_unconfirmed"}:
            raise ContainerError("runtime_stop_fence_required")
        try:
            with self.repository._connect() as db:
                if db.execute("SELECT 1 FROM runtime_effects WHERE intent_id=? AND action='start'", (row["intent_id"],)).fetchone():
                    raise ContainerError("runtime_start_unconfirmed")
            self.engine.verify_engine()
            inspected = self._inspect(row) if row["container_id"] else None
            if inspected and (inspected["State"]["Running"] or inspected["State"]["Pid"] != 0 or inspected["State"].get("Status") != "created"):
                raise ContainerError("container_never_started_unconfirmed")
            domain = None
            if row["domain_json"]:
                record = json.loads(row["domain_json"])
                self.observer.verify(record)
                if record["identity"] is None and not Path(record["path"]).exists():
                    domain = {"identity": None, "populated": 0, "never_materialized": True}
                else:
                    fd = self.observer._open(record["path"])
                    try:
                        identity = self.observer._identity(fd)
                        if identity != record["identity"] or self.observer._populated(fd) != 0 or self.observer._identity(fd) != identity:
                            raise ContainerError("execution_domain_not_empty")
                        domain = {"identity": identity, "populated": 0}
                    finally:
                        os.close(fd)
            if inspected and self._inspect(row)["State"] != inspected["State"]:
                raise ContainerError("container_state_changed")
            evidence = {"schema": 1, "kind": "never_started", "engine_id": row["engine_id"],
                        "container_id": row["container_id"], "intent_id": row["intent_id"],
                        "instance_id": row["instance_id"], "epoch": row["epoch"],
                        "stop_effect": row["effect_token"], "spec_digest": row["spec_digest"], "domain": domain}
        except ContainerError as exc:
            return self._finish(row, "exit_unconfirmed", error=exc.code)
        return self._finish(row, "exited", evidence=evidence)

    def _confirm_stopped(self, row):
        if row["state"] not in {"stop_pending", "stop_unknown", "exit_unconfirmed"}:
            raise ContainerError("runtime_stop_fence_required")
        try:
            inspected = self._inspect(row)
            state = inspected["State"]
            if state["Running"] or state["Pid"] != 0 or state.get("Status") != "exited":
                raise ContainerError("container_exit_unconfirmed")
            record = json.loads(row["domain_json"])
            domain_proof = self.observer.prove_empty(record, record.get("membership"))
            second = self._inspect(row)
            if second["State"] != state:
                raise ContainerError("container_state_changed")
            # Re-read the original parent after engine observation, before CAS.
            if self.observer.prove_empty(record, record.get("membership")) != domain_proof:
                raise ContainerError("execution_domain_changed")
            evidence = {"schema": 1, "engine_id": row["engine_id"], "container_id": row["container_id"],
                        "intent_id": row["intent_id"], "instance_id": row["instance_id"], "epoch": row["epoch"],
                        "stop_effect": row["effect_token"], "spec_digest": row["spec_digest"],
                        "domain": domain_proof, "container_state": state, "observed_at": now()}
        except ContainerError as error:
            return self._finish(row, "exit_unconfirmed", error=error.code)
        return self._finish(row, "exited", evidence=evidence)
