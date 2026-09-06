from __future__ import annotations

from enum import Enum


class ServiceKind(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    SPEECH = "speech"
    MUSIC = "music"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    ASSIGNED = "assigned"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    INTERRUPTED = "interrupted"


TERMINAL_STATUSES = {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELED, TaskStatus.INTERRUPTED}
ACTIVE_TASK_STATUSES = {TaskStatus.QUEUED, TaskStatus.ASSIGNED, TaskStatus.RUNNING, TaskStatus.CANCEL_REQUESTED}

SERVICE_DEFAULTS = {
    ServiceKind.IMAGE: ("图片生成", "SDXL Base 1.0 文生图", 900),
    ServiceKind.VIDEO: ("视频生成", "Wan2.1 T2V-1.3B 文生视频", 3600),
    ServiceKind.SPEECH: ("语音生成", "CosyVoice2-0.5B 文本转语音", 900),
    ServiceKind.MUSIC: ("音乐合成", "MusicGen Small 文本生成音乐", 1800),
}
