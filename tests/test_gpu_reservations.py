from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from mediacenter.gpu_scheduler import GPULeaseScheduler
from mediacenter.instance_policy import InstancePolicy
from mediacenter.repository import Repository
from mediacenter.task_state import TaskState, TaskStateError, canonical, digest
from tests.test_resident_policy import (
    seed_deployment, policy_value, package_identity, model_event,
    fixture_template, worker_registered_event,
)


class Inventory:
    def __init__(self, total=10000, used=3000):
        self.total, self.used = total, used
    def gpu_snapshot(self, **kwargs):
        return {"available": True, "items": [{"uuid": "GPU-one", "index": 0,
            "memory_used_mib": self.used, "memory_total_mib": self.total, "processes": []}]}


class ReservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.temp.name) / "state.db")
        self.state = TaskState(self.repo)
        self.authority = InstancePolicy(self.repo, self.state)
        self.packages = {}
        self.authority.package_validator = lambda _db, record_id: self.packages.get(record_id)
        self.inventory = Inventory()
        self.scheduler = GPULeaseScheduler((0,), authority=self.authority, gpu_uuids=("GPU-one",),
            hardware=self.inventory, memory_provider=lambda: SimpleNamespace(available_bytes=64*1024**3, pressure_avg60=0))
    def tearDown(self): self.temp.cleanup()
    def configure(self, name, base=3500, task=500, sharing="shared"):
        binding = seed_deployment(self.repo, name)
        value = policy_value(binding, ("GPU-one",))
        value.update(base_mib=base, task_mib=task, sharing_mode=sharing)
        template = fixture_template()
        template["resources"].update(base_mib=base, task_mib=task, sharing_mode=sharing)
        value["package_id"] = "template_" + digest(template)
        with self.repo._connect() as db:
            db.execute("UPDATE instance_installation_bindings SET template_json=?,template_digest=? WHERE instance_id=?",
                       (canonical(template), digest(template), name))
        self.authority.configure(name, value)
        self.authority.desire(name, "loaded")
        package = package_identity(value, "epoch-" + name)
        self.packages[package["runtime_record_id"]] = package
        return package
    def load(self, name, package):
        claim = self.scheduler.start_container(name, package)
        policy = self.authority.get(name)["policy"]
        self.authority.receive(worker_registered_event(
            self.authority, name, claim["epoch"], policy["binding"], package,
            policy["gpus"],
        ))
        return self.scheduler.load_model(name)
    def loaded(self, claim):
        with self.repo._connect() as db:
            op = db.execute("SELECT operation_id FROM model_operations WHERE claim_id=?", (claim["claim_id"],)).fetchone()[0]
        cmd = self.authority.command(op)
        self.authority.receive(model_event(cmd, 1, "model.accepted"))
        self.authority.receive(model_event(cmd, 2, "model.terminal"))
        return cmd
    def task(self, name):
        binding = self.authority.get(name)["policy"]["binding"]
        return self.state.accept({"service":"image", "model":name, "prompt":"test", "options":{}, "inputs":[]}, scope="test", binding=binding)[0]
    def test_pending_loads_are_charged_atomically(self):
        packages = {name:self.configure(name) for name in ("one", "two")}
        def load(name):
            try: return self.load(name, packages[name])
            except TaskStateError: return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(x is not None for x in pool.map(load, packages)), 1)
        with self.repo._connect() as db:
            self.assertEqual(db.execute("SELECT SUM(mib) FROM task_reservations WHERE released=0").fetchone()[0],3500)
    def test_warm_large_base_is_not_double_charged(self):
        self.inventory.total, self.inventory.used = 49140, 0
        package = self.configure("one", base=30000, task=8000)
        self.loaded(self.load("one", package))
        self.inventory.used = 30000
        self.scheduler.dispatch(self.task("one"))
        with self.repo._connect() as db:
            self.assertEqual(db.execute("SELECT SUM(mib) FROM task_reservations WHERE released=0").fetchone()[0],38000)
    def test_parallel_unmaterialized_tasks_not_oversold(self):
        self.inventory.used = 0
        for name in ("one", "two"):
            self.loaded(self.load(name,self.configure(name,base=1000,task=3500)))
        self.inventory.used = 3000
        tasks = [self.task(name) for name in ("one","two")]
        def dispatch(task):
            try:return self.scheduler.dispatch(task)
            except TaskStateError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(x is not None for x in pool.map(dispatch,tasks)),1)
    def test_existing_exclusive_blocks_new_shared(self):
        self.load("one",self.configure("one",base=100,sharing="exclusive"))
        with self.assertRaisesRegex(TaskStateError,"gpu_exclusive_reservation"):
            self.load("two",self.configure("two",base=100))
    def test_terminal_cannot_be_replaced_by_later_success(self):
        claim = self.load("one",self.configure("one",base=100))
        with self.repo._connect() as db:
            op=db.execute("SELECT operation_id FROM model_operations").fetchone()[0]
        cmd=self.authority.command(op)
        self.authority.receive(model_event(cmd,1,"model.accepted"))
        failed=model_event(cmd,2,"model.terminal")
        failed["payload"].update(status="failed",error_code="fixture_failure")
        self.authority.receive(failed)
        self.assertEqual(self.authority.receive(failed),"applied")
        with self.assertRaisesRegex(TaskStateError,"model_operation_terminal_conflict"):
            self.authority.receive(model_event(cmd,3,"model.terminal"))
        self.assertEqual(self.authority.get("one")["claim"]["state"],"quarantined")


if __name__ == "__main__": unittest.main()
