from __future__ import annotations
import hashlib, json, tempfile, threading, unittest
from pathlib import Path

from mediacenter.adapters.wan21 import Wan21Adapter
from mediacenter.capabilities import worker_capability_for
from mediacenter.video_worker_cli import read_bootstrap
from mediacenter.worker_common import digest


class _Cuda:
    @staticmethod
    def synchronize(): pass
    @staticmethod
    def empty_cache(): pass
class _Torch:
    cuda=_Cuda()
    class Generator:
        def __init__(self,device): self.device=device
        def manual_seed(self,seed): self.seed=seed;return self
class _Frame:
    size=(480,320)
class _Result:
    frames=[[_Frame() for _ in range(9)]]
class _Pipe:
    _interrupt=False
    def __init__(self): self.calls=0
    def __call__(self,**kwargs):
        self.calls+=1
        for step in range(kwargs["num_inference_steps"]):
            kwargs["callback_on_step_end"](self,step,0,{})
        return _Result()
class _Encoder:
    calls=0
    def encode(self,frames,pending,*,width,height,fps,cancellation,progress):
        type(self).calls+=1
        for index in range(len(frames)):
            if cancellation.is_set(): raise ValueError("task_canceled")
            progress({"phase":"encoding","completed":index+1,"total":len(frames),"unit":"frames"})
        pending.write_bytes(b"\0\0\0\x18ftypisom"+b"fixture")
        return {"width":width,"height":height,"frame_count":len(frames),"fps_numerator":fps,
                "fps_denominator":1,"duration_ms":round(len(frames)*1000/fps)}


class Wan21AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.pipe=_Pipe();_Encoder.calls=0
        binding={"model_key":"wan2.1-t2v-1.3b","recipe_revision":"r1","model_asset_id":"mdl_one",
                 "model_asset_revision":"v1"}
        self.adapter=Wan21Adapter(binding=binding,asset_bindings={},outputs=self.root,encoder_factory=_Encoder)
        self.adapter.pipe=self.pipe;self.adapter.torch=_Torch
        self.request={"message_id":"cmd_one","task_id":"task_one","attempt_id":"attempt_one",
            "instance_id":"instance_one","worker_epoch":"epoch_one","payload":{"model_key":"wan2.1-t2v-1.3b",
            "operation":"video.generate","parameters":{"prompt":"a calm lake","width":480,"height":320,
            "num_frames":9,"steps":2,"fps":8},"inputs":[],"loras":[]}}

    def test_sampling_encoding_publication_and_warm_reuse_are_distinct(self):
        phases=[];result=self.adapter.execute(self.request,lambda value:phases.append(value),threading.Event())
        self.assertTrue(result["asset_id"].startswith("art_"));self.assertEqual(phases[0]["phase"],"sampling")
        self.assertEqual(phases[-1]["phase"],"encoding")
        manifest=json.loads((self.root/"tasks/task_one/attempt_one/manifest.json").read_text())
        self.assertEqual(manifest["media_metadata"]["frame_count"],9)
        self.adapter.reset_task_state()
        self.request["task_id"]="task_two";self.request["attempt_id"]="attempt_two";self.request["message_id"]="cmd_two"
        self.adapter.execute(self.request,lambda _value:None,threading.Event());self.adapter.reset_task_state()
        self.assertEqual(self.pipe.calls,2);self.assertEqual(_Encoder.calls,2)

    def test_sampling_cancellation_never_starts_encoder_or_publishes(self):
        cancel=threading.Event()
        def progress(value):
            if value["phase"]=="sampling":cancel.set()
        with self.assertRaisesRegex(ValueError,"task_canceled"):
            self.adapter.execute(self.request,progress,cancel)
        self.assertEqual(_Encoder.calls,0);self.assertFalse(list(self.root.rglob("artifact.mp4")))
        self.adapter.reset_task_state()

    def test_encoding_cancellation_removes_pending_and_never_publishes(self):
        cancel=threading.Event()
        def progress(value):
            if value["phase"]=="encoding" and value["completed"]==1:cancel.set()
        with self.assertRaisesRegex(ValueError,"task_canceled"):
            self.adapter.execute(self.request,progress,cancel)
        self.assertFalse(list(self.root.rglob("*.pending.mp4")));self.assertFalse(list(self.root.rglob("artifact.mp4")))
        self.adapter.reset_task_state()

    def test_fixed_worker_bootstrap_rejects_task_selected_runtime_fields(self):
        identity={"server_id":"server_one","instance_id":"instance_one","worker_epoch":"epoch_one"}
        binding={"model_key":"wan2.1-t2v-1.3b","recipe_revision":"r1","model_asset_id":"mdl_one",
            "model_asset_revision":"v1","image_digest":"sha256:"+"1"*64,
            "gpu_uuids":["GPU-"+"1"*36],"capability_digest":digest(worker_capability_for("wan2.1-t2v-1.3b"))}
        value={"schema":1,**identity,"binding":binding,"recovery_complete":True,"clock_trusted":True,
            "journal":"/mc-journal/worker.db","outputs":"/mc-outputs","adapter_id":"wan21",
            "redis":{"username":"mc_w_"+digest(list(identity.values())),"secret_file":"/mc-worker-secret",
                     "unix_socket":"/mc-redis/redis.sock"},
            "asset_bindings":{"main":{"asset_id":"mdl_one","revision":"v1","manifest_digest":"2"*64,
                                       "path":"/mc-models/assets/mdl_one"},"dependencies":{}},
            "lora_authority":{"path":"/mc-lora","families":[]}}
        path=self.root/"bootstrap.json";path.write_text(json.dumps(value));path.chmod(0o600)
        parsed,worker,_evidence=read_bootstrap(path)
        self.assertEqual(worker.instance_id,"instance_one");self.assertEqual(parsed["adapter_id"],"wan21")
        value["module"]="task.chosen";path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,"bootstrap_invalid"):read_bootstrap(path)

    def test_wan_release_locks_parent_inputs_sdk_and_offline_requirements(self):
        source=Path(__file__).resolve().parents[1]
        release=json.loads((source/"containers/wan21/release.json").read_text())
        sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(release["parent_release_sha256"],sha(source/"containers/runtime-v1/release.json"))
        for name,expected in release["inputs"].items():
            self.assertEqual(expected,sha(source/"containers/wan21"/name))
        for item in release["sdk"]:
            path=source/item["path"]
            self.assertEqual((item["size"],item["sha256"]),(path.stat().st_size,sha(path)))
        lock=json.loads((source/"containers/wan21/python.lock").read_text())
        requirements=(source/"containers/wan21/requirements.txt").read_text()
        self.assertEqual(len(lock["packages"]),4)
        for package in lock["packages"]:
            self.assertIn(f'{package["name"].lower()}=={package["version"]}',requirements.lower())
            self.assertIn(package["sha256"],requirements)

if __name__=="__main__":unittest.main()
