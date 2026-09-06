"""Offline release-preparation contracts; fixtures do not prove an executable OCI."""
from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path

from mediacenter.container_releases import RuntimeRelease
from scripts.prepare_sdxl_single_file_runtime import prepare, prepare_builtin
from tests.test_runtime_artifacts import image_fixture


class RuntimePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifact = self.root / 'candidate.oci.tar'
        declaration, raw = image_fixture()
        self.artifact.write_bytes(raw)
        self.evidence = {
            'schema': 'mc.derived-oci-build/1', 'status': 'passed', 'mode': 'sdk-only',
            'python_prefix': '/opt/python', 'manifest_digest': 'sha256:' + '2' * 64,
            'config_digest': 'sha256:' + '3' * 64, 'archive_sha256': hashlib.sha256(raw).hexdigest(),
            'archive_bytes': len(raw), 'sdk_digest': '4' * 64,
            'entrypoint': ['/opt/python/bin/python', '-B', '-u', '-m', 'mediacenter.image_worker_cli'],
            'command': [], 'environment': ['PIP_NO_INDEX=1'],
        }
        declaration.update(adapter_id='sdxl', release_id='previous-sdxl')
        self.parent = RuntimeRelease(declaration)
        self.runtime = {'server_id': 'test', 'gpu_uuids': [], 'engine': {}, 'installation': {
            'releases': [declaration], 'approved_release_digests': [self.parent.digest],
            'templates': {'sdxl-base-1.0': {'schema': 1, 'recipe_key': 'sdxl-base-1.0',
                'recipe_digest': '5'*64, 'release_digest': self.parent.digest,
                'limits': dict(uid=1000, gid=1000, memory_bytes=1024, nano_cpus=1, pids_limit=10, tmpfs_bytes=1024),
                'resources': dict(base_mib=1024, task_mib=1024, external_reserve_mib=512,
                                  sharing_mode='shared', residency='on_demand', idle_seconds=300)}},
            'image_store': 'unused', 'download_hosts': [], 'publisher': {}, 'package_root': 'unused',
            'lora': {'root': '/runtime/lora', 'approvals': [{'obsolete': True}]},
            'local_artifact_roots': [str(self.root)], 'local_artifacts': {}, 'runtime_profiles': []}}

    def test_new_profile_retires_static_approvals_only_in_candidate(self):
        original = copy.deepcopy(self.runtime)
        candidate, release = prepare(self.runtime, self.evidence, self.artifact,
            release_id='single-file-v2', artifact_url='https://fixtures.invalid/v2.tar', profile_revision=2)
        self.assertEqual(self.runtime, original)
        self.assertEqual(candidate['installation']['lora'], {'root': '/runtime/lora'})
        self.assertEqual(candidate['installation']['runtime_profiles'][0]['revision'], 2)
        self.assertEqual(candidate['installation']['templates'], original['installation']['templates'])
        self.assertEqual(release['adapter_id'], 'sdxl-single-file')

    def test_builtin_sdk_changes_only_runtime_binding_and_retains_rollback_release(self):
        evidence = copy.deepcopy(self.evidence)
        evidence.update(parent_config_digest=self.parent.data['image']['image_id'],
                        parent_manifest_digest=self.parent.image_digest)
        evidence['entrypoint'][-1] = 'mediacenter.worker_cli'
        original = copy.deepcopy(self.runtime)
        candidate, release = prepare_builtin(self.runtime, evidence, self.artifact,
            model_key='sdxl-base-1.0', release_id='sdxl-v2', artifact_url='https://fixtures.invalid/sdxl-v2.tar')
        self.assertEqual(self.runtime, original)
        self.assertEqual(candidate['installation']['releases'][0], self.parent.data)
        template = candidate['installation']['templates']['sdxl-base-1.0']
        self.assertEqual(template['release_digest'], RuntimeRelease(release).digest)
        template['release_digest'] = self.parent.digest
        self.assertEqual(template, original['installation']['templates']['sdxl-base-1.0'])
        for field, wrong in [('parent_config_digest', 'sha256:'+'9'*64), ('mode', 'dependencies-and-sdk')]:
            bad = dict(evidence, **{field: wrong})
            with self.assertRaisesRegex(ValueError, 'parent_or_entrypoint'):
                prepare_builtin(self.runtime, bad, self.artifact, model_key='sdxl-base-1.0',
                    release_id='sdxl-v2', artifact_url='https://fixtures.invalid/sdxl-v2.tar')
        self.assertEqual(self.runtime, original)

    def test_preparation_rejects_changed_artifact_and_unsupported_builtin(self):
        self.artifact.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'artifact_identity_changed'):
            prepare(self.runtime, self.evidence, self.artifact, release_id='v2',
                    artifact_url='https://fixtures.invalid/v2.tar', profile_revision=2)
        with self.assertRaisesRegex(ValueError, 'identity_invalid'):
            prepare_builtin(self.runtime, self.evidence, self.artifact, model_key='user/module',
                            release_id='v2', artifact_url='https://fixtures.invalid/v2.tar')


if __name__ == '__main__':
    unittest.main()
