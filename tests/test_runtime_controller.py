from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from mediacenter.config import ContainerError, ContainerPolicy, EngineConfig, ImageApproval, MountGrant
from mediacenter.repository import Repository
from mediacenter.runtime_controller import RuntimeController
from mediacenter.container_runtime import CgroupObserver
from mediacenter.instance_policy import InstancePolicy
from mediacenter.task_state import TaskStateError
from tests.test_resident_policy import seed_deployment, policy_value, package_identity, fixture_capacity, model_event


def controller_claim(repository, policy, instance="instance", epoch="epoch-one", *, restart_recovery=False):
    authority = InstancePolicy(repository)
    binding = seed_deployment(repository, instance)
    value = policy_value(binding, policy.gpu_uuids)
    old = authority.get(instance)
    authority.configure(instance, value, expected_version=old["version"] if old else None,
                        restart_recovery=restart_recovery)
    row = authority.desire(instance, "loaded")
    package = package_identity(value, epoch)
    authority.package_validator = lambda _db, record_id: package if record_id == package["runtime_record_id"] else None
    claim = authority.claim_container(instance, epoch, expected_version=row["version"], backend="container",
        limits={gpu:100 for gpu in policy.gpu_uuids}, package_identity=package)
    return authority, claim["revision"]


def fixture_policy(root, database=None):
    grants = []
    for role in ("models", "inputs", "bootstrap", "redis_credentials", "redis_socket", "journal", "outputs"):
        source = root / role
        if role in {"bootstrap", "redis_credentials"}:
            source.write_text("fixture", encoding="utf-8")
        else:
            source.mkdir()
        grants.append(MountGrant.capture(role, source, "/worker/" + role))
    forbidden = []
    for name in ("server.db", "server-key", "engine.sock"):
        path = root / name
        if not path.exists(): path.write_bytes(b"private")
        forbidden.append(str(path))
    if database is not None:
        forbidden[0] = str(database)
    return ContainerPolicy(ImageApproval("example/worker@sha256:" + "a" * 64, "sha256:" + "b" * 64,
                                          "linux/amd64", ("/usr/bin/python3",), ("-m", "worker")),
                           tuple(grants), *forbidden, 1000, 1000, 1024 * 1024 * 512,
                           1000000000, 64, 1024 * 1024 * 16)


class FixtureObserver:
    """Flow fixture only. Not evidence of actual kernel isolation or exit."""
    def __init__(self):
        self.records, self.populated, self.calls = {}, 0, []
        self.changed = False

    def docker_parent(self, name):
        return "/mediacenter/" + name

    def previous_boot(self, record):
        return None  # same-boot fixture; cross-boot tests supply an explicit proof

    def create(self, name):
        self.calls.append(("create", name))
        if name in self.records:
            raise ContainerError("cgroup_create_unknown")
        result = {"path": "/sys/fs/cgroup" + self.docker_parent(name), "docker_parent": self.docker_parent(name),
                  "identity": {"handle": name, "boot": "fixture-boot"}}
        self.records[name] = result
        return copy.deepcopy(result)

    def verify(self, record):
        if self.changed:
            raise ContainerError("cgroup_identity_changed")
        if record["identity"] != self.records[Path(record["path"]).name]["identity"]:
            raise ContainerError("cgroup_identity_changed")

    def observe_member(self, record, pid):
        self.verify(record)
        return {"pid": pid, "starttime": 1234, "membership": record["docker_parent"] + "/docker-leaf"}

    def prove_empty(self, record, membership):
        self.verify(record)
        if not membership:
            raise ContainerError("container_domain_mapping_unproven")
        if self.populated:
            raise ContainerError("execution_domain_not_empty")
        return {"kind": "fixture-empty", "identity": record["identity"], "membership": membership, "populated": 0}


class FixtureEngine:
    def __init__(self):
        self.config = EngineConfig("/run/docker.sock", "fixture-engine", "/sys/fs/cgroup", "/sys/fs/cgroup/mediacenter")
        self.calls, self.containers = [], {}
        self.create_error, self.start_error, self.stop_error, self.remove_error = None, None, None, None
        self.create_hook, self.start_hook, self.stop_hook, self.remove_hook = (
            lambda: None, lambda: None, lambda: None, lambda: None)

    def verify_engine(self):
        return {"engine_id": self.config.engine_id}

    def verify_image(self, image):
        return image.image_id

    def create(self, name, spec):
        self.calls.append(("create", name)); self.create_hook()
        full_id = format(len(self.containers) + 1, "064x")
        config = {key: copy.deepcopy(value) for key, value in spec.items() if key != "HostConfig"}
        self.containers[full_id] = {"Id": full_id, "Name": "/" + name, "Image": "sha256:" + "b" * 64,
            "Config": config, "HostConfig": copy.deepcopy(spec["HostConfig"]), "RestartCount": 0,
            "Mounts": [{"Type": "bind", "Source": m["Source"], "Destination": m["Target"],
                        "RW": not m["ReadOnly"], "Propagation": "rprivate"} for m in spec["HostConfig"]["Mounts"]],
            "State": {"Status": "created", "Running": False, "Pid": 0, "Restarting": False, "Paused": False, "Dead": False}}
        if self.create_error:
            raise self.create_error
        return full_id

    def inspect(self, container_id):
        self.calls.append(("inspect", container_id))
        if container_id not in self.containers:
            raise ContainerError("engine_object_missing")
        return copy.deepcopy(self.containers[container_id])

    def inspect_intent_name(self, name):
        self.calls.append(("inspect_name", name))
        for value in self.containers.values():
            if value["Name"] == "/" + name:
                return copy.deepcopy(value)
        raise ContainerError("engine_object_missing")

    def start(self, container_id):
        self.calls.append(("start", container_id)); self.start_hook()
        self.containers[container_id]["State"].update(Status="running", Running=True, Pid=3456)
        if self.start_error:
            raise self.start_error

    def stop(self, container_id):
        self.calls.append(("stop", container_id)); self.stop_hook()
        self.containers[container_id]["State"].update(Status="exited", Running=False, Pid=0)
        if self.stop_error:
            raise self.stop_error

    def wait(self, container_id):
        self.calls.append(("wait", container_id)); return 0

    def remove(self, container_id):
        self.calls.append(("remove", container_id)); self.remove_hook()
        if self.remove_error:
            raise self.remove_error
        if container_id not in self.containers:
            raise ContainerError("engine_object_missing")
        del self.containers[container_id]


class RuntimeControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.repository = Repository(self.root / "state.db")
        self.policy, self.engine, self.observer = replace(fixture_policy(self.root, self.repository.path), gpu_uuids=("GPU-00000000-0000-0000-0000-000000000001",)), FixtureEngine(), FixtureObserver()
        self.authority, self.revision = controller_claim(
            self.repository, self.policy,
            restart_recovery=self._testMethodName == "test_confirmed_unexpected_exit_rebuilds_new_generation_without_policy_revision_change")
        self.engine.config = SimpleNamespace(**dict(vars(self.engine.config), socket_path=self.policy.engine_socket))
        self.controller = RuntimeController(self.repository, self.engine, self.observer, self.policy)

    def tearDown(self):
        self.temp.cleanup()

    def prepared(self):
        row = self.controller.prepare("instance", "epoch-one", self.revision)
        self.authority.bind_execution(self.authority.get("instance")["claim"]["claim_id"], {"intent_id":row["intent_id"]})
        return row

    def next_epoch(self, epoch):
        claim = self.authority.get("instance")["claim"]
        self.authority.desire("instance", "unloaded")
        self.authority.confirm_container_exit(claim["claim_id"], self.controller)
        self.authority, revision = controller_claim(self.repository, self.policy, epoch=epoch)
        return revision

    def created(self):
        row = self.prepared()
        row = self.controller.create_domain(row["intent_id"], row["version"])
        return self.controller.create(row["intent_id"], row["version"])

    def running(self):
        row = self.created()
        return self.controller.start(row["intent_id"], row["version"])

    def test_full_lifecycle_requires_domain_and_preserves_task_tables(self):
        row = self.running(); self.assertEqual(row["state"], "running")
        result = self.controller.stop(row["intent_id"], row["version"])
        self.assertEqual(result["state"], "exited")
        proof = json.loads(result["exit_evidence_json"])
        self.assertEqual(proof["container_id"], row["container_id"])
        self.assertEqual(proof["epoch"], "epoch-one")
        self.assertEqual(len(self.observer.records), 1)  # retained, never removed
        with self.repository._connect() as db:
            for table in ("tasks", "task_attempts"):
                self.assertEqual(db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM task_reservations").fetchone()[0], 0)
        revision = self.next_epoch("epoch-two")
        self.assertEqual(self.controller.prepare("instance", "epoch-two", revision)["state"], "prepared")

    def test_host_reboot_exit_is_bound_and_never_reissues_engine_mutations(self):
        row = self.running()
        self.engine.containers[row["container_id"]]["State"].update(Status="exited", Running=False, Pid=0)
        self.observer.changed = True  # ordinary old-domain proof is unavailable
        proof = {"kind": "previous-kernel-boot", "previous_host": {"boot_id": "old"},
                 "current_host": {"boot_id": "new"}, "domain_digest": row["domain_digest"]}
        self.engine.calls.clear()
        with patch.object(self.observer, "previous_boot", return_value=proof, create=True):
            result = self.controller.recover_host_reboot(row["intent_id"], row["version"])
            repeated = self.controller.recover_host_reboot(result["intent_id"], result["version"])
        self.assertEqual(result["state"], "exited")
        self.assertEqual(repeated, result)
        evidence = json.loads(result["exit_evidence_json"])
        self.assertEqual(evidence["kind"], "host_reboot")
        self.assertEqual(evidence["domain"], proof)
        self.assertEqual(evidence["epoch"], row["epoch"])
        self.assertFalse([call for call in self.engine.calls if call[0] != "inspect"])
        # Controller proof alone must not close claims or release reservations.
        self.assertNotEqual(self.authority.get("instance")["claim"]["state"], "exited")

    def test_previous_boot_observer_requires_distinct_stable_kernel_identity(self):
        observer = object.__new__(CgroupObserver)
        observer.config = SimpleNamespace(cgroup_driver="cgroupfs")
        observer.mount = self.root
        observer.delegated = self.root / "domains"
        name = "mc-" + "a" * 32
        old = {"boot_id": "11111111-1111-1111-1111-111111111111"}
        current = {"boot_id": "22222222-2222-2222-2222-222222222222"}
        record = {"path": str(observer.parent_path(name)), "docker_parent": observer.docker_parent(name),
                  "membership_parent": observer.docker_parent(name), "state": "retained", "identity": {"host": old}}
        with patch.object(observer, "_host", return_value=current), patch.object(observer, "_open") as opened:
            self.assertEqual(observer.previous_boot(record)["previous_host"], old)
            opened.assert_not_called()  # old boot's path is deliberately absent
        with patch.object(observer, "_host", return_value=old):
            self.assertIsNone(observer.previous_boot(record))
        with patch.object(observer, "_host", return_value={**old, "namespace": [5, 99]}):
            self.assertIsNone(observer.previous_boot(record))
        with patch.object(observer, "_host", side_effect=[current, old]):
            with self.assertRaisesRegex(ContainerError, "cgroup_host_identity_changed"):
                observer.previous_boot(record)
        record["identity"]["host"] = {"boot_id": "invalid"}
        with self.assertRaisesRegex(ContainerError, "cgroup_host_identity_invalid"):
            observer.previous_boot(record)
        observer.config = SimpleNamespace(cgroup_driver="systemd")
        record.update(path=str(observer.parent_path(name)), docker_parent=observer.docker_parent(name),
                      membership_parent="/" + observer.parent_path(name).relative_to(observer.mount).as_posix(),
                      identity={"host": old})
        with patch.object(observer, "_host", return_value=current):
            self.assertEqual(observer.previous_boot(record)['current_host'], current)

    def test_reboot_recovery_keeps_same_boot_and_live_container_closed(self):
        row = self.running()
        with patch.object(self.observer, "previous_boot", return_value=None, create=True):
            self.assertEqual(self.controller.recover_host_reboot(row["intent_id"], row["version"]), row)
        with patch.object(self.observer, "previous_boot", return_value={"kind": "previous-kernel-boot"}, create=True):
            with self.assertRaisesRegex(ContainerError, "container_exit_unconfirmed"):
                self.controller.recover_host_reboot(row["intent_id"], row["version"])
        self.assertEqual(self.controller.get(row["intent_id"]), row)

    def test_reboot_recovery_crash_after_fence_resumes_without_stop_reissue(self):
        row = self.running()
        self.engine.containers[row["container_id"]]["State"].update(Status="exited", Running=False, Pid=0)
        proof = {"kind": "previous-kernel-boot", "domain_digest": row["domain_digest"]}
        self.controller.fault = lambda point: (_ for _ in ()).throw(RuntimeError("crash")) if point == "stop.after_intent_commit" else None
        with patch.object(self.observer, "previous_boot", return_value=proof, create=True):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                self.controller.recover_host_reboot(row["intent_id"], row["version"])
            pending = self.controller.get(row["intent_id"])
            self.assertEqual(pending["state"], "stop_pending")
            self.controller.fault = lambda _: None
            result = self.controller.recover_host_reboot(pending["intent_id"], pending["version"])
        self.assertEqual(result["state"], "exited")
        self.assertFalse([call for call in self.engine.calls if call[0] == "stop"])

    def test_reboot_recovery_rejects_missing_or_drifted_container_without_fence(self):
        row = self.running()
        item = self.engine.containers[row["container_id"]]
        item["State"].update(Status="exited", Running=False, Pid=0)
        original = copy.deepcopy(item)
        mutations = [lambda v: v.update(Id="f" * 64),
                     lambda v: v["HostConfig"].update(Privileged=True),
                     lambda v: v["HostConfig"].update(RestartPolicy={"Name": "always"}),
                     lambda v: v.update(RestartCount=1)]
        with patch.object(self.observer, "previous_boot", return_value={"kind": "previous-kernel-boot"}):
            for mutate in mutations:
                self.engine.containers[row["container_id"]] = copy.deepcopy(original)
                mutate(self.engine.containers[row["container_id"]])
                with self.subTest(mutation=mutate), self.assertRaises(ContainerError):
                    self.controller.recover_host_reboot(row["intent_id"], row["version"])
                self.assertEqual(self.controller.get(row["intent_id"]), row)
            self.engine.containers.pop(row["container_id"])
            with self.assertRaisesRegex(ContainerError, "engine_object_missing"):
                self.controller.recover_host_reboot(row["intent_id"], row["version"])
        self.assertEqual(self.controller.get(row["intent_id"]), row)

    def test_reboot_recovery_rejects_engine_failure_and_stale_version(self):
        row = self.running()
        with self.assertRaisesRegex(ContainerError, "runtime_state_conflict"):
            self.controller.recover_host_reboot(row["intent_id"], row["version"] - 1)
        with patch.object(self.observer, "previous_boot", return_value={"kind": "previous-kernel-boot"}), \
                patch.object(self.engine, "verify_engine", side_effect=ContainerError("engine_unavailable")):
            with self.assertRaisesRegex(ContainerError, "engine_unavailable"):
                self.controller.recover_host_reboot(row["intent_id"], row["version"])
        self.assertEqual(self.controller.get(row["intent_id"]), row)

    def test_reboot_recovery_rejects_proof_change_after_fence(self):
        row = self.running()
        self.engine.containers[row["container_id"]]["State"].update(Status="exited", Running=False, Pid=0)
        with patch.object(self.observer, "previous_boot", side_effect=[{"boot": "one"}, {"boot": "two"}]):
            with self.assertRaisesRegex(ContainerError, "execution_domain_changed"):
                self.controller.recover_host_reboot(row["intent_id"], row["version"])
        self.assertEqual(self.controller.get(row["intent_id"])["state"], "stop_pending")
        self.assertIsNone(self.controller.get(row["intent_id"])["exit_evidence_json"])

    def test_reboot_recovery_result_commit_crash_is_retryable(self):
        row = self.running()
        self.engine.containers[row["container_id"]]["State"].update(Status="exited", Running=False, Pid=0)
        self.controller.fault = lambda p: (_ for _ in ()).throw(RuntimeError("crash")) if p == "result.before_commit" else None
        with patch.object(self.observer, "previous_boot", return_value={"kind": "previous-kernel-boot"}):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                self.controller.recover_host_reboot(row["intent_id"], row["version"])
            row = self.controller.get(row["intent_id"])
            self.assertEqual(row["state"], "stop_pending")
            self.controller.fault = lambda _: None
            self.assertEqual(self.controller.recover_host_reboot(row["intent_id"], row["version"])["state"], "exited")
        self.assertFalse([call for call in self.engine.calls if call[0] == "stop"])

    def test_reboot_recovery_rejects_container_state_change_between_reads(self):
        row = self.running()
        self.engine.containers[row["container_id"]]["State"].update(Status="exited", Running=False, Pid=0)
        first = self.engine.inspect(row["container_id"])
        second = copy.deepcopy(first)
        second['State']['ExitCode'] = 9
        with patch.object(self.observer, 'previous_boot', return_value={'kind': 'previous-kernel-boot'}), \
                patch.object(self.engine, 'inspect', side_effect=[first, second]):
            with self.assertRaisesRegex(ContainerError, 'execution_domain_changed'):
                self.controller.recover_host_reboot(row['intent_id'], row['version'])
        self.assertIsNone(self.controller.get(row['intent_id'])['exit_evidence_json'])

    def test_reboot_recovery_does_not_resolve_unstarted_or_ambiguous_start(self):
        row = self.created()
        with patch.object(self.observer, "previous_boot") as proof:
            self.assertEqual(self.controller.recover_host_reboot(row["intent_id"], row["version"]), row)
            proof.assert_not_called()
        self.engine.start_error = ContainerError("engine_unavailable", outcome_unknown=True)
        row = self.controller.start(row["intent_id"], row["version"])
        self.assertEqual(row["state"], "start_unknown")
        with patch.object(self.observer, "previous_boot") as proof:
            self.assertEqual(self.controller.recover_host_reboot(row["intent_id"], row["version"]), row)
            proof.assert_not_called()

    def test_stopped_service_materializes_container_then_starts_same_identity(self):
        # Retire the default running-intent fixture setup before constructing a
        # second independent stopped deployment in the same repository.
        other = "stopped-instance"
        binding = seed_deployment(self.repository, other)
        value = policy_value(binding, self.policy.gpu_uuids)
        authority = InstancePolicy(self.repository)
        authority.configure(other, value)
        current = authority.get(other)
        authority.set_service(other, False, expected_version=current["version"])
        current = authority.get(other)
        package = package_identity(value, "stopped-epoch")
        authority.package_validator = lambda _db, record_id: (
            package if record_id == package["runtime_record_id"] else None)
        claim = authority.claim_container(
            other, "stopped-epoch", expected_version=current["version"],
            backend="container", limits={gpu: 100 for gpu in self.policy.gpu_uuids},
            package_identity=package, materialize_only=True)
        controller = RuntimeController(
            self.repository, self.engine, self.observer, self.policy)
        row = controller.prepare(other, "stopped-epoch", claim["revision"])
        authority.bind_execution(claim["claim_id"], {"intent_id": row["intent_id"]})
        row = controller.create_domain(row["intent_id"], row["version"])
        row = controller.create(row["intent_id"], row["version"])
        materialized = authority.container_materialized(other, "stopped-epoch")
        self.assertEqual(row["state"], "created")
        self.assertEqual(materialized["claim"]["state"], "container_stopped")
        self.assertEqual(materialized["status"], "unloaded")
        with self.repository._connect() as watcher:
            before = watcher.execute(
                "SELECT runtime_updated_at,updated_at FROM model_deployments WHERE id=?",
                (other,),
            ).fetchone()
            data_version = watcher.execute("PRAGMA data_version").fetchone()[0]
            repeated = authority.container_materialized(other, "stopped-epoch")
            after = watcher.execute(
                "SELECT runtime_updated_at,updated_at FROM model_deployments WHERE id=?",
                (other,),
            ).fetchone()
            self.assertEqual(
                watcher.execute("PRAGMA data_version").fetchone()[0], data_version
            )
        self.assertEqual(tuple(after), tuple(before))
        self.assertEqual(repeated["claim"]["state"], "container_stopped")
        container_id = row["container_id"]
        with self.assertRaisesRegex(TaskStateError, "backend_claim_fenced"):
            controller.start(row["intent_id"], row["version"])
        authority.set_service(other, True, expected_version=materialized["version"])
        started = controller.start(row["intent_id"], row["version"])
        self.assertEqual(started["state"], "running")
        self.assertEqual(started["container_id"], container_id)

    def test_proven_exited_container_removal_is_durable_and_idempotent(self):
        running = self.running()
        exited = self.controller.stop(running["intent_id"], running["version"])
        removed = self.controller.remove_exited(exited["intent_id"])
        self.assertEqual(removed["state"], "removed")
        self.assertNotIn(exited["container_id"], self.engine.containers)
        self.assertEqual(self.controller.remove_exited(exited["intent_id"]), removed)
        with self.repository._connect() as db:
            record = db.execute(
                "SELECT state,result_digest FROM runtime_container_removals WHERE intent_id=?",
                (exited["intent_id"],),
            ).fetchone()
        self.assertEqual(record["state"], "removed")
        self.assertIsNotNone(record["result_digest"])

    def test_container_removal_recovers_after_response_is_lost(self):
        running = self.running()
        exited = self.controller.stop(running["intent_id"], running["version"])
        self.controller.fault = lambda point: (
            (_ for _ in ()).throw(RuntimeError(point))
            if point == "remove.after_external" else None)
        with self.assertRaisesRegex(RuntimeError, "remove.after_external"):
            self.controller.remove_exited(exited["intent_id"])
        self.controller.fault = lambda _: None
        self.assertEqual(self.controller.remove_exited(exited["intent_id"])["state"],
                         "removed")

    def historical_controller(self, row, **overrides):
        from mediacenter.runtime_provisioning import _RemovalRuntime
        values = dict(instance_id=row['instance_id'], epoch=row['epoch'], record_id='package-fixture',
                      engine=self.engine.config, policy=self.policy, boundary_check=lambda: None,
                      generation=row['generation'])
        values.update(overrides)
        with patch('mediacenter.container_runtime.UnixEngine', return_value=self.engine), \
                patch('mediacenter.container_runtime.CgroupObserver', return_value=self.observer):
            return _RemovalRuntime(**values).controller(self.repository)

    def test_historical_removal_uses_recorded_spec_not_current_launcher_defaults(self):
        running = self.running()
        exited = self.controller.stop(running['intent_id'], running['version'])
        original = self.policy.spec
        def newer_spec(_policy, **kwargs):
            result = original(**kwargs)
            result['HostConfig']['Tmpfs']['/new-launch-default'] = 'rw,size=4096'
            return result
        with patch.object(ContainerPolicy, 'spec', newer_spec):
            with self.assertRaisesRegex(ContainerError, 'runtime_intent_integrity_error'):
                self.controller.remove_exited(exited['intent_id'])
            historical = self.historical_controller(exited)
            with self.assertRaisesRegex(ContainerError, 'runtime_intent_integrity_error'):
                historical.get(exited['intent_id'])
            self.assertEqual(historical.remove_exited(exited['intent_id'])['state'], 'removed')
        self.assertNotIn(exited['container_id'], self.engine.containers)
        self.assertEqual(self.controller.get(exited['intent_id']), exited)

    def test_historical_removal_rechecks_package_boundary_before_engine_access(self):
        running = self.running()
        exited = self.controller.stop(running['intent_id'], running['version'])
        boundary = SimpleNamespace(failed=False)
        def check():
            if boundary.failed: raise ContainerError('runtime_package_changed')
        controller = self.historical_controller(exited, boundary_check=check)
        boundary.failed = True
        self.engine.calls.clear()
        with self.assertRaisesRegex(ContainerError, 'runtime_package_changed'):
            controller.remove_exited(exited['intent_id'])
        self.assertEqual(self.engine.calls, [])
        self.assertEqual(self.controller.get(exited['intent_id']), exited)

    def test_historical_removal_rejects_wrong_authority_even_if_container_missing(self):
        running = self.running()
        exited = self.controller.stop(running['intent_id'], running['version'])
        del self.engine.containers[exited['container_id']]
        for mismatch in ({'instance_id':'other'}, {'epoch':'other'}, {'generation':exited['generation']+1},
                         {'policy':replace(self.policy, image=replace(self.policy.image, image_id='sha256:'+'c'*64))},
                         {'policy':replace(self.policy, mounts=tuple(reversed(self.policy.mounts)))}):
            with self.subTest(mismatch=list(mismatch)):
                controller = self.historical_controller(exited, **mismatch)
                self.engine.calls.clear()
                with self.assertRaisesRegex(ContainerError, 'historical_removal_authority_mismatch'):
                    controller.remove_exited(exited['intent_id'])
                self.assertEqual(self.engine.calls, [])

    def test_historical_removal_rejects_live_and_unproven_execution(self):
        running = self.running()
        historical = self.historical_controller(running)
        with self.assertRaisesRegex(ContainerError, 'container_exit_unconfirmed'):
            historical.remove_exited(running['intent_id'])
        exited = self.controller.stop(running['intent_id'], running['version'])
        self.engine.containers[exited['container_id']]['State'].update(Running=True, Pid=123, Status='running')
        self.engine.calls.clear()
        with self.assertRaisesRegex(ContainerError, 'container_exit_unconfirmed'):
            historical.remove_exited(exited['intent_id'])
        self.assertFalse(any(call[0]=='remove' for call in self.engine.calls))

    def test_same_epoch_idempotent_other_epoch_and_revision_fail_closed(self):
        row = self.prepared()
        self.assertEqual(row, self.prepared())
        with self.assertRaisesRegex(ContainerError, "runtime_claim_binding_mismatch"):
            self.controller.prepare("instance", "epoch-one", self.revision + 1)
        with self.assertRaisesRegex(TaskStateError, "backend_claim_required"):
            self.controller.prepare("instance", "epoch-two", 2)

    def test_confirmed_unexpected_exit_rebuilds_new_generation_without_policy_revision_change(self):
        row = self.running()
        first_claim = self.authority.get('instance')['claim']
        self.authority.require_stop('instance', 'epoch-one', 'runtime_exited')
        self.assertEqual(self.authority.get('instance')['desired_state'], 'loaded')
        next_package = package_identity(self.authority.get('instance')['policy'], 'epoch-two')
        self.authority.package_validator = lambda _db, record_id: next_package if record_id == next_package['runtime_record_id'] else None
        with self.assertRaisesRegex(TaskStateError, 'backend_already_claimed'):
            current = self.authority.get('instance')
            self.authority.claim_container('instance', 'epoch-two', expected_version=current['version'], backend='container',
                limits={gpu: 100 for gpu in self.policy.gpu_uuids}, package_identity=next_package)
        self.controller.stop(row['intent_id'], row['version'])
        self.authority.confirm_container_exit(first_claim['claim_id'], self.controller)
        current = self.authority.get('instance')
        new_claim = self.authority.claim_container('instance', 'epoch-two', expected_version=current['version'], backend='container',
            limits={gpu: 100 for gpu in self.policy.gpu_uuids}, package_identity=next_package)
        rebuilt = self.controller.prepare('instance', 'epoch-two', self.revision)
        self.assertEqual(rebuilt['generation'], row['generation'] + 1)
        self.assertEqual(rebuilt['desired_revision'], row['desired_revision'])
        self.assertEqual(rebuilt['claim_id'], new_claim['claim_id'])
        self.assertEqual(self.controller.prepare('instance', 'epoch-two', self.revision), rebuilt)
        with self.assertRaises(TaskStateError): self.controller.prepare('instance', 'epoch-one', self.revision)

    def test_authority_db_and_engine_socket_cannot_be_omitted_from_mount_denials(self):
        for changed in ({"server_database": str(self.root / "server.db")}, {"engine_socket": str(self.root / "server-key")}):
            with self.assertRaisesRegex(ContainerError, "container_authority_boundary_mismatch"):
                RuntimeController(self.repository, self.engine, self.observer, replace(self.policy, **changed))
        grants = list(self.policy.mounts)
        grants[2] = MountGrant.capture("bootstrap", self.repository.path, "/worker/bootstrap")
        controller = RuntimeController(self.repository, self.engine, self.observer, replace(self.policy, mounts=tuple(grants)))
        with self.assertRaisesRegex(ContainerError, "mount_forbidden_source"):
            controller.prepare("instance", "epoch", 1)
        self.assertFalse(self.engine.calls)

    def test_dual_connection_create_claim_issues_one_external_effect(self):
        row = self.prepared(); row = self.controller.create_domain(row["intent_id"], row["version"])
        def create(_):
            controller = RuntimeController(Repository(self.repository.path), self.engine, self.observer, self.policy)
            try:
                return controller.create(row["intent_id"], row["version"])["state"]
            except ContainerError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, range(2)))
        self.assertEqual(results.count("created"), 1)
        self.assertEqual(len([v for v in self.engine.calls if v[0] == "create"]), 1)

    def test_unknown_create_restart_recovers_full_id_without_second_post(self):
        row = self.prepared(); row = self.controller.create_domain(row["intent_id"], row["version"])
        self.engine.create_error = ContainerError("engine_unavailable", outcome_unknown=True)
        row = self.controller.create(row["intent_id"], row["version"])
        self.assertEqual(row["state"], "create_unknown")
        restarted = RuntimeController(Repository(self.repository.path), self.engine, self.observer, self.policy)
        with self.assertRaises(ContainerError):
            restarted.create(row["intent_id"], row["version"])
        result = restarted.reconcile(row["intent_id"], row["version"])
        self.assertEqual(result["state"], "created")
        self.assertEqual(len([v for v in self.engine.calls if v[0] == "create"]), 1)

    def test_pending_crash_and_missing_name_do_not_allow_create_reissue(self):
        row = self.prepared(); row = self.controller.create_domain(row["intent_id"], row["version"])
        self.controller.fault = lambda point: (_ for _ in ()).throw(RuntimeError()) if point == "create.after_intent_commit" else None
        with self.assertRaises(RuntimeError):
            self.controller.create(row["intent_id"], row["version"])
        row = self.controller.get(row["intent_id"])
        self.assertEqual(row["state"], "create_pending")
        with self.assertRaisesRegex(ContainerError, "engine_object_missing"):
            self.controller.reconcile(row["intent_id"], row["version"])
        self.assertFalse([v for v in self.engine.calls if v[0] == "create"])

    def test_domain_creation_crash_never_adopts_existing_parent(self):
        row = self.prepared()
        self.controller.fault = lambda point: (_ for _ in ()).throw(RuntimeError()) if point == "domain.after_external" else None
        with self.assertRaises(RuntimeError):
            self.controller.create_domain(row["intent_id"], row["version"])
        row = self.controller.get(row["intent_id"])
        with self.assertRaises(ContainerError):
            self.controller.create_domain(row["intent_id"], row["version"])
        self.assertEqual(len(self.observer.records), 1)
        self.assertEqual(row["state"], "domain_pending")

    def test_forged_name_labels_config_or_engine_never_authorize_stop(self):
        row = self.running(); original = copy.deepcopy(self.engine.containers[row["container_id"]])
        mutations = [lambda c: c.update(Id="f" * 64), lambda c: c.update(Image="sha256:" + "c" * 64),
                     lambda c: c["Config"]["Labels"].update({"mediacenter.epoch": "foreign"}),
                     lambda c: c["HostConfig"].update(Privileged=True), lambda c: c["Mounts"].append({}),
                     lambda c: c.update(RestartCount=1), lambda c: c["HostConfig"].update(NetworkMode="host")]
        for mutation in mutations:
            self.engine.containers[row["container_id"]] = copy.deepcopy(original)
            mutation(self.engine.containers[row["container_id"]])
            with self.assertRaises(ContainerError):
                self.controller.stop(row["intent_id"], row["version"])
        self.assertFalse([v for v in self.engine.calls if v[0] == "stop"])

    def test_unknown_start_recognizes_running_but_never_repeats_start(self):
        row = self.created(); self.engine.start_error = ContainerError("engine_unavailable", outcome_unknown=True)
        row = self.controller.start(row["intent_id"], row["version"])
        self.assertEqual(row["state"], "start_unknown")
        with self.assertRaises(ContainerError):
            self.controller.start(row["intent_id"], row["version"])
        row = self.controller.reconcile(row["intent_id"], row["version"])
        self.assertEqual(row["state"], "running")
        self.assertEqual(len([v for v in self.engine.calls if v[0] == "start"]), 1)

    def test_unknown_start_exited_or_short_lived_mapping_does_not_prove_exit(self):
        row = self.created(); self.engine.start_error = ContainerError("engine_unavailable", outcome_unknown=True)
        row = self.controller.start(row["intent_id"], row["version"])
        self.engine.containers[row["container_id"]]["State"].update(Status="exited", Running=False, Pid=0)
        row = self.controller.reconcile(row["intent_id"], row["version"])
        self.assertEqual(row["state"], "start_unknown")
        self.assertIsNone(row["exit_evidence_json"])
        with self.assertRaises(ContainerError):
            self.controller.stop(row["intent_id"], row["version"])

    def test_descendants_or_changed_domain_keep_isolation_even_after_wait(self):
        row = self.running(); self.observer.populated = 1
        row = self.controller.stop(row["intent_id"], row["version"])
        self.assertEqual(row["state"], "exit_unconfirmed")
        self.assertIsNone(row["exit_evidence_json"])
        with self.assertRaises(TaskStateError):
            self.controller.prepare("instance", "epoch-two", 2)
        self.observer.populated = 0; self.observer.changed = True
        row = self.controller.reconcile(row["intent_id"], row["version"])
        self.assertEqual(row["state"], "exit_unconfirmed")
        self.observer.changed = False
        row = self.controller.reconcile(row["intent_id"], row["version"])
        self.assertEqual(row["state"], "exited")

    def test_404_and_permission_failures_never_prove_exit(self):
        row = self.running(); self.engine.stop_error = ContainerError("engine_permission_denied")
        row = self.controller.stop(row["intent_id"], row["version"])
        self.assertEqual(row["state"], "stop_unknown")
        self.engine.containers.clear()
        row = self.controller.reconcile(row["intent_id"], row["version"])
        self.assertEqual(row["state"], "exit_unconfirmed")
        self.assertIsNone(row["exit_evidence_json"])

    def test_stop_fence_blocks_concurrent_start_and_duplicate_stop(self):
        row = self.running()
        def during_stop():
            current = self.controller.get(row["intent_id"])
            for method in (self.controller.start, self.controller.stop):
                with self.assertRaises(ContainerError):
                    method(current["intent_id"], current["version"])
        self.engine.stop_hook = during_stop
        result = self.controller.stop(row["intent_id"], row["version"])
        self.assertEqual(result["state"], "exited")

    def test_durable_spec_tamper_fails_before_engine_mutation(self):
        row = self.running()
        with self.repository._connect() as db:
            spec = json.loads(row["spec_json"]); spec["HostConfig"]["Privileged"] = True
            db.execute("UPDATE runtime_intents SET spec_json=? WHERE intent_id=?", (json.dumps(spec), row["intent_id"]))
        with self.assertRaisesRegex(ContainerError, "runtime_intent_integrity_error"):
            self.controller.stop(row["intent_id"], row["version"])
        self.assertFalse([v for v in self.engine.calls if v[0] == "stop"])

    def test_recapturing_replaced_mount_cannot_reauthorize_old_intent(self):
        row = self.prepared(); row = self.controller.create_domain(row["intent_id"], row["version"])
        (self.root / "models").rename(self.root / "models-preserved")
        (self.root / "models").mkdir()
        with self.assertRaisesRegex(ContainerError, "mount_identity_changed"):
            self.controller.get(row["intent_id"])
        recaptured = replace(self.policy, mounts=tuple(MountGrant.capture(g.role, g.source, g.target) for g in self.policy.mounts))
        restarted = RuntimeController(Repository(self.repository.path), self.engine, self.observer, recaptured)
        with self.assertRaisesRegex(ContainerError, "runtime_mount_grant_changed"):
            restarted.create(row["intent_id"], row["version"])
        self.assertFalse([v for v in self.engine.calls if v[0] == "create"])

    def test_new_epoch_after_confirmed_exit_can_use_explicit_new_budget(self):
        row = self.running(); self.controller.stop(row["intent_id"], row["version"])
        policy = replace(self.policy, memory_bytes=self.policy.memory_bytes * 2)
        restarted = RuntimeController(Repository(self.repository.path), self.engine, self.observer, policy)
        new = restarted.prepare("instance", "epoch-two", self.next_epoch("epoch-two"))
        self.assertEqual(json.loads(new["spec_json"])["HostConfig"]["Memory"], policy.memory_bytes)

    def test_clock_rollback_or_equal_timestamps_cannot_reuse_revision(self):
        with patch("mediacenter.runtime_controller.now", return_value="2100-01-01"):
            row = self.running(); self.controller.stop(row["intent_id"], row["version"])
        with patch("mediacenter.runtime_controller.now", return_value="2020-01-01"):
            row = self.controller.prepare("instance", "epoch-two", self.next_epoch("epoch-two"))
            self.authority.bind_execution(self.authority.get("instance")["claim"]["claim_id"], {"intent_id":row["intent_id"]})
            row = self.controller.create_domain(row["intent_id"], row["version"])
            row = self.controller.create(row["intent_id"], row["version"])
            row = self.controller.start(row["intent_id"], row["version"])
            self.controller.stop(row["intent_id"], row["version"])
            revision = self.next_epoch("epoch-three")
            with self.assertRaisesRegex(ContainerError, "runtime_claim_binding_mismatch"):
                self.controller.prepare("instance", "epoch-three", 2)
            self.assertEqual(self.controller.prepare("instance", "epoch-three", revision)["desired_revision"], revision)

    def test_real_process_crash_retains_effect_intent_and_never_fabricates_exit(self):
        script = """
import os,sys
from pathlib import Path
from mediacenter.repository import Repository
from mediacenter.runtime_controller import RuntimeController
from tests.test_runtime_controller import fixture_policy,FixtureEngine,FixtureObserver,controller_claim
from dataclasses import replace
root=Path(sys.argv[1]); point=sys.argv[2]
repository=Repository(root/'state.db')
policy=replace(fixture_policy(root,repository.path),gpu_uuids=('GPU-00000000-0000-0000-0000-000000000001',)); authority,revision=controller_claim(repository,policy,epoch='epoch'); engine=FixtureEngine(); engine.config=__import__('types').SimpleNamespace(**dict(vars(engine.config),socket_path=policy.engine_socket))
controller=RuntimeController(repository,engine,FixtureObserver(),policy)
controller.fault=lambda current: os._exit(79) if current==point else None
print('MC031_CRASH_PID='+str(os.getpid()),flush=True)
row=controller.prepare('instance','epoch',revision)
row=controller.create_domain(row['intent_id'],row['version'])
row=controller.create(row['intent_id'],row['version'])
row=controller.start(row['intent_id'],row['version'])
controller.stop(row['intent_id'],row['version'])
os._exit(80)
"""
        cases = [("intent.before_commit", None), ("domain.after_intent_commit", "domain_pending"),
                 ("domain.after_external", "domain_pending"), ("create.after_intent_commit", "create_pending"),
                 ("create.after_external", "create_pending"), ("start.after_external", "start_pending"),
                 ("stop.after_external", "stop_pending")]
        for index, (point, expected_state) in enumerate(cases):
            root = self.root / f"crash-{index}"; root.mkdir()
            process = subprocess.Popen([sys.executable, "-B", "-c", script, str(root), point], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                stdout, stderr = process.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill(); process.communicate(); self.fail("fixture process timed out")
            self.assertEqual(process.returncode, 79, stderr)
            self.assertIn("MC031_CRASH_PID=" + str(process.pid), stdout)
            print(json.dumps({"fixture": "mc031-journal-crash", "point": point, "pid": process.pid,
                              "exitcode": process.returncode, "observed_exit": process.poll() is not None}), flush=True)
            with sqlite3.connect(root / "state.db") as db:
                rows = db.execute("SELECT state,exit_evidence_json FROM runtime_intents").fetchall()
                self.assertEqual(rows, [] if expected_state is None else [(expected_state, None)])
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
