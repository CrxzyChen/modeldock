from __future__ import annotations

import copy
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mediacenter.config import ContainerError, EngineConfig, MountGrant, digest
from mediacenter.container_runtime import CgroupObserver, UnixEngine, full_id, opaque_handle, verify_inspection
from tests.test_runtime_controller import fixture_policy, FixtureEngine


class ContainerPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.policy = fixture_policy(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def spec(self, policy=None):
        return (policy or self.policy).spec(name="mc-" + "a" * 32, instance_id="instance", epoch="epoch",
                                            intent_id="runtime-one", cgroup_parent="/mediacenter/mc-" + "a" * 32)

    def test_fixed_security_and_explicit_cpu_or_uuid_mapping(self):
        spec = self.spec(); host = spec["HostConfig"]
        self.assertEqual(host["PidMode"], "")
        self.assertEqual(host["NetworkMode"], "none")
        self.assertEqual(host["CapDrop"], ["ALL"])
        self.assertTrue(host["ReadonlyRootfs"])
        self.assertFalse(host["Privileged"])
        self.assertEqual(host["DeviceRequests"], [])
        self.assertEqual(set(host["Tmpfs"]), {"/tmp", "/dev/shm"})
        self.assertIn("noexec,nosuid,nodev", host["Tmpfs"]["/dev/shm"])
        for mount in host["Mounts"]:
            self.assertTrue(mount["BindOptions"]["NonRecursive"])
        gpu = "GPU-12345678-1234-1234-1234-123456789abc"
        request = self.spec(replace(self.policy, gpu_uuids=(gpu,)))["HostConfig"]["DeviceRequests"][0]
        self.assertEqual(request["Driver"], "cdi")
        self.assertEqual(request["DeviceIDs"], ["nvidia.com/gpu=" + gpu])
        self.assertIsNone(request["Capabilities"])
        self.assertIsNone(request["Options"])

        systemd = self.policy.spec(name="mc-" + "b" * 32, instance_id="instance", epoch="epoch",
                                   intent_id="runtime-two",
                                   cgroup_parent="mediacenter-" + "b" * 32 + ".slice")
        self.assertEqual(systemd["HostConfig"]["CgroupParent"],
                         "mediacenter-" + "b" * 32 + ".slice")

    def test_no_root_all_gpu_bad_limits_or_missing_roles(self):
        for changes in ({"uid": 0}, {"gid": 0}, {"memory_bytes": 0}, {"pids_limit": -1},
                        {"gpu_uuids": ("all",)}, {"gpu_uuids": ("0",)}, {"mounts": self.policy.mounts[:-1]}):
            with self.subTest(changes=changes), self.assertRaises(ContainerError):
                self.spec(replace(self.policy, **changes))

    def test_mount_source_ancestor_and_forbidden_hardlink_rejected(self):
        grants = list(self.policy.mounts)
        grants[0] = MountGrant.capture("models", self.root, "/worker/models")
        with self.assertRaisesRegex(ContainerError, "mount_forbidden_source"):
            self.spec(replace(self.policy, mounts=tuple(grants)))
        hardlink = self.root / "disguised-key"
        os.link(self.root / "server-key", hardlink)
        grants = list(self.policy.mounts)
        grants[2] = MountGrant.capture("bootstrap", hardlink, "/worker/bootstrap")
        with self.assertRaisesRegex(ContainerError, "mount_forbidden_source"):
            self.spec(replace(self.policy, mounts=tuple(grants)))

    def test_replaced_source_or_overlapping_target_fails_closed(self):
        grants = list(self.policy.mounts)
        source = self.root / "bootstrap"; source.rename(self.root / "old-bootstrap")
        source.write_text("foreign", encoding="utf-8")
        with self.assertRaisesRegex(ContainerError, "mount_identity_changed"):
            self.spec()
        grants[2] = MountGrant.capture("bootstrap", source, "/worker/models/bootstrap")
        with self.assertRaisesRegex(ContainerError, "mount_targets_overlap"):
            self.spec(replace(self.policy, mounts=tuple(grants)))

    def test_fixed_api_configuration_and_full_ids_only(self):
        with self.assertRaises(ContainerError):
            EngineConfig("/run/docker.sock", "engine", "/sys/fs/cgroup", "/sys/fs/cgroup")
        with self.assertRaises(ContainerError):
            EngineConfig("/run/docker.sock", "engine", "/sys/fs/cgroup", "/sys/fs/cgroup/mc", api_version="auto")
        current = EngineConfig("/run/docker.sock", "engine", "/sys/fs/cgroup", "/sys/fs/cgroup/mc",
                               api_version="1.51", server_version="28.3.2")
        self.assertEqual((current.api_version, current.server_version), ("1.51", "28.3.2"))
        with self.assertRaises(ContainerError):
            EngineConfig("/run/docker.sock", "engine", "/sys/fs/cgroup", "/sys/fs/cgroup/mc",
                         api_version="1.51", server_version="29.7.2")
        for value in ("123", "container-name", "../outside", "a" * 63, "A" * 64):
            with self.assertRaises(ContainerError):
                full_id(value)

    def test_remove_uses_exact_full_id_and_never_forces_or_deletes_volumes(self):
        engine = object.__new__(UnixEngine)
        calls = []
        engine._request = lambda method, path, **options: calls.append(
            (method, path, options)) or {}
        container_id = "f" * 64
        engine.remove(container_id)
        self.assertEqual(calls, [("DELETE",
            f"/containers/{container_id}?force=false&v=false&link=false",
            {"expected": (204,)})])

    def test_approved_image_environment_is_exact_and_safe_overrides_are_explicit(self):
        environment = ("PATH=/usr/local/bin:/usr/bin", "LANG=C.UTF-8", "PYTHON_VERSION=3.11.15", "PYTHONUNBUFFERED=0")
        approval = replace(self.policy.image, environment=environment)
        policy = replace(self.policy, image=approval)
        self.assertEqual(self.spec(policy)["Env"], ["PATH=/usr/local/bin:/usr/bin", "LANG=C.UTF-8", "PYTHON_VERSION=3.11.15", "PYTHONUNBUFFERED=1", "PYTHONDONTWRITEBYTECODE=1"])
        engine = object.__new__(UnixEngine)
        engine.config = EngineConfig("/run/docker.sock", "fixture", "/sys/fs/cgroup", "/sys/fs/cgroup/mc")
        image = {"Id": approval.image_id, "RepoDigests": [approval.reference], "Os": "linux", "Architecture": "amd64", "Config": {"Env": list(environment)}}
        engine._request = lambda *args, **kwargs: copy.deepcopy(image)
        self.assertEqual(engine.verify_image(approval), approval.image_id)
        for mutated in (list(environment) + ["UNAPPROVED=1"], ["PATH=/foreign"], []):
            image["Config"]["Env"] = mutated
            with self.assertRaisesRegex(ContainerError, "image_implicit_config_rejected"):
                engine.verify_image(approval)

    def test_triton_cache_is_explicit_bounded_and_executable(self):
        approval = replace(self.policy.image, environment=("TRITON_CACHE_DIR=/mc-triton-cache",))
        host = self.spec(replace(self.policy, image=approval))["HostConfig"]
        self.assertEqual(set(host["Tmpfs"]), {"/tmp", "/dev/shm", "/mc-triton-cache"})
        self.assertIn("rw,exec,nosuid,nodev", host["Tmpfs"]["/mc-triton-cache"])
        self.assertIn("size=16777216", host["Tmpfs"]["/mc-triton-cache"])
        self.assertIn("mode=700,uid=1000,gid=1000", host["Tmpfs"]["/mc-triton-cache"])
        self.assertIn("TRITON_CACHE_DIR=/mc-triton-cache", self.spec(replace(self.policy, image=approval))["Env"])
        invalid = replace(self.policy.image, environment=("TRITON_CACHE_DIR=/tmp",))
        with self.assertRaisesRegex(ContainerError, "image_environment_invalid"):
            self.spec(replace(self.policy, image=invalid))

    def test_bare_oci_identity_requires_containerd_descriptor_and_platform(self):
        approval = replace(self.policy.image, reference=self.policy.image.image_id)
        engine = object.__new__(UnixEngine)
        engine.config = EngineConfig("/run/docker.sock", "fixture", "/sys/fs/cgroup", "/sys/fs/cgroup/mc")
        info = {"ID": "fixture", "ServerVersion": engine.config.server_version,
                "DriverStatus": [["driver-type", "io.containerd.snapshotter.v1"]]}
        image = {"Id": approval.image_id, "RepoDigests": [], "Os": "linux", "Architecture": "amd64",
                 "Descriptor": {"digest": approval.reference, "mediaType": "application/vnd.oci.image.manifest.v1+json", "size": 123},
                 "Config": {"Entrypoint": list(approval.entrypoint), "Cmd": list(approval.command), "Env": []},
                 "RootFS": {"Type": "layers", "Layers": ["sha256:" + "e" * 64]}}
        calls = []
        def request(method, path):
            calls.append((method, path))
            return copy.deepcopy(info if path == '/info' else image)
        engine._request = request
        self.assertEqual(engine.verify_image(approval), approval.image_id)
        self.assertIn('?platform=', calls[-1][1])
        for field, value in [('Id', 'sha256:' + '9' * 64), ('Descriptor', {}), ('RootFS', {}), ('Architecture', 'arm64')]:
            original = image[field]; image[field] = value
            with self.assertRaises(ContainerError): engine.verify_image(approval)
            image[field] = original
        info['DriverStatus'] = []
        with self.assertRaisesRegex(ContainerError, 'image_store_contract_mismatch'): engine.verify_image(approval)
        with self.assertRaises(ContainerError): replace(approval, reference='sha256:' + '9' * 64)

    def test_docker_inspection_normalizes_image_labels_and_omitted_mount_false_defaults(self):
        spec = self.spec()
        container_id = "f" * 64
        row = {"spec_json": json.dumps(spec, sort_keys=True, separators=(",", ":")),
               "spec_digest": digest(spec),
               "container_id": container_id, "container_name": "mc-" + "a" * 32,
               "image_id": self.policy.image.image_id}
        host = copy.deepcopy(spec["HostConfig"])
        for mount in host["Mounts"]:
            mount["BindOptions"].pop("CreateMountpoint")
            if mount["ReadOnly"] is False:
                mount.pop("ReadOnly")
        config = {key: copy.deepcopy(spec[key]) for key in
                  ("Image", "User", "Entrypoint", "Cmd", "Env", "WorkingDir",
                   "Healthcheck", "Tty", "OpenStdin", "Labels")}
        config["Labels"]["org.opencontainers.image.version"] = "24.04"
        inspected = {"Id": container_id, "Name": "/" + row["container_name"],
                     "Image": row["image_id"], "Config": config, "HostConfig": host,
                     "Mounts": [{"Source": item["Source"], "Destination": item["Target"],
                                 "RW": not item.get("ReadOnly", False), "Type": "bind",
                                 "Propagation": "rprivate"} for item in spec["HostConfig"]["Mounts"]],
                     "RestartCount": 0,
                     "State": {"Running": False, "Pid": 0, "Restarting": False,
                               "Paused": False, "Dead": False, "Status": "created"}}
        self.assertEqual(verify_inspection(row, inspected), container_id)
        inspected["Config"]["Labels"]["mediacenter.foreign"] = "bad"
        with self.assertRaisesRegex(ContainerError, "container_config_drift"):
            verify_inspection(row, inspected)
        inspected["Config"]["Labels"].pop("mediacenter.foreign")
        inspected["HostConfig"]["Mounts"][0]["BindOptions"]["CreateMountpoint"] = True
        with self.assertRaisesRegex(ContainerError, "container_mount_drift"):
            verify_inspection(row, inspected)


@unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "Linux FD parser fixture")
class CgroupParserTests(unittest.TestCase):
    """Ordinary files exercise parser/identity flow, not kernel enforcement."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.parent = self.root / ("mc-" + "a" * 32); self.parent.mkdir()
        (self.parent / "cgroup.events").write_text("populated 0\nfrozen 0\n", encoding="ascii")
        (self.parent / "cgroup.procs").write_text("", encoding="ascii")
        self.observer = object.__new__(CgroupObserver)
        self.observer.config = EngineConfig("/run/docker.sock", "fixture", str(self.root.parent),
                                            str(self.root), api_version="1.51",
                                            server_version="28.3.2")
        self.observer.mount, self.observer.delegated = self.root.parent, self.root
        self.identity = {"handle": "fixture-original", "host": "fixture-boot"}
        self.observer._identity = lambda fd: copy.deepcopy(self.identity)
        self.record = {"path": str(self.parent),
                       "docker_parent": self.observer.docker_parent(self.parent.name),
                       "membership_parent": self.observer.docker_parent(self.parent.name),
                       "identity": copy.deepcopy(self.identity), "state": "retained"}
        self.member = {"pid": 1, "starttime": 10, "membership": self.record["docker_parent"] + "/leaf"}

    def tearDown(self):
        self.temp.cleanup()

    def test_parent_empty_procs_is_not_descendant_exit(self):
        (self.parent / "cgroup.events").write_text("populated 1\nfrozen 0\n", encoding="ascii")
        with self.assertRaisesRegex(ContainerError, "execution_domain_not_empty"):
            self.observer.prove_empty(self.record, self.member)

    def test_empty_eof_duplicate_or_missing_populated_never_proves_exit(self):
        for text in ("", "frozen 0\n", "populated 0\npopulated 0\n", "populated x\n"):
            (self.parent / "cgroup.events").write_text(text, encoding="ascii")
            with self.subTest(text=text), self.assertRaises(ContainerError):
                self.observer.prove_empty(self.record, self.member)

    def test_replaced_handle_boot_or_missing_mapping_never_proves_exit(self):
        with self.assertRaisesRegex(ContainerError, "container_domain_mapping_unproven"):
            self.observer.prove_empty(self.record, None)
        self.identity["handle"] = "fixture-new-generation"
        with self.assertRaisesRegex(ContainerError, "cgroup_identity_changed"):
            self.observer.prove_empty(self.record, self.member)

    def test_leaf_deletion_not_consulted_parent_original_handle_still_required(self):
        leaf = self.parent / "leaf"; leaf.mkdir(); leaf.rmdir()
        proof = self.observer.prove_empty(self.record, self.member)
        self.assertEqual(proof["populated"], 0)
        self.assertFalse(proof["gpu_release_proven"])
        self.identity["host"] = "new-boot"
        with self.assertRaises(ContainerError):
            self.observer.prove_empty(self.record, self.member)

    def test_enoent_enodev_and_permission_read_errors_are_unknown(self):
        for error in (FileNotFoundError(), PermissionError(), OSError(19, "inactive")):
            with patch.object(self.observer, "_populated", side_effect=error):
                with self.assertRaisesRegex(ContainerError, "execution_domain_unconfirmed"):
                    self.observer.prove_empty(self.record, self.member)

    def test_pid_starttime_reuse_and_outside_membership_fail(self):
        first = dict(self.member, pid=123)
        with patch.object(self.observer, "_process", side_effect=[first, dict(first, starttime=11)]):
            with self.assertRaisesRegex(ContainerError, "container_pid_reused"):
                self.observer.observe_member(self.record, 123)
        with patch.object(self.observer, "_process", return_value=dict(first, membership="/external/leaf")):
            with self.assertRaisesRegex(ContainerError, "container_outside_owned_domain"):
                self.observer.observe_member(self.record, 123)

    def test_systemd_slice_is_planned_then_materialized_and_retained(self):
        systemd_root = self.root / "systemd"
        systemd_root.mkdir()
        (systemd_root / "mediacenter.slice").mkdir()
        observer = object.__new__(CgroupObserver)
        observer.config = EngineConfig("/run/docker.sock", "fixture", str(systemd_root),
                                       str(systemd_root), api_version="1.51",
                                       server_version="28.3.2", cgroup_driver="systemd",
                                       image_store="overlay2")
        observer.mount = observer.delegated = systemd_root
        observer._identity = lambda fd: copy.deepcopy(self.identity)
        name = "mc-" + "c" * 32
        record = observer.create(name)
        self.assertEqual(record["state"], "planned")
        self.assertIsNone(record["identity"])
        self.assertEqual(record["docker_parent"], "mediacenter-" + "c" * 32 + ".slice")
        self.assertEqual(record["membership_parent"],
                         "/mediacenter.slice/mediacenter-" + "c" * 32 + ".slice")
        parent = Path(record["path"])
        parent.mkdir()
        (parent / "cgroup.events").write_text("populated 0\nfrozen 0\n", encoding="ascii")
        membership = {"pid": 123, "starttime": 10,
                      "membership": record["membership_parent"] + "/docker-" + "d" * 64 + ".scope"}
        with patch.object(observer, "_process", side_effect=[membership, membership]):
            self.assertEqual(observer.observe_member(record, 123), membership)
        self.assertEqual(record["state"], "retained")
        self.assertEqual(record["identity"], self.identity)
        proof = observer.prove_empty(record, membership)
        self.assertEqual(proof["populated"], 0)


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Windows runtime lacks AF_UNIX; Linux fixture runs")
class UnixEngineTests(unittest.TestCase):
    """Real Unix HTTP sockets, no container engine or host resource changes."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mc031-http-"); self.root = Path(self.temp.name)
        self.path = self.root / "engine.sock"
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path)); self.server.listen(4); self.server.settimeout(.1)
        self.requests, self.errors, self.threads = [], [], []
        self.stop_event = threading.Event(); self.response = (200, b'{}', .0)
        self.thread = threading.Thread(target=self.serve, name="mc031-unix-fixture")
        self.thread.start()
        self.config = EngineConfig(str(self.path), "fixture-engine", "/sys/fs/cgroup", "/sys/fs/cgroup/mediacenter",
                                   total_timeout=.5, io_timeout=.2, stop_seconds=0, response_limit=1024)
        self.engine = UnixEngine(self.config)

    def serve(self):
        while not self.stop_event.is_set():
            try:
                client, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                client.settimeout(.5); data = bytearray()
                while b"\r\n\r\n" not in data:
                    chunk = client.recv(16384)
                    if not chunk:
                        break
                    data.extend(chunk)
                headers, _, body = bytes(data).partition(b"\r\n\r\n")
                content = next((line.split(b":", 1)[1].strip() for line in headers.split(b"\r\n") if line.lower().startswith(b"content-length:")), b"0")
                while len(body) < int(content):
                    body += client.recv(16384)
                first = headers.split(b"\r\n")[0].decode()
                self.requests.append((first, body.decode()))
                status, payload, delay = self.response
                if delay:
                    self.stop_event.wait(delay)
                raw = (f"HTTP/1.1 {status} fixture\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n").encode() + payload
                client.sendall(raw)
            except (OSError, ValueError) as error:
                if not isinstance(error, (BrokenPipeError, ConnectionResetError)):
                    self.errors.append(type(error).__name__)
            finally:
                client.close()

    def tearDown(self):
        self.stop_event.set(); self.server.close(); self.thread.join(2)
        self.assertFalse(self.thread.is_alive())
        print("MC031_UNIX_HTTP_FIXTURE " + json.dumps({"directory": str(self.root), "requests": len(self.requests),
              "thread_exited": not self.thread.is_alive(), "errors": self.errors}), flush=True)
        self.temp.cleanup()

    def test_fixed_paths_and_no_environment_proxy(self):
        with patch.dict(os.environ, {"DOCKER_HOST": "tcp://external:1234", "HTTP_PROXY": "http://external"}):
            self.engine.inspect("a" * 64)
        self.assertEqual(self.requests[0][0], "GET /v1.55/containers/" + "a" * 64 + "/json?size=false HTTP/1.1")
        with self.assertRaises(ContainerError):
            self.engine.inspect("../../anything")
        self.assertEqual(len(self.requests), 1)

    def test_unknown_create_timeout_is_bounded_and_single_post(self):
        self.response = (201, json.dumps({"Id": "a" * 64, "Warnings": []}).encode(), .8)
        began = time.monotonic()
        with self.assertRaises(ContainerError) as caught:
            self.engine.create("mc-" + "a" * 32, {})
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertLess(time.monotonic() - began, 1.2)
        self.assertEqual(len(self.requests), 1)

    def test_redirect_oversize_duplicate_json_and_warning_rejected(self):
        for response, code in (((302, b'{}', 0), "engine_request_failed"),
                               ((200, b"a" * 1025, 0), "engine_response_too_large"),
                               ((200, b'{"Id":1,"Id":2}', 0), "engine_response_invalid")):
            self.response = response
            with self.assertRaisesRegex(ContainerError, code):
                self.engine.inspect("a" * 64)
        self.response = (201, json.dumps({"Id": "a" * 64, "Warnings": ["unsafe default"]}).encode(), 0)
        with self.assertRaisesRegex(ContainerError, "container_create_warnings"):
            self.engine.create("mc-" + "a" * 32, {})

    def test_socket_replacement_rejected_before_request(self):
        self.path.rename(self.root / "original.sock")
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            replacement.bind(str(self.path))
            with self.assertRaisesRegex(ContainerError, "engine_socket_changed"):
                self.engine.inspect("a" * 64)
            self.assertFalse(self.requests)
        finally:
            replacement.close()


if __name__ == "__main__":
    unittest.main()
