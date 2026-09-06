"""Trusted, local container policy. These values are never taken from a task.

The controller host provisions mount grants, image approval and the delegated
cgroup subtree. This module neither discovers credentials nor installs an engine.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath


class ContainerError(ValueError):
    def __init__(self, code, *, outcome_unknown=False):
        self.code, self.outcome_unknown = code, outcome_unknown
        super().__init__(code)


# Releases admitted by the production container contract. The selected pair is
# still pinned in runtime.json and verify_engine corroborates it at startup.
SUPPORTED_ENGINE_RELEASES = frozenset({
    ("1.51", "28.3.2"),
    ("1.55", "29.7.2"),
})


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ContainerError("container_contract_invalid") from None


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise ContainerError("container_identity_invalid")
    return value


def checked_path(value):
    path = Path(value)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ContainerError("container_path_rejected")
    if not path.exists() or path.resolve() != path:
        raise ContainerError("container_path_rejected")
    return path


def object_identity(path):
    info = checked_path(path).stat()
    return {"device": info.st_dev, "inode": info.st_ino, "mode": stat.S_IFMT(info.st_mode)}


@dataclass(frozen=True)
class EngineConfig:
    socket_path: str
    engine_id: str
    cgroup_mount: str
    delegated_subtree: str
    platform: str = "linux/amd64"
    api_version: str = "1.55"
    server_version: str = "29.7.2"
    cgroup_driver: str = "cgroupfs"
    image_store: str = "containerd"
    total_timeout: float = 10.0
    io_timeout: float = 2.0
    import_timeout: float = 3600.0
    import_io_timeout: float = 60.0
    response_limit: int = 1024 * 1024
    stop_seconds: int = 3

    def __post_init__(self):
        identifier(self.engine_id)
        if ((self.api_version, self.server_version) not in SUPPORTED_ENGINE_RELEASES
                or self.platform not in {"linux/amd64", "linux/arm64/v8"}
                or self.cgroup_driver not in {"cgroupfs", "systemd"}
                or self.image_store not in {"containerd", "overlay2"}
                or not 0 < self.io_timeout <= self.total_timeout <= 60
                or not 0 < self.import_io_timeout <= self.import_timeout <= 7200
                or type(self.response_limit) is not int or not 1024 <= self.response_limit <= 4 * 1024 * 1024
                or type(self.stop_seconds) is not int or not 0 <= self.stop_seconds <= 10
                or self.total_timeout <= self.stop_seconds):
            raise ContainerError("engine_config_invalid")
        for value in (self.socket_path, self.cgroup_mount, self.delegated_subtree):
            if not isinstance(value, str) or not PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts:
                raise ContainerError("engine_config_invalid")
        mount, delegated = PurePosixPath(self.cgroup_mount), PurePosixPath(self.delegated_subtree)
        if (self.cgroup_driver == "cgroupfs" and (delegated == mount or not delegated.is_relative_to(mount))
                or self.cgroup_driver == "systemd" and delegated != mount):
            raise ContainerError("cgroup_delegation_invalid")


@dataclass(frozen=True)
class ImageApproval:
    reference: str
    image_id: str
    platform: str
    entrypoint: tuple[str, ...]
    command: tuple[str, ...]
    environment: tuple[str, ...] = ()

    def __post_init__(self):
        if (not isinstance(self.reference, str)
                or not re.fullmatch(r"(?:[a-z0-9][a-z0-9./:_-]*@)?sha256:[0-9a-f]{64}", self.reference)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.image_id)
                or (self.reference.startswith("sha256:") and self.reference != self.image_id)
                or self.platform not in {"linux/amd64", "linux/arm64/v8"}
                or not self.entrypoint or not self.entrypoint[0].startswith("/")):
            raise ContainerError("image_approval_invalid")
        for argv in (self.entrypoint, self.command):
            if not isinstance(argv, tuple) or len(argv) > 32 or any(not isinstance(v, str) or not v or "\x00" in v or len(v) > 4096 for v in argv):
                raise ContainerError("image_approval_invalid")
        if (not isinstance(self.environment, tuple) or len(self.environment) > 64
                or any(not isinstance(value, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*=[^\x00\r\n]{0,8192}", value) for value in self.environment)
                or len({value.split("=", 1)[0] for value in self.environment}) != len(self.environment)):
            raise ContainerError("image_environment_invalid")


READONLY_ROLES = {"models", "inputs", "bootstrap", "redis_credentials", "redis_socket", "lora_authority"}
WRITABLE_ROLES = {"journal", "outputs"}
REQUIRED_ROLES = {"models", "bootstrap", "redis_credentials", "redis_socket", "journal", "outputs"}


@dataclass(frozen=True)
class ForbiddenSources:
    """Prevalidated immutable deny-set used by every mount in one policy.

    The filesystem identities are captured once for an unchanged tuple.  The
    epoch boundary verifier invalidates its source tuple whenever the durable
    boundary/package registry advances, while each mount source identity is
    still checked on every reconciliation pass.
    """
    values: tuple[str, ...]
    paths: tuple[Path, ...]
    path_set: frozenset[Path]
    ancestor_paths: frozenset[Path]
    identities: frozenset[tuple[int, int]]

    def __iter__(self):
        return iter(self.values)

    def with_values(self, *values):
        return compile_forbidden_sources((*self.values, *(str(value) for value in values)))


@lru_cache(maxsize=256)
def compile_forbidden_sources(values):
    values = tuple(str(value) for value in values)
    paths = tuple(checked_path(value) for value in values)
    identities = tuple(object_identity(path) for path in paths)
    inode_keys = tuple((value["device"], value["inode"]) for value in identities)
    if len(set(inode_keys)) != len(values):
        raise ContainerError("forbidden_sources_invalid")
    return ForbiddenSources(values, paths, frozenset(paths),
        frozenset(parent for path in paths for parent in path.parents),
        frozenset(inode_keys))


@dataclass(frozen=True)
class MountGrant:
    role: str
    source: str
    target: str
    identity: dict

    @classmethod
    def capture(cls, role, source, target):
        return cls(role, str(checked_path(source)), target, object_identity(source))

    def verify(self, forbidden):
        if self.role not in READONLY_ROLES | WRITABLE_ROLES:
            raise ContainerError("mount_role_invalid")
        source = checked_path(self.source)
        target = PurePosixPath(self.target)
        if (not target.is_absolute() or str(target) != self.target or ".." in target.parts
                or target == PurePosixPath("/") or target.parts[1] in {"proc", "sys", "dev", "etc", "bin", "usr", "run"}):
            raise ContainerError("mount_target_rejected")
        # checked_path() has already walked every ancestor and rejected
        # symlinks.  Reuse this pass instead of calling object_identity(),
        # which would repeat the entire walk for every mount on every
        # reconciliation tick.
        source_info = source.stat()
        source_identity = {"device": source_info.st_dev,
                           "inode": source_info.st_ino,
                           "mode": stat.S_IFMT(source_info.st_mode)}
        if source_identity != self.identity:
            raise ContainerError("mount_identity_changed")
        denied_set = (forbidden if isinstance(forbidden, ForbiddenSources)
                      else compile_forbidden_sources(tuple(forbidden)))
        source_inode = (source_identity["device"], source_identity["inode"])
        if (any(path in denied_set.path_set for path in (source, *source.parents))
                or source in denied_set.ancestor_paths
                or source_inode in denied_set.identities):
            raise ContainerError("mount_forbidden_source")
        if (self.role in {"models", "inputs", "journal", "outputs", "redis_socket", "lora_authority"}
                and not stat.S_ISDIR(source_info.st_mode)):
            raise ContainerError("mount_role_type_invalid")
        if self.role in {"bootstrap", "redis_credentials"} and not stat.S_ISREG(source_info.st_mode):
            raise ContainerError("mount_role_type_invalid")
        return {"Type": "bind", "Source": str(source), "Target": self.target,
                "ReadOnly": self.role in READONLY_ROLES,
                "BindOptions": {"Propagation": "rprivate", "NonRecursive": True, "CreateMountpoint": False}}


@dataclass(frozen=True)
class ContainerPolicy:
    image: ImageApproval
    mounts: tuple[MountGrant, ...]
    server_database: str
    server_api_key: str
    engine_socket: str
    uid: int
    gid: int
    memory_bytes: int
    nano_cpus: int
    pids_limit: int
    tmpfs_bytes: int
    gpu_uuids: tuple[str, ...] = ()
    additional_forbidden: tuple[str, ...] = ()

    def forbidden_sources(self):
        result = (self.server_database, self.server_api_key, self.engine_socket, *self.additional_forbidden)
        return compile_forbidden_sources(result)

    def spec(self, *, name, instance_id, epoch, intent_id, cgroup_parent):
        for value in (name, instance_id, epoch, intent_id):
            identifier(value)
        if (any(type(v) is not int or v <= 0 for v in (self.uid, self.gid, self.memory_bytes, self.nano_cpus, self.pids_limit, self.tmpfs_bytes))
                or self.pids_limit > 4096 or self.tmpfs_bytes > self.memory_bytes
                ):
            raise ContainerError("container_limits_invalid")
        if (not isinstance(self.gpu_uuids, tuple) or len(set(self.gpu_uuids)) != len(self.gpu_uuids)
                or any(not isinstance(value, str) or not re.fullmatch(r"GPU-[0-9a-fA-F-]{36}", value) for value in self.gpu_uuids)):
            raise ContainerError("gpu_mapping_invalid")
        roles = [mount.role for mount in self.mounts]
        if not REQUIRED_ROLES <= set(roles) or len(roles) != len(set(roles)):
            raise ContainerError("mount_roles_incomplete")
        mounts = [mount.verify(self.forbidden_sources()) for mount in self.mounts]
        for index, mount in enumerate(mounts):
            for other in mounts[index + 1:]:
                a, b = PurePosixPath(mount["Target"]), PurePosixPath(other["Target"])
                if a == b or a in b.parents or b in a.parents:
                    raise ContainerError("mount_targets_overlap")
                a, b = Path(mount["Source"]), Path(other["Source"])
                if a == b or a in b.parents or b in a.parents:
                    raise ContainerError("mount_sources_overlap")
        parent = PurePosixPath(cgroup_parent)
        if (not (parent.is_absolute() and ".." not in parent.parts and str(parent) == cgroup_parent)
                and not re.fullmatch(r"mediacenter-[0-9a-f]{32}\.slice", cgroup_parent)):
            raise ContainerError("cgroup_parent_invalid")
        requests = ([{"Driver": "cdi", "Count": 0,
                      "DeviceIDs": ["nvidia.com/gpu=" + value for value in self.gpu_uuids],
                      "Capabilities": None, "Options": None}] if self.gpu_uuids else [])
        environment = {value.split("=", 1)[0]: value.split("=", 1)[1] for value in self.image.environment}
        triton_cache = environment.get("TRITON_CACHE_DIR")
        if triton_cache is not None and triton_cache != "/mc-triton-cache":
            raise ContainerError("image_environment_invalid")
        environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
        tmpfs = {
            "/tmp": f"rw,noexec,nosuid,nodev,size={self.tmpfs_bytes},mode=1777",
            "/dev/shm": f"rw,noexec,nosuid,nodev,size={self.tmpfs_bytes},mode=1777",
        }
        if triton_cache is not None:
            cache_bytes = min(self.tmpfs_bytes, 2 * 1024 ** 3)
            tmpfs["/mc-triton-cache"] = (f"rw,exec,nosuid,nodev,size={cache_bytes},"
                                          f"mode=700,uid={self.uid},gid={self.gid}")
        return {"Image": self.image.image_id, "User": f"{self.uid}:{self.gid}",
                "Entrypoint": list(self.image.entrypoint), "Cmd": list(self.image.command),
                "Env": [key + "=" + value for key, value in environment.items()],
                "WorkingDir": "/", "Healthcheck": {"Test": ["NONE"]},
                "AttachStdin": False, "AttachStdout": False, "AttachStderr": False,
                "Tty": False, "OpenStdin": False,
                "Labels": {"mediacenter.intent": intent_id, "mediacenter.instance": instance_id,
                           "mediacenter.epoch": epoch},
                "HostConfig": {"ReadonlyRootfs": True, "Privileged": False, "CapDrop": ["ALL"],
                    "CapAdd": [], "SecurityOpt": ["no-new-privileges:true"], "PidMode": "",
                    "IpcMode": "none", "CgroupnsMode": "private", "NetworkMode": "none",
                    "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0}, "AutoRemove": False,
                    "Memory": self.memory_bytes, "MemorySwap": self.memory_bytes,
                    "NanoCpus": self.nano_cpus, "PidsLimit": self.pids_limit,
                    "Tmpfs": tmpfs,
                    "Mounts": mounts, "DeviceRequests": requests, "Devices": [],
                    "CgroupParent": cgroup_parent, "PortBindings": {}, "PublishAllPorts": False}}


# Exact MC035 frozen contract, extracted from archive SHA 7718ead19b91193c4d165705c0ddefe477be0263f6f6a3da454aa1a9384bd6a9.
# This is recovery data, never an alternate capability accepted for launch.
HISTORICAL_SDXL_DIGEST = 'ad1362920fcb779fc48b6e3fec8bdbfb78247b37910a56930e0190cb991ef7da'
_HISTORICAL_SDXL_JSON = "{\"constraints\":[],\"family\":\"sdxl\",\"lora\":{\"maximum\":0,\"maximum_weight\":0.0,\"minimum_weight\":0.0,\"supported\":false},\"model_key\":\"sdxl-base-1.0\",\"operation\":\"image.generate\",\"options\":[{\"advanced\":false,\"default\":42,\"key\":\"seed\",\"label\":\"随机种子\",\"maximum\":2147483647,\"minimum\":0,\"step\":1,\"type\":\"integer\"},{\"advanced\":true,\"default\":\"\",\"key\":\"negative_prompt\",\"label\":\"负面提示词\",\"max_length\":4000,\"placeholder\":\"描述不希望出现的内容\",\"type\":\"string\"},{\"advanced\":false,\"default\":1024,\"key\":\"width\",\"label\":\"宽度\",\"maximum\":1536,\"minimum\":512,\"step\":64,\"type\":\"integer\"},{\"advanced\":false,\"default\":1024,\"key\":\"height\",\"label\":\"高度\",\"maximum\":1536,\"minimum\":512,\"step\":64,\"type\":\"integer\"},{\"advanced\":true,\"default\":25,\"key\":\"steps\",\"label\":\"推理步数\",\"maximum\":80,\"minimum\":1,\"step\":1,\"type\":\"integer\"},{\"advanced\":true,\"default\":7.0,\"key\":\"guidance_scale\",\"label\":\"提示词引导\",\"maximum\":20.0,\"minimum\":0.0,\"step\":0.5,\"type\":\"number\"}],\"reference_media\":{\"accept\":[],\"maximum\":0,\"minimum\":0,\"supported\":false},\"schema\":\"mc.capability/1\"}"


def historical_sdxl_capability():
    value = json.loads(_HISTORICAL_SDXL_JSON)
    if digest(value) != HISTORICAL_SDXL_DIGEST:
        raise ContainerError('runtime_historical_contract_corrupt')
    return value


@dataclass(frozen=True)
class PreparedRuntime:
    """Operator-prepared epoch package; no default image or generated model CLI."""
    package_id: str
    instance_id: str
    epoch: str
    binding: dict
    engine: EngineConfig
    policy: ContainerPolicy
    redis_options: dict
    bootstrap_sha256: str
    journal_file: str
    server_id: str
    record_id: str | None = None
    generation: int | None = None
    boundary_check: object = field(default=None, repr=False, compare=False)
    launch_check: object = field(default=None, repr=False, compare=False)
    recovery_capability_digest: str | None = field(default=None, repr=False)

    def recovery_capability(self):
        from .capabilities import worker_capability_for
        if self.recovery_capability_digest is not None:
            if (self.recovery_capability_digest != HISTORICAL_SDXL_DIGEST or self.binding['model_key'] != 'sdxl-base-1.0'
                    or self.record_id is None or self.boundary_check is None or self.launch_check is None):
                raise ContainerError('runtime_historical_contract_untrusted')
            return historical_sdxl_capability()
        return worker_capability_for(self.binding['model_key'])

    def verify(self):
        from .capabilities import worker_capability_for
        if self.boundary_check is not None:
            self.boundary_check()
        for value in (self.package_id, self.instance_id, self.epoch, self.server_id):
            identifier(value)
        control_secret = checked_path(self.redis_options["secret_file"])
        if not control_secret.is_file():
            raise ContainerError("runtime_control_secret_invalid")
        forbidden = self.policy.forbidden_sources().with_values(str(control_secret))
        verified_mounts = {grant.role: grant.verify(forbidden)
                           for grant in self.policy.mounts}
        bootstrap = next((grant for grant in self.policy.mounts if grant.role == "bootstrap"), None)
        if bootstrap is None:
            raise ContainerError("runtime_bootstrap_missing")
        # The loop above already verified bootstrap against the stricter deny
        # set (including this epoch's control secret).  Open the same path with
        # O_NOFOLLOW and bind the read to the captured inode, avoiding two more
        # complete path walks while also closing the check/open race.
        bootstrap_source = verified_mounts["bootstrap"]["Source"]
        descriptor = os.open(bootstrap_source,
                             os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(descriptor)
            observed_identity = {"device": info.st_dev, "inode": info.st_ino,
                                 "mode": stat.S_IFMT(info.st_mode)}
            if observed_identity != bootstrap.identity:
                raise ContainerError("mount_identity_changed")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                raw = handle.read(65537)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != self.bootstrap_sha256:
            raise ContainerError("runtime_bootstrap_changed")
        value = json.loads(raw)
        capability = self.recovery_capability()
        worker_binding = {key: self.binding[key] for key in ("model_key", "recipe_revision", "model_asset_id", "model_asset_revision")}
        worker_binding.update(image_digest=self.policy.image.reference.split("@")[-1],
                              gpu_uuids=list(self.policy.gpu_uuids), capability_digest=digest(capability))
        expected = {"server_id": self.server_id, "instance_id": self.instance_id, "worker_epoch": self.epoch, "binding": worker_binding}
        if any(value.get(key) != item for key, item in expected.items()):
            raise ContainerError("runtime_bootstrap_binding_mismatch")
        if value.get("recovery_complete") is not True or value.get("clock_trusted") is not True:
            raise ContainerError("runtime_bootstrap_not_authorized")
        result = {"package_id": self.package_id, "epoch": self.epoch, "binding_digest": digest(self.binding),
                "image_digest": worker_binding["image_digest"], "capability_digest": digest(capability),
                "bootstrap_sha256": self.bootstrap_sha256}
        if self.record_id is not None:
            identifier(self.record_id)
            if type(self.generation) is not int or self.generation <= 0:
                raise ContainerError('runtime_generation_invalid')
            result.update(runtime_record_id=self.record_id, generation=self.generation)
        return result

    def controller(self, repository):
        from .container_runtime import UnixEngine, CgroupObserver
        from .runtime_controller import RuntimeController
        self.verify()
        return RuntimeController(repository, UnixEngine(self.engine), CgroupObserver(self.engine), self.policy, launch_check=self.launch_check)

    def journal_path(self):
        self.verify()
        grant = next((item for item in self.policy.mounts if item.role == "journal"), None)
        path = PurePosixPath(self.journal_file)
        if grant is None or path.is_absolute() or not path.parts or ".." in path.parts:
            raise ContainerError("runtime_journal_path_invalid")
        grant.verify(self.policy.forbidden_sources())
        return checked_path(Path(grant.source).joinpath(*path.parts))

    def outputs_path(self):
        self.verify()
        grant = next((item for item in self.policy.mounts if item.role == "outputs"), None)
        if grant is None:
            raise ContainerError("runtime_outputs_missing")
        grant.verify(self.policy.forbidden_sources())
        return checked_path(grant.source)

    def transport(self, server_id):
        from .redis_transport import RedisEndpoint, RedisTransport
        from .transport import Identity
        self.verify()
        if server_id != self.server_id:
            raise ContainerError("runtime_bootstrap_server_mismatch")
        return RedisTransport(Identity(server_id, self.instance_id, self.epoch), RedisEndpoint(**self.redis_options),
                              role="server", consumer="controller-" + self.epoch)


def load_runtime_configuration(path, *, database, api_key_file, include_installation=False):
    """Read an explicit private Server config. Never accept this from an HTTP task.

    Environment-only API keys support the control UI, but cannot provision
    containers until their real secret source is explicitly configured.
    """
    source = checked_path(path)
    info = source.stat()
    if not source.is_file() or info.st_size > 1024 * 1024 or os.name == "posix" and info.st_mode & 0o077:
        raise ContainerError("runtime_config_not_private")
    if api_key_file is None:
        raise ContainerError("runtime_api_key_source_required")
    secret = checked_path(api_key_file)
    value = json.loads(source.read_bytes())
    if set(value) not in ({"server_id", "gpu_uuids", "engine"}, {"server_id", "gpu_uuids", "engine", "installation"}):
        raise ContainerError("runtime_config_invalid")
    identifier(value["server_id"])
    engine = EngineConfig(**value["engine"])
    basic = (value["server_id"], tuple(value["gpu_uuids"]))
    return (*basic, (engine, value.get('installation'))) if include_installation else basic


def _register_runtime_profiles(repository, images, declarations, profiles):
    """Bind trusted Runtime Profile revisions to one approved OCI release."""
    from .container_releases import RuntimeRelease, RuntimeContractError
    from .runtime_profiles import RuntimeProfileManager, RuntimeProfileError

    if type(profiles) is not list:
        raise ContainerError('runtime_profiles_configuration_invalid')
    releases = [RuntimeRelease(item) for item in declarations]
    manager = RuntimeProfileManager(repository)
    result = []
    for profile in profiles:
        if type(profile) is not dict:
            raise ContainerError('runtime_profiles_configuration_invalid')
        matching = [release for release in releases
                    if release.image_digest == profile.get('image_digest')]
        try:
            if (len(matching) != 1
                    or matching[0].data['adapter_id'] != profile.get('profile_id')):
                raise ContainerError('runtime_profile_release_mismatch')
            images.release(matching[0].digest)
            result.append(manager.register(profile))
        except (RuntimeContractError, RuntimeProfileError) as exc:
            raise ContainerError(getattr(exc, 'code', 'runtime_profile_configuration_invalid')) from None
    return result


def installation_components(repository, server_id, engine, value, *, models_root, inputs_root, api_key_file):
    """Explicit Server-only trusted configuration; never accepts HTTP fields."""
    from .runtime_artifacts import RuntimeArtifactStore, UnixRuntimeImporter
    from .runtime_provisioning import InstallationRuntime, EpochPublisher, EpochProvisioner
    from .container_runtime import UnixEngine
    from .redis_transport import RedisEndpoint
    if value is None: return None, None, None
    base_keys = {'releases','approved_release_digests','templates','image_store','download_hosts','publisher','package_root'}
    optional_keys = {'lora', 'local_artifact_roots', 'local_artifacts', 'runtime_profiles'}
    if (type(value) is not dict or not base_keys <= set(value) or not set(value) <= base_keys | optional_keys
            or type(value['publisher']) is not dict or set(value['publisher']) != {'endpoint','seed_file','state_root'}):
        raise ContainerError('installation_configuration_invalid')
    images = RuntimeArtifactStore(repository, value['image_store'], approved_digests=value['approved_release_digests'],
                                  allowed_hosts=value['download_hosts'],
                                  local_artifact_roots=value.get('local_artifact_roots', ()),
                                  local_artifacts=value.get('local_artifacts', {}))
    for release in value['releases']: images.register(release)
    _register_runtime_profiles(
        repository, images, value['releases'], value.get('runtime_profiles', []))
    installations = InstallationRuntime(repository, images, value['templates'])
    from .runtime_provisioning import LoraAuthority
    lora = value.get('lora')
    if lora is not None:
        if type(lora) is not dict or set(lora) != {'root'}:
            raise ContainerError('lora_configuration_invalid')
        installations.lora_authority = LoraAuthority(repository, lora['root'])
    publisher = value['publisher']
    acl = EpochPublisher(repository, server_id, RedisEndpoint(**publisher['endpoint']), publisher['seed_file'], publisher['state_root'])
    try:
        provisioner = EpochProvisioner(installations, acl, engine, value['package_root'], models_root=models_root,
                                      inputs_root=inputs_root, api_key_file=api_key_file)
        importer = UnixRuntimeImporter(UnixEngine(engine))
    except Exception:
        acl.close(); raise
    return installations, importer, provisioner
