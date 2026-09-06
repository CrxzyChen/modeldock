from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import install_server_release as installer  # noqa: E402
import adopt_legacy_server as legacy_adoption  # noqa: E402
import verify_release_bundle as release_verify  # noqa: E402
import build_server_release as server_builder  # noqa: E402


def make_bundle(root: Path, version: str = "1.1.0", *, extra_member: bool = False) -> Path:
    bundle_root = f"MediaCenter-server-{version}"
    files = {
        "deploy/mediacenter.env.example": (ROOT / "deploy/mediacenter.env.example").read_bytes(),
        "deploy/mediacenter.service": (ROOT / "deploy/mediacenter.service").read_bytes(),
        "deploy/mediacenter-redis.service": (ROOT / "deploy/mediacenter-redis.service").read_bytes(),
        "deploy/model_catalog.json": b'{"models":[]}\n',
        "mediacenter/__init__.py": f'__version__ = "{version}"\n'.encode(),
    }
    records = [{"path": name, "bytes": len(body),
                "sha256": release_verify.digest_bytes(body), "mode": 420}
               for name, body in sorted(files.items())]
    manifest = {
        "schema": "mc.server-bundle/1", "product": "MediaCenter", "version": version,
        "root": bundle_root, "files": records,
        "security": {"credentials_embedded": False, "model_weights_embedded": False,
                     "database_embedded": False, "media_embedded": False},
    }
    path = root / f"MediaCenter-server-{version}.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        payloads = {"release/server-manifest.json": release_verify.canonical(manifest), **files}
        if extra_member:
            payloads["undeclared.txt"] = b"extra\n"
        for relative, body in payloads.items():
            info = tarfile.TarInfo(f"{bundle_root}/{relative}")
            info.size = len(body)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(body))
    return path


class ServerReleaseTests(unittest.TestCase):
    def test_build_rejects_stale_health_version_without_importing_product(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'mediacenter').mkdir()
            source = root / 'mediacenter/release_version.py'
            source.write_text('RELEASE_VERSION = "1.2.22"\nraise RuntimeError("must not execute")\n')
            with self.assertRaisesRegex(release_verify.ReleaseValidationError, 'server_health_version_mismatch'):
                server_builder.verify_health_version(root, '1.2.24')
            server_builder.verify_health_version(root, '1.2.22')

    def test_bundle_carries_the_offline_oci_converter_used_by_the_installer(self) -> None:
        self.assertIn("scripts/convert_oci_to_docker_archive.py", server_builder.SCRIPT_FILES)
        self.assertIn("scripts/migrate_task_kernel.py", server_builder.SCRIPT_FILES)

    def test_control_python_path_is_made_absolute_without_symlink_resolution(self) -> None:
        lexical = installer.lexical_absolute(Path("runtime/bin/python"))
        self.assertTrue(lexical.is_absolute())
        self.assertEqual(lexical.name, "python")
        self.assertEqual(lexical.parent.name, "bin")
        self.assertEqual(installer.lexical_absolute(Path("/opt/mc/bin/python")).as_posix(),
                         "/opt/mc/bin/python")

    def test_legacy_adoption_payload_is_clean_and_rollback_capable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "data/releases/legacy"
            (legacy / "mediacenter/__pycache__").mkdir(parents=True)
            (legacy / "deploy").mkdir()
            (legacy / "mediacenter/server.py").write_text("VALUE = 1\n", encoding="utf-8")
            (legacy / "mediacenter/__pycache__/server.pyc").write_bytes(b"cache")
            (legacy / "deploy/model_catalog.json").write_text('{"models":[]}\n', encoding="utf-8")
            for relative in legacy_adoption.LEGACY_RUNTIME_DEPLOY_FILES:
                path = legacy / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8")
            service = root / "mediacenter.service"
            service.write_text("@MEDIACENTER_DATA_ROOT@ @MEDIACENTER_CONTROL_PYTHON@\n",
                               encoding="utf-8")
            environment = root / "mediacenter.env.example"
            environment.write_text("MEDIACENTER_DATA_ROOT=@MEDIACENTER_DATA_ROOT@\n",
                                   encoding="utf-8")
            records, payload = legacy_adoption.legacy_payload(
                legacy, legacy / "deploy/model_catalog.json", service, environment,
            )
            paths = {item["path"] for item in records}
            self.assertIn("mediacenter/server.py", paths)
            self.assertIn("deploy/model_catalog.json", paths)
            self.assertIn("deploy/mediacenter.service", paths)
            self.assertIn("deploy/mediacenter.env.example", paths)
            self.assertNotIn("mediacenter/__pycache__/server.pyc", paths)
            self.assertEqual(payload["deploy/mediacenter.service"], service.read_bytes())

    def test_stable_environment_rebinds_only_model_catalog(self) -> None:
        lines = [
            "MEDIACENTER_DATA_ROOT=/srv/mediacenter",
            "MEDIACENTER_MODEL_CATALOG=/srv/mediacenter/releases/legacy/deploy/model_catalog.json",
            "MEDIACENTER_GPU_POOL=0,1",
        ]
        rendered = legacy_adoption.render_stable_environment(
            lines, Path("/srv/mediacenter"),
        ).decode("utf-8")
        self.assertIn("MEDIACENTER_MODEL_CATALOG=/srv/mediacenter/current/deploy/model_catalog.json",
                      rendered)
        self.assertIn("MEDIACENTER_GPU_POOL=0,1", rendered)

    def test_adoption_manifest_includes_formal_service_support(self) -> None:
        records = [{"path": "mediacenter/server.py", "bytes": 1,
                    "sha256": "0" * 64, "mode": 420}]
        manifest = legacy_adoption.adoption_manifest("1.0.0", records)
        self.assertEqual(manifest["version"], "1.0.0")
        self.assertEqual(manifest["root"], "MediaCenter-server-1.0.0")
        self.assertEqual(manifest["files"], records)
        self.assertFalse(manifest["security"]["credentials_embedded"])

    def test_preflight_is_read_only_and_reports_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = make_bundle(root)
            data_root = root / "data"
            result = installer.preflight(bundle, data_root, Path("/usr/bin/python3"))
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["mutations_performed"], [])
            self.assertFalse(data_root.exists())

    def test_environment_and_service_templates_have_no_release_specific_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = installer.render_environment(
                ROOT / "deploy/mediacenter.env.example", root, "0,2",
                "GPU-one,GPU-two", "10.0.0.8", 8787,
            ).decode()
            self.assertIn("MEDIACENTER_GPU_POOL=0,2", env)
            self.assertIn("MEDIACENTER_HOST=10.0.0.8", env)
            self.assertIn(f"MEDIACENTER_MODEL_CATALOG={root.as_posix()}/current/deploy/model_catalog.json", env)
            self.assertNotIn("@MEDIACENTER_", env)
            service = installer.render_service(
                ROOT / "deploy/mediacenter.service", root, Path("/opt/mediacenter/python"),
            ).decode()
            self.assertIn(f"WorkingDirectory={root.as_posix()}/current", service)
            self.assertNotIn("refactor-candidate", service)
            self.assertNotIn("@MEDIACENTER_", service)

    def test_runtime_layout_preserves_existing_private_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = make_bundle(root)
            inspected = release_verify.inspect_server_bundle(bundle)
            release, created = installer.extract_release(bundle, root / "data", inspected)
            self.assertTrue(created)
            service_dir = root / "systemd"
            first = installer.ensure_runtime_layout(
                root / "data", release, service_dir, Path("/opt/mediacenter/python"),
                gpu_pool="0", gpu_uuids="GPU-one", host="127.0.0.1", port=8787,
            )
            key = root / "data/config/api-key"
            env = root / "data/config/mediacenter.env"
            key.write_text("kept-key\n", encoding="ascii")
            preserved_environment = (
                f"MEDIACENTER_API_KEY_FILE={key.as_posix()}\nKEPT=1\n"
            )
            env.write_text(preserved_environment, encoding="ascii")
            second = installer.ensure_runtime_layout(
                root / "data", release, service_dir, Path("/opt/mediacenter/python"),
                gpu_pool="1", gpu_uuids="GPU-two", host="127.0.0.1", port=9999,
            )
            self.assertTrue(first["api_key_created"])
            self.assertTrue(first["environment_created"])
            self.assertFalse(second["api_key_created"])
            self.assertFalse(second["environment_created"])
            self.assertEqual(key.read_text(encoding="ascii"), "kept-key\n")
            self.assertEqual(env.read_text(encoding="ascii"), preserved_environment)
            redis_unit = service_dir / "mediacenter-redis.service"
            self.assertEqual(second["redis_service_file"], str(redis_unit))
            self.assertIn("docker update --memory 4g --memory-swap 4g mediacenter-redis",
                          redis_unit.read_text(encoding="utf-8"))

    def test_runtime_layout_uses_configured_nondefault_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = make_bundle(root)
            inspected = release_verify.inspect_server_bundle(bundle)
            data_root = root / "data"
            release, _created = installer.extract_release(bundle, data_root, inspected)
            config = data_root / "config"
            config.mkdir()
            configured_key = config / "api-v4.key"
            configured_key.write_text("existing-key\n", encoding="ascii")
            environment = config / "mediacenter.env"
            environment.write_text(
                f"MEDIACENTER_API_KEY_FILE={configured_key.as_posix()}\n",
                encoding="ascii",
            )
            layout = installer.ensure_runtime_layout(
                data_root, release, root / "systemd", Path("/opt/mediacenter/python"),
                gpu_pool=None, gpu_uuids=None, host="127.0.0.1", port=8787,
            )
            self.assertEqual(layout["api_key_file"], str(configured_key.resolve()))
            self.assertFalse(layout["api_key_created"])
            self.assertFalse((config / "api-key").exists())

    def test_status_and_mutation_boundary_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(installer.status(root)["status"], "not_installed")
            if os.name != "posix":
                with self.assertRaisesRegex(installer.InstallError,
                                            "server_install_mutation_requires_linux"):
                    installer.mutate(
                        "install", None, root, root / "systemd", Path("/usr/bin/python3"),
                        service_control=False, gpu_pool="0", gpu_uuids=None,
                        host="127.0.0.1", port=8787, health_url="http://127.0.0.1:8787",
                    )

    def test_upgrade_restores_service_if_pointer_switch_fails_after_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = {
                "schema": installer.STATE_SCHEMA,
                "current_version": "1.0.0",
                "previous_version": None,
                "manifest_sha256": "0" * 64,
                "updated_at": "2026-09-03T00:00:00Z",
            }
            actions: list[str] = []
            with (
                mock.patch.object(installer, "is_posix_runtime", return_value=True),
                mock.patch.object(installer, "current_version", return_value="1.0.0"),
                mock.patch.object(installer, "read_state", return_value=state),
                mock.patch.object(installer, "preflight", return_value={"status": "passed"}),
                mock.patch.object(installer, "inspect_server_bundle", return_value={
                    "manifest": {"version": "1.1.0"}, "manifest_sha256": "1" * 64,
                }),
                mock.patch.object(installer, "extract_release",
                                  return_value=(root / "releases/1.1.0", True)),
                mock.patch.object(installer, "ensure_runtime_layout", return_value={
                    "api_key_file": str(root / "config/api-v4.key"),
                }),
                mock.patch.object(installer, "systemctl",
                                  side_effect=lambda action, **_kwargs: actions.append(action)),
                mock.patch.object(installer, "switch_current",
                                  side_effect=installer.InstallError("synthetic_switch_failure")),
                mock.patch.object(installer, "wait_health", return_value={"health": 200}),
            ):
                with self.assertRaisesRegex(installer.InstallError, "release_upgrade_failed"):
                    installer.mutate(
                        "upgrade", root / "bundle.tar.gz", root, root / "systemd",
                        Path(sys.executable), service_control=True, gpu_pool=None,
                        gpu_uuids=None, host="127.0.0.1", port=8787,
                        health_url="http://127.0.0.1:8787",
                    )
            self.assertEqual(actions, ["stop", "daemon-reload", "start"])

    def test_bootstrap_delegates_only_to_transactional_installer(self) -> None:
        script = (ROOT / "scripts/bootstrap_server.sh").read_text(encoding="utf-8")
        self.assertIn('install_server_release.py" "$@"', script)
        self.assertNotIn("/opt/projects", script)
        self.assertNotIn("cp ", script)

    def test_bundle_rejects_an_undeclared_member(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = make_bundle(Path(directory), extra_member=True)
            with self.assertRaisesRegex(release_verify.ReleaseValidationError,
                                        "server_bundle_file_set_mismatch"):
                release_verify.inspect_server_bundle(bundle)


if __name__ == "__main__":
    unittest.main()
