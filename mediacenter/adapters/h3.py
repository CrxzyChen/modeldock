from .video import ResidentVideoAdapter
from .sdxl import require

class H3Adapter(ResidentVideoAdapter):
    model_key="minimax-h3-ref2va"
    def load(self,binding):
        root=self.main_asset(binding);import torch
        from diffusers import MiniMaxH3Transformer3DModel,ModularPipeline,TorchAoConfig
        from diffusers.hooks import apply_group_offloading
        from diffusers.modular_pipelines.minimax_h3 import MiniMaxH3AudioReference,MiniMaxH3ImageReference,MiniMaxH3VideoReference
        from torchao.quantization import Int8WeightOnlyConfig
        from transformers import Qwen3VLForConditionalGeneration,TorchAoConfig as TTorchAoConfig
        require(torch.cuda.is_available() and torch.cuda.device_count()==2,"cuda_topology_unsupported")
        pipe=ModularPipeline.from_pretrained(root,workflow="ref2va",local_files_only=True)
        pipe.update_components(transformer_ref=MiniMaxH3Transformer3DModel.from_pretrained(root,subfolder="transformer_ref",dtype=torch.bfloat16,local_files_only=True,
            quantization_config=TorchAoConfig(Int8WeightOnlyConfig(),modules_to_not_convert=["proj_in","audio_proj_in","context_embedder","time_embedder","time_proj","token_refiner","norm_out","proj_out","audio_proj_out"]),low_cpu_mem_usage=True),
            text_encoder=Qwen3VLForConditionalGeneration.from_pretrained(root,subfolder="text_encoder",dtype=torch.bfloat16,local_files_only=True,
            quantization_config=TTorchAoConfig(Int8WeightOnlyConfig(),modules_to_not_convert=["model.visual","model.language_model.embed_tokens","model.language_model.norm","lm_head"]),low_cpu_mem_usage=True))
        pipe.load_components(dtype=torch.bfloat16,pretrained_model_name_or_path=str(root),local_files_only=True)
        device=torch.device("cuda:0");offload=dict(onload_device=device,offload_device=torch.device("cpu"),use_stream=False,low_cpu_mem_usage=True)
        pipe.transformer_ref.enable_group_offload(offload_type="block_level",num_blocks_per_group=1,**offload)
        apply_group_offloading(pipe.text_encoder.model,offload_type="leaf_level",**offload);pipe.vae.to(pipe._execution_device);pipe.audio_vae.to(pipe._execution_device)
        self.torch=torch;self.pipe=pipe;self.references={"image/":MiniMaxH3ImageReference,"video/":MiniMaxH3VideoReference,"audio/":MiniMaxH3AudioReference};torch.cuda.synchronize()
    def execute(self,request,progress,cancellation):
        self.validate_request(request);require(self.pipe is not None and not self.dirty,"model_not_clean");self.dirty=True;self.canceled(cancellation)
        references=[]
        for index,item in enumerate(request["payload"]["inputs"]):
            path=self.input_path(item);factory=next((factory for prefix,factory in self.references.items() if item["media_type"].startswith(prefix)),None)
            require(factory is not None,"unsupported_media");references.append(factory.from_file(str(path)));self.canceled(cancellation)
            progress({"phase":"preparing","completed":index+1,"total":len(request["payload"]["inputs"]),"unit":"references"})
        p=self.parameters(request);p["fps"]=24;progress({"phase":"sampling","completed":0,"total":p["steps"],"unit":"steps"})
        result=self.pipe(prompt=p["prompt"],references=references,width=p["width"],height=p["height"],num_frames=p["num_frames"],
            num_inference_steps=p["steps"],generator=self.torch.Generator().manual_seed(p["seed"]),output=["videos","audio","sampling_rate"])
        self.canceled(cancellation);frames=result["videos"][0];require(len(frames)==p["num_frames"],"video_frame_count_mismatch")
        return self.publish(request,frames,p,progress,cancellation,audio=result["audio"][0],audio_sample_rate=int(result["sampling_rate"]))
