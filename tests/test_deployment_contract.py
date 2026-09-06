from __future__ import annotations

import unittest
from pathlib import Path


class DeploymentContractTests(unittest.TestCase):
    def test_server_verification_uses_explicit_pool_and_neutral_hardware_apis(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "scripts" / "verify_server.sh").read_text()

        self.assertIn("MEDIACENTER_GPU_POOL", script)
        self.assertIn("/api/v1/hardware", script)
        self.assertIn("/api/v1/resources/gpus", script)
        self.assertIn('resources["configured_gpu_indices"]', script)
        self.assertIn('item["configured_for_mediacenter"]', script)
        self.assertIn('any(model["healthy"] for model in item["models"])', script)

    def test_server_verification_has_no_fixed_gpu_model_or_process_owner_assumption(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "scripts" / "verify_server.sh").read_text()

        forbidden = (
            "GPU1_",
            "GPU 1",
            "len(models)==5",
            "len(models) == 5",
            "z-image-turbo",
            "unexpected external compute process",
            "reserved_gpu_indices",
            "assignable_gpu_indices",
        )
        for value in forbidden:
            with self.subTest(value=value):
                self.assertNotIn(value, script)


if __name__ == "__main__":
    unittest.main()
