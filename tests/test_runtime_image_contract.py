from __future__ import annotations

import copy
from contextlib import ExitStack
import hashlib
import importlib.util
import io
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tarfile
import unittest
from unittest import mock
import zipfile

from scripts import build_runtime_image as image

ROOT = Path(__file__).resolve().parents[1]


class RuntimeImageContractTests(unittest.TestCase):
    def setUp(self):
        self.lock = json.loads((ROOT / "containers/runtime-v1/python.lock").read_text(encoding="utf-8"))
        self.temp = tempfile.TemporaryDirectory(prefix="mc034-contract-")
        self.path = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def assertCode(self, code, call, *args):
        with self.assertRaises(image.ContractError) as caught:
            call(*args)
        self.assertEqual(caught.exception.code, code)

    def test_real_target_declaration_closure(self):
        result = image.validate_python_lock(self.lock)
        self.assertEqual(result["packages"], 53)
        self.assertEqual(result["artifact_bytes"], 3056576073)
        self.assertEqual(result["closure"], "declaration_complete")
        self.assertEqual(result["artifact_metadata"], "not_verified")
        self.assertNotIn("async-timeout", {image.normalized(p["name"]) for p in self.lock["packages"]})

    def test_missing_transitive_dependency(self):
        self.lock["packages"] = [p for p in self.lock["packages"] if p["name"] != "mpmath"]
        self.assertCode("dependency_missing", image.validate_python_lock, self.lock)

    def test_conflicting_dependency(self):
        next(p for p in self.lock["packages"] if p["name"] == "sympy")["requires_dist"] = ["mpmath>=9"]
        self.assertCode("dependency_version_conflict", image.validate_python_lock, self.lock)

    def test_duplicate_normalized_package(self):
        self.lock["packages"].append(copy.deepcopy(self.lock["packages"][0]))
        self.assertCode("duplicate_package", image.validate_python_lock, self.lock)

    def test_target_and_core_cannot_float(self):
        self.lock["target"]["python"] = "3.12.0"
        self.assertCode("python_target_mismatch", image.validate_python_lock, self.lock)
        self.lock["target"] = image.TARGET
        self.lock["required_roots"]["torch"] = "latest"
        self.assertCode("core_version_changed", image.validate_python_lock, self.lock)

    def test_extra_dependency_cannot_be_silently_ignored(self):
        row = next(p for p in self.lock["packages"] if p["name"] == "diffusers")
        row["requires_dist"].append("requests[socks]")
        self.assertCode("dependency_missing", image.validate_python_lock, self.lock)

    def test_marker_version_order_and_target(self):
        self.assertFalse(image.marker_matches('python_version < "3.9"'))
        self.assertFalse(image.marker_matches('python_full_version < "3.11.3"'))
        self.assertTrue(image.marker_matches('platform_system == "Linux" and (extra == "" or extra == "cpu")'))
        self.assertIsNone(image.requirement('missing>=1; extra == "dev"'))

    def test_marker_unknown_variable_and_executable_rejected(self):
        self.assertCode("unsupported_marker_variable", image.marker_matches, 'host_secret == "x"')
        self.assertCode("unsupported_marker", image.marker_matches, '__import__("os").getcwd()')

    def test_stable_versions_local_and_wildcards(self):
        self.assertTrue(image.satisfies("2.7.1+cu126", "==2.7.1"))
        self.assertFalse(image.satisfies("2.7.1+cu126", "==2.7.1+cu128"))
        self.assertTrue(image.satisfies("3.11.15", "~=3.11.0,!=3.10.*"))
        self.assertFalse(image.satisfies("3.12.0", "~=3.11.0"))
        self.assertCode("unsupported_version", image.satisfies, "1.0rc1", ">=1")

    def test_abi3_is_target_compatible(self):
        image.compatible_wheel("psutil-7.0.0-cp36-abi3-manylinux2010_x86_64.whl")
        image.compatible_wheel("pkg-1.0-py3-none-manylinux_2_28_x86_64.whl")
        self.assertCode("wheel_python_mismatch", image.compatible_wheel, "pkg-1.0-cp312-abi3-manylinux2014_x86_64.whl")
        self.assertCode("wheel_platform_mismatch", image.compatible_wheel, "pkg-1.0-cp311-cp311-manylinux_2_36_x86_64.whl")
        self.assertCode("wheel_platform_mismatch", image.compatible_wheel, "pkg-1.0-cp311-cp311-musllinux_1_2_x86_64.whl")

    def test_artifact_url_cannot_be_local_or_different_file(self):
        self.lock["packages"][0]["url"] = "file:///secret"
        self.assertCode("artifact_url_mismatch", image.validate_python_lock, self.lock)

    def test_json_duplicate_nan_and_invalid_rejected(self):
        self.assertCode("duplicate_json_key", image.strict_json, b'{"x":1,"x":2}')
        self.assertCode("nonfinite_json", image.strict_json, b'{"x":NaN}')
        self.assertCode("invalid_json", image.strict_json, b'[')

    def system(self):
        return json.loads((ROOT / "containers/runtime-v1/system.lock").read_text(encoding="utf-8"))

    def test_authenticated_system_declared_closure(self):
        result = image.validate_system_lock(self.system())
        self.assertEqual(result["build"], {"packages": 164, "added": 63, "artifact_bytes": 80503294})
        self.assertEqual(result["runtime"], {"packages": 106, "added": 5, "artifact_bytes": 2174890})

    def test_system_missing_edge_cannot_claim_complete(self):
        system = self.system()
        system["stages"]["build"]["edges"].pop()
        self.assertCode("system_dependency_edges_missing", image.validate_system_lock, system)

    def test_system_provider_drift_fails(self):
        system = self.system()
        system["stages"]["build"]["edges"][0]["provider_version"] = "0.0"
        self.assertCode("system_provider_version_changed", image.validate_system_lock, system)

    def test_system_cannot_replace_base_silently(self):
        system = self.system()
        next(p for p in system["packages"] if p["Package"] == "adduser")["Version"] = "999"
        self.assertCode("system_base_changed", image.validate_system_lock, system)

    def test_system_runtime_inventory_and_delta_fail_closed(self):
        system = self.system()
        system["final_inventory"].pop()
        self.assertCode("final_inventory_mismatch", image.validate_system_lock, system)
        system = self.system()
        system["stages"]["runtime"]["incremental_deb_bytes"] += 1
        self.assertCode("system_delta_mismatch", image.validate_system_lock, system)

    def test_debian_version_semantics_not_lexicographic(self):
        for left, right in (("1:1.0", "999.0"), ("1.0", "1.0~rc1"), ("1.0-2", "1.0-1"), ("1.10", "1.9")):
            self.assertGreater(image.deb_version_compare(left, right), 0)
            self.assertLess(image.deb_version_compare(right, left), 0)
        self.assertEqual(image.deb_version_compare("1.01-0", "1.1"), 0)

    def test_system_artifacts_missing_do_not_trigger_download(self):
        self.assertCode("input_missing", image.verify_system_artifacts, self.path, self.system())

    def file(self, name, data):
        path = self.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return {"filename": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def test_exact_hash_size_and_budget(self):
        entry = self.file("tiny.bin", b"abc")
        image.verify_file(self.path, entry)
        self.assertCode("input_byte_budget", image.read_file, self.path / "tiny.bin", 2)
        self.assertCode("artifact_size_mismatch", image.verify_file, self.path, dict(entry, size=4))
        self.assertCode("artifact_hash_mismatch", image.verify_file, self.path, dict(entry, sha256="0" * 64))

    def test_escaping_paths(self):
        for path in ("../secret", "/secret", "C:/secret", "a\\b"):
            with self.subTest(path=path):
                self.assertCode("unsafe_path", image.safe_path, self.path, path)

    @unittest.skipUnless(os.name == "posix", "POSIX link fixture")
    def test_symlink_parent_is_rejected(self):
        target = self.path / "original"
        target.mkdir()
        (target / "file").write_bytes(b"x")
        (self.path / "link").symlink_to(target, target_is_directory=True)
        self.assertCode("symlink_input", image.safe_path, self.path, "link/file")

    def wheel(self, metadata=None, extra=None):
        metadata = metadata or b"Metadata-Version: 2.1\nName: tiny\nVersion: 1.0\nRequires-Python: >=3.9\n\n"
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("tiny-1.0.dist-info/METADATA", metadata)
            if extra:
                archive.writestr(*extra)
        row = self.file("tiny-1.0-py3-none-any.whl", stream.getvalue())
        return dict(row, name="tiny", version="1.0", requires_python=">=3.9", requires_dist=[])

    def test_offline_actual_wheel_metadata(self):
        result = image.verify_wheel(self.path, self.wheel())
        self.assertEqual(result["name"], "tiny")

    def test_wheel_metadata_drift_is_not_project_json_proof(self):
        row = self.wheel(metadata=b"Name: tiny\nVersion: 2.0\nRequires-Python: >=3.9\n\n")
        self.assertCode("wheel_metadata_mismatch", image.verify_wheel, self.path, row)

    def test_wheel_path_and_metadata_bomb_rejected(self):
        self.assertCode("unsafe_wheel_member", image.verify_wheel, self.path,
                        self.wheel(extra=("../../escape", b"x")))
        self.assertCode("wheel_metadata_limit", image.verify_wheel, self.path,
                        self.wheel(metadata=b"x" * (2 * 1024**2 + 1)))

    def oci(self):
        def blob(value):
            raw = value if isinstance(value, bytes) else image.canonical(value)
            entry = self.file("blobs/sha256/" + image.hash_bytes(raw), raw)
            return {"digest": "sha256:" + entry["sha256"], "size": entry["size"]}
        config = blob({"os": "linux", "architecture": "amd64"})
        layer = blob(b"synthetic-layer-not-an-actual-image")
        manifest = blob({"schemaVersion": 2, "config": config, "layers": [layer]})
        self.file("oci-layout", image.canonical({"imageLayoutVersion": "1.0.0"}))
        self.file("index.json", image.canonical({"schemaVersion": 2, "manifests": [manifest]}))
        return {"manifest": manifest["digest"], "config": config["digest"], "layers": [layer]}

    def test_oci_all_blobs_verified_and_missing_fails_no_resolver(self):
        base = self.oci()
        self.assertEqual(image.verify_oci(self.path, base)["blobs_verified"], 3)
        leaf = self.path / "blobs/sha256" / base["layers"][0]["digest"][7:]
        leaf.rename(leaf.with_suffix(".preserved"))
        self.assertCode("input_missing", image.verify_oci, self.path, base)

    def test_oci_hash_drift(self):
        base = self.oci()
        leaf = self.path / "blobs/sha256" / base["layers"][0]["digest"][7:]
        leaf.write_bytes(b"X" * leaf.stat().st_size)
        self.assertCode("artifact_hash_mismatch", image.verify_oci, self.path, base)

    def built_oci_fixture(self, uid=1000):
        system = self.system()
        system["base"]["layers"] = []
        python = {"python": "3.11.15 fixture", "uid": uid, "imports": "passed", "compiler": "gcc",
                  "packages": [[p["name"], p["version"]] for p in self.lock["packages"]]}
        inventory = "".join("\t".join((p["name"], p["version"], p["architecture"])) + "\n" for p in system["final_inventory"])
        layer_buffer = io.BytesIO()
        with tarfile.open(fileobj=layer_buffer, mode="w") as layer:
            for name, data in (("opt/runtime-evidence/python.json", image.canonical(python)),
                               ("opt/runtime-evidence/system.tsv", inventory.encode())):
                member = tarfile.TarInfo(name)
                member.size = len(data)
                layer.addfile(member, io.BytesIO(data))
        files = {}
        def blob(data):
            raw = data if isinstance(data, bytes) else image.canonical(data)
            digest = image.hash_bytes(raw)
            files["blobs/sha256/" + digest] = raw
            return {"digest": "sha256:" + digest, "size": len(raw)}
        config = blob({"os": "linux", "architecture": "amd64", "config": {"User": "1000:1000"}})
        layer = dict(blob(gzip.compress(layer_buffer.getvalue())), mediaType="application/vnd.oci.image.layer.v1.tar+gzip")
        manifest = blob({"schemaVersion": 2, "config": config, "layers": [layer]})
        files["index.json"] = image.canonical({"schemaVersion": 2, "manifests": [manifest]})
        files["oci-layout"] = image.canonical({"imageLayoutVersion": "1.0.0"})
        output = self.path / "synthetic.oci.tar"
        with tarfile.open(output, "w") as archive:
            for name, data in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        return output, system

    def test_output_inspection_reads_actual_bytes_not_report_claim(self):
        output, system = self.built_oci_fixture()
        result = image.inspect_built_oci(output, {}, system, self.lock, 1024**2, 5)
        self.assertEqual(result["offline_build_import"], "passed")
        self.assertEqual(result["dynamic_elf_inspection"], "not_executed")
        changed = copy.deepcopy(self.lock)
        changed["packages"][0]["version"] = "999"
        self.assertCode("built_python_inventory_mismatch", image.inspect_built_oci, output, {}, system, changed, 1024**2, 5)

    def test_output_root_import_and_budget_are_not_accepted(self):
        output, system = self.built_oci_fixture(uid=0)
        self.assertCode("build_import_failed", image.inspect_built_oci, output, {}, system, self.lock, 1024**2, 5)
        self.assertCode("output_byte_budget", image.inspect_built_oci, output, {}, system, self.lock, 1, 5)

    def test_verify_only_never_claims_build_import_or_measurements(self):
        report = image.verify_contract(ROOT)
        self.assertFalse(report["build_ready"])
        self.assertEqual(report["build"], "not_executed")
        self.assertEqual(report["offline_import"], "not_executed")
        self.assertIsNone(report["final_image"])
        self.assertIsNone(report["disk_peak_bytes"])
        self.assertIsNone(report["network_bytes"])

    def test_independent_release_approval_is_not_taken_from_manifest(self):
        self.assertCode("release_approval_mismatch", image.verify_contract, ROOT, None, None, None, "0" * 64)
        digest = image.hash_bytes((ROOT / "containers/runtime-v1/release.json").read_bytes())
        self.assertTrue(image.verify_contract(ROOT, approved_release_sha256=digest)["approval_digest_matched"])
        self.assertFalse(image.verify_contract(ROOT)["approval_digest_matched"])

    def test_release_report_digest_binds_first_validated_buffer(self):
        original = image.read_file
        calls = []
        def read(path, limit):
            if path.name == "release.json":
                calls.append(path)
                if len(calls) > 1:
                    return b'{"foreign":"later read"}'
            return original(path, limit)
        with mock.patch.object(image, "read_file", side_effect=read):
            result = image.verify_contract(ROOT)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["release_sha256"], image.hash_bytes(original(ROOT / "containers/runtime-v1/release.json", 2 * 1024**2)))

    def stage_fixture(self):
        artifacts = self.path / "artifacts"
        source = self.path / "source"
        artifacts.mkdir()
        directory = source / "containers/runtime-v1"
        directory.mkdir(parents=True)
        wheel = {"name": "tiny", "version": "1.0", "filename": "tiny.whl", "size": 1, "sha256": image.hash_bytes(b"w")}
        python_source = {"filename": "source.xz", "size": 1, "sha256": image.hash_bytes(b"s")}
        (artifacts / "tiny.whl").write_bytes(b"w")
        (artifacts / "source.xz").write_bytes(b"s")
        (directory / "python.lock").write_bytes(image.canonical({"packages": [wheel]}))
        (directory / "system.lock").write_bytes(image.canonical({"packages": [], "stages": {"build": {"added": []}, "runtime": {"added": []}}}))
        (directory / "Dockerfile").write_bytes(b"FROM runtime_parent\n")
        release = {"inputs": {n: image.hash_bytes((directory / n).read_bytes()) for n in ("python.lock", "system.lock", "Dockerfile")},
                   "sdk": [], "python_source": python_source}
        (directory / "release.json").write_bytes(image.canonical(release))
        digest = image.hash_bytes((directory / "release.json").read_bytes())
        return source, artifacts, digest

    def test_staging_only_copies_pinned_bytes_and_retains_failure(self):
        source, artifacts, digest = self.stage_fixture()
        destination = self.path / "context"
        with mock.patch.object(image, "verify_contract", return_value={"release_sha256": digest}):
            result = image.stage_context(source, artifacts, source, self.path, destination, 8192, digest)
        self.assertEqual(result["status"], "staged_not_built")
        self.assertEqual({r["filename"] for r in result["files"]}, {"wheels/tiny.whl", "source.xz", "Dockerfile", "requirements.txt"})
        self.assertNotIn("artifacts", {p.name for p in destination.iterdir()})
        (artifacts / "tiny.whl").write_bytes(b"X")
        failed = self.path / "failed-context"
        with mock.patch.object(image, "verify_contract", return_value={"release_sha256": digest}):
            self.assertCode("input_changed", image.stage_context, source, artifacts, source, self.path, failed, 8192, digest)
        self.assertTrue((failed / "context-intent.json").exists())
        self.assertFalse((failed / "context-receipt.json").exists())

    def test_staging_budget_and_existing_output_fail_without_overwrite(self):
        source, artifacts, digest = self.stage_fixture()
        destination = self.path / "context"
        with mock.patch.object(image, "verify_contract", return_value={"release_sha256": digest}):
            self.assertCode("context_byte_budget", image.stage_context, source, artifacts, source, self.path, destination, 1, digest)
        self.assertFalse(destination.exists())
        destination.mkdir()
        (destination / "preserve").write_bytes(b"keep")
        with mock.patch.object(image, "verify_contract", return_value={"release_sha256": digest}):
            self.assertCode("output_exists", image.stage_context, source, artifacts, source, self.path, destination, 8192, digest)
        self.assertEqual((destination / "preserve").read_bytes(), b"keep")

    def test_cli_build_fails_without_subprocess_or_network(self):
        with mock.patch("subprocess.Popen", side_effect=AssertionError("must not execute")), mock.patch("builtins.print") as printed:
            self.assertEqual(image.main(["--source", str(ROOT), "--build"]), 2)
        self.assertIn("explicit_build_inputs_required", printed.call_args.args[0])

    def test_real_fast_exit_log_overflow_never_succeeds(self):
        for i in range(10):
            evidence = self.path / ("logs-%d" % i)
            evidence.mkdir()
            self.assertCode("client_log_budget", image.run_bounded,
                            [sys.executable, "-B", "-c", "import sys; sys.stdout.write('x'*32768)"],
                            dict(os.environ), self.path, 5, 64, evidence)
            result = json.loads((evidence / "client-exit.json").read_text())
            self.assertEqual(result["error"], "client_log_budget")
            self.assertTrue(result["reader_exited"])
            print("MC034 overflow pid=%s exit=%s" % (result["pid"], result["exit_code"]), flush=True)

    def test_spawn_record_failure_reaps_exact_direct_child(self):
        evidence = self.path / "spawn-failure"
        evidence.mkdir()
        spawned = []
        popen, write = subprocess.Popen, image.write_exclusive
        def spawn(*args, **kwargs):
            child = popen(*args, **kwargs)
            spawned.append(child)
            return child
        def fail(path, data):
            if path.name == "client-start.json": raise OSError("simulated disk full")
            return write(path, data)
        with mock.patch.object(image.subprocess, "Popen", side_effect=spawn), mock.patch.object(image, "write_exclusive", side_effect=fail):
            with self.assertRaises(OSError):
                image.run_bounded([sys.executable, "-B", "-c", "import time; time.sleep(10)"],
                                  dict(os.environ), self.path, 1, 1024, evidence)
        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(spawned[0].poll())
        print("MC034 spawn_record_failure pid=%s exit=%s" % (spawned[0].pid, spawned[0].returncode), flush=True)

    def test_real_client_timeout_keeps_daemon_unknown(self):
        evidence = self.path / "timeout"
        evidence.mkdir()
        self.assertCode("client_timeout", image.run_bounded,
                        [sys.executable, "-B", "-c", "import time; time.sleep(10)"], dict(os.environ),
                        self.path, 0.1, 1024, evidence)
        result = json.loads((evidence / "client-exit.json").read_text())
        self.assertEqual(result["daemon_execution"], "unknown")
        self.assertIsNotNone(result["exit_code"])
        print("MC034 timeout pid=%s exit=%s" % (result["pid"], result["exit_code"]), flush=True)

    def test_cgroup_mount_binds_real_path_not_arbitrary_limit_folder(self):
        mounts = "10 1 0:1 / / rw - ext4 /dev/sda rw\n20 10 0:22 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
        result = image.cgroup_mount_binding(mounts, "/sys/fs/cgroup/private", "/private")
        self.assertEqual(result["mount_id"], 20)
        self.assertCode("not_cgroup2_mount", image.cgroup_mount_binding, mounts, "/tmp/fake", "/private")
        self.assertCode("cgroup_membership_path_mismatch", image.cgroup_mount_binding, mounts, "/sys/fs/cgroup/private", "/other")
        overlay = mounts + "21 20 0:33 / /sys/fs/cgroup/private rw - tmpfs tmpfs rw\n"
        self.assertCode("not_cgroup2_mount", image.cgroup_mount_binding, overlay, "/sys/fs/cgroup/private", "/private")
        same_path = mounts + "21 10 0:33 / /sys/fs/cgroup rw - tmpfs tmpfs rw\n"
        self.assertCode("ambiguous_cgroup_mount", image.cgroup_mount_binding, same_path, "/sys/fs/cgroup/private", "/private")

    def test_resource_limits_must_be_finite_positive(self):
        valid = {"memory.max": "1048576", "pids.max": "64", "cpu.max": "200000 100000"}
        self.assertEqual(image.validate_resource_limits(valid)["cpu_quota"], 200000)
        for key, value in (("memory.max", "max"), ("pids.max", "0"), ("cpu.max", "max 100000"), ("cpu.max", "1 0")):
            self.assertCode("unbounded_resource_limit", image.validate_resource_limits, dict(valid, **{key: value}))

    def test_signed_stanza_cannot_authorize_changed_deb_fields(self):
        body = b"Package: tiny\nVersion: 1.0\nArchitecture: all\nSize: 3\nSHA256: abc\nDepends: libc6"
        row = {"Package": "tiny", "Version": "1.0", "Architecture": "all", "Size": "3", "SHA256": "abc",
               "Depends": "libc6", "_stanza_sha256": image.hash_bytes(body)}
        image.verify_package_stanzas(body + b"\n\n", [row])
        for key, value in (("SHA256", "foreign"), ("Size", "5"), ("Depends", "evil")):
            self.assertCode("package_stanza_mismatch", image.verify_package_stanzas, body + b"\n\n", [dict(row, **{key: value})])

    def test_fixed_builder_config_binds_storage_no_arbitrary_toml(self):
        prep = {"storage": {"path": "/private/store"}, "socket": {"path": "/private/builder.sock"},
                "binaries": {"bin/buildkit-runc": "/private/bin/buildkit-runc"}}
        config = image.expected_buildkit_config(prep)
        self.assertIn(b'root = "/private/store"', config)
        self.assertIn(b'gc = false', config)
        self.assertIn(b'[worker.containerd]\nenabled = false', config)
        prep["storage"]["path"] = '/private/store"\nmalicious=true'
        self.assertCode("invalid_preparation_path", image.expected_buildkit_config, prep)

    @unittest.skipUnless(os.name == "posix", "Linux-only build orchestration fixture; no daemon")
    def test_build_orchestration_exact_remote_and_persistent_unknown_gate(self):
        storage, group = self.path / "storage", self.path / "group"
        storage.mkdir()
        group.mkdir()
        limits = {"memory.max": "1048576", "pids.max": "64", "cpu.max": "200000 100000"}
        for name, value in limits.items(): (group / name).write_text(value)
        prep = {"process": {"pid": 123, "start_ticks": 77, "boot_id": "fixture"},
                "socket": {"path": "/private/buildkit.sock", "identity": {}},
                "storage": {"path": str(storage), "identity": {}}, "worker_id": "fixture-worker",
                "cgroup": {"path": str(group), "membership": "/mc-private"}, "limits": limits}
        release = image.hash_bytes((ROOT / "containers/runtime-v1/release.json").read_bytes())
        calls, fail_build = [], [False]
        def client(argv, env, cwd, timeout, log_limit, evidence, observer=None):
            calls.append(argv)
            self.assertNotIn("DOCKER_HOST", env)
            if argv[-1] == "version": return b"github.com/docker/buildx v0.31.1", {}
            if "info" in argv: return image.canonical({"buildkitVersion": {"version": "v0.27.0"}}), {}
            if "workers" in argv: return image.canonical([{"id": "fixture-worker", "buildkitVersion": {"version": "v0.27.0"}, "platforms": [{"os": "linux", "architecture": "amd64"}]}]), {}
            if "create" in argv: return b"mc-runtime-fixture", {}
            if "inspect" in argv: return b"Driver: remote\nEndpoint: unix:///private/buildkit.sock\n", {}
            if "build" in argv:
                self.assertIn("--cgroup-parent", argv)
                self.assertEqual(argv[argv.index("--cgroup-parent") + 1], "/mc-private")
                if fail_build[0]: raise image.ContractError("client_timeout")
                return b"completed", {}
            self.fail("unexpected command")
        def stage(*args):
            Path(args[4]).mkdir()
            return {"status": "fixture_staged"}
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(image, "verify_contract", return_value={"release_sha256": release}))
            stack.enter_context(mock.patch.object(image, "verify_preparation", return_value=(prep, "/private/buildx", "/private/buildctl")))
            stack.enter_context(mock.patch.object(image, "proc_identity", return_value=prep["process"]))
            stack.enter_context(mock.patch.object(image, "object_identity", return_value={}))
            stack.enter_context(mock.patch.object(image, "run_bounded", side_effect=client))
            stack.enter_context(mock.patch.object(image, "stage_context", side_effect=stage))
            stack.enter_context(mock.patch.object(image, "inspect_built_oci", return_value={"fixture": "not actual OCI evidence"}))
            args = (ROOT, self.path, ROOT, self.path, self.path / "prep", release, "1" * 64)
            result = image.build_runtime(*args, self.path / "success", "cold", 1024**2, 20, 1024)
            self.assertEqual(result["build"], "completed")
            self.assertFalse((storage / ".runtime-build-active.json").exists())
            self.assertEqual(len(list(storage.glob(".runtime-build-completed-*.json"))), 1)
            fail_build[0] = True
            self.assertCode("client_timeout", image.build_runtime, *args, self.path / "timeout-build", "warm", 1024**2, 20, 1024)
            self.assertTrue((storage / ".runtime-build-active.json").exists())
            count = len(calls)
            copied_prep_args = (ROOT, self.path, ROOT, self.path, self.path / "copied-prep", release, "2" * 64)
            self.assertCode("previous_build_exit_unconfirmed", image.build_runtime, *copied_prep_args, self.path / "repeat", "warm", 1024**2, 20, 1024)
            self.assertEqual(len(calls), count)
            # Third prep check is immediately before the first client spawn.
            # Its elapsed time must not be given back as a fresh client budget.
            new_storage = self.path / "deadline-storage"
            new_storage.mkdir()
            prep["storage"]["path"] = str(new_storage)
            clock, checks = [0.0], [0]
            def slow_preparation(*unused):
                checks[0] += 1
                if checks[0] == 3: clock[0] = 21.0
                return prep, "/private/buildx", "/private/buildctl"
            with mock.patch.object(image.time, "monotonic", side_effect=lambda: clock[0]), mock.patch.object(image, "verify_preparation", side_effect=slow_preparation):
                self.assertCode("build_timeout", image.build_runtime, *args, self.path / "expired-preflight", "cold", 1024**2, 20, 1024)
            self.assertEqual(len(calls), count)

    @unittest.skipUnless(os.name == "posix", "POSIX flock")
    def test_stable_storage_lock_excludes_other_preparation_paths(self):
        with image.builder_lock(self.path):
            with self.assertRaisesRegex(image.ContractError, "builder_busy"):
                with image.builder_lock(self.path): pass
        with image.builder_lock(self.path): pass
        self.assertTrue((self.path / ".runtime-build.lock").exists())

    def test_plan_remote_oci_only_no_pull_prune_or_default_load(self):
        plan = image.fixed_build_argv("/tools/buildx", "mc-runtime-test", "unix:///run/mc/buildkit.sock",
                                     "/context", "/parent", "/output/image.oci", "cold")
        self.assertEqual(plan["driver"], "remote")
        self.assertIn("runtime_parent=oci-layout:///parent@" + image.PARENT, plan["argv"])
        self.assertIn("--no-cache", plan["argv"])
        self.assertNotIn("--load", plan["argv"])
        self.assertNotIn("prune", plan["argv"])
        self.assertEqual(plan["daemon_exit_on_client_timeout"], "unknown")

    def test_plan_rejects_remote_endpoint_and_option_injection(self):
        self.assertCode("invalid_builder_endpoint", image.fixed_build_argv, "/tools/buildx", "mc-runtime-test",
                        "tcp://host:1234", "/context", "/parent", "/output", "warm")
        self.assertCode("invalid_build_path", image.fixed_build_argv, "/tools/buildx", "mc-runtime-test",
                        "unix:///run/private.sock", "/context", "/parent", "/output,push=true", "warm")

    def test_shared_helper_exact_identity_and_bytes(self):
        from mediacenter import task_state, worker_common
        self.assertIs(task_state.TaskStateError, worker_common.TaskStateError)
        self.assertIs(task_state.canonical, worker_common.canonical)
        self.assertIs(task_state.digest, worker_common.digest)
        self.assertEqual(worker_common.canonical({"中": 1, "a": [True, None]}), '{"a":[true,null],"中":1}')
        with self.assertRaises(task_state.TaskStateError) as caught:
            worker_common.canonical({"x": float("nan")})
        self.assertEqual((caught.exception.code, caught.exception.status), ("invalid_task_json", 400))

    def isolated_sdk(self, with_redis):
        for name in image.SDK:
            dest = self.path / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, dest)
        dependency_paths = []
        if with_redis:
            spec = importlib.util.find_spec("redis")
            if not spec:
                self.skipTest("redis-py unavailable; Linux verification uses existing private prefix")
            dependency_paths.append(str(Path(spec.origin).parent.parent))
            for path in os.environ.get("PYTHONPATH", "").split(os.pathsep):
                if path and not (Path(path) / "mediacenter").exists():
                    dependency_paths.append(path)
        code = "import sys,json; sys.path[:0]=" + repr([str(self.path)] + dependency_paths) + "; "
        code += "import mediacenter.worker_runtime,mediacenter.worker_journal,mediacenter.transport; "
        if with_redis:
            code += "import mediacenter.redis_transport; "
        code += "assert not any(x in sys.modules for x in ('mediacenter.task_state','mediacenter.repository','mediacenter.server')); print('sdk_import_isolated_ok')"
        process = subprocess.Popen([sys.executable, "-I", "-B", "-c", code], cwd=self.path,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            stdout, stderr = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
            self.fail("isolated import timed out")
        print("MC034 SDK process pid=%s exit=%s redis=%s" % (process.pid, process.returncode, with_redis), flush=True)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("sdk_import_isolated_ok", stdout)

    def test_sdk_import_from_whitelist_without_control_source(self):
        self.isolated_sdk(False)

    def test_sdk_redis_import_from_whitelist_without_control_source(self):
        self.isolated_sdk(True)


if __name__ == "__main__":
    unittest.main()
