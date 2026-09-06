from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from mediacenter.hardware import HardwareProbe


class HardwareProbeTests(unittest.TestCase):
    def test_snapshot_reports_observed_facts_without_service_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "uptime").write_text("123.45 10.0\n", encoding="utf-8")
            (root / "cpuinfo").write_text(
                "processor : 0\nphysical id : 0\ncore id : 0\nmodel name : Test CPU\n\n"
                "processor : 1\nphysical id : 0\ncore id : 1\nmodel name : Test CPU\n",
                encoding="utf-8",
            )
            (root / "meminfo").write_text(
                "MemTotal:       131072 kB\nMemAvailable:    98304 kB\n", encoding="utf-8")

            def run(args: list[str]) -> subprocess.CompletedProcess[str]:
                if "--query-gpu=" in args[1]:
                    output = "0, GPU-a, Test GPU, 600.1, 2048, 49140, 25, 51, 120.5, 450.0\n"
                else:
                    output = "GPU-a, 1234, /opt/vllm/python, 1024\n"
                return subprocess.CompletedProcess(args, 0, output, "")

            result = HardwareProbe((1,), proc_root=root, storage_paths=(root,),
                                   command_runner=run).snapshot()

        self.assertEqual(result["host"]["uptime_seconds"], 123)
        self.assertEqual(result["cpu"]["physical_cores"], 2)
        self.assertEqual(result["memory"]["used_mib"], 32)
        gpu = result["gpu"]["items"][0]
        self.assertFalse(gpu["configured_for_mediacenter"])
        self.assertEqual(gpu["processes"], [{"pid": 1234, "process_name": "python", "memory_mib": 1024}])
        self.assertNotIn("owner", gpu["processes"][0])
        self.assertNotIn("service", gpu["processes"][0])

    def test_gpu_telemetry_failure_is_explicit(self) -> None:
        def fail(_args: list[str]) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(1, "nvidia-smi")

        result = HardwareProbe((0,), command_runner=fail).snapshot()
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["gpu"]["available"])
        self.assertEqual(result["gpu"]["items"], [])
        self.assertTrue(result["gpu"]["error"])


if __name__ == "__main__":
    unittest.main()
