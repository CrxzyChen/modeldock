from __future__ import annotations

import unittest

from scripts.prepare_production_runtime import _resources


class ProductionRuntimeResourceTests(unittest.TestCase):
    def test_external_reserve_is_bounded_by_target_gpu_memory(self):
        ordinary = _resources(12288, "exclusive", 8192, 49140)
        self.assertEqual(ordinary["external_reserve_mib"], 8192)
        h3 = _resources(40960, "exclusive", 8192, 49140)
        self.assertEqual((h3["base_mib"], h3["task_mib"], h3["external_reserve_mib"]),
                         (36864, 4096, 4096))
        self.assertLessEqual(h3["base_mib"] + h3["task_mib"] +
                             h3["external_reserve_mib"] + 2048, 49140)

    def test_impossible_target_gpu_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "cannot fit"):
            _resources(40960, "exclusive", 8192, 44000)


if __name__ == "__main__":
    unittest.main()
