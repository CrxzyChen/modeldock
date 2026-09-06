from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from mediacenter.repository import Repository
from mediacenter.domain import ServiceKind
from mediacenter.task_state import TaskState, TaskStateError, digest
from mediacenter.protocol import PROTOCOL, validate_envelope
from mediacenter.client_events import EventCursor, MemoryEventBroker, StateEventObserver
from mediacenter.runtime_profiles import RuntimeProfileManager
from tests.test_resident_policy import ready_instance, fixture_capacity


class TaskStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = Repository(self.root / "state.db")
        self.state = TaskState(self.repository)
        self.request = {"service": "image", "model": "deployment-one", "prompt": "a garden", "options": {}, "inputs": []}
        self.binding = {"model_key": "sdxl-base-1.0", "recipe_revision": "recipe-v1",
                        "model_asset_id": "mdl_base", "model_asset_revision": "revision-v1", "dependencies": []}
        self.gpus = {"GPU-one": 100, "GPU-two": 100}
        self.authority = ready_instance(self.repository, self.state, binding=self.binding, limits=self.gpus)
        self.initial_counts = {}
        with self.repository._connect() as db:
            for table in ("task_outbox", "task_reservations"):
                self.initial_counts[table] = db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def tearDown(self):
        self.temp.cleanup()

    def accepted(self, key=None):
        return self.state.accept(self.request, scope="admin", key=key, binding=self.binding)[0]

    def dispatched(self):
        task = self.accepted()
        command = self.state.dispatch(task["id"], task["version"], "instance-one", "epoch-one", {key: 20 for key in self.gpus}, observe_capacity=fixture_capacity)
        return task, command

    def event(self, command, seq=1, kind="task.accepted", payload=None):
        if payload is None:
            payload = {key: command["payload"][key] for key in ("reservation_id", "reservation_generation")}
        return {"protocol": PROTOCOL, "type": kind, "message_id": f"evt-{command['attempt_id']}-{seq}",
                "server_id": "mediacenter", "instance_id": command["instance_id"], "worker_epoch": command["worker_epoch"],
                "correlation_id": command["task_id"], "created_at": "2020-01-01T00:00:00Z", "event_seq": seq,
                "task_id": command["task_id"], "attempt_id": command["attempt_id"], "payload": payload}

    def seal(self, command, asset_id="art-one", path="image/one.png"):
        data = b"sealed test image"
        sha = hashlib.sha256(data).hexdigest()
        self.state.record_sealed_artifact(command["task_id"], command["attempt_id"], cancel_revision=0,
            asset_id=asset_id, revision="v1", sha256=sha, relative_path=path, byte_size=len(data), execution={"fixture": True})
        return {"asset_id": asset_id, "revision": "v1", "sha256": sha}

    def count(self, table):
        with self.repository._connect() as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] - self.initial_counts.get(table, 0)

    def test_idempotent_multiconnection_accept_and_conflict(self):
        def accept(_):
            return TaskState(Repository(self.repository.path)).accept(self.request, scope="admin", key="same-key", binding=self.binding)[0]["id"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(accept, range(8)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(self.count("audit_events"), 1)
        with self.assertRaisesRegex(TaskStateError, "idempotency_conflict"):
            self.state.accept(dict(self.request, prompt="different"), scope="admin", key="same-key")
        self.assertNotEqual(self.state.accept(self.request, scope="other", key="same-key")[0]["id"], ids[0])
        self.assertNotEqual(self.accepted()["id"], self.accepted()["id"])

    def test_accept_failure_at_each_write_rolls_back_task_and_audit(self):
        for point in ("accept.task", "accept.audit"):
            with self.subTest(point=point):
                self.state.fault = lambda stage: (_ for _ in ()).throw(RuntimeError(stage)) if stage == point else None
                with self.assertRaises(RuntimeError):
                    self.accepted("rollback-key")
                self.assertEqual(self.count("tasks"), 0)
                self.assertEqual(self.count("audit_events"), 0)

    def test_dispatch_double_claim_single_attempt_and_command(self):
        task = self.accepted()
        def dispatch(_):
            try:
                return self.state.dispatch(task["id"], 1, "instance-one", "epoch-one", {key: 20 for key in self.gpus}, observe_capacity=fixture_capacity)
            except TaskStateError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(dispatch, range(2)))
        self.assertEqual(sum(item is not None for item in results), 1)
        self.assertEqual(self.count("task_attempts"), 1)
        self.assertEqual(self.count("task_outbox"), 1)
        self.assertEqual(self.count("task_reservations"), 2)
        validate_envelope(next(item for item in results if item))

    def test_dispatch_blocks_applying_configuration_before_any_execution_write(self):
        task = self.accepted()
        before = {table: self.count(table) for table in ('task_attempts', 'task_outbox', 'task_reservations')}
        with self.repository._connect() as db:
            db.execute("UPDATE instance_policies SET configuration_state='applying' WHERE instance_id='instance-one'")
        with self.assertRaisesRegex(TaskStateError, 'deployment_configuration_pending'):
            self.state.dispatch(task['id'], 1, 'instance-one', 'epoch-one',
                                {key: 20 for key in self.gpus}, observe_capacity=fixture_capacity)
        self.assertEqual(before, {table: self.count(table) for table in before})
        with self.repository._connect() as db:
            db.execute("UPDATE instance_policies SET configuration_state='applied' WHERE instance_id='instance-one'")
        self.state.dispatch(task['id'], 1, 'instance-one', 'epoch-one',
                            {key: 20 for key in self.gpus}, observe_capacity=fixture_capacity)

    def test_task_deployment_revision_is_durable_and_dispatch_fails_closed_on_drift(self):
        profile = RuntimeProfileManager(self.repository).register(
            RuntimeProfileManager.sdxl_single_file("sha256:" + "b" * 64))
        deployment = self.repository.get_deployment("instance-one")
        asset = self.repository.get_model_asset(deployment["asset_id"])
        revision = {
            "deployment_id": "instance-one", "config_revision": 7,
            "runtime_profile_id": profile["profile_id"],
            "runtime_profile_revision": profile["revision"],
            "runtime_profile_digest": profile["profile_digest"],
            "runtime_image_digest": profile["image_digest"],
            "base_asset_id": asset["id"], "base_asset_revision": asset["revision"],
            "base_asset_manifest_digest": asset["manifest_digest"],
            "vae_asset_id": None, "vae_asset_revision": None,
            "vae_asset_manifest_digest": None,
            "gpu_uuids": list(self.gpus), "required_vram_mib": 20,
            "sharing_mode": "shared", "residency": "resident",
            "external_reserve_mib": 8192, "idle_seconds": 0,
            "license_confirmation": {"accepted": True},
            "experimental_compatibility_accepted": False,
            "desired_state": "running", "config_digest": "fixture-config-seven",
            "created_at": "fixture",
        }
        self.repository.put_model_deployment_revision(revision)
        self.assertTrue(self.repository.set_model_deployment_revision_head(
            "instance-one", 7, expected_current=None, pending=False, updated_at="fixture"))
        task = self.state.accept(
            self.request, scope="admin", binding=self.binding,
            deployment_id="instance-one", deployment_config_revision=7,
        )[0]
        self.assertEqual((task["deployment_id"], task["deployment_config_revision"]),
                         ("instance-one", 7))
        with self.repository._connect() as db:
            db.execute("UPDATE model_deployments SET current_config_revision=8 WHERE id='instance-one'")
        with self.assertRaisesRegex(TaskStateError,
                                    "deployment_config_revision_changed"):
            self.state.dispatch(
                task["id"], task["version"], "instance-one", "epoch-one",
                {key: 20 for key in self.gpus}, observe_capacity=fixture_capacity)
        self.assertEqual(self.count("task_attempts"), 0)
        self.assertEqual(self.count("task_reservations"), 0)

    def test_dispatch_faults_rollback_every_boundary(self):
        task = self.accepted()
        for point in ("dispatch.attempt", "dispatch.reservation", "dispatch.task", "dispatch.outbox"):
            with self.subTest(point=point):
                self.state.fault = lambda stage: (_ for _ in ()).throw(RuntimeError(stage)) if stage == point else None
                with self.assertRaises(RuntimeError):
                    self.state.dispatch(task["id"], 1, "instance-one", "epoch-one", {key: 20 for key in self.gpus}, observe_capacity=fixture_capacity)
                for table in ("task_attempts", "task_reservations", "task_outbox"):
                    self.assertEqual(self.count(table), 0)
                self.assertEqual(self.repository.get_task(task["id"])["version"], 1)

    def test_all_gpus_or_none_including_second_gpu_capacity(self):
        task = self.accepted()
        for gpus in ({"GPU-one": 10}, {"GPU-one": 10, "GPU-two": 101}):
            with self.assertRaises(TaskStateError):
                self.state.dispatch(task["id"], 1, "instance-one", "epoch-one", gpus, observe_capacity=fixture_capacity)
            self.assertEqual(self.count("task_attempts"), 0)
            self.assertEqual(self.count("task_reservations"), 0)

    def test_cancel_queued_dispatch_race_is_atomic(self):
        task = self.accepted()
        def dispatch():
            try:
                self.state.dispatch(task["id"], 1, "instance-one", "epoch-one", {key: 20 for key in self.gpus}, observe_capacity=fixture_capacity)
            except TaskStateError:
                pass
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(dispatch)
            second = pool.submit(self.state.cancel, task["id"])
            first.result(); second.result()
        final = self.repository.get_task(task["id"])
        commands = self.state.outbox()
        if final["status"] == "canceled":
            self.assertEqual(commands, [])
            self.assertEqual(self.count("task_attempts"), 0)
        else:
            self.assertEqual(final["status"], "cancel_requested")
            self.assertEqual([item["type"] for item in commands], ["task.execute", "task.cancel"])

    def test_running_cancel_persists_gpu_cooperative_grace_before_container_fence(self):
        state = TaskState(self.repository, cancel_grace_seconds=60)
        task, command = self.dispatched()
        before = datetime.now(timezone.utc)
        state.cancel(task["id"])
        with self.repository._connect() as db:
            deadline = db.execute(
                "SELECT due_at,state FROM instance_cancel_deadlines WHERE attempt_id=?",
                (command["attempt_id"],),
            ).fetchone()
        due = datetime.fromisoformat(deadline["due_at"])
        self.assertEqual(deadline["state"], "waiting")
        self.assertGreaterEqual((due - before).total_seconds(), 59)
        self.assertLessEqual((due - before).total_seconds(), 61)

    def test_cancel_grace_is_bounded_configuration(self):
        for value in (0, 301, 1.5, True):
            with self.subTest(value=value), self.assertRaisesRegex(TaskStateError, "invalid_cancel_grace"):
                TaskState(self.repository, cancel_grace_seconds=value)

    def test_cancel_success_arbitration_and_manifest_authority(self):
        for cancel_first in (True, False):
            with self.subTest(cancel_first=cancel_first):
                task, command = self.dispatched()
                manifest = self.seal(command, asset_id=command["attempt_id"], path=f"image/{command['attempt_id']}.png")
                if cancel_first:
                    self.state.cancel(task["id"])
                terminal = self.event(command, kind="task.terminal", payload={"status": "succeeded", "error_code": None, "manifest": manifest})
                self.assertEqual(self.state.receive(terminal), "applied")
                if not cancel_first:
                    self.state.cancel(task["id"])
                result = self.repository.get_task(task["id"])
                self.assertEqual(result["status"], "canceled" if cancel_first else "succeeded")
                self.assertEqual(self.repository.authorized_artifact(f"image/{command['attempt_id']}.png") is None, cancel_first)
                self.state.confirm_exit(task["id"], command["attempt_id"], instance_id="instance-one", epoch="epoch-one", evidence="ended-" + command["attempt_id"])

    def test_different_tasks_same_instance_are_single_flight_even_after_terminal(self):
        tasks = [self.accepted(), self.accepted()]
        def dispatch(task):
            try:
                return self.state.dispatch(task["id"], 1, "instance-one", "epoch-one", {key: 20 for key in self.gpus}, observe_capacity=fixture_capacity)
            except TaskStateError as error:
                self.assertEqual(error.code, "instance_execution_unconfirmed")
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(dispatch, tasks))
        self.assertEqual(sum(item is not None for item in results), 1)
        command = next(item for item in results if item)
        other = next(task for task in tasks if task["id"] != command["task_id"])
        self.state.receive(self.event(command, kind="task.terminal", payload={"status": "failed", "error_code": "failed", "manifest": None}))
        with self.assertRaisesRegex(TaskStateError, "instance_execution_unconfirmed"):
            self.state.dispatch(other["id"], 1, "instance-one", "epoch-one", {key: 20 for key in self.gpus}, observe_capacity=fixture_capacity)
        self.authority.quarantine("instance-one", "epoch-one", "fixture_lost_epoch")
        with self.assertRaisesRegex(TaskStateError, "backend_claim_required"):
            self.state.dispatch(other["id"], 1, "instance-one", "epoch-two", {key: 20 for key in self.gpus}, observe_capacity=fixture_capacity)

    def test_exit_required_retry_and_base_reservation_survives(self):
        with self.repository._connect() as db:
            base = db.execute("SELECT reservation_id,generation FROM task_reservations WHERE kind='base'").fetchone()
        self.state.reserve_base("instance-one", "epoch-one", base[0], base[1], {key: 10 for key in self.gpus})
        task, command = self.dispatched()
        self.state.receive(self.event(command, kind="task.terminal", payload={"status": "failed", "error_code": "test_failure", "manifest": None}))
        task = self.repository.get_task(task["id"])
        with self.assertRaisesRegex(TaskStateError, "execution_exit_unconfirmed"):
            self.state.retry(task["id"], task["version"])
        self.state.confirm_exit(task["id"], command["attempt_id"], instance_id="instance-one", epoch="epoch-one", evidence="controller-exit-one")
        with self.assertRaisesRegex(TaskStateError, "task_version_conflict"):
            self.state.retry(task['id'], task['version'])
        task = self.repository.get_task(task['id'])
        with self.repository._connect() as db:
            self.assertEqual(db.execute("SELECT SUM(mib) FROM task_reservations WHERE released=0").fetchone()[0], 20)
        retried = self.state.retry(task["id"], task["version"])
        next_command = self.state.dispatch(task["id"], retried["version"], "instance-one", "epoch-one", {key: 20 for key in self.gpus}, observe_capacity=fixture_capacity)
        self.assertNotEqual(command["attempt_id"], next_command["attempt_id"])
        self.assertEqual(self.state.receive(self.event(command, seq=2, kind="task.terminal", payload={"status": "interrupted", "error_code": "late", "manifest": None})), "stale")
        self.assertEqual(self.repository.get_task(task["id"])["current_attempt_id"], next_command["attempt_id"])

    def test_controller_exit_without_worker_terminal_interrupts_original_attempt(self):
        task, command = self.dispatched()
        self.state.confirm_exit(task["id"], command["attempt_id"], instance_id="instance-one", epoch="epoch-one", evidence="container-exited")
        self.assertEqual(self.repository.get_task(task["id"])["status"], "interrupted")
        self.assertEqual(self.count("task_attempts"), 1)
        with self.repository._connect() as db:
            self.assertEqual(db.execute("SELECT SUM(released) FROM task_reservations").fetchone()[0], 2)

    def test_terminal_exit_proof_advances_task_sse_version_once_for_both_controllers(self):
        for path in ('attempt', 'domain'):
            with self.subTest(path=path):
                task, command = self.dispatched()
                self.state.receive(self.event(command, kind='task.terminal', payload={
                    'status':'failed', 'error_code':'adapter_execution_failed', 'manifest':None}))
                before = self.repository.get_task(task['id'])
                self.assertEqual(before['attempt']['exit_confirmed'], 0)
                broker = MemoryEventBroker(capacity=32)
                observer = StateEventObserver(self.repository, broker)
                self.assertEqual(observer.scan_once(), 0)
                evidence = 'confirmed-' + path
                if path == 'attempt':
                    self.state.confirm_exit(task['id'], command['attempt_id'], instance_id='instance-one',
                                            epoch='epoch-one', evidence=evidence)
                else:
                    with self.repository._connect() as db:
                        db.execute('BEGIN IMMEDIATE')
                        claim = db.execute("SELECT * FROM instance_claims WHERE instance_id='instance-one' AND state!='exited'").fetchone()
                        self.authority._close_claim(db, claim, {'fixture':'confirmed-domain-exit'})
                after = self.repository.get_task(task['id'])
                self.assertEqual(after['version'], before['version'] + 1)
                self.assertEqual(after['status'], 'failed')
                self.assertEqual(after['attempt']['exit_confirmed'], 1)
                observer.scan_once()
                events, _, reset = broker.wait(EventCursor(broker.stream_id, 0), timeout=0.01)
                self.assertFalse(reset)
                hints = [event for event in events if event['type'] == 'task.changed' and event['resource_id'] == task['id']]
                self.assertEqual(len(hints), 1)
                self.assertEqual(hints[0]['version'], after['version'])
                with self.repository._connect() as db:
                    self.state._record_exit_confirmation(db, task['id'], command['attempt_id'], after['attempt']['exit_evidence'])
                self.assertEqual(self.repository.get_task(task['id'])['version'], after['version'])
                self.assertEqual(observer.scan_once(), 0)

    def test_old_epoch_requires_explicit_original_attempt_recovery(self):
        task, command = self.dispatched()
        self.authority.quarantine("instance-one", "epoch-one", "fixture_lost_epoch")
        terminal = self.event(command, kind="task.terminal", payload={"status": "failed", "error_code": "old_failure", "manifest": None})
        with self.assertRaisesRegex(TaskStateError, "old_epoch_unreconciled"):
            self.state.receive(terminal)
        self.state.authorize_recovery(task["id"], command["attempt_id"], "epoch-one", "verified-old-journal")
        self.assertEqual(self.state.receive(terminal), "applied")
        self.assertEqual(self.count("task_attempts"), 1)

    def test_restart_does_not_recover_claim_or_release(self):
        task, command = self.dispatched()
        before = self.repository.get_task(task["id"])
        reopened = Repository(self.repository.path)
        TaskState(reopened)
        self.assertEqual(reopened.get_task(task["id"]), before)
        self.assertEqual(self.count("task_reservations"), 2)
        for method in ("claim_next", "recover_running", "update_task_if_status", "update_task_progress", "request_cancel", "insert_task"):
            self.assertFalse(hasattr(reopened, method), method)

    def deadline(self, command):
        with self.repository._connect() as db:
            row = db.execute("SELECT * FROM task_attempts WHERE id=?", (command['attempt_id'],)).fetchone()
        return datetime.fromisoformat(row['execution_deadline_at'])

    def test_dispatch_snapshots_service_timeout_without_reinterpreting_wire_expiry(self):
        self.repository.update_service(ServiceKind.IMAGE, {'timeout_seconds': 10})
        task, command = self.dispatched()
        deadline = self.deadline(command)
        start = datetime.fromisoformat(command['created_at'].replace('Z', '+00:00'))
        self.assertAlmostEqual((deadline - start).total_seconds(), 10, delta=1)
        self.assertAlmostEqual((datetime.fromisoformat(command['expires_at'].replace('Z', '+00:00')) - start).total_seconds(), 3600)
        self.repository.update_service(ServiceKind.IMAGE, {'timeout_seconds': 7200})
        self.assertEqual(self.deadline(command), deadline)
        self.assertEqual(self.state.expire_due_tasks(at=deadline - timedelta(seconds=1)), [])
        self.assertEqual(self.repository.get_task(task['id'])['status'], 'assigned')

    def test_deadline_restart_and_duplicate_scans_preserve_intent_without_release(self):
        task, command = self.dispatched()
        at = self.deadline(command)
        self.assertEqual(self.state.expire_due_tasks(at=at), [task['id']])
        current = self.repository.get_task(task['id'])
        self.assertEqual((current['status'], current['stage'], current['error']),
                         ('cancel_requested', 'timeout_stopping', 'task_timed_out'))
        before = {t: self.count(t) for t in ('audit_events', 'task_outbox', 'instance_cancel_deadlines')}
        restarted = TaskState(Repository(self.repository.path))
        for _ in range(3):
            self.assertEqual(restarted.expire_due_tasks(at=at + timedelta(seconds=30)), [])
        self.assertEqual(before, {t: self.count(t) for t in before})
        with self.repository._connect() as db:
            self.assertEqual(db.execute('SELECT termination_reason,exit_confirmed FROM task_attempts WHERE id=?',
                                       (command['attempt_id'],)).fetchone()[:], ('task_timed_out', 0))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM task_reservations WHERE kind='task' AND released=0").fetchone()[0], 2)
        self.state.confirm_exit(task['id'], command['attempt_id'], instance_id='instance-one', epoch='epoch-one', evidence='timeout-domain-exit')
        with self.repository._connect() as db:
            self.assertEqual(db.execute('SELECT state FROM instance_cancel_deadlines WHERE attempt_id=?', (command['attempt_id'],)).fetchone()[0], 'completed')
        result = self.repository.get_task(task['id'])
        self.assertEqual((result['status'], result['error']), ('failed', 'task_timed_out'))
        retry = self.state.retry(task['id'], result['version'])
        fresh = self.state.dispatch(task['id'], retry['version'], 'instance-one', 'epoch-one',
                                    {key: 20 for key in self.gpus}, observe_capacity=fixture_capacity)
        self.assertNotEqual(fresh['attempt_id'], command['attempt_id'])
        with self.repository._connect() as db:
            self.assertIsNone(db.execute('SELECT termination_reason FROM task_attempts WHERE id=?', (fresh['attempt_id'],)).fetchone()[0])

    def test_user_cancel_before_deadline_wins_and_after_deadline_is_timeout(self):
        for before_deadline in (True, False):
            task, command = self.dispatched()
            at = self.deadline(command) + timedelta(seconds=-1 if before_deadline else 1)
            with patch('mediacenter.task_state.now', return_value=at.isoformat()):
                self.state.cancel(task['id'])
                self.state.expire_due_tasks(at=at + timedelta(seconds=5))
            self.state.confirm_exit(task['id'], command['attempt_id'], instance_id='instance-one', epoch='epoch-one',
                                    evidence='first-cause-' + command['attempt_id'])
            result = self.repository.get_task(task['id'])
            self.assertEqual(result['status'], 'canceled' if before_deadline else 'failed')
            if not before_deadline:
                self.assertEqual(result['error'], 'task_timed_out')

    def test_timeout_intent_is_atomic_at_existing_cancel_fault_boundaries(self):
        task, command = self.dispatched()
        for point in ('cancel.task', 'cancel.outbox'):
            self.state.fault = lambda stage: (_ for _ in ()).throw(RuntimeError(stage)) if stage == point else None
            with self.assertRaisesRegex(RuntimeError, point):
                self.state.expire_due_tasks(at=self.deadline(command))
            self.assertEqual(self.repository.get_task(task['id'])['status'], 'assigned')
            self.assertEqual(self.count('instance_cancel_deadlines'), 0)
            self.assertEqual(self.count('task_outbox'), 1)
            with self.repository._connect() as db:
                self.assertIsNone(db.execute('SELECT termination_reason FROM task_attempts WHERE id=?', (command['attempt_id'],)).fetchone()[0])

    def test_late_success_without_quiescence_waits_for_exit_and_never_publishes(self):
        task, command = self.dispatched()
        manifest = self.seal(command)
        event = self.event(command, kind='task.terminal', payload={'status':'succeeded','error_code':None,'manifest':manifest})
        with patch('mediacenter.task_state.now', return_value=self.deadline(command).isoformat()):
            self.assertEqual(self.state.receive(event), 'pending')
        self.assertEqual(self.repository.get_task(task['id'])['status'], 'cancel_requested')
        self.assertEqual(self.count('task_artifacts'), 0)
        self.state.confirm_exit(task['id'], command['attempt_id'], instance_id='instance-one', epoch='epoch-one', evidence='late-result-exit')
        self.assertEqual(self.repository.get_task(task['id'])['error'], 'task_timed_out')
        self.assertEqual(self.state.receive(event), 'stale')
        self.assertEqual(self.count('task_artifacts'), 0)

    def test_timeout_quiescent_terminal_finalizes_and_releases_only_task_memory(self):
        task, command = self.dispatched()
        event = self.event(command, kind='task.terminal', payload={'status':'failed','error_code':'adapter_execution_failed','manifest':None})
        event['extensions'] = {'execution_quiescence': {
            'kind':'quiescent', 'command_message_id':command['message_id'], 'command_digest':digest(command),
            **{k:command[k] for k in ('server_id','instance_id','worker_epoch','task_id','attempt_id')},
            'execution_token':'execution-one', 'child_token':'child-one'}}
        with patch('mediacenter.task_state.now', return_value=self.deadline(command).isoformat()):
            self.assertEqual(self.state.receive(event), 'applied')
        self.assertEqual((self.repository.get_task(task['id'])['status'], self.repository.get_task(task['id'])['error']),
                         ('failed', 'task_timed_out'))
        with self.repository._connect() as db:
            self.assertEqual(db.execute("SELECT SUM(mib) FROM task_reservations WHERE released=0").fetchone()[0], 20)

    def test_persisted_timeout_wins_over_oom_and_failed_cleanup_until_exact_exit(self):
        for clean in (True, False):
            with self.subTest(clean=clean):
                task, command = self.dispatched()
                self.assertEqual(self.state.expire_due_tasks(at=self.deadline(command)), [task['id']])
                # Match the child/journal contract: reset failure replaces the
                # worker code, but it must not replace the server's first cause.
                code = 'model_out_of_memory' if clean else 'adapter_reset_failed'
                event = self.event(command, kind='task.terminal', payload={'status':'failed','error_code':code,'manifest':None})
                if clean:
                    event['extensions'] = {'execution_quiescence': {
                        'kind':'quiescent', 'command_message_id':command['message_id'], 'command_digest':digest(command),
                        **{k:command[k] for k in ('server_id','instance_id','worker_epoch','task_id','attempt_id')},
                        'execution_token':'execution-one', 'child_token':'child-one'}}
                self.assertEqual(self.state.receive(event), 'applied' if clean else 'pending')
                current = self.repository.get_task(task['id'])
                self.assertEqual((current['status'], current['error']),
                                 ('failed' if clean else 'cancel_requested', 'task_timed_out'))
                with self.repository._connect() as db:
                    attempt = db.execute('SELECT termination_reason,exit_confirmed FROM task_attempts WHERE id=?', (command['attempt_id'],)).fetchone()
                    self.assertEqual(tuple(attempt), ('task_timed_out', int(clean)))
                    held = db.execute("SELECT COUNT(*) FROM task_reservations WHERE attempt_id=? AND kind='task' AND released=0", (command['attempt_id'],)).fetchone()[0]
                    self.assertEqual(held, 0 if clean else 2)
                if not clean:
                    self.state.confirm_exit(task['id'], command['attempt_id'], instance_id='instance-one',
                                            epoch='epoch-one', evidence='oom-after-timeout-exit')
                    self.assertEqual(self.repository.get_task(task['id'])['error'], 'task_timed_out')
                    self.assertEqual(self.state.receive(event), 'stale')

    def test_seal_after_deadline_is_rejected_even_before_deadline_scanner(self):
        task, command = self.dispatched()
        with patch('mediacenter.task_state.now', return_value=self.deadline(command).isoformat()):
            with self.assertRaisesRegex(TaskStateError, 'seal_attempt_timed_out'):
                self.seal(command)
        self.assertEqual(self.count('task_seals'), 0)
        self.assertEqual(self.count('task_artifacts'), 0)

    def test_completed_or_historical_or_local_attempts_are_not_retroactively_expired(self):
        task, command = self.dispatched()
        manifest = self.seal(command)
        self.state.receive(self.event(command, kind='task.terminal', payload={'status':'succeeded','error_code':None,'manifest':manifest}))
        self.assertEqual(self.state.expire_due_tasks(at=self.deadline(command)), [])
        self.assertEqual(self.repository.get_task(task['id'])['status'], 'succeeded')
        self.state.confirm_exit(task['id'], command['attempt_id'], instance_id='instance-one', epoch='epoch-one', evidence='completed-exit')
        historical, older = self.dispatched()
        with self.repository._connect() as db:
            db.execute('UPDATE task_attempts SET execution_deadline_at=NULL WHERE id=?', (older['attempt_id'],))
        local = self.state.accept(dict(self.request, model='local-edit'), scope='local', mode='local')[0]
        self.assertEqual(self.state.expire_due_tasks(at=datetime(2100,1,1,tzinfo=timezone.utc)), [])
        self.assertEqual(self.repository.get_task(historical['id'])['status'], 'assigned')
        self.assertEqual(self.repository.get_task(local['id'])['status'], 'running')

    def test_repeated_deadline_observation_does_not_write_database_or_audit(self):
        task, command = self.dispatched()
        at = self.deadline(command) - timedelta(seconds=1)
        before = self.repository.path.read_bytes()
        counts = {table:self.count(table) for table in ('audit_events','task_outbox','instance_cancel_deadlines')}
        for _ in range(30):
            self.assertEqual(self.state.expire_due_tasks(at=at), [])
        self.assertEqual(self.repository.path.read_bytes(), before)
        self.assertEqual(counts, {table:self.count(table) for table in counts})
        self.assertEqual(self.repository.get_task(task['id'])['status'], 'assigned')

    def test_publication_crossing_deadline_replays_canceled_without_authorizing_bytes(self):
        from tests.test_artifact_commit import ArtifactCommitTests
        fixture = ArtifactCommitTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        with fixture.repo._connect() as db:
            at = datetime.fromisoformat(db.execute('SELECT execution_deadline_at FROM task_attempts WHERE id=?',
                                                   (fixture.command['attempt_id'],)).fetchone()[0])
        with patch('mediacenter.task_state.now', return_value=at.isoformat()):
            with self.assertRaisesRegex(TaskStateError, 'seal_attempt_timed_out'):
                fixture.store.worker(fixture.event, fixture.outputs)
        with fixture.repo._connect() as db:
            publication = db.execute('SELECT phase,descriptor_json FROM artifact_publications').fetchone()
        self.assertEqual(publication['phase'], 'published')
        relative = json.loads(publication['descriptor_json'])['relative_path']
        self.assertTrue((fixture.store.root / relative).is_file())
        self.assertIsNone(fixture.repo.authorized_artifact(relative))
        fixture.tasks.expire_due_tasks(at=at)
        self.assertEqual(fixture.store.worker(fixture.event, fixture.outputs), 'canceled')
        self.assertIsNone(fixture.repo.authorized_artifact(relative))
        with self.assertRaisesRegex(TaskStateError, 'artifact_not_found'):
            fixture.store.authorize(relative)
        with fixture.repo._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM task_reservations WHERE kind='task' AND released=0").fetchone()[0], 2)
        fixture.tasks.confirm_exit(fixture.task['id'], fixture.command['attempt_id'],
            instance_id='instance-one', epoch='epoch-one', evidence='publication-deadline-exit')
        self.assertEqual(fixture.repo.get_task(fixture.task['id'])['error'], 'task_timed_out')
        self.assertFalse((fixture.store.root / relative).exists())
        self.assertEqual(list(fixture.store.root.glob('*.part')), [])
        self.assertTrue((fixture.directory / 'artifact.png').is_file())
        self.assertEqual(fixture.store.recover_cleanup(), {})

    def test_sqlite_durability_and_foreign_keys_are_explicit(self):
        with self.repository._connect() as db:
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_real_process_exit_at_transaction_boundaries_and_after_commit(self):
        child = r'''
import json, os, sys
from mediacenter.repository import Repository
from mediacenter.task_state import TaskState
data = json.loads(sys.argv[1])
repo = Repository(data["db"])
state = TaskState(repo, fault=lambda point: os._exit(73) if point == data["fault"] else None)
if data["action"] == "accept":
    state.accept(data["request"], scope="crash-admin", key="crash-once", binding=data["binding"])
elif data["action"] == "dispatch":
    from tests.test_resident_policy import fixture_capacity
    state.dispatch(data["task"], 1, "instance-one", "epoch-one", {"GPU-one":20,"GPU-two":20}, observe_capacity=fixture_capacity)
else:
    state.receive(data["event"])
os._exit(74)
'''
        data = {"db": str(self.repository.path), "request": self.request, "binding": self.binding}
        def exit_child(action, fault, expected, **values):
            completed = subprocess.run([sys.executable, "-B", "-c", child,
                json.dumps(dict(data, action=action, fault=fault, **values))],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20)
            self.assertEqual(completed.returncode, expected, completed.stderr)
            self.repository = Repository(self.repository.path)
            self.state = TaskState(self.repository)
            with self.repository._connect() as db:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        for point in ("accept.task", "accept.audit"):
            exit_child("accept", point, 73)
            self.assertEqual(self.count("tasks"), 0)
            self.assertEqual(self.count("audit_events"), 0)
        for _ in range(2):
            exit_child("accept", "after-commit", 74)
            self.assertEqual(self.count("tasks"), 1)
            self.assertEqual(self.count("audit_events"), 1)
        task = self.repository.list_tasks()[0]
        for point in ("dispatch.attempt", "dispatch.reservation", "dispatch.task", "dispatch.outbox"):
            exit_child("dispatch", point, 73, task=task["id"])
            self.assertEqual(self.repository.get_task(task["id"])["status"], "queued")
            for table in ("task_attempts", "task_reservations", "task_outbox"):
                self.assertEqual(self.count(table), 0)
        exit_child("dispatch", "after-commit", 74, task=task["id"])
        self.assertEqual(self.count("task_attempts"), 1)
        self.assertEqual(self.count("task_reservations"), 2)
        self.assertEqual(self.count("task_outbox"), 1)
        command = self.state.outbox()[0]
        manifest = self.seal(command)
        event = self.event(command, kind="task.terminal", payload={"status": "succeeded", "error_code": None, "manifest": manifest})
        for point in ("event.inbox", "event.manifest", "event.terminal", "event.receipt", "event.applied"):
            exit_child("terminal", point, 73, event=event)
            self.assertEqual(self.repository.get_task(task["id"])["status"], "assigned")
            self.assertEqual(self.count("task_inbox"), 0)
            self.assertEqual(self.count("task_artifacts"), 0)
            self.assertEqual(self.count("task_outbox"), 1)
        for _ in range(2):
            exit_child("terminal", "after-commit", 74, event=event)
            self.assertEqual(self.repository.get_task(task["id"])["status"], "succeeded")
            self.assertEqual(self.count("task_inbox"), 1)
            self.assertEqual(self.count("task_artifacts"), 1)
            self.assertEqual(self.count("task_outbox"), 2)


if __name__ == "__main__":
    unittest.main()
