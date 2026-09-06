from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, urlopen
from unittest.mock import patch

from mediacenter.model_assets import (
    MAX_CHUNK_BYTES,
    ModelAssetError,
    ModelAssetManager,
    normalize_relative_path,
    validate_https_url,
)
from mediacenter.repository import Repository
from mediacenter.service_center import ServiceCenter, ServiceCenterError
from mediacenter.server import Handler, MediaCenterHTTPServer


def safetensors_bytes() -> bytes:
    header = json.dumps({"weight": {"dtype": "F32", "shape": [1],
                                    "data_offsets": [0, 4]}}, separators=(",", ":")).encode()
    return struct.pack("<Q", len(header)) + header + b"\x00\x00\x00\x00"


class ModelAssetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = Repository(self.root / "state.db")
        self.manager = ModelAssetManager(self.repository, self.root / "model-store")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create_transfer(self, data: bytes | None = None) -> tuple[dict, bytes]:
        data = data or safetensors_bytes()
        transfer = self.manager.create_upload({
            "display_name": "Test Image Model",
            "media_kind": "image",
            "role": "checkpoint",
            "format": "safetensors",
            "revision": "test-v1",
            "license_declared": "test-only",
            "files": [{"relative_path": "model.safetensors", "byte_size": len(data),
                       "sha256": hashlib.sha256(data).hexdigest()}],
        })
        return transfer, data

    def upload_and_complete(self) -> tuple[dict, dict]:
        transfer, data = self.create_transfer()
        file_id = transfer["files"][0]["id"]
        uploaded = self.manager.append_upload_chunk(transfer["id"], file_id, 0, data)
        self.assertEqual(uploaded["received_bytes"], len(data))
        completed = self.manager.complete_upload(transfer["id"])
        return completed, self.manager.get_asset(completed["asset_id"])

    def test_upload_verifies_and_publishes_immutable_asset(self) -> None:
        completed, asset = self.upload_and_complete()
        self.assertEqual(completed["state"], "succeeded")
        self.assertEqual(asset["state"], "ready")
        self.assertEqual(asset["file_count"], 1)
        self.assertEqual(asset["files"][0]["relative_path"], "model.safetensors")
        self.assertNotIn("storage_relpath", asset)
        self.assertNotIn("quarantine_relpath", completed)
        published = self.root / "model-store" / "assets" / asset["id"] / "manifest.json"
        self.assertTrue(published.is_file())

    def test_upload_requires_exact_resume_offset_and_hash(self) -> None:
        transfer, data = self.create_transfer()
        file_id = transfer["files"][0]["id"]
        half = len(data) // 2
        self.manager.append_upload_chunk(transfer["id"], file_id, 0, data[:half])
        with self.assertRaisesRegex(ModelAssetError, "偏移量"):
            self.manager.append_upload_chunk(transfer["id"], file_id, 0, data[half:])
        self.manager.append_upload_chunk(transfer["id"], file_id, half, data[half:-1] + b"x")
        with self.assertRaisesRegex(ModelAssetError, "SHA-256"):
            self.manager.complete_upload(transfer["id"])
        self.assertEqual(self.manager.get_transfer(transfer["id"])["state"], "failed")

    def test_readonly_asset_revision_and_manifest_binding_are_enforced(self):
        _,asset=self.upload_and_complete()
        path=self.manager.readonly_asset_path(asset['id'],asset['revision'])
        self.assertEqual(path,self.root/'model-store'/'assets'/asset['id'])
        with self.assertRaisesRegex(ModelAssetError,'版本'):
            self.manager.readonly_asset_path(asset['id'],'forged-revision')
        manifest=json.loads((path/'manifest.json').read_text())
        manifest['files'][0]['sha256']='0'*64
        (path/'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ModelAssetError,'清单'):
            self.manager.readonly_asset_path(asset['id'],asset['revision'])

    def test_duplicate_manifest_reuses_existing_asset(self) -> None:
        first, asset = self.upload_and_complete()
        second_transfer, data = self.create_transfer()
        self.manager.append_upload_chunk(second_transfer["id"], second_transfer["files"][0]["id"], 0, data)
        second = self.manager.complete_upload(second_transfer["id"])
        self.assertEqual(first["asset_id"], second["asset_id"])
        self.assertEqual(len(self.manager.list_assets()), 1)
        self.assertEqual(second["asset_id"], asset["id"])

    def test_archive_is_recoverable_and_does_not_remove_files(self) -> None:
        _, asset = self.upload_and_complete()
        archived = self.manager.archive(asset["id"])
        self.assertEqual(archived["state"], "archived")
        restored = self.manager.restore(asset["id"])
        self.assertEqual(restored["state"], "ready")
        self.assertTrue((self.root / "model-store" / "assets" / asset["id"]).is_dir())

    def test_pause_resume_cancel_state_machine_is_monotonic(self) -> None:
        transfer, _ = self.create_transfer()
        self.assertEqual(self.manager.pause(transfer["id"])["state"], "paused")
        self.assertEqual(self.manager.resume(transfer["id"])["state"], "queued")
        self.assertEqual(self.manager.cancel(transfer["id"])["state"], "canceled")
        with self.assertRaisesRegex(ModelAssetError, "状态"):
            self.manager.resume(transfer["id"])

    def test_late_chunk_is_rejected_before_creating_or_changing_a_file(self):
        for terminal in ('paused', 'canceled', 'succeeded'):
            with self.subTest(state=terminal):
                transfer, data = self.create_transfer()
                self.repository.set_model_transfer_state(transfer['id'], {'queued'}, terminal, 'fixture')
                target = self.root/'model-store'/'quarantine'/transfer['id']/'model.safetensors'
                with self.assertRaises(ModelAssetError) as raised:
                    self.manager.append_upload_chunk(transfer['id'], transfer['files'][0]['id'], 0, data)
                self.assertEqual(raised.exception.code, 'invalid_transfer_state')
                self.assertFalse(target.exists())
        complete, asset = self.upload_and_complete()
        target = self.root/'model-store'/'quarantine'/complete['id']/'model.safetensors'
        before = target.read_bytes()
        with self.assertRaises(ModelAssetError):
            self.manager.append_upload_chunk(complete['id'], complete['files'][0]['id'], len(before), b'x')
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(self.manager.get_asset(asset['id'])['state'], 'ready')

    def test_oversized_chunk_does_not_create_a_partial_file(self):
        transfer, data = self.create_transfer()
        with self.assertRaises(ModelAssetError) as raised:
            self.manager.append_upload_chunk(transfer['id'], transfer['files'][0]['id'], 0, data+b'x')
        self.assertEqual(raised.exception.code, 'upload_size_exceeded')
        self.assertFalse((self.root/'model-store'/'quarantine'/transfer['id']/'model.safetensors').exists())
        self.assertEqual(self.manager.get_transfer(transfer['id'])['received_bytes'], 0)

    def test_scoped_maintenance_open_does_not_recover_unrelated_transfers(self):
        transfer, data = self.create_transfer()
        self.manager.append_upload_chunk(transfer['id'], transfer['files'][0]['id'], 0, data[:8])
        ModelAssetManager(self.repository, self.manager.storage_root, recover_interrupted=False)
        self.assertEqual(self.manager.get_transfer(transfer['id'])['state'], 'transferring')

    def test_real_process_exit_after_fsync_recovers_only_journaled_chunk(self):
        transfer, data = self.create_transfer()
        half = len(data)//2
        self.manager.append_upload_chunk(transfer['id'], transfer['files'][0]['id'], 0, data[:half])
        source = '''import os,sys
from mediacenter.repository import Repository
from mediacenter.model_assets import ModelAssetManager
from tests.test_model_assets import safetensors_bytes
repo=Repository(sys.argv[1])
manager=ModelAssetManager(repo,sys.argv[2])
item=manager.get_transfer(sys.argv[3])
if item['state']=='paused': manager.resume(item['id'])
repo.append_model_transfer_bytes=lambda *a,**k: os._exit(71)
data=safetensors_bytes(); half=len(data)//2
manager.append_upload_chunk(item['id'],item['files'][0]['id'],half,data[half:])
'''
        process = subprocess.run([sys.executable, '-B', '-c', source, str(self.repository.path),
                                  str(self.manager.storage_root), transfer['id']],
                                 cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=20)
        self.assertEqual(process.returncode, 71, process.stderr.decode())
        path = self.manager.storage_root/'quarantine'/transfer['id']/'model.safetensors'
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(self.repository.get_model_transfer(transfer['id'])['received_bytes'], half)
        self.assertIsNotNone(self.repository.get_model_transfer_write(transfer['id']))
        restarted = ModelAssetManager(self.repository, self.manager.storage_root)
        if restarted.get_transfer(transfer['id'])['state'] == 'paused':
            restarted.resume(transfer['id'])
        restarted.append_upload_chunk(transfer['id'], transfer['files'][0]['id'], half, data[half:])
        self.assertIsNone(self.repository.get_model_transfer_write(transfer['id']))
        completed = restarted.complete_upload(transfer['id'])
        self.assertEqual(completed['state'], 'succeeded')
        self.assertEqual(restarted.cleanup_completed_transfers(), 1)

    def test_journal_does_not_authorize_truncating_replaced_or_unknown_files(self):
        transfer, data = self.create_transfer()
        with patch.object(self.repository, 'append_model_transfer_bytes', side_effect=SystemExit('crash')):
            with self.assertRaises(SystemExit):
                self.manager.append_upload_chunk(transfer['id'], transfer['files'][0]['id'], 0, data)
        path = self.manager.storage_root/'quarantine'/transfer['id']/'model.safetensors'
        replacement = path.with_name('replacement.bin')
        replacement.write_bytes(data)
        replacement.replace(path)
        with self.assertRaises(ModelAssetError) as caught:
            self.manager.append_upload_chunk(transfer['id'], transfer['files'][0]['id'], 0, data)
        self.assertEqual(caught.exception.code, 'transfer_write_recovery_conflict')
        self.assertEqual(path.read_bytes(), data)
        other, data = self.create_transfer()
        unknown = self.manager.storage_root/'quarantine'/other['id']/'model.safetensors'
        unknown.write_bytes(data)
        with self.assertRaises(ModelAssetError):
            self.manager.append_upload_chunk(other['id'], other['files'][0]['id'], 0, data)
        self.assertEqual(unknown.read_bytes(), data)

    def test_cleanup_is_registered_with_publish_and_keeps_all_formal_bytes(self):
        completed, asset = self.upload_and_complete()
        row = self.repository.get_transfer_cleanup(completed['id'])
        self.assertEqual((row['state'], row['asset_id'], row['revision']),
                         ('pending', asset['id'], asset['revision']))
        quarantine = self.root/'model-store'/'quarantine'/completed['id']/'model.safetensors'
        source = quarantine.read_bytes()
        formal = self.manager.readonly_asset_path(asset['id'], asset['revision'])/'model.safetensors'
        blob = self.root/'model-store'/self.repository.get_model_asset(asset['id'])['files'][0]['storage_relpath']
        before = [(path.read_bytes(), path.stat().st_ino) for path in (formal, blob)]
        self.assertEqual(self.manager.cleanup_completed_transfers(), 1)
        self.assertFalse(quarantine.exists())
        self.assertTrue(quarantine.parent.is_dir())  # No directory cleanup.
        self.assertEqual([(path.read_bytes(), path.stat().st_ino) for path in (formal, blob)], before)
        self.assertEqual(self.repository.get_transfer_cleanup(completed['id'])['state'], 'done')
        self.assertEqual(self.manager.cleanup_completed_transfers(), 0)
        with self.assertRaises(ModelAssetError):
            self.manager.append_upload_chunk(completed['id'], completed['files'][0]['id'], len(source), b'x')
        self.assertFalse(quarantine.exists())

    def test_full_duplicate_upload_cleans_both_copies_but_only_one_asset(self):
        first, asset = self.upload_and_complete()
        second, reused = self.upload_and_complete()
        self.assertEqual(asset['id'], reused['id'])
        self.assertEqual(self.manager.cleanup_completed_transfers(), 2)
        for transfer in (first, second):
            self.assertFalse((self.root/'model-store'/'quarantine'/transfer['id']/'model.safetensors').exists())
        self.assertEqual(len(self.manager.list_assets()), 1)

    def test_cleanup_missing_record_never_grants_historical_or_canceled_ownership(self):
        completed, _asset = self.upload_and_complete()
        with self.repository._connect() as db:
            db.execute('DELETE FROM model_transfer_cleanup WHERE transfer_id=?', (completed['id'],))
        other, data = self.create_transfer()
        self.manager.append_upload_chunk(other['id'], other['files'][0]['id'], 0, data)
        self.manager.cancel(other['id'])
        unknown = self.root/'model-store'/'quarantine'/'unowned.bin'
        unknown.write_bytes(b'unknown')
        self.assertEqual(self.manager.cleanup_completed_transfers(), 0)
        for transfer in (completed, other):
            self.assertTrue((self.root/'model-store'/'quarantine'/transfer['id']/'model.safetensors').is_file())
        self.assertEqual(unknown.read_bytes(), b'unknown')
        snapshot = self.manager.refresh_storage_inventory()
        self.assertTrue(snapshot['inventory_complete'])
        self.assertEqual(snapshot['quarantine_bytes'], 2*len(data)+7)

    def test_cleanup_registration_rolls_back_with_failed_publication_transaction(self):
        transfer, data = self.create_transfer()
        self.manager.append_upload_chunk(transfer['id'], transfer['files'][0]['id'], 0, data)
        register = self.repository._register_transfer_cleanup
        def fail_after_insert(*args):
            register(*args)
            raise RuntimeError('fixture-before-commit')
        with patch.object(self.repository, '_register_transfer_cleanup', side_effect=fail_after_insert):
            with self.assertRaisesRegex(RuntimeError, 'fixture-before-commit'):
                self.manager.complete_upload(transfer['id'])
        self.assertEqual(self.repository.get_model_transfer(transfer['id'])['state'], 'verifying')
        self.assertIsNone(self.repository.get_transfer_cleanup(transfer['id']))
        self.assertEqual(self.manager.list_assets(), [])
        self.assertEqual(self.manager.cleanup_completed_transfers(), 0)
        self.assertTrue((self.root/'model-store'/'quarantine'/transfer['id']/'model.safetensors').is_file())

    def test_cleanup_resumes_after_unlink_before_ledger_commit(self):
        completed, asset = self.upload_and_complete()
        with patch.object(self.repository, 'finish_transfer_cleanup', side_effect=SystemExit('fixture-crash')):
            with self.assertRaises(SystemExit):
                self.manager.cleanup_completed_transfers()
        self.assertEqual(self.repository.get_transfer_cleanup(completed['id'])['state'], 'pending')
        restarted = ModelAssetManager(self.repository, self.root/'model-store')
        self.assertEqual(restarted.cleanup_completed_transfers(), 1)
        self.assertTrue(restarted.readonly_asset_path(asset['id'], asset['revision']).is_dir())

    def test_cleanup_keeps_source_if_formal_hash_is_corrupt(self):
        completed, asset = self.upload_and_complete()
        formal = self.manager.readonly_asset_path(asset['id'], asset['revision'])/'model.safetensors'
        data = formal.read_bytes()
        formal.chmod(0o600)
        formal.write_bytes(data[:-1]+b'x')
        self.assertEqual(self.manager.cleanup_completed_transfers(), 0)
        row = self.repository.get_transfer_cleanup(completed['id'])
        self.assertEqual((row['state'], row['error_code']), ('blocked', 'cleanup_formal_copy_invalid'))
        self.assertEqual((self.root/'model-store'/'quarantine'/completed['id']/'model.safetensors').read_bytes(), data)
        with patch.object(self.repository, 'finish_transfer_cleanup') as update:
            self.manager.cleanup_completed_transfers()
        update.assert_not_called()  # No repeated failure writes or rehash loop.

    def test_transient_cleanup_failure_retries_without_persistent_failure_writes(self):
        completed, _asset = self.upload_and_complete()
        with patch.object(self.manager, '_unlink_owned_copy', side_effect=PermissionError('fixture-busy')):
            self.assertEqual(self.manager.cleanup_completed_transfers(), 0)
        self.assertEqual(self.repository.get_transfer_cleanup(completed['id'])['state'], 'pending')
        with patch.object(self.manager, '_cleanup_transfer') as check:
            self.assertEqual(self.manager.cleanup_completed_transfers(), 0)
        check.assert_not_called()
        self.manager._cleanup_retries[completed['id']] = 0
        self.assertEqual(self.manager.cleanup_completed_transfers(), 1)

    def test_busy_first_page_cannot_starve_later_cleanup(self):
        completed = [self.upload_and_complete()[0] for _ in range(9)]
        ordered = sorted(completed, key=lambda item: item['id'])
        with ExitStack() as stack:
            for item in ordered[:8]:
                stack.enter_context(self.manager._transfer_guard(item['id']))
            self.assertEqual(self.manager.cleanup_completed_transfers(), 0)
            self.assertEqual(self.manager.cleanup_completed_transfers(), 1)
            self.assertEqual(self.repository.get_transfer_cleanup(ordered[-1]['id'])['state'], 'done')
        self.assertEqual(self.manager.cleanup_completed_transfers(), 8)

    def test_cleanup_keeps_replacement_even_when_bytes_match(self):
        completed, _asset = self.upload_and_complete()
        path = self.root/'model-store'/'quarantine'/completed['id']/'model.safetensors'
        replacement = path.with_name('replacement.bin')
        replacement.write_bytes(path.read_bytes())
        os.replace(replacement, path)
        self.assertEqual(self.manager.cleanup_completed_transfers(), 0)
        self.assertTrue(path.exists())
        self.assertEqual(self.repository.get_transfer_cleanup(completed['id'])['error_code'], 'cleanup_file_changed')

    def test_cleanup_rejects_symlink_and_manifest_changes(self):
        completed, asset = self.upload_and_complete()
        manifest_path = self.manager.readonly_asset_path(asset['id'], asset['revision'])/'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['manifest_digest'] = '0'*64
        manifest_path.write_text(json.dumps(manifest))
        self.assertEqual(self.manager.cleanup_completed_transfers(), 0)
        path = self.root/'model-store'/'quarantine'/completed['id']/'model.safetensors'
        self.assertTrue(path.is_file())
        self.assertEqual(self.repository.get_transfer_cleanup(completed['id'])['error_code'], 'cleanup_manifest_invalid')

    @unittest.skipUnless(os.name == 'posix', 'POSIX symlink boundary; Windows rejects reparse points')
    def test_cleanup_does_not_follow_quarantine_parent_symlink(self):
        completed, _asset = self.upload_and_complete()
        parent = self.root/'model-store'/'quarantine'/completed['id']
        moved = self.root/'kept-source'
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
        self.assertEqual(self.manager.cleanup_completed_transfers(), 0)
        self.assertTrue((moved/'model.safetensors').is_file())
        self.assertEqual(self.repository.get_transfer_cleanup(completed['id'])['state'], 'blocked')

    def test_cleanup_resumes_partway_through_multi_file_transfer(self):
        data = {'model_index.json': b'{"_class_name":"StableDiffusionXLPipeline"}',
                'unet/model.safetensors': safetensors_bytes()}
        transfer = self.manager.create_upload({
            'display_name': 'partial cleanup', 'media_kind': 'image', 'role': 'checkpoint',
            'format': 'diffusers', 'revision': 'fixture', 'license_declared': 'fixture',
            'files': [{'relative_path': name, 'byte_size': len(value), 'sha256': hashlib.sha256(value).hexdigest()}
                      for name, value in data.items()],
        })
        for file in transfer['files']:
            self.manager.append_upload_chunk(transfer['id'], file['id'], 0, data[file['relative_path']])
        completed = self.manager.complete_upload(transfer['id'])
        unlink = self.manager._unlink_owned_copy
        def crash_after_first(*args):
            unlink(*args)
            raise SystemExit('fixture-partial-crash')
        with patch.object(self.manager, '_unlink_owned_copy', side_effect=crash_after_first):
            with self.assertRaises(SystemExit):
                self.manager.cleanup_completed_transfers()
        parent = self.root/'model-store'/'quarantine'/transfer['id']
        self.assertFalse((parent/'model_index.json').exists())
        self.assertTrue((parent/'unet/model.safetensors').is_file())
        restarted = ModelAssetManager(self.repository, self.root/'model-store')
        self.assertEqual(restarted.cleanup_completed_transfers(), 1)
        for name, value in data.items():
            self.assertFalse((parent/name).exists())
            self.assertEqual((self.root/'model-store'/'assets'/completed['asset_id']/name).read_bytes(), value)

    def test_storage_partial_scan_is_unknown_and_never_a_cleanup_authority(self):
        self.upload_and_complete()
        with patch('os.scandir', side_effect=OSError('fixture unavailable')):
            summary = self.manager.refresh_storage_inventory()
        self.assertFalse(summary['inventory_complete'])
        self.assertIsNone(summary['quarantine_bytes'])
        self.assertEqual(self.manager.storage_summary()['cleanup_counts'], {'pending': 1})

    def test_background_hash_can_stop_and_storage_http_never_scans_files(self):
        completed, _asset = self.upload_and_complete()
        self.assertIsNone(self.manager.storage_summary()['quarantine_bytes'])
        self.assertFalse(self.manager.storage_summary()['inventory_complete'])
        started = threading.Event()
        original = self.manager._verified_file
        def slow_hash(*args, **kwargs):
            started.set()
            self.manager._maintenance_stop.wait(5)
            return original(*args, **kwargs)
        with patch.object(self.manager, '_verified_file', side_effect=slow_hash):
            self.manager.start_maintenance()
            try:
                self.assertTrue(started.wait(5))
                with patch('os.scandir', side_effect=AssertionError('HTTP must not scan')):
                    begin = time.monotonic()
                    self.assertTrue(self.manager.storage_summary()['inventory_complete'])
                    self.assertLess(time.monotonic()-begin, 1)
            finally:
                self.assertTrue(self.manager.stop_maintenance())
        self.assertEqual(self.repository.get_transfer_cleanup(completed['id'])['state'], 'pending')
        restarted = ModelAssetManager(self.repository, self.root/'model-store')
        self.assertEqual(restarted.cleanup_completed_transfers(), 1)

    def test_stop_closes_lifecycle_and_failed_start_leaves_no_invalid_thread(self):
        with patch('threading.Thread.start', side_effect=RuntimeError('fixture-start-failure')):
            with self.assertRaisesRegex(RuntimeError, 'fixture-start-failure'):
                self.manager.start_maintenance()
        self.assertIsNone(self.manager._maintenance_thread)
        entered, release = threading.Event(), threading.Event()
        def loop():
            entered.set()
            self.manager._maintenance_stop.wait(5)
            release.wait(5)
        with patch.object(self.manager, '_maintenance_loop', side_effect=loop):
            self.manager.start_maintenance()
            self.assertTrue(entered.wait(5))
            result = []
            stopper = threading.Thread(target=lambda: result.append(self.manager.stop_maintenance()))
            stopper.start()
            self.assertTrue(self.manager._maintenance_stop.wait(5))
            errors = []
            def start_again():
                try: self.manager.start_maintenance()
                except RuntimeError as error: errors.append(str(error))
            starter = threading.Thread(target=start_again)
            starter.start()
            release.set()
            stopper.join(5); starter.join(5)
        self.assertEqual(result, [True])
        self.assertEqual(errors, ['model store maintenance is closed'])
        self.assertFalse(self.manager._maintenance_thread.is_alive())

    @patch('mediacenter.model_assets.validate_https_url', return_value=('models.example', ['8.8.8.8']))
    def test_download_runner_token_is_not_replaced_by_bearer_credential(self, _validate):
        self.manager.allowed_download_hosts = ('models.example',)
        self.manager.configure_download_credential('models.example', 'fixture-read-token')
        data = safetensors_bytes()
        with patch.object(self.manager, '_start_download'):
            transfer = self.manager.create_https_download({
                'display_name': 'download fixture', 'media_kind': 'image', 'role': 'checkpoint',
                'format': 'safetensors', 'revision': 'fixture', 'license_declared': 'fixture',
                'url': 'https://models.example/model.safetensors', 'filename': 'model.safetensors',
                'expected_bytes': len(data), 'expected_sha256': hashlib.sha256(data).hexdigest(),
            })
        runner = self.repository.claim_transfer_download(transfer['id'])
        class Response(io.BytesIO):
            status = 200
            def getcode(self): return self.status
        requests = []
        def request(value, **_kwargs):
            requests.append(value)
            return Response(data)
        with patch('mediacenter.model_assets.build_opener', return_value=SimpleNamespace(open=request)):
            self.manager._download_worker(transfer['id'], runner)
        self.assertEqual(requests[0].get_header('Authorization'), 'Bearer fixture-read-token')
        self.assertEqual(self.manager.get_transfer(transfer['id'])['state'], 'succeeded')
        self.assertEqual(self.manager.cleanup_completed_transfers(), 1)

    def test_transfer_guard_is_cross_manager_and_does_not_wait_in_http_control(self):
        transfer, data = self.create_transfer()
        second = ModelAssetManager(self.repository, self.root/'model-store')
        with self.manager._transfer_guard(transfer['id']):
            started = time.monotonic()
            with self.assertRaises(ModelAssetError) as raised:
                second.append_upload_chunk(transfer['id'], transfer['files'][0]['id'], 0, data)
            self.assertEqual(raised.exception.code, 'model_transfer_busy')
            self.assertLess(time.monotonic()-started, 1)
        second.append_upload_chunk(transfer['id'], transfer['files'][0]['id'], 0, data)
        self.assertEqual(second.get_transfer(transfer['id'])['received_bytes'], len(data))

    def test_transfer_guard_fences_a_real_second_process(self):
        transfer, _ = self.create_transfer()
        source = '''import sys
from mediacenter.repository import Repository
from mediacenter.model_assets import ModelAssetManager, ModelAssetError
manager=ModelAssetManager(Repository(sys.argv[1]),sys.argv[2])
try:
    manager.pause(sys.argv[3])
except ModelAssetError as error:
    print(error.code)
else:
    raise RuntimeError('second process obtained a held transfer')
'''
        with self.manager._transfer_guard(transfer['id']):
            child = subprocess.run([sys.executable, '-B', '-c', source, str(self.repository.path),
                                    str(self.root/'model-store'), transfer['id']],
                                   capture_output=True, text=True, timeout=15)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), 'model_transfer_busy')
        self.assertEqual(self.manager.get_transfer(transfer['id'])['state'], 'queued')

    def test_startup_recovery_does_not_pause_a_live_locked_publication(self):
        transfer, _ = self.create_transfer()
        self.repository.set_model_transfer_state(transfer['id'], {'queued'}, 'verifying', 'fixture')
        with self.manager._transfer_guard(transfer['id']):
            ModelAssetManager(self.repository, self.root/'model-store')
            self.assertEqual(self.manager.get_transfer(transfer['id'])['state'], 'verifying')

    def test_installation_controls_use_the_same_transfer_guard(self):
        transfer, _ = self.create_transfer()
        with self.manager._transfer_guard(transfer['id']), \
                patch.object(self.repository, 'get_service_installation', return_value={'transfer_id': transfer['id']}), \
                patch.object(self.repository, 'control_service_installation') as change:
            with self.assertRaises(ModelAssetError) as raised:
                self.manager.control_installation_transfer(('op', 'attempt', None), 'pause')
            self.assertEqual(raised.exception.code, 'model_transfer_busy')
            change.assert_not_called()

    @patch('mediacenter.model_assets.validate_https_url', return_value=('models.example', ['8.8.8.8']))
    def test_download_network_read_does_not_hold_pause_lock(self, _validate):
        with patch.object(self.manager, '_start_download'):
            transfer = self.manager.create_https_download({
                'display_name': 'pause fixture', 'media_kind': 'general', 'role': 'checkpoint',
                'format': 'diffusers', 'revision': 'fixture', 'license_declared': 'fixture',
                'url': 'https://models.example/model.json', 'filename': 'model.json',
                'expected_bytes': 2, 'expected_sha256': hashlib.sha256(b'{}').hexdigest(),
            })
        started, release = threading.Event(), threading.Event()
        result = []
        class Response:
            status = 200
            def getcode(self): return self.status
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def read(self, _size):
                started.set()
                if not release.wait(5): raise RuntimeError('fixture read release missing')
                return b'{}'
        stored = self.repository.get_model_transfer(transfer['id'])
        def download():
            try:
                result.append(self.manager._download_file(transfer['id'], stored, stored['files'][0]))
            except BaseException as error:
                result.append(error)
        with patch('mediacenter.model_assets.build_opener', return_value=SimpleNamespace(open=lambda *_a, **_k: Response())):
            thread = threading.Thread(target=download)
            thread.start()
            try:
                self.assertTrue(started.wait(5))
                self.assertEqual(self.manager.pause(transfer['id'])['state'], 'paused')
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [False])
        self.assertEqual(self.manager.get_transfer(transfer['id'])['received_bytes'], 0)
        self.assertFalse((self.root/'model-store'/'quarantine'/transfer['id']/'model.json').exists())

    @patch("mediacenter.model_assets.validate_https_url", return_value=("models.example", ["8.8.8.8"]))
    def test_catalog_download_persists_fixed_multifile_manifest_without_exposing_urls(self, _validate) -> None:
        payloads = {"model_index.json": b"{}", "unet/model.safetensors": safetensors_bytes()}
        with patch.object(self.manager, "_start_download"):
            transfer = self.manager.create_catalog_download({
                "display_name": "Catalog SDXL", "media_kind": "image", "role": "checkpoint",
                "format": "diffusers", "source_type": "huggingface",
                "source_ref": "official/model", "revision": "a" * 40,
                "license_declared": "test", "files": [
                    {"relative_path": path, "byte_size": len(data),
                     "sha256": hashlib.sha256(data).hexdigest(),
                     "url": f"https://models.example/{path}"}
                    for path, data in payloads.items()
                ],
            })
        self.assertEqual((transfer["direction"], transfer["expected_bytes"], len(transfer["files"])),
                         ("download", sum(map(len, payloads.values())), 2))
        self.assertTrue(all("source_url" not in item for item in transfer["files"]))
        stored = self.repository.get_model_transfer(transfer["id"])
        self.assertTrue(all(item["source_url"].startswith("https://models.example/")
                            for item in stored["files"]))

    @patch("mediacenter.model_assets.validate_https_url", return_value=("github.com", ["8.8.8.8"]))
    def test_fixed_catalog_recipe_may_download_trusted_bundle(self, _validate) -> None:
        with patch.object(self.manager, "_start_download"):
            transfer = self.manager.create_catalog_download({
                "display_name": "Real-ESRGAN", "media_kind": "image", "role": "upscaler",
                "format": "trusted-bundle", "source_type": "github-release",
                "source_ref": "official/realesrgan", "revision": "a" * 40,
                "license_declared": "BSD-3-Clause", "files": [{
                    "relative_path": "RealESRGAN_x2plus.pth", "byte_size": 42,
                    "sha256": "a" * 64,
                    "url": "https://github.com/official/release/RealESRGAN_x2plus.pth",
                }],
            })
        self.assertEqual((transfer["format"], transfer["source_type"]),
                         ("trusted-bundle", "github-release"))

    @patch("mediacenter.model_assets.validate_https_url", return_value=("huggingface.co", ["8.8.8.8"]))
    def test_musicgen_recipe_queues_exact_eight_files_without_network(self, _validate) -> None:
        catalog = Path(__file__).resolve().parents[1] / "deploy/model_catalog.json"
        entry = next(item for item in json.loads(catalog.read_text(encoding="utf-8"))["models"]
                     if item["catalog_key"] == "musicgen-small")
        recipe = entry["service_recipe"]
        with patch.object(self.manager, "_start_download") as download:
            transfer = self.manager.create_catalog_download({
                "display_name": entry["label"], "media_kind": entry["kind"],
                "role": recipe["role"], "format": recipe["format"],
                "source_type": recipe["source_type"], "source_ref": entry["model_id"],
                "revision": entry["recommended_revision"], "license_declared": entry["license"],
                "files": recipe["files"],
            })
        download.assert_called_once_with(transfer["id"])
        stored = self.repository.get_model_transfer(transfer["id"])
        self.assertEqual((stored["media_kind"], stored["format"], stored["expected_bytes"]),
                         ("music", "transformers", 2367653971))
        self.assertEqual({f["relative_path"]: (f["expected_bytes"], f["expected_sha256"], f["source_url"])
                          for f in stored["files"]},
                         {f["relative_path"]: (f["byte_size"], f["sha256"], f["url"])
                          for f in recipe["files"]})
        self.assertEqual(self.manager.list_assets(), [])

    def test_paths_reject_traversal_absolute_backslash_and_case_collision(self) -> None:
        for value in ("../model.safetensors", "/model.safetensors", "a\\b.safetensors",
                      "C:/model.safetensors", "a/./b.safetensors"):
            with self.subTest(value=value), self.assertRaises(ModelAssetError):
                normalize_relative_path(value)
        with self.assertRaisesRegex(ModelAssetError, "大小写冲突"):
            self.manager.create_upload({
                "display_name": "Collision", "media_kind": "image", "role": "checkpoint",
                "format": "safetensors", "revision": "v1", "license_declared": "unknown",
                "files": [{"relative_path": "A.safetensors", "byte_size": 1},
                          {"relative_path": "a.safetensors", "byte_size": 1}],
            })

    def test_trusted_bundle_cannot_be_created_by_upload(self) -> None:
        with self.assertRaisesRegex(ModelAssetError, "不能声明"):
            self.manager.create_upload({
                "display_name": "Unsafe", "media_kind": "image", "role": "checkpoint",
                "format": "trusted-bundle", "revision": "v1", "license_declared": "unknown",
                "files": [{"relative_path": "model.pth", "byte_size": 1}],
            })

    def test_pickle_and_executable_extensions_are_rejected(self) -> None:
        for filename in ("model.pth", "model.ckpt", "model.bin", "remote.py"):
            with self.subTest(filename=filename), self.assertRaisesRegex(ModelAssetError, "Pickle"):
                self.manager.create_upload({
                    "display_name": "Unsafe", "media_kind": "image", "role": "checkpoint",
                    "format": "diffusers", "revision": "v1", "license_declared": "unknown",
                    "files": [{"relative_path": filename, "byte_size": 1}],
                })

    def test_https_download_requires_allowlisted_fixed_manifest(self) -> None:
        manager = ModelAssetManager(self.repository, self.root / "download-store", ["models.example"])
        public = [(2, 1, 6, "", ("8.8.8.8", 443))]
        with patch("socket.getaddrinfo", return_value=public), patch.object(
                manager, "_start_download") as start:
            transfer = manager.create_https_download({
                "display_name": "Pinned Model", "media_kind": "image", "role": "checkpoint",
                "format": "safetensors", "revision": "release-v1",
                "license_declared": "unknown", "url": "https://models.example/model.safetensors",
                "filename": "model.safetensors", "expected_bytes": 42,
                "expected_sha256": "a" * 64,
            })
        self.assertEqual((transfer["direction"], transfer["state"]), ("download", "queued"))
        self.assertNotIn("quarantine_relpath", transfer)
        start.assert_called_once_with(transfer["id"])

    def test_chunk_limit_is_enforced_before_disk_write(self) -> None:
        transfer, _ = self.create_transfer(b"x" * (MAX_CHUNK_BYTES + 1))
        with self.assertRaisesRegex(ModelAssetError, "8 MiB"):
            self.manager.append_upload_chunk(
                transfer["id"], transfer["files"][0]["id"], 0,
                b"x" * (MAX_CHUNK_BYTES + 1),
            )

    def test_https_source_requires_allowlist_and_public_dns(self) -> None:
        public = [(2, 1, 6, "", ("8.8.8.8", 443))]
        private = [(2, 1, 6, "", ("10.0.0.7", 443))]
        with patch("socket.getaddrinfo", return_value=public):
            host, addresses = validate_https_url("https://models.example/model.gguf",
                                                 ["models.example"])
            self.assertEqual((host, addresses), ("models.example", ["8.8.8.8"]))
            with self.assertRaisesRegex(ModelAssetError, "允许列表"):
                validate_https_url("https://other.example/model.gguf", ["models.example"])
        with patch("socket.getaddrinfo", return_value=private):
            with self.assertRaisesRegex(ModelAssetError, "非公网"):
                validate_https_url("https://models.example/model.gguf", ["models.example"])

    def test_runtime_source_credential_is_memory_only_exact_host_and_clearable(self) -> None:
        manager = ModelAssetManager(
            self.repository, self.root / "runtime-credential-store", ["huggingface.co"])
        self.assertFalse(manager.has_download_credential("huggingface.co"))
        manager.configure_download_credential("HUGGINGFACE.CO.", "hf_runtime_fixture")
        self.assertTrue(manager.has_download_credential("huggingface.co"))
        self.assertFalse(any("hf_runtime_fixture" in path.read_text(
            encoding="utf-8", errors="ignore") for path in self.root.rglob("*") if path.is_file()))
        manager.clear_download_credential("huggingface.co")
        self.assertFalse(manager.has_download_credential("huggingface.co"))

    def test_runtime_source_credential_rejects_unknown_host_and_invalid_token(self) -> None:
        manager = ModelAssetManager(
            self.repository, self.root / "runtime-credential-invalid", ["huggingface.co"])
        with self.assertRaisesRegex(ModelAssetError, "允许列表"):
            manager.configure_download_credential("cdn.example", "hf_fixture")
        for value in ("", "contains whitespace", "x" * 4097):
            with self.subTest(value=value[:20]), self.assertRaisesRegex(ModelAssetError, "格式"):
                manager.configure_download_credential("huggingface.co", value)

    def test_service_center_source_authorization_contract_never_returns_token(self) -> None:
        manager = ModelAssetManager(
            self.repository, self.root / "service-source-credential", ["huggingface.co"])
        center = ServiceCenter.__new__(ServiceCenter)
        center.model_assets = manager
        configured = center.configure_source_authorization(
            "huggingface", {"token": "hf_service_fixture"})
        self.assertEqual(configured, {
            "provider": "huggingface", "configured": True, "persistence": "memory"})
        self.assertNotIn("token", configured)
        cleared = center.configure_source_authorization("huggingface", {"action": "clear"})
        self.assertFalse(cleared["configured"])
        with self.assertRaises(ServiceCenterError) as unsupported:
            center.configure_source_authorization("unknown", {"token": "secret"})
        self.assertEqual(unsupported.exception.status, 404)

    def test_source_authorization_http_contract_returns_status_only(self) -> None:
        calls = []

        class Center:
            @staticmethod
            def stop_deployment_worker():
                return True

            @staticmethod
            def source_authorizations():
                return [{"provider": "huggingface", "configured": False,
                         "persistence": "memory"}]

            @staticmethod
            def configure_source_authorization(provider, payload):
                calls.append((provider, payload))
                return {"provider": provider, "configured": True,
                        "persistence": "memory"}

        server = MediaCenterHTTPServer(("127.0.0.1", 0), Handler)
        server.center = Center()
        server.api_key = "test-api-key"
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            request = Request(
                base + "/api/v1/source-authorizations/huggingface",
                data=json.dumps({"token": "hf_api_fixture"}).encode(),
                headers={"Content-Type": "application/json", "X-API-Key": "test-api-key"},
            )
            with urlopen(request, timeout=3) as response:
                configured = json.loads(response.read())
            self.assertEqual(configured, {"provider": "huggingface", "configured": True,
                                          "persistence": "memory"})
            self.assertNotIn("token", configured)
            self.assertEqual(calls, [("huggingface", {"token": "hf_api_fixture"})])

            with urlopen(Request(base + "/api/v1/source-authorizations",
                         headers={"X-API-Key": "test-api-key"}), timeout=3) as response:
                status = json.loads(response.read())
            self.assertEqual(status["items"][0]["configured"], False)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_source_authorization_transport_rejects_non_loopback_plain_http(self) -> None:
        remote = SimpleNamespace(connection=object(), client_address=("10.0.0.8", 42100), headers={})
        local = SimpleNamespace(connection=object(), client_address=("127.0.0.2", 42100), headers={})
        trusted_lan = SimpleNamespace(
            connection=object(), client_address=("10.0.0.8", 42100),
            headers={"X-MediaCenter-Insecure-Transport-Accepted": "1"})
        self.assertFalse(Handler._source_authorization_transport_allowed(remote))
        self.assertTrue(Handler._source_authorization_transport_allowed(local))
        self.assertTrue(Handler._source_authorization_transport_allowed(trusted_lan))

    @patch("mediacenter.model_assets.validate_https_url")
    def test_bearer_token_is_sent_only_to_exact_source_host_not_redirect(self, validate) -> None:
        validate.side_effect = lambda url, _allowed: (
            "huggingface.co" if url.startswith("https://huggingface.co/")
            else "cdn-lfs.huggingface.co", ["8.8.8.8"])
        manager = ModelAssetManager(
            self.repository, self.root / "gated-store",
            ["huggingface.co", "cdn-lfs.huggingface.co"],
            download_bearer_tokens={"huggingface.co": "hf_private_fixture"},
        )
        with patch.object(manager, "_start_download"):
            transfer = manager.create_catalog_download({
                "display_name": "Gated Model", "media_kind": "image", "role": "checkpoint",
                "format": "diffusers", "source_type": "huggingface",
                "source_ref": "official/gated", "revision": "a" * 40,
                "license_declared": "fixture", "files": [{
                    "relative_path": "model_index.json", "byte_size": 2,
                    "sha256": hashlib.sha256(b"{}").hexdigest(),
                    "url": "https://huggingface.co/official/gated/resolve/revision/model_index.json",
                }],
            })
        stored = self.repository.get_model_transfer(transfer["id"])
        requests = []

        class Response:
            status = 200
            def __init__(self): self.remaining = b"{}"
            def getcode(self): return self.status
            def read(self, _size): value, self.remaining = self.remaining, b""; return value
            def close(self): pass
            def __enter__(self): return self
            def __exit__(self, *_args): self.close()

        def open_request(request, timeout):
            requests.append(request)
            if len(requests) == 1:
                raise HTTPError(request.full_url, 302, "redirect", {
                    "Location": "https://cdn-lfs.huggingface.co/blob/model_index.json",
                }, None)
            return Response()

        with patch("mediacenter.model_assets.build_opener",
                   return_value=SimpleNamespace(open=open_request)):
            self.assertTrue(manager._download_file(transfer["id"], stored, stored["files"][0]))
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer hf_private_fixture")
        self.assertIsNone(requests[1].get_header("Authorization"))
        self.assertTrue(manager.has_download_credential("HUGGINGFACE.CO."))

    @patch("mediacenter.model_assets.validate_https_url",
           return_value=("models.example", ["8.8.8.8"]))
    def test_explicit_download_proxy_is_validated_and_used_without_ambient_fallback(self, _validate) -> None:
        proxy = "http://127.0.0.1:1081"
        manager = ModelAssetManager(
            self.repository, self.root / "proxy-store", ["models.example"],
            download_proxy=proxy,
        )
        with self.assertRaisesRegex(ValueError, "download proxy is invalid"):
            ModelAssetManager(
                self.repository, self.root / "bad-proxy-store", ["models.example"],
                download_proxy="socks5://127.0.0.1:1081",
            )
        data = b"{}"
        with patch.object(manager, "_start_download"):
            transfer = manager.create_https_download({
                "display_name": "Proxy fixture", "media_kind": "general",
                "role": "checkpoint", "format": "diffusers", "revision": "fixture-v1",
                "license_declared": "fixture", "url": "https://models.example/model.json",
                "filename": "model.json", "expected_bytes": len(data),
                "expected_sha256": hashlib.sha256(data).hexdigest(),
            })
        stored = self.repository.get_model_transfer(transfer["id"])

        class Response:
            status = 200
            def __init__(self): self.remaining = data
            def getcode(self): return self.status
            def read(self, _size): value, self.remaining = self.remaining, b""; return value
            def close(self): pass
            def __enter__(self): return self
            def __exit__(self, *_args): self.close()

        with patch("mediacenter.model_assets.build_opener",
                   return_value=SimpleNamespace(open=lambda *_args, **_kwargs: Response())) as opener:
            self.assertTrue(manager._download_file(transfer["id"], stored, stored["files"][0]))
        handler = next(item for item in opener.call_args.args if isinstance(item, ProxyHandler))
        self.assertEqual(handler.proxies, {"http": proxy, "https": proxy})


if __name__ == "__main__":
    unittest.main()
