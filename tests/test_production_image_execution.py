import importlib.util
import hashlib
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/execute_production_images.py"
MATRIX = ROOT / "deploy/production-images/build-matrix.json"
ASSEMBLER = ROOT / "scripts/assemble_oci_images.py"


def module():
    spec = importlib.util.spec_from_file_location("execute_production_images", SCRIPT)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def assembler():
    spec = importlib.util.spec_from_file_location("assemble_oci_images", ASSEMBLER)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class ProductionImageExecutionTests(unittest.TestCase):
    def test_matrix_covers_six_groups_and_all_fourteen_models(self):
        value = module().load_matrix(MATRIX)
        self.assertEqual(len(value["images"]), 11)
        self.assertEqual(len(value["model_bindings"]), 14)
        self.assertEqual({row["group"] for row in value["images"] if row["group"] != "foundation"},
                         {"sdxl", "image-models", "wan21", "video-models", "musicgen", "cosyvoice"})
        image_ids = {row["id"] for row in value["images"]}
        self.assertLessEqual(set(value["model_bindings"].values()), image_ids)

    def test_matrix_and_base_are_fixed_not_task_selected(self):
        value = json.loads(MATRIX.read_text(encoding="utf-8"))
        self.assertEqual((value["base_repository"], value["base_tag"]), ("ubuntu", "24.04"))
        self.assertIsNotNone(module().BASE.fullmatch("ubuntu@sha256:" + "a" * 64))
        for unsafe in ("ubuntu:24.04", "latest", "evil.example/image@sha256:" + "a" * 64):
            self.assertIsNone(module().BASE.fullmatch(unsafe))

    def test_context_resolution_rejects_escape_and_symlink(self):
        image = module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "safe").mkdir()
            self.assertEqual(image.inside(root, "safe", existing=True), root / "safe")
            with self.assertRaises(ValueError):
                image.inside(root, "../escape", existing=False)
            link = root / "link"
            try:
                link.symlink_to(root / "safe", target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation unavailable")
            with self.assertRaises(ValueError):
                image.inside(root, "link", existing=True)

    def test_dockerfiles_are_offline_and_use_fixed_worker_entrypoints(self):
        directory = ROOT / "deploy/production-images"
        files = {path.name: path.read_text(encoding="utf-8")
                 for path in directory.glob("Dockerfile.*")}
        self.assertEqual(set(files), {
            "Dockerfile.runtime-v0", "Dockerfile.runtime-v1", "Dockerfile.sdxl",
            "Dockerfile.image-worker", "Dockerfile.video-worker",
            "Dockerfile.audio-worker", "Dockerfile.cosyvoice",
        })
        for name, body in files.items():
            self.assertIn("ARG BASE_IMAGE", body, name)
            self.assertIn("FROM ${BASE_IMAGE}", body, name)
            self.assertNotIn("http://", body, name)
            self.assertNotIn("https://", body, name)
            self.assertNotIn("pip install -U", body, name)
            self.assertIn("--no-index", body, name)
            self.assertIn("chmod -R a+rX /opt/mediacenter", body, name)
        self.assertIn("mediacenter.image_worker_cli", files["Dockerfile.image-worker"])
        self.assertIn("mediacenter.video_worker_cli", files["Dockerfile.video-worker"])
        self.assertIn("redis,ftfy,wcwidth", files["Dockerfile.video-worker"])
        self.assertIn("mediacenter.audio_worker_cli", files["Dockerfile.audio-worker"])

    def test_daemonless_assembler_has_fixed_image_profile_mapping(self):
        image = assembler()
        value = json.loads(MATRIX.read_text(encoding="utf-8"))
        self.assertEqual(set(image.PROFILE_BY_IMAGE), {row["id"] for row in value["images"]})
        self.assertEqual(image.PROFILE_BY_IMAGE["cosyvoice2-0.5b"], "runtime-v0")
        self.assertEqual(image.PROFILE_BY_IMAGE["video-models"], "h3")
        self.assertEqual(image.ENTRYPOINT_BY_IMAGE["qwen-image-2512"][-1],
                         "mediacenter.image_worker_cli")

    def test_source_copy_preserves_confined_links_and_requires_declared_external_links(self):
        image = assembler()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source", root / "target"
            (source / "inside").mkdir(parents=True)
            (source / "inside/value.txt").write_text("fixed\n")
            try:
                (source / "internal").symlink_to("inside", target_is_directory=True)
                (source / "external").symlink_to(root / "outside", target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation unavailable")
            with self.assertRaisesRegex(ValueError, "symlink_escape"):
                image.copy_regular_tree(source, target)
            image.copy_regular_tree(
                source, target, ignored_external_symlinks=("external",)
            )
            self.assertTrue((target / "internal").is_symlink())
            self.assertFalse((target / "external").exists())
            if os.name == "posix":
                self.assertEqual(target.stat().st_mode & 0o777, 0o755)
                self.assertEqual((target / "inside").stat().st_mode & 0o777, 0o755)
                self.assertEqual((target / "inside/value.txt").stat().st_mode & 0o777, 0o644)

            second = root / "second"
            with self.assertRaisesRegex(ValueError, "expected_external_symlink_missing"):
                image.copy_regular_tree(
                    source, second,
                    ignored_external_symlinks=("external", "missing"),
                )

    def test_wheel_overlay_is_confined_to_relocated_site_packages(self):
        image = assembler()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contexts, stages, environments = root / "contexts", root / "layers", root / "envs"
            wheel_dir = contexts / "profile/wheels"
            site = environments / "profile/opt/python/lib/python3.11/site-packages"
            wheel_dir.mkdir(parents=True); site.mkdir(parents=True); stages.mkdir()
            (site / "existing.py").write_text("# frozen environment\n")
            (environments / "profile/opt/python/lib/python3.12/site-packages").mkdir(parents=True)
            try:
                (environments / "profile/opt/python/lib/python3.1").symlink_to("python3.11", target_is_directory=True)
            except OSError:
                pass
            with zipfile.ZipFile(wheel_dir / "fixture-1.0-py3-none-any.whl", "w") as archive:
                archive.writestr("fixture/__init__.py", "VALUE = 1\n")
                archive.writestr("fixture-1.0.dist-info/METADATA", "Name: fixture\nVersion: 1.0\n")
            result = image.wheel_stage(contexts, stages, environments, "profile")
            self.assertEqual((result / "opt/python/lib/python3.11/site-packages/fixture/__init__.py").read_text(),
                             "VALUE = 1\n")
            self.assertFalse((result / "fixture").exists())

    def test_image_config_replaces_base_execution_and_appends_diff_ids(self):
        image = assembler()
        base = {"config":{"Volumes":{"/old":{}},"Cmd":["old"]},
                "rootfs":{"type":"layers","diff_ids":["sha256:" + "1" * 64]},
                "history":[]}
        layer = {"name":"fixture","diff_id":"sha256:" + "2" * 64}
        result = image.image_config(base, "fixture", [layer], ["A=B"], ["/worker"], [])
        self.assertEqual(result["rootfs"]["diff_ids"],
                         ["sha256:" + "1" * 64, "sha256:" + "2" * 64])
        self.assertEqual(result["config"]["Entrypoint"], ["/worker"])
        self.assertNotIn("Volumes", result["config"])

    def test_prefetched_base_closure_is_digest_verified(self):
        image = assembler()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = root / "registry"; registry.mkdir()
            diff_id = "sha256:" + "1" * 64
            config_raw = image.canonical({"rootfs": {"diff_ids": [diff_id]}})
            config_digest = "sha256:" + hashlib.sha256(config_raw).hexdigest()
            (registry / config_digest.split(":", 1)[1]).write_bytes(config_raw)
            layer_raw = b"fixed-layer"
            layer_digest = "sha256:" + hashlib.sha256(layer_raw).hexdigest()
            layer_path = registry / layer_digest.split(":", 1)[1]
            layer_path.write_bytes(layer_raw)
            closure = {"schema": "mc.oci-base-closure/1", "manifest_digest": "sha256:" + "2" * 64,
                       "config_digest": config_digest,
                       "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                                   "digest": layer_digest, "size": len(layer_raw)}],
                       "diff_ids": [diff_id]}
            (root / "closure.json").write_bytes(image.canonical(closure))
            self.assertEqual(image.load_prefetched_base(root)["layers"][0]["digest"], layer_digest)
            layer_path.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "layer_changed"):
                image.load_prefetched_base(root)

    def test_reusable_layer_cache_is_fully_digest_verified(self):
        image = assembler()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); layers = root / "layers"; layers.mkdir()
            raw, compressed = b"uncompressed", b"compressed"
            (layers / "fixture.tar").write_bytes(raw)
            (layers / "fixture.tar.zst").write_bytes(compressed)
            value = {
                "name": "fixture",
                "source_digest": "sha256:" + "3" * 64,
                "diff_id": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "uncompressed_bytes": len(raw),
                "digest": "sha256:" + hashlib.sha256(compressed).hexdigest(),
                "bytes": len(compressed),
                "mediaType": "application/vnd.oci.image.layer.v1.tar+zstd",
            }
            (layers / "fixture.json").write_text(json.dumps(value))
            self.assertEqual(image.reuse_layer(
                root, "fixture", expected_source_digest=value["source_digest"])["bytes"],
                len(compressed))
            with self.assertRaisesRegex(ValueError, "reusable_layer_source_changed"):
                image.reuse_layer(root, "fixture", expected_source_digest="sha256:" + "4" * 64)
            (layers / "fixture.tar.zst").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "reusable_layer_changed"):
                image.reuse_layer(root, "fixture", expected_source_digest=value["source_digest"])

    def test_source_tree_digest_changes_with_locked_layer_input(self):
        image = assembler()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "locked").mkdir()
            wheel = root / "locked/ftfy.whl"
            wheel.write_bytes(b"first")
            first = image.source_tree_digest(root)
            wheel.write_bytes(b"second")
            self.assertNotEqual(first, image.source_tree_digest(root))

    def test_legacy_layer_tar_is_bound_to_current_source_tree(self):
        image = assembler()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"; source.mkdir()
            (source / "fixed.txt").write_bytes(b"fixed")
            raw = root / "legacy.tar"
            with tarfile.open(raw, "w", format=tarfile.PAX_FORMAT) as archive:
                archive.add(source, arcname=".")
            expected = image.source_tree_digest(source)
            self.assertEqual(image.tar_source_tree_digest(raw), expected)
            (source / "fixed.txt").write_bytes(b"changed")
            self.assertNotEqual(image.tar_source_tree_digest(raw), image.source_tree_digest(source))

    def test_locked_video_profile_rejects_missing_required_distribution(self):
        image = assembler()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate"
            requirements = candidate / "containers/video-models/requirements.txt"
            requirements.parent.mkdir(parents=True)
            requirements.write_text("ftfy==6.3.1 --hash=sha256:" + "1" * 64 + "\n")
            environment = root / "environment"
            overlay = root / "overlay"
            environment.mkdir(); overlay.mkdir()
            with self.assertRaisesRegex(ValueError, "profile_requirement_missing:ftfy"):
                image.validate_locked_profile_requirements(candidate, environment, overlay, "h3")
            metadata = overlay / "opt/python/lib/python3.11/site-packages/ftfy-6.3.1.dist-info/METADATA"
            metadata.parent.mkdir(parents=True)
            metadata.write_text("Metadata-Version: 2.1\nName: ftfy\nVersion: 6.3.1\n\n")
            image.validate_locked_profile_requirements(candidate, environment, overlay, "h3")
            lower = environment / "opt/python/lib/python3.11/site-packages/ftfy-5.0.dist-info/METADATA"
            lower.parent.mkdir(parents=True)
            lower.write_text("Metadata-Version: 2.1\nName: ftfy\nVersion: 5.0\n\n")
            image.validate_locked_profile_requirements(candidate, environment, overlay, "h3")

    def test_cosyvoice_stage_excludes_only_frozen_external_training_data_link(self):
        image = assembler()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = root / "contexts/runtime-v0/sources"
            embedded = sources / "CosyVoice/third_party/Matcha-TTS"
            separate = sources / "Matcha-TTS"
            stages = root / "stages"
            embedded.mkdir(parents=True); separate.mkdir(); stages.mkdir()
            (embedded / "runtime.py").write_text("VALUE = 1\n")
            (separate / "runtime.py").write_text("VALUE = 1\n")
            try:
                (embedded / "data").symlink_to(root / "foreign-data", target_is_directory=True)
                (separate / "data").symlink_to(root / "foreign-data", target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation unavailable")
            result = image.cosy_stage(root / "contexts", stages)
            copied = result / "opt/cosyvoice/third_party/Matcha-TTS"
            self.assertEqual((copied / "runtime.py").read_text(), "VALUE = 1\n")
            self.assertFalse((copied / "data").exists())


if __name__ == "__main__":
    unittest.main()
