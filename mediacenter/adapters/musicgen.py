"""Resident MusicGen Small adapter using the fixed Transformers asset."""
from __future__ import annotations

import gc

from ..capabilities import worker_capability_for
from .audio import ResidentAudioAdapter
from .sdxl import asset_path, require


class MusicGenAdapter(ResidentAudioAdapter):
    model_key = "musicgen-small"

    def describe_capabilities(self): return worker_capability_for(self.model_key)

    def load(self, binding):
        require(self.model is None and all(binding.get(key) == self.binding.get(key) for key in
            ("model_key", "recipe_revision", "model_asset_id", "model_asset_revision")), "model_binding_changed")
        main = self.assets.get("main") if type(self.assets) is dict else None
        require(type(main) is dict and main.get("asset_id") == binding.get("model_asset_id")
                and main.get("revision") == binding.get("model_asset_revision"), "model_asset_changed")
        root = asset_path(main)
        import torch
        from transformers import AutoProcessor, MusicgenForConditionalGeneration
        require(torch.cuda.is_available(), "cuda_unavailable")
        self.processor = AutoProcessor.from_pretrained(str(root), local_files_only=True)
        self.model = MusicgenForConditionalGeneration.from_pretrained(
            str(root), torch_dtype=torch.float16, local_files_only=True, use_safetensors=True).to("cuda")
        self.torch = torch; torch.cuda.synchronize()

    def execute(self, request, progress, cancellation):
        self.validate_request(request); require(self.model is not None and not self.dirty, "model_not_clean")
        self.dirty = True; self.canceled(cancellation)
        payload = request["payload"]
        defaults = {field["key"]: field["default"] for field in self.describe_capabilities()["options"]}
        parameters = {**defaults, **payload["parameters"]}
        frame_rate = int(self.model.config.audio_encoder.frame_rate)
        sample_rate = int(self.model.config.audio_encoder.sampling_rate)
        total = min(1503, max(1, int(parameters["duration_seconds"] * frame_rate)))
        values = self.processor(text=[parameters["prompt"]], padding=True, return_tensors="pt").to("cuda")
        owner = self
        class CancelCriteria:
            def __call__(self, _input_ids, _scores, **_kwargs):
                owner.canceled(cancellation)
                completed = min(total, int(getattr(_input_ids, "shape", [0, 0])[-1]))
                progress({"phase": "sampling", "completed": completed, "total": total, "unit": "tokens"})
                return False
        devices = [self.torch.cuda.current_device()]
        with self.torch.random.fork_rng(devices=devices):
            self.torch.manual_seed(parameters["seed"])
            audio = self.model.generate(**values, max_new_tokens=total, do_sample=True,
                guidance_scale=parameters["guidance_scale"], temperature=parameters["temperature"],
                stopping_criteria=[CancelCriteria()])
        self.canceled(cancellation); progress({"phase": "decoding", "completed": 1, "total": 1, "unit": "audio"})
        waveform = audio[0]
        return self.publish(request, waveform, sample_rate, progress, cancellation)

    def reset_task_state(self):
        require(self.model is not None and self.active_output is None, "model_not_loaded")
        self.torch.cuda.synchronize(); self.dirty = False

    def unload(self):
        if self.model is not None:
            if self.dirty: self.reset_task_state()
            self.processor = self.model = None; gc.collect()
            self.torch.cuda.synchronize(); self.torch.cuda.empty_cache()
