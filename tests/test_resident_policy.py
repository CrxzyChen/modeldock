from __future__ import annotations

import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mediacenter.instance_policy import InstancePolicy
from mediacenter.repository import Repository
from mediacenter.task_state import TaskState, TaskStateError, digest
from mediacenter.capabilities import worker_capability_for
from mediacenter.protocol import PROTOCOL


def fixture_template():
    return {
        "schema": 1, "recipe_key": "fixture", "recipe_digest": "recipe-v1",
        "release_digest": "a" * 64,
        "resources": {"base_mib": 10, "task_mib": 20, "external_reserve_mib": 0,
                      "sharing_mode": "shared", "residency": "resident", "idle_seconds": 300},
        "limits": {"uid": 1000, "gid": 1000, "memory_bytes": 1024 ** 3,
                   "nano_cpus": 10 ** 9, "pids_limit": 64, "tmpfs_bytes": 16 * 1024 ** 2},
    }


def seed_deployment(repository, instance="instance-one", binding=None):
    binding = binding or {"model_key": "sdxl-base-1.0", "recipe_revision": "recipe-v1", "model_asset_id": "mdl_base", "model_asset_revision": "revision-v1", "dependencies": []}
    asset_manifest = digest(binding)
    with repository._connect() as db:
        db.execute("INSERT OR IGNORE INTO model_assets(id,display_name,media_kind,role,format,source_type,source_ref,revision,license_declared,manifest_digest,state,total_bytes,file_count,storage_relpath,created_at,updated_at) VALUES(?,'fixture','image','checkpoint','diffusers','upload','fixture',?,'fixture',?,'ready',1,1,?,'fixture','fixture')", (binding["model_asset_id"], binding["model_asset_revision"], asset_manifest, binding["model_asset_id"]))
    if repository.get_deployment(instance) is None:
        repository.insert_deployment({"id": instance, "asset_id": binding["model_asset_id"], "catalog_key": binding["model_key"], "kind": "image", "label": "fixture", "model_id": "fixture/model", "revision": binding["model_asset_revision"], "manifest_digest": binding["recipe_revision"], "enabled": True, "is_default": False, "gpu_indices": [0, 1], "model_path": "fixture", "license": "fixture", "required_files": ["fixture"], "required_vram_mib": 20, "install_state": "ready", "created_at": "fixture", "updated_at": "fixture"})
    deployment = repository.get_deployment(instance)
    installed = {
        "schema": 1, "instance_id": instance, "incarnation": deployment["incarnation"],
        "operation_id": "install-" + instance, "attempt_id": "attempt-" + instance,
        "release_digest": "a" * 64, "recipe_digest": binding["recipe_revision"],
        "asset_id": binding["model_asset_id"], "asset_revision": binding["model_asset_revision"],
        "asset_manifest_digest": asset_manifest, "catalog_key": binding["model_key"],
        "model_id": deployment["model_id"], "image_digest": "sha256:" + "a" * 64,
        "dependencies": [],
    }
    template = fixture_template()
    with repository._connect() as db:
        db.execute("INSERT OR IGNORE INTO service_installations(id,recipe_key,state,current_step,progress,options_json,steps_json,created_at,updated_at) VALUES(?,?,'ready','complete',1,'{}','[]','fixture','fixture')", (installed["operation_id"], binding["model_key"]))
        db.execute("INSERT OR IGNORE INTO installation_attempts(id,operation_id,generation,state,created_at,updated_at) VALUES(?,?,1,'ready','fixture','fixture')", (installed["attempt_id"], installed["operation_id"]))
        db.execute("INSERT OR IGNORE INTO runtime_release_records(release_digest,release_id,contract_json,approved_at) VALUES(?,'fixture','{}','fixture')", (installed["release_digest"],))
        db.execute("INSERT OR REPLACE INTO instance_installation_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            instance, deployment["incarnation"], installed["operation_id"], installed["attempt_id"],
            installed["release_digest"], installed["recipe_digest"], installed["asset_id"],
            installed["asset_revision"], installed["asset_manifest_digest"],
            json.dumps(installed, sort_keys=True, separators=(",", ":")), digest(installed),
            json.dumps(template, sort_keys=True, separators=(",", ":")), digest(template), "fixture"))
    return binding


def policy_value(binding, gpus=("GPU-one", "GPU-two"), *, backend="container"):
    return {"backend": backend, "residency": "resident", "idle_seconds": 300, "gpus": list(gpus), "base_mib": 10, "task_mib": 20,
            "binding": binding, "package_id": "template_" + digest(fixture_template()), "sharing_mode": "shared", "external_reserve_mib": 0}


def package_identity(value, epoch="epoch-one"):
    return {"package_id": value["package_id"], "binding_digest": digest(value["binding"]), "image_digest": "sha256:" + "a" * 64,
            "capability_digest": digest(worker_capability_for(value["binding"]["model_key"])),
            "runtime_record_id": "record-" + epoch, "epoch": epoch}


def fixture_capacity(policy, *, task=False):
    return {gpu: {"limit": 100000, "free": 100000} for gpu in policy["gpus"]}


def model_event(command, seq, kind, payload=None):
    if payload is None:
        payload = {key: command["payload"][key] for key in ("operation_id", "desired_revision")}
        payload["action"] = command["type"].split(".")[1]
        if payload["action"] == "load":
            payload.update({key: command["payload"][key] for key in ("reservation_id", "reservation_generation")})
        if kind == "model.terminal":
            payload.update(status="succeeded", error_code=None)
    return {"protocol": PROTOCOL, "type": kind, "message_id": "evt-" + command["message_id"] + "-" + str(seq),
            "server_id": command["server_id"], "instance_id": command["instance_id"], "worker_epoch": command["worker_epoch"],
            "correlation_id": "instance", "created_at": "2026-01-01T00:00:00Z", "event_seq": seq, "payload": payload}


def worker_registered_event(authority, instance, epoch, binding, package, gpus):
    return {
        "protocol": PROTOCOL,
        "type": "worker.registered",
        "message_id": f"evt-registered-{instance}-{epoch}",
        "server_id": authority.tasks.server_id,
        "instance_id": instance,
        "worker_epoch": epoch,
        "correlation_id": "instance",
        "created_at": "2026-01-01T00:00:00Z",
        "event_seq": 1,
        "payload": {
            "model_key": binding["model_key"],
            "recipe_revision": binding["recipe_revision"],
            "image_digest": package["image_digest"],
            "capability_digest": package["capability_digest"],
            "gpu_uuids": list(gpus),
        },
    }


def ready_instance(repository, state, instance="instance-one", epoch="epoch-one", binding=None, limits=None, backend="container"):
    binding = seed_deployment(repository, instance, binding)
    authority = InstancePolicy(repository, state)
    value = policy_value(binding, list(limits or {"GPU-one": 100, "GPU-two": 100}), backend=backend)
    authority.configure(instance, value)
    row = authority.desire(instance, "loaded")
    package = package_identity(value)
    authority.package_validator = lambda _db, record_id: package if record_id == package["runtime_record_id"] else None
    authority.claim_container(instance, epoch, expected_version=row["version"], backend=backend, limits=limits or {"GPU-one": 100, "GPU-two": 100}, package_identity=package)
    authority.receive(worker_registered_event(
        authority, instance, epoch, binding, package, value["gpus"]))
    authority.load_model(instance, observe_capacity=fixture_capacity)
    with repository._connect() as db:
        op = db.execute("SELECT operation_id FROM model_operations ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    command = authority.command(op)
    authority.receive(model_event(command, 1, "model.accepted"))
    authority.receive(model_event(command, 2, "model.terminal"))
    for message in state.outbox(replay=True):
        state.mark_delivered(message["message_id"], digest(message))
    return authority


class ResidentPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temp.name) / "state.db")
        self.state = TaskState(self.repository)
        self.authority = InstancePolicy(self.repository, self.state)
        self.binding = seed_deployment(self.repository)
        self.value = policy_value(self.binding)
        self.authority.configure("instance-one", self.value)
        self.row = self.authority.desire("instance-one", "loaded")

    def tearDown(self):
        self.temp.cleanup()

    def claim(self, authority=None, **changes):
        target = authority or self.authority
        identity = package_identity(self.value)
        target.package_validator = lambda _db, record_id: identity if record_id == identity["runtime_record_id"] else None
        values = {"expected_version": self.row["version"], "backend": "container", "limits": {"GPU-one": 100, "GPU-two": 100}, "package_identity": identity, "observe_capacity": fixture_capacity}
        values.update(changes)
        values.pop("observe_capacity", None)
        return target.claim_container("instance-one", "epoch-one", **values)

    def test_claim_reservations_and_command_are_atomic_at_each_write(self):
        for point in ("claim.identity", "claim.command"):
            self.authority.fault = lambda stage: (_ for _ in ()).throw(RuntimeError(stage)) if stage == point else None
            with self.assertRaises(RuntimeError): self.claim()
            with self.repository._connect() as db:
                for table in ("instance_claims", "task_instances", "task_reservations", "task_outbox", "model_operations"):
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)

    def test_second_gpu_failure_rolls_back_first_gpu_and_claim(self):
        claim = self.claim()
        package = package_identity(self.value)
        self.authority.receive(worker_registered_event(
            self.authority, "instance-one", "epoch-one", self.binding,
            package, self.value["gpus"]))
        with self.assertRaisesRegex(TaskStateError, "gpu_capacity_unavailable"):
            self.authority.load_model("instance-one", observe_capacity=lambda policy, task=False: {
                "GPU-one": {"limit": 100, "free": 100},
                "GPU-two": {"limit": 100, "free": 1},
            })
        with self.repository._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM task_reservations").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM model_operations").fetchone()[0], 0)
        self.assertEqual(self.authority.get("instance-one")["claim"]["claim_id"],
                         claim["claim_id"])

    def test_two_connections_only_one_backend_claim(self):
        def run(_):
            try:
                return self.claim(InstancePolicy(Repository(self.repository.path)))["claim_id"]
            except TaskStateError: return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(x is not None for x in pool.map(run, range(2))), 1)
        with self.assertRaises(TaskStateError): self.claim(backend="legacy")

    def test_policy_survives_restart_and_active_claim_defers_structural_change(self):
        self.claim()
        other = InstancePolicy(Repository(self.repository.path))
        self.assertEqual(other.get("instance-one")["policy"], self.value)
        same = other.configure("instance-one", self.value, expected_version=self.row["version"])
        self.assertEqual((same["version"], same["configuration_state"]),
                         (self.row["version"], "applied"))

        changed = copy.deepcopy(self.value)
        changed["sharing_mode"] = "exclusive"
        pending = other.configure("instance-one", changed,
                                  expected_version=self.row["version"])
        self.assertEqual(pending["configuration_state"], "restart_pending")
        self.assertEqual(pending["pending_policy"], changed)
        self.assertEqual(pending["policy"], self.value)
        self.assertEqual(pending["desired_state"], "unloaded")
        with self.repository._connect() as db:
            self.assertEqual(db.execute("SELECT enabled FROM model_deployments WHERE id='instance-one'").fetchone()[0], 0)

        self.assertTrue(other.close_unstarted_claim("instance-one"))
        self.assertTrue(other.apply_pending("instance-one"))
        applying = other.get("instance-one")
        self.assertEqual((applying["configuration_state"], applying["policy"],
                          applying["pending_policy"]),
                         ("applying", changed, self.value))
        applied = other.commit_configuration("instance-one")
        self.assertEqual((applied["configuration_state"], applied["policy"],
                          applied["pending_policy"]),
                         ("applied", changed, None))
        with self.repository._connect() as db:
            self.assertEqual(db.execute("SELECT enabled FROM model_deployments WHERE id='instance-one'").fetchone()[0], 1)

    def test_failed_candidate_rolls_back_previous_ready_policy_after_exit(self):
        self.claim()
        changed = copy.deepcopy(self.value)
        changed["sharing_mode"] = "exclusive"
        pending = self.authority.configure(
            "instance-one", changed, expected_version=self.row["version"])
        self.assertTrue(self.authority.close_unstarted_claim("instance-one"))
        self.assertTrue(self.authority.apply_pending("instance-one"))
        failed = self.authority.begin_configuration_rollback(
            "instance-one", "candidate_health_failed")
        self.assertEqual((failed["configuration_state"], failed["policy"],
                          failed["pending_policy"]),
                         ("failed", changed, self.value))
        self.assertTrue(self.authority.rollback_configuration("instance-one"))
        restored = self.authority.get("instance-one")
        self.assertEqual((restored["configuration_state"], restored["policy"],
                          restored["pending_policy"]),
                         ("applied", self.value, None))
        self.assertEqual(restored["configuration_error"],
                         "rolled_back:candidate_health_failed")

    def test_hot_policy_change_does_not_replace_active_claim(self):
        claim = self.claim()
        package = package_identity(self.value)
        self.authority.receive(worker_registered_event(
            self.authority, "instance-one", "epoch-one", self.binding,
            package, self.value["gpus"]))
        self.authority.load_model("instance-one", observe_capacity=fixture_capacity)
        with self.repository._connect() as db:
            operation = db.execute(
                "SELECT operation_id FROM model_operations WHERE claim_id=?",
                (claim["claim_id"],),
            ).fetchone()[0]
        command = self.authority.command(operation)
        self.assertEqual(command["payload"]["residency"], "resident")
        self.authority.receive(model_event(command, 1, "model.accepted"))
        self.authority.receive(model_event(command, 2, "model.terminal"))
        self.assertEqual(self.authority.get("instance-one")["claim"]["state"], "loaded")

        changed = copy.deepcopy(self.value)
        changed.update(residency="idle", idle_seconds=60)
        applied = self.authority.configure(
            "instance-one", changed, expected_version=self.row["version"])
        self.assertEqual(applied["configuration_state"], "applied")
        self.assertIsNone(applied["pending_policy"])
        self.assertEqual(applied["claim"]["claim_id"], claim["claim_id"])
        self.assertEqual(applied["policy"], changed)
        self.assertEqual(applied["desired_state"], "loaded")

    def test_service_start_requests_container_while_on_demand_model_stays_unloaded(self):
        stopped = self.authority.set_service(
            "instance-one", False, expected_version=self.row["version"])
        self.assertEqual((stopped["desired_state"], stopped["status"]),
                         ("unloaded", "unloaded"))
        with self.repository._connect() as db:
            self.assertEqual(db.execute("SELECT enabled FROM model_deployments WHERE id='instance-one'").fetchone()[0], 0)

        on_demand = copy.deepcopy(self.value)
        on_demand["residency"] = "on_demand"
        configured = self.authority.configure(
            "instance-one", on_demand, expected_version=stopped["version"])
        started = self.authority.set_service(
            "instance-one", True, expected_version=configured["version"])
        self.assertEqual((started["desired_state"], started["status"]),
                         ("unloaded", "waiting_runtime"))
        with self.repository._connect() as db:
            self.assertEqual(db.execute("SELECT enabled FROM model_deployments WHERE id='instance-one'").fetchone()[0], 1)

    def test_krea_resident_intent_reserves_base_and_commands_full_gpu_load(self):
        binding = {
            "model_key": "krea-2-turbo", "recipe_revision": "recipe-v1",
            "model_asset_id": "mdl-krea", "model_asset_revision": "revision-v1",
            "dependencies": [],
        }
        seed_deployment(self.repository, "krea", binding)
        value = policy_value(binding, ("GPU-one",))
        row = self.authority.configure("krea", value)
        row = self.authority.desire("krea", "loaded", expected_version=row["version"])
        package = package_identity(value, "epoch-krea")
        self.authority.package_validator = lambda _db, record_id: (
            package if record_id == package["runtime_record_id"] else None)
        claim = self.authority.claim_container(
            "krea", "epoch-krea", expected_version=row["version"],
            backend="container", limits={"GPU-one": 100},
            package_identity=package,
        )
        self.assertEqual(claim["state"], "claimed")
        self.authority.receive(worker_registered_event(
            self.authority, "krea", "epoch-krea", binding, package,
            value["gpus"]))
        current = self.authority.get("krea")
        self.assertIsNone(current["error_code"])
        loading = self.authority.load_model("krea", observe_capacity=fixture_capacity)
        self.assertEqual(loading["state"], "loading")
        with self.repository._connect() as db:
            operation = db.execute(
                "SELECT operation_id FROM model_operations WHERE claim_id=?",
                (claim["claim_id"],),
            ).fetchone()[0]
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM task_reservations WHERE instance_id='krea'"
            ).fetchone()[0], 1)
        self.assertEqual(self.authority.command(operation)["payload"]["residency"],
                         "resident")

    def test_policy_cannot_select_a_foreign_asset_or_package(self):
        altered = copy.deepcopy(self.value); altered["binding"]["model_asset_id"] = "foreign"
        with self.assertRaisesRegex(TaskStateError, "deployment_binding_mismatch"):
            self.authority.configure("instance-one", altered, expected_version=self.row["version"])
        with self.assertRaisesRegex(TaskStateError, "runtime_package_binding_mismatch"):
            self.claim(package_identity={"package_id": "fake", "binding_digest": digest(self.binding)})

    def test_runtime_exit_restarts_only_when_operator_enabled_recovery(self):
        configured = self.authority.configure("instance-one", self.value,
                                              expected_version=self.row["version"], restart_recovery=True)
        self.row = self.authority.desire("instance-one", "loaded", expected_version=configured["version"])
        claim = self.claim()
        self.authority.require_stop("instance-one", claim["epoch"], "runtime_exited")
        recovered = self.authority.get("instance-one")
        self.assertTrue(recovered["restart_recovery"])
        self.assertEqual((recovered["desired_state"], recovered["status"]), ("loaded", "quarantined"))

        other_repo = Repository(Path(self.temp.name) / "disabled.db")
        other_state = TaskState(other_repo)
        binding = seed_deployment(other_repo, "disabled-instance")
        other = InstancePolicy(other_repo, other_state)
        other.configure("disabled-instance", policy_value(binding), restart_recovery=False)
        row = other.desire("disabled-instance", "loaded")
        disabled_package = package_identity(policy_value(binding), "disabled-epoch")
        other.package_validator = lambda _db, record_id: disabled_package if record_id == disabled_package["runtime_record_id"] else None
        claim = other.claim_container("disabled-instance", "disabled-epoch", expected_version=row["version"],
                                 backend="container", limits={"GPU-one": 100, "GPU-two": 100},
                                 package_identity=disabled_package)
        other.require_stop("disabled-instance", claim["epoch"], "runtime_exited")
        self.assertEqual(other.get("disabled-instance")["desired_state"], "unloaded")

    def test_operator_stop_closes_never_started_claim_and_releases_base_atomically(self):
        claim = self.claim()
        stopped = self.authority.stop("instance-one", expected_version=self.row["version"])
        self.assertEqual((stopped["desired_state"], stopped["status"], stopped["claim"]),
                         ("unloaded", "unloaded", None))
        with self.repository._connect() as db:
            self.assertEqual(db.execute("SELECT state FROM instance_claims WHERE claim_id=?", (claim["claim_id"],)).fetchone()[0], "exited")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM task_reservations WHERE instance_id='instance-one' AND released=0").fetchone()[0], 0)
        self.assertIsNone(InstancePolicy(Repository(self.repository.path)).get("instance-one")["claim"])
        with self.assertRaisesRegex(TaskStateError, "instance_version_conflict"):
            self.authority.stop("instance-one", expected_version=self.row["version"])

    def test_operator_stop_fences_bound_or_unknown_execution_instead_of_assuming_never_started(self):
        claim = self.claim()
        self.authority.bind_execution(claim["claim_id"], {"intent_id": "intent-unknown"})
        stopped = self.authority.stop("instance-one", expected_version=self.row["version"])
        self.assertEqual((stopped["desired_state"], stopped["status"], stopped["claim"]["state"], stopped["claim"]["stop_reason"]),
                         ("unloaded", "quarantined", "quarantined", "operator_stop"))
        with self.repository._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM task_reservations WHERE instance_id='instance-one' AND released=0").fetchone()[0], 0)


if __name__ == "__main__": unittest.main()
