from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mediacenter.model_assets import ModelAssetManager
from mediacenter.model_deployments import ModelDeploymentManager
from mediacenter.repository import Repository
from mediacenter.model_registry import ModelRegistry
from tests.test_model_assets import safetensors_bytes
from mediacenter.service_installer import ServiceInstaller, ServiceInstallerError


REVISION = "a" * 40


class ServiceInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = Repository(self.root / "state.db")
        self.catalog = self.root / "catalog.json"
        files = {"model_index.json": b"{}", "config.json": b"{}",
                 "unet/model.safetensors": safetensors_bytes()}
        dependent_files = {"model_index.json": b'{"dependent":true}',
                           "config.json": b'{"dependent":true}',
                           "unet/model.safetensors": safetensors_bytes()}
        self.catalog.write_text(json.dumps({"models": [{
            "catalog_key": "test-image", "kind": "image", "label": "Test Image",
            "model_id": "official/test-image", "license": "Test License",
            "recommended_revision": REVISION,
            "download_profile": "immutable-service-recipe", "download_allow_patterns": ["*"],
            "required_files": ["model_index.json", "config.json", "unet/model.safetensors"],
            "required_vram_mib": 1024, "min_gpus": 1, "max_gpus": 1,
            "recommended_gpus": [0],
            "service_recipe": {"description": "one click", "role": "checkpoint",
                               "format": "diffusers", "source_type": "huggingface",
                               "files": [{"relative_path": path, "byte_size": len(data),
                                          "sha256": hashlib.sha256(data).hexdigest(),
                                          "url": f"https://models.example/{path}"}
                                         for path, data in files.items()]},
        }, {
            "catalog_key": "dependent-image", "kind": "image", "label": "Dependent Image",
            "model_id": "official/dependent-image", "license": "Dependent License",
            "recommended_revision": "b" * 40,
            "download_profile": "immutable-service-recipe", "download_allow_patterns": ["*"],
            "required_files": ["model_index.json", "config.json", "unet/model.safetensors"],
            "required_vram_mib": 1024, "min_gpus": 1, "max_gpus": 1,
            "recommended_gpus": [0],
            "service_recipe": {"description": "depends on test image", "role": "checkpoint",
                               "format": "diffusers", "source_type": "huggingface",
                               "prerequisites": ["test-image"],
                               "files": [{"relative_path": path, "byte_size": len(data),
                                          "sha256": hashlib.sha256(data).hexdigest(),
                                          "url": f"https://models.example/dependent/{path}"}
                                         for path, data in dependent_files.items()]},
        }]}), encoding="utf-8")
        self.assets = ModelAssetManager(self.repository, self.root / "model-store")
        self.deployments = ModelDeploymentManager(
            self.repository, self.catalog, self.root, (0,), model_store_root=self.root / "model-store")
        self.installer = ServiceInstaller(
            self.repository, self.catalog, self.assets, self.deployments,
            lambda _kind, _deployment: (True, "ready"), poll_seconds=0.01,
            prepare_timeout_seconds=2)
        from tests.test_container_installer import attach_runtime_fixture
        attach_runtime_fixture(self)

    def tearDown(self) -> None:
        for attempt_id in list(self.installer._threads):
            self.assertTrue(self.installer.wait_runner(attempt_id, 5), "installer did not exit")
        self.temporary.cleanup()

    def ready_asset(self, revision: str = REVISION) -> str:
        payloads = ({"model_index.json": b"{}", "config.json": b"{}",
                     "unet/model.safetensors": safetensors_bytes()}
                    if revision == REVISION else
                    {"model_index.json": b'{"dependent":true}',
                     "config.json": b'{"dependent":true}',
                     "unet/model.safetensors": safetensors_bytes()})
        transfer = self.assets.create_upload({
            "display_name": "Test Image", "media_kind": "image", "role": "checkpoint",
            "format": "diffusers", "revision": revision, "license_declared": "Test License" if revision == REVISION else 'Dependent License',
            "files": [{"relative_path": path, "byte_size": len(data),
                       "sha256": hashlib.sha256(data).hexdigest()}
                      for path, data in payloads.items()],
        })
        for file in transfer["files"]:
            self.assets.append_upload_chunk(transfer["id"], file["id"], 0,
                                            payloads[file["relative_path"]])
        asset_id = self.assets.complete_upload(transfer["id"])["asset_id"]
        return asset_id

    def wait_terminal(self, installation_id: str) -> dict:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = self.installer.get(installation_id)
            if current["state"] in {"ready", "failed", "canceled", "paused"}:
                self.assertTrue(self.installer.wait_runner(current["current_attempt_id"], 5), "installer did not exit")
                return current
            time.sleep(0.01)
        self.fail("installation did not finish")

    def test_one_click_uses_existing_asset_prepares_deployment_and_records_steps(self) -> None:
        asset_id = self.ready_asset()
        created = self.installer.start({"recipe_key": "test-image", "gpu_indices": [0],
                                        "license_accepted": True})
        finished = self.wait_terminal(created["id"])
        self.assertEqual((finished["state"], finished["asset_id"], finished["deployment_id"]),
                         ("ready", asset_id, "test-image"))
        deployment = self.deployments.get("test-image")
        self.assertEqual((deployment["install_state"], deployment["gpu_indices"], deployment["enabled"]),
                         ("ready", [0], False))
        self.assertTrue(all(step["state"] == "succeeded" for step in finished["steps"]))
        self.assertEqual(self.installer.catalog()[0]["state"], "installed")
        with self.repository._connect() as db:
            db.execute("UPDATE model_asset_files SET sha256=? WHERE asset_id=? AND relative_path=?",
                       ("0" * 64, asset_id, "config.json"))
        self.assertNotEqual(self.installer.catalog()[0]["state"], "installed")
        with self.repository._connect() as db:
            db.execute("UPDATE model_asset_files SET sha256=? WHERE asset_id=? AND relative_path=?",
                       (hashlib.sha256(b"{}").hexdigest(), asset_id, "config.json"))
            db.execute("INSERT INTO model_asset_files(asset_id,relative_path,sha256,byte_size,storage_relpath) VALUES(?,?,?,?,?)",
                       (asset_id, "unexpected.bin", hashlib.sha256(b"x").hexdigest(), 1,
                        "assets/test/unexpected.bin"))
        self.assertNotEqual(self.installer.catalog()[0]["state"], "installed")

    def test_license_gpu_and_duplicate_installations_fail_closed(self) -> None:
        with self.assertRaisesRegex(ServiceInstallerError, "许可证"):
            self.installer.start({"recipe_key": "test-image", "gpu_indices": [0]})
        with self.assertRaises(ServiceInstallerError) as blocked:
            self.installer.start({"recipe_key": "test-image", "gpu_indices": [1],
                                  "license_accepted": True})
        self.assertEqual(blocked.exception.code, "gpu_out_of_pool")
        self.ready_asset()
        first = self.installer.start({"recipe_key": "test-image", "gpu_indices": [0],
                                      "license_accepted": True})
        self.wait_terminal(first["id"])
        with self.assertRaisesRegex(ServiceInstallerError, "已经部署"):
            self.installer.start({"recipe_key": "test-image", "gpu_indices": [0],
                                  "license_accepted": True})

    def test_gated_recipe_reports_auth_and_reuses_verified_asset_without_token(self) -> None:
        document = json.loads(self.catalog.read_text(encoding="utf-8"))
        document["models"][0]["service_recipe"]["authentication"] = {
            "provider": "huggingface", "host": "huggingface.co", "required": True,
            "terms_url": "https://huggingface.co/official/test-image",
        }
        document["models"][0]["service_recipe"]["files"] = [
            {**item, "url": item["url"].replace("models.example", "huggingface.co")}
            for item in document["models"][0]["service_recipe"]["files"]
        ]
        self.catalog.write_text(json.dumps(document), encoding="utf-8")
        self.installer = ServiceInstaller(
            self.repository, self.catalog, self.assets, self.deployments,
            lambda _kind, _deployment: (True, "ready"), poll_seconds=0.01,
            prepare_timeout_seconds=2)
        from tests.test_container_installer import attach_runtime_fixture
        attach_runtime_fixture(self)

        item = self.installer.catalog()[0]
        self.assertEqual(item["authentication"], {
            "provider": "huggingface", "required": True, "configured": False,
            "needed": True, "terms_url": "https://huggingface.co/official/test-image",
        })
        with self.assertRaises(ServiceInstallerError) as blocked:
            self.installer.start({"recipe_key": "test-image", "gpu_indices": [0],
                                  "license_accepted": True})
        self.assertEqual(blocked.exception.code, "source_authentication_required")
        self.assertIn("客户端完成", blocked.exception.message)
        self.assertEqual(self.repository.list_service_installations(), [])

        self.ready_asset()
        self.assertFalse(self.installer.catalog()[0]["authentication"]["needed"])
        operation = self.installer.start({
            "recipe_key": "test-image", "gpu_indices": [0], "license_accepted": True})
        self.assertEqual(self.wait_terminal(operation["id"])["state"], "ready")

    def test_uninstall_detaches_service_but_preserves_assets_cache_and_history(self) -> None:
        asset_id = self.ready_asset()
        created = self.installer.start({"recipe_key": "test-image", "gpu_indices": [0],
                                        "license_accepted": True})
        finished = self.wait_terminal(created["id"])
        with self.repository._connect() as db:
            cache_before = db.execute("SELECT COUNT(*) FROM runtime_image_bindings").fetchone()[0]
        removed = []
        self.installer.on_uninstall = removed.append

        result = self.installer.uninstall("test-image")

        self.assertEqual(removed, ["test-image"])
        self.assertEqual(result["state"], "available")
        self.assertEqual(result["retained_deployment_id"], "test-image")
        deployment = self.repository.get_deployment("test-image")
        self.assertEqual((deployment["install_state"], deployment["enabled"], deployment["is_default"]),
                         ("configured", False, False))
        self.assertEqual(self.repository.get_model_asset(asset_id)["state"], "ready")
        self.assertEqual(self.installer.get(finished["id"])["state"], "ready")
        with self.repository._connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM instance_installation_bindings WHERE instance_id='test-image'"
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM runtime_image_bindings"
            ).fetchone()[0], cache_before)

        reinstalled = self.installer.start({
            "recipe_key": "test-image", "gpu_indices": [0], "license_accepted": True,
            "adopt_existing": True,
        })
        self.assertEqual(self.wait_terminal(reinstalled["id"])["state"], "ready")
        self.assertEqual(self.installer.catalog()[0]["state"], "installed")

    def test_uninstall_rejects_active_tasks_runtime_and_downstream_dependencies(self) -> None:
        self.ready_asset()
        base = self.installer.start({"recipe_key": "test-image", "gpu_indices": [0],
                                     "license_accepted": True})
        self.wait_terminal(base["id"])
        with self.repository._connect() as db:
            db.execute("UPDATE instance_policies SET desired_state='loaded' WHERE instance_id='test-image'")
            db.execute("UPDATE model_deployments SET desired_state='loaded' WHERE id='test-image'")
        with self.assertRaises(ServiceInstallerError) as runtime_blocked:
            self.installer.uninstall("test-image")
        self.assertEqual(runtime_blocked.exception.code, "service_runtime_active")
        with self.repository._connect() as db:
            db.execute("UPDATE instance_policies SET desired_state='unloaded' WHERE instance_id='test-image'")
            db.execute("UPDATE model_deployments SET desired_state='unloaded' WHERE id='test-image'")
            db.execute(
                """INSERT INTO tasks(id,service,model_key,status,prompt,options_json,inputs_json,
                   progress,stage,cancel_requested,created_at,updated_at)
                   VALUES('task-active','image','test-image','queued','test','{}','[]',0,'queued',0,'now','now')"""
            )
        with self.assertRaises(ServiceInstallerError) as task_blocked:
            self.installer.uninstall("test-image")
        self.assertEqual(task_blocked.exception.code, "service_tasks_active")
        with self.repository._connect() as db:
            db.execute("UPDATE tasks SET status='canceled' WHERE id='task-active'")

        self.ready_asset("b" * 40)
        dependent = self.installer.start({"recipe_key": "dependent-image", "gpu_indices": [0],
                                          "license_accepted": True})
        self.wait_terminal(dependent["id"])
        with self.assertRaises(ServiceInstallerError) as dependency_blocked:
            self.installer.uninstall("test-image")
        self.assertEqual(dependency_blocked.exception.code, "service_dependency_in_use")
        self.assertEqual(self.installer.uninstall("dependent-image")["state"], "available")
        self.assertEqual(self.installer.uninstall("test-image")["state"], "available")

    def test_prerequisite_is_visible_and_blocks_install_until_exact_service_is_installed(self) -> None:
        dependent = next(item for item in self.installer.catalog()
                         if item["recipe_key"] == "dependent-image")
        self.assertFalse(dependent["prerequisites_ready"])
        self.assertEqual(dependent["prerequisites"], [{
            "recipe_key": "test-image", "label": "Test Image", "installed": False,
        }])
        with self.assertRaises(ServiceInstallerError) as blocked:
            self.installer.start({"recipe_key": "dependent-image", "gpu_indices": [0],
                                  "license_accepted": True})
        self.assertEqual(blocked.exception.code, "service_prerequisite_missing")
        self.assertEqual(self.repository.list_service_installations(), [])

        self.ready_asset()
        installed = self.installer.start({"recipe_key": "test-image", "gpu_indices": [0],
                                          "license_accepted": True})
        self.wait_terminal(installed["id"])
        dependent = next(item for item in self.installer.catalog()
                         if item["recipe_key"] == "dependent-image")
        self.assertTrue(dependent["prerequisites_ready"])
        self.assertTrue(dependent["prerequisites"][0]["installed"])
        self.ready_asset("b" * 40)
        created = self.installer.start({"recipe_key": "dependent-image", "gpu_indices": [0],
                                        "license_accepted": True})
        finished = self.wait_terminal(created["id"])
        self.assertEqual(finished["state"], "ready")
        deployment = self.repository.get_deployment("dependent-image")
        self.assertEqual(deployment["dependencies"][0]["deployment_id"], "test-image")
        dependent = next(item for item in self.installer.catalog()
                         if item["recipe_key"] == "dependent-image")
        self.assertEqual(dependent["state"], "installed")

    def test_environment_failure_removes_only_new_deployment_metadata(self) -> None:
        self.ready_asset()
        from mediacenter.container_releases import RuntimeContractError
        def fail_commit(point):
            if point == 'binding.insert': raise RuntimeContractError('fixture_binding_failed')
        self.installer.installation_runtime.fault = fail_commit
        try:
            created = self.installer.start({"recipe_key": "test-image", "gpu_indices": [0],
                                            "license_accepted": True})
            finished = self.wait_terminal(created["id"])
        finally:
            self.installer.installation_runtime.fault = lambda _: None
        self.assertEqual((finished["state"], finished["error_code"]),
                         ("failed", "fixture_binding_failed"))
        self.assertIsNone(self.repository.get_deployment("test-image"))
        self.assertEqual(len(self.repository.list_model_assets()), 1)



if __name__ == '__main__':
    unittest.main()
