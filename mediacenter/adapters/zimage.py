"""Resident Z-Image Turbo adapter."""
from __future__ import annotations

from .illustrious import ImageAdapterBase
from .sdxl import require


class ZImageAdapter(ImageAdapterBase):
    model_key = "z-image-turbo"

    def load(self, binding):
        root = self._main_asset(binding)
        import torch
        from diffusers import ZImagePipeline
        require(torch.cuda.is_available(), "cuda_unavailable")
        self.torch = torch
        self.pipe = ZImagePipeline.from_pretrained(
            str(root), torch_dtype=torch.bfloat16, local_files_only=True).to("cuda")
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
        result = self.pipe(prompt=parameters["prompt"], width=parameters["width"],
            height=parameters["height"], num_inference_steps=parameters["steps"],
            guidance_scale=parameters["guidance_scale"], generator=generator,
            callback_on_step_end=callback)
        self._canceled(cancellation)
        require(result.images[0].size == (parameters["width"], parameters["height"]), "image_size_mismatch")
        return self._publish(request, result.images[0])
