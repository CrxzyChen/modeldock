"""Server-owned reconciliation. No model execution or inferred process ownership."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone

from .container_runtime import ContainerError
from .gpu_scheduler import GPUBusyError
from .instance_policy import InstancePolicy
from .task_state import TaskStateError, digest
from .transport import DeliveryPump, DurableResult, OutboxRelay, TaskEventHandler, TransportError
from .worker_journal import WorkerJournal


class JournalSnapshot(WorkerJournal):
    """Read-only recovery adapter; never invokes the journal's write constructor."""
    def __init__(self, connection, instance_id, capabilities):
        self.connection = connection
        self.instance_id, self.capabilities = instance_id, capabilities
        connection.row_factory = sqlite3.Row
        if [tuple(row) for row in connection.execute("SELECT version,instance_id FROM journal_meta")] != [(1, instance_id)]:
            raise TaskStateError("recovery_journal_identity_mismatch")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise TaskStateError("recovery_journal_integrity_error")
    @contextmanager
    def _connect(self):
        yield self.connection


class InstanceEventHandler:
    def __init__(self, authority):
        self.authority = authority
        self.tasks = TaskEventHandler(authority.tasks)

    def __call__(self, event):
        if "task_id" in event:
            return self.tasks(event)
        try:
            result = self.authority.receive(event)
            receipt = self.authority.receipt_for_event(event)
        except TaskStateError as exc:
            # Only current, pre-persistence identity failures are input errors.
            # Stored-inbox/receipt integrity faults must retain the delivery PEL.
            if exc.code in {"instance_event_identity_conflict", "model_event_identity_conflict",
                            "instance_registration_mismatch", "model_operation_terminal_conflict",
                            "unsupported_instance_event"}:
                raise TaskStateError("event_identity_conflict") from None
            raise
        return DurableResult(result, (receipt,) if receipt else ())


class Reconciler:
    def __init__(self, repository, registry, scheduler, *,
                 poll_seconds=0.5, artifact_root=None, model_assets=None, package_provider=None):
        self.repository, self.registry, self.scheduler = repository, registry, scheduler
        self.authority = scheduler.authority
        self.packages = {}
        self.package_provider = package_provider
        if package_provider is not None:
            self.authority.package_validator = package_provider.validate_claim
        self.model_assets = model_assets
        self.poll_seconds = poll_seconds
        self._transports, self._controllers, self._cursors = {}, {}, {}
        self._cursor_replay_at = {}
        self._validated_package_assets = set()
        self._cache_lock = threading.Lock()
        self._stop = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._threads = []
        self.last_errors = {}
        self.artifact_errors = {}
        self._artifact_cursor = ""
        self._validation_cursor = ""
        self.artifacts = None
        if artifact_root is not None:
            from .artifacts import ArtifactStore
            self.artifacts = ArtifactStore(self.authority.tasks, artifact_root)

    def seal_pending(self, *, instance=None, epoch=None):
        """Discover durable ACKed events even if publication creation never ran.

        Bounded keyset pages avoid a permanently missing file starving the tail.
        Recovery scopes explicitly scan their own original epoch from the start.
        """
        if self.artifacts is None: return
        cursor = self._artifact_cursor if instance is None else ""
        with self.repository._connect() as db:
            rows = db.execute("SELECT i.* FROM task_inbox i JOIN task_attempts a ON a.id=i.attempt_id WHERE i.result='pending' AND i.message_id>? AND (? IS NULL OR (a.instance_id=? AND a.epoch=?)) ORDER BY i.message_id LIMIT 100",
                (cursor,instance,instance,epoch)).fetchall()
        for item in rows:
            event = json.loads(item["envelope_json"])
            if event.get("type") != "task.terminal" or event["payload"]["status"] != "succeeded": continue
            try:
                if digest(event) != item["digest"]: raise TaskStateError("inbox_integrity_error")
                if self.package_provider is None: raise TaskStateError("artifact_package_missing")
                package = self.package_provider.for_epoch(event['instance_id'], event['worker_epoch'])
                self.packages[package.record_id] = package
                self.artifacts.worker(event, package.outputs_path())
                self.artifact_errors.pop(item["message_id"],None)
            except Exception as exc:
                self.artifact_errors[item["message_id"]] = getattr(exc,"code",type(exc).__name__)
        if instance is None:
            self._artifact_cursor = rows[-1]["message_id"] if len(rows)==100 else ""

    def start(self):
        with self._lifecycle_lock:
            if self._threads:
                return
            self._stop.clear()
            loops = [(self.tick, "runtime-reconcile"), (self.cancel_tick, "runtime-cancel-deadlines")]
            if self.artifacts is not None:
                loops.append((self.cleanup_tick, "runtime-artifact-recovery"))
            try:
                for target, name in loops:
                    thread = threading.Thread(target=self._loop, args=(target,), name=name, daemon=True)
                    thread.start()
                    self._threads.append(thread)
            except BaseException:
                # Only successfully started threads need joining. Keep them
                # owned for stop(), but prevent additional callback admission.
                self._stop.set()
                raise

    def _loop(self, callback):
        while not self._stop.is_set():
            try:
                callback()
                self.last_errors.pop(callback.__name__, None)
            except Exception as exc:
                self.last_errors[callback.__name__] = type(exc).__name__
            self._stop.wait(self.poll_seconds)

    def stop(self, timeout=5.0):
        with self._lifecycle_lock:
            self._stop.set()
            deadline = time.monotonic() + max(0.0, timeout)
            for thread in self._threads:
                thread.join(timeout=max(0.0, deadline-time.monotonic()))
            if any(thread.is_alive() for thread in self._threads):
                # An in-flight copy/unlink is not preempted. Retain ownership
                # and dependencies until a later stop confirms actual exit.
                raise RuntimeError("reconciler_exit_unconfirmed")
            self._threads.clear()
            for transport in self._transports.values():
                transport.close()
            publisher = getattr(self.package_provider, 'publisher', None)
            if publisher is not None:
                publisher.close()
            # Stopping the Server does not unload models or release reservations.

    def cleanup_tick(self):
        """Single lifecycle-owned recovery loop, never a scheduling callback.

        The publication ledger is its queue. Small pages and between-item stop
        checks bound shutdown admission; all existing file/authority checks
        and publication locks remain mandatory. Ordinary foreground sealing
        retains its existing contract and is not claimed to be asynchronous.
        """
        if self.artifacts is None or self._stop.is_set():
            return
        errors = self.artifacts.recover_local(limit=8, stop_requested=self._stop.is_set)
        errors.update(self.artifacts.recover_cleanup(limit=8, stop_requested=self._stop.is_set))
        if errors:
            self.last_errors['publication_cleanup'] = ','.join(sorted(set(errors.values())))
        else:
            self.last_errors.pop('publication_cleanup', None)

    def _package(self, row):
        if self.package_provider is None:
            raise TaskStateError("runtime_package_unavailable")
        claim = row.get('claim')
        # A package is immutable for one recorded epoch.  Rebuilding it on every
        # 500 ms reconciliation pass repeated the full historical-boundary scan
        # even though PreparedRuntime.verify() already rechecks those boundaries.
        # Reuse only the package named by the durable claim identity; a changed
        # record or a new epoch necessarily misses this cache.
        record_id = None
        if claim and claim.get("execution_json"):
            execution = json.loads(claim["execution_json"])
            if isinstance(execution, dict):
                record_id = execution.get("runtime_record_id")
        package = self.packages.get(record_id) if record_id else None
        if package is None:
            package = (self.package_provider.for_epoch(row['instance_id'], claim['epoch']) if claim
                       else self.package_provider.ensure(row['instance_id']))
        self.packages[package.record_id] = package
        identity = package.verify()
        if package.instance_id != row["instance_id"] or package.binding != row["policy"]["binding"]:
            raise TaskStateError("runtime_package_binding_mismatch")
        claim = row.get("claim")
        if claim and (package.epoch != claim["epoch"] or identity["binding_digest"] != digest(claim["policy"]["binding"])):
            raise TaskStateError("runtime_package_epoch_changed")
        # A model file manifest belongs to an immutable epoch package. Validate
        # every file once when this server process first sees that record. The
        # package/mount root identities continue to be rechecked every tick;
        # rescanning all weight files every 500 ms adds disk metadata pressure
        # without protecting already loaded model memory.
        assets_key = package.record_id
        if (self.model_assets is not None and hasattr(package,'policy')
                and assets_key not in self._validated_package_assets):
            models=next((grant for grant in package.policy.mounts if grant.role=='models'),None)
            references=[(package.binding['model_asset_id'],package.binding['model_asset_revision'])]
            references.extend((dep['asset_id'],dep['revision']) for dep in package.binding.get('dependencies',[]))
            for asset_id,revision in references:
                source=self.model_assets.readonly_asset_path(asset_id,revision)
                if models is None or not (Path(models.source)==source or Path(models.source) in source.parents):
                    raise TaskStateError('model_asset_not_in_readonly_mount')
            self._validated_package_assets.add(assets_key)
        return package, identity

    def _transport(self, package):
        key = (package.instance_id, package.epoch)
        with self._cache_lock:
            transport = self._transports.get(key)
            if transport is None:
                transport = package.transport(self.authority.tasks.server_id)
                try:
                    transport.provision()
                except Exception:
                    transport.close()
                    raise
                self._transports[key] = transport
            return transport

    def _controller(self, package):
        key = (package.instance_id, package.epoch)
        with self._cache_lock:
            if key not in self._controllers:
                self._controllers[key] = package.controller(self.repository)
            return self._controllers[key]

    def request_load(self, instance):
        self.authority.desire(instance, "loaded")
        # Asynchronous reconciliation; HTTP never claims a model is loaded.

    def request_unload(self, instance):
        self.authority.desire(instance, "unloaded")

    def retire(self, instance):
        row = self.authority.get(instance)
        if row and row["claim"]:
            raise TaskStateError("unload_and_confirm_exit_before_configuration")

    def status(self, instance):
        row = self.authority.get(instance)
        if row is None:
            return {"actual_state": "waiting_runtime", "runtime_last_error": "instance_policy_required"}
        return {"actual_state": row["status"], "desired_state": row["desired_state"],
                "runtime_last_error": row["error_code"], "policy_version": row["version"],
                "policy": row["policy"], "backend": row["claim"]["backend"] if row["claim"] else None}

    def _configuration_failed(self, instance, error):
        """Persist rollback intent only for the candidate currently applying."""
        current = self.authority.get(instance)
        if current and current.get("configuration_state") == "applying":
            code = (str(error) if isinstance(error, GPUBusyError)
                    else getattr(error, "code", None)) or "runtime_unavailable"
            self.authority.begin_configuration_rollback(instance, code)
        elif current and current.get('configuration_state') in {'restart_pending', 'replace_pending'}:
            with self.repository._connect() as db:
                context = self.repository.active_configuration_context_tx(db, instance)
            if context is not None and context['phase'] == 'draining':
                code = getattr(error, 'code', None) or 'configuration_binding_unavailable'
                self.authority.abort_configuration_operation(context['operation_id'], code)

    def _reconcile_configuration(self, instance):
        current = self.authority.get(instance)
        changed = (self.authority.rollback_configuration(instance)
                   if current['configuration_state'] == 'failed'
                   else self.authority.apply_pending(instance))
        deployment = self.repository.get_deployment(instance)
        current = self.authority.get(instance)
        if (deployment and not deployment['enabled'] and
                (changed or current['configuration_state'] == 'applying')):
            # Replay persisted state, including a crash after the switch
            # transaction but before the stopped candidate was materialized.
            self.materialize_stopped_instance(instance)

    def tick(self):
        # Epoch retirement must also retire its Pub/Sub socket and bounded
        # cache. Never retain one background subscriber per historical epoch.
        with self._cache_lock:
            with self.repository._connect() as db:
                live_epochs = {(row[0], row[1]) for row in db.execute(
                    "SELECT instance_id,epoch FROM instance_claims WHERE state!='exited'")}
            for key in list(self._transports):
                if key not in live_epochs and self._transports[key].close(timeout=0):
                    self._transports.pop(key)
        # Deferred structural settings never block an HTTP request waiting for
        # a model container. They become current only after the old execution,
        # tasks and durable runtime intent have all reached their exit boundary.
        with self.repository._connect() as db:
            pending_configuration = [row[0] for row in db.execute(
                "SELECT instance_id FROM instance_policies "
                "WHERE configuration_state IN ('restart_pending','replace_pending','applying','failed') "
                "ORDER BY instance_id LIMIT 128")]
        for instance in pending_configuration:
            try:
                self._reconcile_configuration(instance)
            except Exception as exc:
                self._configuration_failed(instance, exc)
                self.last_errors[instance] = getattr(exc, 'code', type(exc).__name__)
        if self.package_provider is not None:
            with self.repository._connect() as db:
                pending = [r[0] for r in db.execute(
                    "SELECT b.instance_id FROM instance_installation_bindings b "
                    "LEFT JOIN instance_policies p USING(instance_id) "
                    "WHERE p.instance_id IS NULL OR p.policy_json='null' "
                    "OR json_extract(p.policy_json,'$.package_id')!='template_'||b.template_digest "
                    "ORDER BY b.instance_id LIMIT 128")]
            for instance in pending:
                try: self.install_instance(instance)
                except Exception as exc: self.last_errors[instance] = getattr(exc, 'code', type(exc).__name__)
            with self.repository._connect() as db:
                stopped_missing = [r[0] for r in db.execute(
                    """SELECT p.instance_id FROM instance_policies p
                       JOIN model_deployments d ON d.id=p.instance_id
                       JOIN instance_installation_bindings b ON b.instance_id=p.instance_id
                       WHERE d.enabled=0 AND d.install_state='ready'
                         AND d.removal_operation_id IS NULL
                         AND p.configuration_state='applied'
                         AND NOT EXISTS(SELECT 1 FROM instance_claims c
                                        WHERE c.instance_id=p.instance_id AND c.state!='exited')
                       ORDER BY p.instance_id LIMIT 128""")]
            for instance in stopped_missing:
                try: self.materialize_stopped_instance(instance)
                except Exception as exc: self.last_errors[instance] = getattr(exc, 'code', type(exc).__name__)
        self.seal_pending()  # Independent of Redis availability or delivery ACK.
        installation = getattr(self.registry, 'installation_runtime', None)
        if installation is not None:
            self._validation_cursor = installation.reconcile_validations(self.artifacts, after=self._validation_cursor)
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # On-demand wake-up is derived from durable queued work, never a
            # restart replay of an already executing attempt.
            for candidate in db.execute("SELECT p.* FROM instance_policies p JOIN model_deployments d ON d.id=p.instance_id WHERE d.enabled=1 AND d.install_state='ready' AND p.desired_state='unloaded' AND p.status IN ('unloaded','waiting_runtime') AND p.error_code IS NULL AND EXISTS(SELECT 1 FROM tasks WHERE tasks.model_key=p.instance_id AND tasks.status='queued' AND tasks.execution_mode='worker') LIMIT 128").fetchall():
                if self.authority._policy(candidate)["policy"]["residency"] in {"on_demand", "idle"}:
                    db.execute("UPDATE instance_policies SET desired_state='loaded',revision=revision+1,version=version+1 WHERE instance_id=?", (candidate["instance_id"],))
            instances = [row[0] for row in db.execute("SELECT instance_id FROM instance_policies ORDER BY instance_id LIMIT 128")]
        for instance in instances:
            row = self.authority.get(instance)
            if row["policy"] is None or row["status"] == "legacy_unreconciled":
                continue
            try:
                self._instance(row)
                self.last_errors.pop(instance, None)
            except Exception as exc:
                self._configuration_failed(instance, exc)
                code = (str(exc) if isinstance(exc, GPUBusyError)
                        else getattr(exc, "code", None)) or "runtime_unavailable"
                self.last_errors[instance] = code
                if row["claim"] is None:
                    self.authority.waiting(instance, code)

    def install_instance(self, instance):
        """Project installed metadata and materialize its stopped container."""
        if self.package_provider is None: raise TaskStateError('runtime_package_unavailable')
        old = self.authority.get(instance)
        deployment = self.repository.get_deployment(instance)
        mapping = dict(zip(self.scheduler.allowed_indices, self.scheduler.allowed_uuids))
        if deployment and deployment.get('removal_operation_id'):
            raise TaskStateError('deployment_removal_pending')
        if not deployment:
            raise TaskStateError('explicit_gpu_uuid_pool_required')
        current_revision = deployment.get('current_config_revision')
        revision = (self.repository.get_model_deployment_revision(instance, current_revision)
                    if current_revision is not None else None)
        if current_revision is not None and revision is None:
            raise TaskStateError('deployment_config_revision_not_found')
        if revision is not None:
            gpu_uuids = list(revision['gpu_uuids'])
            if (not gpu_uuids
                    or any(gpu not in set(self.scheduler.allowed_uuids) for gpu in gpu_uuids)):
                raise TaskStateError('explicit_gpu_uuid_pool_required')
        else:
            if any(index not in mapping for index in deployment['gpu_indices']):
                raise TaskStateError('explicit_gpu_uuid_pool_required')
            gpu_uuids = [mapping[index] for index in deployment['gpu_indices']]
        value = self.package_provider.default_policy(instance, gpu_uuids)
        if revision is not None:
            if value['base_mib'] + value['task_mib'] != revision['required_vram_mib']:
                raise TaskStateError('deployment_vram_revision_mismatch')
            value.update(
                sharing_mode=revision['sharing_mode'],
                external_reserve_mib=revision['external_reserve_mib'],
                residency=revision['residency'], idle_seconds=revision['idle_seconds'],
            )
        try:
            self.scheduler.validate_budget(value)
        except Exception as exc:
            # An older explicit reservation can become structurally impossible
            # after a runtime-template/GPU-capacity upgrade.  Only that upgrade
            # path may fall back to the new template defaults; interactive policy
            # edits remain strict and are never silently rewritten.
            changed_template = bool(old and old['policy'] and
                old['policy']['package_id'] != value['package_id'])
            if str(exc) != "gpu_policy_unschedulable" or not changed_template:
                raise TaskStateError(str(exc) or "gpu_policy_unschedulable") from None
            value = self.package_provider.default_policy(instance,
                gpu_uuids,
                include_runtime_options=False)
            if revision is not None:
                if value['base_mib'] + value['task_mib'] != revision['required_vram_mib']:
                    raise TaskStateError('deployment_vram_revision_mismatch')
                value.update(
                    sharing_mode=revision['sharing_mode'],
                    external_reserve_mib=revision['external_reserve_mib'],
                    residency=revision['residency'], idle_seconds=revision['idle_seconds'],
                )
            try:
                self.scheduler.validate_budget(value)
            except Exception as fallback_exc:
                raise TaskStateError(str(fallback_exc) or "gpu_policy_unschedulable") from None
        result = (old if old and old['policy'] == value else
                  self.authority.configure(instance, value, expected_version=old['version'] if old else None))
        if not deployment['enabled']:
            return self.materialize_stopped_instance(instance)
        return result

    def materialize_stopped_instance(self, instance):
        """Create the exact configured container while task admission stays closed."""
        if self.package_provider is None:
            raise TaskStateError("runtime_package_unavailable")
        deployment = self.repository.get_deployment(instance)
        if deployment and deployment.get("removal_operation_id"):
            raise TaskStateError("deployment_removal_pending")
        if (not deployment or deployment["install_state"] != "ready"
                or deployment["enabled"]):
            raise TaskStateError("stopped_service_required")
        row = self.authority.get(instance)
        if (row is None or row["policy"] is None
                or row.get("pending_policy") is not None
                and row.get("configuration_state") != "applying"):
            raise TaskStateError("instance_policy_required")
        if row["claim"] is None:
            # Remove only exact proven-exited Docker objects. Model assets and
            # immutable package/evidence records are deliberately retained.
            self.remove_instance_containers(instance)
            package = self.package_provider.ensure(instance, allow_stopped=True)
            self.packages[package.record_id] = package
            identity = package.verify()
            self.scheduler.materialize_container(instance, identity)
            row = self.authority.get(instance)
        package, _ = self._package(row)
        controller = self._controller(package)
        claim = row["claim"]
        execution = json.loads(claim["execution_json"])
        if "owned_execution" not in execution:
            with self.repository._connect() as db:
                saved = db.execute(
                    "SELECT intent_id FROM runtime_intents WHERE instance_id=? AND epoch=?",
                    (instance, claim["epoch"]),
                ).fetchone()
            record = (controller.get(saved[0]) if saved else
                      controller.prepare(instance, claim["epoch"], claim["revision"]))
            self.authority.bind_execution(claim["claim_id"], {"intent_id": record["intent_id"]})
        else:
            record = controller.get(execution["owned_execution"]["intent_id"])
        if record["state"] == "prepared":
            record = controller.create_domain(record["intent_id"], record["version"])
        if record["state"] == "domain_ready":
            record = controller.create(record["intent_id"], record["version"])
        if record["state"] in {"create_pending", "create_unknown", "created_unverified"}:
            record = controller.reconcile(record["intent_id"], record["version"])
        if record["state"] != "created":
            raise TaskStateError("container_materialization_unconfirmed")
        result = self.authority.container_materialized(instance, claim["epoch"])
        if result.get("configuration_state") == "applying":
            result = self.authority.commit_configuration(instance)
        return result

    def remove_instance_containers(self, instance):
        """Delete exact stopped containers while retaining immutable runtime evidence."""
        with self.repository._connect() as db:
            rows = db.execute(
                """SELECT intent_id,epoch FROM runtime_intents
                   WHERE instance_id=? AND state='exited' AND container_id IS NOT NULL
                   ORDER BY generation""",
                (instance,),
            ).fetchall()
        if rows and self.package_provider is None:
            raise TaskStateError("runtime_package_unavailable")
        removed = []
        for row in rows:
            package = self.package_provider.for_epoch_removal(instance, row["epoch"])
            self.packages[package.record_id] = package
            controller = self._controller(package)
            removed.append(controller.remove_exited(row["intent_id"]))
        return removed

    def retire_instance_containers(self, instance):
        """Retire one stopped service container, preserving every model asset."""
        deployment = self.repository.get_deployment(instance)
        if not deployment or deployment["enabled"]:
            raise TaskStateError("service_runtime_active")
        row = self.authority.get(instance)
        if row and row["claim"]:
            package, _ = self._package(row)
            controller = self._controller(package)
            execution = json.loads(row["claim"]["execution_json"])
            owned = execution.get("owned_execution")
            if owned is None:
                raise TaskStateError("service_runtime_active")
            record = controller.get(owned["intent_id"])
            if record["state"] in {"prepared", "domain_ready", "created"}:
                record = controller.abandon_before_start(record["intent_id"], record["version"])
            elif record["state"] in {"stop_pending", "exit_unconfirmed"}:
                with self.repository._connect() as db:
                    self.repository.assert_removal_exit_recoverable_tx(db, instance, record["intent_id"])
                record = controller.reconcile(record["intent_id"], record["version"])
            elif record["state"] != "exited":
                raise TaskStateError("service_runtime_active")
            if record["state"] == "exited":
                self.authority.confirm_container_exit(row["claim"]["claim_id"], controller)
        return self.remove_instance_containers(instance)

    def _instance(self, row):
        deployment = self.repository.get_deployment(row["instance_id"])
        if deployment and deployment.get("removal_operation_id"):
            return
        enabled = bool(deployment and deployment["enabled"] and deployment["install_state"] == "ready")
        if not enabled and row["claim"] is None:
            return
        if not enabled and self.authority.close_unstarted_claim(row["instance_id"]):
            return
        if row["policy"]["backend"] == "legacy":
            raise TaskStateError("legacy_model_runtime_retired")
        package, identity = self._package(row)
        if row["claim"] is None:
            self.remove_instance_containers(row["instance_id"])
            self.scheduler.start_container(row["instance_id"], identity)
            row = self.authority.get(row["instance_id"])
        claim = row["claim"]
        controller = self._controller(package)
        execution = json.loads(claim["execution_json"])
        if "owned_execution" not in execution:
            with self.repository._connect() as db:
                saved = db.execute("SELECT intent_id FROM runtime_intents WHERE instance_id=? AND epoch=?", (row["instance_id"], claim["epoch"])).fetchone()
            record = controller.get(saved[0]) if saved else controller.prepare(row["instance_id"], claim["epoch"], claim["revision"])
            self.authority.bind_execution(claim["claim_id"], {"intent_id": record["intent_id"]})
        else:
            record = controller.get(execution["owned_execution"]["intent_id"])
        if self._recover_reboot_exit(row, package, controller, record):
            return
        if (not enabled and row.get("configuration_state") in {"restart_pending", "replace_pending", "failed"}
                and record["state"] in {"prepared", "domain_ready", "created", "stop_pending", "exit_unconfirmed", "exited"}):
            # A materialized, never-started container still owns its old binding.
            # Retire it with durable exit evidence before apply/rollback can switch.
            # Resume unconfirmed retirement here without requiring worker transport.
            with self.repository._connect() as db:
                never_started = not db.execute(
                    "SELECT 1 FROM runtime_effects WHERE intent_id=? AND action='start'",
                    (record["intent_id"],)).fetchone()
            if never_started:
                current = self.authority.get(row["instance_id"])
                if (current["claim"] is None or current["claim"]["claim_id"] != claim["claim_id"]
                        or current["claim"]["epoch"] != claim["epoch"]):
                    raise TaskStateError("backend_claim_conflict")
                self._stop_and_recover(current, package, controller, record)
                return
        if record["state"] == "prepared":
            record = controller.create_domain(record["intent_id"], record["version"])
        if record["state"] == "domain_ready":
            record = controller.create(record["intent_id"], record["version"])
        if record["state"] == "created" and not enabled:
            stopped = self.authority.container_materialized(row["instance_id"], claim["epoch"])
            if stopped.get("configuration_state") == "applying":
                self.authority.commit_configuration(row["instance_id"])
            return
        transport = self._transport(package)
        if record["state"] == "created" and enabled:
            record = controller.start(record["intent_id"], record["version"])
        elif record["state"] in {"create_pending", "create_unknown", "created_unverified",
                                "start_pending", "start_unknown", "stop_pending",
                                "stop_unknown", "exit_unconfirmed"}:
            record = controller.reconcile(record["intent_id"], record["version"])
        if record["state"] == "running":
            try:
                running = controller.observe_running(record["intent_id"])
            except ContainerError:
                self.authority.require_stop(row["instance_id"], claim["epoch"], "runtime_observation_unknown")
                raise
            if not running:
                self.authority.require_stop(row["instance_id"], claim["epoch"], "runtime_exited")
            heartbeat = None
            try:
                heartbeat = transport.telemetry("heartbeat")
            finally:
                self.authority.observe_heartbeat(row["instance_id"], claim["epoch"], heartbeat)
        row = self.authority.get(row["instance_id"])
        if row["claim"]["stop_reason"]:
            self._stop_and_recover(row, package, controller, record)
            return
        key = (package.instance_id, package.epoch)
        clock = time.monotonic()
        cursor = self._cursors.get(key, 0)
        if clock >= self._cursor_replay_at.get(key, 0):
            cursor = 0
            self._cursor_replay_at[key] = clock + 60
        replay = OutboxRelay(self.authority.tasks, transport).flush(replay=True, after_sequence=cursor, max_pages=1)
        self._cursors[key] = replay["next_cursor"]
        transport.promote_events()
        DeliveryPump(transport, InstanceEventHandler(self.authority)).once("events", count=20, block_ms=1)
        self.seal_pending(instance=package.instance_id, epoch=package.epoch)
        row = self.authority.get(row["instance_id"])
        claim = row["claim"]
        if (row.get("configuration_state") == "applying" and claim["registered"]
                and (row["policy"]["residency"] != "resident"
                     or row["status"] == "loaded")):
            row = self.authority.commit_configuration(row["instance_id"])
            claim = row["claim"]
        if not enabled:
            if claim["state"] in {"loaded", "loading", "unloading"}:
                self.authority.unload(row["instance_id"])
                return
            if claim["state"] == "online_unloaded" and record["state"] == "running":
                record = controller.stop(record["intent_id"], record["version"])
            if record["state"] == "exited" and self.recover_before_release(row, package, controller):
                self.authority.confirm_container_exit(claim["claim_id"], controller)
            return
        if row["desired_state"] == "loaded" and claim["state"] == "online_unloaded":
            try:
                self.scheduler.load_model(row["instance_id"])
            except (GPUBusyError, TaskStateError) as exc:
                code = (str(exc) if isinstance(exc, GPUBusyError)
                        else getattr(exc, "code", None)) or "runtime_unavailable"
                self.authority.model_waiting(row["instance_id"], code)
                raise
            return
        if row["desired_state"] == "unloaded" and claim["state"] == "loaded":
            self.authority.unload(row["instance_id"])
            return
        if row["status"] != "loaded":
            return
        with self.repository._connect() as db:
            task = db.execute("SELECT id FROM tasks WHERE status='queued' AND execution_mode='worker' AND model_key=? ORDER BY created_at,id LIMIT 1", (row["instance_id"],)).fetchone()
        if task:
            self.scheduler.dispatch(self.repository.get_task(task[0]))
        else:
            self.authority.idle_unload(row["instance_id"])

    def _recover_reboot_exit(self, row, package, controller, record):
        # No Worker transport or normal old-cgroup observation precedes this
        # path. The Controller still verifies package/engine/container identity.
        recovered = controller.recover_host_reboot(record["intent_id"], record["version"])
        if (recovered["state"] != "exited" or
                json.loads(recovered["exit_evidence_json"]).get("kind") != "host_reboot"):
            return False
        self.authority.require_stop(row["instance_id"], row["claim"]["epoch"],
                                    "runtime_exited", only_if_unfenced=True)
        current = self.authority.get(row["instance_id"])
        claim = current.get("claim") if current else None
        if (not claim or claim["claim_id"] != row["claim"]["claim_id"]
                or claim["epoch"] != recovered["epoch"]
                or json.loads(claim["execution_json"]).get("owned_execution", {}).get("intent_id") != recovered["intent_id"]):
            raise TaskStateError("backend_claim_conflict")
        # Existing quarantine/explicit-stop intent is never inferred away.
        if self.recover_before_release(current, package, controller):
            self.authority.confirm_container_exit(current["claim"]["claim_id"], controller)
        return True

    def _stop_and_recover(self, row, package, controller, record):
        if self._recover_reboot_exit(row, package, controller, record):
            return
        if record["state"] in {"prepared", "domain_ready", "created"}:
            record = controller.abandon_before_start(record["intent_id"], record["version"])
        elif record["state"] == "running":
            record = controller.stop(record["intent_id"], record["version"])
        elif record["state"] != "exited":
            record = controller.reconcile(record["intent_id"], record["version"])
        if record["state"] == "exited" and self.recover_before_release(row, package, controller):
            self.authority.confirm_container_exit(row["claim"]["claim_id"], controller)

    def cancel_tick(self):
        # No Redis read or write precedes this durable deadline processing.
        self.authority.tasks.expire_due_tasks()
        for deadline in self.authority.due_cancellations():
            try:
                with self.repository._connect() as db:
                    pending = db.execute("""SELECT 1 FROM instance_cancel_deadlines d
                        JOIN task_attempts a ON a.id=d.attempt_id
                        WHERE d.attempt_id=? AND d.claim_id=? AND d.state='stop_requested'
                        AND a.exit_confirmed=0 AND a.epoch=? AND a.instance_id=?""",
                        (deadline['attempt_id'], deadline['claim_id'], deadline['epoch'], deadline['instance_id'])).fetchone()
                if not pending:
                    continue
                row = self.authority.get(deadline["instance_id"])
                if (row is None or row['claim'] is None
                        or row['claim']['claim_id'] != deadline['claim_id']
                        or row['claim']['epoch'] != deadline['epoch']):
                    continue
                if row["claim"]["backend"] == "legacy":
                    raise TaskStateError("legacy_model_runtime_retired")
                package, _ = self._package(row)
                controller = self._controller(package)
                execution = json.loads(row["claim"]["execution_json"])
                record = controller.get(execution["owned_execution"]["intent_id"])
                if record["state"] == "running":
                    record = controller.stop(record["intent_id"], record["version"])
                elif record["state"] != "exited":
                    record = controller.reconcile(record["intent_id"], record["version"])
                if record["state"] == "exited":
                    if self.recover_before_release(row, package, controller):
                        self.authority.confirm_container_exit(row["claim"]["claim_id"], controller)
            except Exception as exc:
                self.last_errors[deadline["attempt_id"]] = getattr(exc,"code",type(exc).__name__)
        # Persisted liveness fences survive Server restart and are processed
        # without a Redis dependency, just like cancellation deadlines.
        with self.repository._connect() as db:
            instances = [item[0] for item in db.execute("SELECT instance_id FROM instance_claims WHERE stop_reason IS NOT NULL AND state!='exited' ORDER BY instance_id LIMIT 100")]
        for instance in instances:
            try:
                row = self.authority.get(instance)
                if row is None or row['claim'] is None or row['claim']['stop_reason'] is None:
                    continue
                package, _ = self._package(row)
                controller = self._controller(package)
                owned = json.loads(row["claim"]["execution_json"])["owned_execution"]
                self._stop_and_recover(row, package, controller, controller.get(owned["intent_id"]))
            except Exception as exc:
                self.last_errors[instance] = getattr(exc,"code",type(exc).__name__)

    def recover_before_release(self, row, package, controller):
        from .repository import _startup_schema_connection
        from .capabilities import worker_capability_for
        claim = row["claim"]
        owned = json.loads(claim["execution_json"])["owned_execution"]
        record = controller.get(owned["intent_id"])
        if record["state"] != "exited" or not record["exit_evidence_json"]:
            raise TaskStateError("recovery_execution_exit_unconfirmed")
        with self.repository._connect() as db:
            attempts = {item["id"]:dict(item) for item in db.execute("SELECT * FROM task_attempts WHERE instance_id=? AND epoch=? AND exit_confirmed=0", (claim["instance_id"], claim["epoch"]))}
        if not attempts:
            return True
        capability = package.recovery_capability()
        evidence = "journal-" + digest([claim["claim_id"], record["exit_evidence_digest"]])
        with _startup_schema_connection(package.journal_path()) as connection:
            journal = JournalSnapshot(connection, claim["instance_id"], {capability["model_key"]:capability})
            cursor = 0
            for _ in range(100):
                page = journal.recovery(after_sequence=cursor, limit=100)
                for work in page["work"]:
                    if work["kind"] != "task" or work["scope_id"] not in attempts:
                        continue
                    attempt = attempts[work["scope_id"]]
                    command = json.loads(work["envelope_json"])
                    if work["epoch"] != claim["epoch"] or work["task_id"] != attempt["task_id"]:
                        raise TaskStateError("recovery_attempt_identity_mismatch")
                    with self.repository._connect() as db:
                        original = db.execute("SELECT * FROM task_outbox WHERE message_id=? AND task_id=? AND attempt_id=?", (command["message_id"], attempt["task_id"], attempt["id"])).fetchone()
                    if not original or original["digest"] != digest(command) or digest(json.loads(original["envelope_json"])) != digest(command):
                        raise TaskStateError("recovery_command_identity_mismatch")
                    self.authority.tasks.authorize_recovery(attempt["task_id"], attempt["id"], claim["epoch"], evidence)
                cursor = page["next_cursor"]
                if not page["has_more"]: break
            else:
                raise TaskStateError("recovery_page_budget_exceeded")
            cursor = 0
            for _ in range(100):
                page = journal.pending_events(after_sequence=cursor, limit=100)
                for event in page["items"]:
                    if event.get("attempt_id") in attempts:
                        self.authority.tasks.receive(event)
                cursor = page["next_cursor"]
                if not page["has_more"]: break
            else:
                raise TaskStateError("recovery_page_budget_exceeded")
        self.seal_pending(instance=claim["instance_id"], epoch=claim["epoch"])
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for attempt_id in attempts:
                if db.execute("SELECT 1 FROM task_inbox WHERE attempt_id=? AND result='pending'", (attempt_id,)).fetchone():
                    return False  # Completed data without trusted seal is not interruption.
            current = InstancePolicy.assert_claim(db, claim["instance_id"], claim["epoch"], claim_id=claim["claim_id"])
            if current["execution_digest"] != claim["execution_digest"]:
                raise TaskStateError("recovery_execution_identity_changed")
            db.execute("UPDATE instance_claims SET recovery_evidence=? WHERE claim_id=?", (evidence, claim["claim_id"]))
        return True
