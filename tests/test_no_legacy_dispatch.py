from __future__ import annotations

import json
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LegacyDispatchRetirementTests(unittest.TestCase):
    def test_host_stdin_dispatcher_is_unconditionally_retired(self):
        self.assertEqual(list((ROOT / "mediacenter/drivers").glob("*.py")), [])

    def test_legacy_worker_pool_module_is_absent(self):
        self.assertFalse((ROOT / "mediacenter/worker_pool.py").exists())
        source = (ROOT / "mediacenter/resource_observer.py").read_text(encoding="utf-8")
        self.assertIn("linux_memory_state", source)

    def test_host_environment_installer_is_hard_retired(self):
        self.assertFalse((ROOT / "scripts/install_drivers.sh").exists())
        self.assertFalse((ROOT / "mediacenter/runtime_recipes.py").exists())
        deployments = (ROOT / "mediacenter/model_deployments.py").read_text(encoding="utf-8")
        for forbidden in ("subprocess", "RuntimeRecipe", "python_path", "prepare("):
            self.assertNotIn(forbidden, deployments)

    def test_every_catalog_model_has_a_fixed_container_worker_contract(self):
        catalog = json.loads((ROOT / "deploy/model_catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(len(catalog["models"]), 15)
        for model in catalog["models"]:
            with self.subTest(model=model["catalog_key"]):
                worker = model["worker_contract"]
                self.assertEqual(worker["schema"], 1)
                self.assertTrue(worker["release"])
                self.assertTrue(worker["module"].startswith("mediacenter.adapters."))
                self.assertTrue(worker["class"].endswith("Adapter"))

    def test_kernel_composition_has_no_legacy_executor(self):
        kernel = (ROOT / "mediacenter/kernel.py").read_text(encoding="utf-8")
        config = (ROOT / "mediacenter/config.py").read_text(encoding="utf-8")
        self.assertNotIn("legacy_pool", kernel)
        self.assertNotIn("WorkerPool", kernel)
        self.assertNotIn("legacy_runtime_package_retired", config)
        self.assertNotIn('"packages"', config.split("def load_runtime_configuration", 1)[1])

    def test_final_release_preflight_is_read_only_and_resource_gated(self):
        location = ROOT / "scripts/prepare_container_release.py"
        spec = importlib.util.spec_from_file_location("mc044_preflight", location)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.preflight(ROOT, ROOT / "deploy/container-release-plan.json", None)
        self.assertEqual(result["models"], 14)
        self.assertEqual(result["mutations_performed"], [])
        self.assertFalse(result["production_ready"])
        self.assertIn("gpu_uuids", result["missing_resource_inputs"])
        self.assertEqual(len(result["unbuilt_release_metadata"]), 6)


if __name__ == "__main__":
    unittest.main()
