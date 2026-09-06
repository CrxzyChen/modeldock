"""CPU contract fixtures, deliberately not SDXL/GPU acceptance evidence."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mediacenter.adapters.sdxl import SDXLAdapter, asset_path, verify_files
from mediacenter.capabilities import worker_capability_for
from tests import test_task_state


class SDXLAdapterTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_task_state.TaskStateTests(); self.fixture.setUp()
        self.task, self.command = self.fixture.dispatched()
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.outputs = self.root / 'outputs'; self.outputs.mkdir()
        self.adapter = SDXLAdapter(binding={}, asset_bindings={}, outputs=str(self.outputs), lora_directory=str(self.root))
        self.image = Mock(size=(1024, 1024))
        self.image.save.side_effect = lambda stream, format: stream.write(b'fixture-png-encoding')
        component = lambda: SimpleNamespace(peft_config={}, _hf_peft_config_loaded=False, modules=lambda: [])
        self.pipe = Mock(unet=component(), text_encoder=component(), text_encoder_2=component())
        self.pipe.return_value = SimpleNamespace(images=[self.image])
        self.adapter.pipe = self.pipe
        self.adapter.torch = Mock()
        self.adapter.scheduler_class = Mock()
        self.adapter.scheduler_config = {'fixed': True}
        self.adapter.tuner_class = type('Tuner', (), {})
        self.cancel = Mock(); self.cancel.is_set.return_value = False

    def tearDown(self):
        self.temp.cleanup(); self.fixture.tearDown()

    def test_same_pipeline_two_tasks_reset_and_seed_are_task_scoped(self):
        first = self.adapter.execute(self.command, Mock(), self.cancel)
        self.assertEqual(first['sha256'], hashlib.sha256(b'fixture-png-encoding').hexdigest())
        with self.assertRaisesRegex(ValueError, 'model_not_clean'):
            self.adapter.execute(self.command, Mock(), self.cancel)
        self.adapter.reset_task_state()
        second = json.loads(json.dumps(self.command)); second['attempt_id'] = 'att_second'
        second['payload']['parameters']['seed'] = 73
        self.adapter.execute(second, Mock(), self.cancel)
        self.adapter.reset_task_state()
        self.assertIs(self.adapter.pipe, self.pipe)
        self.assertEqual(self.pipe.call_count, 2)
        self.assertEqual(self.adapter.torch.Generator.call_count, 2)
        self.assertEqual(self.pipe.unload_lora_weights.call_count, 2)
        manifest = json.loads((self.outputs / 'tasks' / self.command['task_id'] / self.command['attempt_id'] / 'manifest.json').read_text())
        self.assertEqual(manifest['attempt_id'], self.command['attempt_id'])

    def test_cancel_before_pipeline_and_text_encoder_residue_fail_closed(self):
        self.cancel.is_set.return_value = True
        with self.assertRaisesRegex(ValueError, 'task_canceled'):
            self.adapter.execute(self.command, Mock(), self.cancel)
        self.pipe.assert_not_called()
        self.pipe.text_encoder_2.peft_config = {'residual': True}
        with self.assertRaisesRegex(ValueError, 'lora_reset_unconfirmed'):
            self.adapter.reset_task_state()
        self.assertTrue(self.adapter.dirty)

    def test_lora_fixed_loader_flags_and_reset_without_base_reload(self):
        self.command['payload']['loras'] = [{'asset_id':'lora_A','revision':'v1','family':'sdxl','weight':0.75}]
        with patch('mediacenter.adapters.sdxl.lora_descriptor', return_value={'path':'/approved', 'weight_name':'a.safetensors'}):
            self.adapter.execute(self.command, Mock(), self.cancel)
        self.pipe.load_lora_weights.assert_called_once_with('/approved', weight_name='a.safetensors', adapter_name='mc_task',
            local_files_only=True, use_safetensors=True, hotswap=False)
        self.pipe.set_adapters.assert_called_once_with(['mc_task'], adapter_weights=[0.75])
        self.adapter.reset_task_state()

    def test_asset_manifest_uses_actual_published_digest_and_rejects_drift(self):
        body = b'weights'; sha = hashlib.sha256(body).hexdigest()
        files = [{'relative_path':'model.safetensors','sha256':sha,'byte_size':len(body)}]
        manifest_digest = hashlib.sha256(b'model.safetensors\0' + sha.encode() + b'\0' + b'7').hexdigest()
        (self.root / 'model.safetensors').write_bytes(body)
        (self.root / 'manifest.json').write_text(json.dumps({'asset_id':'mdl_a','manifest_digest':manifest_digest,'files':files}))
        mapping = dict(asset_id='mdl_a', revision='v1', manifest_digest=manifest_digest, path=str(self.root))
        self.assertEqual(asset_path(mapping), self.root)
        (self.root / 'model.safetensors').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'asset_content_changed'): asset_path(mapping)


if __name__ == '__main__': unittest.main()
