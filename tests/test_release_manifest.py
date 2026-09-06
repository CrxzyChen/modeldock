from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_release_manifest as release_builder  # noqa: E402
import build_server_release as server_builder  # noqa: E402
import verify_release_bundle as release_verify  # noqa: E402


class ReleaseManifestTests(unittest.TestCase):
    def fixture(self, root: Path) -> None:
        for relative in server_builder.ROOT_FILES + server_builder.DEPLOY_FILES + server_builder.SCRIPT_FILES:
            source = ROOT / relative
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        # The synthetic release has its own version, independent of the checkout.
        (root / 'pyproject.toml').write_text(
            '[project]\nname = "mediacenter-fixture"\nversion = "1.2.7"\n', encoding='utf-8')
        for directory in ("mediacenter", "containers", "deploy/production-images",
                          "electron", "client/src", "client/dist"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        (root / "mediacenter/__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
        (root / "mediacenter/release_version.py").write_text(
            'RELEASE_VERSION = "1.2.7"\n', encoding="utf-8",
        )
        (root / "containers/fixture.json").write_text("{}\n", encoding="utf-8")
        (root / "deploy/production-images/fixture.json").write_text("{}\n", encoding="utf-8")
        (root / "electron/main.js").write_text("// fixture\n", encoding="utf-8")
        (root / "client/src/App.vue").write_text("<template />\n", encoding="utf-8")
        (root / "client/dist/index.html").write_text("<!doctype html>\n", encoding="utf-8")
        package = {
            "version": "1.2.7",
            "build": {"files": release_builder.WINDOWS_PACKAGE_SCOPE,
                      "win": {"target": ["nsis"]}},
            "devDependencies": {"electron": "44.0.0", "electron-builder": "26.15.3"},
        }
        (root / "package.json").write_text(json.dumps(package), encoding="utf-8")
        (root / "package-lock.json").write_text("{}\n", encoding="utf-8")

    def build_fixture(self, root: Path) -> tuple[Path, Path, Path]:
        self.fixture(root)
        linux = root / "dist/MediaCenter-server-1.2.7.tar.gz"
        windows = root / "dist/MediaCenter-Setup-1.2.7.exe"
        manifest = root / "dist/MediaCenter-1.2.7-release.json"
        linux.parent.mkdir()
        windows.write_bytes(b"fixture-windows-installer")
        server_builder.build(root, linux, "1.2.7")
        release_builder.build(root, "1.2.7", windows, linux, manifest,
                              "2026-09-03T19:00:00Z")
        return windows, linux, manifest

    def test_server_bundle_is_deterministic_and_self_verifying(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            output = root / "dist/MediaCenter-server-1.2.7.tar.gz"
            output.parent.mkdir()
            first = server_builder.build(root, output, "1.2.7")
            first_bytes = output.read_bytes()
            second = server_builder.build(root, output, "1.2.7", force=True)
            self.assertEqual(first_bytes, output.read_bytes())
            self.assertEqual(first["sha256"], second["sha256"])
            self.assertEqual(release_verify.inspect_server_bundle(output)["files"], first["files"])

    def test_cross_platform_manifest_detects_source_and_artifact_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            windows, _linux, manifest = self.build_fixture(root)
            result = release_verify.verify_release(root, manifest)
            self.assertEqual(result["status"], "passed")
            source = root / "client/src/App.vue"
            original = source.read_bytes()
            source.write_bytes(original + b"tamper")
            with self.assertRaisesRegex(release_verify.ReleaseValidationError,
                                        "release_source_file_changed"):
                release_verify.verify_release(root, manifest)
            source.write_bytes(original)
            windows.write_bytes(windows.read_bytes() + b"tamper")
            with self.assertRaisesRegex(release_verify.ReleaseValidationError,
                                        "release_artifact_digest_mismatch"):
                release_verify.verify_release(root, manifest)

    def test_paths_records_and_security_claims_fail_closed(self) -> None:
        for value in ("../outside", "/absolute", "a\\b", "a/./b", ""):
            with self.subTest(value=value), self.assertRaises(release_verify.ReleaseValidationError):
                release_verify.safe_relative(value)
        with self.assertRaisesRegex(release_verify.ReleaseValidationError,
                                    "release_payload_private_root_forbidden"):
            release_verify.scan_payload("models/example.json", b"{}")
        with self.assertRaisesRegex(release_verify.ReleaseValidationError,
                                    "release_payload_large_asset_forbidden"):
            release_verify.scan_payload("weights/model.safetensors", b"model")
        with self.assertRaisesRegex(release_verify.ReleaseValidationError,
                                    "release_payload_secret_detected"):
            release_verify.scan_payload("config.env.example", b"HF_TOKEN=secret-value-12345\n")

    def test_manifest_sidecar_and_exact_file_set_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _windows, _linux, manifest = self.build_fixture(root)
            sidecar = manifest.with_name(manifest.name + ".sha256")
            sidecar.write_text("0" * 64 + "\n", encoding="ascii")
            with self.assertRaisesRegex(release_verify.ReleaseValidationError,
                                        "release_manifest_sidecar_mismatch"):
                release_verify.verify_release(root, manifest)


if __name__ == "__main__":
    unittest.main()
