from __future__ import annotations
import contextlib,json,tempfile,threading,unittest,wave
from pathlib import Path
try:
    import numpy as np
except ModuleNotFoundError as exc:
    raise unittest.SkipTest("NumPy belongs to the worker image, not the control runtime") from exc
from mediacenter.adapters.musicgen import MusicGenAdapter
from mediacenter.audio_worker_cli import read_bootstrap
from mediacenter.capabilities import worker_capability_for
from mediacenter.worker_common import digest

class Cuda:
    @staticmethod
    def synchronize(): pass
    @staticmethod
    def empty_cache(): pass
    @staticmethod
    def current_device(): return 0
class Random:
    @staticmethod
    def fork_rng(devices): return contextlib.nullcontext()
class Torch:
    cuda=Cuda(); random=Random()
    @staticmethod
    def manual_seed(seed): Torch.seed=seed
class Values(dict):
    def to(self,_device): return self
class Processor:
    def __init__(self): self.calls=0
    def __call__(self,**_kwargs): self.calls+=1;return Values(input_ids=np.zeros((1,2),dtype=np.int64))
class Config:
    audio_encoder=type("Audio",(),{"frame_rate":50,"sampling_rate":32000})()
class Model:
    config=Config()
    def __init__(self,cancel=None):self.calls=0;self.cancel=cancel
    def generate(self,**kwargs):
        self.calls+=1
        criteria=kwargs["stopping_criteria"][0]
        criteria(np.zeros((1,kwargs["max_new_tokens"]//2),dtype=np.int64),None)
        if self.cancel:self.cancel.set();criteria(np.zeros((1,kwargs["max_new_tokens"]),dtype=np.int64),None)
        return np.zeros((1,1,3200),dtype=np.float32)

class MusicAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.binding={"model_key":"musicgen-small","recipe_revision":"r1","model_asset_id":"mdl_one","model_asset_revision":"v1"}
    def request(self,name="one"):
        return {"message_id":"cmd_"+name,"task_id":"task_"+name,"attempt_id":"attempt_"+name,
            "instance_id":"instance_one","worker_epoch":"epoch_one","payload":{"model_key":"musicgen-small",
            "operation":"music.generate","parameters":{"prompt":"warm analog synth","duration_seconds":2.0},"inputs":[],"loras":[]}}
    def adapter(self,cancel=None):
        value=MusicGenAdapter(binding=self.binding,asset_bindings={},outputs=self.root)
        value.processor=Processor();value.model=Model(cancel);value.torch=Torch
        return value
    def test_warm_reuse_reset_and_fully_decoded_typed_wav(self):
        adapter=self.adapter();progress=[]
        first=adapter.execute(self.request("one"),progress.append,threading.Event())
        self.assertTrue(first["asset_id"].startswith("art_"));adapter.reset_task_state()
        second=adapter.execute(self.request("two"),progress.append,threading.Event())
        self.assertEqual(adapter.model.calls,2);self.assertEqual(adapter.processor.calls,2)
        with wave.open(str(self.root/"tasks"/"task_two"/"attempt_two"/"artifact.wav"),"rb") as reader:
            self.assertEqual((reader.getframerate(),reader.getnchannels(),reader.getnframes(),reader.getsampwidth()),(32000,1,3200,2))
        manifest=json.loads((self.root/"tasks"/"task_two"/"attempt_two"/"manifest.json").read_text())
        self.assertEqual(manifest["media_metadata"],{"sample_rate":32000,"channels":1,"sample_count":3200,"duration_ms":100,"bits_per_sample":16})
        self.assertEqual({item["phase"] for item in progress},{"sampling","decoding","encoding"})
    def test_cancel_during_sampling_never_publishes_partial_audio(self):
        cancel=threading.Event();adapter=self.adapter(cancel)
        with self.assertRaisesRegex(ValueError,"task_canceled"):
            adapter.execute(self.request("cancel"),lambda _value:None,cancel)
        self.assertFalse((self.root/"tasks"/"task_cancel"/"attempt_cancel"/"artifact.wav").exists())
    def test_fixed_bootstrap_one_gpu_and_no_task_selected_adapter(self):
        identity=["server_one","instance_one","epoch_one"]
        binding=dict(self.binding,image_digest="sha256:"+"1"*64,gpu_uuids=["GPU-"+"1"*36],
                     capability_digest=digest(worker_capability_for("musicgen-small")))
        value={"schema":1,"server_id":identity[0],"instance_id":identity[1],"worker_epoch":identity[2],
            "binding":binding,"recovery_complete":True,"clock_trusted":True,"journal":"/mc-journal/worker.db",
            "outputs":"/mc-outputs","adapter_id":"musicgen","redis":{"username":"mc_w_"+digest(identity),
            "secret_file":"/mc-worker-secret","unix_socket":"/mc-redis/redis.sock"},"asset_bindings":{"main":{
            "asset_id":"mdl_one","revision":"v1","manifest_digest":"2"*64,"path":"/mc-models/assets/mdl_one"},
            "dependencies":{}},"lora_authority":{"path":"/mc-lora","families":[]}}
        path=self.root/"bootstrap.json";path.write_text(json.dumps(value));path.chmod(0o600)
        self.assertEqual(read_bootstrap(path)[0]["adapter_id"],"musicgen")
        value["binding"]["gpu_uuids"].append("GPU-"+"2"*36);path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,"bootstrap_gpu_invalid"):read_bootstrap(path)
    def test_legacy_driver_is_retired_and_container_has_zero_dependency_delta(self):
        self.assertFalse((Path(__file__).parents[1]/"mediacenter/drivers/musicgen.py").exists())
        lock=json.loads((Path(__file__).parents[1]/"containers/musicgen/python.lock").read_text())
        self.assertEqual(lock["packages"],[]);self.assertIn("transformers",lock["closure"]["base_reused"])
        release=json.loads((Path(__file__).parents[1]/"containers/musicgen/release.json").read_text(encoding="utf-8"))
        self.assertEqual(release["license"],{"spdx":"CC-BY-NC-4.0","commercial_use":False,
                         "notice":"MusicGen Small 仅限许可证允许的非商业用途。"})
        for item in release["sdk"]:
            path=Path(__file__).parents[1]/item["path"]
            import hashlib
            self.assertEqual((item["size"],item["sha256"]),(path.stat().st_size,hashlib.sha256(path.read_bytes()).hexdigest()))

if __name__=="__main__":unittest.main()
