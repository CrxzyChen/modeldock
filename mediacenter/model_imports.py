from __future__ import annotations

import hashlib
import json
import math
import struct
from pathlib import Path
from typing import Any


MAX_SAFETENSORS_HEADER_BYTES = 16 * 1024 * 1024
MAX_SAFETENSORS_TENSORS = 500_000
MAX_METADATA_ITEMS = 256
MAX_METADATA_BYTES = 64 * 1024
DETECTOR_VERSION = "mc-safetensors-1"
DTYPE_BYTES = {
    "BOOL": 1, "U8": 1, "I8": 1,
    "U16": 2, "I16": 2, "F16": 2, "BF16": 2,
    "U32": 4, "I32": 4, "F32": 4,
    "U64": 8, "I64": 8, "F64": 8,
    "F8_E4M3": 1, "F8_E5M2": 1,
}


class ModelInspectionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical_digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ModelInspectionError("safetensors_duplicate_key",
                                       "Safetensors header 包含重复 key")
        value[key] = item
    return value


METADATA_KEYS = {
    "modelspec.architecture", "modelspec.resolution", "modelspec.prediction_type",
    "modelspec.title", "modelspec.description", "modelspec.license",
    "modelspec.usage_hint", "ss_network_dim", "ss_network_alpha",
    "ss_new_sd_model_hash", "sshs_model_hash", "ss_sd_model_name",
    "ss_network_module", "ss_output_name", "ss_training_comment",
}


def _metadata(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 4096:
        raise ModelInspectionError("safetensors_metadata_invalid", "Safetensors metadata 无效")
    result: dict[str, str] = {}
    for key, item in value.items():
        if (not isinstance(key, str) or not isinstance(item, str) or not key
                or len(key) > 256 or "\x00" in key or "\x00" in item):
            raise ModelInspectionError("safetensors_metadata_invalid", "Safetensors metadata 无效")
        # Training tools often embed multi-megabyte captions and bucket reports.
        # They are valid Safetensors metadata but are neither needed nor safe to
        # persist in the control plane. Keep only the bounded identity contract.
        if key in METADATA_KEYS and len(item) <= 4096:
            result[key] = item
        if len(result) > MAX_METADATA_ITEMS:
            raise ModelInspectionError("safetensors_metadata_too_large", "Safetensors identity metadata 超过上限")
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_METADATA_BYTES:
        raise ModelInspectionError("safetensors_metadata_too_large", "Safetensors metadata 超过上限")
    return result


def _infer_role(names: list[str], metadata: dict[str, str], role_hint: str | None) -> tuple[str, list[str]]:
    folded_metadata = " ".join(f"{key}={value}" for key, value in metadata.items()).lower()
    lower_names = [name.lower() for name in names]
    looks_lora = ("lora" in folded_metadata or any(
        "lora_" in name or ".lora." in name or name.endswith(".alpha") for name in lower_names))
    looks_checkpoint = any(
        name.startswith(("model.diffusion_model.", "conditioner.embedders.",
                         "unet.", "text_encoder.", "text_encoder_2."))
        for name in lower_names)
    looks_vae = (not looks_checkpoint and any(name.startswith(("encoder.", "decoder.",
                                                                "quant_conv.", "post_quant_conv."))
                                           for name in lower_names))
    inferred = "lora" if looks_lora else "vae" if looks_vae else "checkpoint" if looks_checkpoint else "unknown"
    reasons = [f"role_signature_{inferred}"] if inferred != "unknown" else ["role_signature_unknown"]
    if role_hint and role_hint != inferred and inferred != "unknown":
        raise ModelInspectionError("model_role_mismatch",
                                   f"声明角色 {role_hint} 与检测结果 {inferred} 不一致")
    return (inferred if inferred != "unknown" else role_hint or "unknown"), reasons


def _infer_family(names: list[str], shapes: dict[str, list[int]], metadata: dict[str, str],
                  role: str) -> tuple[str, list[str]]:
    folded_metadata = " ".join(f"{key}={value}" for key, value in metadata.items()).lower()
    lower_names = [name.lower() for name in names]
    if any(marker in folded_metadata for marker in (
            "sdxl", "stable-diffusion-xl", "stable diffusion xl", "pony", "illustrious")):
        return "sdxl", ["metadata_declares_sdxl"]
    if any("conditioner.embedders.1" in name or "text_encoder_2" in name
           or "lora_te2" in name for name in lower_names):
        return "sdxl", ["dual_text_encoder_signature"]
    if role == "lora" and any("lora_unet" in name or name.startswith("unet.")
                              for name in lower_names):
        # Without TE2 or metadata this proves diffusion LoRA shape, not the exact base family.
        return "unknown", ["lora_base_family_not_declared"]
    if role == "vae":
        decoder_in = next((shape for name, shape in shapes.items()
                           if name.lower() == "decoder.conv_in.weight"), None)
        decoder_out = next((shape for name, shape in shapes.items()
                            if name.lower() == "decoder.conv_out.weight"), None)
        if (decoder_in and decoder_out and len(decoder_in) == 4 and len(decoder_out) == 4
                and decoder_in[1] == 4 and decoder_out[0] == 3):
            return "sdxl", ["autoencoder_kl_latent_interface"]
    return "unknown", ["architecture_family_unknown"]


def _derived_metadata(role: str, shapes: dict[str, list[int]], metadata: dict[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = dict(metadata)
    resolution = metadata.get("modelspec.resolution")
    if resolution:
        result["suggested_resolution"] = resolution
    declared_base = metadata.get("ss_new_sd_model_hash")
    if declared_base and len(declared_base) == 64 and all(c in "0123456789abcdefABCDEF" for c in declared_base):
        result["declared_base_identity"] = declared_base.lower()
    if role == "lora":
        ranks = sorted({shape[0] for name, shape in shapes.items()
                        if name.lower().endswith(".lora_down.weight") and len(shape) >= 2})
        if ranks:
            result["lora_rank"] = ranks[0] if len(ranks) == 1 else ranks
        raw_alpha = metadata.get("ss_network_alpha")
        if raw_alpha:
            try:
                alpha = float(raw_alpha)
                result["lora_alpha"] = int(alpha) if alpha.is_integer() else alpha
            except ValueError:
                result["lora_alpha"] = raw_alpha
        result["target_modules"] = sorted({
            "unet" if name.lower().startswith("lora_unet_") else
            "text_encoder_2" if name.lower().startswith("lora_te2_") else
            "text_encoder" if name.lower().startswith("lora_te1_") else "other"
            for name in shapes
        })
    return result


def inspect_safetensors(path: str | Path, *, role_hint: str | None = None) -> dict[str, Any]:
    source = Path(path)
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise ModelInspectionError("model_file_unreadable", "模型文件无法读取") from exc
    if size < 10:
        raise ModelInspectionError("safetensors_too_short", "Safetensors 文件过短")
    with source.open("rb") as handle:
        prefix = handle.read(8)
        if len(prefix) != 8:
            raise ModelInspectionError("safetensors_too_short", "Safetensors 文件过短")
        header_length = struct.unpack("<Q", prefix)[0]
        if header_length <= 1 or header_length > MAX_SAFETENSORS_HEADER_BYTES:
            raise ModelInspectionError("safetensors_header_limit", "Safetensors header 长度无效或超过 16 MiB")
        if header_length > size - 8:
            raise ModelInspectionError("safetensors_header_bounds", "Safetensors header 越界")
        raw_header = handle.read(header_length)
    try:
        header = json.loads(raw_header, object_pairs_hook=_pairs_no_duplicates)
    except ModelInspectionError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ModelInspectionError("safetensors_header_invalid", "Safetensors header 无效") from None
    if not isinstance(header, dict):
        raise ModelInspectionError("safetensors_header_invalid", "Safetensors header 必须是对象")
    metadata = _metadata(header.pop("__metadata__", None))
    if not header or len(header) > MAX_SAFETENSORS_TENSORS:
        raise ModelInspectionError("safetensors_tensor_limit", "Safetensors tensor 数量无效或超过上限")

    data_bytes = size - 8 - header_length
    dtypes: set[str] = set()
    parameter_count = 0
    intervals: list[tuple[int, int, str]] = []
    shapes: dict[str, list[int]] = {}
    for name, spec in header.items():
        if (not isinstance(name, str) or not name or len(name) > 1024 or "\x00" in name
                or not isinstance(spec, dict)
                or set(spec) != {"dtype", "shape", "data_offsets"}):
            raise ModelInspectionError("safetensors_tensor_invalid", "Safetensors tensor 描述无效")
        dtype = spec["dtype"]
        shape = spec["shape"]
        offsets = spec["data_offsets"]
        if dtype not in DTYPE_BYTES:
            raise ModelInspectionError("safetensors_dtype_unsupported", "Safetensors dtype 不受支持")
        if (not isinstance(shape, list) or len(shape) > 16
                or any(not isinstance(dim, int) or isinstance(dim, bool) or dim < 0 for dim in shape)):
            raise ModelInspectionError("safetensors_shape_invalid", "Safetensors tensor shape 无效")
        if (not isinstance(offsets, list) or len(offsets) != 2
                or any(not isinstance(item, int) or isinstance(item, bool) for item in offsets)
                or not 0 <= offsets[0] <= offsets[1] <= data_bytes):
            raise ModelInspectionError("safetensors_offset_bounds", "Safetensors tensor offset 越界")
        count = math.prod(shape)
        if count * DTYPE_BYTES[dtype] != offsets[1] - offsets[0]:
            raise ModelInspectionError("safetensors_tensor_size_mismatch",
                                       "Safetensors tensor shape 与数据长度不一致")
        parameter_count += count
        dtypes.add(dtype)
        intervals.append((offsets[0], offsets[1], name))
        shapes[name] = shape
    intervals.sort()
    for previous, current in zip(intervals, intervals[1:]):
        if current[0] < previous[1]:
            raise ModelInspectionError("safetensors_tensor_overlap", "Safetensors tensor 数据区重叠")

    names = list(header)
    role, role_reasons = _infer_role(names, metadata, role_hint)
    family, family_reasons = _infer_family(names, shapes, metadata, role)
    precision = next(iter(dtypes)).lower() if len(dtypes) == 1 else "mixed"
    persisted_metadata = _derived_metadata(role, shapes, metadata)
    result = {
        "format": "safetensors",
        "suggested_role": role,
        "architecture_family": family,
        "tensor_precision": precision,
        "parameter_summary": {
            "tensor_count": len(header),
            "parameter_count": parameter_count,
            "data_bytes": data_bytes,
            "dtypes": sorted(dtypes),
        },
        "metadata": persisted_metadata,
        "detector_version": DETECTOR_VERSION,
        "reason_codes": role_reasons + family_reasons,
    }
    result["metadata_digest"] = canonical_digest(result)
    return result
