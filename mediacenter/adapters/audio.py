"""Shared resident audio publication with fully decoded PCM WAV facts."""
from __future__ import annotations

import hashlib
import os
import re
import wave

from ..adapter import Adapter
from ..worker_common import canonical, digest
from .sdxl import checked, regular, require


class ManagedWavEncoder:
    """Write PCM16 and then read every frame back through the WAV decoder."""

    def encode(self, samples, pending, *, sample_rate, cancellation, progress):
        import numpy as np
        require(type(sample_rate) is int and 8000 <= sample_rate <= 192000,
                "audio_sample_rate_invalid")
        value = samples.detach().float().cpu().numpy() if hasattr(samples, "detach") else np.asarray(samples)
        value = np.asarray(value, dtype=np.float32)
        require(value.ndim in (1, 2) and value.size and np.isfinite(value).all(),
                "audio_samples_invalid")
        if value.ndim == 1:
            value = value[:, None]
        elif value.shape[0] <= 8:
            value = value.T
        require(1 <= value.shape[1] <= 8 and value.shape[0] <= sample_rate * 86400,
                "audio_samples_invalid")
        pcm = np.ascontiguousarray((np.clip(value, -1, 1) * 32767).round().astype("<i2"))
        require(not pending.exists() and not cancellation.is_set(), "task_canceled")
        with pending.open("xb") as raw:
            with wave.open(raw, "wb") as writer:
                writer.setnchannels(pcm.shape[1]); writer.setsampwidth(2); writer.setframerate(sample_rate)
                block = max(1, sample_rate)
                for offset in range(0, pcm.shape[0], block):
                    require(not cancellation.is_set(), "task_canceled")
                    writer.writeframesraw(pcm[offset:offset + block].tobytes())
                    progress({"phase": "encoding", "completed": min(offset + block, pcm.shape[0]),
                              "total": pcm.shape[0], "unit": "samples"})
            raw.flush(); os.fsync(raw.fileno())
        require(not cancellation.is_set(), "task_canceled")
        return self.validate(pending, cancellation)

    @staticmethod
    def validate(path, cancellation):
        decoded = 0
        with wave.open(str(path), "rb") as reader:
            channels = reader.getnchannels(); rate = reader.getframerate()
            width = reader.getsampwidth(); declared = reader.getnframes()
            require(1 <= channels <= 8 and 8000 <= rate <= 192000 and width == 2 and declared > 0,
                    "audio_decode_metadata_invalid")
            while True:
                require(not cancellation.is_set(), "task_canceled")
                raw = reader.readframes(min(rate, declared - decoded))
                if not raw: break
                require(len(raw) % (channels * width) == 0, "audio_decode_frame_invalid")
                decoded += len(raw) // (channels * width)
        require(decoded == declared, "audio_decode_sample_count_mismatch")
        return {"sample_rate": rate, "channels": channels, "sample_count": decoded,
                "duration_ms": max(1, round(decoded * 1000 / rate)), "bits_per_sample": width * 8}


class ResidentAudioAdapter(Adapter):
    model_key = ""

    def __init__(self, *, binding, asset_bindings, outputs, encoder_factory=ManagedWavEncoder):
        self.binding = dict(binding); self.assets = asset_bindings; self.outputs = outputs
        self.encoder_factory = encoder_factory; self.processor = self.model = self.torch = None
        self.dirty = False; self.active_output = None

    @staticmethod
    def canceled(event): require(not event.is_set(), "task_canceled")

    def publish(self, request, samples, sample_rate, progress, cancellation):
        root = checked(self.outputs)
        for component in ("tasks", request["task_id"], request["attempt_id"]):
            require(type(component) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", component),
                    "output_identity_invalid")
            root = root / component; root.mkdir(mode=0o700, exist_ok=True); checked(root)
        pending = root / "artifact.pending.wav"; self.active_output = pending
        try:
            metadata = self.encoder_factory().encode(samples, pending, sample_rate=sample_rate,
                                                     cancellation=cancellation, progress=progress)
            self.canceled(cancellation)
            final = root / "artifact.wav"; require(not final.exists(), "audio_output_exists")
            os.replace(pending, final)
            sha = hashlib.sha256(); size = 0
            with regular(final) as stream:
                while block := stream.read(1024 * 1024): size += len(block); sha.update(block)
            value = sha.hexdigest()
            manifest = {"asset_id": "art_" + digest([request["task_id"], request["attempt_id"]])[:32],
                        "revision": value, "sha256": value}
            descriptor = dict(manifest, schema=1, **{key: request[key] for key in
                ("task_id", "attempt_id", "instance_id", "worker_epoch")},
                command_message_id=request["message_id"], command_digest=digest(request), byte_size=size,
                media_type="audio/wav", media_metadata=metadata)
            with (root / "manifest.json").open("xb") as stream:
                stream.write(canonical(descriptor).encode()); stream.flush(); os.fsync(stream.fileno())
            return manifest
        finally:
            self.active_output = None
            if pending.exists(): pending.unlink()
