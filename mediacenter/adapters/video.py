"""Shared resident video adapter, managed input and bounded FFmpeg helpers."""
from __future__ import annotations
import gc, hashlib, inspect, os, re, subprocess, threading, time
from pathlib import Path
from ..adapter import Adapter
from ..capabilities import worker_capability_for
from ..worker_common import canonical, digest
from .sdxl import asset_path, checked, regular, require

INPUT_EXTENSIONS={"image/png":".png","image/jpeg":".jpg","image/webp":".webp",
                  "video/mp4":".mp4","video/webm":".webm","audio/wav":".wav",
                  "audio/mpeg":".mp3","audio/flac":".flac","audio/mp4":".m4a"}

class ManagedFfmpegEncoder:
    def __init__(self):
        import imageio_ffmpeg
        self.library=imageio_ffmpeg;exe=checked(imageio_ffmpeg.get_ffmpeg_exe())
        package=checked(Path(imageio_ffmpeg.__file__).parent)
        require(exe.is_file() and package in exe.parents,"ffmpeg_binary_untrusted");self.exe=str(exe)

    @staticmethod
    def frame_bytes(frame,width,height):
        if hasattr(frame,"convert"):
            frame=frame.convert("RGB");require(frame.size==(width,height),"video_frame_size_mismatch");return frame.tobytes()
        import numpy as np
        value=np.asarray(frame);require(value.shape==(height,width,3),"video_frame_size_mismatch")
        if value.dtype!=np.uint8:
            require(np.isfinite(value).all(),"video_frame_nonfinite")
            if value.min()>=0 and value.max()<=1:value=(np.clip(value,0,1)*255).round()
            value=value.astype(np.uint8)
        return value.tobytes()

    @staticmethod
    def audio_file(audio,path):
        if audio is None:return None
        import numpy as np
        value=audio.detach().float().cpu().numpy() if hasattr(audio,"detach") else np.asarray(audio,dtype=np.float32)
        require(value.ndim in (1,2) and value.size>0 and np.isfinite(value).all(),"audio_samples_invalid")
        if value.ndim==1:value=value[:,None]
        elif value.shape[0]<=8:value=value.T
        require(1<=value.shape[1]<=8,"audio_channels_invalid")
        value=np.ascontiguousarray(value,dtype="<f4")
        with path.open("xb") as stream:stream.write(value.tobytes());stream.flush();os.fsync(stream.fileno())
        return value.shape[1]

    def encode(self,frames,pending,*,width,height,fps,cancellation,progress,audio=None,audio_sample_rate=None):
        require(not pending.exists() and len(frames)>0,"video_output_exists")
        raw_audio=pending.with_suffix(".audio.f32");channels=self.audio_file(audio,raw_audio)
        if channels is not None:require(type(audio_sample_rate) is int and 8000<=audio_sample_rate<=192000,"audio_rate_invalid")
        command=[self.exe,"-nostdin","-hide_banner","-loglevel","error","-y","-f","rawvideo","-pix_fmt","rgb24",
                 "-s",f"{width}x{height}","-r",str(fps),"-i","pipe:0"]
        if channels is not None:command += ["-f","f32le","-ar",str(audio_sample_rate),"-ac",str(channels),"-i",str(raw_audio)]
        command += ["-map","0:v:0"]
        if channels is not None:command += ["-map","1:a:0"]
        command += ["-c:v","libx264","-pix_fmt","yuv420p","-movflags","+faststart"]
        # LTX audio can be a few samples shorter than the requested frame grid.
        # The video frame contract is authoritative, so never let FFmpeg's
        # shortest-stream policy silently truncate valid generated frames.
        if channels is not None:command += ["-c:a","aac"]
        command += [str(pending)]
        process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        errors=[]
        reader=threading.Thread(target=lambda:errors.append(process.stderr.read(65537)),daemon=True);reader.start()
        try:
            for index,frame in enumerate(frames):
                require(not cancellation.is_set(),"task_canceled")
                process.stdin.write(self.frame_bytes(frame,width,height));process.stdin.flush()
                progress({"phase":"encoding","completed":index+1,"total":len(frames),"unit":"frames"})
            process.stdin.close();deadline=time.monotonic()+120
            while process.poll() is None:
                if cancellation.is_set() or time.monotonic()>deadline:process.kill();process.wait();raise ValueError("task_canceled" if cancellation.is_set() else "video_encode_timeout")
                time.sleep(.02)
            reader.join(2);require(not reader.is_alive() and process.returncode==0 and sum(map(len,errors))<=65536,"video_encode_failed")
            require(pending.is_file() and pending.stat().st_size>0,"video_encode_failed")
            metadata=self.validate(pending,width=width,height=height,frames=len(frames),fps=fps,cancellation=cancellation)
            metadata.update(audio_streams=1 if channels else 0,audio_sample_rate=audio_sample_rate or 0,audio_channels=channels or 0)
            return metadata
        finally:
            if process.poll() is None:process.kill();process.wait()
            if raw_audio.exists():raw_audio.unlink()

    def validate(self,path,*,width,height,frames,fps,cancellation):
        stream=self.library.read_frames(str(path),pix_fmt="rgb24",output_params=["-vsync","0"]);count=0
        try:
            meta=next(stream);require(tuple(meta.get("size",()))==(width,height),"video_decode_metadata_mismatch")
            for raw in stream:
                require(not cancellation.is_set(),"task_canceled");require(len(raw)==width*height*3,"video_decode_frame_invalid");count+=1
        finally:stream.close()
        require(count==frames,"video_decode_frame_count_mismatch")
        return {"width":width,"height":height,"frame_count":count,"fps_numerator":fps,"fps_denominator":1,
                "duration_ms":max(1,round(count*1000/fps))}

class ResidentVideoAdapter(Adapter):
    model_key=""
    def __init__(self,*,binding,asset_bindings,outputs,inputs="/mc-inputs",encoder_factory=ManagedFfmpegEncoder):
        self.binding=dict(binding);self.assets=asset_bindings;self.outputs=outputs;self.inputs=inputs
        self.encoder_factory=encoder_factory;self.pipe=self.torch=None;self.dirty=False;self.active_output=None
    def describe_capabilities(self):return worker_capability_for(self.model_key)
    def main_asset(self,binding):
        require(self.pipe is None and binding.get("model_key")==self.model_key,"model_binding_changed")
        require(all(binding.get(k)==self.binding.get(k) for k in ("recipe_revision","model_asset_id","model_asset_revision")),"model_binding_changed")
        main=self.assets.get("main") if type(self.assets) is dict else None
        require(type(main) is dict and main.get("asset_id")==binding.get("model_asset_id") and main.get("revision")==binding.get("model_asset_revision"),"model_asset_changed")
        return asset_path(main)
    def canceled(self,event):require(not event.is_set(),"task_canceled")
    def parameters(self,request):
        defaults={f["key"]:f["default"] for f in self.describe_capabilities()["options"]}
        return {**defaults,**request["payload"]["parameters"]}
    def input_path(self,reference):
        require(type(reference) is dict and set(reference)=={"asset_id","revision","media_type"}
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}",reference["asset_id"])
                and re.fullmatch(r"[0-9a-f]{64}",reference["revision"])
                and reference["media_type"] in INPUT_EXTENSIONS,"input_binding_changed")
        path=checked(Path(self.inputs)/(reference["asset_id"]+INPUT_EXTENSIONS[reference["media_type"]]));sha=hashlib.sha256();size=0
        with regular(path) as stream:
            while block:=stream.read(1024*1024):size+=len(block);require(size<=4*1024**3,"input_content_changed");sha.update(block)
        require(size>0 and sha.hexdigest()==reference["revision"],"input_content_changed");return path
    def publish(self,request,frames,p,progress,cancellation,*,audio=None,audio_sample_rate=None):
        root=checked(self.outputs)
        for component in ("tasks",request["task_id"],request["attempt_id"]):
            require(type(component) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}",component),"output_identity_invalid")
            root=root/component;root.mkdir(mode=0o700,exist_ok=True);checked(root)
        pending=root/"artifact.pending.mp4";self.active_output=pending
        try:
            metadata=self.encoder_factory().encode(frames,pending,width=p["width"],height=p["height"],fps=p["fps"],
                cancellation=cancellation,progress=progress,audio=audio,audio_sample_rate=audio_sample_rate)
            self.canceled(cancellation);final=root/"artifact.mp4";require(not final.exists(),"video_output_exists");os.replace(pending,final)
            with regular(final) as stream:sha=hashlib.sha256(stream.read()).hexdigest();size=os.fstat(stream.fileno()).st_size
            manifest={"asset_id":"art_"+digest([request["task_id"],request["attempt_id"]])[:32],"revision":sha,"sha256":sha}
            descriptor=dict(manifest,schema=1,**{k:request[k] for k in ("task_id","attempt_id","instance_id","worker_epoch")},
                command_message_id=request["message_id"],command_digest=digest(request),byte_size=size,media_type="video/mp4",media_metadata=metadata)
            with (root/"manifest.json").open("xb") as stream:stream.write(canonical(descriptor).encode());stream.flush();os.fsync(stream.fileno())
            return manifest
        finally:
            self.active_output=None
            if pending.exists():pending.unlink()
    def callback(self,progress,cancellation,total):
        def value(_pipe,step,_time,kwargs):self.canceled(cancellation);progress({"phase":"sampling","completed":step+1,"total":total,"unit":"steps"});return kwargs
        return value
    def sampling_callback_kwargs(self,progress,cancellation,total):
        """Pass the Diffusers callback only when the pinned pipeline accepts it.

        The four video pipelines do not share one Python signature.  Keeping the
        compatibility decision at the adapter boundary avoids a runtime
        ``TypeError`` while the supervisor remains the hard-cancellation owner
        for pipelines without step callbacks.
        """
        parameters=inspect.signature(self.pipe.__call__).parameters.values()
        supported=any(value.name=="callback_on_step_end" or value.kind is inspect.Parameter.VAR_KEYWORD for value in parameters)
        return {"callback_on_step_end":self.callback(progress,cancellation,total)} if supported else {}
    def reset_task_state(self):
        require(self.pipe is not None and self.active_output is None,"model_not_loaded")
        if hasattr(self.pipe,"_interrupt"):self.pipe._interrupt=False
        self.torch.cuda.synchronize();self.dirty=False
    def unload(self):
        if self.pipe is not None:
            if self.dirty:self.reset_task_state()
            self.pipe=None;gc.collect();self.torch.cuda.synchronize();self.torch.cuda.empty_cache()
