from .video import ResidentVideoAdapter
from .sdxl import require

class LTXAdapter(ResidentVideoAdapter):
    model_key="ltx-2.3-distilled"
    def load(self,binding):
        root=self.main_asset(binding);import torch
        from diffusers import LTX2Pipeline
        require(torch.cuda.is_available(),"cuda_unavailable");self.torch=torch
        self.pipe=LTX2Pipeline.from_pretrained(str(root),dtype=torch.bfloat16,local_files_only=True)
        self.pipe.enable_sequential_cpu_offload(device="cuda:0");self.pipe.vae.enable_tiling();torch.cuda.synchronize()
    def execute(self,request,progress,cancellation):
        self.validate_request(request);require(self.pipe is not None and not self.dirty,"model_not_clean");self.dirty=True;self.canceled(cancellation)
        p=self.parameters(request);generator=self.torch.Generator(device="cuda:0").manual_seed(p["seed"])
        video,audio=self.pipe(prompt=p["prompt"],negative_prompt=p["negative_prompt"],width=p["width"],height=p["height"],
            num_frames=p["num_frames"],frame_rate=float(p["fps"]),num_inference_steps=p["steps"],guidance_scale=p["guidance_scale"],
            stg_scale=p["stg_scale"],modality_scale=p["modality_scale"],audio_guidance_scale=7.0,audio_stg_scale=1.0,
            audio_modality_scale=3.0,guidance_rescale=.7,audio_guidance_rescale=.7,spatio_temporal_guidance_blocks=[28],
            use_cross_timestep=True,generator=generator,output_type="np",return_dict=False,
            **self.sampling_callback_kwargs(progress,cancellation,p["steps"]))
        self.canceled(cancellation);frames=video[0]
        require(len(frames)==p["num_frames"],"video_frame_count_mismatch")
        return self.publish(request,frames,p,progress,cancellation,audio=audio[0],audio_sample_rate=int(self.pipe.vocoder.config.output_sampling_rate))
