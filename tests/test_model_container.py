import copy
import hashlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import verify_model_container as verify

ROOT=Path(__file__).resolve().parents[1]


def plan():
    value=json.loads((ROOT/'tests/fixtures/container-smoke/sdxl.json').read_text())
    value.update(run_id='fixture-run',server_url='http://127.0.0.1:12345',instance_id='sdxl',binding_digest='a'*64,
        image_digest='sha256:'+'b'*64,release_digest='c'*64,gpu_uuids=['GPU-'+'1'*36],installation_id='installation')
    value['task']['model']='sdxl'
    value['lora_a']={'asset_id':'fixture-a','revision':'r1','family':'sdxl','weight':.7}
    value['lora_b']={'asset_id':'fixture-b','revision':'r1','family':'sdxl','weight':.5}
    return value


class ModelContainerTests(unittest.TestCase):
    def test_missing_real_resources_cannot_be_a_smoke_success(self):
        incomplete=json.loads((ROOT/'tests/fixtures/container-smoke/sdxl.json').read_text())
        with patch.object(verify.http.client,'HTTPConnection',side_effect=AssertionError('network')):
            with self.assertRaises(ValueError):verify.check_plan(incomplete)
            self.assertEqual(verify.check_plan(plan())['task']['options']['seed'],42)

    def test_remote_plaintext_unbounded_and_wrong_family_rejected(self):
        for key,wrong in [('server_url','http://example.org'),('timeout_seconds',999999),('lora_b',dict(plan()['lora_b'],family='wan'))]:
            value=plan();value[key]=wrong
            with self.assertRaises(ValueError):verify.check_plan(value)

    def test_execute_without_independent_approval_does_not_connect(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'plan.json';path.write_text(json.dumps(plan()))
            with patch.object(verify.http.client,'HTTPConnection',side_effect=AssertionError('network')):
                with self.assertRaisesRegex(ValueError,'smoke_independent_approval_required'):
                    verify.main(['--plan',str(path),'--execute'])

    def test_actual_cpu_decode_hash_and_crc(self):
        try:from PIL import Image
        except ImportError:self.skipTest('Pillow unavailable; actual decoder positive not executed')
        stream=io.BytesIO();Image.new('RGB',(16,16)).save(stream,format='PNG');raw=stream.getvalue()
        self.assertEqual(verify.decode(raw,{'width':16,'height':16},hashlib.sha256(raw).hexdigest())['bytes'],len(raw))
        with self.assertRaises((ValueError,OSError,SyntaxError)):
            verify.decode(raw[:-12],{'width':16,'height':16},hashlib.sha256(raw[:-12]).hexdigest())

    def test_preflight_target_mismatch_is_read_only_and_failure_retained(self):
        class Client:
            deadline=time.monotonic()+10;bytes=0
            def request(self,method,path,*args):
                self_method=method
                if self_method!='GET':raise AssertionError('preflight must not mutate')
                return {'instance_id':'wrong-instance'}
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'evidence'
            with self.assertRaises((ValueError,KeyError)):verify.exercise(plan(),Client(),output)
            self.assertTrue((output/'intent.json').is_file());self.assertTrue((output/'failure.json').is_file())
            self.assertFalse((output/'result.json').exists())


if __name__=='__main__':unittest.main()
