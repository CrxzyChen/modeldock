from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
import time
import types
from dataclasses import replace
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from mediacenter.repository import InstallationOwnershipError, Repository
from mediacenter.model_deployments import DeploymentError, ModelDeploymentManager
from mediacenter.model_registry import ModelRegistry
from mediacenter.domain import ServiceKind
from mediacenter.service_installer import ServiceInstaller, ServiceInstallerError, utc_now
from tests import test_service_installer as service_fixture


class InstallationOwnershipTests(unittest.TestCase):
    setUp = service_fixture.ServiceInstallerTests.setUp
    ready_asset = service_fixture.ServiceInstallerTests.ready_asset

    def tearDown(self):
        deadline = time.monotonic() + 5
        while (self.installer._running or self.assets._downloading) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.installer._running, "installer did not exit")
        self.assertFalse(self.assets._downloading, "downloader did not exit")
        service_fixture.ServiceInstallerTests.tearDown(self)

    def test_changed_recipe_never_wakes_previous_download(self):
        operation = self.operation()
        owner = self.claim(operation)
        transfer = self.transfer(owner)
        self.fail_operation(operation, owner)
        self.repository.release_installation_runner(owner)
        with patch.object(self.installer, "_spawn"):
            self.installer.control(operation["id"], "retry")
        self.installer.recipes["test-image"]["recommended_revision"] = "different-revision"
        with patch.object(self.assets, "wake_installation_download") as wake:
            self.installer._run_guarded(operation["id"])
        wake.assert_not_called()
        current = self.installer.get(operation["id"])
        self.assertEqual(current["error_code"], "installation_recipe_changed")
        self.assertEqual(current["transfer_id"], transfer["id"])

    def operation(self):
        with patch.object(self.installer, "_spawn"):
            return self.installer.start({"recipe_key": "test-image", "gpu_indices": [0], "license_accepted": True})

    def claim(self, operation):
        return self.repository.claim_installation_runner(operation["id"], operation["current_attempt_id"])

    def fail_operation(self, operation, owner):
        self.installer._set(operation["id"], owner=owner, state="failed", step="failed", progress=0.5,
                            error_code="test_failure", error_message="fixture")

    def create_deployment(self, asset_id, *, owner=None, deployment_id="test-image"):
        return self.deployments.create({"deployment_id": deployment_id, "asset_id": asset_id,
                                        "catalog_key": "test-image", "gpu_indices": [0], "enabled": True}, owner=owner)

    def transfer(self, owner):
        entry = self.installer.recipes["test-image"]
        with patch("mediacenter.model_assets.validate_https_url"), patch.object(self.assets, "_start_download"):
            return self.assets.create_catalog_download({"display_name": "test", "media_kind": "image",
                "role": "checkpoint", "format": "diffusers", "source_type": "huggingface",
                "source_ref": entry["model_id"], "revision": entry["recommended_revision"],
                "license_declared": "test", "files": entry["service_recipe"]["files"]}, owner=owner)

    def second_operation(self, first):
        second = copy.deepcopy(first)
        second.update(id="sin_other", recipe_key="other-fixture", state="preflight", deployment_id=None,
                      asset_id=None, transfer_id=None)
        second["options"]["deployment_id"] = "other-fixture"
        self.repository.insert_service_installation(second)
        return self.repository.get_service_installation(second["id"])

    def test_historical_retry_and_rollback_cannot_delete_same_name_new_deployment(self):
        asset = self.ready_asset()
        operation = self.operation()
        owner = self.claim(operation)
        self.create_deployment(asset, owner=owner)
        self.fail_operation(operation, owner)
        self.assertTrue(self.deployments.rollback_created("test-image", owner=owner))
        replacement = self.create_deployment(asset)
        before = self.repository.get_deployment(replacement["id"])
        with self.assertRaises(ServiceInstallerError) as caught:
            self.installer.control(operation["id"], "retry")
        self.assertEqual(caught.exception.code, "installation_deployment_conflict")
        self.assertFalse(self.deployments.rollback_created("test-image", owner=owner))
        self.assertFalse(self.deployments.rollback_created("test-image"))
        self.assertEqual(self.repository.get_deployment("test-image"), before)

    def test_runner_never_adopts_a_deployment_created_after_preflight(self):
        asset = self.ready_asset()
        operation = self.operation()
        self.create_deployment(asset)
        before = self.repository.get_deployment("test-image")
        self.installer._run_guarded(operation["id"])
        self.assertEqual(self.installer.get(operation["id"])["state"], "failed")
        self.assertEqual(self.repository.get_deployment("test-image"), before)

    def test_concurrent_retry_creates_only_one_new_attempt(self):
        self.ready_asset()
        operation = self.operation()
        owner = self.claim(operation)
        self.fail_operation(operation, owner)
        barrier = threading.Barrier(2)
        def retry():
            barrier.wait(timeout=5)
            try:
                return self.repository.retry_service_installation(operation["id"], operation["current_attempt_id"], utc_now(), [])
            except InstallationOwnershipError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: retry(), range(2)))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(len(self.repository.installation_attempts(operation["id"])), 2)

    def test_retry_is_fenced_across_two_independent_processes(self):
        self.ready_asset()
        operation = self.operation()
        self.fail_operation(operation, self.claim(operation))
        code = """import sys
from mediacenter.repository import Repository, InstallationOwnershipError
r=Repository(sys.argv[1])
try:
 r.retry_service_installation(sys.argv[2],sys.argv[3],'test',[])
 print('won')
except InstallationOwnershipError:
 print('fenced')
"""
        commands = [sys.executable, "-B", "-c", code, str(self.repository.path), operation["id"], operation["current_attempt_id"]]
        processes = [subprocess.Popen(commands, cwd=Path(__file__).resolve().parents[1],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        results = [process.communicate(timeout=15) for process in processes]
        self.assertEqual(sorted(stdout.strip() for stdout, _ in results), ["fenced", "won"], results)
        self.assertTrue(all(process.returncode == 0 for process in processes), results)
        self.assertEqual(len(self.repository.installation_attempts(operation["id"])), 2)

    def test_only_one_runner_claim_and_old_runner_cannot_commit_or_delete(self):
        asset = self.ready_asset()
        operation = self.operation()
        owner = self.claim(operation)
        self.assertIsNone(self.claim(operation))
        self.fail_operation(operation, owner)
        next_operation = self.repository.retry_service_installation(operation["id"], owner[1], utc_now(), [])
        new_owner = self.claim(next_operation)
        self.create_deployment(asset, owner=new_owner)
        before = self.repository.get_deployment("test-image")
        with self.assertRaises(InstallationOwnershipError):
            self.installer._set(operation["id"], owner=owner, state="ready", step="health", progress=1)
        with self.assertRaises(InstallationOwnershipError):
            self.deployments.rollback_created("test-image", owner=owner)
        with self.assertRaises(InstallationOwnershipError):
            self.create_deployment(asset, owner=owner, deployment_id="stale-created")
        self.assertEqual(self.repository.get_deployment("test-image"), before)
        self.assertIsNone(self.repository.get_deployment("stale-created"))

    def test_shared_transfer_cancel_detaches_without_mutating_shared_resources(self):
        asset_id = self.ready_asset()
        first = self.operation()
        owner = self.claim(first)
        transfer = self.transfer(owner)
        self.installer._set(first["id"], owner=owner, state="downloading", step="download", progress=0.1)
        second = self.second_operation(first)
        second_owner = self.claim(second)
        self.repository.record_installation_resource(second_owner, "transfer", transfer["id"], transfer["id"], False, utc_now())
        for current_owner in (owner, second_owner):
            self.repository.record_installation_resource(current_owner, "asset", asset_id, "same-version", False, utc_now())
        before_transfer = self.repository.get_model_transfer(transfer["id"])
        before_asset = self.repository.get_model_asset(asset_id)
        with self.assertRaises(ServiceInstallerError):
            self.installer.control(first["id"], "pause")
        self.installer.control(first["id"], "cancel")
        self.assertEqual(self.installer.get(first["id"])["state"], "canceled")
        self.assertEqual(self.repository.get_model_transfer(transfer["id"]), before_transfer)
        self.assertEqual(self.repository.get_model_asset(asset_id), before_asset)
        self.assertEqual(self.installer.get(second["id"])["state"], "preflight")

    def test_owned_transfer_control_is_atomic_and_stale_runner_cannot_resurrect_cancel(self):
        self.ready_asset()
        operation = self.operation()
        owner = self.claim(operation)
        transfer = self.transfer(owner)
        self.installer._set(operation["id"], owner=owner, state="downloading", step="download", progress=0.1)
        self.installer.control(operation["id"], "pause")
        self.assertEqual(self.assets.get_transfer(transfer["id"])["state"], "paused")
        with patch.object(self.installer, "_spawn"), patch.object(self.assets, "_start_download") as start:
            self.installer.control(operation["id"], "resume")
            start.assert_called_once_with(transfer["id"])
        self.installer.control(operation["id"], "cancel")
        self.assertEqual(self.assets.get_transfer(transfer["id"])["state"], "canceled")
        with self.assertRaises(InstallationOwnershipError):
            self.installer._set(operation["id"], owner=owner, state="downloading", step="download", progress=0.2)

    def test_unknown_transfer_ownership_fails_closed(self):
        self.ready_asset()
        operation = self.operation()
        owner = self.claim(operation)
        transfer = self.transfer(None)
        self.installer._set(operation["id"], owner=owner, state="downloading", step="download", progress=0.1,
                            transfer_id=transfer["id"])
        before = self.repository.get_model_transfer(transfer["id"])
        self.installer.control(operation["id"], "cancel")
        self.assertEqual(self.repository.get_model_transfer(transfer["id"]), before)

    def test_owned_deployment_referenced_by_another_attempt_is_not_rolled_back(self):
        asset = self.ready_asset()
        first = self.operation()
        owner = self.claim(first)
        self.create_deployment(asset, owner=owner)
        row = self.repository.get_deployment("test-image")
        other = self.second_operation(first)
        other_owner = self.claim(other)
        self.repository.record_installation_resource(other_owner, "deployment", row["id"], row["incarnation"], False, utc_now())
        self.assertFalse(self.deployments.rollback_created(row["id"], owner=owner))
        self.assertEqual(self.repository.get_deployment(row["id"]), row)

    def test_creation_and_ownership_record_commit_together(self):
        asset = self.ready_asset()
        operation = self.operation()
        owner = self.claim(operation)
        with patch.object(self.repository, "_record_installation_resource", side_effect=RuntimeError("crash before commit")):
            with self.assertRaises(RuntimeError):
                self.create_deployment(asset, owner=owner)
            with self.assertRaises(RuntimeError):
                self.transfer(owner)
        self.assertIsNone(self.repository.get_deployment("test-image"))
        self.assertIsNone(self.installer.get(operation["id"])["transfer_id"])
        self.assertEqual(len(self.repository.list_model_transfers()), 1)  # Existing asset upload only.

    def test_binding_failure_never_enables_installation_and_preserves_asset(self):
        asset_id = self.ready_asset()
        observed = []
        def fail(point):
            if point == 'binding.insert':
                row = self.repository.get_deployment('test-image')
                observed.append((row['enabled'], row['is_default'], row['startup_policy']))
                raise RuntimeError('binding commit failed')
        self.installer.installation_runtime.fault = fail
        operation = self.operation()
        self.installer._run_guarded(operation['id'])
        self.assertEqual(observed, [(False, False, 'manual')])
        self.assertEqual(self.installer.get(operation['id'])['state'], 'failed')
        self.assertIsNone(self.repository.get_deployment('test-image'))
        self.assertEqual(self.repository.get_model_asset(asset_id)['state'], 'ready')

    def test_success_refreshes_stopped_service_only_after_atomic_binding(self):
        self.ready_asset()
        registry = ModelRegistry(self.deployments)
        observed = []
        def before(point):
            if point == 'binding.insert':
                observed.append(self.repository.get_deployment('test-image')['enabled'])
        self.installer.installation_runtime.fault = before
        self.deployments.on_change = registry.refresh
        with patch.object(self.installer, 'health_check', side_effect=AssertionError('no implicit check')):
            operation = self.operation()
            self.installer._run_guarded(operation['id'])
        self.assertEqual(observed, [False])
        self.assertEqual(self.installer.get(operation['id'])['state'], 'ready')
        self.assertFalse(self.repository.get_deployment('test-image')['enabled'])
        self.assertFalse(registry.get(ServiceKind.IMAGE, 'test-image').enabled)

    def test_postcommit_registry_error_preserves_ready_and_is_rebuildable(self):
        self.ready_asset()
        operation = self.operation()
        def fail_after_commit():
            if self.installer.get(operation["id"])["state"] == "ready":
                raise RuntimeError("observer unavailable")
        self.deployments.on_change = fail_after_commit
        with self.assertLogs("mediacenter.service_installer", level="ERROR") as logged:
            self.installer._run_guarded(operation["id"])
        self.assertIn("installation_registry_refresh_failed", logged.output[0])
        self.assertEqual(self.installer.get(operation["id"])["state"], "ready")
        self.assertFalse(self.repository.get_deployment("test-image")["enabled"])
        rebuilt = ModelRegistry(self.deployments)
        self.assertFalse(rebuilt.get(ServiceKind.IMAGE, "test-image").enabled)

    def test_active_import_cannot_be_retried_while_external_call_is_unresolved(self):
        self.ready_asset()
        operation = self.operation()
        reached, release = threading.Event(), threading.Event()
        def blocked_load(*args):
            reached.set()
            if not release.wait(5):
                raise RuntimeError('fixture import deadline')
            raise RuntimeError('connection lost after import dispatch')
        runner = threading.Thread(target=self.installer._run_guarded, args=(operation['id'],))
        with patch.object(self.installer.runtime_importer, 'load', side_effect=blocked_load) as load:
            runner.start()
            try:
                self.assertTrue(reached.wait(5))
                self.assertEqual(self.installer.get(operation['id'])['runtime_image']['phase'], 'import_pending')
                with self.assertRaises(ServiceInstallerError):
                    self.installer.control(operation['id'], 'retry')
            finally:
                release.set()
                runner.join(5)
            self.assertFalse(runner.is_alive())
            self.assertEqual(load.call_count, 1)
        self.assertEqual(self.installer.get(operation['id'])['state'], 'failed')
        self.assertEqual(self.installer.get(operation['id'])['runtime_image']['phase'], 'import_unknown')

    def test_import_timeout_keeps_cross_attempt_handover_closed(self):
        self.ready_asset()
        operation = self.operation()
        with patch.object(self.installer.runtime_importer, 'load', side_effect=TimeoutError('uncertain')) as load:
            self.installer._run_guarded(operation['id'])
            with patch.object(self.installer, '_spawn'):
                self.installer.control(operation['id'], 'retry')
            self.installer._run_guarded(operation['id'])
            self.assertEqual(load.call_count, 1)
        result = self.installer.get(operation['id'])
        self.assertEqual(result['state'], 'failed')
        self.assertIsNone(self.repository.get_deployment('test-image'))
        with self.repository._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM runtime_image_transfers WHERE phase='import_unknown'").fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM instance_installation_bindings').fetchone()[0], 0)

    def test_restart_preserves_live_owner_and_recovers_dead_owner_without_fake_installed(self):
        asset = self.ready_asset()
        operation = self.operation()
        owner = self.claim(operation)
        self.create_deployment(asset, owner=owner)
        self.deployments.mark_install_state("test-image", "ready", owner=owner)
        other = ServiceInstaller(self.repository, self.catalog, self.assets, self.deployments)
        self.assertEqual(other.get(operation["id"])["state"], "preflight")
        with patch("mediacenter.repository._pid_alive", return_value=False):
            recovered = ServiceInstaller(self.repository, self.catalog, self.assets, self.deployments)
        self.assertEqual(recovered.get(operation["id"])["state"], "failed")
        self.assertIsNone(self.repository.get_deployment("test-image"))
        self.assertNotEqual(recovered.catalog()[0]["state"], "installed")
        reopened = Repository(self.repository.path)
        self.assertEqual(reopened.installation_attempts(operation["id"])[0]["state"], "failed")
        self.assertTrue(reopened.installation_resources(owner[1]))
        with self.assertRaises(InstallationOwnershipError):
            self.installer._set(operation["id"], owner=owner, state="ready", step="health", progress=1)


    def test_live_attempt_cannot_mutate_replacement_even_using_its_new_incarnation(self):
        asset = self.ready_asset()
        operation = self.operation()
        owner = self.claim(operation)
        self.create_deployment(asset, owner=owner)
        self.assertTrue(self.deployments.rollback_created("test-image", owner=owner))
        self.create_deployment(asset)
        replacement = self.repository.get_deployment("test-image")
        with self.assertRaises(InstallationOwnershipError):
            self.deployments.mark_install_state("test-image", "ready", owner=owner)
        self.assertEqual(self.repository.get_deployment("test-image"), replacement)





    def test_success_atomically_installs_stopped_deployment_and_retains_attempt_history(self):
        self.ready_asset()
        operation = self.operation()
        self.installer._run_guarded(operation["id"])
        current = self.installer.get(operation["id"])
        deployment = self.repository.get_deployment("test-image")
        self.assertEqual(current["state"], "ready")
        self.assertFalse(deployment["enabled"])
        self.assertTrue(deployment["is_default"])
        attempts = self.repository.installation_attempts(operation["id"])
        self.assertEqual([(item["generation"], item["state"]) for item in attempts], [(1, "ready")])
        resources = self.repository.installation_resources(attempts[0]["id"])
        self.assertEqual({resource["kind"] for resource in resources}, {"deployment", "asset", "runtime-transfer", "runtime-image", "runtime-binding"})

    def test_failed_owned_download_hands_over_same_id_and_offsets(self):
        self.ready_asset()
        operation = self.operation()
        owner = self.claim(operation)
        transfer = self.transfer(owner)
        raw = self.repository.get_model_transfer(transfer["id"])
        first = raw["files"][0]
        target = self.assets.storage_root / raw["quarantine_relpath"] / first["relative_path"]
        target.write_bytes(b"{")
        self.assertEqual(self.repository.append_model_transfer_bytes(transfer["id"], first["id"], 0, 1, utc_now()), "updated")
        self.repository.set_model_transfer_state(transfer["id"], {"transferring"}, "failed", utc_now())
        self.fail_operation(operation, owner)
        with patch.object(self.installer, "_spawn"):
            retried = self.installer.control(operation["id"], "retry")
        self.assertEqual(retried["transfer_id"], transfer["id"])
        continued = self.repository.get_model_transfer(transfer["id"])
        self.assertEqual((continued["received_bytes"], continued["files"][0]["received_bytes"]), (1, 1))
        self.assertEqual(target.read_bytes(), b"{")
        old_record = next(r for r in self.repository.installation_resources(owner[1]) if r["kind"] == "transfer")
        self.assertEqual(old_record["successor_attempt_id"], retried["current_attempt_id"])
        new_owner = self.claim(retried)
        self.installer._set(operation["id"], owner=new_owner, state="downloading", step="download", progress=0.2)
        self.installer.control(operation["id"], "pause")
        self.assertEqual(self.assets.get_transfer(transfer["id"])["state"], "paused")

    def test_retry_does_not_handover_a_live_or_shared_downloader(self):
        self.ready_asset()
        operation = self.operation()
        owner = self.claim(operation)
        transfer = self.transfer(owner)
        token = self.repository.claim_transfer_download(transfer["id"])
        self.repository.set_model_transfer_state(transfer["id"], {"queued"}, "failed", utc_now())
        self.fail_operation(operation, owner)
        with self.assertRaises(ServiceInstallerError) as caught:
            self.installer.control(operation["id"], "retry")
        self.assertEqual(caught.exception.code, "installation_transfer_running")
        self.repository.finish_transfer_download(transfer["id"], token)
        other = self.second_operation(operation)
        self.repository.record_installation_resource(self.claim(other), "transfer", transfer["id"], transfer["id"], False, utc_now())
        with self.assertRaises(ServiceInstallerError) as caught:
            self.installer.control(operation["id"], "retry")
        self.assertEqual(caught.exception.code, "installation_resource_shared")
        self.assertEqual(len(self.repository.installation_attempts(operation["id"])), 1)

    def test_download_resume_racing_old_worker_exit_restarts_once(self):
        transfer = self.transfer(None)
        token = self.repository.claim_transfer_download(transfer["id"])
        self.assertIsNone(self.repository.claim_transfer_download(transfer["id"]))
        def paused_then_resumed(*args, **kwargs):
            self.assertEqual(kwargs, {"token": token})
            self.repository.set_model_transfer_state(transfer["id"], {"queued"}, "paused", utc_now())
            self.repository.set_model_transfer_state(transfer["id"], {"paused"}, "queued", utc_now())
            return False
        with patch.object(self.assets, "_download_file", side_effect=paused_then_resumed), \
                patch.object(self.assets, "_start_download") as restart:
            self.assets._download_worker(transfer["id"], token)
            restart.assert_called_once_with(transfer["id"])
        self.assertIsNotNone(self.repository.claim_transfer_download(transfer["id"]))
if __name__ == "__main__":
    unittest.main()
