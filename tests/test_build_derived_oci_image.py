from __future__ import annotations

import hashlib
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.build_derived_oci_image import assemble, canonical, copy_sdk, make_layer, verify_lock


def _parent_oci(path: Path) -> str:
    import json

    layout = path.parent / "parent-layout"
    blobs = layout / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    config = canonical({
        "architecture": "amd64", "os": "linux", "config": {"Env": []},
        "rootfs": {"type": "layers", "diff_ids": []}, "history": [],
    })
    config_digest = hashlib.sha256(config).hexdigest()
    (blobs / config_digest).write_bytes(config)
    manifest = canonical({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                   "digest": "sha256:" + config_digest, "size": len(config)},
        "layers": [],
    })
    manifest_digest = hashlib.sha256(manifest).hexdigest()
    (blobs / manifest_digest).write_bytes(manifest)
    (layout / "oci-layout").write_bytes(canonical({"imageLayoutVersion": "1.0.0"}))
    (layout / "index.json").write_bytes(canonical({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [{
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": "sha256:" + manifest_digest, "size": len(manifest),
            "platform": {"architecture": "amd64", "os": "linux"},
        }],
    }))
    with tarfile.open(path, "w") as archive:
        for name in ("oci-layout", "index.json"):
            archive.add(layout / name, arcname=name)
        for blob in sorted(blobs.iterdir()):
            archive.add(blob, arcname="blobs/sha256/" + blob.name)
    return "sha256:" + config_digest


def _wheel(path: Path, *, name: str = "example-package", version: str = "1.2.3") -> None:
    dist_info = f"{name.replace('-', '_')}-{version}.dist-info"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        )
        archive.writestr(f"{name.replace('-', '_')}/__init__.py", "VALUE = 1\n")


class DerivedOCIBuilderTests(unittest.TestCase):
    def test_verify_lock_accepts_only_the_exact_hashed_wheel_set(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            wheels = root / "wheels"
            wheels.mkdir()
            wheel = wheels / "example_package-1.2.3-py3-none-any.whl"
            _wheel(wheel)
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            lock = root / "requirements.txt"
            lock.write_text(
                f"example-package==1.2.3 --hash=sha256:{digest}\n",
                encoding="utf-8",
            )

            result = verify_lock(lock, wheels)

            self.assertEqual(result[0]["name"], "example-package")
            self.assertEqual(result[0]["version"], "1.2.3")
            self.assertEqual(result[0]["sha256"], digest)
            self.assertEqual(result[0]["filename"], wheel.name)

    def test_verify_lock_rejects_a_digest_or_wheel_set_mismatch(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            wheels = root / "wheels"
            wheels.mkdir()
            _wheel(wheels / "example_package-1.2.3-py3-none-any.whl")
            lock = root / "requirements.txt"
            lock.write_text(
                "example-package==1.2.3 --hash=sha256:" + "0" * 64 + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "wheel identity mismatch"):
                verify_lock(lock, wheels)

            _wheel(wheels / "second_package-1.2.3-py3-none-any.whl", name="second-package")
            with self.assertRaisesRegex(ValueError, "wheel set differs"):
                verify_lock(lock, wheels)

    def test_make_layer_is_reproducible_and_normalizes_tar_metadata(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            stage = root / "stage"
            payload = stage / "opt" / "runtime" / "value.txt"
            payload.parent.mkdir(parents=True)
            payload.write_bytes(b"fixed payload\n")

            first = make_layer(stage, root / "first.tar.gz", "first", uid=1000, gid=1000)
            second = make_layer(stage, root / "second.tar.gz", "second", uid=1000, gid=1000)

            self.assertEqual(first["digest"], second["digest"])
            self.assertEqual(first["diff_id"], second["diff_id"])
            with tarfile.open(first["path"], "r:gz") as archive:
                member = archive.getmember("opt/runtime/value.txt")
                self.assertEqual((member.uid, member.gid, member.mtime), (1000, 1000, 0))
                self.assertEqual(archive.extractfile(member).read(), b"fixed payload\n")

    def test_copy_sdk_excludes_bytecode_and_records_content_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            (source / "worker.py").write_text("VALUE = 1\n", encoding="utf-8")
            cache = source / "__pycache__"
            cache.mkdir()
            (cache / "worker.pyc").write_bytes(b"bytecode")

            result = copy_sdk(source, root / "stage")

            self.assertEqual([item["path"] for item in result], ["mediacenter/worker.py"])
            copied = root / "stage" / "opt" / "mediacenter" / "mediacenter" / "worker.py"
            self.assertEqual(copied.read_text(encoding="utf-8"), "VALUE = 1\n")
            self.assertEqual(result[0]["sha256"], hashlib.sha256(copied.read_bytes()).hexdigest())

    def test_sdk_overlay_requires_the_exact_parent_image_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            parent = root / "parent.oci.tar"
            parent_image_id = _parent_oci(parent)
            stage = root / "stage"
            payload = stage / "opt" / "mediacenter" / "mediacenter" / "worker.py"
            payload.parent.mkdir(parents=True)
            payload.write_text("VALUE = 2\n", encoding="utf-8")
            layer = make_layer(stage, root / "sdk.tar.gz", "sdk", uid=1000, gid=1000)

            with self.assertRaisesRegex(ValueError, "parent OCI image identity mismatch"):
                assemble(parent, root / "bad-layout", [layer], root / "bad.oci.tar",
                         "2026-09-04T00:00:00Z", expected_parent_image_id="sha256:" + "0" * 64)

            result = assemble(parent, root / "good-layout", [layer], root / "good.oci.tar",
                              "2026-09-04T00:00:00Z",
                              expected_parent_image_id=parent_image_id)
            self.assertEqual(result["parent_config_digest"], parent_image_id)
            self.assertTrue((root / "good.oci.tar").is_file())

    def test_sdk_only_overlay_can_reuse_parent_python_without_a_second_environment(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            parent = root / "parent.oci.tar"
            parent_image_id = _parent_oci(parent)
            stage = root / "stage"
            payload = stage / "opt" / "mediacenter" / "mediacenter" / "worker.py"
            payload.parent.mkdir(parents=True)
            payload.write_text("VALUE = 2\n", encoding="utf-8")
            layer = make_layer(stage, root / "sdk.tar.gz", "sdk", uid=1000, gid=1000)

            result = assemble(
                parent, root / "layout", [layer], root / "derived.oci.tar",
                "2026-09-04T00:00:00Z", expected_parent_image_id=parent_image_id,
                python_prefix="/opt/python",
            )

            self.assertEqual(
                result["entrypoint"],
                ["/opt/python/bin/python", "-B", "-u", "-m", "mediacenter.image_worker_cli"],
            )
            self.assertEqual(
                result["environment"][0].split(":", 1)[0],
                "PATH=/opt/python/bin",
            )

    def test_sdk_overlay_rejects_an_unapproved_python_prefix(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            parent = root / "parent.oci.tar"
            parent_image_id = _parent_oci(parent)
            with self.assertRaisesRegex(ValueError, "Python prefix"):
                assemble(
                    parent, root / "layout", [], root / "derived.oci.tar",
                    "2026-09-04T00:00:00Z", expected_parent_image_id=parent_image_id,
                    python_prefix="/task/selected/python",
                )

    def test_sdxl_overlay_preserves_its_fixed_worker_entrypoint(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            parent = root / 'parent.oci.tar'
            parent_id = _parent_oci(parent)
            result = assemble(parent, root / 'layout', [], root / 'derived.oci.tar',
                              '2026-09-05T00:00:00Z', expected_parent_image_id=parent_id,
                              python_prefix='/opt/python', worker_entrypoint='mediacenter.worker_cli')
            self.assertEqual(result['entrypoint'][-1], 'mediacenter.worker_cli')
            with self.assertRaisesRegex(ValueError, 'Worker entrypoint'):
                assemble(parent, root / 'bad-layout', [], root / 'bad.tar',
                         '2026-09-05T00:00:00Z', worker_entrypoint='user.custom_loader')
            self.assertFalse((root / 'bad-layout').exists())

    def test_multimedia_sdk_refresh_preserves_fixed_audio_and_video_entrypoints(self):
        for module in ('mediacenter.audio_worker_cli', 'mediacenter.video_worker_cli'):
            with self.subTest(module=module), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                parent = root / 'parent.oci.tar'
                parent_id = _parent_oci(parent)
                result = assemble(parent, root / 'layout', [], root / 'derived.oci.tar',
                                  '2026-09-06T00:00:00Z', expected_parent_image_id=parent_id,
                                  python_prefix='/opt/python', worker_entrypoint=module)
                self.assertEqual(result['entrypoint'], ['/opt/python/bin/python', '-B', '-u', '-m', module])


if __name__ == "__main__":
    unittest.main()
