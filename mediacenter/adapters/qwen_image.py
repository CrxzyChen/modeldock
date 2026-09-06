"""Resident Qwen-Image-2512 adapter."""
from __future__ import annotations

from .illustrious import ImageAdapterBase
from .sdxl import require


class QwenImageAdapter(ImageAdapterBase):
    model_key = "qwen-image-2512"

    def load(self, binding):
        root = self._main_asset(binding)
        import torch
        from diffusers import QwenImagePipeline
        require(torch.cuda.is_available(), "cuda_unavailable")
        self.torch = torch
        self.pipe = QwenImagePipeline.from_pretrained(
            str(root), torch_dtype=torch.bfloat16, local_files_only=True,
            low_cpu_mem_usage=True)
        self.pipe.enable_model_cpu_offload(gpu_id=0)
        torch.cuda.synchronize()

    def execute(self, request, progress, cancellation):
        self.validate_request(request)
        require(self.pipe is not None and not self.dirty, "model_not_clean")
        self.dirty = True; self._canceled(cancellation)
        payload = request["payload"]
        defaults = {field["key"]: field["default"] for field in self.describe_capabilities()["options"]}
        parameters = {**defaults, **payload["parameters"]}
        generator = self.torch.Generator(device="cuda").manual_seed(parameters["seed"])
        def callback(_pipe, step, _timestep, values):
            self._canceled(cancellation)
            progress({"phase": "generating", "completed": step + 1,
                      "total": parameters["steps"], "unit": "steps"})
            return values
        result = self.pipe(prompt=parameters["prompt"], negative_prompt=parameters["negative_prompt"],
            width=parameters["width"], height=parameters["height"],
            num_inference_steps=parameters["steps"], true_cfg_scale=parameters["true_cfg_scale"],
            generator=generator, callback_on_step_end=callback)
        self._canceled(cancellation)
        require(result.images[0].size == (parameters["width"], parameters["height"]), "image_size_mismatch")
        return self._publish(request, result.images[0])
