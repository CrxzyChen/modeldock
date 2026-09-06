from __future__ import annotations
import hashlib,json,sys,tempfile,threading,types,unittest
from pathlib import Path
from unittest import mock
from mediacenter.adapters.h3 import H3Adapter
from mediacenter.adapters.hunyuan15 import Hunyuan15Adapter
from mediacenter.adapters.ltx import LTXAdapter
from mediacenter.adapters.wan22 import Wan22Adapter
from mediacenter.adapters.video import ManagedFfmpegEncoder
from mediacenter.capabilities import worker_capability_for
from mediacenter.video_worker_cli import read_bootstrap
from mediacenter.worker_common import digest

class Cuda:
    @staticmethod
    def synchronize():pass
    @staticmethod
    def empty_cache():pass
class Torch:
    cuda=Cuda()
    class Generator:
        def __init__(self,device=None):self.device=device
        def manual_seed(self,seed):return self
class Frame:pass
class Encoder:
    def encode(self,frames,pending,*,width,height,fps,cancellation,progress,audio=None,audio_sample_rate=None):
        for i in range(len(frames)):progress({"phase":"encoding","completed":i+1,"total":len(frames),"unit":"frames"})
        pending.write_bytes(b"\0\0\0\x18ftypisomfixture")
        return {"width":width,"height":height,"frame_count":len(frames),"fps_numerator":fps,"fps_denominator":1,
                "duration_ms":round(len(frames)*1000/fps),"audio_streams":int(audio is not None),
                "audio_sample_rate":audio_sample_rate or 0,"audio_channels":1 if audio is not None else 0}
class Result:
    def __init__(self,count):self.frames=[[Frame() for _ in range(count)]]
class Pipe:
    _interrupt=False
    def __init__(self,kind):self.kind=kind;self.vocoder=type("V",(),{"config":type("C",(),{"output_sampling_rate":24000})()})()
    def __call__(self,**kwargs):
        callback=kwargs.get("callback_on_step_end")
        if callback:
            for i in range(kwargs["num_inference_steps"]):callback(self,i,0,{})
        count=kwargs["num_frames"]
        if self.kind=="ltx":return [[Frame() for _ in range(count)]],[0.0]*240
        if self.kind=="h3":return {"videos":[[Frame() for _ in range(count)]],"audio":[[0.0]*240],"sampling_rate":24000}
        return Result(count)
class PipeWithoutCallback(Pipe):
    def __call__(self,prompt,negative_prompt,width,height,num_frames,num_inference_steps,generator):
        return Result(num_frames)
class Reference:
    @classmethod
    def from_file(cls,path):return (cls.__name__,path)

class VideoModelAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        raw=b"reference";self.revision=hashlib.sha256(raw).hexdigest();(self.root/"ast_ref.png").write_bytes(raw)
    def request(self,model,task,inputs=()):
        return {"message_id":"cmd_"+task,"task_id":"task_"+task,"attempt_id":"attempt_"+task,
            "instance_id":"instance_one","worker_epoch":"epoch_one","payload":{"model_key":model,
            "operation":"video.generate","parameters":{"prompt":"test scene"},"inputs":list(inputs),"loras":[]}}
    def adapter(self,cls,model,kind):
        binding={"model_key":model,"recipe_revision":"r1","model_asset_id":"mdl_one","model_asset_revision":"v1"}
        value=cls(binding=binding,asset_bindings={},outputs=self.root,inputs=self.root,encoder_factory=Encoder)
        value.pipe=Pipe(kind);value.torch=Torch;return value
    def test_all_four_publish_and_reset_with_exact_reference_contracts(self):
        reference={"asset_id":"ast_ref","revision":self.revision,"media_type":"image/png"}
        cases=[(LTXAdapter,"ltx-2.3-distilled","ltx",()),(Hunyuan15Adapter,"hunyuanvideo-1.5-720p-t2v","hunyuan",()),
               (Wan22Adapter,"wan2.2-i2v-a14b","wan22",(reference,)),(H3Adapter,"minimax-h3-ref2va","h3",(reference,))]
        for index,(cls,model,kind,inputs) in enumerate(cases):
            with self.subTest(model=model):
                adapter=self.adapter(cls,model,kind)
                if cls is Wan22Adapter:adapter.load_image=lambda path:path
                if cls is H3Adapter:adapter.references={"image/":Reference,"video/":Reference,"audio/":Reference}
                progress=[];result=adapter.execute(self.request(model,str(index),inputs),progress.append,threading.Event())
                self.assertTrue(result["asset_id"].startswith("art_"));self.assertEqual(progress[-1]["phase"],"encoding")
                adapter.reset_task_state();self.assertFalse(adapter.dirty)
    def test_reference_models_reject_missing_or_wrong_media_before_pipeline(self):
        for cls,model in ((Wan22Adapter,"wan2.2-i2v-a14b"),(H3Adapter,"minimax-h3-ref2va")):
            adapter=self.adapter(cls,model,"unused")
            with self.subTest(model=model),self.assertRaises(ValueError):
                adapter.execute(self.request(model,"bad"),lambda _value:None,threading.Event())
    def test_pipeline_without_step_callback_uses_supervisor_cancellation_contract(self):
        adapter=self.adapter(Hunyuan15Adapter,"hunyuanvideo-1.5-720p-t2v","hunyuan")
        adapter.pipe=PipeWithoutCallback("hunyuan");progress=[]
        result=adapter.execute(self.request("hunyuanvideo-1.5-720p-t2v","no_callback"),progress.append,threading.Event())
        self.assertTrue(result["asset_id"].startswith("art_"))
        self.assertEqual({item["phase"] for item in progress},{"encoding"})
    def test_short_audio_never_truncates_the_authoritative_video_frame_grid(self):
        import io
        try:
            import numpy as np
        except ModuleNotFoundError:
            self.skipTest("NumPy belongs to the worker image, not the control runtime")
        pending=self.root/"short-audio.pending.mp4"
        frames=[np.zeros((32,32,3),dtype=np.uint8) for _ in range(17)]
        commands=[]
        class Process:
            def __init__(self,command):
                commands.append(command);self.stdin=io.BytesIO();self.stderr=io.BytesIO();self.returncode=0
                Path(command[-1]).write_bytes(b"fixture")
            def poll(self):return self.returncode
            def kill(self):self.returncode=-9
            def wait(self):return self.returncode
        class Library:
            @staticmethod
            def read_frames(*_args,**_kwargs):
                raw=b"\0"*(32*32*3)
                def values():
                    yield {"size":(32,32)}
                    yield from [raw]*17
                return values()
        encoder=ManagedFfmpegEncoder.__new__(ManagedFfmpegEncoder);encoder.exe="/trusted/ffmpeg";encoder.library=Library
        with mock.patch("mediacenter.adapters.video.subprocess.Popen",side_effect=lambda command,**_kwargs:Process(command)):
            metadata=encoder.encode(frames,pending,width=32,height=32,fps=8,
                cancellation=threading.Event(),progress=lambda _value:None,
                audio=np.zeros(2400,dtype=np.float32),audio_sample_rate=24000)
        self.assertEqual((metadata["frame_count"],metadata["audio_streams"]),(17,1))
        self.assertNotIn("-shortest",commands[0])
    def test_wan22_load_uses_the_pinned_diffusers_utils_export(self):
        torch=types.ModuleType("torch");torch.bfloat16=object()
        torch.cuda=type("Cuda",(),{"is_available":staticmethod(lambda:True),"synchronize":staticmethod(lambda:None)})()
        pipe=type("Pipeline",(),{
            "vae":type("Vae",(),{"enable_tiling":lambda self:None})(),
            "enable_model_cpu_offload":lambda self,device:None})()
        pipeline=type("DiffusionPipeline",(),{"from_pretrained":staticmethod(lambda *args,**kwargs:pipe)})
        diffusers=types.ModuleType("diffusers");diffusers.__path__=[];diffusers.DiffusionPipeline=pipeline
        utilities=types.ModuleType("diffusers.utils");sentinel=lambda path:path;utilities.load_image=sentinel
        adapter=self.adapter(Wan22Adapter,"wan2.2-i2v-a14b","wan22")
        adapter.pipe=None;adapter.main_asset=lambda binding:self.root
        with mock.patch.dict(sys.modules,{"torch":torch,"diffusers":diffusers,"diffusers.utils":utilities}):
            adapter.load(adapter.binding)
        self.assertIs(adapter.pipe,pipe);self.assertIs(adapter.load_image,sentinel)
    def test_fixed_bootstrap_binds_each_adapter_and_declared_gpu_cardinality(self):
        models={"minimax-h3-ref2va":("h3",2),"ltx-2.3-distilled":("ltx",1),
            "wan2.2-i2v-a14b":("wan22",1),"hunyuanvideo-1.5-720p-t2v":("hunyuan15",1)}
        for index,(model,(adapter_id,count)) in enumerate(models.items()):
            with self.subTest(model=model):
                identity=["server_one","instance_one","epoch_one"]
                binding={"model_key":model,"recipe_revision":"r1","model_asset_id":"mdl_one","model_asset_revision":"v1",
                    "image_digest":"sha256:"+"1"*64,"gpu_uuids":["GPU-"+str(number)*36 for number in range(1,count+1)],
                    "capability_digest":digest(worker_capability_for(model))}
                value={"schema":1,"server_id":identity[0],"instance_id":identity[1],"worker_epoch":identity[2],
                    "binding":binding,"recovery_complete":True,"clock_trusted":True,"journal":"/mc-journal/worker.db",
                    "outputs":"/mc-outputs","adapter_id":adapter_id,"redis":{"username":"mc_w_"+digest(identity),
                    "secret_file":"/mc-worker-secret","unix_socket":"/mc-redis/redis.sock"},"asset_bindings":{"main":{
                    "asset_id":"mdl_one","revision":"v1","manifest_digest":"2"*64,"path":"/mc-models/assets/mdl_one"},
                    "dependencies":{}},"lora_authority":{"path":"/mc-lora","families":[]}}
                path=self.root/f"bootstrap-{index}.json";path.write_text(json.dumps(value));path.chmod(0o600)
                self.assertEqual(read_bootstrap(path)[0]["adapter_id"],adapter_id)
                if model=="minimax-h3-ref2va":
                    value["binding"]["gpu_uuids"]=value["binding"]["gpu_uuids"][:1];path.write_text(json.dumps(value))
                    with self.assertRaisesRegex(ValueError,"bootstrap_gpu_invalid"):read_bootstrap(path)
if __name__=="__main__":unittest.main()
