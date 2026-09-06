"""Resident Krea 2 Turbo adapter using the pinned local Diffusers snapshot."""
from __future__ import annotations

from .illustrious import ImageAdapterBase
from .sdxl import require


class Krea2Adapter(ImageAdapterBase):
    model_key = "krea-2-turbo"

    def load(self, binding):
        root = self._main_asset(binding)
        residency = binding.get("residency")
        require(residency in {"on_demand", "idle", "resident"},
                "krea2_residency_invalid")
        import torch
        from diffusers import Krea2Pipeline

        require(torch.cuda.is_available(), "cuda_unavailable")
        self.torch = torch
        self.pipe = Krea2Pipeline.from_pretrained(
            str(root),
            torch_dtype=torch.bfloat16,
            local_files_only=True,
            low_cpu_mem_usage=True,
        )
        require(getattr(self.pipe, "is_distilled", False) is True,
                "krea2_turbo_identity_invalid")
        self.pipe.vae.enable_tiling()
        if residency == "on_demand":
            # Keep the low-pressure task path available when the operator does
            # not ask the platform to hold the complete model in VRAM.
            self.pipe.enable_model_cpu_offload(gpu_id=0)
        else:
            # Idle and resident have a strict full-GPU meaning. Never silently
            # fall back to CPU offload: a real allocation failure must surface
            # through the model.load operation and leave the instance unloaded.
            self.pipe.to("cuda")
        torch.cuda.synchronize()

    def execute(self, request, progress, cancellation):
        self.validate_request(request)
        require(self.pipe is not None and not self.dirty, "model_not_clean")
        self.dirty = True
        self._canceled(cancellation)
        payload = request["payload"]
        defaults = {field["key"]: field["default"]
                    for field in self.describe_capabilities()["options"]}
        parameters = {**defaults, **payload["parameters"]}
        generator = self.torch.Generator(device="cuda").manual_seed(parameters["seed"])

        def callback(_pipe, step, _timestep, values):
            self._canceled(cancellation)
            progress({"phase": "generating", "completed": step + 1,
                      "total": parameters["steps"], "unit": "steps"})
            return values

        result = self.pipe(
            prompt=parameters["prompt"],
            width=parameters["width"],
            height=parameters["height"],
            num_inference_steps=parameters["steps"],
            guidance_scale=parameters["guidance_scale"],
            generator=generator,
            callback_on_step_end=callback,
        )
        self._canceled(cancellation)
        require(result.images[0].size == (parameters["width"], parameters["height"]),
                "image_size_mismatch")
        return self._publish(request, result.images[0])
