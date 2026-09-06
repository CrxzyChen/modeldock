"""Resident CosyVoice2 adapter bound to fixed offline source and reference audio."""
from __future__ import annotations

import gc
import hashlib
import struct
import sys
from pathlib import Path

from ..capabilities import worker_capability_for
from .audio import ResidentAudioAdapter
from .sdxl import asset_path, regular, require


REFERENCE_RELATIVE = "asset/zero_shot_prompt.wav"
REFERENCE_SIZE = 334138
REFERENCE_SHA256 = "c7b31d6dbe7cc6a716dded00550db5b50940bf209e424e4ad207b12e657c8ff6"
REFERENCE_TEXT = "希望你以后能够做的比我还好呦。"
REQUIRED_MODEL_FILES = ("cosyvoice2.yaml", "campplus.onnx", "speech_tokenizer_v2.onnx",
                        "llm.pt", "flow.pt", "hift.pt", "CosyVoice-BlankEN/model.safetensors")


def _fixed_reference(source_root: Path) -> Path:
    root = source_root.resolve(strict=True)
    reference = (root / REFERENCE_RELATIVE).resolve(strict=True)
    require(reference.is_relative_to(root), "cosyvoice_reference_path_invalid")
    sha = hashlib.sha256(); size = 0; raw = bytearray()
    with regular(reference) as stream:
        while block := stream.read(1024 * 1024):
            size += len(block); sha.update(block); raw.extend(block)
    require(size == REFERENCE_SIZE and sha.hexdigest() == REFERENCE_SHA256,
            "cosyvoice_reference_identity_invalid")
    require(len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"
            and struct.unpack_from("<I", raw, 4)[0] + 8 == len(raw),
            "cosyvoice_reference_audio_invalid")
    offset = 12; metadata = None; data_size = None
    while offset < len(raw):
        require(offset + 8 <= len(raw), "cosyvoice_reference_audio_invalid")
        kind, chunk_size = bytes(raw[offset:offset + 4]), struct.unpack_from("<I", raw, offset + 4)[0]
        start, end = offset + 8, offset + 8 + chunk_size
        require(end <= len(raw), "cosyvoice_reference_audio_invalid")
        if kind == b"fmt ":
            require(metadata is None and chunk_size >= 16, "cosyvoice_reference_audio_invalid")
            metadata = struct.unpack_from("<HHIIHH", raw, start)
        elif kind == b"data":
            require(data_size is None, "cosyvoice_reference_audio_invalid"); data_size = chunk_size
        offset = end + (chunk_size & 1)
    require(offset == len(raw) and metadata is not None and data_size is not None,
            "cosyvoice_reference_audio_invalid")
    encoding, channels, rate, byte_rate, block_align, bits = metadata
    require(encoding in (1, 3) and 1 <= channels <= 8 and rate >= 16000
            and bits in ((8,16,24,32) if encoding == 1 else (32,64))
            and block_align == channels * bits // 8 and byte_rate == rate * block_align
            and data_size % block_align == 0, "cosyvoice_reference_audio_invalid")
    frames = data_size // block_align
    require(0 < frames <= rate * 30, "cosyvoice_reference_audio_invalid")
    return reference


class CosyVoiceAdapter(ResidentAudioAdapter):
    model_key = "cosyvoice2-0.5b"

    def __init__(self, *, source_root="/opt/cosyvoice", **kwargs):
        super().__init__(**kwargs)
        self.source_root = Path(source_root)
        self.reference = None
        self.inference_complete = False

    def describe_capabilities(self): return worker_capability_for(self.model_key)

    def load(self, binding):
        require(self.model is None and all(binding.get(key) == self.binding.get(key) for key in
            ("model_key", "recipe_revision", "model_asset_id", "model_asset_revision")),
            "model_binding_changed")
        main = self.assets.get("main") if type(self.assets) is dict else None
        require(type(main) is dict and main.get("asset_id") == binding.get("model_asset_id")
                and main.get("revision") == binding.get("model_asset_revision"), "model_asset_changed")
        model_root = asset_path(main)
        for relative in REQUIRED_MODEL_FILES:
            with regular(model_root / relative): pass
        self.reference = _fixed_reference(self.source_root)
        matcha = (self.source_root / "third_party/Matcha-TTS").resolve(strict=True)
        source = self.source_root.resolve(strict=True)
        require(matcha.is_relative_to(source), "cosyvoice_source_path_invalid")
        for path in (str(matcha), str(source)):
            if path not in sys.path: sys.path.insert(0, path)
        import torch
        from cosyvoice.cli.cosyvoice import CosyVoice2
        require(torch.cuda.is_available(), "cuda_unavailable")
        self.model = CosyVoice2(str(model_root), load_jit=False, load_trt=False, fp16=True)
        self.torch = torch
        require(type(self.model.sample_rate) is int and self.model.sample_rate == 24000,
                "cosyvoice_sample_rate_invalid")
        torch.cuda.synchronize()

    def execute(self, request, progress, cancellation):
        self.validate_request(request)
        require(self.model is not None and not self.dirty and self.reference is not None, "model_not_clean")
        self.dirty = True; self.inference_complete = False; self.canceled(cancellation)
        payload = request["payload"]
        defaults = {field["key"]: field["default"] for field in self.describe_capabilities()["options"]}
        parameters = {**defaults, **payload["parameters"]}
        prompt = parameters["prompt"]
        require(type(prompt) is str and 0 < len(prompt.strip()) <= 4096, "cosyvoice_text_invalid")
        chunks = []
        generator = self.model.inference_zero_shot(
            prompt, REFERENCE_TEXT, str(self.reference), zero_shot_spk_id="",
            stream=True, speed=parameters["speed"], text_frontend=True)
        try:
            for index, chunk in enumerate(generator, 1):
                self.canceled(cancellation)
                require(type(chunk) is dict and "tts_speech" in chunk, "cosyvoice_chunk_invalid")
                chunks.append(chunk["tts_speech"])
                progress({"phase": "synthesizing", "completed": index, "total": index + 1,
                          "unit": "chunks"})
            self.canceled(cancellation); require(chunks, "cosyvoice_empty_audio")
            self.inference_complete = True
        except BaseException:
            # Closing the public generator does not prove its internal LLM thread
            # stopped or its UUID caches were cleared. reset_task_state therefore
            # fails and forces the Worker supervisor to quarantine this domain.
            try: generator.close()
            finally: self.inference_complete = False
            raise
        progress({"phase": "synthesizing", "completed": len(chunks), "total": len(chunks),
                  "unit": "chunks"})
        waveform = self.torch.cat(chunks, dim=1)
        return self.publish(request, waveform, self.model.sample_rate, progress, cancellation)

    def reset_task_state(self):
        require(self.model is not None and self.active_output is None and self.inference_complete,
                "cosyvoice_quiescence_unconfirmed")
        self.torch.cuda.synchronize()
        self.dirty = False; self.inference_complete = False

    def unload(self):
        require(not self.dirty and self.active_output is None, "cosyvoice_quiescence_unconfirmed")
        if self.model is not None:
            self.model = None; self.reference = None; gc.collect()
            self.torch.cuda.synchronize(); self.torch.cuda.empty_cache()
