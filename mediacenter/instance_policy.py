"""Single SQLite authority for residency, backend ownership and model commands.

Only trusted Server assembly supplies prepared runtime packages and observations.
This module never starts a process or treats telemetry as an exit certificate.
"""
from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timedelta, timezone

from .capabilities import worker_capability_for
from .residency import residency_modes_for
from .protocol import validate_envelope
from .task_state import TaskState, TaskStateError, canonical, digest, now, token


HOT_POLICY_FIELDS = frozenset({"residency", "idle_seconds"})
RESTART_POLICY_FIELDS = frozenset({"sharing_mode", "external_reserve_mib"})


def validate_policy(value):
    fields = {"backend", "residency", "idle_seconds", "gpus", "base_mib", "task_mib",
              "binding", "package_id", "sharing_mode", "external_reserve_mib"}
    if type(value) is not dict or set(value) != fields:
        raise TaskStateError("invalid_instance_policy", 400)
    if type(value["backend"]) is not str or type(value["residency"]) is not str or value["backend"] != "container" or value["residency"] not in {"on_demand", "idle", "resident"}:
        raise TaskStateError("invalid_instance_policy", 400)
    if type(value["sharing_mode"]) is not str or value["sharing_mode"] not in {"exclusive", "shared"}:
        raise TaskStateError("invalid_instance_policy", 400)
    for key, low, high in (("idle_seconds", 0, 86400), ("base_mib", 1, 1048576),
                           ("task_mib", 1, 1048576), ("external_reserve_mib", 0, 1048576)):
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise TaskStateError("invalid_instance_policy", 400)
    token(value["package_id"])
    gpus = value["gpus"]
    if type(gpus) is not list or not 1 <= len(gpus) <= 16 or any(type(gpu) is not str for gpu in gpus) or len(set(gpus)) != len(gpus):
        raise TaskStateError("invalid_gpu_binding", 400)
    if any(not isinstance(gpu, str) or not gpu.startswith("GPU-") for gpu in gpus):
        raise TaskStateError("invalid_gpu_binding", 400)
    for gpu in gpus:
        token(gpu)
    binding = value["binding"]
    if type(binding) is not dict or set(binding) != {"model_key", "recipe_revision", "model_asset_id", "model_asset_revision", "dependencies"}:
        raise TaskStateError("invalid_instance_binding", 400)
    for key in ("model_key", "recipe_revision", "model_asset_id", "model_asset_revision"):
        token(binding[key])
    if type(binding["dependencies"]) is not list or len(binding["dependencies"]) > 64:
        raise TaskStateError("invalid_dependency_binding", 400)
    for dep in binding["dependencies"]:
        if type(dep) is not dict or set(dep) != {"dependency_key", "deployment_id", "asset_id", "revision"}:
            raise TaskStateError("invalid_dependency_binding", 400)
        for item in dep.values():
            token(item)
    if len({dep["dependency_key"] for dep in binding["dependencies"]}) != len(binding["dependencies"]):
        raise TaskStateError("invalid_dependency_binding", 400)
    return json.loads(canonical(value))


class InstancePolicy:
    def __init__(self, repository, task_state=None, *, fault=lambda _: None):
        self.repository = repository
        self.tasks = task_state or TaskState(repository)
        self.fault = fault
        self.package_validator = None
        # Healthy heartbeats are high-frequency observations, not durable
        # milestones.  Keep the latest sequence/time in memory and write one
        # restart floor per claim for this Server process.  Error heartbeats
        # still enter the durable stop fence immediately.
        self._heartbeat_observations = {}
        self._heartbeat_lock = threading.Lock()

    @staticmethod
    def deployment_binding(db, deployment):
        asset = db.execute("SELECT state,revision FROM model_assets WHERE id=?", (deployment["asset_id"],)).fetchone()
        if not asset or asset["state"] != "ready" or asset["revision"] != deployment["revision"]:
            raise TaskStateError("deployment_asset_unavailable")
        installed = db.execute('SELECT * FROM instance_installation_bindings WHERE instance_id=?', (deployment['id'],)).fetchone()
        if not installed:
            raise TaskStateError('runtime_installation_binding_required')
        value = json.loads(installed['binding_json'])
        if (digest(value) != installed['binding_digest'] or installed['incarnation'] != deployment['incarnation']
                or installed['asset_id'] != deployment['asset_id'] or installed['asset_revision'] != deployment['revision']
                or value['model_id'] != deployment['model_id'] or value['catalog_key'] != deployment['catalog_key']):
            raise TaskStateError('runtime_installation_binding_changed')
        dependencies = []
        for row in db.execute("SELECT * FROM model_deployment_dependencies WHERE deployment_id=? ORDER BY dependency_key", (deployment["id"],)):
            target = db.execute("SELECT * FROM model_deployments WHERE id=?", (row["dependency_deployment_id"],)).fetchone()
            if not target or target["asset_id"] != row["dependency_asset_id"] or target["revision"] != row["dependency_revision"]:
                raise TaskStateError("deployment_dependency_changed")
            if target["install_state"] != "ready" or not db.execute("SELECT 1 FROM model_assets WHERE id=? AND revision=? AND state='ready'", (target["asset_id"],target["revision"])).fetchone():
                raise TaskStateError("deployment_dependency_unavailable")
            dependencies.append({"dependency_key": row["dependency_key"], "deployment_id": row["dependency_deployment_id"], "asset_id": row["dependency_asset_id"], "revision": row["dependency_revision"]})
        return {"model_key": deployment["catalog_key"], "recipe_revision": installed['recipe_digest'],
                "model_asset_id": deployment["asset_id"], "model_asset_revision": deployment["revision"], "dependencies": dependencies}

    @staticmethod
    def _policy(row):
        row = dict(row)
        try:
            value = json.loads(row["policy_json"])
            if digest(value) != row["policy_digest"]:
                raise ValueError()
            if value is not None:
                validate_policy(value)
        except (ValueError, TypeError, KeyError):
            raise TaskStateError("instance_policy_integrity_error") from None
        row["policy"] = value
        pending_raw = row.get("pending_policy_json")
        pending_digest = row.get("pending_policy_digest")
        if pending_raw is None:
            if pending_digest is not None or row.get("pending_restart_recovery") is not None:
                raise TaskStateError("instance_pending_policy_integrity_error")
            pending = None
        else:
            try:
                pending = json.loads(pending_raw)
                if digest(pending) != pending_digest:
                    raise ValueError()
                validate_policy(pending)
            except (ValueError, TypeError, KeyError):
                raise TaskStateError("instance_pending_policy_integrity_error") from None
            if row.get("configuration_state") not in {"restart_pending", "replace_pending", "applying", "failed"}:
                raise TaskStateError("instance_pending_policy_integrity_error")
        row["pending_policy"] = pending
        return row

    @staticmethod
    def assert_claim(db, instance, epoch, backend=None, claim_id=None, *, loaded=False):
        row = db.execute("SELECT c.*,d.incarnation AS current_incarnation FROM instance_claims c JOIN model_deployments d ON d.id=c.instance_id WHERE c.instance_id=? AND c.epoch=? AND c.state!='exited'", (instance, epoch)).fetchone()
        if (not row or row["incarnation"] != row["current_incarnation"] or
                backend is not None and row["backend"] != backend or
                claim_id is not None and row["claim_id"] != claim_id):
            raise TaskStateError("backend_claim_required")
        row = InstancePolicy._policy(row)
        if row["execution_json"] and digest(json.loads(row["execution_json"])) != row["execution_digest"]:
            raise TaskStateError("execution_identity_corrupt")
        if loaded and (row["state"] != "loaded" or not row["registered"]):
            raise TaskStateError("instance_not_loaded")
        return row

    def get(self, instance):
        with self.repository._connect() as db:
            row = db.execute("SELECT * FROM instance_policies WHERE instance_id=?", (instance,)).fetchone()
            result = self._policy(row) if row else None
            if result:
                claim = db.execute("SELECT * FROM instance_claims WHERE instance_id=? AND state!='exited'", (instance,)).fetchone()
                result["claim"] = self._policy(claim) if claim else None
            return result

    @staticmethod
    def assert_materialize(db, claim):
        """Authorize creation of the configured container without starting it.

        A stopped service is still an installed deployment.  Its immutable
        container may be created while admission is closed, but no process may
        be started until ``assert_launch`` also observes ``enabled=1``.
        """
        from .repository import Repository
        Repository.assert_not_removing_tx(db, claim["instance_id"])
        current = db.execute("SELECT * FROM instance_policies WHERE instance_id=?", (claim["instance_id"],)).fetchone()
        deployment = db.execute("SELECT install_state FROM model_deployments WHERE id=?", (claim["instance_id"],)).fetchone()
        current_policy = InstancePolicy._policy(current)["policy"] if current is not None else None
        claim_policy = InstancePolicy._policy(claim)["policy"]
        execution_keys = set(claim_policy) - HOT_POLICY_FIELDS
        if (current is None or deployment is None or deployment["install_state"] != "ready"
                or current_policy is None
                or any(current_policy[key] != claim_policy[key] for key in execution_keys)
                or claim["state"] not in {"claimed", "starting", "container_stopped"}):
            raise TaskStateError("backend_claim_fenced")

    @staticmethod
    def assert_launch(db, claim):
        current = db.execute("SELECT * FROM instance_policies WHERE instance_id=?", (claim["instance_id"],)).fetchone()
        deployment = db.execute("SELECT enabled,install_state FROM model_deployments WHERE id=?", (claim["instance_id"],)).fetchone()
        InstancePolicy.assert_materialize(db, claim)
        if deployment is None or not deployment["enabled"]:
            raise TaskStateError("backend_claim_fenced")

    @staticmethod
    def legacy_unreconciled(db):
        return any(db.execute(query).fetchone() for query in (
            "SELECT 1 FROM instance_policies WHERE status='legacy_unreconciled' LIMIT 1",
            "SELECT 1 FROM runtime_intents r WHERE (r.state!='exited' OR r.exit_evidence_json IS NULL) AND NOT EXISTS(SELECT 1 FROM instance_claims c WHERE c.instance_id=r.instance_id AND c.epoch=r.epoch) LIMIT 1",
            "SELECT 1 FROM task_attempts a WHERE a.mode='worker' AND a.exit_confirmed=0 AND NOT EXISTS(SELECT 1 FROM instance_claims c WHERE c.instance_id=a.instance_id AND c.epoch=a.epoch) LIMIT 1",
            "SELECT 1 FROM task_reservations r WHERE r.released=0 AND NOT EXISTS(SELECT 1 FROM instance_claims c WHERE c.instance_id=r.instance_id AND c.epoch=r.epoch) LIMIT 1"))

    def configure(self, instance, value, *, expected_version=None, restart_recovery=None,
                  deployment_revision=None):
        if isinstance(value, dict) and value.get("backend") == "legacy":
            raise TaskStateError("legacy_model_runtime_retired")
        value = validate_policy(value)
        if restart_recovery is not None and type(restart_recovery) is not bool:
            raise TaskStateError("invalid_restart_recovery", 400)
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.repository.assert_not_removing_tx(db, instance)
            if self.repository.active_configuration_context_tx(db, instance) is not None:
                raise TaskStateError("deployment_configuration_pending")
            deployment = db.execute("SELECT * FROM model_deployments WHERE id=?", (instance,)).fetchone()
            if not deployment:
                raise TaskStateError("deployment_not_found", 404)
            if value["binding"] != self.deployment_binding(db, deployment):
                raise TaskStateError("deployment_binding_mismatch")
            installed = db.execute('SELECT * FROM instance_installation_bindings WHERE instance_id=?', (instance,)).fetchone()
            if installed:
                template = json.loads(installed['template_json'])
                if digest(template) != installed['template_digest']:
                    raise TaskStateError('runtime_template_changed')
                fixed = template['resources']
                if (value['backend'] != 'container' or value['package_id'] != 'template_' + installed['template_digest']
                        or any(value[key] != fixed[key] for key in ('base_mib','task_mib'))):
                    raise TaskStateError('runtime_template_policy_mismatch')
            old = db.execute("SELECT * FROM instance_policies WHERE instance_id=?", (instance,)).fetchone()
            if old and old["status"] == "legacy_unreconciled":
                raise TaskStateError("legacy_execution_unreconciled")
            if (old and expected_version != old["version"] or
                    not old and expected_version not in {None, 0}):
                raise TaskStateError("instance_version_conflict")
            current = self._policy(old) if old else None
            if current and current["pending_policy"] is not None:
                raise TaskStateError("instance_configuration_pending")
            active = db.execute("SELECT 1 FROM instance_claims WHERE instance_id=? AND state!='exited'", (instance,)).fetchone()
            previous = current["policy"] if current else None
            changed = ({key for key in value if previous is None or previous[key] != value[key]})
            recovery = int(restart_recovery if restart_recovery is not None else bool(old and old["restart_recovery"]))
            recovery_changed = bool(old and recovery != old["restart_recovery"])
            versioned = deployment_revision is not None
            if versioned:
                if previous is None or not changed:
                    raise TaskStateError("deployment_config_revision_invalid")
                if db.execute(
                        """SELECT 1 FROM tasks
                           WHERE model_key=? AND status='queued' LIMIT 1""",
                        (instance,),
                ).fetchone():
                    raise TaskStateError("deployment_queue_not_empty")
                current_config = deployment["current_config_revision"]
                if type(current_config) is not int:
                    raise TaskStateError("deployment_config_revision_unavailable")
                expected = {
                    "gpu_uuids": value["gpus"],
                    "sharing_mode": value["sharing_mode"],
                    "external_reserve_mib": value["external_reserve_mib"],
                    "residency": value["residency"],
                    "idle_seconds": value["idle_seconds"],
                }
                if any(deployment_revision.get(key) != expected_value
                       for key, expected_value in expected.items()):
                    raise TaskStateError("deployment_config_revision_invalid")
                try:
                    self.repository.stage_model_deployment_revision_tx(
                        db, deployment_revision, expected_current=current_config)
                except ValueError as exc:
                    raise TaskStateError(str(exc)) from None
            if active and (changed - HOT_POLICY_FIELDS):
                mode = ("restart_pending" if changed <= RESTART_POLICY_FIELDS | HOT_POLICY_FIELDS
                        else "replace_pending")
                resume = int(bool(deployment["enabled"]))
                db.execute("""UPDATE instance_policies
                              SET pending_policy_json=?,pending_policy_digest=?,pending_restart_recovery=?,
                                  configuration_state=?,configuration_error=NULL,resume_after_apply=?,
                                  desired_state='unloaded',revision=revision+1,version=version+1
                              WHERE instance_id=?""",
                           (canonical(value), digest(value), recovery, mode, resume, instance))
                db.execute("""UPDATE model_deployments
                              SET enabled=0,desired_state='unloaded',updated_at=? WHERE id=?""",
                           (now(), instance))
                self.fault("policy.configure_pending")
            elif active and versioned:
                # Hot policy fields avoid a container restart, but they still
                # have a health boundary. Retain the previous ready policy as
                # rollback data until Worker/model state proves the candidate.
                claim = db.execute(
                    "SELECT state FROM instance_claims WHERE instance_id=? AND state!='exited'",
                    (instance,),
                ).fetchone()
                keep_idle = bool(
                    deployment["enabled"] and value["residency"] == "idle"
                    and claim and claim["state"] in {"loading", "loaded"}
                )
                desired = "loaded" if deployment["enabled"] and (
                    value["residency"] == "resident" or keep_idle
                ) else "unloaded"
                db.execute("""UPDATE instance_policies
                              SET policy_json=?,policy_digest=?,restart_recovery=?,desired_state=?,
                                  configuration_state='applying',configuration_error=NULL,
                                  pending_policy_json=?,pending_policy_digest=?,
                                  pending_restart_recovery=?,resume_after_apply=?,
                                  last_activity=CASE WHEN ? THEN ? ELSE last_activity END,
                                  revision=revision+1,version=version+1
                              WHERE instance_id=?""",
                           (canonical(value), digest(value), recovery, desired,
                            canonical(previous), digest(previous), old["restart_recovery"],
                            int(bool(deployment["enabled"])), int(keep_idle), now(), instance))
                self.fault("policy.configure_hot_pending")
            elif active:
                if not changed and not recovery_changed:
                    return current
                # Residency and recovery preferences do not alter the Docker
                # spec, GPU reservation, model binding or current command
                # generation. They are safe to apply without stopping work.
                claim = db.execute(
                    "SELECT state FROM instance_claims WHERE instance_id=? AND state!='exited'",
                    (instance,),
                ).fetchone()
                keep_idle = bool(
                    deployment["enabled"] and value["residency"] == "idle"
                    and claim and claim["state"] in {"loading", "loaded"}
                )
                desired = "loaded" if deployment["enabled"] and (
                    value["residency"] == "resident" or keep_idle
                ) else "unloaded"
                db.execute("""UPDATE instance_policies
                              SET policy_json=?,policy_digest=?,restart_recovery=?,desired_state=?,
                                  configuration_state='applied',configuration_error=NULL,
                                  last_activity=CASE WHEN ? THEN ? ELSE last_activity END,
                                  revision=revision+1,version=version+1
                              WHERE instance_id=?""",
                           (canonical(value), digest(value), recovery, desired,
                            int(keep_idle), now(), instance))
                self.fault("policy.configure_hot")
            elif versioned:
                revision = old["revision"] + 1
                desired = ("loaded" if deployment["enabled"] and
                           value["residency"] == "resident" else "unloaded")
                db.execute("""UPDATE instance_policies
                              SET incarnation=?,revision=?,desired_state=?,policy_json=?,
                                  policy_digest=?,status='waiting_runtime',error_code=NULL,
                                  restart_recovery=?,configuration_state='applying',
                                  pending_policy_json=?,pending_policy_digest=?,
                                  pending_restart_recovery=?,resume_after_apply=?,
                                  configuration_error=NULL,version=version+1
                              WHERE instance_id=?""",
                           (deployment["incarnation"], revision, desired, canonical(value),
                            digest(value), recovery, canonical(previous), digest(previous),
                            old["restart_recovery"], int(bool(deployment["enabled"])), instance))
                self.fault("policy.configure_pending_without_claim")
            else:
                revision = old["revision"] + 1 if old else 1
                desired = ("loaded" if deployment["enabled"] and
                           value["residency"] == "resident" else "unloaded")
                db.execute("""INSERT INTO instance_policies(
                              instance_id,incarnation,revision,desired_state,policy_json,policy_digest,
                              status,restart_recovery,configuration_state)
                              VALUES(?,?,?,?,?,?,'waiting_runtime',?,'applied')
                              ON CONFLICT(instance_id) DO UPDATE SET
                              incarnation=excluded.incarnation,revision=excluded.revision,
                              desired_state=excluded.desired_state,policy_json=excluded.policy_json,
                              policy_digest=excluded.policy_digest,status='waiting_runtime',error_code=NULL,
                              restart_recovery=excluded.restart_recovery,configuration_state='applied',
                              pending_policy_json=NULL,pending_policy_digest=NULL,
                              pending_restart_recovery=NULL,resume_after_apply=0,
                              configuration_error=NULL,version=instance_policies.version+1""",
                           (instance, deployment["incarnation"], revision, desired,
                            canonical(value), digest(value), recovery))
                self.fault("policy.configure")
        return self.get(instance)

    def set_service(self, instance, started, *, expected_version=None):
        """Atomically control task admission and the configured residency goal.

        Stopping closes admission immediately but lets an already dispatched
        attempt finish. Reconciliation unloads the model and container after
        the durable task/journal boundary is clear. With applied configuration
        and no dispatched attempt, a pending/accepted preload is instead fenced
        for Controller stop: unload cannot overtake that load in the child.
        Only confirmed container exit releases its reservation. This does not
        impose a model-load deadline or alter pending configuration rollback.
        """
        if type(started) is not bool:
            raise TaskStateError("invalid_service_state", 400)
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.repository.assert_not_removing_tx(db, instance)
            if self.repository.active_configuration_context_tx(db, instance) is not None:
                raise TaskStateError("deployment_configuration_pending")
            raw = db.execute("SELECT * FROM instance_policies WHERE instance_id=?", (instance,)).fetchone()
            deployment = db.execute("SELECT * FROM model_deployments WHERE id=?", (instance,)).fetchone()
            if raw is None or deployment is None:
                raise TaskStateError("instance_policy_required", 404)
            row = self._policy(raw)
            if expected_version is not None and expected_version != row["version"]:
                raise TaskStateError("instance_version_conflict")
            if deployment["install_state"] != "ready" or row["policy"] is None:
                raise TaskStateError("deployment_not_ready")
            if row["pending_policy"] is not None:
                db.execute("""UPDATE instance_policies
                              SET resume_after_apply=?,desired_state='unloaded',version=version+1
                              WHERE instance_id=?""", (int(started), instance))
                db.execute("UPDATE model_deployments SET enabled=0,desired_state='unloaded',updated_at=? WHERE id=?",
                           (now(), instance))
                self.fault("service.pending_intent")
            else:
                if started:
                    # Validate the exact installed asset/dependency binding before
                    # exposing the service to new task admission.
                    self.deployment_binding(db, deployment)
                desired = "loaded" if started and row["policy"]["residency"] == "resident" else "unloaded"
                active = db.execute("SELECT * FROM instance_claims WHERE instance_id=? AND state!='exited'", (instance,)).fetchone()
                status = row["status"]
                if not active:
                    status = "waiting_runtime" if started else "unloaded"
                changed = bool(deployment["enabled"]) != started or row["desired_state"] != desired
                if changed:
                    db.execute("""UPDATE instance_policies SET desired_state=?,status=?,error_code=NULL,
                                  revision=revision+1,version=version+1,resume_after_apply=0
                                  WHERE instance_id=?""", (desired, status, instance))
                    db.execute("""UPDATE model_deployments SET enabled=?,desired_state=?,actual_state=?,
                                  runtime_last_error=NULL,runtime_updated_at=?,updated_at=? WHERE id=?""",
                               (int(started), desired, status if status in {"unloaded", "loading", "loaded", "unloading", "error"} else "unloaded",
                                now(), now(), instance))
                    self.fault("service.state")
                if (not started and row["configuration_state"] == "applied" and active is not None
                        and not active["stop_reason"]
                        and db.execute("SELECT 1 FROM model_operations WHERE claim_id=? AND action='load' "
                                       "AND state IN ('pending','accepted')", (active["claim_id"],)).fetchone()
                        and not db.execute("SELECT 1 FROM task_attempts WHERE instance_id=? AND exit_confirmed=0",
                                           (instance,)).fetchone()):
                    # Loading and unload share the inference child's execution
                    # lane. Queueing unload cannot stop a stuck load. Persist
                    # the exact claim fence in the same admission transaction;
                    # Controller exit confirmation alone may release its base
                    # lease. Already-dispatched tasks retain their drain path.
                    claim = self.assert_claim(db, instance, active["epoch"], backend="container",
                                              claim_id=active["claim_id"])
                    self._fence_stop(db, claim, "operator_stop")
                    self.fault("service.stop_loading")
        return self.get(instance)

    def begin_configuration_operation(self, operation_id):
        """Stage a prepared full-binding update in the existing policy machine."""
        with self.repository._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            context = self.repository.configuration_context_tx(db, operation_id)
            if context is None:
                raise TaskStateError('deployment_configuration_context_required')
            if context['phase'] in {'draining', 'applying', 'committed'}:
                return False
            if context['phase'] != 'prepared' or context['candidate'] is None:
                raise TaskStateError('deployment_configuration_candidate_required')
            instance = context['instance_id']
            raw = db.execute('SELECT * FROM instance_policies WHERE instance_id=?', (instance,)).fetchone()
            deployment = db.execute('SELECT * FROM model_deployments WHERE id=?', (instance,)).fetchone()
            installed = db.execute('SELECT * FROM instance_installation_bindings WHERE instance_id=?', (instance,)).fetchone()
            previous = context['previous']
            if (raw is None or deployment is None or installed is None
                    or deployment['incarnation'] != context['incarnation']
                    or deployment['current_config_revision'] != context['expected_configuration']['config_revision']
                    or deployment['pending_config_revision'] != context['revision']['config_revision']
                    or dict(installed) != previous['installation']):
                raise TaskStateError('deployment_config_revision_conflict')
            current = self._policy(raw)
            if (current['configuration_state'] != 'applied' or current['pending_policy'] is not None
                    or current['policy'] != previous['policy']
                    or current['restart_recovery'] != previous['restart_recovery']
                    or bool(deployment['enabled']) != previous['enabled']):
                raise TaskStateError('deployment_configuration_changed')
            candidate = context['candidate']['installation']
            template = json.loads(candidate['template_json'])
            revision = context['revision']
            operation = db.execute('SELECT payload_json,state FROM deployment_operations WHERE id=?', (operation_id,)).fetchone()
            if operation['state'] not in {'creating_container', 'starting_worker', 'health_check'}:
                raise TaskStateError('deployment_operation_changed')
            options = json.loads(operation['payload_json'])['policy_options']
            policy = dict(current['policy'], **template['resources'])
            policy.update(package_id='template_'+candidate['template_digest'],
                binding={'model_key': 'sdxl-single-file', 'recipe_revision': candidate['recipe_digest'],
                    'model_asset_id': candidate['asset_id'], 'model_asset_revision': candidate['asset_revision'],
                    'dependencies': []}, gpus=revision['gpu_uuids'], residency=revision['residency'],
                idle_seconds=revision['idle_seconds'], sharing_mode=revision['sharing_mode'],
                external_reserve_mib=revision['external_reserve_mib'])
            policy = validate_policy(policy)
            old_digest = digest(context)
            context['candidate']['policy'] = policy
            context['previous_epochs'] = [row[0] for row in db.execute(
                "SELECT epoch FROM instance_claims WHERE instance_id=? AND state!='exited'", (instance,))]
            if context['requires_restart']:
                context['phase'] = 'draining'
                db.execute("UPDATE instance_policies SET pending_policy_json=?,pending_policy_digest=?,"
                    "pending_restart_recovery=?,configuration_state='replace_pending',configuration_error=NULL,"
                    "resume_after_apply=?,desired_state='unloaded',revision=revision+1,version=version+1 WHERE instance_id=?",
                    (canonical(policy), digest(policy), int(options['restart_recovery']), int(previous['enabled']), instance))
                db.execute("UPDATE model_deployments SET enabled=0,desired_state='unloaded',updated_at=? WHERE id=?",
                           (now(), instance))
            else:
                context['phase'] = 'applying'
                self.repository.switch_configuration_binding_tx(db, context)
                desired = 'loaded' if previous['enabled'] and policy['residency'] == 'resident' else 'unloaded'
                db.execute("UPDATE instance_policies SET policy_json=?,policy_digest=?,restart_recovery=?,"
                    "configuration_state='applying',configuration_error=NULL,pending_policy_json=?,pending_policy_digest=?,"
                    "pending_restart_recovery=?,resume_after_apply=?,desired_state=?,revision=revision+1,version=version+1 "
                    "WHERE instance_id=?", (canonical(policy), digest(policy), int(options['restart_recovery']),
                    canonical(previous['policy']), digest(previous['policy']), int(previous['restart_recovery']),
                    int(previous['enabled']), desired, instance))
                db.execute('UPDATE model_deployments SET desired_state=?,updated_at=? WHERE id=?', (desired, now(), instance))
            self.repository.store_configuration_context_tx(db, context, expected_digest=old_digest)
            self.fault('policy.configuration_operation_staged')
        return True

    def apply_pending(self, instance):
        """Begin a validated policy revision while retaining its rollback base."""
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            raw = db.execute("SELECT * FROM instance_policies WHERE instance_id=?", (instance,)).fetchone()
            if raw is None:
                return False
            row = self._policy(raw)
            if row["configuration_state"] not in {"restart_pending", "replace_pending"}:
                return False
            pending = row["pending_policy"]
            if pending is None:
                raise TaskStateError("instance_pending_policy_integrity_error")
            if (db.execute("SELECT 1 FROM instance_claims WHERE instance_id=? AND state!='exited'", (instance,)).fetchone()
                    or db.execute("SELECT 1 FROM runtime_intents WHERE instance_id=? AND state!='exited'", (instance,)).fetchone()
                    or db.execute("SELECT 1 FROM task_attempts WHERE instance_id=? AND exit_confirmed=0", (instance,)).fetchone()):
                return False
            deployment = db.execute("SELECT * FROM model_deployments WHERE id=?", (instance,)).fetchone()
            if deployment is None or deployment["install_state"] != "ready":
                raise TaskStateError("deployment_not_ready")
            resume = bool(row["resume_after_apply"])
            desired = "loaded" if resume and pending["residency"] == "resident" else "unloaded"
            recovery = int(bool(row["pending_restart_recovery"]))
            context = self.repository.active_configuration_context_tx(db, instance)
            if context is not None:
                if (context['phase'] != 'draining' or context['candidate']['policy'] != pending
                        or deployment['pending_config_revision'] != context['revision']['config_revision']):
                    raise TaskStateError('deployment_configuration_changed')
                old_digest = digest(context)
                self.repository.switch_configuration_binding_tx(db, context)
                context['phase'] = 'applying'
                self.repository.store_configuration_context_tx(db, context, expected_digest=old_digest)
            db.execute("""UPDATE instance_policies SET policy_json=?,policy_digest=?,restart_recovery=?,
                          desired_state=?,status='waiting_runtime',error_code=NULL,
                          configuration_state='applying',pending_policy_json=?,
                          pending_policy_digest=?,pending_restart_recovery=?,
                          configuration_error=NULL,version=version+1
                          WHERE instance_id=?""",
                       (canonical(pending), digest(pending), recovery, desired,
                        canonical(row["policy"]), digest(row["policy"]),
                        int(bool(row["restart_recovery"])), instance))
            db.execute("""UPDATE model_deployments SET enabled=?,desired_state=?,actual_state='unloaded',
                          runtime_last_error=NULL,runtime_updated_at=?,updated_at=? WHERE id=?""",
                       (int(resume), desired, now(), now(), instance))
            self.fault("policy.pending_applied")
            return True

    def commit_configuration(self, instance):
        """Promote an applying policy only after its required health boundary."""
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            raw = db.execute("SELECT * FROM instance_policies WHERE instance_id=?", (instance,)).fetchone()
            if raw is None:
                raise TaskStateError("instance_policy_required")
            row = self._policy(raw)
            if row["configuration_state"] == "applied":
                return row
            if row["configuration_state"] != "applying" or row["pending_policy"] is None:
                raise TaskStateError("instance_configuration_not_committable")
            context = self.repository.active_configuration_context_tx(db, instance)
            if context is not None:
                self._verify_configuration_health(db, context, row)
                old_digest = digest(context)
                context['phase'] = 'committed'
                self.repository.store_configuration_context_tx(db, context, expected_digest=old_digest)
                db.execute("UPDATE deployment_operations SET state='ready',milestone='configuration_applied',"
                    "result_json=?,recovery_cursor_json=?,updated_at=? WHERE id=? AND state NOT IN ('failed','canceled','ready')",
                    (canonical({'deployment_id': instance, 'config_revision': context['revision']['config_revision'],
                        'config_digest': context['revision']['config_digest'], 'service_started': context['previous']['enabled']}),
                     canonical({'step': 'configuration_applied'}), now(), context['operation_id']))
            db.execute("""UPDATE instance_policies
                          SET configuration_state='applied',pending_policy_json=NULL,
                              pending_policy_digest=NULL,pending_restart_recovery=NULL,
                              resume_after_apply=0,configuration_error=NULL,version=version+1
                          WHERE instance_id=?""", (instance,))
            self.repository.commit_model_deployment_revision_tx(db, instance, now())
            self.fault("policy.configuration_committed")
        return self.get(instance)

    def _verify_configuration_health(self, db, context, policy):
        operation = db.execute('SELECT state FROM deployment_operations WHERE id=?', (context['operation_id'],)).fetchone()
        if operation is None or operation['state'] != 'health_check':
            raise TaskStateError('deployment_operation_changed')
        if context['phase'] != 'applying' or policy['policy'] != context['candidate']['policy']:
            raise TaskStateError('deployment_configuration_changed')
        installed = db.execute('SELECT * FROM instance_installation_bindings WHERE instance_id=?',
                               (context['instance_id'],)).fetchone()
        deployment = db.execute('SELECT * FROM model_deployments WHERE id=?', (context['instance_id'],)).fetchone()
        if (installed is None or dict(installed) != context['candidate']['installation']
                or deployment is None or deployment['incarnation'] != context['incarnation']
                or deployment['pending_config_revision'] != context['revision']['config_revision']):
            raise TaskStateError('deployment_configuration_binding_changed')
        claim = db.execute("SELECT * FROM instance_claims WHERE instance_id=? AND state!='exited'",
                           (context['instance_id'],)).fetchone()
        if (claim is None or claim['stop_reason'] or claim['incarnation'] != context['incarnation']
                or context['requires_restart'] and claim['epoch'] in context['previous_epochs']):
            raise TaskStateError('deployment_configuration_health_unconfirmed')
        intent = db.execute('SELECT state FROM runtime_intents WHERE instance_id=? AND epoch=?',
                            (context['instance_id'], claim['epoch'])).fetchone()
        package = db.execute("SELECT record_json,record_digest FROM runtime_epoch_packages WHERE instance_id=? AND epoch=? AND phase='ready'",
                             (context['instance_id'], claim['epoch'])).fetchone()
        if intent is None or package is None:
            raise TaskStateError('deployment_configuration_health_unconfirmed')
        evidence = json.loads(package['record_json'])
        if (digest(evidence) != package['record_digest']
                or evidence.get('binding_digest') != installed['binding_digest']):
            raise TaskStateError('deployment_configuration_health_unconfirmed')
        if context['previous']['enabled']:
            healthy = intent['state'] == 'running' and claim['registered'] and (
                policy['policy']['residency'] != 'resident' or claim['state'] == 'loaded')
        else:
            healthy = intent['state'] == 'created' and claim['state'] == 'container_stopped'
        if not healthy:
            raise TaskStateError('deployment_configuration_health_unconfirmed')

    def begin_configuration_rollback(self, instance, error_code):
        """Fence a failed candidate; reconciliation drains it before restore."""
        token(error_code)
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._begin_configuration_rollback_tx(db, instance, error_code)
        return self.get(instance)

    def _begin_configuration_rollback_tx(self, db, instance, error_code):
        """Share one atomic boundary with the candidate's liveness fence."""
        raw = db.execute("SELECT * FROM instance_policies WHERE instance_id=?", (instance,)).fetchone()
        if raw is None:
            raise TaskStateError("instance_policy_required")
        row = self._policy(raw)
        if row['configuration_state'] == 'failed':
            return
        if row['configuration_state'] != 'applying' or row['pending_policy'] is None:
            raise TaskStateError('instance_configuration_not_rollbackable')
        stamp = now()
        db.execute("""UPDATE instance_policies
                      SET configuration_state='failed',configuration_error=?,
                          desired_state='unloaded',status='quarantined',
                          error_code=?,version=version+1
                      WHERE instance_id=?""", (error_code, error_code, instance))
        db.execute("""UPDATE model_deployments
                      SET enabled=0,desired_state='unloaded',runtime_last_error=?,
                          runtime_updated_at=?,updated_at=? WHERE id=?""",
                   (error_code, stamp, stamp, instance))
        self.fault('policy.configuration_rollback_requested')

    def rollback_configuration(self, instance):
        """Restore the retained ready policy after the failed execution exits."""
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            raw = db.execute("SELECT * FROM instance_policies WHERE instance_id=?", (instance,)).fetchone()
            if raw is None:
                raise TaskStateError("instance_policy_required")
            row = self._policy(raw)
            if row["configuration_state"] != "failed":
                return False
            if row["pending_policy"] is None:
                raise TaskStateError("instance_pending_policy_integrity_error")
            if (db.execute("SELECT 1 FROM instance_claims WHERE instance_id=? AND state!='exited'", (instance,)).fetchone()
                    or db.execute("SELECT 1 FROM runtime_intents WHERE instance_id=? AND state!='exited'", (instance,)).fetchone()
                    or db.execute("SELECT 1 FROM task_attempts WHERE instance_id=? AND exit_confirmed=0", (instance,)).fetchone()):
                return False
            previous = row["pending_policy"]
            resume = bool(row["resume_after_apply"])
            desired = "loaded" if resume and previous["residency"] == "resident" else "unloaded"
            error_code = row["configuration_error"]
            stamp = now()
            context = self.repository.active_configuration_context_tx(db, instance)
            if context is not None:
                if context['phase'] != 'applying' or context['previous']['policy'] != previous:
                    raise TaskStateError('deployment_configuration_changed')
                self.repository.switch_configuration_binding_tx(db, context, restore=True)
                self._finish_configuration_rollback_tx(db, context, error_code, stamp)
            db.execute("""UPDATE instance_policies
                          SET policy_json=?,policy_digest=?,restart_recovery=?,desired_state=?,
                              status='waiting_runtime',error_code=NULL,configuration_state='applied',
                              pending_policy_json=NULL,pending_policy_digest=NULL,
                              pending_restart_recovery=NULL,resume_after_apply=0,
                              configuration_error=?,version=version+1
                          WHERE instance_id=?""",
                       (canonical(previous), digest(previous),
                        int(bool(row["pending_restart_recovery"])), desired,
                        "rolled_back:" + error_code, instance))
            db.execute("""UPDATE model_deployments
                          SET enabled=?,desired_state=?,actual_state='unloaded',
                              runtime_last_error=NULL,runtime_updated_at=?,updated_at=?
                          WHERE id=?""", (int(resume), desired, stamp, stamp, instance))
            self.repository.rollback_model_deployment_revision_tx(db, instance, stamp)
            self.fault("policy.configuration_rolled_back")
        return True

    def abort_configuration_operation(self, operation_id, error_code):
        """Before activation restore intent only; after activation drain first."""
        token(error_code)
        with self.repository._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            context = self.repository.configuration_context_tx(db, operation_id)
            if context is None or context['phase'] in {'committed', 'rolled_back'}:
                return False
            instance = context['instance_id']
            if context['phase'] == 'applying':
                raw = db.execute('SELECT * FROM instance_policies WHERE instance_id=?', (instance,)).fetchone()
                if raw is None or raw['configuration_state'] not in {'applying', 'failed'}:
                    raise TaskStateError('deployment_configuration_changed')
                if raw['configuration_state'] == 'failed':
                    return False
                db.execute("UPDATE instance_policies SET configuration_state='failed',configuration_error=?,"
                    "desired_state='unloaded',status='quarantined',error_code=?,version=version+1 WHERE instance_id=?",
                    (error_code, error_code, instance))
                db.execute("UPDATE model_deployments SET enabled=0,desired_state='unloaded',updated_at=? WHERE id=?",
                           (now(), instance))
            else:
                installed = db.execute('SELECT * FROM instance_installation_bindings WHERE instance_id=?', (instance,)).fetchone()
                deployment = db.execute('SELECT * FROM model_deployments WHERE id=?', (instance,)).fetchone()
                if (installed is None or dict(installed) != context['previous']['installation']
                        or deployment is None or deployment['incarnation'] != context['incarnation']
                        or deployment['pending_config_revision'] != context['revision']['config_revision']):
                    raise TaskStateError('deployment_configuration_binding_changed')
                if context['phase'] == 'draining':
                    previous = context['previous']
                    desired = 'loaded' if previous['enabled'] and previous['policy']['residency'] == 'resident' else 'unloaded'
                    db.execute("UPDATE instance_policies SET configuration_state='applied',configuration_error=?,"
                        "pending_policy_json=NULL,pending_policy_digest=NULL,pending_restart_recovery=NULL,"
                        "resume_after_apply=0,desired_state=?,revision=revision+1,version=version+1 WHERE instance_id=?",
                        ('rolled_back:'+error_code, desired, instance))
                    db.execute('UPDATE model_deployments SET enabled=?,desired_state=?,updated_at=? WHERE id=?',
                               (int(previous['enabled']), desired, now(), instance))
                self.repository.rollback_model_deployment_revision_tx(db, instance, now())
                self._finish_configuration_rollback_tx(db, context, error_code, now())
            self.fault('policy.configuration_operation_aborted')
        return True

    def _finish_configuration_rollback_tx(self, db, context, error_code, stamp):
        old_digest = digest(context)
        context['phase'] = 'rolled_back'
        self.repository.store_configuration_context_tx(db, context, expected_digest=old_digest)
        operation = db.execute('SELECT state,error_class,error_code,error_message FROM deployment_operations WHERE id=?',
                               (context['operation_id'],)).fetchone()
        canceled = operation['state'] == 'canceling' or operation['error_class'] == 'canceled'
        terminal = 'canceled' if canceled else 'failed'
        db.execute("UPDATE deployment_operations SET state=?,milestone='configuration_rolled_back',"
            "error_class=?,error_code=?,error_message=?,recovery_cursor_json=?,updated_at=? WHERE id=?",
            (terminal, 'canceled' if canceled else operation['error_class'] or 'recoverable',
             'user_canceled' if canceled else operation['error_code'] or error_code,
             '用户取消配置，已恢复旧配置。' if canceled else operation['error_message'] or '配置未生效，已恢复旧配置。',
             canonical({'step': 'configuration_rolled_back'}), stamp, context['operation_id']))
        db.execute("UPDATE service_installations SET state=?,updated_at=? WHERE id=?",
                   (terminal, stamp, context['operation_id']))
        db.execute("UPDATE installation_attempts SET state=?,updated_at=? WHERE operation_id=?",
                   (terminal, stamp, context['operation_id']))

    def desire(self, instance, state, *, expected_version=None, validation=None):
        if state not in {"loaded", "unloaded"}:
            raise TaskStateError("invalid_desired_state", 400)
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if state == "loaded":
                self.repository.assert_not_removing_tx(db, instance)
            if ((expected_version is not None or validation is not None)
                    and self.repository.active_configuration_context_tx(db, instance) is not None):
                raise TaskStateError('deployment_configuration_pending')
            row = db.execute("SELECT * FROM instance_policies WHERE instance_id=?", (instance,)).fetchone()
            if validation is not None:
                from .runtime_provisioning import register_load_validation
                if register_load_validation(db, instance, state, row, validation):
                    return self._policy(row)
            if not row or row["policy_json"] == "null":
                raise TaskStateError("instance_policy_required")
            if state == "loaded" and not db.execute("SELECT 1 FROM model_deployments WHERE id=? AND enabled=1 AND install_state='ready'", (instance,)).fetchone():
                raise TaskStateError("deployment_not_ready")
            if expected_version is not None and expected_version != row["version"]:
                raise TaskStateError("instance_version_conflict")
            if row["desired_state"] != state:
                db.execute("UPDATE instance_policies SET desired_state=?,revision=revision+1,version=version+1,error_code=NULL WHERE instance_id=?", (state, instance))
            if validation is not None:
                db.execute('UPDATE runtime_validation_records SET policy_revision=(SELECT revision FROM instance_policies WHERE instance_id=?) WHERE validation_id=?',
                           (instance, validation['validation_id']))
            self.fault("policy.desire")
        return self.get(instance)

    def waiting(self, instance, code):
        token(code)
        with self.repository._connect() as db:
            db.execute("UPDATE instance_policies SET status='waiting_runtime',error_code=? WHERE instance_id=? AND status!='legacy_unreconciled' AND NOT EXISTS(SELECT 1 FROM instance_claims WHERE instance_id=instance_policies.instance_id AND state!='exited')", (code, instance))

    def container_materialized(self, instance, epoch):
        """Record a verified, never-started container as the stopped service state."""
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            claim = self.assert_claim(db, instance, epoch, backend="container")
            if claim["registered"] or claim["state"] not in {"claimed", "starting", "container_stopped"}:
                raise TaskStateError("container_materialization_state_invalid")
            intent = db.execute(
                "SELECT state FROM runtime_intents WHERE instance_id=? AND epoch=?",
                (instance, epoch),
            ).fetchone()
            if intent is None or intent["state"] != "created":
                raise TaskStateError("container_materialization_unconfirmed")
            # Reconciliation revisits stopped containers on every tick.  The
            # already-proven state is a read-only success: rewriting identical
            # timestamps would create fake deployment/instance changes and
            # grow the SQLite WAL for as long as the service remained stopped.
            if claim["state"] == "container_stopped":
                return self.get(instance)
            stamp = now()
            db.execute("UPDATE instance_claims SET state='container_stopped',updated_at=? WHERE claim_id=?",
                       (stamp, claim["claim_id"]))
            db.execute("UPDATE instance_policies SET status='unloaded',error_code=NULL,last_activity=? WHERE instance_id=?",
                       (stamp, instance))
            db.execute("UPDATE model_deployments SET actual_state='unloaded',runtime_last_error=NULL,runtime_updated_at=?,updated_at=? WHERE id=?",
                       (stamp, stamp, instance))
        return self.get(instance)

    def model_waiting(self, instance, code):
        """Persist a retryable model-admission error without stopping its container."""
        token(code)
        with self.repository._connect() as db:
            db.execute("""UPDATE instance_policies
                          SET status='unloaded',error_code=?
                          WHERE instance_id=? AND status!='legacy_unreconciled'
                          AND EXISTS(SELECT 1 FROM instance_claims
                                     WHERE instance_id=instance_policies.instance_id
                                     AND state='online_unloaded')""",
                       (code, instance))

    def idle_unload(self, instance):
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            raw = db.execute("SELECT * FROM instance_policies WHERE instance_id=?", (instance,)).fetchone()
            if not raw or raw["status"] != "loaded" or raw["desired_state"] != "loaded":
                return False
            row = self._policy(raw)
            if row["policy"]["residency"] == "resident" or not row["last_activity"]:
                return False
            if db.execute("SELECT 1 FROM runtime_validation_records WHERE instance_id=? AND kind='env_checked' AND state='pending'",
                          (instance,)).fetchone():
                return False
            if db.execute("SELECT 1 FROM tasks WHERE model_key=? AND status='queued'", (instance,)).fetchone() or db.execute("SELECT 1 FROM task_attempts WHERE instance_id=? AND exit_confirmed=0", (instance,)).fetchone():
                return False
            seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(row["last_activity"])).total_seconds()
            if row["policy"]["residency"] == "idle" and seconds < row["policy"]["idle_seconds"]:
                return False
            db.execute("UPDATE instance_policies SET desired_state='unloaded',revision=revision+1,version=version+1 WHERE instance_id=?", (instance,))
            return True

    def quarantine(self, instance, epoch, code):
        token(code)
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            claim = self.assert_claim(db, instance, epoch)
            db.execute("UPDATE instance_claims SET state='quarantined' WHERE claim_id=?", (claim["claim_id"],))
            db.execute("UPDATE task_instances SET active=0 WHERE instance_id=? AND epoch=?", (instance, epoch))
            db.execute("UPDATE instance_policies SET status='quarantined',error_code=? WHERE instance_id=?", (code, instance))

    @staticmethod
    def admit_capacity(db, instance, policy, observe_capacity, *, task=False):
        """Observe while holding the writer lock; reliable load precedes observation.

        A successful load is already reflected in fresh used memory, so its base
        is not deducted twice. Unmaterialized/unknown base and all outstanding
        task increments remain charged. The total ledger ceiling is separate.
        """
        if not callable(observe_capacity):
            raise TaskStateError("gpu_observer_required")
        observed = observe_capacity(policy, task=task)
        if type(observed) is not dict or set(observed) != set(policy["gpus"]):
            raise TaskStateError("invalid_gpu_capacity")
        limits = {}
        for gpu in policy["gpus"]:
            item = observed[gpu]
            if (type(item) is not dict or set(item) != {"limit", "free"} or
                    any(type(v) is not int or v < 0 for v in item.values())):
                raise TaskStateError("invalid_gpu_capacity")
            pending = 0
            for reservation in db.execute("SELECT r.*,c.policy_json,c.policy_digest,c.state AS claim_state FROM task_reservations r LEFT JOIN instance_claims c ON c.instance_id=r.instance_id AND c.epoch=r.epoch WHERE r.released=0 AND r.gpu_uuid=?", (gpu,)):
                if reservation["policy_json"] is None:
                    raise TaskStateError("legacy_execution_unreconciled")
                held = InstancePolicy._policy(reservation)["policy"]
                if reservation["instance_id"] != instance and (policy["sharing_mode"] == "exclusive" or held["sharing_mode"] == "exclusive"):
                    raise TaskStateError("gpu_exclusive_reservation")
                if reservation["kind"] == "task" or reservation["claim_state"] != "loaded":
                    pending += reservation["mib"]
            if pending + policy["task_mib" if task else "base_mib"] > item["free"]:
                raise TaskStateError("gpu_capacity_unavailable")
            limits[gpu] = item["limit"]
        return limits

    def claim_container(self, instance, epoch, *, expected_version, backend, limits, package_identity,
                        materialize_only=False):
        """Claim one container execution without claiming model GPU memory."""
        token(epoch)
        if type(package_identity) is not dict or not package_identity:
            raise TaskStateError("runtime_package_required")
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.repository.assert_not_removing_tx(db, instance)
            if self.legacy_unreconciled(db):
                raise TaskStateError("legacy_execution_unreconciled")
            raw = db.execute("SELECT * FROM instance_policies WHERE instance_id=?", (instance,)).fetchone()
            if raw is None:
                raise TaskStateError("instance_policy_required")
            row = self._policy(raw); policy = row["policy"]
            if row["version"] != expected_version or policy is None or policy["backend"] != backend:
                raise TaskStateError("instance_version_conflict")
            deployment = db.execute("SELECT * FROM model_deployments WHERE id=?", (instance,)).fetchone()
            if (not deployment or deployment["incarnation"] != row["incarnation"]
                    or deployment["install_state"] != "ready"
                    or not deployment["enabled"] and materialize_only is not True):
                raise TaskStateError("deployment_not_ready")
            if backend != "container":
                raise TaskStateError("legacy_model_runtime_retired")
            if policy["binding"] != self.deployment_binding(db, deployment) or package_identity.get("binding_digest") != digest(policy["binding"]) or package_identity.get("package_id") != policy["package_id"]:
                raise TaskStateError("runtime_package_binding_mismatch")
            if db.execute('SELECT 1 FROM instance_installation_bindings WHERE instance_id=?', (instance,)).fetchone():
                if backend != 'container' or self.package_validator is None:
                    raise TaskStateError('runtime_package_authority_required')
                approved = self.package_validator(db, package_identity.get('runtime_record_id'))
                if approved != package_identity or approved['epoch'] != epoch:
                    raise TaskStateError('runtime_package_binding_mismatch')
            if db.execute("SELECT 1 FROM instance_claims WHERE instance_id=? AND state!='exited'", (instance,)).fetchone():
                raise TaskStateError("backend_already_claimed")
            if set(limits) != set(policy["gpus"]) or any(type(v) is not int or v <= 0 for v in limits.values()):
                raise TaskStateError("invalid_gpu_capacity")
            if db.execute("SELECT 1 FROM task_attempts WHERE instance_id=? AND exit_confirmed=0", (instance,)).fetchone():
                raise TaskStateError("execution_exit_unconfirmed")
            db.execute("UPDATE task_instances SET active=0 WHERE instance_id=?", (instance,))
            db.execute("INSERT INTO task_instances VALUES(?,?,?,?,1)", (instance, epoch, canonical(policy["binding"]), canonical(limits)))
            claim = "clm_" + secrets.token_hex(16)
            stamp = now()
            db.execute("INSERT INTO instance_claims(claim_id,instance_id,epoch,backend,incarnation,revision,policy_json,policy_digest,execution_json,execution_digest,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,'claimed',?,?)",
                       (claim, instance, epoch, backend, row["incarnation"], row["revision"], row["policy_json"], row["policy_digest"], canonical(package_identity), digest(package_identity), stamp, stamp))
            self.fault("claim.identity")
            db.execute("UPDATE instance_policies SET status='waiting_runtime',error_code=NULL,last_activity=? WHERE instance_id=?",
                       (stamp, instance))
            self.fault("claim.command")
        return self.get(instance)["claim"]

    def load_model(self, instance, *, observe_capacity):
        """Atomically reserve GPU model memory and enqueue model.load."""
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.repository.assert_not_removing_tx(db, instance)
            raw = db.execute("SELECT * FROM instance_policies WHERE instance_id=?", (instance,)).fetchone()
            claim_raw = db.execute("SELECT * FROM instance_claims WHERE instance_id=? AND state!='exited'", (instance,)).fetchone()
            deployment = db.execute("SELECT * FROM model_deployments WHERE id=?", (instance,)).fetchone()
            if raw is None or claim_raw is None or deployment is None or not deployment["enabled"]:
                raise TaskStateError("backend_claim_required")
            row, claim = self._policy(raw), self._policy(claim_raw)
            if row["desired_state"] != "loaded" or not claim["registered"]:
                raise TaskStateError("instance_not_ready")
            if row["policy"]["residency"] not in residency_modes_for(
                    row["policy"]["binding"]["model_key"]):
                raise TaskStateError("residency_mode_unsupported")
            if claim["state"] == "loaded":
                return claim
            if claim["state"] not in {"online_unloaded", "starting"}:
                raise TaskStateError("model_operation_in_progress")
            active = db.execute("SELECT 1 FROM model_operations WHERE claim_id=? AND state IN ('pending','accepted')", (claim["claim_id"],)).fetchone()
            if active:
                return claim
            policy = row["policy"]
            limits = self.admit_capacity(db, instance, policy, observe_capacity)
            reservation, operation = (prefix + secrets.token_hex(16) for prefix in ("res_", "op_"))
            self.tasks._reserve(db, instance, claim["epoch"], reservation, row["revision"], {gpu: policy["base_mib"] for gpu in policy["gpus"]})
            payload = {key: policy["binding"][key] for key in ("model_key", "recipe_revision", "model_asset_id", "model_asset_revision")}
            payload.update(operation_id=operation, desired_revision=row["revision"],
                           reservation_id=reservation, reservation_generation=row["revision"],
                           residency=policy["residency"])
            message = self.tasks._envelope("model.load", instance, claim["epoch"], payload)
            self.tasks._outbox(db, message, None, None)
            stamp = now()
            db.execute("INSERT INTO model_operations(operation_id,claim_id,desired_revision,action,command_id,command_digest,state,created_at,updated_at) VALUES(?,?,?,'load',?,?,'pending',?,?)",
                       (operation, claim["claim_id"], row["revision"], message["message_id"], digest(message), stamp, stamp))
            # Only this transaction may attach a registered validation to a new
            # model load. Container startup itself never proves model readiness.
            db.execute("UPDATE runtime_validation_records SET operation_id=?,claim_id=?,epoch=?,command_digest=?,updated_at=? WHERE instance_id=? AND incarnation=? AND kind='env_checked' AND state='pending' AND operation_id IS NULL AND policy_revision=? AND expires_at>? AND binding_digest=(SELECT binding_digest FROM instance_installation_bindings WHERE instance_id=?)",
                       (operation, claim["claim_id"], claim["epoch"], digest(message), stamp,
                        instance, row['incarnation'], row['revision'], stamp, instance))
            db.execute("UPDATE instance_claims SET state='loading',updated_at=? WHERE claim_id=?", (stamp, claim["claim_id"]))
            db.execute("UPDATE instance_policies SET status='loading',error_code=NULL,last_activity=? WHERE instance_id=?", (stamp, instance))
        return self.get(instance)["claim"]

    def unload(self, instance):
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            policy = db.execute("SELECT * FROM instance_policies WHERE instance_id=?", (instance,)).fetchone()
            claim = db.execute("SELECT * FROM instance_claims WHERE instance_id=? AND state!='exited'", (instance,)).fetchone()
            if not claim:
                return None
            self.assert_claim(db, instance, claim["epoch"])
            if db.execute("SELECT 1 FROM task_attempts WHERE instance_id=? AND exit_confirmed=0", (instance,)).fetchone():
                raise TaskStateError("instance_execution_unconfirmed")
            if claim["state"] == "online_unloaded":
                return None
            old = db.execute("SELECT command_id FROM model_operations WHERE claim_id=? AND action='unload' AND state IN ('pending','accepted') ORDER BY desired_revision DESC LIMIT 1", (claim["claim_id"],)).fetchone()
            if old:
                return old[0]
            revision = max(policy["revision"], claim["revision"] + 1)
            operation = "op_" + secrets.token_hex(16)
            message = self.tasks._envelope("model.unload", instance, claim["epoch"], {"operation_id": operation, "desired_revision": revision})
            self.tasks._outbox(db, message, None, None)
            stamp = now()
            db.execute("INSERT INTO model_operations(operation_id,claim_id,desired_revision,action,command_id,command_digest,state,created_at,updated_at) VALUES(?,?,?,'unload',?,?,'pending',?,?)",
                       (operation, claim["claim_id"], revision, message["message_id"], digest(message), stamp, stamp))
            db.execute("UPDATE instance_policies SET revision=?,desired_state='unloaded',status='unloading',version=version+1 WHERE instance_id=?", (revision, instance))
            db.execute("UPDATE instance_claims SET state='unloading',updated_at=? WHERE claim_id=?", (stamp, claim["claim_id"]))
            self.fault("unload.command")
        return message["message_id"]

    def bind_execution(self, claim_id, identity):
        """Persist exact owned execution handle before allowing model loading."""
        if type(identity) is not dict or not identity:
            raise TaskStateError("execution_identity_required")
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM instance_claims WHERE claim_id=?", (claim_id,)).fetchone()
            if not row:
                raise TaskStateError("backend_claim_required")
            self.assert_claim(db, row["instance_id"], row["epoch"], claim_id=claim_id)
            original = json.loads(row["execution_json"])
            if "owned_execution" in original:
                if original["owned_execution"] != identity:
                    raise TaskStateError("execution_identity_conflict")
                return
            if row["state"] != "claimed":
                raise TaskStateError("execution_identity_conflict")
            original["owned_execution"] = identity
            db.execute("UPDATE instance_claims SET execution_json=?,execution_digest=?,state='starting',updated_at=? WHERE claim_id=?", (canonical(original), digest(original), now(), claim_id))

    def command(self, operation):
        with self.repository._connect() as db:
            row = db.execute("SELECT o.envelope_json,o.digest,m.command_digest FROM model_operations m JOIN task_outbox o ON o.message_id=m.command_id WHERE m.operation_id=?", (operation,)).fetchone()
            if not row:
                raise TaskStateError("model_operation_missing")
            message = validate_envelope(json.loads(row[0]), capabilities=self.tasks.capabilities)
            if digest(message) != row[1] or row[1] != row[2]:
                raise TaskStateError("model_command_integrity_error")
            return message

    def _event_binding(self, db, message):
        claim = db.execute("SELECT * FROM instance_claims WHERE instance_id=? AND epoch=?", (message["instance_id"], message["worker_epoch"])).fetchone()
        if not claim or message["server_id"] != self.tasks.server_id:
            raise TaskStateError("instance_event_identity_conflict")
        claim = self._policy(claim)
        if not claim["execution_json"] or digest(json.loads(claim["execution_json"])) != claim["execution_digest"]:
            raise TaskStateError("execution_identity_corrupt")
        payload = message["payload"]
        if message["type"] == "worker.registered":
            if message["event_seq"] != 1:
                raise TaskStateError("event_sequence_conflict")
            expected = json.loads(claim["execution_json"])
            binding = claim["policy"]["binding"]
            if (payload["model_key"] != binding["model_key"] or payload["recipe_revision"] != binding["recipe_revision"]
                    or payload["image_digest"] != expected.get("image_digest")
                    or payload["capability_digest"] != expected.get("capability_digest")
                    or sorted(payload["gpu_uuids"]) != sorted(claim["policy"]["gpus"])):
                raise TaskStateError("instance_registration_mismatch")
            return claim, "registration", None
        operation = db.execute("SELECT * FROM model_operations WHERE operation_id=? AND claim_id=?", (payload["operation_id"], claim["claim_id"])).fetchone()
        if not operation or operation["desired_revision"] != payload["desired_revision"]:
            raise TaskStateError("model_event_identity_conflict")
        if operation["state"] in {"succeeded", "failed", "canceled"}:
            previous = db.execute("SELECT digest,result FROM instance_inbox WHERE message_id=?", (message["message_id"],)).fetchone()
            if not previous or previous["digest"] != digest(message) or previous["result"] == "pending":
                raise TaskStateError("model_operation_terminal_conflict")
        raw = db.execute("SELECT * FROM task_outbox WHERE message_id=?", (operation["command_id"],)).fetchone()
        command = validate_envelope(json.loads(raw["envelope_json"]), capabilities=self.tasks.capabilities)
        if digest(command) != operation["command_digest"] or digest(command) != raw["digest"]:
            raise TaskStateError("model_command_integrity_error")
        if message["type"] in {"model.accepted", "model.terminal"}:
            if payload["action"] != operation["action"]:
                raise TaskStateError("model_event_identity_conflict")
            if operation["action"] == "load" and any(payload[key] != command["payload"][key] for key in ("reservation_id", "reservation_generation")):
                raise TaskStateError("reservation_identity_conflict")
        elif message["type"] == "worker.snapshot" and operation["action"] != "snapshot":
            raise TaskStateError("model_event_identity_conflict")
        return claim, operation["operation_id"], operation

    def _receipt(self, db, event):
        payload = {"event_message_id": event["message_id"], "event_seq": event["event_seq"]}
        if event["type"] == "worker.registered":
            payload["subject"] = "worker"
        else:
            payload.update(subject="operation", operation_id=event["payload"]["operation_id"], desired_revision=event["payload"]["desired_revision"])
        receipt = self.tasks._envelope("event.receipt", event["instance_id"], event["worker_epoch"], payload)
        receipt["message_id"] = self.tasks._receipt_id(event["message_id"])
        self.tasks._outbox(db, receipt, None, None)

    def receive(self, envelope):
        message = validate_envelope(envelope, capabilities=self.tasks.capabilities)
        if message["type"] not in {"worker.registered", "model.accepted", "model.terminal", "phase.changed", "worker.snapshot"} or "task_id" in message:
            raise TaskStateError("unsupported_instance_event")
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM instance_inbox WHERE message_id=?", (message["message_id"],)).fetchone()
            if previous:
                if (previous["digest"] != digest(message)
                        or digest(json.loads(previous["envelope_json"])) != previous["digest"]):
                    raise TaskStateError("event_identity_conflict")
                if previous["result"] != "pending":
                    # The exact event was applied atomically with its receipt.
                    # Deduplication must not re-run old command validation or
                    # lifecycle effects after a protocol/runtime upgrade.
                    self._receipt_for_event_tx(db, message)
                    return previous["result"]
            claim, scope, operation = self._event_binding(db, message)
            if previous:
                if (previous["claim_id"] != claim["claim_id"] or previous["scope_id"] != scope
                        or previous["event_seq"] != message["event_seq"]):
                    raise TaskStateError("instance_inbox_integrity_error")
            else:
                if db.execute("SELECT 1 FROM instance_inbox WHERE claim_id=? AND scope_id=? AND event_seq=?", (claim["claim_id"], scope, message["event_seq"])).fetchone():
                    raise TaskStateError("event_sequence_conflict")
                db.execute("INSERT INTO instance_inbox VALUES(?,?,?,?,?,?,'pending',?)", (message["message_id"], claim["claim_id"], scope, message["event_seq"], canonical(message), digest(message), now()))
                self.fault("instance.event_inbox")
            try:
                self._drain(db, claim["claim_id"], scope)
            except (TaskStateError, ValueError, KeyError, TypeError):
                # A previously durable poison row is an internal failure, never
                # a reason to quarantine/ACK today's otherwise valid delivery.
                raise TaskStateError("instance_inbox_integrity_error") from None
            return db.execute("SELECT result FROM instance_inbox WHERE message_id=?", (message["message_id"],)).fetchone()[0]

    def _drain(self, db, claim_id, scope):
        while True:
            op = db.execute("SELECT * FROM model_operations WHERE operation_id=?", (scope,)).fetchone()
            sequence = op["next_event_seq"] if op else 1
            row = db.execute("SELECT * FROM instance_inbox WHERE claim_id=? AND scope_id=? AND event_seq=? AND result='pending'", (claim_id, scope, sequence)).fetchone()
            if not row:
                return
            event = validate_envelope(json.loads(row["envelope_json"]), capabilities=self.tasks.capabilities)
            if digest(event) != row["digest"] or event["message_id"] != row["message_id"] or event["event_seq"] != row["event_seq"]:
                raise TaskStateError("instance_event_corrupt")
            claim, actual_scope, op = self._event_binding(db, event)
            if claim["claim_id"] != claim_id or actual_scope != scope:
                raise TaskStateError("instance_event_corrupt")
            current = db.execute("SELECT * FROM instance_policies WHERE instance_id=?", (claim["instance_id"],)).fetchone()
            lifecycle_current = claim["state"] != "exited" and not claim["stop_reason"] and current["incarnation"] == claim["incarnation"]
            stale = not lifecycle_current
            if op and op["desired_revision"] < current["revision"]:
                stale = True
            kind = event["type"]
            if kind == "worker.registered" and not stale:
                pending_load = db.execute("SELECT 1 FROM model_operations WHERE claim_id=? AND action='load' AND state IN ('pending','accepted')", (claim_id,)).fetchone()
                db.execute("UPDATE instance_claims SET registered=1,state=?,updated_at=? WHERE claim_id=?",
                           (claim["state"] if pending_load else "online_unloaded", now(), claim_id))
                if not pending_load:
                    current_policy = self._policy(current)["policy"]
                    unsupported = (current["desired_state"] == "loaded" and
                        current_policy["residency"] not in residency_modes_for(
                            current_policy["binding"]["model_key"]))
                    db.execute("UPDATE instance_policies SET status='unloaded',error_code=? WHERE instance_id=?",
                               ("residency_mode_unsupported" if unsupported else None,
                                claim["instance_id"]))
            elif op:
                payload = event["payload"]
                if kind == "model.terminal":
                    db.execute("UPDATE model_operations SET state=?,updated_at=? WHERE operation_id=?", (payload["status"], now(), scope))
                    # A terminal load/unload describes physical model state even
                    # when a newer desired revision has superseded the command.
                    # Reconciliation can then issue the compensating operation.
                    if lifecycle_current:
                        state = ("loaded" if op["action"] == "load" else "online_unloaded") if payload["status"] == "succeeded" else "quarantined"
                        db.execute("UPDATE instance_claims SET state=?,updated_at=? WHERE claim_id=?", (state, now(), claim_id))
                        policy_state = "unloaded" if state == "online_unloaded" else state
                        db.execute("UPDATE instance_policies SET status=?,error_code=?,last_activity=? WHERE instance_id=?", (policy_state, payload["error_code"], now(), claim["instance_id"]))
                        if op["action"] == "unload" and payload["status"] == "succeeded":
                            db.execute("UPDATE task_reservations SET released=1 WHERE instance_id=? AND epoch=? AND kind='base'", (claim["instance_id"], claim["epoch"]))
                elif kind == "model.accepted":
                    db.execute("UPDATE model_operations SET state='accepted',updated_at=? WHERE operation_id=? AND state='pending'", (now(), scope))
                db.execute("UPDATE model_operations SET next_event_seq=next_event_seq+1 WHERE operation_id=?", (scope,))
            self._receipt(db, event)
            db.execute("UPDATE instance_inbox SET result=? WHERE message_id=?", ("stale" if stale else "applied", event["message_id"]))
            self.fault("instance.event_applied")
            if op is None:
                return

    def receipt_for_event(self, event):
        with self.repository._connect() as db:
            return self._receipt_for_event_tx(db, event)

    def _receipt_for_event_tx(self, db, event):
        row = db.execute("SELECT * FROM instance_inbox WHERE message_id=?", (event["message_id"],)).fetchone()
        if (not row or row["digest"] != digest(event)
                or digest(json.loads(row["envelope_json"])) != row["digest"]):
            raise TaskStateError("instance_event_identity_conflict")
        claim = db.execute("SELECT instance_id,epoch FROM instance_claims WHERE claim_id=?", (row["claim_id"],)).fetchone()
        scope = 'registration' if event['type'] == 'worker.registered' else event['payload']['operation_id']
        if (not claim or event['server_id'] != self.tasks.server_id
                or event['instance_id'] != claim['instance_id'] or event['worker_epoch'] != claim['epoch']
                or row['event_seq'] != event['event_seq'] or row['scope_id'] != scope):
            raise TaskStateError("instance_event_identity_conflict")
        if row["result"] == "pending":
            return None
        receipt = db.execute("SELECT * FROM task_outbox WHERE message_id=?", (self.tasks._receipt_id(event["message_id"]),)).fetchone()
        if not receipt:
            raise TaskStateError("receipt_integrity_error")
        message = json.loads(receipt["envelope_json"])
        payload = {'event_message_id': event['message_id'], 'event_seq': event['event_seq']}
        if event['type'] == 'worker.registered':
            payload['subject'] = 'worker'
        else:
            payload.update(subject='operation', operation_id=scope,
                           desired_revision=event['payload']['desired_revision'])
        if (digest(message) != receipt['digest'] or message.get('type') != 'event.receipt'
                or message.get('message_id') != receipt['message_id'] or message.get('payload') != payload
                or any(message.get(key) != event[key] for key in ('server_id','instance_id','worker_epoch'))):
            raise TaskStateError("receipt_integrity_error")
        return validate_envelope(message)

    def confirm_container_exit(self, claim_id, controller):
        """Read the owned Controller record, never accept a caller's exited flag."""
        with self.repository._connect() as db:
            raw = db.execute("SELECT * FROM instance_claims WHERE claim_id=?", (claim_id,)).fetchone()
            if raw is None:
                raise TaskStateError("backend_claim_required")
            original = self._policy(raw)
            execution = json.loads(original["execution_json"])["owned_execution"]
        record = controller.get(execution["intent_id"])
        if (record["state"] != "exited" or not record["exit_evidence_json"] or
                record["instance_id"] != original["instance_id"] or record["epoch"] != original["epoch"]):
            raise TaskStateError("execution_exit_unconfirmed")
        evidence = json.loads(record["exit_evidence_json"])
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM instance_claims WHERE claim_id=?", (claim_id,)).fetchone()
            if current["state"] == "exited":
                if current["exit_digest"] != digest(evidence):
                    raise TaskStateError("exit_evidence_conflict")
                return
            self.assert_claim(db, original["instance_id"], original["epoch"], backend="container", claim_id=claim_id)
            if current["execution_digest"] != original["execution_digest"]:
                raise TaskStateError("execution_identity_conflict")
            durable = db.execute("SELECT * FROM runtime_intents WHERE intent_id=?", (record["intent_id"],)).fetchone()
            if not durable or durable["version"] != record["version"] or durable["exit_evidence_digest"] != digest(evidence):
                raise TaskStateError("execution_exit_unconfirmed")
            no_base = not db.execute(
                "SELECT 1 FROM task_reservations WHERE instance_id=? AND epoch=? AND kind='base' AND released=0",
                (current["instance_id"], current["epoch"])).fetchone()
            never_loaded = (current["state"] in {"claimed", "starting", "online_unloaded"}
                            and no_base and not db.execute(
                "SELECT 1 FROM model_operations WHERE claim_id=? AND action='load'",
                (claim_id,)).fetchone())
            unloaded = (current["state"] == "online_unloaded" and no_base) or never_loaded or db.execute(
                "SELECT 1 FROM model_operations WHERE claim_id=? AND action='unload' AND state='succeeded'", (claim_id,)).fetchone()
            if evidence.get("kind") == "never_started":
                if db.execute("SELECT 1 FROM runtime_effects WHERE intent_id=? AND action='start'", (record["intent_id"],)).fetchone():
                    raise TaskStateError("execution_exit_unconfirmed")
                if db.execute("SELECT desired_state FROM instance_policies WHERE instance_id=?", (current["instance_id"],)).fetchone()[0] != "unloaded":
                    raise TaskStateError("unload_confirmation_required")
                db.execute("UPDATE model_operations SET state='canceled' WHERE claim_id=? AND action='load' AND state IN ('pending','accepted')", (claim_id,))
                db.execute("UPDATE model_operations SET state='succeeded' WHERE claim_id=? AND action='unload'", (claim_id,))
                unloaded = True
            forced = db.execute("SELECT 1 FROM instance_cancel_deadlines WHERE claim_id=? AND state='stop_requested'", (claim_id,)).fetchone()
            if not unloaded and not forced and not current["stop_reason"]:
                raise TaskStateError("unload_confirmation_required")
            if db.execute("SELECT 1 FROM task_attempts WHERE instance_id=? AND epoch=? AND exit_confirmed=0", (current["instance_id"], current["epoch"])).fetchone():
                expected = "journal-" + digest([claim_id, record["exit_evidence_digest"]])
                if current["recovery_evidence"] != expected:
                    raise TaskStateError("journal_recovery_required_before_release")
            self._close_claim(db, current, evidence)

    def observe_heartbeat(self, instance, epoch, message, *, at=None, timeout_seconds=600):
        """Fence sustained liveness loss; task deadlines and runtime observation remain primary.

        AI loaders can starve the supervisor process for several minutes while
        saturating the container CPU quota.  A short heartbeat deadline would
        kill a healthy owned execution even though the Controller still
        observes its exact container.  This secondary fence therefore allows
        bounded scheduler jitter, while task/cancellation deadlines remain the
        authoritative limits for active work.
        """
        from datetime import datetime, timezone
        stamp = at or now()
        current_time = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        event = validate_envelope(message) if message is not None else None
        if event and (event["type"] != "telemetry.heartbeat" or event["server_id"] != self.tasks.server_id or
                      event["instance_id"] != instance or event["worker_epoch"] != epoch):
            raise TaskStateError("heartbeat_identity_conflict")
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            claim = self.assert_claim(db, instance, epoch, backend="container")
            key = (instance, epoch)
            with self._heartbeat_lock:
                cached = self._heartbeat_observations.get(key)
            observed_sequence = max(
                claim["heartbeat_seq"], cached[0] if cached is not None else 0
            )
            if event and event["telemetry_seq"] > observed_sequence:
                age = (current_time - datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))).total_seconds()
                if 0 <= age <= timeout_seconds:
                    with self._heartbeat_lock:
                        first_after_start = key not in self._heartbeat_observations
                        self._heartbeat_observations[key] = (
                            event["telemetry_seq"], stamp
                        )
                    if first_after_start or event["payload"]["state"] == "error":
                        db.execute(
                            "UPDATE instance_claims SET heartbeat_seq=?,heartbeat_at=? WHERE claim_id=?",
                            (event["telemetry_seq"], stamp, claim["claim_id"]),
                        )
                    if event["payload"]["state"] == "error":
                        self._fence_stop(db, claim, "worker_error")
                        return False
                    return not bool(claim["stop_reason"])
            prior = cached[1] if cached is not None else (
                claim["heartbeat_at"] or claim["updated_at"]
            )
            age = (current_time - datetime.fromisoformat(prior.replace("Z", "+00:00"))).total_seconds()
            if age < 0 or age > timeout_seconds:
                self._fence_stop(db, claim, "worker_heartbeat_expired")
                return False
            return not bool(claim["stop_reason"])

    def _fence_stop(self, db, claim, reason):
        db.execute("UPDATE instance_claims SET state='quarantined',stop_reason=COALESCE(stop_reason,?) WHERE claim_id=?", (reason, claim["claim_id"]))
        # An observed process exit fences the old execution, not the user's
        # resident intent. Only after its precise exit and journal recovery can
        # the unchanged policy obtain a fresh generation. Cleanup/health errors
        # still require an explicit reload instead of an automatic error loop.
        db.execute("UPDATE instance_policies SET status='quarantined',error_code=?,desired_state=CASE WHEN ?='runtime_exited' AND restart_recovery=1 THEN desired_state ELSE 'unloaded' END WHERE instance_id=?", (reason, reason, claim["instance_id"]))
        # A failed *candidate* is not a normal service restart. Persist rollback
        # before exit can release its claim; otherwise the next tick would
        # launch that same unhealthy candidate forever. Binding restoration and
        # DOP termination still wait for the Controller's confirmed exit.
        policy = db.execute('SELECT configuration_state FROM instance_policies WHERE instance_id=?',
                            (claim['instance_id'],)).fetchone()
        if policy['configuration_state'] == 'applying':
            self._begin_configuration_rollback_tx(db, claim['instance_id'], reason)

    def require_stop(self, instance, epoch, reason, *, only_if_unfenced=False):
        if reason not in {"runtime_exited", "runtime_observation_unknown", "worker_heartbeat_expired", "operator_stop"}:
            raise TaskStateError("stop_reason_invalid")
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            claim = self.assert_claim(db, instance, epoch, backend="container")
            if only_if_unfenced and claim["stop_reason"]:
                return
            self._fence_stop(db, claim, reason)

    def stop(self, instance, *, expected_version):
        """Fence only the current managed container claim; never target an external process."""
        if type(expected_version) is not int or expected_version < 1:
            raise TaskStateError("instance_version_conflict")
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            policy = db.execute("SELECT * FROM instance_policies WHERE instance_id=?", (instance,)).fetchone()
            if policy is None:
                raise TaskStateError("instance_policy_required")
            if policy["version"] != expected_version:
                raise TaskStateError("instance_version_conflict")
            claim = db.execute("SELECT * FROM instance_claims WHERE instance_id=? AND state!='exited'", (instance,)).fetchone()
            if claim is None:
                if policy["desired_state"] != "unloaded":
                    db.execute("UPDATE instance_policies SET desired_state='unloaded',revision=revision+1,version=version+1 WHERE instance_id=?", (instance,))
            else:
                claim = self.assert_claim(db, instance, claim["epoch"], backend="container")
                db.execute("UPDATE instance_policies SET desired_state='unloaded',revision=revision+1,version=version+1 WHERE instance_id=?", (instance,))
                execution = json.loads(claim["execution_json"])
                intent = db.execute("SELECT 1 FROM runtime_intents WHERE instance_id=? AND epoch=?", (instance, claim["epoch"])).fetchone()
                if claim["state"] == "claimed" and "owned_execution" not in execution and not intent:
                    db.execute("UPDATE model_operations SET state='canceled' WHERE claim_id=? AND state IN ('pending','accepted')", (claim["claim_id"],))
                    self._close_claim(db, claim, {"kind": "operator_stop_before_start", "claim_id": claim["claim_id"], "epoch": claim["epoch"]})
                else:
                    self._fence_stop(db, claim, "operator_stop")
        return self.get(instance)

    def close_unstarted_claim(self, instance):
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM instance_claims WHERE instance_id=? AND state!='exited'", (instance,)).fetchone()
            if not row:
                return True
            current = db.execute("SELECT p.desired_state,d.enabled FROM instance_policies p JOIN model_deployments d ON d.id=p.instance_id WHERE p.instance_id=?", (instance,)).fetchone()
            execution = json.loads(row["execution_json"])
            if current["enabled"] or row["state"] != "claimed" or "owned_execution" in execution:
                return False
            if db.execute("SELECT 1 FROM runtime_intents WHERE instance_id=? AND epoch=?", (instance, row["epoch"])).fetchone():
                return False
            db.execute("UPDATE model_operations SET state='canceled' WHERE claim_id=? AND state IN ('pending','accepted')", (row["claim_id"],))
            self._close_claim(db, row, {"kind": "claim_never_started", "claim_id": row["claim_id"], "epoch": row["epoch"]})
            return True

    def _close_claim(self, db, claim, evidence):
        # Domain exit is stronger than per-task quiescence. Only this path may
        # retire unknown attempts and release base after an explicit stop fence.
        for attempt in db.execute("SELECT * FROM task_attempts WHERE instance_id=? AND epoch=? AND exit_confirmed=0", (claim["instance_id"], claim["epoch"])).fetchall():
            task = self.tasks._task(db, attempt["task_id"])
            if attempt["status"] not in {"succeeded", "failed", "canceled", "interrupted"}:
                if self.tasks._expire_tx(db, task, attempt):
                    task = self.tasks._task(db, attempt['task_id'])
                    attempt = db.execute('SELECT * FROM task_attempts WHERE id=?', (attempt['id'],)).fetchone()
                self.tasks._terminal(db, task, attempt, "canceled" if task["cancel_requested"] else "interrupted", error="owned_execution_domain_exited")
            self.tasks._record_exit_confirmation(db, task['id'], attempt['id'], "domain-" + digest(evidence))
        db.execute("UPDATE task_reservations SET released=1 WHERE instance_id=? AND epoch=?", (claim["instance_id"], claim["epoch"]))
        db.execute("UPDATE task_instances SET active=0 WHERE instance_id=? AND epoch=?", (claim["instance_id"], claim["epoch"]))
        db.execute("UPDATE instance_claims SET state='exited',exit_json=?,exit_digest=?,updated_at=? WHERE claim_id=?", (canonical(evidence), digest(evidence), now(), claim["claim_id"]))
        db.execute("UPDATE instance_cancel_deadlines SET state='completed',updated_at=? WHERE claim_id=?", (now(), claim["claim_id"]))
        db.execute("UPDATE instance_policies SET status='unloaded',last_activity=?,error_code=NULL WHERE instance_id=?", (now(), claim["instance_id"]))
        with self._heartbeat_lock:
            self._heartbeat_observations.pop(
                (claim["instance_id"], claim["epoch"]), None
            )
        self.fault("instance.exit_release")

    def due_cancellations(self, at=None):
        at = at or now()
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT d.*,c.instance_id,c.epoch FROM instance_cancel_deadlines d JOIN instance_claims c ON c.claim_id=d.claim_id WHERE d.state IN ('waiting','stop_requested') AND d.due_at<=? ORDER BY d.due_at,d.attempt_id LIMIT 100", (at,)).fetchall()
            for row in rows:
                db.execute("UPDATE instance_cancel_deadlines SET state='stop_requested',updated_at=? WHERE attempt_id=?", (at, row["attempt_id"]))
                db.execute("UPDATE instance_claims SET state='quarantined' WHERE claim_id=? AND state!='exited'", (row["claim_id"],))
                db.execute("UPDATE instance_policies SET desired_state='unloaded',status='cancel_timeout' WHERE instance_id=?", (row["instance_id"],))
            return [dict(row) for row in rows]
