from .video import ResidentVideoAdapter
from .sdxl import require

class Hunyuan15Adapter(ResidentVideoAdapter):
    model_key="hunyuanvideo-1.5-720p-t2v"
    def load(self,binding):
        root=self.main_asset(binding);import torch
        from diffusers import HunyuanVideo15Pipeline
        require(torch.cuda.is_available(),"cuda_unavailable");self.torch=torch
        self.pipe=HunyuanVideo15Pipeline.from_pretrained(str(root),dtype=torch.bfloat16,local_files_only=True)
        self.pipe.enable_model_cpu_offload(device="cuda:0");self.pipe.vae.enable_tiling();torch.cuda.synchronize()
    def execute(self,request,progress,cancellation):
        self.validate_request(request);require(self.pipe is not None and not self.dirty,"model_not_clean");self.dirty=True;self.canceled(cancellation);p=self.parameters(request)
        result=self.pipe(prompt=p["prompt"],negative_prompt=p["negative_prompt"],width=p["width"],height=p["height"],
            num_frames=p["num_frames"],num_inference_steps=p["steps"],generator=self.torch.Generator(device="cuda:0").manual_seed(p["seed"]),
            **self.sampling_callback_kwargs(progress,cancellation,p["steps"]))
        self.canceled(cancellation);frames=result.frames[0];require(len(frames)==p["num_frames"],"video_frame_count_mismatch")
        return self.publish(request,frames,p,progress,cancellation)
