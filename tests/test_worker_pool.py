from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from mediacenter.resource_observer import MemoryState, linux_memory_state


class WorkerPoolRetirementTests(unittest.TestCase):
    def test_only_fail_closed_host_memory_observation_remains(self):
        state = MemoryState(available_bytes=64 * 1024 ** 3, pressure_avg60=0.25)
        self.assertEqual(state.available_bytes, 64 * 1024 ** 3)
        self.assertEqual(state.pressure_avg60, 0.25)

    def test_linux_memory_observer_parses_proc_contract(self):
        meminfo = "MemTotal: 1024 kB\nMemAvailable: 512 kB\n"
        pressure = "some avg10=0.00 avg60=1.25 avg300=0.10 total=1\n"
        with patch.object(Path, "read_text", side_effect=[meminfo, pressure]):
            self.assertEqual(linux_memory_state(), MemoryState(512 * 1024, 1.25))

    def test_linux_memory_observer_fails_closed(self):
        with patch.object(Path, "read_text", side_effect=OSError("unavailable")):
            self.assertEqual(linux_memory_state(), MemoryState(0, float("inf")))


if __name__ == "__main__":
    unittest.main()
