from __future__ import annotations
import json,re,unittest
from pathlib import Path

ROOT=Path(__file__).parents[1]

class ContainerSecurityMatrixTests(unittest.TestCase):
    def test_all_model_release_manifests_are_offline_and_digest_bound(self):
        releases=list((ROOT/"containers").glob("*/release.json"))
        self.assertGreaterEqual(len(releases),7)
        for path in releases:
            value=json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(path=path.parent.name):
                if "network" in value:self.assertEqual(value["network"],"none")
                self.assertNotIn("/run/docker.sock",path.read_text(encoding="utf-8"))
                for item in value.get("sdk",[]):
                    target=ROOT/item["path"]
                    import hashlib
                    self.assertEqual(item["sha256"],hashlib.sha256(target.read_bytes()).hexdigest())

    def test_derivatives_run_unprivileged_and_never_mount_engine_socket(self):
        for path in (ROOT/"containers").glob("*/Dockerfile"):
            source=path.read_text(encoding="utf-8")
            with self.subTest(path=path.parent.name):
                self.assertNotIn("docker.sock",source)
                if path.parent.name!="runtime-v1" and path.parent.name!="runtime-v0":
                    self.assertRegex(source,r"(?m)^USER 1000:1000$")

