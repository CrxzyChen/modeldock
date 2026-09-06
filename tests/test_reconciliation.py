from __future__ import annotations

import json
import hashlib
import http.client
import os
import copy
import multiprocessing
import threading
import time
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mediacenter.adapter import AdapterFactory
from mediacenter.capabilities import worker_capability_for
from mediacenter.gpu_scheduler import GPULeaseScheduler
from mediacenter.instance_policy import InstancePolicy
from mediacenter.reconciler import Reconciler
from mediacenter.repository import Repository
from mediacenter.runtime_controller import RuntimeController
from mediacenter.task_state import digest
from mediacenter.task_state import TaskStateError
from mediacenter.transport import Identity, Delivery, TransportError
from mediacenter.worker_journal import WorkerJournal, CommandAuthority
from mediacenter.worker_runtime import WorkerRuntime
from tests.fake_model_adapter import FakeAdapter
from tests.test_gpu_reservations import Inventory
from tests.test_resident_policy import seed_deployment, policy_value, package_identity
from tests.test_runtime_controller import fixture_policy, FixtureEngine, FixtureObserver
from tests.test_worker_runtime import child_exit_watcher

GPU = "GPU-00000000-0000-0000-0000-000000000001"


class CountingAdapter(FakeAdapter):
    def __init__(self, loads, executions, reset_fail=False):
        super().__init__(counter=executions, execute_fail=True, reset_fail=reset_fail)
        self.loads = loads
    def load(self, binding):
        with self.loads.get_lock(): self.loads.value += 1
        super().load(binding)


class BlockingLoadAdapter(CountingAdapter):
    def __init__(self, loads, executions, load_entered, load_release, reset_fail=False):
        super().__init__(loads, executions, reset_fail)
        self.load_entered, self.load_release = load_entered, load_release

    def load(self, binding):
        super().load(binding)
        self.load_entered.set()
        if not self.load_release.wait(timeout=60):
            raise RuntimeError('fixture_load_barrier_timeout')


class WritingAdapter(CountingAdapter):
    def __init__(self,loads,executions,outputs,reset_fail=False):
        super().__init__(loads,executions,reset_fail)
        self.outputs=Path(outputs)
    def execute(self,request,progress,cancellation):
        from tests.test_artifact_commit import PNG
        with self.counter.get_lock():self.counter.value+=1
        folder=self.outputs/'tasks'/request['task_id']/request['attempt_id'];folder.mkdir(parents=True)
        sha=hashlib.sha256(PNG).hexdigest()
        manifest={'asset_id':'art_'+request['attempt_id'],'revision':'v1','sha256':sha}
        description={'schema':1,**{k:request[k] for k in ('task_id','attempt_id','instance_id','worker_epoch')},
            'command_message_id':request['message_id'],'command_digest':digest(request),**manifest,'byte_size':len(PNG),'media_type':'image/png'}
        for name,data in (('artifact.png',PNG),('manifest.json',json.dumps(description).encode())):
            with (folder/name).open('xb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        return manifest


class Bridge:
    """In-memory transport boundary, explicitly not an actual Redis ACL test."""
    def __init__(self, fixture):
        self.fixture = fixture
        self.identity = fixture.identity
        self.events, self.acks, self.quarantined = [], [], []
        self.offline = False
    def provision(self):
        if self.offline: raise TransportError("fixture_disconnected")
    def telemetry(self, kind):
        if self.offline: raise TransportError("fixture_disconnected")
        return self.fixture.worker.heartbeat_once()
    def publish(self, message):
        if self.offline: raise TransportError("fixture_disconnected")
        runtime = self.fixture.worker
        runtime.handle(message, authority=self.fixture.command_authority if message["type"] in {"task.execute", "model.load"} else None)
        runtime.execute_once()
        return message["message_id"]
    def promote_events(self):
        if self.offline: raise TransportError("fixture_disconnected")
        pending = self.fixture.journal.pending_events(limit=100)["items"]
        ids = {item.entry_id for item in self.events}
        self.events.extend(Delivery("events", msg["message_id"], msg) for msg in pending if msg["message_id"] not in ids)
    def read(self, lane, **kwargs): return list(self.events)
    def ack(self, delivery):
        self.acks.append(delivery.entry_id)
        self.events = [item for item in self.events if item.entry_id != delivery.entry_id]
    def quarantine(self, delivery, code):
        self.quarantined.append((delivery.entry_id,code))
        self.ack(delivery)
    def close(self, timeout=3): return True


class FixturePackageProvider:
    def __init__(self, package):
        self.package=package
        self.ensure_calls=0
        self.for_epoch_calls=0
    def ensure(self, instance):
        self.ensure_calls+=1
        if instance != self.package.instance_id: raise TaskStateError('runtime_package_unavailable')
        return self.package
    def for_epoch(self, instance, epoch):
        self.for_epoch_calls+=1
        if instance != self.package.instance_id or epoch != self.package.epoch:
            raise TaskStateError('runtime_package_unavailable')
        return self.package
    def for_epoch_removal(self, instance, epoch):
        return self.for_epoch(instance, epoch)
    def validate_claim(self, _db, record_id):
        identity=self.package.verify()
        return identity if record_id == identity['runtime_record_id'] else None


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.repo = Repository(self.root / "state.db")
        self.authority = InstancePolicy(self.repo)
        self.binding = seed_deployment(self.repo)
        self.value = policy_value(self.binding, (GPU,))
        self.authority.configure("instance-one", self.value)
        self.authority.desire("instance-one", "loaded")
        self.inventory = Inventory()
        self.inventory.gpu_snapshot = lambda **_: {"available":True,"items":[{"uuid":GPU,"index":0,"memory_total_mib":49140,"memory_used_mib":0,"processes":[]}]}
        self.scheduler = GPULeaseScheduler((0,), authority=self.authority,gpu_uuids=(GPU,),hardware=self.inventory,
            memory_provider=lambda:SimpleNamespace(available_bytes=64*1024**3,pressure_avg60=0))
        self.policy = replace(fixture_policy(self.root, self.repo.path),gpu_uuids=(GPU,))
        self.engine, self.observer = FixtureEngine(), FixtureObserver()
        self.engine.config=SimpleNamespace(**dict(vars(self.engine.config),socket_path=self.policy.engine_socket))
        self.controller=RuntimeController(self.repo,self.engine,self.observer,self.policy)
        self.identity=Identity("mediacenter","instance-one","epoch-one")
        capability=worker_capability_for(self.binding["model_key"])
        self.worker_binding={key:self.binding[key] for key in ("model_key","recipe_revision","model_asset_id","model_asset_revision")}
        self.worker_binding.update(image_digest="sha256:"+"a"*64,gpu_uuids=[GPU],capability_digest=digest(capability))
        self.journal=WorkerJournal(self.root/"journal"/"worker.db","instance-one",{capability["model_key"]:capability})
        self.command_authority=CommandAuthority(self.identity,digest(self.worker_binding),"fixture-private-command-channel")
        context=multiprocessing.get_context("spawn")
        self.loads,self.executions=context.Value("i",0),context.Value("i",0)
        self.worker=None
        self.reset_fail=False
        self.successful=False
        self.load_barriers=None
        self.outputs=Path(next(grant.source for grant in self.policy.mounts if grant.role=='outputs'))
        self.bridge=Bridge(self)
        self.package=SimpleNamespace(instance_id="instance-one",epoch="epoch-one",record_id="record-epoch-one",binding=self.binding,
            recovery_capability=lambda:worker_capability_for(self.binding['model_key']),
            verify=lambda:package_identity(self.value),
            controller=lambda _:self.controller,transport=lambda _:self.bridge,journal_path=lambda:self.journal.path,
            policy=self.policy,outputs_path=lambda:self.outputs)
        self.provider=FixturePackageProvider(self.package)
        self.reconciler=Reconciler(self.repo,None,self.scheduler,package_provider=self.provider,artifact_root=self.root/'sealed')
        self.engine.start_hook=self.start_worker
        self.engine.stop_hook=self.stop_worker
    def start_worker(self):
        options={"loads":self.loads,"executions":self.executions,"reset_fail":self.reset_fail}
        if self.successful:options['outputs']=str(self.outputs)
        adapter = "WritingAdapter" if self.successful else "CountingAdapter"
        if self.load_barriers:
            adapter = 'BlockingLoadAdapter'
            options.update(load_entered=self.load_barriers[0], load_release=self.load_barriers[1])
        self.worker=WorkerRuntime(self.identity,self.worker_binding,self.journal,
            AdapterFactory("tests.test_reconciliation",adapter,options),
            command_authority=self.command_authority)
        self.worker.admit(evidence="fixture-owned-epoch",recovery_complete=True,clock_trusted=True)
    def stop_worker(self):
        if self.worker and self.worker._process:
            pid=self.worker._process.pid
            wait=child_exit_watcher(pid)
            self.worker.close()
            exited=wait()
            self.assertTrue(exited)
            print(json.dumps({"fixture":"mc032-resident-child","pid":pid,"observed_exit":exited}),flush=True)
    def tearDown(self):
        self.stop_worker()
        self.reconciler.stop()
        self.temp.cleanup()
    def task(self):
        return self.authority.tasks.accept({"service":"image","model":"instance-one","prompt":"fixture","options":{},"inputs":[]},scope="test",binding=self.binding)[0]
    def tick(self):
        self.reconciler.tick()
        self.assertEqual(self.reconciler.last_errors,{})

    def pending_model_load(self):
        def defer_load(message):
            authority = self.command_authority if message['type'] in {'model.load', 'task.execute'} else None
            self.worker.handle(message, authority=authority)
            # Receipt delivery must not run the queued inference work either.
            return message['message_id']
        with patch.object(self.bridge, 'publish', side_effect=defer_load):
            self.tick()
            self.tick()  # Deliver the newly queued load without executing it.
        row = self.authority.get('instance-one')
        self.assertEqual(row['claim']['state'], 'loading')
        self.assertEqual(self.loads.value, 0)
        return row

    def test_public_stop_fences_pending_load_despite_fresh_busy_heartbeat(self):
        loading = self.pending_model_load()
        heartbeat = self.worker._telemetry('telemetry.heartbeat', {'state': 'busy', 'uptime_seconds': 1})
        self.assertTrue(self.authority.observe_heartbeat('instance-one', 'epoch-one', heartbeat))
        stopped = self.authority.set_service('instance-one', False, expected_version=loading['version'])
        self.assertEqual(stopped['claim']['stop_reason'], 'operator_stop')
        self.assertFalse(self.repo.get_deployment('instance-one')['enabled'])
        self.assertEqual(stopped['desired_state'], 'unloaded')
        with self.repo._connect() as db:
            self.assertEqual(db.execute("SELECT SUM(mib) FROM task_reservations WHERE released=0").fetchone()[0], 10)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM model_operations WHERE action='unload'").fetchone()[0], 0)
        fresh = self.worker._telemetry('telemetry.heartbeat', {'state': 'busy', 'uptime_seconds': 2})
        self.assertFalse(self.authority.observe_heartbeat('instance-one', 'epoch-one', fresh))
        # The durable fence survives a new control-plane object and Redis loss.
        self.bridge.offline = True
        restarted = Reconciler(Repository(self.repo.path), None, self.scheduler, package_provider=self.provider)
        self.observer.populated = 1
        restarted.cancel_tick()
        with self.repo._connect() as db:
            self.assertEqual(db.execute("SELECT SUM(mib) FROM task_reservations WHERE released=0").fetchone()[0], 10)
        self.observer.populated = 0
        restarted.cancel_tick()
        self.assertIsNone(self.authority.get('instance-one')['claim'])
        with self.repo._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM task_reservations WHERE released=0").fetchone()[0], 0)
        self.assertEqual(len([call for call in self.engine.calls if call[0] == 'stop']), 1)
        self.assertEqual(self.loads.value, 0)

    def test_http_stop_interrupts_executing_load_without_waiting_for_load_completion(self):
        from mediacenter.server import Handler, MediaCenterHTTPServer
        from mediacenter.service_center import ServiceCenter

        context = multiprocessing.get_context('spawn')
        entered, release = context.Event(), context.Event()
        self.load_barriers = (entered, release)
        loading = self.pending_model_load()
        execution_results, execution_errors = [], []
        def execute():
            try:
                execution_results.append(self.worker.execute_once())
            except Exception as error:
                execution_errors.append(error)
        execution = threading.Thread(target=execute, daemon=True)
        # Use the supervisor's owned-thread shutdown, including its real child
        # join/termination; the load barrier itself is never released to stop it.
        self.worker._threads.append(execution)
        execution.start()
        self.assertTrue(entered.wait(timeout=15))
        child_pid = self.worker._process.pid
        self.assertTrue(self.worker._process.is_alive())
        heartbeat = self.worker.heartbeat_once()
        self.assertEqual(heartbeat['payload']['state'], 'busy')
        self.assertTrue(self.authority.observe_heartbeat('instance-one', 'epoch-one', heartbeat))

        # Exercise the actual HTTP handler and ServiceCenter.stop_deployment;
        # only unrelated registry/status projection and transport are fixtures.
        center = ServiceCenter.__new__(ServiceCenter)
        center.runtime, center.repository = self.reconciler, self.repo
        center.registry = SimpleNamespace(refresh=lambda: None)
        center._runtime_status = self.authority.get
        center.stop_deployment_worker = lambda: True
        server = MediaCenterHTTPServer(('127.0.0.1', 0), Handler)
        server.api_key, server.center = 'fixture-key', center
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        def request(version, authenticated=True):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            try:
                headers = {'Content-Type': 'application/json'}
                if authenticated:
                    headers['X-API-Key'] = 'fixture-key'
                connection.request('POST', '/api/v1/deployments/instance-one/stop',
                                   json.dumps({'version': version}), headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        try:
            self.assertEqual(request(loading['version'], authenticated=False)[0], 401)
            self.assertEqual(request(loading['version'] - 1)[0], 409)
            self.assertIsNone(self.authority.get('instance-one')['claim']['stop_reason'])
            status, stopped = request(loading['version'])
            self.assertEqual(status, 202)
            self.assertEqual(stopped['claim']['stop_reason'], 'operator_stop')
            self.assertFalse(self.repo.get_deployment('instance-one')['enabled'])
            self.assertTrue(execution.is_alive())
            self.assertTrue(self.worker._process.is_alive())
            self.assertFalse(release.is_set())

            # A disconnected broker cannot prevent the durable stop from being
            # reconciled. A still-populated cgroup cannot release its GPU lease.
            self.bridge.offline = True
            self.observer.populated = 1
            self.reconciler.cancel_tick()
            self.assertFalse(execution.is_alive())
            self.assertEqual(execution_errors, [])
            self.assertEqual(execution_results, [False])
            self.assertIsNone(self.worker._process)
            self.assertFalse(release.is_set())
            with self.repo._connect() as db:
                self.assertEqual(db.execute('SELECT SUM(mib) FROM task_reservations WHERE released=0').fetchone()[0], 10)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM model_operations WHERE action='unload'").fetchone()[0], 0)
            self.observer.populated = 0
            self.reconciler.cancel_tick()
            self.assertIsNone(self.authority.get('instance-one')['claim'])
            with self.repo._connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM task_reservations WHERE released=0').fetchone()[0], 0)
            self.assertEqual(len([call for call in self.engine.calls if call[0] == 'stop']), 1)
            self.assertEqual(self.loads.value, 1)
            self.assertEqual(self.executions.value, 0)
            print(json.dumps({'fixture': 'http-stop-blocked-model-load', 'worker_pid': child_pid,
                              'load_entered': True, 'load_barrier_released': False,
                              'http_status': status, 'child_exit_observed': True,
                              'real_gpu_or_docker': False}), flush=True)
        finally:
            server.shutdown()
            server.server_close()
            serving.join(timeout=2)

    def test_public_stop_loading_is_atomic_and_versioned(self):
        loading = self.pending_model_load()
        with self.assertRaisesRegex(TaskStateError, 'instance_version_conflict'):
            self.authority.set_service('instance-one', False, expected_version=loading['version'] - 1)
        self.assertIsNone(self.authority.get('instance-one')['claim']['stop_reason'])
        self.authority.fault = lambda point: (_ for _ in ()).throw(RuntimeError(point)) if point == 'service.stop_loading' else None
        with self.assertRaisesRegex(RuntimeError, 'service.stop_loading'):
            self.authority.set_service('instance-one', False, expected_version=loading['version'])
        current = self.authority.get('instance-one')
        self.assertEqual(current['version'], loading['version'])
        self.assertIsNone(current['claim']['stop_reason'])
        self.assertTrue(self.repo.get_deployment('instance-one')['enabled'])

    def test_public_stop_before_load_prevents_new_load(self):
        value = dict(self.value, residency='on_demand')
        self.authority.configure('instance-one', value,
                                 expected_version=self.authority.get('instance-one')['version'])
        self.tick()
        current = self.authority.get('instance-one')
        self.authority.set_service('instance-one', False, expected_version=current['version'])
        with self.assertRaisesRegex(TaskStateError, 'backend_claim_required'):
            self.scheduler.load_model('instance-one')
        with self.repo._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM model_operations').fetchone()[0], 0)

    def test_public_stop_retains_dispatched_task_drain(self):
        self.tick(); self.tick()
        task = self.task()
        dispatched = self.scheduler.dispatch(task)
        self.assertIsNotNone(dispatched)
        current = self.authority.get('instance-one')
        stopped = self.authority.set_service('instance-one', False, expected_version=current['version'])
        self.assertIsNone(stopped['claim']['stop_reason'])
        self.assertFalse(self.repo.get_deployment('instance-one')['enabled'])
        self.reconciler.cancel_tick()
        self.assertFalse(any(call[0] == 'stop' for call in self.engine.calls))

    def test_public_stop_late_load_success_cannot_clear_fence(self):
        from tests.test_resident_policy import model_event
        loading = self.pending_model_load()
        with self.repo._connect() as db:
            row = db.execute("SELECT operation_id,state,next_event_seq FROM model_operations WHERE action='load'").fetchone()
            operation = row['operation_id']
            self.assertEqual((row['state'], row['next_event_seq']), ('accepted', 2))
        command = self.authority.command(operation)
        stopped = self.authority.set_service('instance-one', False, expected_version=loading['version'])
        # The actual Worker accepted event was already consumed by tick two.
        self.authority.receive(model_event(command, 2, 'model.terminal'))
        current = self.authority.get('instance-one')
        self.assertEqual(current['claim']['stop_reason'], 'operator_stop')
        self.assertEqual(current['desired_state'], 'unloaded')
        repeated = self.authority.set_service('instance-one', False, expected_version=stopped['version'])
        self.assertEqual(repeated['version'], stopped['version'])
        self.assertFalse(self.repo.get_deployment('instance-one')['enabled'])
        with self.repo._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM model_operations').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM task_reservations WHERE released=0').fetchone()[0], 1)

    def test_public_stop_loading_keeps_pending_configuration_intent(self):
        loading = self.pending_model_load()
        changed = dict(self.value, sharing_mode='exclusive')
        pending = self.authority.configure('instance-one', changed, expected_version=loading['version'])
        stopped = self.authority.set_service('instance-one', False, expected_version=pending['version'])
        self.assertEqual(stopped['configuration_state'], pending['configuration_state'])
        self.assertEqual(stopped['pending_policy'], pending['pending_policy'])
        self.assertFalse(stopped['resume_after_apply'])
        self.assertIsNone(stopped['claim']['stop_reason'])

    def test_model_unload_keeps_started_container_online(self):
        self.tick(); self.tick()
        self.assertEqual(self.authority.get("instance-one")["status"],"loaded")
        pid=self.worker._process.pid
        for _ in range(2):
            task=self.task()
            self.tick();self.tick()
            self.assertEqual(self.repo.get_task(task["id"])["status"],"failed")
            self.assertEqual(self.worker._process.pid,pid)
            with self.repo._connect() as db:
                self.assertEqual(db.execute("SELECT SUM(mib) FROM task_reservations WHERE released=0").fetchone()[0],10)
        self.assertEqual((self.loads.value,self.executions.value),(1,2))
        self.assertFalse(self.bridge.quarantined)
        self.reconciler.request_unload('instance-one')
        self.tick();self.tick()
        current = self.authority.get('instance-one')
        self.assertEqual(current['claim']['state'],'online_unloaded')
        intent = json.loads(current['claim']['execution_json'])['owned_execution']['intent_id']
        self.assertEqual(self.controller.get(intent)['state'],'running')
        with self.repo._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM task_reservations WHERE released=0').fetchone()[0],0)

    def test_on_demand_starts_container_without_loading_until_work_arrives(self):
        value = copy.deepcopy(self.value)
        value["residency"] = "on_demand"
        configured = self.authority.configure(
            "instance-one", value,
            expected_version=self.authority.get("instance-one")["version"],
        )
        self.assertEqual(configured["desired_state"], "unloaded")

        self.tick()
        online = self.authority.get("instance-one")
        self.assertEqual(online["claim"]["state"], "online_unloaded")
        self.assertEqual(self.loads.value, 0)
        container_pid = self.worker._process.pid

        task = self.task()
        for _ in range(6):
            self.tick()
        settled = self.authority.get("instance-one")
        self.assertEqual(self.repo.get_task(task["id"])["status"], "failed")
        self.assertEqual(self.loads.value, 1)
        self.assertEqual(settled["claim"]["state"], "online_unloaded")
        self.assertEqual(self.worker._process.pid, container_pid)
        with self.repo._connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM task_reservations WHERE released=0"
            ).fetchone()[0], 0)

    def test_on_demand_capacity_wait_is_visible_while_container_stays_online(self):
        value = copy.deepcopy(self.value)
        value["residency"] = "on_demand"
        self.authority.configure(
            "instance-one", value,
            expected_version=self.authority.get("instance-one")["version"],
        )
        self.tick()
        container_pid = self.worker._process.pid
        self.inventory.gpu_snapshot = lambda **_: {
            "available": True,
            "items": [{
                "uuid": GPU, "index": 0, "memory_total_mib": 49140,
                "memory_used_mib": 49135, "processes": [],
            }],
        }
        task = self.task()
        self.reconciler.tick()
        waiting = self.authority.get("instance-one")
        self.assertEqual(self.reconciler.last_errors["instance-one"],
                         "gpu_capacity_unavailable")
        self.assertEqual(waiting["error_code"], "gpu_capacity_unavailable")
        self.assertEqual(waiting["claim"]["state"], "online_unloaded")
        self.assertEqual(self.worker._process.pid, container_pid)
        self.assertEqual(self.repo.get_task(task["id"])["status"], "queued")

    def test_idle_keeps_loaded_model_until_timeout_then_only_unloads_model(self):
        value = copy.deepcopy(self.value)
        value.update(residency="idle", idle_seconds=60)
        self.authority.configure(
            "instance-one", value,
            expected_version=self.authority.get("instance-one")["version"],
        )
        self.tick()
        container_pid = self.worker._process.pid
        self.assertEqual(self.authority.get("instance-one")["claim"]["state"],
                         "online_unloaded")

        task = self.task()
        for _ in range(3):
            self.tick()
        warm = self.authority.get("instance-one")
        self.assertEqual(self.repo.get_task(task["id"])["status"], "failed")
        self.assertEqual(warm["claim"]["state"], "loaded")
        self.assertEqual(warm["desired_state"], "loaded")

        with self.repo._connect() as db:
            db.execute(
                "UPDATE instance_policies SET last_activity='2000-01-01T00:00:00+00:00' "
                "WHERE instance_id='instance-one'"
            )
        self.tick()
        self.tick()
        self.tick()
        cooled = self.authority.get("instance-one")
        self.assertEqual(cooled["claim"]["state"], "online_unloaded")
        self.assertEqual(self.worker._process.pid, container_pid)

    def test_resident_loads_model_as_part_of_started_container_readiness(self):
        self.tick(); self.tick()
        ready = self.authority.get("instance-one")
        self.assertEqual(ready["policy"]["residency"], "resident")
        self.assertEqual(ready["claim"]["state"], "loaded")
        self.assertEqual(self.loads.value, 1)
        with self.repo._connect() as db:
            self.assertEqual(db.execute(
                "SELECT SUM(mib) FROM task_reservations "
                "WHERE instance_id='instance-one' AND kind='base' AND released=0"
            ).fetchone()[0], 10)

    def test_immutable_epoch_package_is_reused_between_reconciliation_ticks(self):
        self.tick()
        self.assertEqual(self.provider.ensure_calls, 1)
        self.assertEqual(self.provider.for_epoch_calls, 0)

    def test_stable_reconciliation_does_not_redeliver_or_write_database(self):
        for _ in range(5):
            self.tick()
        with self.repo._connect() as watcher:
            data_version = watcher.execute('PRAGMA data_version').fetchone()[0]
            delivered = watcher.execute(
                'SELECT message_id,delivered_at FROM task_outbox ORDER BY sequence'
            ).fetchall()
            self.tick()
            for _ in range(10):
                self.reconciler.cleanup_tick()
            self.assertEqual(
                watcher.execute('PRAGMA data_version').fetchone()[0], data_version
            )
            self.assertEqual(
                watcher.execute(
                    'SELECT message_id,delivered_at FROM task_outbox ORDER BY sequence'
                ).fetchall(),
                delivered,
            )

    def test_immutable_epoch_model_files_are_validated_once_but_package_each_tick(self):
        model_root = Path(next(grant.source for grant in self.policy.mounts
                               if grant.role == "models"))
        calls = []
        self.reconciler.model_assets = SimpleNamespace(
            readonly_asset_path=lambda asset, revision: (
                calls.append((asset, revision)) or model_root))
        verifies = []
        original_verify = self.package.verify
        self.package.verify = lambda: (verifies.append(True) or original_verify())
        self.tick(); self.tick(); self.tick()
        self.assertEqual(calls, [(self.binding["model_asset_id"],
                                  self.binding["model_asset_revision"])])
        # The initial claim validation may also verify the package.  What
        # matters here is that every later reconciliation tick still verifies
        # the package identity while the heavyweight asset walk stays cached.
        verified_before = len(verifies)
        self.tick(); self.tick()
        self.assertEqual(len(verifies) - verified_before, 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.provider.ensure_calls, 1)
        self.assertEqual(self.provider.for_epoch_calls, 0)

    def test_capacity_failure_keeps_container_online_with_precise_operator_error(self):
        self.inventory.gpu_snapshot = lambda **_: {"available": True, "items": [{
            "uuid": GPU, "index": 0, "memory_total_mib": 49140,
            "memory_used_mib": 48000, "processes": []}]}
        self.reconciler.tick()
        row = self.authority.get("instance-one")
        self.assertEqual(row["error_code"], "gpu_capacity_unavailable")
        self.assertEqual(row["claim"]["state"], "online_unloaded")
        self.assertIsNotNone(self.worker._process.pid)
        with self.repo._connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM task_reservations WHERE released=0"
            ).fetchone()[0], 0)

    def test_real_worker_success_is_sealed_by_normal_reconciler_without_precreated_seal(self):
        from tests.test_artifact_commit import PNG
        self.successful=True;self.tick();pid=self.worker._process.pid
        for _ in range(2):
            task=self.task();self.tick();self.tick()
            final=self.repo.get_task(task['id']);self.assertEqual(final['status'],'succeeded')
            with self.reconciler.artifacts.authorize(final['output']['artifact_path']) as stream:self.assertEqual(stream.stream.read(),PNG)
            self.assertEqual(self.worker._process.pid,pid)
        self.assertEqual((self.loads.value,self.executions.value),(1,2))
        self.assertEqual(self.reconciler.artifact_errors,{})
        with self.repo._connect() as db:self.assertEqual(db.execute('SELECT SUM(mib) FROM task_reservations WHERE released=0').fetchone()[0],10)

    def test_acked_success_before_publication_is_discovered_after_restart_offline(self):
        self.successful=True;self.tick();task=self.task();self.tick()
        with patch.object(self.reconciler.artifacts,'worker',side_effect=SystemExit('crash before intent')):
            with self.assertRaises(SystemExit):self.reconciler.tick()
        with self.repo._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM artifact_publications').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM task_inbox WHERE result='pending'").fetchone()[0],1)
        self.assertTrue(self.bridge.acks);self.bridge.offline=True
        restarted=Reconciler(Repository(self.repo.path),None,self.scheduler,package_provider=self.provider,artifact_root=self.root/'sealed')
        restarted.seal_pending()
        self.assertEqual(self.repo.get_task(task['id'])['status'],'succeeded')
        self.assertEqual(self.executions.value,1)

    def test_committed_cleanup_recovers_in_background_tick_with_redis_offline(self):
        from tests.test_artifact_commit import PNG
        self.successful=True; self.tick(); task=self.task(); self.tick()
        self.reconciler.artifacts.fault = lambda p: (_ for _ in ()).throw(SystemExit('publication cut')) if p == 'publication.committed' else None
        with self.assertRaises(SystemExit):
            self.reconciler.tick()
        result = self.repo.get_task(task['id'])
        self.assertEqual(result['status'], 'succeeded')
        self.assertTrue(list((self.root/'sealed').glob('*.part')))
        self.bridge.offline = True
        restarted = Reconciler(Repository(self.repo.path), None, self.scheduler,
                               package_provider=self.provider, artifact_root=self.root/'sealed')
        restarted.cleanup_tick()
        self.assertEqual(list((self.root/'sealed').glob('*.part')), [])
        with restarted.artifacts.authorize(result['output']['artifact_path']) as stream:
            self.assertEqual(stream.stream.read(), PNG)
        self.assertEqual(self.executions.value, 1)

    def test_linked_candidate_cancel_recovers_in_background_tick_without_package_lookup(self):
        self.successful=True; self.tick(); task=self.task(); self.tick()
        self.reconciler.artifacts.fault = lambda p: (_ for _ in ()).throw(SystemExit('publication cut')) if p == 'publication.linked' else None
        with self.assertRaises(SystemExit):
            self.reconciler.tick()
        with self.repo._connect() as db:
            row = db.execute('SELECT * FROM artifact_publications WHERE task_id=?', (task['id'],)).fetchone()
        final = self.root/'sealed'/json.loads(row['descriptor_json'])['relative_path']
        self.assertTrue(final.exists())
        self.authority.tasks.cancel(task['id'])
        self.bridge.offline = True
        restarted = Reconciler(Repository(self.repo.path), None, self.scheduler, artifact_root=self.root/'sealed')
        restarted.cleanup_tick()
        self.assertFalse(final.exists())
        self.assertEqual(list((self.root/'sealed').glob('*.part')), [])
        self.assertEqual(self.executions.value, 1)
        self.assertTrue((self.outputs/'tasks'/task['id']/row['attempt_id']/'artifact.png').is_file())

    def seed_committed_cleanup_backlog(self):
        self.successful=True; self.tick(); task=self.task(); self.tick()
        self.reconciler.artifacts.fault = lambda p: (_ for _ in ()).throw(SystemExit('publication cut')) if p == 'publication.committed' else None
        try:
            with self.assertRaises(SystemExit): self.reconciler.tick()
        finally:
            self.reconciler.artifacts.fault = lambda _:None
        with self.repo._connect() as db:
            row=db.execute('SELECT * FROM artifact_publications WHERE task_id=?',(task['id'],)).fetchone()
        return self.root/'sealed'/row['temporary_path']

    def cleanup_barrier(self, entered, release):
        original=self.reconciler.artifacts.check_boundary
        def blocked():
            if threading.current_thread().name == 'runtime-artifact-recovery' and not release.is_set():
                entered.set()
                if not release.wait(15): raise RuntimeError('cleanup fixture barrier timeout')
            return original()
        return blocked

    def wait_until(self, predicate, timeout=8):
        deadline=time.monotonic()+timeout
        while not predicate():
            self.assertLess(time.monotonic(),deadline,'background progress deadline exceeded')
            threading.Event().wait(0.01)

    def test_blocked_historical_cleanup_does_not_block_dispatch_or_event_consumption(self):
        pending=self.seed_committed_cleanup_backlog()
        entered,release=threading.Event(),threading.Event()
        self.reconciler.poll_seconds=0.01
        with patch.object(self.reconciler.artifacts,'check_boundary',side_effect=self.cleanup_barrier(entered,release)):
            try:
                self.reconciler.start()
                self.assertTrue(entered.wait(8))
                threads=tuple(self.reconciler._threads)
                for _ in range(5): self.reconciler.start()
                self.assertEqual(tuple(self.reconciler._threads),threads)
                self.assertEqual(len(threads),3)
                self.assertEqual(sum(t.name=='runtime-artifact-recovery' for t in threads),1)
                acks=len(self.bridge.acks)
                task=self.task()
                self.wait_until(lambda:self.repo.get_task(task['id'])['status']=='succeeded')
                self.assertFalse(release.is_set())
                self.assertTrue(pending.is_file())
                self.assertGreater(len(self.bridge.acks),acks)
                self.assertEqual(self.executions.value,2)
                with self.repo._connect() as db:
                    self.assertEqual(db.execute('SELECT exit_confirmed FROM task_attempts WHERE task_id=?',(task['id'],)).fetchone()[0],1)
            finally:
                release.set(); self.reconciler.stop()
        self.assertFalse(pending.exists())

    def test_deadline_bookkeeping_progresses_while_historical_cleanup_is_blocked(self):
        pending=self.seed_committed_cleanup_backlog()
        task=self.task(); self.tick()
        self.assertEqual(self.repo.get_task(task['id'])['status'],'assigned')
        self.bridge.offline=True
        entered,release=threading.Event(),threading.Event()
        self.reconciler.poll_seconds=0.01
        with patch.object(self.reconciler.artifacts,'check_boundary',side_effect=self.cleanup_barrier(entered,release)):
            try:
                self.reconciler.start(); self.assertTrue(entered.wait(8))
                with self.repo._connect() as db:
                    db.execute("UPDATE task_attempts SET execution_deadline_at='2000-01-01T00:00:00+00:00' WHERE task_id=?",(task['id'],))
                self.wait_until(lambda:self.repo.get_task(task['id'])['error']=='task_timed_out')
                self.assertTrue(pending.is_file()); self.assertFalse(release.is_set())
                with self.repo._connect() as db:
                    self.assertEqual(db.execute('SELECT exit_confirmed FROM task_attempts WHERE task_id=?',(task['id'],)).fetchone()[0],0)
                    self.assertGreater(db.execute("SELECT COUNT(*) FROM task_reservations WHERE kind='task' AND released=0").fetchone()[0],0)
            finally:
                release.set(); self.reconciler.stop()

    def test_stop_retains_dependencies_until_inflight_cleanup_has_actually_exited(self):
        pending=self.seed_committed_cleanup_backlog()
        entered,release=threading.Event(),threading.Event()
        closes=[]
        self.provider.publisher=SimpleNamespace(close=lambda:closes.append('publisher'))
        self.bridge.close=lambda:closes.append('transport')
        self.reconciler.poll_seconds=0.01
        with patch.object(self.reconciler.artifacts,'check_boundary',side_effect=self.cleanup_barrier(entered,release)):
            try:
                self.reconciler.start(); self.assertTrue(entered.wait(8))
                owned_threads=tuple(self.reconciler._threads)
                with self.assertRaisesRegex(RuntimeError,'reconciler_exit_unconfirmed'):
                    self.reconciler.stop(timeout=0.05)
                self.assertTrue(any(t.is_alive() for t in owned_threads))
                self.assertTrue(self.reconciler._stop.is_set())
                self.assertEqual(closes,[])
                self.assertTrue(pending.is_file())
                self.reconciler.start()
                self.assertEqual(tuple(self.reconciler._threads),owned_threads)
            finally:
                release.set(); self.reconciler.stop()
        self.assertFalse(any(t.is_alive() for t in owned_threads))
        self.assertCountEqual(closes,['transport','publisher'])
        self.assertFalse(pending.exists())

    def test_partial_thread_start_failure_remains_stoppable_and_restartable(self):
        original=threading.Thread.start
        tick_called,cancel_called=threading.Event(),threading.Event()
        thread_errors=[]
        def tick_probe():
            tick_called.set()
        def cancel_probe():
            cancel_called.set()
        def fail_recovery(thread):
            if thread.name=='runtime-artifact-recovery':
                self.assertTrue(tick_called.wait(2))
                self.assertTrue(cancel_called.wait(2))
                raise RuntimeError('fixture thread allocation failed')
            return original(thread)
        with patch.object(self.reconciler,'tick',new=tick_probe), \
                patch.object(self.reconciler,'cancel_tick',new=cancel_probe), \
                patch.object(threading,'excepthook',new=thread_errors.append):
            with patch.object(threading.Thread,'start',new=fail_recovery):
                with self.assertRaisesRegex(RuntimeError,'fixture thread allocation failed'):
                    self.reconciler.start()
            self.assertEqual(len(self.reconciler._threads),2)
            self.assertTrue(self.reconciler._stop.is_set())
            started=tuple(self.reconciler._threads)
            self.reconciler.stop()
            self.assertFalse(any(t.is_alive() for t in started))
            self.assertEqual(self.reconciler._threads,[])
            tick_called.clear();cancel_called.clear()
            try:
                self.reconciler.start()
                self.assertTrue(tick_called.wait(2))
                self.assertTrue(cancel_called.wait(2))
                self.assertEqual(len(self.reconciler._threads),3)
                self.assertTrue(all(t.is_alive() for t in self.reconciler._threads))
            finally:
                self.reconciler.stop()
            self.assertEqual(thread_errors,[])
            self.assertEqual(self.reconciler.last_errors,{})

    def test_old_journal_success_waits_for_scratch_then_seals_original_attempt(self):
        self.successful=True;self.tick();task=self.task();self.tick()
        command=next(msg for msg in self.authority.tasks.outbox() if msg['type']=='task.execute')
        self.bridge.publish(command)
        scratch=self.outputs/'tasks'/task['id']/command['attempt_id']/'manifest.json'
        scratch.rename(scratch.with_suffix('.retained'))
        self.stop_worker()
        for container in self.engine.containers.values():container['State'].update(Running=False,Pid=0,Status='exited')
        self.reconciler.tick()
        self.assertEqual(self.repo.get_task(task['id'])['status'],'running')
        self.assertIsNotNone(self.authority.get('instance-one')['claim'])
        scratch.with_suffix('.retained').rename(scratch)
        self.reconciler.tick()
        self.assertEqual(self.repo.get_task(task['id'])['status'],'succeeded')
        self.assertIsNone(self.authority.get('instance-one')['claim'])
        self.assertEqual(self.executions.value,1)
    def test_controller_exit_alone_does_not_lose_journaled_terminal(self):
        self.tick(); task=self.task(); self.tick()
        # Dispatch persisted. Deliver/execute but deliberately lose event delivery.
        message=next(msg for msg in self.authority.tasks.outbox() if msg["type"]=="task.execute")
        self.bridge.publish(message)
        self.assertEqual(self.repo.get_task(task["id"])["status"],"assigned")
        row=self.authority.get("instance-one")
        owned=json.loads(row["claim"]["execution_json"])["owned_execution"]
        record=self.controller.get(owned["intent_id"])
        self.controller.stop(record["intent_id"],record["version"])
        restarted=Reconciler(Repository(self.repo.path),None,self.scheduler,package_provider=self.provider)
        self.assertTrue(restarted.recover_before_release(row,self.package,self.controller))
        self.assertEqual(self.repo.get_task(task["id"])["status"],"failed")
        with self.repo._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM task_attempts").fetchone()[0],1)
        self.assertEqual(self.executions.value,1)
    def test_cancel_deadline_runs_without_redis_and_releases_only_after_domain_exit(self):
        self.tick();task=self.task();self.tick()
        self.authority.tasks.cancel(task["id"])
        with self.repo._connect() as db: db.execute("UPDATE instance_cancel_deadlines SET due_at='2000-01-01'")
        self.bridge.offline=True
        self.observer.populated=1
        self.reconciler.cancel_tick()
        self.assertIsNotNone(self.authority.get("instance-one")["claim"])
        self.observer.populated=0
        self.reconciler.cancel_tick()
        self.assertIsNone(self.authority.get("instance-one")["claim"])
        self.assertEqual(self.repo.get_task(task["id"])["status"],"canceled")
        self.assertEqual(len([call for call in self.engine.calls if call[0]=="stop"]),1)

    def test_execution_timeout_survives_restart_redis_failure_and_waits_for_domain_exit(self):
        # Real supervisor child and journal, fixture Engine/GPU/transport.
        # This is not evidence of a production GPU timeout.
        self.tick(); task=self.task(); self.tick()
        with self.repo._connect() as db:
            db.execute("UPDATE task_attempts SET execution_deadline_at='2000-01-01T00:00:00+00:00' WHERE task_id=?", (task['id'],))
        self.bridge.offline=True
        restarted=Reconciler(Repository(self.repo.path),None,self.scheduler,package_provider=self.provider)
        restarted.cancel_tick()
        current=self.repo.get_task(task['id'])
        self.assertEqual((current['status'], current['error']), ('cancel_requested','task_timed_out'))
        with self.repo._connect() as db:
            db.execute("UPDATE instance_cancel_deadlines SET due_at='2000-01-01' WHERE attempt_id=?", (current['current_attempt_id'],))
        self.observer.populated=1
        restarted.cancel_tick()
        self.assertEqual(self.repo.get_task(task['id'])['status'],'cancel_requested')
        self.assertIsNotNone(self.authority.get('instance-one')['claim'])
        self.observer.populated=0
        restarted.cancel_tick()
        final=self.repo.get_task(task['id'])
        self.assertEqual((final['status'],final['error']),('failed','task_timed_out'))
        self.assertIsNone(self.authority.get('instance-one')['claim'])
        with self.repo._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM task_reservations WHERE released=0').fetchone()[0], 0)
        self.assertEqual(len([call for call in self.engine.calls if call[0]=='stop']),1)

    def test_old_cancel_snapshot_never_stops_a_replacement_claim(self):
        self.tick(); task=self.task(); self.tick()
        self.authority.tasks.cancel(task['id'])
        with self.repo._connect() as db:
            db.execute("UPDATE instance_cancel_deadlines SET due_at='2000-01-01'")
        deadlines=self.authority.due_cancellations()
        original=self.authority.get('instance-one')
        replacement=copy.deepcopy(original)
        replacement['claim']['claim_id']='new-claim'
        replacement['claim']['epoch']='new-epoch'
        # Simulate the narrower race: the deadline query has returned the old
        # identity, while get(instance) now sees a replacement. No real GPU.
        stops=len([call for call in self.engine.calls if call[0]=='stop'])
        with patch.object(self.authority,'due_cancellations',return_value=deadlines), \
                patch.object(self.authority,'get',return_value=replacement):
            self.reconciler.cancel_tick()
        self.assertEqual(len([call for call in self.engine.calls if call[0]=='stop']),stops)
        self.assertEqual(self.reconciler.last_errors,{})

    def test_completed_cancel_snapshot_is_ignored_before_container_lookup(self):
        self.tick(); task=self.task(); self.tick()
        self.authority.tasks.cancel(task['id'])
        with self.repo._connect() as db:
            db.execute("UPDATE instance_cancel_deadlines SET due_at='2000-01-01'")
        deadlines=self.authority.due_cancellations()
        with self.repo._connect() as db:
            db.execute("UPDATE instance_cancel_deadlines SET state='completed'")
        stops=len([call for call in self.engine.calls if call[0]=='stop'])
        with patch.object(self.authority,'due_cancellations',return_value=deadlines):
            self.reconciler.cancel_tick()
        self.assertEqual(len([call for call in self.engine.calls if call[0]=='stop']),stops)

    def test_timeout_pending_unclean_terminal_recovers_after_exact_container_exit(self):
        self.reset_fail=True
        self.tick(); task=self.task(); self.tick()
        with self.repo._connect() as db:
            db.execute("UPDATE task_attempts SET execution_deadline_at='2000-01-01T00:00:00+00:00' WHERE task_id=?", (task['id'],))
        self.authority.tasks.expire_due_tasks()
        events=self.journal.pending_events(limit=100)['items']
        for event in events:
            if event.get('task_id')==task['id']:
                self.authority.tasks.receive(event)
        self.assertEqual(self.repo.get_task(task['id'])['status'],'cancel_requested')
        with self.repo._connect() as db:
            db.execute("UPDATE instance_cancel_deadlines SET due_at='2000-01-01' WHERE attempt_id=?",
                       (self.repo.get_task(task['id'])['current_attempt_id'],))
        self.authority.due_cancellations()
        row=self.authority.get('instance-one')
        record=self.controller.get(json.loads(row['claim']['execution_json'])['owned_execution']['intent_id'])
        self.controller.stop(record['intent_id'],record['version'])
        self.assertTrue(self.reconciler.recover_before_release(row,self.package,self.controller))
        self.authority.confirm_container_exit(row['claim']['claim_id'],self.controller)
        final=self.repo.get_task(task['id'])
        self.assertEqual((final['status'],final['error']),('failed','task_timed_out'))
    def test_heartbeat_loss_fences_dispatch_and_survives_reconciler_restart(self):
        self.tick();task=self.task()
        with self.repo._connect() as db:
            db.execute("UPDATE instance_claims SET heartbeat_at='2000-01-01T00:00:00+00:00'")
        # Simulate a fresh control process: live observations are intentionally
        # process-local while the durable checkpoint remains stale.
        self.authority._heartbeat_observations.clear()
        self.bridge.offline=True
        self.reconciler.tick()
        row=self.authority.get('instance-one')
        self.assertEqual(row['status'],'quarantined')
        self.assertEqual(row['claim']['stop_reason'],'worker_heartbeat_expired')
        with self.assertRaises(TaskStateError):self.scheduler.dispatch(task)
        self.observer.populated=1
        restarted=Reconciler(Repository(self.repo.path),None,self.scheduler,package_provider=self.provider)
        restarted.cancel_tick()
        with self.repo._connect() as db:
            self.assertEqual(db.execute('SELECT SUM(mib) FROM task_reservations WHERE released=0').fetchone()[0],10)
        self.observer.populated=0
        restarted.cancel_tick()
        self.assertIsNone(self.authority.get('instance-one')['claim'])
        self.assertEqual(self.repo.get_task(task['id'])['status'],'queued')

    def test_busy_worker_survives_loader_heartbeat_jitter_but_still_has_a_finite_fence(self):
        self.tick()
        with self.repo._connect() as db:
            db.execute("UPDATE instance_claims SET heartbeat_at='2026-01-01T00:00:00+00:00'")
        # Recreate a cold observation cache when switching to the fixture clock.
        self.authority._heartbeat_observations.pop(('instance-one', 'epoch-one'), None)
        self.assertTrue(self.authority.observe_heartbeat(
            'instance-one', 'epoch-one', None, at='2026-01-01T00:05:00+00:00'))
        self.assertIsNone(self.authority.get('instance-one')['claim']['stop_reason'])
        self.assertFalse(self.authority.observe_heartbeat(
            'instance-one', 'epoch-one', None, at='2026-01-01T00:10:01+00:00'))
        self.assertEqual(self.authority.get('instance-one')['claim']['stop_reason'],
                         'worker_heartbeat_expired')

    def test_fresh_healthy_heartbeats_do_not_create_sustained_database_writes(self):
        self.tick()
        event = self.worker._telemetry(
            'telemetry.heartbeat', {'state': 'ready', 'uptime_seconds': 2})
        with self.repo._connect() as watcher:
            before = watcher.execute(
                "SELECT heartbeat_seq,heartbeat_at FROM instance_claims "
                "WHERE instance_id='instance-one' AND state!='exited'"
            ).fetchone()
            data_version = watcher.execute('PRAGMA data_version').fetchone()[0]
            self.assertTrue(self.authority.observe_heartbeat(
                'instance-one', 'epoch-one', event))
            after = watcher.execute(
                "SELECT heartbeat_seq,heartbeat_at FROM instance_claims "
                "WHERE instance_id='instance-one' AND state!='exited'"
            ).fetchone()
            self.assertEqual(
                watcher.execute('PRAGMA data_version').fetchone()[0], data_version)
        self.assertEqual(tuple(after), tuple(before))
        self.assertGreater(
            self.authority._heartbeat_observations[('instance-one', 'epoch-one')][0],
            before['heartbeat_seq'],
        )

    def test_unexpected_exit_recovers_original_result_before_release(self):
        self.tick();task=self.task();self.tick()
        command=next(msg for msg in self.authority.tasks.outbox() if msg['type']=='task.execute')
        self.bridge.publish(command)
        self.stop_worker()
        for container in self.engine.containers.values():
            container['State'].update(Running=False,Pid=0,Status='exited')
        self.reconciler.tick()
        self.assertEqual(self.repo.get_task(task['id'])['status'],'failed')
        self.assertIsNone(self.authority.get('instance-one')['claim'])
        self.assertEqual(self.executions.value,1)

    def test_host_reboot_recovers_journal_without_worker_transport(self):
        self.tick(); task = self.task(); self.tick()
        command = next(msg for msg in self.authority.tasks.outbox() if msg['type'] == 'task.execute')
        self.bridge.publish(command)
        self.stop_worker()
        for container in self.engine.containers.values():
            container['State'].update(Running=False, Pid=0, Status='exited')
        self.observer.changed = True
        self.bridge.offline = True
        self.engine.calls.clear()
        with patch.object(self.observer, 'previous_boot', return_value={'kind': 'previous-kernel-boot'}), \
                patch.object(self.reconciler, '_transport', side_effect=AssertionError('must not contact old Worker')):
            self.tick()
        self.assertEqual(self.repo.get_task(task['id'])['status'], 'failed')
        self.assertIsNone(self.authority.get('instance-one')['claim'])
        self.assertEqual(self.executions.value, 1)
        self.assertFalse([call for call in self.engine.calls if call[0] != 'inspect'])

    def test_host_reboot_does_not_release_claim_before_journal_recovery(self):
        self.tick(); task = self.task(); self.tick()
        self.stop_worker()
        for container in self.engine.containers.values():
            container['State'].update(Running=False, Pid=0, Status='exited')
        original = self.authority.get('instance-one')['claim']['claim_id']
        with patch.object(self.observer, 'previous_boot', return_value={'kind': 'previous-kernel-boot'}), \
                patch.object(self.reconciler, 'recover_before_release', return_value=False):
            self.tick()
        self.assertEqual(self.authority.get('instance-one')['claim']['claim_id'], original)
        with self.repo._connect() as db:
            self.assertGreater(db.execute("SELECT COUNT(*) FROM task_reservations WHERE released=0").fetchone()[0], 0)
        self.assertEqual(self.repo.get_task(task['id'])['status'], 'assigned')

    def test_host_reboot_preserves_intervening_operator_stop(self):
        self.tick()
        row = self.authority.get('instance-one')
        record = self.controller.get(json.loads(row['claim']['execution_json'])['owned_execution']['intent_id'])
        for container in self.engine.containers.values():
            container['State'].update(Running=False, Pid=0, Status='exited')
        recover = self.controller.recover_host_reboot
        def operator_race(*args):
            result = recover(*args)
            self.authority.require_stop('instance-one', row['claim']['epoch'], 'operator_stop')
            return result
        with patch.object(self.observer, 'previous_boot', return_value={'kind': 'previous-kernel-boot'}), \
                patch.object(self.controller, 'recover_host_reboot', side_effect=operator_race), \
                patch.object(self.reconciler, 'recover_before_release', return_value=False):
            self.assertTrue(self.reconciler._recover_reboot_exit(row, self.package, self.controller, record))
        current = self.authority.get('instance-one')
        self.assertEqual(current['claim']['stop_reason'], 'operator_stop')
        self.assertEqual(current['error_code'], 'operator_stop')
        self.assertEqual(current['desired_state'], 'unloaded')

    def test_host_reboot_rejects_changed_claim_before_journal_access(self):
        self.tick()
        row = self.authority.get('instance-one')
        record = self.controller.get(json.loads(row['claim']['execution_json'])['owned_execution']['intent_id'])
        for container in self.engine.containers.values():
            container['State'].update(Running=False, Pid=0, Status='exited')
        with patch.object(self.observer, 'previous_boot', return_value={'kind': 'previous-kernel-boot'}):
            recovered = self.controller.recover_host_reboot(record['intent_id'], record['version'])
        changed = copy.deepcopy(row)
        changed['claim']['claim_id'] = 'another-claim'
        for current in [None, {**row, 'claim': None}, changed]:
            with self.subTest(current=current is None), patch.object(self.authority, 'get', return_value=current), \
                    patch.object(self.reconciler, 'recover_before_release') as journal:
                with self.assertRaisesRegex(TaskStateError, 'backend_claim_conflict'):
                    self.reconciler._recover_reboot_exit(row, self.package, self.controller, recovered)
                journal.assert_not_called()

    def test_host_reboot_keeps_explicitly_stopped_service_disabled(self):
        self.tick()
        row = self.authority.get('instance-one')
        with self.repo._connect() as db:
            db.execute("UPDATE model_deployments SET enabled=0 WHERE id='instance-one'")
        self.authority.require_stop('instance-one', row['claim']['epoch'], 'operator_stop')
        self.stop_worker()
        for container in self.engine.containers.values():
            container['State'].update(Running=False, Pid=0, Status='exited')
        self.engine.calls.clear()
        with patch.object(self.observer, 'previous_boot', return_value={'kind': 'previous-kernel-boot'}):
            self.tick()
            self.tick()
        self.assertFalse(self.repo.get_deployment('instance-one')['enabled'])
        self.assertEqual(self.authority.get('instance-one')['desired_state'], 'unloaded')
        self.assertIsNone(self.authority.get('instance-one')['claim'])
        self.assertFalse([call for call in self.engine.calls if call[0] in {'create', 'start'}])

    def test_idle_deadline_does_not_change_desire_with_unquiescent_attempt(self):
        self.value['residency']='idle'
        self.authority.configure('instance-one',self.value,expected_version=self.authority.get('instance-one')['version'])
        self.tick();task=self.task();self.tick()
        with self.repo._connect() as db:
            db.execute("UPDATE instance_policies SET last_activity='2000-01-01T00:00:00+00:00'")
        self.authority.idle_unload('instance-one')
        self.assertEqual(self.authority.get('instance-one')['desired_state'],'loaded')
        self.tick();self.tick()
        self.assertEqual(self.repo.get_task(task['id'])['status'],'failed')
        with self.repo._connect() as db:
            db.execute("UPDATE instance_policies SET last_activity='2000-01-01T00:00:00+00:00'")
        self.assertTrue(self.authority.idle_unload('instance-one'))
        self.assertEqual(self.authority.get('instance-one')['desired_state'],'unloaded')

    def test_unload_before_start_keeps_enabled_container_online_without_model(self):
        row=self.authority.get('instance-one')
        self.scheduler.start_container('instance-one',self.package.verify())
        claim=self.authority.get('instance-one')['claim']
        record=self.controller.prepare('instance-one',claim['epoch'],claim['revision'])
        # Simulate a crash after prepare commit but before claim binding.
        self.authority.desire('instance-one','unloaded')
        for _ in range(4): self.tick()
        self.assertEqual(self.authority.get('instance-one')['claim']['state'],'online_unloaded')
        self.assertEqual(self.controller.get(record['intent_id'])['state'],'running')
        self.assertTrue(any(call[0] == 'start' for call in self.engine.calls))

    def test_reset_failure_fences_with_fresh_heartbeat_then_recovers_after_domain_exit(self):
        self.reset_fail=True
        self.tick();task=self.task();self.tick();self.tick()
        self.assertEqual(self.repo.get_task(task['id'])['status'],'failed')
        self.observer.populated=1
        self.tick()
        row=self.authority.get('instance-one')
        self.assertEqual((row['status'],row['claim']['stop_reason']),('quarantined','worker_error'))
        with self.repo._connect() as db:
            self.assertEqual(db.execute('SELECT SUM(mib) FROM task_reservations WHERE released=0').fetchone()[0],30)
            self.assertEqual(db.execute('SELECT exit_confirmed FROM task_attempts').fetchone()[0],0)
        healthy=self.worker._telemetry('telemetry.heartbeat',{'state':'ready','uptime_seconds':1})
        self.assertFalse(self.authority.observe_heartbeat('instance-one','epoch-one',healthy))
        self.assertEqual(self.authority.get('instance-one')['claim']['stop_reason'],'worker_error')
        restarted=Reconciler(Repository(self.repo.path),None,self.scheduler,package_provider=self.provider)
        self.bridge.offline=True
        self.observer.populated=0
        restarted.cancel_tick()
        self.assertIsNone(self.authority.get('instance-one')['claim'])
        self.assertEqual(self.repo.get_task(task['id'])['status'],'failed')
        self.assertEqual(self.executions.value,1)
        self.assertEqual(len([call for call in self.engine.calls if call[0]=='stop']),1)

    def test_error_heartbeat_requires_current_identity_new_sequence_and_fresh_time(self):
        self.tick()
        row=self.authority.get('instance-one')
        event=self.worker._telemetry('telemetry.heartbeat',{'state':'error','uptime_seconds':1})
        wrong=copy.deepcopy(event);wrong['worker_epoch']='wrong-epoch'
        with self.assertRaisesRegex(TaskStateError,'heartbeat_identity_conflict'):
            self.authority.observe_heartbeat('instance-one','epoch-one',wrong)
        repeated=copy.deepcopy(event);repeated['telemetry_seq']=row['claim']['heartbeat_seq']
        self.assertTrue(self.authority.observe_heartbeat('instance-one','epoch-one',repeated))
        stale=copy.deepcopy(event);stale['created_at']='2000-01-01T00:00:00Z'
        self.assertTrue(self.authority.observe_heartbeat('instance-one','epoch-one',stale))
        self.assertIsNone(self.authority.get('instance-one')['claim']['stop_reason'])


class DeferredConfigurationReconciliationTests(unittest.TestCase):
    def stopped_container(self, directory, *, bind=True):
        repository = Repository(Path(directory) / 'state.db')
        authority = InstancePolicy(repository)
        value = policy_value(seed_deployment(repository), (GPU,))
        authority.configure('instance-one', value)
        stopped = authority.set_service('instance-one', False)
        identity = package_identity(value)
        authority.package_validator = lambda _db, _id: identity
        claim = authority.claim_container('instance-one', 'epoch-one',
            expected_version=stopped['version'], backend='container',
            limits={GPU:49140}, package_identity=identity, materialize_only=True)
        policy = replace(fixture_policy(Path(directory), repository.path), gpu_uuids=(GPU,))
        engine, observer = FixtureEngine(), FixtureObserver()
        engine.config = SimpleNamespace(**dict(vars(engine.config), socket_path=policy.engine_socket))
        controller = RuntimeController(repository, engine, observer, policy)
        intent = controller.prepare('instance-one', 'epoch-one', claim['revision'])
        if bind:
            authority.bind_execution(claim['claim_id'], {'intent_id':intent['intent_id']})
        intent = controller.create_domain(intent['intent_id'], intent['version'])
        intent = controller.create(intent['intent_id'], intent['version'])
        if bind:
            authority.container_materialized('instance-one', 'epoch-one')
        # Controlled observation fixture; not evidence of real kernel isolation.
        observer._open = lambda _path: os.open(__file__, os.O_RDONLY)
        observer._identity = lambda _fd: next(iter(observer.records.values()))['identity']
        observer._populated = lambda _fd: 0
        harness = SimpleNamespace(repository=repository, authority=authority,
            _package=lambda _: (None, identity), _controller=lambda _: controller)
        harness.recover_before_release = lambda *args: Reconciler.recover_before_release(harness, *args)
        harness._stop_and_recover = lambda *args: Reconciler._stop_and_recover(harness, *args)
        harness._recover_reboot_exit = lambda *args: Reconciler._recover_reboot_exit(harness, *args)
        return repository, authority, value, controller, engine, observer, intent, harness

    def test_stopped_configuration_recovers_saved_unbound_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, authority, value, controller, engine, observer, intent, harness = self.stopped_container(directory, bind=False)
            changed = copy.deepcopy(value)
            changed['sharing_mode'] = 'exclusive'
            authority.configure('instance-one', changed, expected_version=authority.get('instance-one')['version'])
            Reconciler._instance(harness, authority.get('instance-one'))
            self.assertIsNone(authority.get('instance-one')['claim'])
            self.assertTrue(authority.apply_pending('instance-one'))

    def test_running_configuration_keeps_existing_worker_drain_path(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, authority, value, controller, engine, observer, intent, harness = self.stopped_container(directory)
            authority.set_service('instance-one', True, expected_version=authority.get('instance-one')['version'])
            controller.start(intent['intent_id'], intent['version'])
            authority.set_service('instance-one', False, expected_version=authority.get('instance-one')['version'])
            changed = copy.deepcopy(value)
            changed['sharing_mode'] = 'exclusive'
            authority.configure('instance-one', changed, expected_version=authority.get('instance-one')['version'])
            def worker_drain(_):
                raise RuntimeError('existing_worker_transport_path')
            harness._transport = worker_drain
            with self.assertRaisesRegex(RuntimeError, 'existing_worker_transport_path'):
                Reconciler._instance(harness, authority.get('instance-one'))
            self.assertEqual(controller.get(intent['intent_id'])['state'], 'running')
            self.assertFalse(any(call[0] == 'stop' for call in engine.calls))
            self.assertFalse(authority.apply_pending('instance-one'))

    def test_stopped_configuration_retires_created_container_before_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, authority, value, controller, engine, observer, intent, harness = self.stopped_container(directory)
            # Ordinary stopped containers must remain materialized.
            Reconciler._instance(harness, authority.get('instance-one'))
            self.assertEqual(controller.get(intent['intent_id'])['state'], 'created')
            changed = copy.deepcopy(value)
            changed['sharing_mode'] = 'exclusive'
            authority.configure('instance-one', changed, expected_version=authority.get('instance-one')['version'])
            self.assertFalse(authority.apply_pending('instance-one'))
            Reconciler._instance(harness, authority.get('instance-one'))
            exited = controller.get(intent['intent_id'])
            self.assertEqual(exited['state'], 'exited')
            self.assertEqual(json.loads(exited['exit_evidence_json'])['kind'], 'never_started')
            self.assertIsNone(authority.get('instance-one')['claim'])
            self.assertTrue(authority.apply_pending('instance-one'))
            self.assertEqual(authority.get('instance-one')['policy'], changed)
            self.assertFalse(any(call[0] in {'start', 'stop'} for call in engine.calls))

    def test_stopped_configuration_retries_unconfirmed_exit_without_switching(self):
        for obstruction in ('inspection', 'running', 'populated'):
            with self.subTest(obstruction=obstruction), tempfile.TemporaryDirectory() as directory:
                repo, authority, value, controller, engine, observer, intent, harness = self.stopped_container(directory)
                changed = copy.deepcopy(value)
                changed['sharing_mode'] = 'exclusive'
                authority.configure('instance-one', changed, expected_version=authority.get('instance-one')['version'])
                original = copy.deepcopy(engine.containers)
                if obstruction == 'running':
                    engine.containers[intent['container_id']]['State'].update(Running=True, Pid=123)
                if obstruction == 'populated':
                    observer._populated = lambda _fd: 1
                from mediacenter.config import ContainerError
                with patch.object(engine, 'inspect', side_effect=ContainerError('fixture_transient')) if obstruction == 'inspection' else patch.object(engine, 'inspect', wraps=engine.inspect):
                    Reconciler._instance(harness, authority.get('instance-one'))
                self.assertEqual(controller.get(intent['intent_id'])['state'], 'exit_unconfirmed')
                self.assertFalse(authority.apply_pending('instance-one'))
                self.assertEqual(authority.get('instance-one')['policy'], value)
                engine.containers = original
                observer._populated = lambda _fd: 0
                # New controller simulates recovery after a server restart.
                resumed = RuntimeController(repo, engine, observer, controller.policy)
                harness._controller = lambda _: resumed
                Reconciler._instance(harness, authority.get('instance-one'))
                self.assertIsNone(authority.get('instance-one')['claim'])
                self.assertTrue(authority.apply_pending('instance-one'))
                with repo._connect() as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM runtime_effects WHERE intent_id=? AND action='stop'", (intent['intent_id'],)).fetchone()[0], 1)
                self.assertFalse(any(call[0] in {'start', 'stop'} for call in engine.calls))

    def test_pending_runtime_configuration_keeps_rollback_base_until_health_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "state.db")
            authority = InstancePolicy(repository)
            binding = seed_deployment(repository)
            value = policy_value(binding)
            configured = authority.configure("instance-one", value)
            loaded = authority.desire("instance-one", "loaded",
                                      expected_version=configured["version"])
            package = package_identity(value)
            authority.package_validator = lambda _db, record_id: (
                package if record_id == package["runtime_record_id"] else None)
            authority.claim_container(
                "instance-one", "epoch-one", expected_version=loaded["version"],
                backend="container", limits={"GPU-one": 100, "GPU-two": 100},
                package_identity=package)

            changed = copy.deepcopy(value)
            changed["sharing_mode"] = "exclusive"
            pending = authority.configure(
                "instance-one", changed, expected_version=loaded["version"])
            stopped = authority.set_service(
                "instance-one", False, expected_version=pending["version"])
            self.assertEqual(stopped["configuration_state"], "restart_pending")
            self.assertTrue(authority.close_unstarted_claim("instance-one"))

            self.assertTrue(authority.apply_pending("instance-one"))
            applying = authority.get("instance-one")
            self.assertEqual((applying["configuration_state"], applying["policy"],
                              applying["pending_policy"], applying["desired_state"]),
                             ("applying", changed, value, "unloaded"))
            applied = authority.commit_configuration("instance-one")
            self.assertEqual((applied["configuration_state"], applied["policy"],
                              applied["pending_policy"]),
                             ("applied", changed, None))


if __name__=="__main__":unittest.main()
