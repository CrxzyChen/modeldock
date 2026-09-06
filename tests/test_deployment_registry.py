from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mediacenter.model_deployments import ModelDeploymentManager
from mediacenter.model_registry import ModelRegistry
from mediacenter.repository import Repository
from tests.test_service_installer import ServiceInstallerTests


class DeploymentRegistryTests(unittest.TestCase):
    def test_installed_stopped_service_is_valid_but_not_open_for_task_admission(self):
        fixture = ServiceInstallerTests(); fixture.setUp()
        try:
            fixture.ready_asset()
            operation = fixture.installer.start({
                "recipe_key": "test-image", "gpu_indices": [0], "license_accepted": True})
            self.assertEqual(fixture.wait_terminal(operation["id"])["state"], "ready")
            registry = ModelRegistry(
                fixture.deployments,
                installation_runtime=fixture.installer.installation_runtime)
            spec = registry.specs["test-image"]
            self.assertFalse(spec.health()[0])
            self.assertEqual(spec.health(allow_disabled=True),
                             (True, "installed; runtime checks are separately reported"))
            self.assertNotIn("python", spec.__dict__)
            self.assertNotIn("module", spec.__dict__)
            self.assertNotIn("runtime_binding", spec.__dict__)
        finally:
            fixture.tearDown()

    def test_catalog_rejects_retired_host_runtime_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); repository = Repository(root / "state.db")
            source = json.loads((Path(__file__).resolve().parents[1] / "deploy" /
                                 "model_catalog.json").read_text(encoding="utf-8"))
            item = dict(source["models"][0]); item["environment"] = "host-env"
            catalog = root / "catalog.json"
            catalog.write_text(json.dumps({"models": [item]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "retired host runtime fields"):
                ModelDeploymentManager(repository, catalog, root, (0,))

    def test_repository_migrates_legacy_metadata_without_touching_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"; repository = Repository(database)
            with repository._connect() as db:
                db.execute("ALTER TABLE model_deployments ADD COLUMN python_path TEXT")
                db.execute("ALTER TABLE model_deployments ADD COLUMN module TEXT")
                db.execute("ALTER TABLE model_deployments ADD COLUMN extension TEXT")
                db.execute("ALTER TABLE model_deployments ADD COLUMN probe_enabled INTEGER")
                db.execute("CREATE TABLE model_deployment_runtimes(deployment_id TEXT PRIMARY KEY)")
                db.execute("CREATE TABLE environment_preparations(environment_key TEXT PRIMARY KEY)")
            migrated = Repository(database)
            with migrated._connect() as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(model_deployments)")}
                tables = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                resources = db.execute(
                    "SELECT sql FROM sqlite_master WHERE name='installation_resources'").fetchone()[0]
            self.assertTrue({"python_path", "module", "extension", "probe_enabled"}.isdisjoint(columns))
            self.assertNotIn("model_deployment_runtimes", tables)
            self.assertNotIn("environment_preparations", tables)
            self.assertNotIn("'environment'", resources)

    def test_production_catalog_has_fifteen_container_only_contracts(self):
        catalog = json.loads((Path(__file__).resolve().parents[1] / "deploy" /
                              "model_catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(len(catalog["models"]), 15)
        retired = {"environment", "environment_packages", "module", "probe", "runtime_recipe"}
        for item in catalog["models"]:
            with self.subTest(model=item["catalog_key"]):
                self.assertTrue(retired.isdisjoint(item))
                self.assertTrue(item["worker_contract"]["module"].startswith("mediacenter.adapters."))


if __name__ == "__main__":
    unittest.main()
