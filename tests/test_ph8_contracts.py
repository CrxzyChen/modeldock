from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from mediacenter.asset_compatibility import (
    AssetCompatibilityError,
    AssetCompatibilityManager,
)
from mediacenter.deployment_operations import (
    DeploymentOperationError,
    DeploymentOperationManager,
)
from mediacenter.model_assets import ModelAssetManager
from mediacenter.model_deployments import (
    DeploymentError, ModelDeploymentManager, deployment_config_digest,
)
from mediacenter.model_registry import ModelRegistry
from mediacenter.repository import Repository
from mediacenter.runtime_artifacts import RuntimeArtifactStore
from mediacenter.runtime_provisioning import InstallationRuntime
from mediacenter.runtime_profiles import RuntimeProfileError, RuntimeProfileManager
from mediacenter.instance_policy import InstancePolicy
from mediacenter.reconciler import Reconciler
from mediacenter.service_center import ServiceCenter, ServiceCenterError
from mediacenter.task_state import TaskState, TaskStateError, canonical, digest
from mediacenter.protocol import PROTOCOL, ProtocolError, validate_envelope
from mediacenter.service_installer import ServiceInstaller, ServiceInstallerError
from mediacenter.container_releases import RuntimeContractError, RuntimeRelease
from mediacenter.config import ContainerError, _register_runtime_profiles
from tests.test_runtime_artifacts import image_fixture
from tests.test_resident_policy import ready_instance


def safetensors_bytes(name: str, value: int) -> bytes:
    tensor = value.to_bytes(4, "little")
    header = json.dumps({
        "__metadata__": {"name": name},
        "weight": {"dtype": "F32", "shape": [1], "data_offsets": [0, len(tensor)]},
    }, separators=(",", ":")).encode("utf-8")
    return struct.pack("<Q", len(header)) + header + tensor


class PH8ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = Repository(self.root / "state.db")
        self.assets = ModelAssetManager(self.repository, self.root / "model-store")

    def tearDown(self) -> None:
        self.doCleanups()  # Join background runners before removing their owned fixture files.
        self.temporary.cleanup()

    def publish(self, name: str, role: str, value: int) -> dict:
        data = safetensors_bytes(name, value)
        transfer = self.assets.create_upload({
            "display_name": name,
            "media_kind": "image",
            "role": role,
            "format": "safetensors",
            "revision": f"{name}-v1",
            "license_declared": "test-only",
            "files": [{
                "relative_path": f"{name}.safetensors",
                "byte_size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }],
        })
        file_id = transfer["files"][0]["id"]
        self.assets.append_upload_chunk(transfer["id"], file_id, 0, data)
        complete = self.assets.complete_upload(transfer["id"])
        return self.repository.get_model_asset(complete["asset_id"])

    def metadata(self, asset: dict, *, family: str,
                 declared_base_identity: str | None = None) -> dict:
        detail = {"declared_base_identity": declared_base_identity} if declared_base_identity else {}
        contract = {
            "architecture_family": family,
            "tensor_precision": "fp16",
            "parameter_summary": {"tensor_count": 1, "parameter_count": 1},
            "metadata": detail,
            "detector_version": "mc-sdxl-1",
        }
        contract["metadata_digest"] = hashlib.sha256(json.dumps(
            contract, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        return self.repository.set_model_asset_metadata(
            asset["id"], asset["revision"], contract, "2026-09-04T00:00:00+00:00")

    def operation_payload(self, base: dict, profile: dict) -> dict:
        return {
            "deployment_id": "wai-user-one",
            "plan_digest": "1" * 64,
            "base_asset": {
                "asset_id": base["id"], "revision": base["revision"],
                "manifest_digest": base["manifest_digest"],
            },
            "runtime_profile": {
                "profile_id": profile["profile_id"], "revision": profile["revision"],
                "image_digest": profile["image_digest"],
                "profile_digest": profile["profile_digest"],
            },
            "vae_asset": None,
            "gpu_uuids": ["GPU-test-0001"],
            "residency": "on_demand",
            "sharing_mode": "shared",
            "required_vram_mib": 16384,
            "confirmations": {
                "license_accepted": True,
                "experimental_compatibility_accepted": False,
            },
        }

    def test_lora_matches_single_file_sha_and_rejects_declared_other_base(self):
        wai = self.metadata(self.publish('wai-exact', 'checkpoint', 1), family='sdxl')
        pony = self.metadata(self.publish('pony-other', 'checkpoint', 2), family='sdxl')
        lora = self.metadata(self.publish('wai-lora', 'lora', 3), family='sdxl',
                             declared_base_identity=wai['files'][0]['sha256'])
        manager = AssetCompatibilityManager(self.repository)
        self.assertEqual(manager.assess(lora['id'], wai['id'])['verdict'], 'exact')
        rejected = manager.assess(lora['id'], pony['id'])
        self.assertEqual(rejected['verdict'], 'incompatible')
        self.assertEqual(rejected['reason_codes'], ['declared_base_identity_mismatch'])
        with self.assertRaisesRegex(AssetCompatibilityError, '不允许'):
            manager.require_task_compatible(lora['id'], lora['revision'], pony['id'], pony['revision'],
                                             allow_experimental=True)

    def completed_old_model_command(self):
        state = TaskState(self.repository)
        authority = ready_instance(self.repository, state)
        with self.repository._connect() as db:
            operation = db.execute('SELECT * FROM model_operations ORDER BY rowid DESC LIMIT 1').fetchone()
            row = db.execute('SELECT * FROM task_outbox WHERE message_id=?', (operation['command_id'],)).fetchone()
            message = json.loads(row['envelope_json'])
            # Fixture of a formerly valid, already completed immutable command.
            # Production history is never rewritten by the new implementation.
            message['payload'].pop('residency')
            db.execute('UPDATE task_outbox SET envelope_json=?,digest=? WHERE message_id=?',
                       (canonical(message), digest(message), message['message_id']))
            db.execute('UPDATE model_operations SET command_digest=? WHERE operation_id=?',
                       (digest(message), operation['operation_id']))
            terminal = json.loads(db.execute("SELECT envelope_json FROM instance_inbox WHERE scope_id=? "
                "AND json_extract(envelope_json,'$.type')='model.terminal'", (operation['operation_id'],)).fetchone()[0])
        return state, authority, message, terminal

    def test_completed_old_model_command_does_not_block_new_unload_or_receipts(self):
        state, authority, old, event = self.completed_old_model_command()
        with self.assertRaises(ProtocolError):
            validate_envelope(old, capabilities=state.capabilities)
        authority.set_service('instance-one', False)
        unload = authority.unload('instance-one')
        with self.repository._connect() as db:
            before = tuple(db.iterdump())
        cursor, messages, skipped_page = 0, [], False
        for _ in range(20):
            page = state.outbox_page(1, replay=True, after_sequence=cursor)
            self.assertGreater(page['next_cursor'], cursor)
            cursor = page['next_cursor']
            messages.extend(page['items'])
            skipped_page |= not page['items']
            if not page['has_more']:
                break
        else:
            self.fail('bounded replay did not finish')
        self.assertTrue(skipped_page)
        self.assertNotIn(old['message_id'], [item['message_id'] for item in messages])
        self.assertIn(unload, [item['message_id'] for item in messages])
        receipt = authority.receipt_for_event(event)
        self.assertIn(receipt, messages)
        self.assertEqual(authority.receive(event), 'applied')
        self.assertEqual(authority.receipt_for_event(event), receipt)
        with self.repository._connect() as db:
            self.assertEqual(tuple(db.iterdump()), before)

    def test_incomplete_old_command_is_not_excused_by_delivery_or_acceptance(self):
        state, _, old, _ = self.completed_old_model_command()
        for stage in ('pending', 'accepted'):
            with self.subTest(stage=stage), self.repository._connect() as db:
                db.execute("UPDATE model_operations SET state=?", (stage,))
                db.execute("UPDATE task_outbox SET delivered_at='sent',acknowledged_at='accepted' WHERE message_id=?",
                           (old['message_id'],))
            with self.assertRaises(ProtocolError):
                state.outbox_page(replay=True)

    def test_terminal_command_without_committed_event_remains_fail_closed(self):
        state, _, old, event = self.completed_old_model_command()
        with self.repository._connect() as db:
            db.execute("UPDATE instance_inbox SET result='pending' WHERE message_id=?", (event['message_id'],))
        with self.assertRaises(ProtocolError):
            state.outbox_page(replay=True)

    def test_terminal_command_and_event_integrity_cannot_be_hidden_by_retirement(self):
        state, authority, old, event = self.completed_old_model_command()
        for table, field, key, value in (
            ('task_outbox', 'digest', 'message_id', old['message_id']),
            ('model_operations', 'command_digest', 'command_id', old['message_id']),
            ('instance_inbox', 'digest', 'message_id', event['message_id']),
        ):
            with self.subTest(table=table):
                with self.repository._connect() as db:
                    original = db.execute(f'SELECT {field} FROM {table} WHERE {key}=?', (value,)).fetchone()[0]
                    db.execute(f'UPDATE {table} SET {field}=? WHERE {key}=?', ('broken', value))
                with self.assertRaisesRegex(TaskStateError, 'outbox_integrity_error'):
                    state.outbox_page(replay=True)
                with self.repository._connect() as db:
                    db.execute(f'UPDATE {table} SET {field}=? WHERE {key}=?', (original, value))
        tampered = json.loads(canonical(event))
        tampered['payload']['status'] = 'failed'
        tampered['payload']['error_code'] = 'bad-event'
        with self.assertRaisesRegex(TaskStateError, 'event_identity_conflict'):
            authority.receive(tampered)
        receipt = authority.receipt_for_event(event)
        wrong = dict(receipt, instance_id='another-instance')
        with self.repository._connect() as db:
            db.execute('UPDATE task_outbox SET envelope_json=?,digest=? WHERE message_id=?',
                       (canonical(wrong), digest(wrong), receipt['message_id']))
        with self.assertRaisesRegex(TaskStateError, 'receipt_integrity_error'):
            authority.receive(event)

    def test_compatibility_rechecks_current_identity_and_evidence(self):
        base = self.metadata(self.publish('base', 'checkpoint', 1), family='sdxl')
        lora = self.metadata(self.publish('lora', 'lora', 2), family='sdxl')
        manager = AssetCompatibilityManager(self.repository)
        manager.assess(lora['id'], base['id'])
        with self.repository._connect() as db:
            db.execute("UPDATE model_assets SET manifest_digest=? WHERE id=?", ('0'*64, base['id']))
        with self.assertRaisesRegex(AssetCompatibilityError, '不一致'):
            manager.require_task_compatible(lora['id'], lora['revision'], base['id'], base['revision'])

    def test_repeated_compatibility_assessment_has_no_durable_write(self):
        base = self.metadata(self.publish('base', 'checkpoint', 1), family='sdxl')
        lora = self.metadata(self.publish('lora', 'lora', 2), family='sdxl')
        center = ServiceCenter(self.repository, SimpleNamespace(), self.root/'artifacts')
        payload = dict(subject_asset_id=lora['id'], base_asset_id=base['id'])
        center.assess_asset_compatibility(payload)
        with self.repository._connect() as db:
            version = db.execute('PRAGMA data_version').fetchone()[0]
            for _ in range(3):
                self.assertEqual(center.assess_asset_compatibility(payload)['disposition'], 'existing')
            self.assertEqual(db.execute('PRAGMA data_version').fetchone()[0], version)

    def test_repository_installs_ph8_schema_without_touching_audit(self) -> None:
        before = self.repository.list_audit()
        with self.repository._connect() as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            asset_columns = {row[1] for row in db.execute("PRAGMA table_info(model_assets)")}
        self.assertTrue({"runtime_profiles", "asset_compatibility", "model_deployment_revisions",
                         "deployment_operations", "deployment_operation_resources"} <= tables)
        self.assertTrue({"architecture_family", "parameter_summary_json", "metadata_json",
                         "detector_version", "metadata_digest"} <= asset_columns)
        self.assertEqual(self.repository.list_audit(), before)

    def test_existing_1_1_19_metadata_is_preserved_by_additive_schema_migration(self) -> None:
        asset = self.publish("legacy", "checkpoint", 9)
        before = {key: asset[key] for key in (
            "id", "display_name", "revision", "manifest_digest", "total_bytes", "storage_relpath")}
        with sqlite3.connect(self.repository.path) as db:
            db.executescript("""
                DROP TABLE deployment_operation_resources;
                DROP TABLE deployment_operations;
                DROP TABLE model_deployment_revisions;
                DROP TABLE asset_compatibility;
                DROP TABLE runtime_profiles;
            """)
            for column in ("metadata_digest", "detector_version", "metadata_json",
                           "parameter_summary_json", "tensor_precision", "architecture_family"):
                db.execute(f"ALTER TABLE model_assets DROP COLUMN {column}")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        migrated = Repository(self.repository.path).get_model_asset(asset["id"])
        self.assertEqual({key: migrated[key] for key in before}, before)
        self.assertEqual(migrated["architecture_family"], "unknown")
        self.assertEqual((migrated["parameter_summary"], migrated["metadata"]), ({}, {}))

    def test_runtime_profile_is_strict_immutable_and_loader_allowlisted(self) -> None:
        manager = RuntimeProfileManager(self.repository)
        payload = manager.sdxl_single_file("sha256:" + "a" * 64)
        created = manager.register(payload)
        replayed = manager.register(payload)
        self.assertEqual((created["disposition"], replayed["disposition"]),
                         ("created", "existing"))
        self.assertEqual(created["profile_digest"], replayed["profile_digest"])
        changed = dict(payload, image_digest="sha256:" + "b" * 64)
        with self.assertRaisesRegex(RuntimeProfileError, "相同 Runtime") as conflict:
            manager.register(changed)
        self.assertEqual(conflict.exception.status, 409)
        with self.assertRaisesRegex(RuntimeProfileError, "trust_remote_code"):
            manager.register(dict(payload, revision=2, trust_remote_code=True))
        with self.assertRaisesRegex(RuntimeProfileError, "受控列表"):
            manager.register(dict(payload, revision=2, loader="os.system"))

    def test_asset_metadata_and_compatibility_are_revision_bound(self) -> None:
        base = self.publish("wai", "checkpoint", 1)
        base = self.metadata(base, family="sdxl")
        lora = self.publish("shadow", "lora", 2)
        lora = self.metadata(lora, family="sdxl", declared_base_identity=base["manifest_digest"])
        manager = AssetCompatibilityManager(self.repository)
        first = manager.assess(lora["id"], base["id"])
        second = manager.assess(lora["id"], base["id"])
        self.assertEqual(first["verdict"], "exact")
        self.assertEqual((first["disposition"], second["disposition"]),
                         ("created", "existing"))
        self.assertEqual(manager.require_task_compatible(
            lora["id"], lora["revision"], base["id"], base["revision"])["verdict"], "exact")
        with self.assertRaisesRegex(ValueError, "immutable"):
            changed = self.metadata(base, family="sdxl-other")
            self.assertIsNone(changed)

    def test_unknown_compatibility_fails_closed_for_task_binding(self) -> None:
        base = self.publish("base", "checkpoint", 3)
        subject = self.publish("lora", "lora", 4)
        record = AssetCompatibilityManager(self.repository).assess(subject["id"], base["id"])
        self.assertEqual(record["verdict"], "unknown")
        with self.assertRaises(AssetCompatibilityError) as raised:
            AssetCompatibilityManager(self.repository).require_task_compatible(
                subject["id"], subject["revision"], base["id"], base["revision"])
        self.assertEqual(raised.exception.code, "asset_incompatible")

    def test_deployment_operation_idempotency_and_monotonic_transitions(self) -> None:
        base = self.metadata(self.publish("base", "checkpoint", 5), family="sdxl")
        profile = RuntimeProfileManager(self.repository).register(
            RuntimeProfileManager.sdxl_single_file("sha256:" + "c" * 64))
        manager = DeploymentOperationManager(self.repository)
        payload = self.operation_payload(base, profile)
        first = manager.create(payload, server_profile_id="server-one",
                               authenticated_principal="operator-one",
                               idempotency_key="import-wai-0001")
        replay = manager.create(payload, server_profile_id="server-one",
                                authenticated_principal="operator-one",
                                idempotency_key="import-wai-0001")
        self.assertEqual(first["id"], replay["id"])
        self.assertEqual((first["disposition"], replay["disposition"]), ("created", "replayed"))
        self.assertEqual(manager.list(25)[0]["id"], first["id"])
        changed = dict(payload, residency="resident")
        with self.assertRaises(DeploymentOperationError) as conflict:
            manager.create(changed, server_profile_id="server-one",
                           authenticated_principal="operator-one",
                           idempotency_key="import-wai-0001")
        self.assertEqual((conflict.exception.code, conflict.exception.status),
                         ("idempotency_conflict", 409))
        preparing = manager.transition(first["id"], "preparing_runtime")
        self.assertEqual(preparing["state"], "preparing_runtime")
        with self.assertRaises(DeploymentOperationError) as invalid:
            manager.transition(first["id"], "ready")
        self.assertEqual(invalid.exception.code, "invalid_deployment_operation_transition")
        with self.assertRaises(DeploymentOperationError) as missing_error:
            manager.transition(first["id"], "rollback")
        self.assertEqual(missing_error.exception.code, "deployment_operation_error_required")
        rolled_back = manager.transition(first["id"], "rollback",
                                         error_class="recoverable", error_code="gpu_capacity_changed",
                                         error_message="capacity changed")
        failed = manager.transition(first["id"], "failed",
                                    error_class="recoverable", error_code="gpu_capacity_changed")
        self.assertEqual((rolled_back["state"], failed["state"]), ("rollback", "failed"))

    def test_operation_resource_identity_is_bounded_and_idempotent(self) -> None:
        base = self.metadata(self.publish("base", "checkpoint", 6), family="sdxl")
        profile = RuntimeProfileManager(self.repository).register(
            RuntimeProfileManager.sdxl_single_file("sha256:" + "d" * 64))
        manager = DeploymentOperationManager(self.repository)
        operation = manager.create(self.operation_payload(base, profile),
                                   server_profile_id="server-one",
                                   authenticated_principal="operator-one",
                                   idempotency_key="import-base-0002")
        value = manager.record_resource(operation["id"], kind="runtime-image",
                                        resource_id="image-one", identity="sha256:runtime-one",
                                        created=False)
        self.assertEqual(len(value["resources"]), 1)
        value = manager.record_resource(operation["id"], kind="runtime-image",
                                        resource_id="image-one", identity="sha256:runtime-one",
                                        created=False)
        self.assertEqual(len(value["resources"]), 1)
        with self.assertRaises(DeploymentOperationError):
            manager.record_resource(operation["id"], kind="host-path",
                                    resource_id="escape", identity="bad", created=True)

    def _runtime(self):
        declaration, archive = image_fixture()
        declaration["release_id"] = "sdxl-single-file-v1"
        declaration["adapter_id"] = "sdxl-single-file"
        release = RuntimeRelease(declaration)

        @contextmanager
        def source(_release, offset, _timeout):
            yield io.BytesIO(archive[offset:])

        images = RuntimeArtifactStore(
            self.repository, self.root / "runtime-images",
            approved_digests=[release.digest], source=source,
            local_artifact_roots=[str(self.root)],
            local_artifacts={release.digest: str(self.root / 'approved-runtime.oci.tar')})
        (self.root / 'approved-runtime.oci.tar').write_bytes(archive)
        images.register(declaration)

        class Importer:
            engine_id = "fixture-engine"
            calls = 0

            def preflight(self):
                pass

            def load(self, _stream, _release):
                self.calls += 1

            def inspect(self, approved, _verified):
                return {"engine_id": self.engine_id,
                        "image_id": approved.data["image"]["image_id"]}

        self.importer = Importer()
        return release, InstallationRuntime(self.repository, images, {})

    def prepare_runtime(self, runtime, operation_id):
        with runtime.user_operation_lock(operation_id):
            DeploymentOperationManager(self.repository).transition(operation_id, 'preparing_runtime')
            runtime.prepare_user_runtime(operation_id, self.importer, lambda: None)

    def wait_operation(self, center, operation):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = center.get_deployment_operation(operation['id'])
            if current['state'] in {'ready', 'failed', 'canceled'}:
                return current
            threading.Event().wait(0.02)
        self.fail(f"operation did not settle: {current}")

    def user_deployment_fixture(self, gpu_uuid='GPU-test-0'):
        base = self.metadata(self.publish('runtime-user', 'checkpoint', 51), family='sdxl')
        release, installation = self._runtime()
        profile = RuntimeProfileManager(self.repository).register(
            RuntimeProfileManager.sdxl_single_file(release.image_digest))
        deployments = ModelDeploymentManager(
            self.repository, Path(__file__).resolve().parents[1] / 'deploy' / 'model_catalog.json',
            self.root, (0,), self.assets.storage_root)
        registry = ModelRegistry(deployments, installation_runtime=installation)
        authority = InstancePolicy(self.repository)
        scheduler = SimpleNamespace(allowed_uuids=(gpu_uuid,), allowed_indices=(0,),
                                    validate_budget=lambda _: True)

        def install_instance(instance):
            old = authority.get(instance)
            if old is not None:
                return old
            template = installation.template(instance)
            with self.repository._connect() as db:
                row = db.execute('SELECT * FROM model_deployments WHERE id=?', (instance,)).fetchone()
                binding = InstancePolicy.deployment_binding(db, row)
            return authority.configure(instance, dict(template.data['resources'], backend='container',
                binding=binding, package_id='template_' + template.digest, gpus=[gpu_uuid]))

        lifecycle = SimpleNamespace(artifacts=None, package_provider=object(), authority=authority,
                                    install_instance=install_instance)

        def new_center():
            center = ServiceCenter(self.repository, registry, self.root / 'artifacts',
                deployments=deployments, gpu_scheduler=scheduler, model_assets=self.assets,
                runtime=lifecycle, runtime_importer=self.importer)
            center._runtime_status = lambda _: {'container_state': 'created',
                'service_state': 'stopped', 'actual_state': 'unloaded', 'desired_state': 'unloaded'}
            self.addCleanup(center.stop_deployment_worker)
            return center

        center = new_center()
        def plan(deployment_id):
            return center.plan_user_deployment({
                'deployment_id': deployment_id, 'base_asset_id': base['id'], 'vae_asset_id': None,
                'runtime_profile_id': profile['profile_id'], 'runtime_profile_revision': profile['revision'],
                'gpu_uuids': [gpu_uuid], 'residency': 'on_demand', 'sharing_mode': 'shared',
                'required_vram_mib': 16384, 'license_accepted': True,
                'experimental_compatibility_accepted': False})['operation']
        return center, installation, plan, new_center, lifecycle, base

    def test_first_runtime_import_runs_in_background_and_second_deployment_reuses_it(self):
        center, installation, plan, new_center, _, _ = self.user_deployment_fixture()
        with self.repository._connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM runtime_image_bindings').fetchone()[0], 0)
        entered, release = threading.Event(), threading.Event()
        original = self.importer.load
        def delayed(*args):
            original(*args)
            entered.set()
            if not release.wait(8):
                raise RuntimeError('test import release timed out')
        self.importer.load = delayed
        try:
            request = plan('first-runtime')
            before = time.monotonic()
            operation = center.create_user_deployment(request, idempotency_key='first-runtime')
            self.assertEqual(operation['state'], 'accepted')
            self.assertLess(time.monotonic() - before, 1)
            self.assertTrue(entered.wait(3))
            other = new_center()
            replay = other.create_user_deployment(request, idempotency_key='first-runtime')
            self.assertEqual(replay['id'], operation['id'])
            self.assertEqual(replay['state'], 'preparing_runtime')
            self.assertEqual(self.importer.calls, 1)
        finally:
            release.set()
        ready = self.wait_operation(center, operation)
        self.assertEqual(ready['state'], 'ready', ready)
        second = center.create_user_deployment(plan('second-runtime'), idempotency_key='second-runtime')
        self.assertEqual(self.wait_operation(center, second)['state'], 'ready')
        self.assertEqual(self.importer.calls, 1)
        first_binding, second_binding = installation.get('first-runtime'), installation.get('second-runtime')
        self.assertEqual(first_binding['image_digest'], second_binding['image_digest'])
        self.assertNotEqual(first_binding['instance_id'], second_binding['instance_id'])
        from mediacenter.domain import ServiceKind
        center.registry.refresh()
        self.assertIsNone(center.registry.get(ServiceKind.IMAGE))
        self.assertIsNotNone(center.registry.get(ServiceKind.IMAGE, 'second-runtime'))
        resources = {item['kind']: item for item in ready['resources']}
        self.assertTrue(resources['deployment']['created'])
        ledger = self.repository.get_service_installation(operation['id'])
        self.assertEqual(ledger['state'], 'ready')
        self.assertIsNone(self.repository.installation_attempts(operation['id'])[0]['runner_token'])

    def configuration_fixture(self, instance='configure-user', gpu_uuid='GPU-test-0'):
        center, installation, make_plan, _, lifecycle, base = self.user_deployment_fixture(gpu_uuid)
        request = make_plan(instance)
        self.assertEqual(self.wait_operation(center, center.create_user_deployment(
            request, idempotency_key=instance+'-install'))['state'], 'ready')
        self.assertTrue(center.stop_deployment_worker())
        center.start_deployment_worker = lambda: None
        center._deployment_stop.clear()
        second = self.metadata(self.publish('configuration-base', 'checkpoint', 64), family='sdxl')
        old = self.repository.get_model_deployment_revision(instance, 1)
        policy = lifecycle.authority.get(instance)
        payload = dict(center._plan_payload(request), base_asset_id=second['id'],
            expected_configuration={'config_revision': 1, 'config_digest': old['config_digest'],
                                    'policy_version': policy['version']},
            policy_options={'external_reserve_mib': 8192, 'idle_seconds': 0, 'restart_recovery': False})
        return center, installation, lifecycle.authority, base, second, payload

    def removal_fixture(self, gpu_uuid='GPU-test-0'):
        center, installation, authority, base, _, _ = self.configuration_fixture('remove-user', gpu_uuid)
        center.runtime.retire_instance_containers = lambda _: []
        deployment = self.repository.get_deployment('remove-user')
        head = self.repository.get_model_deployment_revision('remove-user', 1)
        payload = dict(incarnation=deployment['incarnation'], config_revision=1,
                       config_digest=head['config_digest'], policy_version=authority.get('remove-user')['version'],
                       retry_of=None)
        return center, installation, authority, base, payload

    def test_user_removal_is_atomic_idempotent_and_preserves_assets_and_history(self):
        center, installation, authority, base, payload = self.removal_fixture()
        before = self.repository.get_model_asset(base['id'])
        binding = installation.get('remove-user')
        operation = center.uninstall_user_deployment('remove-user', payload, idempotency_key='remove-1')
        self.assertEqual((operation['state'], operation['milestone']), ('accepted', 'removing_containers'))
        self.assertEqual(installation.get('remove-user'), binding)  # Read identity remains available to retirement.
        replay = center.uninstall_user_deployment('remove-user', payload, idempotency_key='remove-1')
        self.assertEqual((replay['id'], replay['disposition']), (operation['id'], 'replayed'))
        with self.assertRaises(ServiceCenterError):
            center.uninstall_user_deployment('remove-user', payload, idempotency_key='remove-other')
        manager = DeploymentOperationManager(self.repository)
        with self.assertRaisesRegex(DeploymentOperationError, '卸载'):
            manager.cancel(operation['id'])
        with self.assertRaises(DeploymentOperationError):
            manager.transition(operation['id'], 'preparing_runtime')
        result = center._execute_user_deployment(operation['id'])
        self.assertEqual((result['state'], result['milestone']), ('ready', 'uninstalled'), result)
        self.assertIsNone(installation.get('remove-user'))
        self.assertEqual(self.repository.get_deployment('remove-user')['install_state'], 'configured')
        self.assertIsNone(self.repository.get_deployment('remove-user')['removal_operation_id'])
        self.assertEqual(self.repository.get_model_asset(base['id']), before)
        self.assertEqual(self.repository.get_service_installation(binding['operation_id'])['state'], 'ready')
        self.assertIsNotNone(self.repository.get_model_deployment_revision('remove-user', 1))
        self.assertEqual(center.uninstall_user_deployment('remove-user', payload,
                         idempotency_key='remove-1')['state'], 'ready')

    def test_user_removal_fences_start_configuration_task_retry_dispatch_and_materialize(self):
        center, _, authority, _, payload = self.removal_fixture()
        task = center.task_state.accept(dict(service='image', model='remove-user', prompt='test', options={}, inputs=[]),
                                        scope='test')[0]
        with self.repository._connect() as db:
            db.execute("UPDATE tasks SET status='failed' WHERE id=?", (task['id'],))
        operation = center.uninstall_user_deployment('remove-user', payload, idempotency_key='fence')
        calls = [lambda: authority.set_service('remove-user', True),
                 lambda: authority.configure('remove-user', authority.get('remove-user')['policy']),
                 lambda: authority.desire('remove-user', 'loaded'),
                 lambda: authority.load_model('remove-user', observe_capacity=lambda: {}),
                 lambda: center.task_state.retry(task['id'], task['version']),
                 lambda: center.task_state.accept(dict(service='image', model='remove-user', prompt='new', options={}, inputs=[]), scope='test'),
                 lambda: self.repository.update_deployment('remove-user', {'enabled': True})]
        for call in calls:
            with self.assertRaisesRegex(TaskStateError, 'deployment_removal_pending'):
                call()
        with self.repository._connect() as db:
            with self.assertRaisesRegex(TaskStateError, 'deployment_removal_pending'):
                InstancePolicy.assert_materialize(db, {'instance_id':'remove-user'})
        self.assertEqual(center.get_deployment_operation(operation['id'])['state'], 'accepted')

    def test_user_removal_failure_retries_with_new_owner_and_old_executor_cannot_finish(self):
        center, installation, authority, _, payload = self.removal_fixture()
        operation = center.uninstall_user_deployment('remove-user', payload, idempotency_key='fail-remove')
        def fail(_):
            raise TaskStateError('fixture_engine_unavailable')
        center.runtime.retire_instance_containers = fail
        failed = center._execute_user_deployment(operation['id'])
        self.assertEqual((failed['state'], failed['error_code']), ('failed', 'fixture_engine_unavailable'))
        self.assertEqual(self.repository.get_deployment('remove-user')['removal_operation_id'], failed['id'])
        self.assertIsNotNone(installation.get('remove-user'))
        with self.assertRaisesRegex(TaskStateError, 'deployment_removal_pending'):
            authority.set_service('remove-user', True)
        retried = center.uninstall_user_deployment('remove-user', dict(payload, retry_of=failed['id']),
                                                  idempotency_key='retry-remove')
        self.assertNotEqual(failed['id'], retried['id'])
        with self.assertRaisesRegex(TaskStateError, 'deployment_removal_owner_changed'):
            self.repository.finish_removal(operation, 'now')
        self.assertEqual(center._execute_user_deployment(operation['id']), failed)
        center.runtime.retire_instance_containers = lambda _: []
        self.assertEqual(center._execute_user_deployment(retried['id'])['state'], 'ready')
        self.assertEqual(center.get_deployment_operation(failed['id']), failed)

    def test_removal_finish_detach_fault_rolls_back_binding_and_keeps_owner(self):
        center, installation, _, _, payload = self.removal_fixture()
        operation = center.uninstall_user_deployment('remove-user', payload, idempotency_key='commit-fault')
        original = self.repository.uninstall_service_binding
        def fail_after_detach(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('transaction_fault')
        binding = installation.get('remove-user')
        with patch.object(self.repository, 'uninstall_service_binding', side_effect=fail_after_detach):
            failed = center._execute_user_deployment(operation['id'])
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(installation.get('remove-user'), binding)
        self.assertEqual(self.repository.get_deployment('remove-user')['install_state'], 'ready')
        self.assertEqual(self.repository.get_deployment('remove-user')['removal_operation_id'], operation['id'])

    def test_removal_temporary_exit_inspection_failure_retries_original_stop_confirmation(self):
        import os
        from dataclasses import replace
        from mediacenter.runtime_controller import RuntimeController
        from mediacenter.config import ContainerError
        from tests.test_runtime_controller import fixture_policy, FixtureEngine, FixtureObserver
        from tests.test_resident_policy import package_identity
        center, installation, authority, base, payload = self.removal_fixture('GPU-00000000-0000-0000-0000-000000000001')
        policy = authority.get('remove-user')['policy']
        engine, observer = FixtureEngine(), FixtureObserver()
        folder = self.root/'controller-fixture'
        folder.mkdir()
        container_policy = replace(fixture_policy(folder, self.repository.path), gpu_uuids=tuple(policy['gpus']))
        engine.config = SimpleNamespace(**dict(vars(engine.config), socket_path=container_policy.engine_socket))
        controller = RuntimeController(self.repository, engine, observer, container_policy)
        package = package_identity(policy, 'removal-epoch')
        authority.package_validator = lambda _db, record_id: package if record_id == package['runtime_record_id'] else None
        claim = authority.claim_container('remove-user', 'removal-epoch', expected_version=payload['policy_version'],
            backend='container', limits={gpu:49140 for gpu in policy['gpus']}, package_identity=package, materialize_only=True)
        intent = controller.prepare('remove-user', 'removal-epoch', claim['revision'])
        authority.bind_execution(claim['claim_id'], {'intent_id':intent['intent_id']})
        intent = controller.create_domain(intent['intent_id'], intent['version'])
        intent = controller.create(intent['intent_id'], intent['version'])
        authority.container_materialized('remove-user', 'removal-epoch')
        payload['policy_version'] = authority.get('remove-user')['version']
        # Controlled empty-domain observer only; this is not real cgroup proof.
        observer._open = lambda _path: os.open(__file__, os.O_RDONLY)
        observer._identity = lambda _fd: next(iter(observer.records.values()))['identity']
        observer._populated = lambda _fd: 0
        runtime_package = SimpleNamespace(record_id='removal-package')
        harness = SimpleNamespace(repository=self.repository, authority=authority, packages={},
            package_provider=SimpleNamespace(for_epoch_removal=lambda *_: runtime_package),
            _package=lambda _: (runtime_package, None), _controller=lambda _:controller)
        harness.remove_instance_containers = lambda instance: Reconciler.remove_instance_containers(harness, instance)
        center.runtime.retire_instance_containers = lambda instance: Reconciler.retire_instance_containers(harness, instance)
        operation = center.uninstall_user_deployment('remove-user', payload, idempotency_key='inspect-failure')
        with patch.object(engine, 'inspect', side_effect=ContainerError('fixture_inspect_transient')):
            failed = center._execute_user_deployment(operation['id'])
        self.assertEqual(failed['state'], 'failed', failed)
        self.assertEqual(controller.get(intent['intent_id'])['state'], 'exit_unconfirmed')
        self.assertEqual(self.repository.get_deployment('remove-user')['removal_operation_id'], operation['id'])
        retried = center.uninstall_user_deployment('remove-user', dict(payload, retry_of=operation['id']),
                                                   idempotency_key='inspect-retry')
        result = center._execute_user_deployment(retried['id'])
        self.assertEqual((result['state'], result['milestone']), ('ready', 'uninstalled'), result)
        self.assertEqual(center.get_deployment_operation(operation['id']), failed)
        self.assertEqual(controller.get(intent['intent_id'])['state'], 'exited')
        self.assertEqual(engine.containers, {})
        self.assertFalse(any(call[0] in {'start','stop'} for call in engine.calls))
        with self.repository._connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runtime_effects WHERE intent_id=? AND action='stop'",
                                        (intent['intent_id'],)).fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT state FROM runtime_container_removals WHERE intent_id=?',
                                        (intent['intent_id'],)).fetchone()[0], 'removed')
        self.assertIsNone(installation.get('remove-user'))
        self.assertEqual(self.repository.get_model_asset(base['id'])['state'], 'ready')

    def test_uninstalled_deployment_history_cannot_be_requeued(self):
        center, _, authority, _, payload = self.removal_fixture()
        authority.set_service('remove-user', True)
        task = center.task_state.accept(dict(service='image',model='remove-user',prompt='history',options={},inputs=[]),
            scope='history-test', deployment_id='remove-user', deployment_config_revision=1)[0]
        with self.repository._connect() as db:
            db.execute("UPDATE tasks SET status='failed' WHERE id=?", (task['id'],))
        policy = authority.set_service('remove-user', False)
        operation = center.uninstall_user_deployment('remove-user', dict(payload,policy_version=policy['version']),
                                                     idempotency_key='remove-history')
        self.assertEqual(center._execute_user_deployment(operation['id'])['state'], 'ready')
        with self.assertRaisesRegex(TaskStateError, 'deployment_not_ready'):
            center.task_state.retry(task['id'], task['version'])
        self.assertEqual(self.repository.get_task(task['id'])['status'], 'failed')

    def test_reinstall_reuses_retained_asset_and_image_under_new_deployment_identity(self):
        center, installation, authority, base, payload = self.removal_fixture()
        original = next(item for item in center.list_deployment_operations(100)
                        if item['deployment_id']=='remove-user' and item['state']=='ready')
        assets_before = self.repository.get_model_asset(base['id'])
        image_imports_before = self.importer.calls
        removal = center.uninstall_user_deployment('remove-user', payload, idempotency_key='uninstall-for-reinstall')
        self.assertEqual(center._execute_user_deployment(removal['id'])['state'], 'ready')
        plan = center.plan_user_deployment(dict(center._plan_payload(original['payload']), deployment_id='reinstalled-user'))
        operation = center.create_user_deployment(plan['operation'], idempotency_key='reinstall-new-instance')
        self.assertEqual(center._execute_user_deployment(operation['id'])['state'], 'ready')
        self.assertEqual(installation.get('reinstalled-user')['asset_id'], base['id'])
        self.assertEqual(self.importer.calls, image_imports_before)
        self.assertEqual(self.repository.get_model_asset(base['id']), dict(assets_before,
                         deployment_references=assets_before['deployment_references'] + 1))
        self.assertEqual(self.repository.get_deployment('remove-user')['install_state'], 'configured')
        self.assertIsNone(installation.get('remove-user'))
        with self.assertRaises(DeploymentError):
            center.deployments.plan_user(center._plan_payload(original['payload']))

    def test_configuration_operation_stages_switches_and_restores_existing_binding(self):
        center, installation, authority, base, second, payload = self.configuration_fixture()
        old_binding = installation.get('configure-user')
        plan = center.plan_user_deployment(payload)
        self.assertTrue(plan['effects']['updates_existing'])
        self.assertNotIn('configuration_deployment', plan)
        operation = center.create_user_deployment(plan['operation'], idempotency_key='config-1')
        self.assertEqual(operation['state'], 'accepted')
        self.assertNotIn('configuration_context_json', operation)
        context = self.repository.get_configuration_context(operation['id'])
        self.assertEqual(context['phase'], 'admitted')
        self.assertEqual(installation.get('configure-user'), old_binding)
        head = self.repository.get_deployment('configure-user')
        self.assertEqual((head['current_config_revision'], head['pending_config_revision']), (1, 2))
        replay = center.create_user_deployment(plan['operation'], idempotency_key='config-1')
        self.assertEqual((replay['id'], replay['disposition']), (operation['id'], 'replayed'))
        self.assertEqual(self.repository.get_configuration_context(operation['id']), context)
        with self.assertRaises(ServiceCenterError):
            center.create_user_deployment(plan['operation'], idempotency_key='config-race')
        with self.assertRaisesRegex(TaskStateError, 'deployment_configuration_pending'):
            authority.set_service('configure-user', True)
        result = center._execute_user_deployment(operation['id'])
        self.assertEqual(result['state'], 'health_check', result)
        self.assertEqual(installation.get('configure-user'), old_binding)
        self.assertEqual(authority.get('configure-user')['configuration_state'], 'replace_pending')
        self.assertTrue(authority.apply_pending('configure-user'))
        self.assertEqual(installation.get('configure-user')['asset_id'], second['id'])
        self.assertEqual(self.repository.get_deployment('configure-user')['current_config_revision'], 1)
        with self.assertRaisesRegex(TaskStateError, 'deployment_configuration_health_unconfirmed'):
            authority.commit_configuration('configure-user')
        # A failed candidate is fenced first, then the same operation restores
        # the exact old binding/projection; no uninstall/new instance is used.
        authority.begin_configuration_rollback('configure-user', 'test_candidate_health_failed')
        self.assertTrue(authority.rollback_configuration('configure-user'))
        self.assertEqual(installation.get('configure-user'), old_binding)
        self.assertEqual(center.get_deployment_operation(operation['id'])['state'], 'failed')
        self.assertEqual(self.repository.get_deployment('configure-user')['asset_id'], base['id'])
        self.assertEqual(self.repository.get_model_asset(second['id'])['state'], 'ready')
        self.assertIsNotNone(self.repository.get_model_deployment_revision('configure-user', 2))
        payload['expected_configuration']['policy_version'] = authority.get('configure-user')['version']
        retried = center.deployments.plan_user_configuration(payload)
        self.assertEqual(retried['configuration_revision']['config_revision'], 3)
        self.assertEqual(center.create_user_deployment(plan['operation'], idempotency_key='config-1')['id'], operation['id'])

    def test_configuration_operation_cancel_before_preparation_retains_old_instance(self):
        center, installation, authority, _, _, payload = self.configuration_fixture()
        old = installation.get('configure-user')
        plan = center.plan_user_deployment(payload)
        operation = center.create_user_deployment(plan['operation'], idempotency_key='cancel-config')
        center.cancel_deployment_operation(operation['id'])
        result = center._execute_user_deployment(operation['id'])
        self.assertEqual(result['state'], 'canceled', result)
        self.assertEqual(installation.get('configure-user'), old)
        self.assertEqual(authority.get('configure-user')['configuration_state'], 'applied')
        self.assertIsNone(self.repository.get_deployment('configure-user')['pending_config_revision'])
        self.assertIsNotNone(self.repository.get_model_deployment_revision('configure-user', 2))
        serialized = json.dumps(center.list_deployment_operations())
        self.assertNotIn('configuration_context', serialized)
        self.assertNotIn('model_path', serialized)

    def configuration_fault_fixture(self):
        center, installation, authority, _, _, payload = self.configuration_fixture()
        started = authority.set_service('configure-user', True)
        old_binding, old_policy = installation.get('configure-user'), started['policy']
        payload['expected_configuration']['policy_version'] = started['version']
        operation = center.create_user_deployment(
            center.plan_user_deployment(payload)['operation'], idempotency_key='configuration-fault')
        center._execute_user_deployment(operation['id'])
        authority.apply_pending('configure-user')
        self.seed_configuration_health_evidence(operation['id'], authority, started=True)
        with self.repository._connect() as db:
            db.execute("UPDATE instance_claims SET registered=0,state='starting',"
                       "updated_at='2026-01-01T00:00:00+00:00' WHERE claim_id='config-claim'")
        return center, installation, authority, payload, operation, old_binding, old_policy

    def assert_configuration_fault_restores_after_exit(self, fixture, reason):
        center, installation, authority, payload, operation, old_binding, old_policy = fixture
        current = authority.get('configure-user')
        self.assertEqual(current['configuration_state'], 'failed')
        self.assertEqual(current['configuration_error'], reason)
        self.assertFalse(self.repository.get_deployment('configure-user')['enabled'])
        self.assertNotEqual(installation.get('configure-user'), old_binding)
        self.assertFalse(authority.rollback_configuration('configure-user'))
        self.assertEqual(center.get_deployment_operation(operation['id'])['state'], 'health_check')
        with self.assertRaisesRegex(TaskStateError, 'instance_configuration_not_committable'):
            authority.commit_configuration('configure-user')
        # Explicit database exit fixture; real Engine proof is a separate gate.
        with self.repository._connect() as db:
            claim = authority.assert_claim(db, 'configure-user', 'config-epoch')
            db.execute("UPDATE runtime_intents SET state='exited' WHERE intent_id='config-intent'")
            authority._close_claim(db, claim, {'kind': 'fixture-confirmed-exit'})
        recovered = InstancePolicy(self.repository, authority.tasks)
        self.assertTrue(recovered.rollback_configuration('configure-user'))
        self.assertEqual(installation.get('configure-user'), old_binding)
        self.assertEqual(recovered.get('configure-user')['policy'], old_policy)
        self.assertTrue(self.repository.get_deployment('configure-user')['enabled'])
        self.assertEqual(center.get_deployment_operation(operation['id'])['state'], 'failed')
        self.assertEqual(self.repository.get_configuration_context(operation['id'])['phase'], 'rolled_back')
        payload['expected_configuration']['policy_version'] = recovered.get('configure-user')['version']
        self.assertEqual(center.deployments.plan_user_configuration(payload)['configuration_revision']['config_revision'], 3)
        with self.assertRaisesRegex(TaskStateError, 'backend_claim_required'):
            recovered.require_stop('configure-user', 'config-epoch', 'runtime_exited')

    def test_configuration_runtime_exit_automatically_requests_rollback(self):
        fixture = self.configuration_fault_fixture()
        fixture[2].require_stop('configure-user', 'config-epoch', 'runtime_exited')
        self.assert_configuration_fault_restores_after_exit(fixture, 'runtime_exited')

    def test_configuration_error_heartbeat_automatically_requests_rollback(self):
        fixture = self.configuration_fault_fixture()
        event = {'protocol': PROTOCOL, 'type': 'telemetry.heartbeat',
                 'message_id': 'heartbeat-config-error', 'server_id': fixture[2].tasks.server_id,
                 'instance_id': 'configure-user', 'worker_epoch': 'config-epoch',
                 'correlation_id': 'config', 'created_at': '2026-01-01T00:00:01Z',
                 'telemetry_seq': 1, 'payload': {'state': 'error', 'uptime_seconds': 1}}
        self.assertFalse(fixture[2].observe_heartbeat('configure-user', 'config-epoch', event,
                         at=event['created_at']))
        self.assert_configuration_fault_restores_after_exit(fixture, 'worker_error')

    def test_configuration_expired_heartbeat_automatically_requests_rollback(self):
        fixture = self.configuration_fault_fixture()
        self.assertFalse(fixture[2].observe_heartbeat('configure-user', 'config-epoch', None,
                         at='2026-01-01T00:11:00+00:00'))
        self.assert_configuration_fault_restores_after_exit(fixture, 'worker_heartbeat_expired')

    def test_configuration_fault_fence_and_rollback_intent_are_atomic(self):
        fixture = self.configuration_fault_fixture()
        authority = fixture[2]
        with self.repository._connect() as db:
            before = tuple(db.iterdump())
        def fail(stage):
            if stage == 'policy.configuration_rollback_requested':
                raise RuntimeError('configuration-fence-crash')
        authority.fault = fail
        with self.assertRaisesRegex(RuntimeError, 'configuration-fence-crash'):
            authority.require_stop('configure-user', 'config-epoch', 'runtime_exited')
        with self.repository._connect() as db:
            self.assertEqual(tuple(db.iterdump()), before)

    def test_committed_configuration_runtime_exit_does_not_roll_back(self):
        center, installation, authority, _, operation, _, _ = self.configuration_fault_fixture()
        with self.repository._connect() as db:
            db.execute("UPDATE instance_claims SET registered=1,state='online_unloaded' WHERE claim_id='config-claim'")
        authority.commit_configuration('configure-user')
        binding = installation.get('configure-user')
        authority.require_stop('configure-user', 'config-epoch', 'runtime_exited')
        self.assertEqual(authority.get('configure-user')['configuration_state'], 'applied')
        self.assertTrue(self.repository.get_deployment('configure-user')['enabled'])
        self.assertEqual(installation.get('configure-user'), binding)
        self.assertEqual(center.get_deployment_operation(operation['id'])['state'], 'ready')

    def test_configuration_failure_keeps_first_reason_before_exit(self):
        fixture = self.configuration_fault_fixture()
        authority = fixture[2]
        authority.require_stop('configure-user', 'config-epoch', 'runtime_exited')
        version = authority.get('configure-user')['version']
        authority.require_stop('configure-user', 'config-epoch', 'runtime_observation_unknown')
        self.assertEqual(authority.get('configure-user')['version'], version)
        self.assert_configuration_fault_restores_after_exit(fixture, 'runtime_exited')

    def test_configuration_admission_is_atomic_and_private_context_is_not_client_input(self):
        center, installation, authority, _, _, payload = self.configuration_fixture()
        old = installation.get('configure-user')
        plan = center.plan_user_deployment(payload)
        original = self.repository.stage_model_deployment_revision_tx
        def fail_after_stage(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('crash-after-stage')
        with self.repository._connect() as db:
            before = tuple(db.iterdump())
        with patch.object(self.repository, 'stage_model_deployment_revision_tx', fail_after_stage):
            with self.assertRaisesRegex(RuntimeError, 'crash-after-stage'):
                center.create_user_deployment(plan['operation'], idempotency_key='atomic-config')
        with self.repository._connect() as db:
            self.assertEqual(tuple(db.iterdump()), before)
        for key in ('configuration_context_json', 'configuration_context_digest', 'configuration_context'):
            with self.assertRaises(ServiceCenterError):
                center.create_user_deployment(dict(plan['operation'], **{key: 'untrusted'}), idempotency_key='bad-private')
        operation = center.create_user_deployment(plan['operation'], idempotency_key='atomic-config')
        with self.repository._connect() as db:
            saved = db.execute('SELECT configuration_context_digest FROM deployment_operations WHERE id=?', (operation['id'],)).fetchone()[0]
            db.execute("UPDATE deployment_operations SET configuration_context_digest='broken' WHERE id=?", (operation['id'],))
        with self.assertRaisesRegex(ValueError, 'deployment_configuration_context_corrupt'):
            self.repository.get_configuration_context(operation['id'])
        with self.repository._connect() as db:
            db.execute('UPDATE deployment_operations SET configuration_context_digest=? WHERE id=?', (saved, operation['id']))
            raw = db.execute('SELECT configuration_context_json FROM deployment_operations WHERE id=?', (operation['id'],)).fetchone()[0]
            db.execute('UPDATE deployment_operations SET configuration_context_json=NULL WHERE id=?', (operation['id'],))
        with self.assertRaisesRegex(ValueError, 'deployment_configuration_context_corrupt'):
            authority.set_service('configure-user', True)
        with self.repository._connect() as db:
            db.execute('UPDATE deployment_operations SET configuration_context_json=? WHERE id=?', (raw, operation['id']))
        self.assertEqual(installation.get('configure-user'), old)
        center.cancel_deployment_operation(operation['id'])
        center._execute_user_deployment(operation['id'])

    def test_configuration_switch_failure_can_abort_without_uninstalling_old_binding(self):
        center, installation, authority, _, second, payload = self.configuration_fixture()
        old = installation.get('configure-user')
        operation = center.create_user_deployment(center.plan_user_deployment(payload)['operation'], idempotency_key='failed-switch')
        self.assertEqual(center._execute_user_deployment(operation['id'])['state'], 'health_check')
        with self.repository._connect() as db:
            db.execute("UPDATE model_assets SET state='archived' WHERE id=?", (second['id'],))
        with self.assertRaisesRegex(ValueError, 'deployment_configuration_asset_unavailable'):
            authority.apply_pending('configure-user')
        # Simulate the reconciler receiving a deterministic switch failure.
        from mediacenter.reconciler import Reconciler
        runner = SimpleNamespace(authority=authority, repository=self.repository)
        Reconciler._configuration_failed(runner, 'configure-user', ValueError('candidate-unavailable'))
        self.assertEqual(center.get_deployment_operation(operation['id'])['state'], 'failed')
        self.assertEqual(installation.get('configure-user'), old)
        self.assertIsNone(self.repository.get_deployment('configure-user')['pending_config_revision'])

    def test_configuration_switch_fault_rolls_back_both_binding_and_policy(self):
        center, installation, authority, _, _, payload = self.configuration_fixture()
        old = installation.get('configure-user')
        operation = center.create_user_deployment(center.plan_user_deployment(payload)['operation'], idempotency_key='switch-crash')
        center._execute_user_deployment(operation['id'])
        with self.repository._connect() as db:
            before = tuple(db.iterdump())
        def fault(stage):
            if stage == 'policy.pending_applied':
                raise RuntimeError('switch-crash')
        authority.fault = fault
        with self.assertRaisesRegex(RuntimeError, 'switch-crash'):
            authority.apply_pending('configure-user')
        with self.repository._connect() as db:
            self.assertEqual(tuple(db.iterdump()), before)
        self.assertEqual(installation.get('configure-user'), old)
        authority.fault = lambda _: None
        self.assertTrue(authority.apply_pending('configure-user'))
        # Process reconstruction must see the same pending candidate, not build
        # a new revision or replace the original rollback base.
        recovered = InstancePolicy(self.repository)
        self.assertFalse(recovered.begin_configuration_operation(operation['id']))
        self.assertEqual(recovered.get('configure-user')['configuration_state'], 'applying')
        materialized = []
        runner = SimpleNamespace(authority=recovered, repository=self.repository,
                                 materialize_stopped_instance=materialized.append)
        Reconciler._reconcile_configuration(runner, 'configure-user')
        self.assertEqual(materialized, ['configure-user'])
        recovered.begin_configuration_rollback('configure-user', 'test-restart')
        self.assertTrue(recovered.rollback_configuration('configure-user'))
        self.assertEqual(installation.get('configure-user'), old)

    def seed_configuration_health_evidence(self, operation_id, authority, *, started=False, loaded=False):
        """Deterministic DB authority fixture, not a real Engine/GPU assertion."""
        context = self.repository.get_configuration_context(operation_id)
        instance = context['instance_id']
        policy = authority.get(instance)
        record = {'binding_digest': context['candidate']['installation']['binding_digest']}
        with self.repository._connect() as db:
            db.execute("INSERT INTO instance_claims(claim_id,instance_id,epoch,backend,incarnation,revision,"
                "policy_json,policy_digest,state,registered,created_at,updated_at) VALUES(?,?,?,'container',?,?,?,?,?,?,?,?)",
                ('config-claim', instance, 'config-epoch', context['incarnation'], policy['revision'],
                 canonical(policy['policy']), digest(policy['policy']),
                 'loaded' if loaded else 'unloaded' if started else 'container_stopped', int(started), 'now','now'))
            db.execute("INSERT INTO runtime_intents(intent_id,instance_id,epoch,engine_id,container_name,desired_revision,"
                "spec_json,spec_digest,image_id,mount_grants_json,mount_grants_digest,intent_digest,state,created_at,updated_at,"
                "generation,identity_version,claim_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,2,?)",
                ('config-intent', instance, 'config-epoch', 'fixture-engine', 'fixture-container', policy['revision'],
                 '{}', digest({}), 'fixture-image', '{}', digest({}), 'fixture-intent',
                 'running' if started else 'created', 'now','now','config-claim'))
            db.execute("INSERT INTO runtime_epoch_packages(package_id,instance_id,incarnation,epoch,desired_revision,generation,"
                "template_digest,authority_digest,phase,record_json,record_digest,created_at,updated_at) "
                "VALUES(?,?,?,?,?,1,?,?,'ready',?,?,?,?)", ('config-package', instance, context['incarnation'], 'config-epoch',
                 policy['revision'], context['candidate']['installation']['template_digest'], 'fixture-authority',
                 canonical(record), digest(record), 'now','now'))

    def test_stopped_service_retains_confirmed_container_without_showing_stopping(self):
        center, _, authority, _, _, payload = self.configuration_fixture()
        operation = center.create_user_deployment(
            center.plan_user_deployment(payload)['operation'], idempotency_key='stopped-projection')
        center._execute_user_deployment(operation['id'])
        authority.apply_pending('configure-user')
        self.seed_configuration_health_evidence(operation['id'], authority)
        with self.repository._connect() as db:
            db.execute("UPDATE instance_policies SET status='unloaded' WHERE instance_id='configure-user'")
        authority.commit_configuration('configure-user')

        def status():
            # Exercise the real projection, not the fixture's lifecycle stub.
            return ServiceCenter._runtime_status(center, 'configure-user')

        confirmed = status()
        self.assertEqual(confirmed['service_state'], 'stopped')
        self.assertEqual(confirmed['container_state'], 'created')
        self.assertFalse(confirmed['accepting_tasks'])
        self.assertEqual(confirmed['desired_service_state'], 'stopped')
        cases = (
            ('instance_claims', 'state', 'loaded', 'container_stopped'),
            ('instance_claims', 'registered', 1, 0),
            ('instance_claims', 'stop_reason', 'draining', None),
            ('instance_policies', 'status', 'unloading', 'unloaded'),
            ('runtime_intents', 'state', 'running', 'created'),
            ('runtime_intents', 'state', 'created_unverified', 'created'),
            ('runtime_intents', 'state', 'exit_unconfirmed', 'created'),
            ('runtime_intents', 'epoch', 'another-epoch', 'config-epoch'),
            ('runtime_intents', 'claim_id', 'another-claim', 'config-claim'),
        )
        for table, column, value, original in cases:
            with self.subTest(table=table, column=column, value=value):
                with self.repository._connect() as db:
                    db.execute(f'UPDATE {table} SET {column}=? WHERE instance_id=?',
                               (value, 'configure-user'))
                self.assertEqual(status()['service_state'], 'stopping')
                self.assertFalse(status()['accepting_tasks'])
                with self.repository._connect() as db:
                    db.execute(f'UPDATE {table} SET {column}=? WHERE instance_id=?',
                               (original, 'configure-user'))
        self.assertEqual(status()['service_state'], 'stopped')

    def test_configuration_health_commit_is_atomic_and_replayable(self):
        center, installation, authority, _, second, payload = self.configuration_fixture()
        operation = center.create_user_deployment(center.plan_user_deployment(payload)['operation'], idempotency_key='health-config')
        # Unrelated old-epoch activity can advance the observation version after
        # admission. The frozen policy identity must still be applicable.
        with self.repository._connect() as db:
            db.execute('UPDATE instance_policies SET version=version+1 WHERE instance_id=?', ('configure-user',))
        self.assertEqual(center._execute_user_deployment(operation['id'])['state'], 'health_check')
        authority.apply_pending('configure-user')
        self.seed_configuration_health_evidence(operation['id'], authority)
        with self.repository._connect() as db:
            before = tuple(db.iterdump())
        def fault(stage):
            if stage == 'policy.configuration_committed':
                raise RuntimeError('commit-crash')
        authority.fault = fault
        with self.assertRaisesRegex(RuntimeError, 'commit-crash'):
            authority.commit_configuration('configure-user')
        with self.repository._connect() as db:
            self.assertEqual(tuple(db.iterdump()), before)
        authority.fault = lambda _: None
        authority.commit_configuration('configure-user')
        self.assertEqual(center.get_deployment_operation(operation['id'])['state'], 'ready')
        self.assertEqual(self.repository.get_deployment('configure-user')['current_config_revision'], 2)
        self.assertEqual(installation.get('configure-user')['asset_id'], second['id'])
        self.assertEqual(authority.commit_configuration('configure-user')['configuration_state'], 'applied')

    def test_configuration_resident_health_requires_loaded_and_matching_package(self):
        center, installation, authority, _, _, payload = self.configuration_fixture()
        started = authority.set_service('configure-user', True)
        payload['expected_configuration']['policy_version'] = started['version']
        payload['residency'] = 'resident'
        operation = center.create_user_deployment(center.plan_user_deployment(payload)['operation'], idempotency_key='resident-config')
        center._execute_user_deployment(operation['id'])
        authority.apply_pending('configure-user')
        self.seed_configuration_health_evidence(operation['id'], authority, started=True)
        with self.assertRaisesRegex(TaskStateError, 'deployment_configuration_health_unconfirmed'):
            authority.commit_configuration('configure-user')
        with self.repository._connect() as db:
            db.execute("UPDATE instance_claims SET state='loaded' WHERE claim_id='config-claim'")
            record = db.execute("SELECT record_json,record_digest FROM runtime_epoch_packages WHERE package_id='config-package'").fetchone()
            wrong = {'binding_digest': 'another-installation'}
            db.execute("UPDATE runtime_epoch_packages SET record_json=?,record_digest=? WHERE package_id='config-package'",
                       (canonical(wrong), digest(wrong)))
        with self.assertRaisesRegex(TaskStateError, 'deployment_configuration_health_unconfirmed'):
            authority.commit_configuration('configure-user')
        with self.repository._connect() as db:
            db.execute("UPDATE runtime_epoch_packages SET record_json=?,record_digest=? WHERE package_id='config-package'", tuple(record))
        authority.commit_configuration('configure-user')
        self.assertEqual(center.get_deployment_operation(operation['id'])['state'], 'ready')

    def test_configuration_rename_and_cancellation_race_preserve_user_intent(self):
        center, installation, authority, _, _, payload = self.configuration_fixture()
        old = installation.get('configure-user')
        operation = center.create_user_deployment(center.plan_user_deployment(payload)['operation'], idempotency_key='cancel-race')
        center._execute_user_deployment(operation['id'])
        authority.apply_pending('configure-user')
        retired = []
        center.retire_worker = retired.append
        with self.assertRaises(ServiceCenterError):
            center.update_deployment('configure-user', {'enabled': True})
        self.assertEqual(retired, [])
        center.update_deployment('configure-user', {'label': 'My renamed model'})
        center.cancel_deployment_operation(operation['id'])
        # Reconciler wins before the operation runner classifies cancellation.
        authority.begin_configuration_rollback('configure-user', 'health-failed')
        authority.rollback_configuration('configure-user')
        result = center.get_deployment_operation(operation['id'])
        self.assertEqual((result['state'], result['error_class'], result['error_code']),
                         ('canceled', 'canceled', 'user_canceled'))
        self.assertEqual(installation.get('configure-user'), old)
        self.assertEqual(self.repository.get_deployment('configure-user')['label'], 'My renamed model')

    def test_existing_user_configuration_preview_is_read_only_and_binds_all_identities(self):
        center, installation, make_plan, _, lifecycle, base = self.user_deployment_fixture()
        request = make_plan('editable-user')
        result = self.wait_operation(center, center.create_user_deployment(
            request, idempotency_key='editable-install'))
        self.assertEqual(result['state'], 'ready', result)
        self.assertTrue(center.stop_deployment_worker())
        vae = self.metadata(self.publish('edit-vae', 'vae', 62), family='sdxl')
        second = self.metadata(self.publish('edit-base', 'checkpoint', 63), family='sdxl')
        profile = RuntimeProfileManager(self.repository).register(dict(
            RuntimeProfileManager.sdxl_single_file(request['runtime_profile']['image_digest']), revision=2))
        old = self.repository.get_model_deployment_revision('editable-user', 1)
        policy = lifecycle.authority.get('editable-user')
        payload = dict(center._plan_payload(request), expected_configuration={
            'config_revision': 1, 'config_digest': old['config_digest'],
            'policy_version': policy['version'],
        }, policy_options={'external_reserve_mib': 8192, 'idle_seconds': 0,
                           'restart_recovery': False})
        with self.assertRaisesRegex(DeploymentError, '配置没有变化'):
            center.deployments.plan_user_configuration(payload)
        with self.repository._connect() as db:
            before = tuple(db.iterdump())
        planned = center.deployments.plan_user_configuration(dict(
            payload, base_asset_id=second['id'], vae_asset_id=vae['id'], runtime_profile_revision=2))
        revision = planned['configuration_revision']
        self.assertEqual((revision['config_revision'], revision['base_asset_id'], revision['vae_asset_id'],
                          revision['runtime_profile_digest']), (2, second['id'], vae['id'], profile['profile_digest']))
        self.assertEqual(revision['config_digest'], deployment_config_digest(revision))
        self.assertTrue(planned['effects']['requires_restart'])
        self.assertFalse(planned['effects']['deletes_assets'])
        self.assertFalse(planned['effects']['rebuilds_runtime_image'])
        self.assertEqual(planned['compatibility']['disposition'], 'preview')
        self.assertEqual(planned['operation']['expected_configuration'], payload['expected_configuration'])
        with self.repository._connect() as db:
            self.assertEqual(tuple(db.iterdump()), before)
        self.assertEqual(installation.get('editable-user')['asset_id'], base['id'])
        RuntimeProfileManager(self.repository).register(dict(
            RuntimeProfileManager.sdxl_single_file(request['runtime_profile']['image_digest']),
            revision=3, optional_deployment_roles=[]))
        with self.repository._connect() as db:
            before_unsupported = tuple(db.iterdump())
        with self.assertRaises(DeploymentError) as unsupported:
            center.deployments.plan_user_configuration(dict(payload, vae_asset_id=vae['id'],
                                                           runtime_profile_revision=3))
        self.assertEqual(unsupported.exception.code, 'runtime_vae_unsupported')
        no_vae = center.deployments.plan_user_configuration(dict(payload, runtime_profile_revision=3))
        self.assertIsNone(no_vae['operation']['vae_asset'])
        with self.repository._connect() as db:
            self.assertEqual(tuple(db.iterdump()), before_unsupported)
        hot = center.deployments.plan_user_configuration(dict(payload, residency='resident'))
        self.assertFalse(hot['effects']['requires_restart'])
        for expected in (
            dict(payload['expected_configuration'], config_digest='0' * 64),
            dict(payload['expected_configuration'], policy_version=policy['version']+1),
        ):
            with self.subTest(expected=expected), self.assertRaises(DeploymentError) as caught:
                center.deployments.plan_user_configuration(dict(payload, expected_configuration=expected))
            self.assertEqual(caught.exception.code, 'deployment_config_revision_conflict')
        for bad in (dict(payload, extra=True), dict(payload, expected_configuration={}),
                    dict(payload, policy_options=dict(payload['policy_options'], idle_seconds=True))):
            with self.subTest(bad=bad), self.assertRaises(DeploymentError):
                center.deployments.plan_user_configuration(bad)
        with self.assertRaises(DeploymentError) as caught:
            center.deployments.plan_user_configuration(dict(payload, vae_asset_id=second['id']))
        self.assertEqual(caught.exception.code, 'model_asset_incompatible')
        with self.repository._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.repository.stage_model_deployment_revision_tx(db, revision, expected_current=1)
        with self.assertRaises(DeploymentError) as caught:
            center.deployments.plan_user_configuration(dict(payload, vae_asset_id=vae['id']))
        self.assertEqual(caught.exception.code, 'deployment_configuration_pending')
        with self.repository._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.repository.rollback_model_deployment_revision_tx(db, 'editable-user', '2026-09-05T00:00:00Z')
        retried = center.deployments.plan_user_configuration(dict(payload, vae_asset_id=vae['id']))
        self.assertEqual(retried['configuration_revision']['config_revision'], 3)
        with self.repository._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            with self.assertRaisesRegex(ValueError, 'deployment_config_revision_conflict'):
                self.repository.stage_model_deployment_revision_tx(db, revision, expected_current=1)

    def test_vae_install_plan_does_not_publish_compatibility_until_admission(self):
        center, _, make_plan, _, _, _ = self.user_deployment_fixture()
        vae = self.metadata(self.publish('preview-vae', 'vae', 64), family='sdxl')
        payload = dict(center._plan_payload(make_plan('preview-user')), vae_asset_id=vae['id'])
        with self.repository._connect() as db:
            before = tuple(db.iterdump())
        planned = center.plan_user_deployment(payload)
        with self.repository._connect() as db:
            self.assertEqual(tuple(db.iterdump()), before)
        result = self.wait_operation(center, center.create_user_deployment(
            planned['operation'], idempotency_key='preview-install'))
        self.assertEqual(result['state'], 'ready', result)
        with self.repository._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM asset_compatibility').fetchone()[0], 1)

    def test_http_remains_responsive_and_cancel_during_import_preserves_assets(self):
        import http.client
        from mediacenter.server import Handler, MediaCenterHTTPServer
        center, installation, plan, _, _, base = self.user_deployment_fixture()
        entered, release = threading.Event(), threading.Event()
        original = self.importer.load
        def delayed(*args):
            original(*args)
            entered.set()
            if not release.wait(8):
                raise RuntimeError('test import release timed out')
        self.importer.load = delayed
        server = MediaCenterHTTPServer(('127.0.0.1', 0), Handler)
        server.center, server.api_key = center, 'fixture-key'
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(method, path, body=None):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
            try:
                connection.request(method, path, body=None if body is None else json.dumps(body),
                    headers={'X-API-Key': 'fixture-key', 'Idempotency-Key': 'http-runtime',
                             'Content-Type': 'application/json'})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        try:
            status, operation = request('POST', '/api/v1/deployment-operations', plan('http-runtime'))
            self.assertEqual((status, operation['state']), (202, 'accepted'))
            self.assertTrue(entered.wait(3))
            status, snapshot = request('GET', '/api/v1/deployment-operations/' + operation['id'])
            self.assertEqual((status, snapshot['state']), (200, 'preparing_runtime'))
            status, canceled = request('POST', '/api/v1/deployment-operations/' + operation['id'] + '/cancel', {})
            self.assertEqual((status, canceled['state']), (200, 'canceling'))
            release.set()
            terminal = self.wait_operation(center, operation)
            self.assertEqual(terminal['state'], 'canceled', terminal)
            self.assertIsNone(installation.get('http-runtime'))
            self.assertEqual(self.repository.get_model_asset(base['id'])['state'], 'ready')
            self.assertEqual(self.repository.get_service_installation(operation['id'])['state'], 'canceled')
            with self.repository._connect() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM runtime_image_bindings').fetchone()[0], 1)
        finally:
            release.set()
            server.shutdown()
            thread.join(2)
            server.server_close()

    def test_restart_after_binding_and_after_container_continues_same_operation(self):
        center, installation, plan, new_center, lifecycle, _ = self.user_deployment_fixture()
        manager = DeploymentOperationManager(self.repository)
        for point in ('binding', 'container'):
            operation = manager.create(plan('recover-' + point), server_profile_id=center.task_state.server_id,
                authenticated_principal='server-admin', idempotency_key='recover-' + point)
            target, name = (installation, 'commit_user') if point == 'binding' else (lifecycle, 'install_instance')
            original = getattr(target, name)
            def interrupted(*args):
                original(*args)
                raise SystemExit('simulate loss after durable side effect')
            setattr(target, name, interrupted)
            try:
                with self.assertRaises(SystemExit):
                    center._execute_user_deployment(operation['id'])
            finally:
                setattr(target, name, original)
            self.assertEqual(manager.get(operation['id'])['state'], 'creating_container')
            binding = installation.get(operation['deployment_id'])
            self.assertEqual(binding['operation_id'], operation['id'])
            resumed = new_center()
            resumed.start_deployment_worker()
            result = self.wait_operation(resumed, operation)
            self.assertEqual(result['state'], 'ready', result)
            self.assertEqual(installation.get(operation['deployment_id']), binding)
            self.assertTrue(next(item for item in result['resources'] if item['kind'] == 'deployment')['created'])
            self.assertTrue(resumed.stop_deployment_worker())
        self.assertEqual(self.importer.calls, 1)

    def test_unknown_import_on_restart_is_quarantined_without_reissuing_load(self):
        center, installation, plan, new_center, _, _ = self.user_deployment_fixture()
        operation = DeploymentOperationManager(self.repository).create(plan('unknown-import'),
            server_profile_id=center.task_state.server_id, authenticated_principal='server-admin',
            idempotency_key='unknown-import')
        def interrupted(point):
            if point == 'runtime.import.external':
                raise SystemExit('lost engine load response')
        installation.images.fault = interrupted
        with self.assertRaises(SystemExit):
            center._execute_user_deployment(operation['id'])
        self.assertEqual(self.importer.calls, 1)
        installation.images.fault = lambda _: None
        resumed = new_center()
        resumed.start_deployment_worker()
        failed = self.wait_operation(resumed, operation)
        self.assertEqual((failed['state'], failed['error_code'], failed['error_class']),
                         ('failed', 'runtime_import_outcome_unknown', 'non_recoverable'))
        self.assertEqual(self.importer.calls, 1)
        with self.repository._connect() as db:
            self.assertEqual(db.execute('SELECT phase FROM runtime_image_transfers WHERE operation_id=?',
                                        (operation['id'],)).fetchone()[0], 'import_pending')
        self.assertEqual(self.repository.get_service_installation(operation['id'])['state'], 'failed')

    def test_catalog_recovery_does_not_take_over_user_preparation_ledger(self):
        center, installation, plan, _, _, _ = self.user_deployment_fixture()
        manager = DeploymentOperationManager(self.repository)
        operation = manager.create(plan('catalog-isolation'), server_profile_id=center.task_state.server_id,
            authenticated_principal='server-admin', idempotency_key='catalog-isolation')
        self.prepare_runtime(installation, operation['id'])
        before = self.repository.get_service_installation(operation['id'])
        installer = ServiceInstaller(self.repository,
            Path(__file__).resolve().parents[1] / 'deploy' / 'model_catalog.json',
            self.assets, center.deployments, installation_runtime=installation, runtime_importer=self.importer)
        self.assertEqual(self.repository.get_service_installation(operation['id']), before)
        self.assertNotIn(operation['id'], [row['id'] for row in installer.list()])
        with self.assertRaises(ServiceInstallerError):
            installer.control(operation['id'], 'cancel')
        with self.assertRaises(RuntimeContractError):
            installation.prepare_user_runtime(operation['id'], self.importer, lambda: None)

    def test_local_partial_and_stale_runner_resume_without_using_network(self):
        from mediacenter.artifacts import identity
        center, installation, plan, new_center, _, _ = self.user_deployment_fixture()
        operation = DeploymentOperationManager(self.repository).create(plan('local-resume'),
            server_profile_id=center.task_state.server_id, authenticated_principal='server-admin',
            idempotency_key='local-resume')
        images = installation.images
        original = images.download
        saved = {}
        def lose_process(owner, transfer_id, **kwargs):
            row = images.get(transfer_id)
            path = images.root / row['local_path']
            path.write_bytes((self.root / 'approved-runtime.oci.tar').read_bytes()[:100])
            saved.update(transfer_id=transfer_id, identity=identity(path.stat()))
            with self.repository._connect() as db:
                db.execute("UPDATE runtime_image_transfers SET phase='downloading',object_json=? WHERE transfer_id=?",
                           (json.dumps(saved['identity']), transfer_id))
            raise SystemExit('process lost during local copy')
        images.download = lose_process
        try:
            with self.assertRaises(SystemExit):
                center._execute_user_deployment(operation['id'])
        finally:
            images.download = original
        with self.repository._connect() as db:
            db.execute("UPDATE installation_attempts SET runner_token='abandoned',owner_pid=0 WHERE operation_id=?",
                       (operation['id'],))
        @contextmanager
        def forbidden_network(*args):
            raise AssertionError('local recovery must not use HTTPS')
            yield
        images.source = forbidden_network
        resumed = new_center()
        resumed.start_deployment_worker()
        result = self.wait_operation(resumed, operation)
        self.assertEqual(result['state'], 'ready', result)
        transfer = images.get(saved['transfer_id'])
        self.assertEqual((transfer['phase'], transfer['operation_id']), ('ready', operation['id']))
        self.assertEqual(identity((images.root / transfer['local_path']).stat()), saved['identity'])
        self.assertEqual(self.importer.calls, 1)
        attempts = self.repository.installation_attempts(operation['id'])
        self.assertEqual(len(attempts), 1)
        self.assertIsNone(attempts[0]['runner_token'])

    def test_cleanup_failure_remains_recoverable_in_same_operation(self):
        center, installation, plan, new_center, lifecycle, _ = self.user_deployment_fixture()
        manager = DeploymentOperationManager(self.repository)
        operation = manager.create(plan('cleanup-recovery'), server_profile_id=center.task_state.server_id,
            authenticated_principal='server-admin', idempotency_key='cleanup-recovery')
        original = lifecycle.install_instance
        def cancel_after_materialize(instance):
            result = original(instance)
            manager.cancel(operation['id'])
            return result
        lifecycle.install_instance = cancel_after_materialize
        retired = []
        def transient_retire(instance):
            retired.append(instance)
            if len(retired) == 1:
                raise OSError('temporary engine outage')
        lifecycle.retire_instance_containers = transient_retire
        with self.assertLogs('mediacenter.service_center', level='ERROR'):
            interrupted = center._execute_user_deployment(operation['id'])
        self.assertEqual(interrupted['state'], 'rollback')
        self.assertEqual(interrupted['recovery_cursor']['step'], 'runtime_cleanup_required')
        self.assertIsNotNone(installation.get(operation['deployment_id']))
        resumed = new_center()
        resumed.start_deployment_worker()
        result = self.wait_operation(resumed, operation)
        self.assertEqual(result['state'], 'canceled', result)
        self.assertIsNone(installation.get(operation['deployment_id']))
        self.assertEqual(retired, [operation['deployment_id']] * 2)
        self.assertEqual(self.repository.get_service_installation(operation['id'])['state'], 'canceled')

    def test_operation_resources_preserve_creation_and_reject_identity_drift(self):
        center, _, plan, _, _, _ = self.user_deployment_fixture()
        manager = DeploymentOperationManager(self.repository)
        operation = manager.create(plan('resource-owner'), server_profile_id=center.task_state.server_id,
            authenticated_principal='server-admin', idempotency_key='resource-owner')
        manager.record_resource(operation['id'], kind='deployment', resource_id='resource-owner',
                                identity='incarnation-one', created=True)
        replay = manager.record_resource(operation['id'], kind='deployment', resource_id='resource-owner',
                                         identity='incarnation-one', created=False)
        self.assertTrue(replay['resources'][0]['created'])
        with self.assertRaisesRegex(ValueError, 'deployment_operation_resource_changed'):
            manager.record_resource(operation['id'], kind='deployment', resource_id='resource-owner',
                                    identity='incarnation-two', created=False)
        self.assertEqual(manager.get(operation['id'])['resources'], replay['resources'])

    def test_shutdown_waits_for_import_then_restart_resumes_safe_checkpoint(self):
        center, installation, plan, new_center, _, _ = self.user_deployment_fixture()
        entered, release = threading.Event(), threading.Event()
        original = self.importer.load
        def delayed(*args):
            original(*args)
            entered.set()
            if not release.wait(8):
                raise RuntimeError('test import release timed out')
        self.importer.load = delayed
        try:
            operation = center.create_user_deployment(plan('shutdown-import'), idempotency_key='shutdown-import')
            self.assertTrue(entered.wait(3))
            thread = center._deployment_thread
            center.start_deployment_worker()
            self.assertIs(center._deployment_thread, thread)
            self.assertFalse(center.stop_deployment_worker(timeout=0.01))
            attempt = self.repository.installation_attempts(operation['id'])[0]
            self.assertIsNotNone(attempt['runner_token'])
        finally:
            release.set()
        self.assertTrue(center.stop_deployment_worker())
        self.assertEqual(center.get_deployment_operation(operation['id'])['state'], 'preparing_runtime')
        self.assertIsNone(installation.get(operation['deployment_id']))
        resumed = new_center()
        resumed.start_deployment_worker()
        result = self.wait_operation(resumed, operation)
        self.assertEqual(result['state'], 'ready', result)
        self.assertEqual(self.importer.calls, 1)

    def test_restart_finds_accepted_work_behind_large_terminal_history(self):
        center, _, plan, _, _, _ = self.user_deployment_fixture()
        operation = DeploymentOperationManager(self.repository).create(plan('old-accepted'),
            server_profile_id=center.task_state.server_id, authenticated_principal='server-admin',
            idempotency_key='old-accepted')
        with self.repository._connect() as db:
            row = dict(db.execute('SELECT * FROM deployment_operations WHERE id=?', (operation['id'],)).fetchone())
            columns = list(row)
            statement = ('INSERT INTO deployment_operations (' + ','.join(columns) + ') VALUES ('
                         + ','.join('?' for _ in columns) + ')')
            for index in range(510):
                historical = dict(row, id=f'dop_{index:032x}', idempotency_key=f'fixture-history-{index}',
                                  state='ready', updated_at='2099-01-01T00:00:00+00:00')
                db.execute(statement, [historical[key] for key in columns])
        self.assertNotIn(operation['id'], [row['id'] for row in center.list_deployment_operations(500)])
        center.start_deployment_worker()
        result = self.wait_operation(center, operation)
        self.assertEqual(result['state'], 'ready', result)

    def test_user_sdxl_instances_share_runtime_image_but_bind_assets_independently(self):
        base = self.metadata(self.publish("wai", "checkpoint", 10), family="sdxl")
        second = self.metadata(self.publish("pony", "checkpoint", 11), family="sdxl")
        vae = self.metadata(self.publish("pony-vae", "vae", 12), family="sdxl")
        AssetCompatibilityManager(self.repository).assess(vae["id"], second["id"])
        release, runtime = self._runtime()
        profile = RuntimeProfileManager(self.repository).register(
            RuntimeProfileManager.sdxl_single_file(release.image_digest))
        deployments = ModelDeploymentManager(
            self.repository, Path(__file__).resolve().parents[1] / "deploy" / "model_catalog.json",
            self.root, (0, 1), self.assets.storage_root)
        operation_manager = DeploymentOperationManager(self.repository)

        def install(asset, deployment_id, gpu, selected_vae=None):
            plan = deployments.plan_user({
                "deployment_id": deployment_id, "base_asset_id": asset["id"],
                "vae_asset_id": selected_vae["id"] if selected_vae else None,
                "runtime_profile_id": profile["profile_id"],
                "runtime_profile_revision": profile["revision"],
                "gpu_uuids": [f"GPU-test-{gpu}"], "residency": "on_demand",
                "sharing_mode": "shared", "required_vram_mib": 16384,
                "license_accepted": True,
                "experimental_compatibility_accepted": False,
            })
            operation = operation_manager.create(
                plan["operation"], server_profile_id="server-one",
                authenticated_principal="operator-one",
                idempotency_key="install-" + deployment_id)
            deployments.create_user(plan["operation"], [gpu])
            self.prepare_runtime(runtime, operation['id'])
            binding = runtime.commit_user(operation["id"])
            return operation, binding

        first_operation, first = install(base, "wai-user", 0)
        second_operation, second_binding = install(second, "pony-user", 1, vae)
        self.assertEqual(first["release_digest"], second_binding["release_digest"])
        self.assertEqual(first["image_digest"], second_binding["image_digest"])
        self.assertNotEqual(first["instance_id"], second_binding["instance_id"])
        self.assertEqual(set(first["optional_assets"]), set())
        self.assertEqual(second_binding["optional_assets"]["vae"]["asset_id"], vae["id"])
        self.assertEqual(runtime.get("wai-user"), first)
        self.assertEqual(runtime.get("pony-user"), second_binding)
        self.assertEqual(runtime.template("wai-user").digest,
                         runtime.template("pony-user").digest)
        self.assertEqual(self.repository.get_deployment("wai-user")["install_state"], "ready")
        self.assertEqual(self.repository.get_deployment("pony-user")["install_state"], "ready")
        self.assertFalse(self.repository.get_deployment("wai-user")["enabled"])
        self.assertEqual(runtime.commit_user(first_operation["id"]), first)
        policy_revision = deployments.next_policy_revision("wai-user", {
            "gpus": ["GPU-test-0"], "sharing_mode": "exclusive",
            "external_reserve_mib": 4096, "residency": "idle",
            "idle_seconds": 300,
        })
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.repository.stage_model_deployment_revision_tx(
                db, policy_revision, expected_current=1)
            self.repository.commit_model_deployment_revision_tx(
                db, "wai-user", "2026-09-04T00:00:00+00:00")
        self.assertEqual(runtime.get("wai-user"), first)
        self.assertEqual(
            self.repository.get_model_deployment_revision("wai-user", 2)["idle_seconds"],
            300,
        )
        with self.repository._connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM runtime_image_bindings").fetchone()[0], 1)

    def test_server_runtime_configuration_registers_only_profile_bound_to_approved_release(self):
        release, runtime = self._runtime()
        profile = RuntimeProfileManager.sdxl_single_file(release.image_digest)

        registered = _register_runtime_profiles(
            self.repository, runtime.images, [release.data], [profile])

        self.assertEqual(registered[0]["profile_id"], "sdxl-single-file")
        self.assertEqual(
            RuntimeProfileManager(self.repository).get("sdxl-single-file", 1)["image_digest"],
            release.image_digest,
        )
        wrong = dict(profile, revision=2, image_digest="sha256:" + "f" * 64)
        with self.assertRaisesRegex(ContainerError, "runtime_profile_release_mismatch"):
            _register_runtime_profiles(
                self.repository, runtime.images, [release.data], [wrong])

    def test_user_runtime_binding_fails_closed_on_profile_or_optional_asset_drift(self):
        base = self.metadata(self.publish("base-drift", "checkpoint", 20), family="sdxl")
        vae = self.metadata(self.publish("vae-drift", "vae", 21), family="sdxl")
        AssetCompatibilityManager(self.repository).assess(vae["id"], base["id"])
        release, runtime = self._runtime()
        profile = RuntimeProfileManager(self.repository).register(
            RuntimeProfileManager.sdxl_single_file(release.image_digest))
        deployments = ModelDeploymentManager(
            self.repository, Path(__file__).resolve().parents[1] / "deploy" / "model_catalog.json",
            self.root, (0,), self.assets.storage_root)
        plan = deployments.plan_user({
            "deployment_id": "drift-user", "base_asset_id": base["id"],
            "vae_asset_id": vae["id"], "runtime_profile_id": profile["profile_id"],
            "runtime_profile_revision": profile["revision"],
            "gpu_uuids": ["GPU-test-0"], "residency": "resident",
            "sharing_mode": "exclusive", "required_vram_mib": 16384,
            "license_accepted": True, "experimental_compatibility_accepted": False,
        })
        operation = DeploymentOperationManager(self.repository).create(
            plan["operation"], server_profile_id="server-one",
            authenticated_principal="operator-one", idempotency_key="drift")
        deployments.create_user(plan["operation"], [0])
        self.prepare_runtime(runtime, operation['id'])
        runtime.commit_user(operation["id"])
        with self.repository._connect() as db:
            db.execute("UPDATE model_assets SET state='archived' WHERE id=?", (vae["id"],))
        with self.assertRaisesRegex(RuntimeContractError, "runtime_optional_asset_changed"):
            runtime.get("drift-user")

    def test_service_center_plan_and_idempotent_operation_create_stopped_instance(self):
        base = self.metadata(self.publish("api-base", "checkpoint", 30), family="sdxl")
        release, installation = self._runtime()
        profile = RuntimeProfileManager(self.repository).register(
            RuntimeProfileManager.sdxl_single_file(release.image_digest))
        deployments = ModelDeploymentManager(
            self.repository, Path(__file__).resolve().parents[1] / "deploy" / "model_catalog.json",
            self.root, (0,), self.assets.storage_root)
        registry = ModelRegistry(deployments, installation_runtime=installation)
        authority = InstancePolicy(self.repository)

        class Scheduler:
            allowed_uuids = ("GPU-test-0",)
            allowed_indices = (0,)

            @staticmethod
            def validate_budget(_policy):
                return True

        class Lifecycle:
            artifacts = None
            package_provider = object()
            scheduler = Scheduler()

            def __init__(self):
                self.authority = authority

            def install_instance(self, instance):
                template = installation.template(instance)
                with self.authority.repository._connect() as db:
                    deployment = db.execute(
                        "SELECT * FROM model_deployments WHERE id=?", (instance,)).fetchone()
                    binding = InstancePolicy.deployment_binding(db, deployment)
                policy = dict(template.data["resources"], backend="container",
                              binding=binding,
                              package_id="template_" + template.digest,
                              gpus=["GPU-test-0"])
                return self.authority.configure(instance, policy)

        lifecycle = Lifecycle()
        center = ServiceCenter(
            self.repository, registry, self.root / "artifacts", deployments=deployments,
            gpu_scheduler=lifecycle.scheduler, model_assets=self.assets, runtime=lifecycle,
            runtime_importer=self.importer)
        self.addCleanup(center.stop_deployment_worker)
        center._runtime_status = lambda deployment_id: {
            "container_state": "created", "service_state": "stopped",
            "actual_state": "unloaded", "desired_state": "unloaded",
        }
        request = {
            "deployment_id": "api-user", "base_asset_id": base["id"],
            "vae_asset_id": None, "runtime_profile_id": profile["profile_id"],
            "runtime_profile_revision": profile["revision"],
            "gpu_uuids": ["GPU-test-0"], "residency": "idle",
            "sharing_mode": "shared", "required_vram_mib": 16384,
            "license_accepted": True, "experimental_compatibility_accepted": False,
        }
        plan = center.plan_user_deployment(request)
        self.assertTrue(plan["capacity"]["schedulable"])
        self.assertNotIn("storage_relpath", json.dumps(plan))
        created = center.create_user_deployment(
            plan["operation"], idempotency_key="api-install-one")
        self.assertEqual(created['state'], 'accepted')
        created = self.wait_operation(center, created)
        replay = center.create_user_deployment(
            plan["operation"], idempotency_key="api-install-one")
        self.assertEqual((created["state"], replay["id"]), ("ready", created["id"]))
        self.assertTrue(created["result"]["container_materialized"])
        deployment = self.repository.get_deployment("api-user")
        self.assertEqual((deployment["install_state"], deployment["enabled"]),
                         ("ready", False))
        state = authority.get("api-user")
        self.assertEqual((state["policy"]["residency"], state["desired_state"]),
                         ("idle", "unloaded"))
        center.configure_instance_policy("api-user", {
            "version": state["version"], "gpu_uuids": ["GPU-test-0"],
            "sharing_mode": "shared", "external_reserve_mib": 8192,
            "residency": "resident", "idle_minutes": 0,
            "restart_recovery": False,
        })
        staged = authority.get("api-user")
        deployment = self.repository.get_deployment("api-user")
        self.assertEqual((staged["configuration_state"],
                          deployment["current_config_revision"],
                          deployment["pending_config_revision"]),
                         ("applying", 1, 2))
        committed = authority.commit_configuration("api-user")
        deployment = self.repository.get_deployment("api-user")
        revision = self.repository.get_model_deployment_revision("api-user", 2)
        self.assertEqual((deployment["current_config_revision"],
                          deployment["pending_config_revision"],
                          revision["residency"], revision["idle_seconds"]),
                         (2, None, "resident", 0))

        center.configure_instance_policy("api-user", {
            "version": committed["version"], "gpu_uuids": ["GPU-test-0"],
            "sharing_mode": "shared", "external_reserve_mib": 8192,
            "residency": "on_demand", "idle_minutes": 0,
            "restart_recovery": False,
        })
        authority.begin_configuration_rollback("api-user", "candidate_health_failed")
        self.assertTrue(authority.rollback_configuration("api-user"))
        deployment = self.repository.get_deployment("api-user")
        restored = authority.get("api-user")
        self.assertEqual((deployment["current_config_revision"],
                          deployment["pending_config_revision"],
                          restored["policy"]["residency"]),
                         (2, None, "resident"))
        failed_revision = self.repository.get_model_deployment_revision("api-user", 3)
        retry = {
            "version": restored["version"], "gpu_uuids": ["GPU-test-0"],
            "sharing_mode": "shared", "external_reserve_mib": 8192,
            "residency": "on_demand", "idle_minutes": 0,
            "restart_recovery": False,
        }
        center.configure_instance_policy("api-user", retry)
        self.assertEqual(self.repository.get_deployment("api-user")["pending_config_revision"], 4)
        self.assertEqual(self.repository.get_model_deployment_revision("api-user", 3), failed_revision)
        # A stale command cannot acquire another revision while one is pending.
        with self.assertRaises(ServiceCenterError):
            center.configure_instance_policy("api-user", retry)
        authority.commit_configuration("api-user")
        self.assertEqual(self.repository.get_deployment("api-user")["current_config_revision"], 4)
        self.assertEqual([r["config_revision"] for r in
                          self.repository.list_model_deployment_revisions("api-user")], [4, 3, 2, 1])
        changed = dict(plan["operation"], residency="resident")
        with self.assertRaises(ServiceCenterError) as conflict:
            center.create_user_deployment(changed, idempotency_key="api-install-one")
        self.assertEqual(conflict.exception.code, "idempotency_conflict")

    def test_failed_and_canceled_user_deployments_preserve_assets_and_retry_exact_config(self):
        base = self.metadata(self.publish("retry-base", "checkpoint", 36), family="sdxl")
        release, installation = self._runtime()
        profile = RuntimeProfileManager(self.repository).register(
            RuntimeProfileManager.sdxl_single_file(release.image_digest))
        deployments = ModelDeploymentManager(
            self.repository, Path(__file__).resolve().parents[1] / "deploy" / "model_catalog.json",
            self.root, (0,), self.assets.storage_root)
        registry = ModelRegistry(deployments, installation_runtime=installation)
        authority = InstancePolicy(self.repository)

        class Scheduler:
            allowed_uuids = ("GPU-test-0",)
            allowed_indices = (0,)

            @staticmethod
            def validate_budget(_policy):
                return True

        class Lifecycle:
            artifacts = None
            package_provider = object()
            scheduler = Scheduler()

            def __init__(self):
                self.authority = authority
                self.failure = None
                self.cancel_next = False

            def install_instance(self, instance):
                if self.failure:
                    error, self.failure = self.failure, None
                    raise TaskStateError(error)
                template = installation.template(instance)
                with self.authority.repository._connect() as db:
                    deployment = db.execute(
                        "SELECT * FROM model_deployments WHERE id=?", (instance,)).fetchone()
                    binding = InstancePolicy.deployment_binding(db, deployment)
                policy = dict(template.data["resources"], backend="container",
                              binding=binding, package_id="template_" + template.digest,
                              gpus=["GPU-test-0"])
                old = self.authority.get(instance)
                result = (old if old and old["policy"] == policy else
                          self.authority.configure(instance, policy))
                if self.cancel_next:
                    self.cancel_next = False
                    DeploymentOperationManager(self.authority.repository).cancel(
                        installation.get(instance)["operation_id"])
                return result

        lifecycle = Lifecycle()
        center = ServiceCenter(
            self.repository, registry, self.root / "artifacts", deployments=deployments,
            gpu_scheduler=lifecycle.scheduler, model_assets=self.assets, runtime=lifecycle,
            runtime_importer=self.importer)
        self.addCleanup(center.stop_deployment_worker)
        center._runtime_status = lambda deployment_id: {
            "container_state": "created", "service_state": "stopped",
            "actual_state": "unloaded", "desired_state": "unloaded",
        }

        def plan(deployment_id):
            return center.plan_user_deployment({
                "deployment_id": deployment_id, "base_asset_id": base["id"],
                "vae_asset_id": None, "runtime_profile_id": profile["profile_id"],
                "runtime_profile_revision": profile["revision"],
                "gpu_uuids": ["GPU-test-0"], "residency": "idle",
                "sharing_mode": "shared", "required_vram_mib": 16384,
                "license_accepted": True,
                "experimental_compatibility_accepted": False,
            })["operation"]

        failed_plan = plan("retry-after-failure")
        lifecycle.failure = "container_materialization_unconfirmed"
        failed = center.create_user_deployment(
            failed_plan, idempotency_key="failure-one")
        failed = self.wait_operation(center, failed)
        self.assertEqual((failed["state"], failed["error_class"]),
                         ("failed", "recoverable"))
        self.assertEqual(failed["recovery_cursor"]["step"], "runtime_cleanup_complete")
        self.assertIsNone(installation.get("retry-after-failure"))
        self.assertEqual(self.repository.get_deployment("retry-after-failure")["install_state"],
                         "configured")
        retried = center.create_user_deployment(
            failed_plan, idempotency_key="failure-two")
        retried = self.wait_operation(center, retried)
        self.assertEqual(retried["state"], "ready")
        with self.repository._connect() as db:
            db.execute("UPDATE model_deployments SET is_default=1 "
                       "WHERE id='retry-after-failure'")
        registry.refresh()

        canceled_plan = plan("retry-after-cancel")
        lifecycle.cancel_next = True
        canceled = center.create_user_deployment(
            canceled_plan, idempotency_key="cancel-one")
        canceled = self.wait_operation(center, canceled)
        self.assertEqual(canceled["state"], "canceled", canceled)
        self.assertIsNone(installation.get("retry-after-cancel"))
        self.assertEqual(self.repository.get_deployment("retry-after-cancel")["install_state"],
                         "configured")
        retry_canceled = center.create_user_deployment(
            canceled_plan, idempotency_key="cancel-two")
        retry_canceled = self.wait_operation(center, retry_canceled)
        self.assertEqual(retry_canceled["state"], "ready")
        self.assertEqual(self.repository.get_model_asset(base["id"])["state"], "ready")

    def test_reconciler_rebuilds_policy_from_current_deployment_revision(self):
        base = self.metadata(self.publish("revision-rebuild", "checkpoint", 31), family="sdxl")
        release, installation = self._runtime()
        profile = RuntimeProfileManager(self.repository).register(
            RuntimeProfileManager.sdxl_single_file(release.image_digest))
        deployments = ModelDeploymentManager(
            self.repository, Path(__file__).resolve().parents[1] / "deploy" / "model_catalog.json",
            self.root, (0, 1), self.assets.storage_root)
        plan = deployments.plan_user({
            "deployment_id": "revision-rebuild", "base_asset_id": base["id"],
            "vae_asset_id": None, "runtime_profile_id": profile["profile_id"],
            "runtime_profile_revision": profile["revision"],
            "gpu_uuids": ["GPU-test-0"], "residency": "on_demand",
            "sharing_mode": "shared", "required_vram_mib": 16384,
            "license_accepted": True, "experimental_compatibility_accepted": False,
        })
        operation = DeploymentOperationManager(self.repository).create(
            plan["operation"], server_profile_id="server-one",
            authenticated_principal="operator-one", idempotency_key="revision-rebuild")
        deployments.create_user(plan["operation"], [0])
        self.prepare_runtime(installation, operation['id'])
        installation.commit_user(operation["id"])
        next_revision = deployments.next_policy_revision("revision-rebuild", {
            "gpus": ["GPU-test-1"], "sharing_mode": "exclusive",
            "external_reserve_mib": 4096, "residency": "idle", "idle_seconds": 180,
        })
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.repository.stage_model_deployment_revision_tx(
                db, next_revision, expected_current=1)
            self.repository.commit_model_deployment_revision_tx(
                db, "revision-rebuild", "2026-09-04T00:00:00+00:00")
            db.execute("UPDATE model_deployments SET enabled=1 WHERE id='revision-rebuild'")

        authority = InstancePolicy(self.repository)

        class Scheduler:
            allowed_indices = (0, 1)
            allowed_uuids = ("GPU-test-0", "GPU-test-1")

            def __init__(self):
                self.authority = authority

            @staticmethod
            def validate_budget(_policy):
                return True

        class PackageProvider:
            @staticmethod
            def validate_claim(_db, _record_id):
                return None

            def default_policy(inner_self, instance, gpu_uuids,
                               *, include_runtime_options=True):
                with self.repository._connect() as db:
                    deployment = db.execute(
                        "SELECT * FROM model_deployments WHERE id=?", (instance,)).fetchone()
                    binding = InstancePolicy.deployment_binding(db, deployment)
                    installed = db.execute(
                        "SELECT template_json,template_digest FROM instance_installation_bindings WHERE instance_id=?",
                        (instance,),
                    ).fetchone()
                resources = json.loads(installed["template_json"])["resources"]
                return dict(resources, **{
                    "backend": "container", "residency": "on_demand", "idle_seconds": 0,
                    "gpus": list(gpu_uuids),
                    "binding": binding, "package_id": "template_" + installed["template_digest"],
                    "sharing_mode": "shared", "external_reserve_mib": 8192,
                })

        row = Reconciler(
            self.repository, object(), Scheduler(), package_provider=PackageProvider(),
        ).install_instance("revision-rebuild")
        self.assertEqual(row["policy"]["gpus"], ["GPU-test-1"])
        self.assertEqual(
            (row["policy"]["sharing_mode"], row["policy"]["external_reserve_mib"],
             row["policy"]["residency"], row["policy"]["idle_seconds"]),
            ("exclusive", 4096, "idle", 180),
        )


if __name__ == "__main__":
    unittest.main()
