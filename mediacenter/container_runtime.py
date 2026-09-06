"""Bounded local Docker operations and retained cgroup-v2 execution domains.

No pull, exec, remove, arbitrary HTTP endpoint, PID kill or automatic retry.
The concrete observer requires a predelegated, exclusive cgroupfs subtree. It
never enables controllers or deletes domains. Its opaque handle is an identity
comparison, not an exit signal. Unreadable/unsupported identity means unknown.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import socket
import stat
import time
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from .config import ContainerError, EngineConfig, canonical, checked_path, digest, object_identity


def full_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ContainerError("container_id_invalid")
    return value


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        value = json.loads(data, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ContainerError("engine_response_invalid") from None


class UnixEngine:
    """One bounded request per connection, against an identity-pinned socket."""
    def __init__(self, config: EngineConfig, *, clock=time.monotonic):
        self.config, self.clock = config, clock
        path = checked_path(config.socket_path)
        if not stat.S_ISSOCK(path.stat().st_mode) or not hasattr(socket, "AF_UNIX"):
            raise ContainerError("local_engine_unavailable")
        self.socket_identity = object_identity(path)

    def _request(self, method, path, payload=None, *, expected=(200,)):
        # Private: public methods construct every path and method.
        if object_identity(self.config.socket_path) != self.socket_identity:
            raise ContainerError("engine_socket_changed")
        body = b"" if payload is None else canonical(payload).encode()
        request = (f"{method} /v{self.config.api_version}{path} HTTP/1.1\r\nHost: localhost\r\n"
                   f"Connection: close\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body
        deadline, sent = self.clock() + self.config.total_timeout, False
        buffer = bytearray()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        def timeout():
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise TimeoutError()
            client.settimeout(min(remaining, self.config.io_timeout))
        def read_more():
            timeout()
            chunk = client.recv(16384)
            if not chunk:
                raise ContainerError("engine_response_truncated", outcome_unknown=sent and method in {"POST", "DELETE"})
            buffer.extend(chunk)
        def line(limit):
            while b"\r\n" not in buffer:
                if len(buffer) > limit:
                    raise ContainerError("engine_response_too_large", outcome_unknown=sent and method in {"POST", "DELETE"})
                read_more()
            index = buffer.index(b"\r\n")
            if index > limit:
                raise ContainerError("engine_response_too_large", outcome_unknown=sent and method in {"POST", "DELETE"})
            result = bytes(buffer[:index]); del buffer[:index + 2]
            return result
        def exact(size):
            if size > self.config.response_limit:
                raise ContainerError("engine_response_too_large", outcome_unknown=sent and method in {"POST", "DELETE"})
            while len(buffer) < size:
                read_more()
            result = bytes(buffer[:size]); del buffer[:size]
            return result
        try:
            timeout(); client.connect(self.config.socket_path)
            # The socket identity is checked again after connect. No proxy/env.
            if object_identity(self.config.socket_path) != self.socket_identity:
                raise ContainerError("engine_socket_changed")
            timeout(); sent = True; client.sendall(request)
            status_line = line(4096)
            matched = re.fullmatch(rb"HTTP/1\.[01] ([0-9]{3}) [^\r\n]*", status_line)
            if not matched:
                raise ContainerError("engine_http_invalid", outcome_unknown=method in {"POST", "DELETE"})
            status = int(matched.group(1)); headers = {}; header_bytes = len(status_line)
            while True:
                current = line(8192); header_bytes += len(current) + 2
                if header_bytes > 32768:
                    raise ContainerError("engine_response_too_large", outcome_unknown=method in {"POST", "DELETE"})
                if not current:
                    break
                key, separator, value = current.partition(b":")
                key = key.lower()
                if not separator or key in headers:
                    raise ContainerError("engine_http_invalid", outcome_unknown=method in {"POST", "DELETE"})
                headers[key] = value.strip()
            if status in (204, 304):
                data = b""
            elif b"transfer-encoding" in headers:
                if headers[b"transfer-encoding"] != b"chunked" or b"content-length" in headers:
                    raise ContainerError("engine_http_invalid", outcome_unknown=method in {"POST", "DELETE"})
                chunks, total = [], 0
                while True:
                    raw_size = line(32)
                    if not re.fullmatch(rb"[0-9a-fA-F]{1,8}", raw_size):
                        raise ContainerError("engine_http_invalid", outcome_unknown=method in {"POST", "DELETE"})
                    size = int(raw_size, 16); total += size
                    if total > self.config.response_limit:
                        raise ContainerError("engine_response_too_large", outcome_unknown=method in {"POST", "DELETE"})
                    if size == 0:
                        if line(2):
                            raise ContainerError("engine_http_invalid", outcome_unknown=method in {"POST", "DELETE"})
                        break
                    chunks.append(exact(size))
                    if exact(2) != b"\r\n":
                        raise ContainerError("engine_http_invalid", outcome_unknown=method in {"POST", "DELETE"})
                data = b"".join(chunks)
            else:
                raw_size = headers.get(b"content-length", b"")
                if not re.fullmatch(rb"[0-9]{1,10}", raw_size):
                    raise ContainerError("engine_http_invalid", outcome_unknown=method in {"POST", "DELETE"})
                data = exact(int(raw_size))
            if status not in expected:
                code = {404: "engine_object_missing", 409: "engine_name_conflict", 403: "engine_permission_denied"}.get(status, "engine_request_failed")
                raise ContainerError(code, outcome_unknown=method in {"POST", "DELETE"} and status >= 500)
            return _json(data) if data else {}
        except (OSError, TimeoutError):
            raise ContainerError("engine_unavailable", outcome_unknown=sent and method in {"POST", "DELETE"}) from None
        finally:
            client.close()

    def verify_engine(self):
        info = self._request("GET", "/info")
        architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(info.get("Architecture"), info.get("Architecture"))
        if (info.get("ID") != self.config.engine_id or info.get("ServerVersion") != self.config.server_version
                or info.get("OSType") != "linux" or architecture != self.config.platform.split("/")[1]
                or info.get("CgroupVersion") != "2" or info.get("CgroupDriver") != self.config.cgroup_driver
                or (self.config.image_store == "overlay2" and info.get("Driver") != "overlay2")
                or (self.config.image_store == "containerd"
                    and ["driver-type", "io.containerd.snapshotter.v1"] not in info.get("DriverStatus", []))
                or any(info.get(key) is not True for key in ("MemoryLimit", "SwapLimit", "CpuCfsPeriod", "CpuCfsQuota", "PidsLimit"))
                or not any(str(v).startswith("name=seccomp") for v in info.get("SecurityOptions", []))):
            raise ContainerError("engine_contract_mismatch")
        return {"engine_id": info["ID"], "socket_identity": self.socket_identity}

    def verify_image(self, approval):
        return self.inspect_image(approval)["Id"]

    def inspect_image(self, approval):
        """Return verified local image metadata; never resolve from a registry."""
        if approval.platform != self.config.platform:
            raise ContainerError("image_platform_mismatch")
        containerd_store = self.config.image_store == "containerd"
        bare_manifest = containerd_store and approval.reference.startswith("sha256:")
        if bare_manifest:
            info = self._request("GET", "/info")
            if (info.get("ID") != self.config.engine_id or info.get("ServerVersion") != self.config.server_version
                    or ["driver-type", "io.containerd.snapshotter.v1"] not in info.get("DriverStatus", [])):
                raise ContainerError("image_store_contract_mismatch")
        target = approval.reference if containerd_store else approval.image_id
        path = "/images/" + quote(target, safe="") + "/json"
        if bare_manifest:
            parts = approval.platform.split("/")
            platform_query = {"os": parts[0], "architecture": parts[1]}
            if len(parts) == 3:
                platform_query["variant"] = parts[2]
            path += "?platform=" + quote(canonical(platform_query), safe="")
        image = self._request("GET", path)
        platform = image.get("Os", "") + "/" + image.get("Architecture", "")
        if image.get("Variant"):
            platform += "/" + image["Variant"]
        if image.get("Id") != approval.image_id or platform != approval.platform:
            raise ContainerError("image_contract_mismatch")
        if bare_manifest:
            descriptor = image.get("Descriptor")
            if (type(descriptor) is not dict or descriptor.get("digest") != approval.reference
                    or descriptor.get("mediaType") != "application/vnd.oci.image.manifest.v1+json"
                    or type(descriptor.get("size")) is not int or descriptor["size"] <= 0):
                raise ContainerError("image_descriptor_mismatch")
            config = image.get("Config", {})
            if ((config.get("Entrypoint") or []) != list(approval.entrypoint)
                    or (config.get("Cmd") or []) != list(approval.command)):
                raise ContainerError("image_entrypoint_mismatch")
            rootfs = image.get("RootFS", {})
            if (rootfs.get("Type") != "layers" or not isinstance(rootfs.get("Layers"), list)
                    or not rootfs["Layers"] or any(not isinstance(v, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", v) for v in rootfs["Layers"])):
                raise ContainerError("image_rootfs_invalid")
        elif containerd_store:
            if approval.reference not in image.get("RepoDigests", []):
                raise ContainerError("image_contract_mismatch")
        else:
            if image.get("Descriptor") is not None or image.get("RepoDigests"):
                raise ContainerError("image_store_contract_mismatch")
            config = image.get("Config", {})
            if ((config.get("Entrypoint") or []) != list(approval.entrypoint)
                    or (config.get("Cmd") or []) != list(approval.command)):
                raise ContainerError("image_entrypoint_mismatch")
            rootfs = image.get("RootFS", {})
            if (rootfs.get("Type") != "layers" or not isinstance(rootfs.get("Layers"), list)
                    or not rootfs["Layers"] or any(not isinstance(v, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", v) for v in rootfs["Layers"])):
                raise ContainerError("image_rootfs_invalid")
        # Image defaults are otherwise merged by Docker despite an explicit spec.
        image_config = image.get("Config", {})
        if image_config.get("Volumes") or image_config.get("ExposedPorts") or image_config.get("OnBuild") or (image_config.get("Env") or []) != list(approval.environment):
            raise ContainerError("image_implicit_config_rejected")
        return image

    def create(self, name, spec):
        if not re.fullmatch(r"mc-[0-9a-f]{32}", name):
            raise ContainerError("container_name_invalid")
        result = self._request("POST", "/containers/create?name=" + name + "&platform=" + quote(self.config.platform, safe=""), spec, expected=(201,))
        if result.get("Warnings") or set(result) - {"Id", "Warnings"}:
            raise ContainerError("container_create_warnings", outcome_unknown=True)
        try:
            return full_id(result.get("Id"))
        except ContainerError:
            raise ContainerError("container_create_identity_unknown", outcome_unknown=True) from None

    def inspect(self, container_id):
        return self._request("GET", "/containers/" + full_id(container_id) + "/json?size=false")

    def inspect_intent_name(self, name):
        if not re.fullmatch(r"mc-[0-9a-f]{32}", name):
            raise ContainerError("container_name_invalid")
        return self._request("GET", "/containers/" + name + "/json?size=false")

    def start(self, container_id):
        return self._request("POST", "/containers/" + full_id(container_id) + "/start", expected=(204, 304))

    def stop(self, container_id):
        return self._request("POST", f"/containers/{full_id(container_id)}/stop?t={self.config.stop_seconds}&signal=SIGTERM", expected=(204, 304))

    def wait(self, container_id):
        result = self._request("POST", "/containers/" + full_id(container_id) + "/wait?condition=not-running")
        if type(result.get("StatusCode")) is not int or result.get("Error"):
            raise ContainerError("container_wait_unconfirmed")
        return result["StatusCode"]

    def remove(self, container_id):
        return self._request(
            "DELETE",
            "/containers/" + full_id(container_id) + "?force=false&v=false&link=false",
            expected=(204,),
        )


def verify_inspection(row, inspected):
    """Labels are corroboration only; the durable intent/full ID is authority."""
    spec = json.loads(row["spec_json"])
    if digest(spec) != row["spec_digest"]:
        raise ContainerError("runtime_intent_integrity_error")
    identity = full_id(inspected.get("Id"))
    if row.get("container_id") and identity != row["container_id"]:
        raise ContainerError("container_ownership_mismatch")
    if inspected.get("Name") != "/" + row["container_name"] or inspected.get("Image") != row["image_id"]:
        raise ContainerError("container_ownership_mismatch")
    actual_config, actual_host = inspected.get("Config", {}), inspected.get("HostConfig", {})
    for key in ("Image", "User", "Entrypoint", "Cmd", "Env", "WorkingDir", "Healthcheck", "Tty", "OpenStdin"):
        if actual_config.get(key) != spec[key]:
            raise ContainerError("container_config_drift")
    # Docker merges immutable image labels into the container configuration.
    # They are metadata only; every MediaCenter authority label must still be
    # exactly the requested value and no additional MediaCenter label may
    # appear through image inheritance.
    expected_labels = spec["Labels"]
    actual_labels = actual_config.get("Labels")
    if (not isinstance(actual_labels, dict)
            or any(actual_labels.get(key) != value for key, value in expected_labels.items())
            or any(key.startswith("mediacenter.") and key not in expected_labels
                   for key in actual_labels)):
        raise ContainerError("container_config_drift")
    for key, value in spec["HostConfig"].items():
        if key == "Mounts":
            continue
        if actual_host.get(key) != value:
            raise ContainerError("container_security_drift")
    requested_host_mounts = spec["HostConfig"]["Mounts"]
    actual_host_mounts = actual_host.get("Mounts")
    if not isinstance(actual_host_mounts, list) or len(actual_host_mounts) != len(requested_host_mounts):
        raise ContainerError("container_mount_drift")
    def normalized_host_mount(mount):
        bind = mount.get("BindOptions") or {}
        return (mount.get("Type"), mount.get("Source"), mount.get("Target"),
                mount.get("ReadOnly", False), bind.get("Propagation", ""),
                bind.get("NonRecursive", False), bind.get("CreateMountpoint", False))
    if ([normalized_host_mount(item) for item in actual_host_mounts]
            != [normalized_host_mount(item) for item in requested_host_mounts]):
        raise ContainerError("container_mount_drift")
    if actual_config.get("Volumes") or actual_config.get("ExposedPorts"):
        raise ContainerError("container_implicit_mount_or_port")
    for forbidden in ("Binds", "VolumesFrom", "Links", "ExtraHosts", "GroupAdd", "DeviceCgroupRules", "MaskedPaths", "ReadonlyPaths"):
        # Docker supplies its default masked/readonly paths; those tighten policy.
        if forbidden not in {"MaskedPaths", "ReadonlyPaths"} and actual_host.get(forbidden):
            raise ContainerError("container_security_drift")
    if actual_host.get("UTSMode") or actual_host.get("UsernsMode") == "host" or actual_host.get("Runtime") not in (None, "", "runc", "nvidia"):
        raise ContainerError("container_security_drift")
    expected_mounts = {(m["Source"], m["Target"], not m["ReadOnly"]) for m in requested_host_mounts}
    actual_mounts = inspected.get("Mounts")
    if not isinstance(actual_mounts, list) or len(actual_mounts) != len(expected_mounts):
        raise ContainerError("container_mount_drift")
    if {(m.get("Source"), m.get("Destination"), m.get("RW")) for m in actual_mounts} != expected_mounts or any(m.get("Type") != "bind" or m.get("Propagation") != "rprivate" for m in actual_mounts):
        raise ContainerError("container_mount_drift")
    if inspected.get("RestartCount") != 0:
        raise ContainerError("container_restart_untrusted")
    state = inspected.get("State", {})
    if type(state.get("Running")) is not bool or type(state.get("Pid")) is not int or state.get("Restarting") or state.get("Paused") or state.get("Dead"):
        raise ContainerError("container_state_untrusted")
    return identity


def _read_fd(fd, limit=8192):
    os.lseek(fd, 0, os.SEEK_SET)
    value = os.read(fd, limit + 1)
    if not value or len(value) > limit:
        raise ContainerError("cgroup_observation_unreadable")
    return value.decode("ascii")


def opaque_handle(fd):
    """Linux export handle comparison only; bounded, no open_by_handle_at."""
    class Handle(ctypes.Structure):
        _fields_ = [("handle_bytes", ctypes.c_uint), ("handle_type", ctypes.c_int),
                    ("f_handle", ctypes.c_ubyte * 128)]
    handle, mount = Handle(), ctypes.c_int()
    handle.handle_bytes = 128
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        function = libc.name_to_handle_at
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.c_int]
        function.restype = ctypes.c_int
        if function(fd, b"", ctypes.byref(handle), ctypes.byref(mount), 0x1000) != 0 or not 0 < handle.handle_bytes <= 128:
            raise ContainerError("cgroup_handle_unavailable")
        return {"type": handle.handle_type, "bytes": handle.handle_bytes,
                "handle": bytes(handle.f_handle[:handle.handle_bytes]).hex(), "mount_id": mount.value}
    except (AttributeError, OSError):
        raise ContainerError("cgroup_handle_unavailable") from None


class CgroupObserver:
    def __init__(self, config: EngineConfig):
        self.config = config
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise ContainerError("cgroup_observer_unsupported")
        self.mount, self.delegated = checked_path(config.cgroup_mount), checked_path(config.delegated_subtree)
        self._host()  # validates the actual cgroup2 mount, not a normal folder.

    def _host(self):
        try:
            boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            if not re.fullmatch(r"[0-9a-f-]{36}", boot):
                raise ContainerError("cgroup_host_identity_invalid")
            namespace = os.stat("/proc/self/ns/mnt")
            matches = []
            for line in Path("/proc/self/mountinfo").read_text(encoding="ascii").splitlines():
                before, separator, after = line.partition(" - ")
                fields = before.split()
                if separator and len(fields) >= 6 and fields[4] == str(self.mount):
                    if after.split()[0] != "cgroup2" or fields[3] != "/":
                        raise ContainerError("cgroup_mount_invalid")
                    matches.append({"mount_id": int(fields[0]), "device": fields[2], "root": fields[3], "mount": fields[4]})
            if len(matches) != 1:
                raise ContainerError("cgroup_mount_invalid")
            return {"boot_id": boot, "namespace": [namespace.st_dev, namespace.st_ino], "mount": matches[0]}
        except (OSError, UnicodeError, ValueError, IndexError):
            raise ContainerError("cgroup_host_identity_unavailable") from None

    def parent_path(self, name):
        if not re.fullmatch(r"mc-[0-9a-f]{32}", name):
            raise ContainerError("cgroup_name_invalid")
        if self.config.cgroup_driver == "systemd":
            return self.mount / "mediacenter.slice" / ("mediacenter-" + name[3:] + ".slice")
        return self.delegated / name

    def docker_parent(self, name):
        if self.config.cgroup_driver == "systemd":
            self.parent_path(name)
            return "mediacenter-" + name[3:] + ".slice"
        return "/" + self.parent_path(name).relative_to(self.mount).as_posix()

    def _open(self, path):
        checked_path(path)
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)

    def _identity(self, parent_fd):
        info = os.fstat(parent_fd)
        result = {"host": self._host(), "device": info.st_dev, "inode": info.st_ino, "handle": opaque_handle(parent_fd)}
        if result["handle"]["mount_id"] != result["host"]["mount"]["mount_id"]:
            raise ContainerError("cgroup_mount_identity_changed")
        return result

    def create(self, name):
        parent = self.parent_path(name)
        if self.config.cgroup_driver == "systemd":
            if parent.exists():
                raise ContainerError("cgroup_create_unknown", outcome_unknown=True)
            return {"path": str(parent), "docker_parent": self.docker_parent(name),
                    "membership_parent": "/" + parent.relative_to(self.mount).as_posix(),
                    "identity": None, "state": "planned"}
        delegated_fd = self._open(self.delegated)
        try:
            kind_fd = os.open("cgroup.type", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=delegated_fd)
            try:
                if _read_fd(kind_fd).strip() != "domain":
                    raise ContainerError("cgroup_delegation_invalid")
            finally:
                os.close(kind_fd)
            # Existing target is not ours simply because its name matches.
            os.mkdir(name, mode=0o700, dir_fd=delegated_fd)
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=delegated_fd)
            try:
                result = {"path": str(parent), "docker_parent": self.docker_parent(name),
                          "membership_parent": self.docker_parent(name),
                          "identity": self._identity(fd), "state": "retained"}
                if self._populated(fd) != 0:
                    raise ContainerError("new_cgroup_not_empty")
                return result
            finally:
                os.close(fd)
        except OSError:
            raise ContainerError("cgroup_create_unknown", outcome_unknown=True) from None
        finally:
            os.close(delegated_fd)

    def _populated(self, fd):
        events = os.open("cgroup.events", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
        try:
            values = {}
            for line in _read_fd(events).splitlines():
                fields = line.split()
                if len(fields) != 2 or fields[0] in values:
                    raise ContainerError("cgroup_events_invalid")
                values[fields[0]] = fields[1]
            if values.get("populated") not in {"0", "1"}:
                raise ContainerError("cgroup_events_invalid")
            return int(values["populated"])
        finally:
            os.close(events)

    def _record_path(self, record):
        path = Path(record["path"])
        if self.config.cgroup_driver == "systemd":
            matched = re.fullmatch(r"mediacenter-([0-9a-f]{32})\.slice", path.name)
            name = "mc-" + matched.group(1) if matched else ""
            expected_parent = "/" + path.relative_to(self.mount).as_posix() if matched else ""
            valid_parent = path.parent == self.mount / "mediacenter.slice"
        else:
            name, expected_parent, valid_parent = path.name, self.docker_parent(path.name), path.parent == self.delegated
        if (not valid_parent or self.parent_path(name) != path
                or record["docker_parent"] != self.docker_parent(name)
                or record.get("membership_parent") != expected_parent
                or record.get("state") not in {"planned", "retained"}):
            raise ContainerError("cgroup_record_invalid")
        return path

    def previous_boot(self, record):
        """Observation only; never grants permission to launch or adopt a domain.

        Only a retained execution from a different kernel boot is eligible.
        Same-boot missing directories still require the ordinary exit proof.
        The Controller must additionally verify the exact stopped container.
        """
        self._record_path(record)
        identity = record.get("identity")
        if identity is None:
            return None
        old = identity.get("host")
        if (record.get("state") != "retained" or not isinstance(old, dict)
                or not isinstance(old.get("boot_id"), str)
                or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", old["boot_id"])):
            raise ContainerError("cgroup_host_identity_invalid")
        current = self._host()
        if current["boot_id"] == old["boot_id"]:
            return None
        if self._host() != current:
            raise ContainerError("cgroup_host_identity_changed")
        return {"kind": "previous-kernel-boot", "previous_host": old,
                "current_host": current, "domain_digest": digest(record)}

    def verify(self, record):
        path = self._record_path(record)
        if record["identity"] is None:
            if record["state"] != "planned":
                raise ContainerError("cgroup_record_invalid")
            return
        fd = self._open(path)
        try:
            if self._identity(fd) != record["identity"]:
                raise ContainerError("cgroup_identity_changed")
        finally:
            os.close(fd)

    @staticmethod
    def _process(pid):
        if type(pid) is not int or pid <= 0:
            raise ContainerError("container_init_unobserved")
        try:
            text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            # comm may contain spaces or parentheses. starttime is field 22.
            fields = text[text.rindex(")") + 2:].split()
            starttime = int(fields[19])
            memberships = Path(f"/proc/{pid}/cgroup").read_text(encoding="ascii").splitlines()
            cgroups = [v[3:] for v in memberships if v.startswith("0::")]
            if len(cgroups) != 1:
                raise ContainerError("container_membership_unavailable")
            return {"pid": pid, "starttime": starttime, "membership": cgroups[0]}
        except (OSError, ValueError, IndexError, UnicodeError):
            raise ContainerError("container_init_unobserved") from None

    def observe_member(self, record, pid):
        self.verify(record)
        first = self._process(pid)
        path, parent = PurePosixPath(first["membership"]), PurePosixPath(record["membership_parent"])
        if path == parent or not path.is_relative_to(parent):
            raise ContainerError("container_outside_owned_domain")
        if record["identity"] is None:
            fd = self._open(record["path"])
            try:
                record["identity"] = self._identity(fd)
                record["state"] = "retained"
            finally:
                os.close(fd)
        second = self._process(pid)
        self.verify(record)
        if first != second:
            raise ContainerError("container_pid_reused")
        return first

    def prove_empty(self, record, membership):
        if not membership or not PurePosixPath(membership["membership"]).is_relative_to(PurePosixPath(record["membership_parent"])):
            raise ContainerError("container_domain_mapping_unproven")
        self.verify(record)
        fd = self._open(record["path"])
        try:
            before = self._identity(fd)
            if before != record["identity"] or self._populated(fd) != 0 or self._identity(fd) != before:
                raise ContainerError("execution_domain_not_empty")
            return {"kind": "cgroup-v2-retained-parent-empty", "identity": before, "membership": membership,
                    "populated": 0, "gpu_release_proven": False}
        except OSError:
            raise ContainerError("execution_domain_unconfirmed") from None
        finally:
            os.close(fd)
