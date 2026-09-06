from __future__ import annotations

import csv
import os
import platform
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _number(value: str, *, integer: bool = False) -> int | float | None:
    cleaned = value.strip().removesuffix(" MiB").removesuffix(" W")
    if not cleaned or cleaned.lower() in {"n/a", "[n/a]", "not supported"}:
        return None
    try:
        return int(float(cleaned)) if integer else round(float(cleaned), 2)
    except ValueError:
        return None


class HardwareProbe:
    """Read-only Linux hardware inventory without inferring process ownership."""

    def __init__(self, configured_gpu_indices: Iterable[int], *,
                 proc_root: str | Path = "/proc", storage_paths: Iterable[str | Path] = ("/",),
                 command_runner: CommandRunner | None = None):
        self.configured_gpu_indices = tuple(sorted(set(configured_gpu_indices)))
        self.proc_root = Path(proc_root)
        self.storage_paths = tuple(Path(path) for path in storage_paths)
        self.command_runner = command_runner or self._default_run

    @staticmethod
    def _default_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, capture_output=True, text=True, timeout=10, check=True)

    def snapshot(self) -> dict[str, Any]:
        cpu = self._cpu()
        memory = self._memory()
        storage = self._storage()
        gpu = self.gpu_snapshot()
        available = all(part.get("available", False) for part in (cpu, memory, gpu))
        return {
            "captured_at": _utc_now(),
            "status": "available" if available else "partial",
            "host": self._host(),
            "cpu": cpu,
            "memory": memory,
            "storage": storage,
            "gpu": gpu,
        }

    def _host(self) -> dict[str, Any]:
        uptime = None
        try:
            uptime = int(float((self.proc_root / "uptime").read_text(encoding="utf-8").split()[0]))
        except (OSError, ValueError, IndexError):
            pass
        return {
            "hostname": socket.gethostname(),
            "operating_system": platform.system(),
            "release": platform.release(),
            "architecture": platform.machine(),
            "uptime_seconds": uptime,
        }

    def _cpu(self) -> dict[str, Any]:
        try:
            blocks = (self.proc_root / "cpuinfo").read_text(encoding="utf-8").strip().split("\n\n")
            records = []
            for block in blocks:
                record = {}
                for line in block.splitlines():
                    if ":" in line:
                        key, value = line.split(":", 1)
                        record[key.strip()] = value.strip()
                if record:
                    records.append(record)
            model = next((item.get("model name") or item.get("Hardware")
                          for item in records if item.get("model name") or item.get("Hardware")), None)
            sockets = {item["physical id"] for item in records if "physical id" in item}
            cores = {(item.get("physical id", "0"), item["core id"])
                     for item in records if "core id" in item}
            return {
                "available": True,
                "model": model,
                "logical_processors": len(records) or os.cpu_count(),
                "physical_cores": len(cores) or None,
                "sockets": len(sockets) or None,
                "error": None,
            }
        except OSError as exc:
            return {"available": False, "model": None, "logical_processors": os.cpu_count(),
                    "physical_cores": None, "sockets": None, "error": str(exc)}

    def _memory(self) -> dict[str, Any]:
        try:
            values = {}
            for line in (self.proc_root / "meminfo").read_text(encoding="utf-8").splitlines():
                if ":" not in line:
                    continue
                key, raw = line.split(":", 1)
                values[key] = int(raw.strip().split()[0]) // 1024
            total = values["MemTotal"]
            available = values["MemAvailable"]
            return {"available": True, "total_mib": total, "available_mib": available,
                    "used_mib": max(0, total - available), "error": None}
        except (OSError, ValueError, KeyError, IndexError) as exc:
            return {"available": False, "total_mib": None, "available_mib": None,
                    "used_mib": None, "error": str(exc)}

    def _storage(self) -> dict[str, Any]:
        items = []
        seen_devices: set[int] = set()
        errors = []
        for candidate in self.storage_paths:
            try:
                path = candidate.resolve(strict=True)
                device = path.stat().st_dev
                if device in seen_devices:
                    continue
                seen_devices.add(device)
                usage = shutil.disk_usage(path)
                items.append({
                    "path": str(path),
                    "total_bytes": usage.total,
                    "used_bytes": usage.used,
                    "free_bytes": usage.free,
                })
            except OSError as exc:
                errors.append(f"{candidate}: {exc}")
        return {"available": bool(items), "items": items,
                "error": "; ".join(errors) if errors else None}

    def gpu_snapshot(self, *, timeout_seconds=None) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
        def run(args):
            if deadline is None:
                return self.command_runner(args)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(args, timeout_seconds)
            if self.command_runner == self._default_run:
                return subprocess.run(args, capture_output=True, text=True, timeout=remaining, check=True)
            result = self.command_runner(args)
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(args, timeout_seconds)
            return result
        query = [
            "nvidia-smi",
            "--query-gpu=index,uuid,name,driver_version,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw,power.limit",
            "--format=csv,noheader,nounits",
        ]
        try:
            gpu_result = run(query)
            process_result = run([
                "nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
                "--format=csv,noheader,nounits",
            ])
        except (OSError, subprocess.SubprocessError) as exc:
            return {"available": False, "items": [], "error": str(exc)}

        processes: dict[str, list[dict[str, Any]]] = {}
        for row in csv.reader(process_result.stdout.splitlines()):
            if len(row) != 4:
                continue
            try:
                item = {"pid": int(row[1].strip()), "process_name": Path(row[2].strip()).name,
                        "memory_mib": int(float(row[3].strip()))}
            except ValueError:
                continue
            processes.setdefault(row[0].strip(), []).append(item)

        items = []
        for row in csv.reader(gpu_result.stdout.splitlines()):
            if len(row) != 10:
                continue
            try:
                index = int(row[0].strip())
            except ValueError:
                continue
            uuid = row[1].strip()
            items.append({
                "index": index,
                "uuid": uuid,
                "name": row[2].strip() or None,
                "driver_version": row[3].strip() or None,
                "memory_used_mib": _number(row[4], integer=True),
                "memory_total_mib": _number(row[5], integer=True),
                "utilization_percent": _number(row[6], integer=True),
                "temperature_celsius": _number(row[7], integer=True),
                "power_draw_watts": _number(row[8]),
                "power_limit_watts": _number(row[9]),
                "configured_for_mediacenter": index in self.configured_gpu_indices,
                "processes": sorted(processes.get(uuid, []), key=lambda item: item["pid"]),
            })
        items.sort(key=lambda item: item["index"])
        return {"available": True, "items": items, "error": None}
