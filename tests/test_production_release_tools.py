import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def load(name):
    path = ROOT / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


class ProductionReleaseToolTests(unittest.TestCase):
    def test_overlay_import_uses_only_assembly_declared_release_files(self):
        tool = load("import_overlay2_release_matrix")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [{"model_key": f"model-{index}"} for index in range(14)]
            for row in rows:
                (root / (row["model_key"] + ".json")).write_text("{}")
            (root / "preparation-evidence.json").write_text("{}")
            paths = tool.declared_release_paths({"releases": rows}, root)
            self.assertEqual(len(paths), 14)
            self.assertNotIn(root / "preparation-evidence.json", paths)

    def test_video_runtime_probe_imports_locked_text_dependencies(self):
        source = (ROOT / "scripts/verify_overlay2_runtime_execution.py").read_text()
        self.assertIn("import ctypes,ftfy,subprocess,wcwidth", source)

    def test_cosyvoice_prune_is_scoped_to_exact_training_only_deepspeed(self):
        tool = load("assemble_oci_images")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); site = root / "staging/runtime-v0/opt/python/lib/python3.10/site-packages"
            (site / "deepspeed").mkdir(parents=True)
            metadata = site / "deepspeed-0.15.1.dist-info/METADATA"
            metadata.parent.mkdir(); metadata.write_text("Metadata-Version: 2.1\nName: deepspeed\nVersion: 0.15.1\n")
            output = tool.cosy_inference_prune_stage(root / "staging", root / "out")
            overlay = output / "opt/python/lib/python3.10/site-packages"
            self.assertEqual({path.name for path in overlay.iterdir()},
                {".wh.deepspeed", ".wh.deepspeed-0.15.1.dist-info"})

    def test_runtime_v0_packaging_repair_is_digest_locked_and_uses_whiteouts(self):
        tool = load("assemble_oci_images")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = root / "contexts"; staging = root / "staging"
            (context / "runtime-v0/wheels").mkdir(parents=True)
            site = staging / "runtime-v0/opt/python/lib/python3.10/site-packages"
            site.mkdir(parents=True); (site / "existing").write_text("keeps site nonempty")
            wheel = root / tool.PACKAGING_24_WHEEL
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("packaging/__init__.py", "__version__='24.2'\n")
            original = tool.PACKAGING_24_SHA256
            tool.PACKAGING_24_SHA256 = tool.sha_file(wheel)[0]
            try:
                output = tool.wheel_stage(context, root / "out", staging, "runtime-v0",
                    runtime_v0_packaging_wheel=wheel)
            finally:
                tool.PACKAGING_24_SHA256 = original
            overlay = output / "opt/python/lib/python3.10/site-packages"
            self.assertTrue((overlay / ".wh.packaging").is_file())
            self.assertTrue((overlay / ".wh.packaging-24.2.dist-info").is_file())
            self.assertEqual((overlay / "packaging/__init__.py").read_text(), "__version__='24.2'\n")
            if os.name == "posix":
                self.assertEqual((overlay / "packaging").stat().st_mode & 0o777, 0o755)
                self.assertEqual((overlay / "packaging/__init__.py").stat().st_mode & 0o777, 0o644)

    def test_control_runtime_wheels_are_exactly_the_frozen_lock(self):
        tool = load("install_control_runtime")
        lock = (ROOT / "requirements/control-runtime.lock").read_text(encoding="utf-8")
        self.assertEqual(set(tool.WHEELS), {
            "redis-5.3.1-py3-none-any.whl", "pyjwt-2.12.1-py3-none-any.whl",
            "async_timeout-5.0.1-py3-none-any.whl",
            "typing_extensions-4.16.0-py3-none-any.whl",
        })
        for digest in tool.WHEELS.values():
            self.assertEqual(len(digest), 64)
            self.assertIn("sha256:" + digest, lock)

    def test_private_redis_material_is_exclusive_private_and_never_exposes_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "private-redis"
            result = subprocess.run([
                sys.executable, "-B", str(ROOT / "scripts/prepare_private_redis.py"),
                "--root", str(root), "--redis-template", str(ROOT / "deploy/redis.conf")],
                check=True, capture_output=True, text=True)
            evidence = json.loads(result.stdout)
            self.assertFalse((root / "secrets/manager.secret").read_bytes().endswith(b"\n"))
            self.assertFalse((root / "secrets/seed.secret").read_bytes().endswith(b"\n"))
            manager = (root / "secrets/manager.secret").read_text().strip()
            seed = (root / "secrets/seed.secret").read_text().strip()
            serialized = json.dumps(evidence)
            self.assertNotIn(manager, serialized); self.assertNotIn(seed, serialized)
            acl_path = root / "data/mediacenter-redis.acl"
            acl = acl_path.read_text()
            if os.name == "posix":
                self.assertEqual(acl_path.stat().st_mode & 0o777, 0o600)
            self.assertIn("user default off", acl)
            self.assertIn("+acl|getuser +acl|setuser +acl|save", acl)
            self.assertNotIn(manager, acl)
            config = (root / "redis.conf").read_text()
            self.assertIn("aclfile /var/lib/mediacenter-redis/mediacenter-redis.acl", config)
            self.assertNotIn("aclfile /run/secrets/", config)
            with self.assertRaises(subprocess.CalledProcessError):
                subprocess.run([
                    sys.executable, "-B", str(ROOT / "scripts/prepare_private_redis.py"),
                    "--root", str(root), "--redis-template", str(ROOT / "deploy/redis.conf")],
                    check=True, capture_output=True, text=True)

    def test_oci_verifier_confines_every_declared_path(self):
        tool = load("verify_production_oci")
        contract = tool.image_contract({"id": "fixture", "manifest_digest": "sha256:" + "a" * 64,
            "config_digest": "sha256:" + "b" * 64, "entrypoint": ["worker"],
            "command": [], "environment": ["PATH=/opt/python/bin"]})
        self.assertEqual(contract["reference"], "mediacenter.local/fixture@sha256:" + "a" * 64)
        self.assertEqual(contract["image_id"], "sha256:" + "b" * 64)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); (root / "inside").write_text("ok")
            self.assertEqual(tool.inside(root, str(root / "inside")), root / "inside")
            with self.assertRaisesRegex(ValueError, "outside_root"):
                tool.inside(root, str(Path(__file__).resolve()))

    def test_overlay_release_accepts_formal_config_digest_reference(self):
        tool = load("prepare_overlay2_releases")
        image = {"id": "fixture", "manifest_digest": "sha256:" + "a" * 64,
                 "config_digest": "sha256:" + "b" * 64}
        resolved = tool.resolve_image({
            "image_id": image["config_digest"],
            "reference": "mediacenter.local/fixture@" + image["manifest_digest"],
        }, [image])
        self.assertIs(resolved, image)
        with self.assertRaisesRegex(ValueError, "image mismatch"):
            tool.resolve_image({"image_id": image["config_digest"],
                                "reference": "sha256:" + "c" * 64}, [image])


if __name__ == "__main__":
    unittest.main()
