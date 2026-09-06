from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts.prepare_sdxl_single_file_runtime import prepare


class PrepareSDXLSingleFileRuntimeTests(unittest.TestCase):
    def fixture(self, root: Path):
        artifact_root = root / "oci"
        artifact_root.mkdir()
        artifact = artifact_root / "sdxl-single-file.oci.tar"
        artifact.write_bytes(b"fixed-oci")
        sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
        evidence = {
            "schema": "mc.derived-oci-build/1", "status": "passed",
            "mode": "sdk-only", "python_prefix": "/opt/python",
            "manifest_digest": "sha256:" + "1" * 64,
            "config_digest": "sha256:" + "2" * 64,
            "archive_sha256": sha, "archive_bytes": artifact.stat().st_size,
            "sdk_digest": "3" * 64,
            "entrypoint": ["/opt/python/bin/python", "-B", "-u", "-m",
                           "mediacenter.image_worker_cli"],
            "command": [], "environment": ["PATH=/opt/python/bin"],
        }
        runtime = {
            "server_id": "fixture", "gpu_uuids": ["GPU-fixture"], "engine": {},
            "installation": {
                "releases": [], "approved_release_digests": [], "templates": {},
                "image_store": str(root / "images"), "download_hosts": [],
                "publisher": {}, "package_root": str(root / "packages"),
                "local_artifact_roots": [str(artifact_root)], "local_artifacts": {},
            },
        }
        return runtime, evidence, artifact

    def test_prepares_approved_release_profile_and_local_artifact_without_model_weight(self):
        with tempfile.TemporaryDirectory() as raw:
            runtime, evidence, artifact = self.fixture(Path(raw))
            candidate, release = prepare(
                runtime, evidence, artifact, release_id="ph8-sdxl-single-file-r1",
                artifact_url="https://unpublished.invalid/ph8/sdxl-single-file.oci.tar",
                profile_revision=1)
            installation = candidate["installation"]
            self.assertEqual(release["adapter_id"], "sdxl-single-file")
            self.assertEqual(installation["runtime_profiles"][0]["image_digest"],
                             "sha256:" + "1" * 64)
            self.assertEqual(len(installation["approved_release_digests"]), 1)
            self.assertEqual(list(installation["local_artifacts"].values()),
                             [str(artifact.resolve())])
            self.assertNotIn("model", str(release["artifact"]))

    def test_rejects_artifact_drift_or_outside_root(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime, evidence, artifact = self.fixture(root)
            artifact.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "identity_changed"):
                prepare(runtime, evidence, artifact, release_id="ph8-sdxl-r1",
                        artifact_url="https://unpublished.invalid/ph8/runtime.oci.tar",
                        profile_revision=1)
            artifact.write_bytes(b"fixed-oci")
            runtime["installation"]["local_artifact_roots"] = [str(root / "other")]
            (root / "other").mkdir()
            with self.assertRaisesRegex(ValueError, "outside_approved_roots"):
                prepare(runtime, evidence, artifact, release_id="ph8-sdxl-r1",
                        artifact_url="https://unpublished.invalid/ph8/runtime.oci.tar",
                        profile_revision=1)


if __name__ == "__main__":
    unittest.main()
