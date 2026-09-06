from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

try:
    import numpy as np
except ModuleNotFoundError as exc:
    raise unittest.SkipTest("NumPy belongs to the worker image, not the control runtime") from exc

from mediacenter.adapters.cosyvoice import CosyVoiceAdapter, _fixed_reference
from mediacenter.audio_worker_cli import read_bootstrap
from mediacenter.capabilities import worker_capability_for
from mediacenter.worker_common import digest


class Cuda:
    @staticmethod
    def synchronize(): pass
    @staticmethod
    def empty_cache(): pass


class Torch:
    cuda = Cuda()
    @staticmethod
    def cat(values, dim): return np.concatenate(values, axis=dim)


class Model:
    sample_rate = 24000
    def __init__(self, cancel=None): self.calls = 0; self.cancel = cancel
    def inference_zero_shot(self, text, reference_text, reference, **options):
        self.calls += 1
        assert text and reference_text and reference and options["stream"] is True
        yield {"tts_speech": np.zeros((1, 1200), dtype=np.float32)}
        if self.cancel is not None: self.cancel.set()
        yield {"tts_speech": np.ones((1, 1200), dtype=np.float32) / 2}


class CosyVoiceContainerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binding = {"model_key":"cosyvoice2-0.5b","recipe_revision":"r1",
                        "model_asset_id":"mdl_voice","model_asset_revision":"v1"}

    def request(self, name="one"):
        return {"message_id":"cmd_"+name,"task_id":"task_"+name,"attempt_id":"attempt_"+name,
            "instance_id":"instance_one","worker_epoch":"epoch_one","payload":{
            "model_key":"cosyvoice2-0.5b","operation":"speech.generate",
            "parameters":{"prompt":"你好，欢迎使用 MediaCenter。","speed":1.0},"inputs":[],"loras":[]}}

    def adapter(self, cancel=None):
        value = CosyVoiceAdapter(binding=self.binding, asset_bindings={}, outputs=self.root,
                                 source_root=self.root)
        value.model = Model(cancel); value.torch = Torch; value.reference = self.root / "reference.wav"
        return value

    def test_warm_reuse_emits_typed_wav(self):
        adapter = self.adapter(); progress=[]
        adapter.execute(self.request("one"), progress.append, threading.Event()); adapter.reset_task_state()
        adapter.execute(self.request("two"), progress.append, threading.Event())
        self.assertEqual(adapter.model.calls, 2)
        with wave.open(str(self.root/"tasks/task_two/attempt_two/artifact.wav"), "rb") as reader:
            self.assertEqual((reader.getframerate(),reader.getnchannels(),reader.getnframes()),(24000,1,2400))
        manifest=json.loads((self.root/"tasks/task_two/attempt_two/manifest.json").read_text())
        self.assertEqual(manifest["media_metadata"]["duration_ms"],100)
        self.assertIn("encoding", {item["phase"] for item in progress})

    def test_cancel_marks_domain_unclean_and_never_publishes(self):
        cancellation=threading.Event(); adapter=self.adapter(cancellation)
        with self.assertRaisesRegex(ValueError,"task_canceled"):
            adapter.execute(self.request("cancel"),lambda _value:None,cancellation)
        with self.assertRaisesRegex(ValueError,"cosyvoice_quiescence_unconfirmed"):
            adapter.reset_task_state()
        self.assertFalse((self.root/"tasks/task_cancel/attempt_cancel/artifact.wav").exists())

    def test_reference_is_exact_bounded_and_fully_decoded(self):
        path=self.root/"asset/zero_shot_prompt.wav"; path.parent.mkdir()
        with wave.open(str(path),"wb") as writer:
            writer.setnchannels(1);writer.setsampwidth(2);writer.setframerate(16000)
            writer.writeframes(b"\0\0"*1600)
        raw=path.read_bytes()
        with patch("mediacenter.adapters.cosyvoice.REFERENCE_SIZE",len(raw)), \
             patch("mediacenter.adapters.cosyvoice.REFERENCE_SHA256",hashlib.sha256(raw).hexdigest()):
            self.assertEqual(_fixed_reference(self.root),path)
        path.write_bytes(raw[:-1])
        with self.assertRaises((ValueError,EOFError,wave.Error)):_fixed_reference(self.root)

    def test_reference_accepts_exact_ieee_float_wav(self):
        import struct
        path=self.root/"asset/zero_shot_prompt.wav";path.parent.mkdir()
        samples=b"\0\0\0\0"*1600
        fmt=struct.pack("<HHIIHH",3,1,16000,64000,4,32)
        raw=(b"RIFF"+struct.pack("<I",4+(8+len(fmt))+(8+len(samples)))+b"WAVE"
             +b"fmt "+struct.pack("<I",len(fmt))+fmt+b"data"+struct.pack("<I",len(samples))+samples)
        path.write_bytes(raw)
        with patch("mediacenter.adapters.cosyvoice.REFERENCE_SIZE",len(raw)), \
             patch("mediacenter.adapters.cosyvoice.REFERENCE_SHA256",hashlib.sha256(raw).hexdigest()):
            self.assertEqual(_fixed_reference(self.root),path)

    def test_fixed_bootstrap_selects_only_cosyvoice_release_target(self):
        identity=["server_one","instance_one","epoch_one"]
        binding=dict(self.binding,image_digest="sha256:"+"1"*64,gpu_uuids=["GPU-"+"1"*36],
                     capability_digest=digest(worker_capability_for("cosyvoice2-0.5b")))
        value={"schema":1,"server_id":identity[0],"instance_id":identity[1],"worker_epoch":identity[2],
            "binding":binding,"recovery_complete":True,"clock_trusted":True,"journal":"/mc-journal/worker.db",
            "outputs":"/mc-outputs","adapter_id":"cosyvoice","redis":{"username":"mc_w_"+digest(identity),
            "secret_file":"/mc-worker-secret","unix_socket":"/mc-redis/redis.sock"},"asset_bindings":{"main":{
            "asset_id":"mdl_voice","revision":"v1","manifest_digest":"2"*64,"path":"/mc-models/assets/mdl_voice"},
            "dependencies":{}},"lora_authority":{"path":"/mc-lora","families":[]}}
        path=self.root/"bootstrap.json";path.write_text(json.dumps(value));path.chmod(0o600)
        self.assertEqual(read_bootstrap(path)[0]["adapter_id"],"cosyvoice")
        value["adapter_id"]="musicgen";path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,"bootstrap_binding_invalid"):read_bootstrap(path)

    def test_runtime_layers_are_neutral_and_derivative_has_no_package_delta(self):
        project=Path(__file__).parents[1]
        runtime=json.loads((project/"containers/runtime-v0/python.lock").read_text())
        self.assertEqual(runtime["inventory_count"],174)
        self.assertEqual(runtime["required_roots"]["torch"],"2.3.1+cu121")
        self.assertFalse(runtime["artifact_closure"]["complete"])
        derivative=json.loads((project/"containers/cosyvoice/python.lock").read_text())
        self.assertEqual(derivative["packages"],[])
        self.assertFalse((project/"mediacenter/drivers/cosyvoice.py").exists())

    def test_bounded_real_smoke_plan_accepts_speech_without_lora(self):
        from scripts.verify_model_container import check_plan
        plan=json.loads((Path(__file__).parents[1]/"tests/fixtures/container-smoke/cosyvoice.json").read_text(encoding="utf-8"))
        plan.update(run_id="run_voice",server_url="http://127.0.0.1:8787",
                    binding_digest="1"*64,image_digest="sha256:"+"2"*64,
                    release_digest="3"*64,gpu_uuids=["GPU-"+"1"*36],installation_id="install_voice")
        self.assertEqual(check_plan(plan)["task"]["service"],"speech")


if __name__ == "__main__": unittest.main()
