from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mediacenter.instance_policy import InstancePolicy
from mediacenter.repository import Repository
from mediacenter.service_center import ServiceCenter, ServiceCenterError
from tests.test_resident_policy import policy_value, seed_deployment


class _Registry:
    def __init__(self):
        self.refreshes = 0

    def refresh(self):
        self.refreshes += 1


class InstanceLifecycleApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temporary.name) / "state.db")
        binding = seed_deployment(self.repository)
        self.authority = InstancePolicy(self.repository)
        self.policy = self.authority.configure("instance-one", policy_value(binding))
        self.registry = _Registry()
        self.center = object.__new__(ServiceCenter)
        self.center.repository = self.repository
        self.center.runtime = SimpleNamespace(authority=self.authority)
        self.center.registry = self.registry

    def tearDown(self):
        self.temporary.cleanup()

    def test_start_and_stop_control_admission_without_claiming_completion(self):
        stopped = self.center.stop_deployment(
            "instance-one", {"version": self.policy["version"]})
        self.assertEqual((stopped["service_state"], stopped["accepting_tasks"],
                          stopped["desired_service_state"], stopped["container_state"]),
                         ("stopped", False, "stopped", "configured"))

        started = self.center.start_deployment(
            "instance-one", {"version": stopped["version"]})
        self.assertEqual((started["service_state"], started["accepting_tasks"],
                          started["desired_service_state"], started["actual_state"]),
                         ("starting", True, "started", "waiting_runtime"))
        self.assertEqual(self.registry.refreshes, 2)

        with self.assertRaisesRegex(ServiceCenterError, "实例设置未提交|服务停止意图未提交") as stale:
            self.center.stop_deployment("instance-one", {"version": stopped["version"]})
        self.assertEqual(stale.exception.status, 409)

    def test_version_is_required_for_lifecycle_cas(self):
        for action in (self.center.start_deployment, self.center.stop_deployment):
            with self.assertRaises(ServiceCenterError) as invalid:
                action("instance-one", {})
            self.assertEqual((invalid.exception.code, invalid.exception.status),
                             ("invalid_instance_version", 400))

    def test_krea_full_gpu_residency_can_be_configured_explicitly(self):
        repository = Repository(Path(self.temporary.name) / "offloaded.db")
        binding = {
            "model_key": "krea-2-turbo",
            "recipe_revision": "recipe-v1",
            "model_asset_id": "mdl-krea",
            "model_asset_revision": "revision-v1",
            "dependencies": [],
        }
        seed_deployment(repository, "krea", binding)
        authority = InstancePolicy(repository)
        current = authority.configure("krea", policy_value(binding))
        center = object.__new__(ServiceCenter)
        center.repository = repository
        center.runtime = SimpleNamespace(
            authority=authority,
            scheduler=SimpleNamespace(allowed_uuids=("GPU-one", "GPU-two")),
        )
        center.gpu_scheduler = SimpleNamespace(validate_budget=lambda _policy: None)
        center.registry = _Registry()
        payload = {
            "version": current["version"],
            "gpu_uuids": ["GPU-one", "GPU-two"],
            "sharing_mode": "shared",
            "external_reserve_mib": 2048,
            "residency": "resident",
            "idle_minutes": 5,
            "restart_recovery": False,
        }
        configured = center.configure_instance_policy("krea", payload)
        self.assertEqual(configured["policy"]["residency"], "resident")
        self.assertEqual(configured["policy"]["gpus"], ["GPU-one", "GPU-two"])

    def test_server_exposes_only_container_lifecycle_and_policy_commands(self):
        source = (Path(__file__).resolve().parents[1] / "mediacenter/server.py").read_text(
            encoding="utf-8")
        for endpoint in ('endswith("/start")', 'endswith("/stop")',
                         'endswith("/policy")'):
            self.assertIn(endpoint, source)
        for retired in ('endswith("/load")', 'endswith("/unload")'):
            self.assertNotIn(retired, source)


if __name__ == "__main__":
    unittest.main()
