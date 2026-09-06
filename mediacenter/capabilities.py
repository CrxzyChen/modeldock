from __future__ import annotations

import math
import copy
import re
from typing import Any

from .domain import ServiceKind


def _number(key: str, label: str, default: int | float, minimum: int | float,
            maximum: int | float, *, step: int | float = 1, advanced: bool = True,
            integer: bool = True, multiple_of: int | None = None) -> dict[str, Any]:
    field = {"key": key, "label": label, "type": "integer" if integer else "number",
             "default": default, "minimum": minimum, "maximum": maximum,
             "step": step, "advanced": advanced}
    if multiple_of is not None:
        field["multiple_of"] = multiple_of
    return field


SEED = _number("seed", "随机种子", 42, 0, 2**31 - 1, advanced=False)
NEGATIVE = {"key": "negative_prompt", "label": "负面提示词", "type": "string",
            "default": "", "max_length": 4000, "advanced": True,
            "placeholder": "描述不希望出现的内容"}

MODEL_CAPABILITIES: dict[str, dict[str, Any]] = {
    "minimax-h3-ref2va": {
        "prompt_label": "参考驱动描述",
        "prompt_placeholder": "按 <Picture 1>、<Video 1>、<Audio 1> 的顺序描述主体、动作、镜头与声音…",
        "input_contract": {"minimum": 1, "maximum": 12, "accept": ["image/", "video/", "audio/"]},
        "workflow_guide": {
            "steps": ["按生成语义排列参考素材", "在提示词中使用 <Picture 1> / <Video 1> / <Audio 1>", "描述主体如何继承参考外观、动作与声音"],
            "prompt_template": "<Picture 1> 中的主体进入场景，保持外观一致；镜头缓慢推进，环境声自然，并与 <Audio 1> 的节奏同步。",
            "asset_guidance": "引用编号按同类素材分别计数；拖动排序后请核对提示词中的编号。",
            "runtime_guidance": "首次冷启动包含 INT8 量化与组件加载，通常需要较长等待；面板会显示真实阶段，可能约 20–60 分钟，以服务器实测为准。",
        },
        "options": [SEED,
                    _number("width", "宽度", 960, 512, 1344, step=32, advanced=False, multiple_of=32),
                    _number("height", "高度", 544, 320, 768, step=32, advanced=False, multiple_of=32),
                    _number("num_frames", "生成帧数", 124, 124, 362, step=17, advanced=False),
                    _number("steps", "推理步数", 50, 2, 50)],
        "constraints": [{"type":"offset_multiple","key":"num_frames","offset":5,"multiple":17}],
        "notices": ["Ref2VA 需要至少一个参考图像、视频或音频；参考顺序会改变生成语义。",
                    "服务器使用官方 BF16 权重并在两张 48GB GPU 上执行 INT8 权重量化运行。"],
    },
    "ltx-2.3-distilled": {
        "prompt_label": "音视频镜头描述",
        "prompt_placeholder": "描述主体、镜头运动、环境声、对白或音乐…",
        "input_contract": {"minimum": 0, "maximum": 0, "accept": []},
        "workflow_guide": {
            "steps": ["描述单一连续镜头", "分别写清视觉动作与声音", "先用较短帧数试跑，再提高时长"],
            "prompt_template": "中景跟拍一名旅人穿过雨夜街道，霓虹倒影随脚步晃动；远处车流声与轻微雨声同步，电影感写实光线。",
            "asset_guidance": "该部署为纯文本音视频生成，不接收参考素材。",
            "runtime_guidance": "视频与音频在同一次采样中生成，编码阶段会合成为单个 MP4。",
        },
        "options": [SEED, NEGATIVE,
                    _number("width", "宽度", 768, 512, 1280, step=32, advanced=False, multiple_of=32),
                    _number("height", "高度", 512, 320, 768, step=32, advanced=False, multiple_of=32),
                    _number("num_frames", "生成帧数", 121, 17, 241, step=8, advanced=False),
                    _number("fps", "帧率", 24, 8, 30, advanced=False),
                    _number("steps", "推理步数", 30, 2, 50),
                    _number("guidance_scale", "视频 CFG", 3.0, 1.0, 10.0, step=0.5, integer=False),
                    _number("stg_scale", "时空引导", 1.0, 0.0, 5.0, step=0.1, integer=False),
                    _number("modality_scale", "模态引导", 3.0, 1.0, 10.0, step=0.5, integer=False)],
        "constraints": [{"type":"offset_multiple","key":"num_frames","offset":1,"multiple":8}],
        "notices": ["LTX-2.3 同步生成视频与音频；帧数需满足 8n+1。"],
    },
    "wan2.2-i2v-a14b": {
        "prompt_label": "首帧动画描述",
        "prompt_placeholder": "描述参考图像接下来发生的动作、镜头和光线变化…",
        "input_contract": {"minimum": 1, "maximum": 1, "accept": ["image/"]},
        "workflow_guide": {
            "steps": ["上传一张清晰首帧", "只描述首帧之后的运动", "明确镜头运动并避免重述画面静态细节"],
            "prompt_template": "人物从首帧姿态自然抬头，衣角被微风吹动；镜头缓慢向右环绕，背景光线保持连续，动作稳定。",
            "asset_guidance": "只接收一张图片；它会作为视频第一帧和构图约束。",
            "runtime_guidance": "高分辨率与更多帧数会显著增加采样时间和显存压力。",
        },
        "options": [SEED, NEGATIVE,
                    _number("width", "宽度", 1280, 640, 1280, step=16, advanced=False,
                            multiple_of=16),
                    _number("height", "高度", 720, 352, 720, step=16, advanced=False,
                            multiple_of=16),
                    _number("num_frames", "生成帧数", 81, 9, 121, step=4, advanced=False),
                    _number("fps", "帧率", 16, 8, 30, advanced=False),
                    _number("steps", "推理步数", 40, 1, 60),
                    _number("guidance_scale", "提示词引导", 3.5, 0.0, 10.0, step=0.5, integer=False)],
        "constraints": [{"type":"offset_multiple","key":"num_frames","offset":1,"multiple":4}],
        "notices": ["Wan 2.2 I2V-A14B 必须上传一张首帧图像。"],
    },
    "hunyuanvideo-1.5-720p-t2v": {
        "prompt_label": "视频描述",
        "prompt_placeholder": "使用中文或英文描述主体、动作、镜头、环境与光线…",
        "input_contract": {"minimum": 0, "maximum": 0, "accept": []},
        "workflow_guide": {
            "steps": ["先写主体和动作", "再写景别、运镜和光线", "用连续时间描述避免镜头跳切"],
            "prompt_template": "清晨薄雾中的山谷，一只白鹿缓慢穿过溪流；低机位长焦跟拍，水面泛起涟漪，柔和逆光，写实电影质感。",
            "asset_guidance": "该部署为纯文本生成，不接收参考素材。",
            "runtime_guidance": "建议先用 640×352 和较少帧数验证提示词，再提升到 720p。",
        },
        "options": [SEED, NEGATIVE,
                    _number("width", "宽度", 1280, 640, 1280, step=16, advanced=False,
                            multiple_of=16),
                    _number("height", "高度", 720, 352, 720, step=16, advanced=False,
                            multiple_of=16),
                    _number("num_frames", "生成帧数", 61, 17, 121, step=4, advanced=False),
                    _number("fps", "帧率", 15, 8, 30, advanced=False),
                    _number("steps", "推理步数", 30, 1, 50)],
        "constraints": [{"type":"offset_multiple","key":"num_frames","offset":1,"multiple":4}],
        "notices": ["HunyuanVideo 1.5 720p 支持中英文文本生成视频。"],
    },
    "sdxl-base-1.0": {
        "prompt_label": "画面描述",
        "prompt_placeholder": "描述画面主体、环境、光线与风格…",
        "size_presets": [
            {"label": "1:1", "width": 1024, "height": 1024},
            {"label": "3:4", "width": 768, "height": 1024},
            {"label": "4:3", "width": 1024, "height": 768},
            {"label": "9:16", "width": 576, "height": 1024},
            {"label": "16:9", "width": 1024, "height": 576},
        ],
        "options": [SEED, NEGATIVE,
                    _number("width", "宽度", 1024, 512, 1536, step=64, advanced=False),
                    _number("height", "高度", 1024, 512, 1536, step=64, advanced=False),
                    _number("steps", "推理步数", 25, 1, 80),
                    _number("guidance_scale", "提示词引导", 7.0, 0.0, 20.0,
                            step=0.5, integer=False)],
    },
    "z-image-turbo": {
        "prompt_label": "画面描述",
        "prompt_placeholder": "描述要由 Z-Image Turbo 生成的画面…",
        "size_presets": [
            {"label": "1:1", "width": 1024, "height": 1024},
            {"label": "3:4", "width": 768, "height": 1024},
            {"label": "4:3", "width": 1024, "height": 768},
            {"label": "9:16", "width": 576, "height": 1024},
            {"label": "16:9", "width": 1024, "height": 576},
        ],
        "options": [SEED,
                    _number("width", "宽度", 1024, 512, 2048, step=32,
                            advanced=False, multiple_of=32),
                    _number("height", "高度", 1024, 512, 2048, step=32,
                            advanced=False, multiple_of=32),
                    _number("steps", "推理步数", 9, 1, 20),
                    _number("guidance_scale", "提示词引导", 0.0, 0.0, 0.0,
                            step=0.1, integer=False)],
        "constraints": [{"type": "pixel_area", "minimum": 512 * 512,
                         "maximum": 2048 * 2048}],
    },
    "qwen-image-2512": {
        "prompt_label": "画面与文字描述",
        "prompt_placeholder": "使用中文或英文描述主体、构图、材质、光线以及需要准确呈现的文字…",
        "size_presets": [
            {"label": "1:1", "width": 1328, "height": 1328},
            {"label": "16:9", "width": 1664, "height": 928},
            {"label": "9:16", "width": 928, "height": 1664},
            {"label": "4:3", "width": 1472, "height": 1104},
            {"label": "3:4", "width": 1104, "height": 1472},
            {"label": "3:2", "width": 1584, "height": 1056},
            {"label": "2:3", "width": 1056, "height": 1584},
        ],
        "options": [SEED, NEGATIVE,
                    _number("width", "宽度", 1328, 928, 1664, step=16,
                            advanced=False, multiple_of=16),
                    _number("height", "高度", 1328, 928, 1664, step=16,
                            advanced=False, multiple_of=16),
                    _number("steps", "推理步数", 50, 1, 80),
                    _number("true_cfg_scale", "真实 CFG 引导", 4.0, 1.0, 10.0,
                            step=0.5, integer=False)],
        "constraints": [{
            "type": "allowed_sizes",
            "values": [[1328, 1328], [1664, 928], [928, 1664], [1472, 1104],
                       [1104, 1472], [1584, 1056], [1056, 1584]],
        }],
        "notices": [
            "使用 Qwen 官方推荐尺寸；该模型支持中英文提示词和复杂文字排版。",
            "完整权重约 53.7 GiB，服务器驱动使用 CPU offload 控制 GPU 显存占用。",
        ],
    },
    "krea-2-turbo": {
        "prompt_label": "画面描述",
        "prompt_placeholder": "描述主体、构图、材质、光线与真实摄影质感…",
        "size_presets": [
            {"label": "1:1", "width": 1024, "height": 1024},
            {"label": "3:4", "width": 1152, "height": 1536},
            {"label": "4:3", "width": 1536, "height": 1152},
            {"label": "9:16", "width": 1024, "height": 1824},
            {"label": "16:9", "width": 1824, "height": 1024},
            {"label": "2K", "width": 2048, "height": 2048},
        ],
        "options": [SEED,
                    _number("width", "宽度", 1024, 1024, 2048, step=16,
                            advanced=False, multiple_of=16),
                    _number("height", "高度", 1024, 1024, 2048, step=16,
                            advanced=False, multiple_of=16),
                    _number("steps", "推理步数", 8, 1, 12),
                    _number("guidance_scale", "提示词引导", 0.0, 0.0, 0.0,
                            step=0.1, integer=False)],
        "constraints": [{"type": "pixel_area", "minimum": 1024 * 1024,
                         "maximum": 2048 * 2048}],
        "notices": [
            "Turbo 为官方 8 步推理版本；默认 CFG 0，管线内部使用固定 mu 1.15。",
            "模型仓库受限，安装前须接受 Krea 2 Community License。",
        ],
    },
    "illustrious-xl-v2.0": {
        "prompt_label": "插画提示词",
        "prompt_placeholder": "输入自然语言或以逗号分隔的角色、服装、构图、画风标签…",
        "size_presets": [
            {"label": "1:1", "width": 1024, "height": 1024},
            {"label": "3:4", "width": 896, "height": 1152},
            {"label": "4:3", "width": 1152, "height": 896},
            {"label": "9:16", "width": 768, "height": 1344},
            {"label": "16:9", "width": 1344, "height": 768},
        ],
        "options": [SEED, NEGATIVE,
                    _number("width", "宽度", 1024, 512, 1536, step=64,
                            advanced=False, multiple_of=64),
                    _number("height", "高度", 1024, 512, 1536, step=64,
                            advanced=False, multiple_of=64),
                    _number("steps", "推理步数", 28, 1, 80),
                    _number("guidance_scale", "提示词引导", 7.0, 0.0, 20.0,
                            step=0.5, integer=False)],
        "constraints": [{"type": "pixel_area", "minimum": 512 * 512,
                         "maximum": 1536 * 1536}],
        "notices": [
            "官方 v2.0-STABLE 单文件 SDXL checkpoint；适合插画与标签式提示词。",
            "模型采用 CreativeML Open RAIL-M，请遵守其使用限制。",
        ],
    },
    "realesrgan-x2plus": {
        "workflow": "upscale",
        "prompt_label": "任务说明",
        "prompt_placeholder": "AI 超分",
        "input_contract": {"minimum": 1, "maximum": 1, "accept": ["image/"]},
        "options": [],
        "notices": ["RealESRGAN_x2plus 通用 2× 超分；自动 tile，输出最长边不超过 8192 且不超过 64MP。"],
    },
    "realesrgan-x4plus": {
        "workflow": "upscale",
        "prompt_label": "任务说明",
        "prompt_placeholder": "AI 超分",
        "input_contract": {"minimum": 1, "maximum": 1, "accept": ["image/"]},
        "options": [],
        "notices": ["RealESRGAN_x4plus 通用 4× 超分；自动 tile，输出最长边不超过 8192 且不超过 64MP。"],
    },
    "realesrgan-x4plus-anime-6b": {
        "workflow": "upscale",
        "prompt_label": "任务说明",
        "prompt_placeholder": "动漫 AI 超分",
        "input_contract": {"minimum": 1, "maximum": 1, "accept": ["image/"]},
        "options": [],
        "notices": ["RealESRGAN_x4plus_anime_6B 面向动漫插画的 4× 超分；自动 tile，输出最长边不超过 8192 且不超过 64MP。"],
    },
    "wan2.1-t2v-1.3b": {
        "prompt_label": "镜头描述",
        "prompt_placeholder": "描述镜头主体、运动、景别与光线…",
        "options": [SEED, NEGATIVE,
                    _number("width", "宽度", 832, 480, 1280, step=16, advanced=False,
                            multiple_of=16),
                    _number("height", "高度", 480, 320, 720, step=16, advanced=False,
                            multiple_of=16),
                    _number("num_frames", "生成帧数", 33, 9, 81, step=4, advanced=False),
                    _number("fps", "帧率", 16, 8, 30, advanced=False),
                    _number("steps", "推理步数", 20, 1, 50),
                    _number("guidance_scale", "提示词引导", 5.0, 0.0, 15.0,
                            step=0.5, integer=False)],
        "constraints": [{"type": "offset_multiple", "key": "num_frames",
                         "offset": 1, "multiple": 4}],
    },
    "cosyvoice2-0.5b": {
        "prompt_label": "朗读文本",
        "prompt_placeholder": "输入需要合成语音的文本…",
        "options": [_number("speed", "语速", 1.0, 0.5, 2.0, step=0.1,
                            advanced=False, integer=False)],
        "notices": ["当前使用服务器内置的官方零样本参考音色。"],
    },
    "musicgen-small": {
        "prompt_label": "音乐描述",
        "prompt_placeholder": "描述曲风、乐器、情绪和节奏…",
        "options": [SEED,
                    _number("duration_seconds", "时长（秒）", 8.0, 1.0, 30.0,
                            step=1.0, advanced=False, integer=False),
                    _number("guidance_scale", "提示词引导", 3.0, 1.0, 8.0,
                            step=0.5, integer=False),
                    _number("temperature", "采样温度", 1.0, 0.1, 2.0,
                            step=0.1, integer=False)],
        "notices": ["MusicGen Small 使用 CC-BY-NC-4.0，仅限许可证允许的非商业用途。"],
    },
}

# User-imported SDXL checkpoints share the same bounded request surface as the
# built-in SDXL base adapter.  The deployment revision, rather than a task
# field, fixes the actual checkpoint and optional VAE identities.
MODEL_CAPABILITIES["sdxl-single-file"] = copy.deepcopy(
    MODEL_CAPABILITIES["sdxl-base-1.0"])


def capability_for(model_key: str, kind: ServiceKind) -> dict[str, Any]:
    capability = MODEL_CAPABILITIES.get(model_key)
    if capability is None:
        raise ValueError("unknown_model")
    operation = WORKER_MODELS[model_key][0]
    if operation.split(".", 1)[0] != kind.value:
        raise ValueError("model_kind_mismatch")
    return capability


def validate_options(model_key: str, kind: ServiceKind, options: dict[str, Any]) -> None:
    capability = capability_for(model_key, kind)
    fields = {field["key"]: field for field in capability["options"]}
    unknown = sorted(set(options) - set(fields))
    if unknown:
        raise ValueError(f"不支持的模型参数: {', '.join(unknown)}")
    for key, value in options.items():
        field = fields[key]
        field_type = field["type"]
        if field_type == "string":
            if not isinstance(value, str) or len(value) > field.get("max_length", 4000):
                raise ValueError(f"{field['label']}格式无效")
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{field['label']}必须是有效数字")
        if field_type == "integer" and not isinstance(value, int):
            raise ValueError(f"{field['label']}必须是整数")
        if not field["minimum"] <= value <= field["maximum"]:
            raise ValueError(f"{field['label']}必须在 {field['minimum']} 到 {field['maximum']} 之间")
        multiple = field.get("multiple_of")
        if multiple and value % multiple:
            raise ValueError(f"{field['label']}必须是 {multiple} 的倍数")
    for constraint in capability.get("constraints", []):
        if constraint["type"] == "pixel_area":
            width = options.get("width", fields["width"]["default"])
            height = options.get("height", fields["height"]["default"])
            if not constraint["minimum"] <= width * height <= constraint["maximum"]:
                raise ValueError("总像素面积超出模型支持范围")
        elif constraint["type"] == "allowed_sizes":
            width = options.get("width", fields["width"]["default"])
            height = options.get("height", fields["height"]["default"])
            if [width, height] not in constraint["values"]:
                raise ValueError("宽高组合必须使用该模型支持的尺寸预设")


# Worker contracts describe adapter inputs, not a claim of container readiness.
WORKER_MODELS = {
    "sdxl-base-1.0": ("image.generate", "sdxl"),
    "sdxl-single-file": ("image.generate", "sdxl"),
    "illustrious-xl-v2.0": ("image.generate", "sdxl"),
    "z-image-turbo": ("image.generate", "z-image"),
    "qwen-image-2512": ("image.generate", "qwen-image"),
    "krea-2-turbo": ("image.generate", "krea2"),
    "realesrgan-x2plus": ("image.upscale", "realesrgan"),
    "realesrgan-x4plus": ("image.upscale", "realesrgan"),
    "realesrgan-x4plus-anime-6b": ("image.upscale", "realesrgan"),
    "minimax-h3-ref2va": ("video.generate", "h3"),
    "ltx-2.3-distilled": ("video.generate", "ltx"),
    "wan2.2-i2v-a14b": ("video.generate", "wan2.2"),
    "hunyuanvideo-1.5-720p-t2v": ("video.generate", "hunyuanvideo"),
    "wan2.1-t2v-1.3b": ("video.generate", "wan2.1"),
    "cosyvoice2-0.5b": ("speech.generate", "cosyvoice2"),
    "musicgen-small": ("music.generate", "musicgen"),
}
_LORA_MODELS = frozenset({"sdxl-base-1.0", "illustrious-xl-v2.0", "sdxl-single-file"})
_WORKER_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_MEDIA_TYPE = re.compile(r"(?:image|video|audio)/[a-z0-9][a-z0-9.+-]{0,63}\Z")


class WorkerCapabilityError(ValueError):
    """Stable, safe code only; never includes a prompt or supplied field name."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _worker_require(condition: bool, code: str = "invalid_capability") -> None:
    if not condition:
        raise WorkerCapabilityError(code)


def _worker_token(value: Any) -> bool:
    return type(value) is str and _WORKER_TOKEN.fullmatch(value) is not None and ".." not in value


def _worker_finite(value: Any) -> bool:
    return type(value) in (int, float) and abs(value) <= 2**53 - 1 and math.isfinite(value)


def _worker_json(value: Any) -> None:
    """Bound direct capability API calls as strictly as wire request values."""
    stack = [(value, 1)]
    nodes = size = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        _worker_require(nodes <= 16384 and depth <= 16, "invalid_json_value")
        kind = type(item)
        if kind is dict:
            _worker_require(len(item) <= 16384, "invalid_json_value")
            for key, child in item.items():
                _worker_require(type(key) is str, "invalid_json_value")
                stack.extend(((key, depth + 1), (child, depth + 1)))
        elif kind is list:
            _worker_require(len(item) <= 16384, "invalid_json_value")
            stack.extend((child, depth + 1) for child in item)
        elif kind is str:
            try:
                size += len(item.encode("utf-8"))
            except UnicodeError:
                raise WorkerCapabilityError("invalid_json_value") from None
            _worker_require(size <= 256 * 1024, "invalid_json_value")
        elif kind in (int, float):
            _worker_require(_worker_finite(item), "invalid_json_value")
        else:
            _worker_require(kind in (bool, type(None)), "invalid_json_value")


def worker_capability_for(model_key: str) -> dict[str, Any]:
    """Return a detached, explicit worker schema; unknown models fail closed."""
    _worker_require(type(model_key) is str and model_key in WORKER_MODELS, "unknown_model")
    declared = MODEL_CAPABILITIES[model_key]
    operation, family = WORKER_MODELS[model_key]
    reference = declared.get("input_contract", {"minimum": 0, "maximum": 0, "accept": []})
    return {
        "schema": "mc.capability/1", "model_key": model_key,
        "operation": operation, "family": family,
        "options": copy.deepcopy(declared["options"]),
        "constraints": copy.deepcopy(declared.get("constraints", [])),
        "reference_media": {"supported": reference["maximum"] > 0,
                            "minimum": reference["minimum"], "maximum": reference["maximum"],
                            "accept": list(reference["accept"])},
        # Only adapters with a verified task-scoped unload/reset contract are
        # advertised.  This contract is consumed by the API, scheduler and
        # Worker bootstrap; adapter-local capability overrides are forbidden.
        "lora": {"supported": model_key in _LORA_MODELS,
                 "maximum": 1 if model_key in _LORA_MODELS else 0,
                 "minimum_weight": -2.0 if model_key in _LORA_MODELS else 0.0,
                 "maximum_weight": 2.0 if model_key in _LORA_MODELS else 0.0},
    }


def validate_worker_capability(capability: dict[str, Any]) -> None:
    """Validate a trusted adapter declaration, including test/future adapters.

    A declaration is not authorization to load an asset or to execute an adapter.
    Callers must obtain it from their bound recipe, never from request extensions.
    """
    _worker_json(capability)
    _worker_require(type(capability) is dict and set(capability) == {
        "schema", "model_key", "operation", "family", "options", "constraints",
        "reference_media", "lora"})
    _worker_require(capability["schema"] == "mc.capability/1")
    _worker_require(_worker_token(capability["model_key"]) and _worker_token(capability["family"]))
    _worker_require(type(capability["operation"]) is str and capability["operation"] in {
        "image.generate", "image.upscale", "video.generate", "speech.generate", "music.generate"})
    options = capability["options"]
    _worker_require(type(options) is list and len(options) <= 32)
    keys = set()
    for field in options:
        _worker_require(type(field) is dict and {"key", "type", "default"} <= set(field))
        _worker_require(set(field) <= {"key", "type", "default", "label", "minimum", "maximum",
                                      "step", "advanced", "multiple_of", "max_length", "placeholder"})
        _worker_require(_worker_token(field["key"]) and field["key"] not in keys
                        and field["key"] != "prompt")
        keys.add(field["key"])
        _worker_require(type(field["type"]) is str and field["type"] in {"integer", "number", "string"})
        for name in ("label", "placeholder"):
            _worker_require(name not in field or type(field[name]) is str)
        _worker_require("advanced" not in field or type(field["advanced"]) is bool)
        _worker_require("step" not in field or _worker_finite(field["step"]) and field["step"] > 0)
        if field["type"] == "string":
            _worker_require(type(field.get("max_length")) is int and 0 <= field["max_length"] <= 16000)
            _worker_require(type(field["default"]) is str and len(field["default"]) <= field["max_length"])
        else:
            for name in ("minimum", "maximum", "default"):
                _worker_require(_worker_finite(field.get(name)))
            _worker_require(field["minimum"] <= field["default"] <= field["maximum"])
            if field["type"] == "integer":
                _worker_require(all(type(field[name]) is int for name in ("minimum", "maximum", "default")))
            if "multiple_of" in field:
                _worker_require(type(field["multiple_of"]) is int and field["multiple_of"] > 0)
                _worker_require(field["default"] % field["multiple_of"] == 0)
    constraints = capability["constraints"]
    _worker_require(type(constraints) is list and len(constraints) <= 8)
    fields = {field["key"]: field for field in options}
    for constraint in constraints:
        _worker_require(type(constraint) is dict)
        if constraint.get("type") == "offset_multiple":
            _worker_require(set(constraint) == {"type", "key", "offset", "multiple"}
                            and constraint["key"] in keys)
            field = fields[constraint["key"]]
            _worker_require(field["type"] == "integer"
                            and type(constraint["offset"]) is int
                            and type(constraint["multiple"]) is int
                            and constraint["multiple"] > 0
                            and (field["default"] - constraint["offset"]) % constraint["multiple"] == 0)
            continue
        _worker_require({"width", "height"} <= keys)
        _worker_require(all(fields[key]["type"] in {"integer", "number"} for key in ("width", "height")))
        width, height = fields["width"]["default"], fields["height"]["default"]
        if constraint.get("type") == "pixel_area":
            _worker_require(set(constraint) == {"type", "minimum", "maximum"})
            _worker_require(type(constraint["minimum"]) is int and type(constraint["maximum"]) is int
                            and 0 < constraint["minimum"] <= constraint["maximum"] <= 2**53 - 1)
            _worker_require(constraint["minimum"] <= width * height <= constraint["maximum"])
        elif constraint.get("type") == "allowed_sizes":
            _worker_require(set(constraint) == {"type", "values"})
            values = constraint["values"]
            _worker_require(type(values) is list and 1 <= len(values) <= 64)
            _worker_require(all(type(pair) is list and len(pair) == 2
                                and all(type(n) is int and 0 < n <= 65536 for n in pair) for pair in values))
            _worker_require([width, height] in values)
        else:
            raise WorkerCapabilityError("invalid_capability")
    reference = capability["reference_media"]
    _worker_require(type(reference) is dict and set(reference) == {"supported", "minimum", "maximum", "accept"})
    _worker_require(type(reference["supported"]) is bool
                    and type(reference["minimum"]) is int and type(reference["maximum"]) is int
                    and 0 <= reference["minimum"] <= reference["maximum"] <= 16)
    _worker_require(type(reference["accept"]) is list
                    and all(type(item) is str and item in {"image/", "video/", "audio/"} for item in reference["accept"]))
    _worker_require(len(set(reference["accept"])) == len(reference["accept"]))
    _worker_require(reference["supported"] == (reference["maximum"] > 0)
                    and bool(reference["accept"]) == reference["supported"])
    lora = capability["lora"]
    _worker_require(type(lora) is dict and set(lora) == {"supported", "maximum", "minimum_weight", "maximum_weight"})
    _worker_require(type(lora["supported"]) is bool and type(lora["maximum"]) is int
                    and 0 <= lora["maximum"] <= 8 and lora["supported"] == (lora["maximum"] > 0))
    _worker_require(all(_worker_finite(lora[name])
                        for name in ("minimum_weight", "maximum_weight")))
    _worker_require(-10 <= lora["minimum_weight"] <= lora["maximum_weight"] <= 10)
    _worker_require(lora["supported"] or lora["minimum_weight"] == lora["maximum_weight"] == 0)


def validate_worker_request(model_key: str, operation: str, parameters: dict[str, Any],
                            inputs: list[dict[str, Any]], loras: list[dict[str, Any]], *,
                            capability: dict[str, Any] | None = None) -> None:
    """Strict task input validation. Asset provenance/mounts are a later gate."""
    _worker_json([model_key, operation, parameters, inputs, loras])
    contract = worker_capability_for(model_key) if capability is None else capability
    validate_worker_capability(contract)
    _worker_require(model_key == contract["model_key"], "model_mismatch")
    _worker_require(operation == contract["operation"], "unsupported_operation")
    fields = {field["key"]: field for field in contract["options"]}
    _worker_require(type(parameters) is dict and "prompt" in parameters
                    and all(type(key) is str for key in parameters), "invalid_parameters")
    _worker_require(set(parameters) <= set(fields) | {"prompt"}, "unknown_parameter")
    prompt = parameters["prompt"]
    _worker_require(type(prompt) is str and len(prompt) <= 16000
                    and (operation == "image.upscale" or bool(prompt.strip())), "invalid_prompt")
    for key, value in parameters.items():
        if key == "prompt":
            continue
        field = fields[key]
        if field["type"] == "string":
            _worker_require(type(value) is str and len(value) <= field["max_length"], "invalid_parameter")
        else:
            _worker_require(_worker_finite(value), "invalid_parameter")
            _worker_require(field["type"] != "integer" or type(value) is int, "invalid_parameter")
            _worker_require(field["minimum"] <= value <= field["maximum"], "parameter_range")
            _worker_require(not field.get("multiple_of") or value % field["multiple_of"] == 0,
                            "parameter_multiple")
    for constraint in contract["constraints"]:
        if constraint["type"] == "offset_multiple":
            value = parameters.get(constraint["key"], fields[constraint["key"]]["default"])
            _worker_require((value - constraint["offset"]) % constraint["multiple"] == 0,
                            "parameter_combination")
            continue
        width = parameters.get("width", fields["width"]["default"])
        height = parameters.get("height", fields["height"]["default"])
        if constraint["type"] == "pixel_area":
            _worker_require(constraint["minimum"] <= width * height <= constraint["maximum"], "parameter_combination")
        else:
            _worker_require([width, height] in constraint["values"], "parameter_combination")
    reference = contract["reference_media"]
    _worker_require(type(inputs) is list, "invalid_inputs")
    _worker_require(reference["minimum"] <= len(inputs) <= reference["maximum"], "unsupported_inputs")
    for item in inputs:
        _worker_require(type(item) is dict and set(item) == {"asset_id", "revision", "media_type"}, "invalid_input")
        _worker_require(_worker_token(item["asset_id"]) and _worker_token(item["revision"]), "invalid_asset")
        media_type = item["media_type"]
        _worker_require(type(media_type) is str and _MEDIA_TYPE.fullmatch(media_type) is not None
                        and any(media_type.startswith(prefix) for prefix in reference["accept"]), "unsupported_media")
    lora = contract["lora"]
    _worker_require(type(loras) is list, "invalid_loras")
    _worker_require(len(loras) <= lora["maximum"], "unsupported_lora")
    seen = set()
    for item in loras:
        _worker_require(type(item) is dict and set(item) == {"asset_id", "revision", "family", "weight"}, "invalid_lora")
        _worker_require(_worker_token(item["asset_id"]) and _worker_token(item["revision"]), "invalid_asset")
        _worker_require(item["family"] == contract["family"], "lora_family_mismatch")
        _worker_require(_worker_finite(item["weight"])
                        and lora["minimum_weight"] <= item["weight"] <= lora["maximum_weight"], "lora_weight")
        identity = (item["asset_id"], item["revision"])
        _worker_require(identity not in seen, "duplicate_lora")
        seen.add(identity)
