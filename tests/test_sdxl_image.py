import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import build_sdxl_image as build
from tests.test_runtime_artifacts import image_fixture

ROOT=Path(__file__).resolve().parents[1]


class SDXLImageTests(unittest.TestCase):
    def export_fixture(self, directory, *, url=None, actual_change=None, template=False):
        root=Path(directory); declaration,raw=image_fixture(); archive=root/'sdxl.oci.tar';archive.write_bytes(raw)
        actual={'manifest':declaration['image']['image_id'],'config':next(
            'sha256:'+name.rsplit('/',1)[-1] for name in self._tar_names(archive)
            if name.startswith('blobs/sha256/') and name.rsplit('/',1)[-1] != declaration['image']['image_id'][7:]),
            'layers':[]}
        # Use the declaration's real manifest/config/layer descriptors, not names guessed by the test.
        import tarfile
        with tarfile.open(archive,'r:') as content:
            manifest=json.load(content.extractfile('blobs/sha256/'+declaration['image']['image_id'][7:]))
        actual['config']=manifest['config']['digest'];actual['layers']=manifest['layers']
        claimed=copy.deepcopy(actual)
        if actual_change:claimed.update(actual_change)
        release_sha=hashlib.sha256((ROOT/'containers/sdxl/release.json').read_bytes()).hexdigest()
        build_result={'build':'completed','release_sha256':release_sha,'parent_result_sha256':'b'*64,'actual':claimed}
        result_path=root/'build-result.json';result_path.write_text(json.dumps(build_result,separators=(',',':')))
        build_sha=hashlib.sha256(result_path.read_bytes()).hexdigest()
        template_path=None
        if template:
            template_path=root/'template.json';template_path.write_text(json.dumps({
                'limits':{'uid':1000,'gid':1000,'memory_bytes':24*1024**3,'nano_cpus':2_000_000_000,'pids_limit':256,'tmpfs_bytes':64*1024**2},
                'resources':{'base_mib':12288,'task_mib':4096,'external_reserve_mib':1024,'sharing_mode':'exclusive','residency':'resident','idle_seconds':0}}))
        with patch.object(build,'parent_contract',return_value={'manifest':'sha256:'+'c'*64,'config':'sha256:'+'d'*64,'layers':[]}), \
             patch.object(build,'inspect_output',return_value=actual):
            return build.export_release(ROOT,release_sha,result_path,build_sha,archive,'unused','b'*64,'unused',root/'export',
                'sdxl-runtime-1',url,template_path),declaration,actual

    @staticmethod
    def _tar_names(path):
        import tarfile
        with tarfile.open(path,'r:') as content:return [item.name for item in content.getmembers()]

    def test_fixed_delta_closure_is_54_without_build_claim(self):
        release,combined,system,report=build.contract(ROOT)
        self.assertEqual(report['declaration']['packages'],54)
        self.assertEqual(report['delta_bytes'],504896)
        self.assertFalse(report['artifact_verified']);self.assertEqual(report['build'],'not_executed')
        self.assertIsNone(release['image_digest']);self.assertIsNone(release['parent_manifest_digest'])
        self.assertEqual(len([p for p in combined['packages'] if p['name']=='peft']),1)

    def test_approval_or_missing_offline_artifact_fails_before_staging(self):
        with self.assertRaisesRegex(ValueError,'independent_approval_mismatch'):
            build.contract(ROOT,'0'*64)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises((ValueError,FileNotFoundError)):
                build.contract(ROOT,artifacts=Path(directory))
            self.assertEqual(list(Path(directory).iterdir()),[])

    def test_plan_is_only_local_oci_and_fixed_private_remote(self):
        argv=build.fixed_argv('/tools/buildx','mc-runtime-sdxl-one','unix:///private/buildkit.sock',
            '/private/context','/private/parent','/private/output.tar','cold','sha256:'+'a'*64,'/owned/build')
        self.assertIn('runtime_parent=oci-layout:///private/parent@sha256:'+'a'*64,argv)
        self.assertIn('--no-cache',argv);self.assertIn('--cgroup-parent',argv)
        self.assertNotIn('--load',argv);self.assertNotIn('--push',argv);self.assertNotIn('--bootstrap',argv)
        self.assertEqual(argv[-1],'/private/context')
        with self.assertRaises(ValueError):
            build.fixed_argv('/tools/buildx','mc-runtime-sdxl-one','tcp://remote:123','/context','/parent','/out','warm','sha256:'+'a'*64,'/')

    def test_dockerfile_runtime_user_and_no_online_dependency_resolution(self):
        value=(ROOT/'containers/sdxl/Dockerfile').read_text()
        self.assertEqual(value.count('FROM '),1)
        self.assertIn('FROM runtime_parent',value)
        self.assertIn('--no-index --no-deps --require-hashes',value)
        self.assertIn('COPY --chown=1000:1000 sdk/',value)
        self.assertLess(value.index('USER 1000:1000'),value.index('RUN python3.11 -m pip check'))
        self.assertIn('"mediacenter.worker_cli"',value)
        self.assertNotIn('#syntax=',value);self.assertNotIn('apt-get',value)

    def test_actual_parent_requires_independent_receipt_before_oci_reads(self):
        release=build.contract(ROOT)[0]
        with patch.object(build.core,'verify_oci',side_effect=AssertionError('unexpected OCI read')):
            with self.assertRaisesRegex(ValueError,'parent_approval_required'):
                build.parent_contract(ROOT,release,'missing',None,'missing')

    def test_incomplete_execution_inputs_never_spawn(self):
        with patch.object(build.core.subprocess,'Popen',side_effect=AssertionError('unexpected child')):
            with self.assertRaisesRegex(ValueError,'build_inputs_missing'):
                build.main(['--source',str(ROOT),'--build'])

    def test_export_without_publication_url_is_explicitly_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            result,_,actual=self.export_fixture(directory)
            self.assertEqual(result['status'],'incomplete_no_publication_url')
            self.assertIsNone(result['runtime_release']);self.assertEqual(result['draft_fields']['artifact']['url'],None)
            self.assertEqual(result['draft_fields']['image']['reference'],actual['manifest'])
            self.assertEqual(result['draft_fields']['image']['image_id'],actual['manifest'])
            self.assertFalse((Path(directory)/'export/runtime-release.json').exists())

    def test_export_actual_oci_becomes_strict_runtime_release_and_template_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            result,_,actual=self.export_fixture(directory,url='https://artifacts.example/sdxl.oci.tar',template=True)
            self.assertEqual(result['status'],'declaration_requires_approval_and_publication')
            self.assertEqual(result['runtime_release']['image']['reference'],actual['manifest'])
            self.assertEqual(result['runtime_release']['image']['image_id'],actual['manifest'])
            self.assertEqual(result['runtime_release']['image']['entrypoint'],['/fixture/worker'])
            self.assertEqual(result['template']['release_digest'],result['runtime_release_digest'])
            self.assertFalse(result['published']);self.assertFalse(result['imported'])
            self.assertEqual(result['missing'],[])

    def test_export_rejects_build_identity_or_publication_url_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError,'build_output_changed'):
                self.export_fixture(directory,actual_change={'manifest':'sha256:'+'e'*64})
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError,'runtime_artifact_url_invalid'):
                self.export_fixture(directory,url='http://remote.example/sdxl.tar')


if __name__=='__main__':unittest.main()
