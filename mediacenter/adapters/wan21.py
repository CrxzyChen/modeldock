"""Resident Wan 2.1 adapter with cancelable sampling and MP4 publication."""
from __future__ import annotations

import gc
import hashlib
import os
import re
from pathlib import Path

from ..adapter import Adapter
from ..capabilities import worker_capability_for
from ..worker_common import canonical, digest
from .sdxl import asset_path, checked, regular, require


class ImageioFfmpegEncoder:
    """Bounded raw-frame encoder and full decoder check using the locked binary."""
    def __init__(self):
        import imageio_ffmpeg
        self.library = imageio_ffmpeg
        executable = checked(imageio_ffmpeg.get_ffmpeg_exe())
        package = checked(Path(imageio_ffmpeg.__file__).parent)
        require(executable.is_file() and package in executable.parents, "ffmpeg_binary_untrusted")
        self.executable = str(executable)

    @staticmethod
    def _bytes(frame, width, height):
        if hasattr(frame, "convert"):
            frame = frame.convert("RGB")
            require(frame.size == (width, height), "video_frame_size_mismatch")
            return frame.tobytes()
        shape = getattr(frame, "shape", None)
        require(shape == (height, width, 3), "video_frame_size_mismatch")
        return frame.astype("uint8", copy=False).tobytes()

    def encode(self, frames, pending, *, width, height, fps, cancellation, progress):
        require(not pending.exists(), "video_output_exists")
        writer = self.library.write_frames(
            str(pending), (width, height), fps=fps, codec="libx264",
            pix_fmt_in="rgb24", pix_fmt_out="yuv420p", quality=8,
            output_params=["-movflags", "+faststart"], ffmpeg_log_level="error")
        writer.send(None)
        try:
            total = len(frames)
            for index, frame in enumerate(frames):
                require(not cancellation.is_set(), "task_canceled")
                writer.send(self._bytes(frame, width, height))
                progress({"phase": "encoding", "completed": index + 1,
                          "total": total, "unit": "frames"})
        finally:
            writer.close()
        require(not cancellation.is_set(), "task_canceled")
        require(pending.is_file() and pending.stat().st_size > 0, "video_encode_failed")
        reader = self.library.read_frames(str(pending), pix_fmt="rgb24", output_params=["-vsync", "0"])
        decoded = 0
        try:
            metadata = next(reader)
            require(tuple(metadata.get("size", ())) == (width, height), "video_decode_metadata_mismatch")
            for raw in reader:
                require(not cancellation.is_set(), "task_canceled")
                require(len(raw) == width * height * 3, "video_decode_frame_invalid")
                decoded += 1
        finally:
            reader.close()
        require(decoded == len(frames), "video_decode_frame_count_mismatch")
        return {"width": width, "height": height, "frame_count": decoded,
                "fps_numerator": fps, "fps_denominator": 1,
                "duration_ms": max(1, round(decoded * 1000 / fps))}


class Wan21Adapter(Adapter):
    model_key = "wan2.1-t2v-1.3b"

    def __init__(self, *, binding, asset_bindings, outputs, encoder_factory=ImageioFfmpegEncoder):
        self.binding = dict(binding); self.assets = asset_bindings
        self.outputs = outputs; self.encoder_factory = encoder_factory
        self.pipe = self.torch = None; self.dirty = False; self.active_output = None

    def describe_capabilities(self):
        return worker_capability_for(self.model_key)

    def load(self, binding):
        require(self.pipe is None, "model_already_loaded")
        require(all(binding.get(key) == self.binding.get(key) for key in
                    ("model_key", "recipe_revision", "model_asset_id", "model_asset_revision"))
                and binding.get("model_key") == self.model_key, "model_binding_changed")
        main = self.assets.get("main") if type(self.assets) is dict else None
        require(type(main) is dict and main.get("asset_id") == binding.get("model_asset_id")
                and main.get("revision") == binding.get("model_asset_revision"), "model_asset_changed")
        root = asset_path(main)
        import torch
        from diffusers import AutoencoderKLWan, WanPipeline
        require(torch.cuda.is_available(), "cuda_unavailable")
        vae = AutoencoderKLWan.from_pretrained(str(root), subfolder="vae", torch_dtype=torch.float32,
                                               local_files_only=True)
        self.pipe = WanPipeline.from_pretrained(str(root), vae=vae, torch_dtype=torch.bfloat16,
                                                local_files_only=True)
        self.pipe.enable_model_cpu_offload(); self.torch = torch; torch.cuda.synchronize()

    @staticmethod
    def _canceled(cancellation): require(not cancellation.is_set(), "task_canceled")

    def _directory(self, request):
        root = checked(self.outputs)
        for component in ("tasks", request["task_id"], request["attempt_id"]):
            require(type(component) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", component),
                    "output_identity_invalid")
            root = root / component; root.mkdir(mode=0o700, exist_ok=True); checked(root)
        return root

    def execute(self, request, progress, cancellation):
        self.validate_request(request)
        require(self.pipe is not None and not self.dirty, "model_not_clean")
        self.dirty = True; self._canceled(cancellation)
        payload = request["payload"]
        defaults = {field["key"]: field["default"] for field in self.describe_capabilities()["options"]}
        p = {**defaults, **payload["parameters"]}
        generator = self.torch.Generator(device="cuda").manual_seed(p["seed"])
        def callback(_pipe, step, _timestep, values):
            self._canceled(cancellation)
            progress({"phase": "sampling", "completed": step + 1,
                      "total": p["steps"], "unit": "steps"})
            return values
        result = self.pipe(prompt=p["prompt"], negative_prompt=p["negative_prompt"],
            width=p["width"], height=p["height"], num_frames=p["num_frames"],
            num_inference_steps=p["steps"], guidance_scale=p["guidance_scale"],
            generator=generator, callback_on_step_end=callback)
        self._canceled(cancellation)
        frames = result.frames[0]
        require(len(frames) == p["num_frames"], "video_frame_count_mismatch")
        root = self._directory(request); pending = root / "artifact.pending.mp4"
        self.active_output = pending
        try:
            metadata = self.encoder_factory().encode(frames, pending, width=p["width"], height=p["height"],
                                                      fps=p["fps"], cancellation=cancellation,
                                                      progress=progress)
            self._canceled(cancellation)
            final = root / "artifact.mp4"; require(not final.exists(), "video_output_exists")
            os.replace(pending, final)
            with regular(final) as stream:
                sha = hashlib.sha256(stream.read()).hexdigest(); size = os.fstat(stream.fileno()).st_size
            manifest = {"asset_id": "art_" + digest([request["task_id"], request["attempt_id"]])[:32],
                        "revision": sha, "sha256": sha}
            descriptor = dict(manifest, schema=1, **{key: request[key] for key in
                              ("task_id", "attempt_id", "instance_id", "worker_epoch")},
                              command_message_id=request["message_id"], command_digest=digest(request),
                              byte_size=size, media_type="video/mp4", media_metadata=metadata)
            with (root / "manifest.json").open("xb") as stream:
                stream.write(canonical(descriptor).encode()); stream.flush(); os.fsync(stream.fileno())
            return manifest
        finally:
            self.active_output = None
            if pending.exists(): pending.unlink()

    def reset_task_state(self):
        require(self.pipe is not None and self.active_output is None, "model_not_loaded")
        if hasattr(self.pipe, "_interrupt"): self.pipe._interrupt = False
        self.torch.cuda.synchronize(); self.dirty = False

    def unload(self):
        if self.pipe is not None:
            if self.dirty: self.reset_task_state()
            self.pipe = None; gc.collect(); self.torch.cuda.synchronize(); self.torch.cuda.empty_cache()

