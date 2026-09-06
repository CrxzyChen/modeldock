"""Server-side GPU placement policy capabilities.

This module is deliberately outside the immutable Worker SDK capability digest:
it controls which policies the Server may promise, while existing Worker
capabilities continue to describe media inputs and task parameters.
"""
from __future__ import annotations


# Idle/resident are an explicit allow-list, never an optimistic default. Add a
# model only after its adapter can attest that the complete steady-state model
# remains on the assigned GPUs. Every other adapter stays on-demand, including
# implementations that intentionally stream/offload weights through host RAM.
FULL_GPU_RESIDENCY_MODEL_KEYS = frozenset({
    "musicgen-small",
    "krea-2-turbo",
    "sdxl-base-1.0",
    "sdxl-single-file",
    "realesrgan-x2plus",
    "realesrgan-x4plus",
    "realesrgan-x4plus-anime-6b",
    "z-image-turbo",
    "illustrious-xl-v2.0",
})


def residency_modes_for(model_key: str) -> tuple[str, ...]:
    """Return only GPU-residency policies the current adapter can honor."""
    if model_key in FULL_GPU_RESIDENCY_MODEL_KEYS:
        return ("on_demand", "idle", "resident")
    return ("on_demand",)
