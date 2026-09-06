"""Read-only host memory observation for GPU admission control."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

GIB = 1024 ** 3


@dataclass(frozen=True)
class MemoryState:
    available_bytes: int
    pressure_avg60: float


def linux_memory_state() -> MemoryState:
    try:
        values = {}
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            name, raw = line.split(":", 1)
            values[name] = int(raw.strip().split()[0]) * 1024
        pressure = float("inf")
        for line in Path("/proc/pressure/memory").read_text(encoding="ascii").splitlines():
            if line.startswith("some "):
                pressure = float(dict(
                    item.split("=", 1) for item in line.split()[1:])["avg60"])
                break
        return MemoryState(values["MemAvailable"], pressure)
    except (OSError, KeyError, ValueError):
        return MemoryState(0, float("inf"))
