"""Resident Real-ESRGAN adapter for the three fixed upscaler recipes."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .illustrious import ImageAdapterBase
from .sdxl import checked, regular, require


MODELS = {
    "realesrgan-x2plus": ("RealESRGAN_x2plus.pth", 2, 23),
    "realesrgan-x4plus": ("RealESRGAN_x4plus.pth", 4, 23),
    "realesrgan-x4plus-anime-6b": ("RealESRGAN_x4plus_anime_6B.pth", 4, 6),
}


class RealESRGANAdapter(ImageAdapterBase):
    def __init__(self, *, binding, asset_bindings, outputs, lora_directory="/mc-lora",
                 inputs="/mc-inputs"):
        self.model_key = binding.get("model_key", "")
        require(self.model_key in MODELS, "model_binding_changed")
        super().__init__(binding=binding, asset_bindings=asset_bindings, outputs=outputs,
                         lora_directory=lora_directory, inputs=inputs)

    def load(self, binding):
        root = self._main_asset(binding)
        filename, scale, blocks = MODELS[self.model_key]
        weights = root / filename
        require(weights.is_file(), "realesrgan_weight_missing")
        import torch
        from .realesrgan_core import load_model
        require(torch.cuda.is_available(), "cuda_unavailable")
        self.torch, self.scale, self.blocks = torch, scale, blocks
        self.pipe = load_model(weights, scale, blocks)
        torch.cuda.synchronize()

    def _input_image(self, reference):
        extensions = {"image/png":".png", "image/jpeg":".jpg", "image/webp":".webp"}
        require(type(reference) is dict and set(reference) == {"asset_id","revision","media_type"}
                and type(reference["asset_id"]) is str
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", reference["asset_id"])
                and type(reference["revision"]) is str
                and re.fullmatch(r"[0-9a-f]{64}", reference["revision"])
                and reference["media_type"] in extensions, "input_binding_changed")
        path = checked(Path(self.inputs) / (reference["asset_id"] + extensions[reference["media_type"]]))
        sha = hashlib.sha256(); size = 0
        with regular(path) as stream:
            while block := stream.read(1024 * 1024):
                size += len(block); require(size <= 256 * 1024**2, "input_content_changed")
                sha.update(block)
        require(size > 0 and sha.hexdigest() == reference["revision"], "input_content_changed")
        return path

    def execute(self, request, progress, cancellation):
        self.validate_request(request)
        require(self.pipe is not None and not self.dirty, "model_not_clean")
        self.dirty = True; self._canceled(cancellation)
        from PIL import Image
        from .realesrgan_core import auto_tile, enhance
        reference = request["payload"]["inputs"][0]
        path = self._input_image(reference)
        with Image.open(path) as source:
            source.load()
            width, height = source.size
            target = (width * self.scale, height * self.scale)
            require(max(target) <= 8192 and target[0] * target[1] <= 64_000_000,
                    "upscale_dimensions_exceeded")
            image = source.convert("RGB")
        tile = auto_tile()
        progress({"phase": "upscaling", "completed": 0, "total": 1, "unit": "tiles"})
        self._canceled(cancellation)
        result = enhance(self.pipe, image, self.scale, tile,
                         checkpoint=lambda: self._canceled(cancellation),
                         progress=lambda completed, total: progress({
                             "phase": "upscaling", "completed": completed,
                             "total": total, "unit": "tiles"}))
        self.torch.cuda.synchronize(); self._canceled(cancellation)
        require(result.size == target, "image_size_mismatch")
        return self._publish(request, result)

    def _reset_pipeline(self):
        # RRDBNet has no task-scoped mutable adapters/scheduler state. CUDA
        # synchronization above proves the task is quiescent before reuse.
        return None
