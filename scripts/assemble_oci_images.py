#!/usr/bin/env python3
"""Assemble fixed OCI images without a privileged daemon.

This production path adopts already verified MediaCenter environments.  It
relocates them to /opt/python before assembly, downloads only a digest-verified
Ubuntu base closure, and emits standard single-platform OCI-layout tar files.
It never reads model weights, invokes a GPU, imports an image, or prunes state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
import zipfile


CREATED = "2026-08-11T00:00:00Z"
SOURCE_EPOCH = "1786406400"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
PACKAGING_24_WHEEL = "packaging-24.2-py3-none-any.whl"
PACKAGING_24_SHA256 = "fdbc12e70e25c89b3e95a853f09065c629ebd6add9bf7a788ce6cfdb35b5c7b0"
PROFILE_BY_IMAGE = {
    "runtime-v1": "runtime-v1", "runtime-v0": "runtime-v0",
    "sdxl-base-1.0": "image", "illustrious-xl-v2.0": "image",
    "z-image-turbo": "zimage", "qwen-image-2512": "qwen-image",
    "realesrgan": "realesrgan", "wan2.1-t2v-1.3b": "runtime-v1",
    "video-models": "h3", "musicgen-small": "music",
    "cosyvoice2-0.5b": "runtime-v0",
}
ENTRYPOINT_BY_IMAGE = {
    "sdxl-base-1.0": ["/opt/python/bin/python", "-B", "-u", "-m", "mediacenter.worker_cli"],
    "illustrious-xl-v2.0": ["/opt/python/bin/python", "-B", "-u", "-m", "mediacenter.image_worker_cli"],
    "z-image-turbo": ["/opt/python/bin/python", "-B", "-u", "-m", "mediacenter.image_worker_cli"],
    "qwen-image-2512": ["/opt/python/bin/python", "-B", "-u", "-m", "mediacenter.image_worker_cli"],
    "realesrgan": ["/opt/python/bin/python", "-B", "-u", "-m", "mediacenter.image_worker_cli"],
    "wan2.1-t2v-1.3b": ["/opt/python/bin/python", "-B", "-u", "-m", "mediacenter.video_worker_cli"],
    "video-models": ["/opt/python/bin/python", "-B", "-u", "-m", "mediacenter.video_worker_cli"],
    "musicgen-small": ["/opt/python/bin/python", "-B", "-u", "-m", "mediacenter.audio_worker_cli"],
    "cosyvoice2-0.5b": ["/opt/python/bin/python", "-B", "-u", "-m", "mediacenter.audio_worker_cli"],
}


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()


def sha_stream(stream) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
        digest.update(block); size += len(block)
    return digest.hexdigest(), size


def sha_file(path: Path) -> tuple[str, int]:
    with path.open("rb") as stream:
        return sha_stream(stream)


def source_tree_digest(root: Path) -> str:
    """Hash every layer-producing input that survives deterministic tar normalization."""
    root = safe_root(root)
    digest = hashlib.sha256()
    paths = [root, *sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())]
    for path in paths:
        relative = "." if path == root else path.relative_to(root).as_posix()
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISDIR(metadata.st_mode):
            row = {"path": relative, "type": "directory", "mode": mode}
        elif stat.S_ISREG(metadata.st_mode):
            content_sha256, size = sha_file(path)
            row = {"path": relative, "type": "file", "mode": mode,
                   "sha256": content_sha256, "bytes": size}
        elif stat.S_ISLNK(metadata.st_mode):
            row = {"path": relative, "type": "symlink", "mode": mode,
                   "target": os.readlink(path)}
        else:
            raise ValueError("oci_layer_source_type_unsupported")
        digest.update(canonical(row) + b"\n")
    return "sha256:" + digest.hexdigest()


def tar_source_tree_digest(raw: Path) -> str:
    """Recover the normalized source identity from a legacy deterministic layer tar."""
    digest = hashlib.sha256()
    files = {}
    with tarfile.open(raw, mode="r:") as archive:
        for member in archive:
            relative = member.name
            while relative.startswith("./"):
                relative = relative[2:]
            relative = relative.rstrip("/") or "."
            mode = member.mode & 0o7777
            if member.isdir():
                row = {"path": relative, "type": "directory", "mode": mode}
            elif member.isfile():
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("oci_reusable_layer_tar_invalid")
                content_sha256, size = sha_stream(stream)
                files[relative] = (content_sha256, size)
                row = {"path": relative, "type": "file", "mode": mode,
                       "sha256": content_sha256, "bytes": size}
            elif member.islnk():
                target = member.linkname
                while target.startswith("./"):
                    target = target[2:]
                if target not in files:
                    raise ValueError("oci_reusable_layer_hardlink_invalid")
                content_sha256, size = files[target]
                files[relative] = (content_sha256, size)
                row = {"path": relative, "type": "file", "mode": mode,
                       "sha256": content_sha256, "bytes": size}
            elif member.issym():
                row = {"path": relative, "type": "symlink", "mode": mode,
                       "target": member.linkname}
            else:
                raise ValueError("oci_reusable_layer_tar_type_unsupported")
            digest.update(canonical(row) + b"\n")
    return "sha256:" + digest.hexdigest()


def exclusive(path: Path, data: bytes, mode=0o600):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def safe_root(path: Path, *, existing=True) -> Path:
    path = path.absolute()
    resolved = path.resolve(strict=existing)
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("oci_path_symlink_rejected")
    return resolved


def copy_regular_tree(source: Path, target: Path, *, ignored_external_symlinks=()):
    source = safe_root(source)
    if target.exists():
        raise ValueError("oci_stage_not_exclusive")
    ignored = {PurePosixPath(value) for value in ignored_external_symlinks}
    seen_ignored = set()
    for root, directories, files in os.walk(source, followlinks=False):
        base = Path(root)
        for name in directories + files:
            path = base / name
            if not path.is_symlink():
                continue
            relative = PurePosixPath(path.relative_to(source).as_posix())
            raw_target = os.readlink(path)
            resolved = ((path.parent / raw_target) if not os.path.isabs(raw_target)
                        else Path(raw_target)).resolve(strict=False)
            try:
                resolved.relative_to(source)
            except ValueError:
                if relative not in ignored:
                    raise ValueError("oci_source_symlink_escape_rejected")
                seen_ignored.add(relative)
    if seen_ignored != ignored:
        raise ValueError("oci_expected_external_symlink_missing")

    def ignore_external(directory, names):
        directory = Path(directory)
        return [name for name in names
                if PurePosixPath((directory / name).relative_to(source).as_posix()) in ignored]

    shutil.copytree(source, target, symlinks=True, ignore=ignore_external)
    # Candidate and adopted upstream roots are deliberately private on the
    # host.  OCI workers run as uid/gid 1000, so host staging modes must not
    # leak into the image and make otherwise present code unimportable.
    def copied_paths():
        yield target
        yield from target.rglob("*")
    for path in copied_paths():
        if path.is_symlink():
            continue
        if path.is_dir():
            os.chmod(path, 0o755)
        elif path.is_file():
            source_mode = path.stat().st_mode & 0o777
            os.chmod(path, 0o755 if source_mode & 0o111 else 0o644)


class Registry:
    def __init__(self, root: Path, maximum_bytes: int):
        self.root, self.maximum, self.downloaded = root, maximum_bytes, 0
        root.mkdir(mode=0o700)

    def _request(self, url, *, token=None, accept=None):
        headers = {"User-Agent": "MediaCenter-MC044/1"}
        if token: headers["Authorization"] = "Bearer " + token
        if accept: headers["Accept"] = accept
        return urlopen(Request(url, headers=headers), timeout=30)

    def _json(self, url, **kwargs):
        with self._request(url, **kwargs) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024: raise ValueError("registry_metadata_oversize")
            return json.loads(raw), response.headers.get("Docker-Content-Digest")

    def blob(self, token: str, digest: str, repository="ubuntu") -> Path:
        if not DIGEST.fullmatch(digest): raise ValueError("registry_digest_invalid")
        target = self.root / digest.split(":", 1)[1]
        if target.exists():
            actual, _ = sha_file(target)
            if actual != digest.split(":", 1)[1]: raise ValueError("registry_cache_changed")
            return target
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", repository):
            raise ValueError("registry_repository_invalid")
        url = "https://registry-1.docker.io/v2/library/" + repository + "/blobs/" + digest
        with self._request(url, token=token) as response:
            declared = response.headers.get("Content-Length")
            if declared is None or not declared.isdigit(): raise ValueError("registry_size_missing")
            size = int(declared)
            if self.downloaded + size > self.maximum: raise ValueError("registry_download_budget_exceeded")
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                digestor, received = hashlib.sha256(), 0
                with os.fdopen(descriptor, "wb", closefd=False) as output:
                    while received < size:
                        block = response.read(min(1024 * 1024, size - received))
                        if not block: raise ValueError("registry_download_short")
                        output.write(block); digestor.update(block); received += len(block)
                    output.flush(); os.fsync(output.fileno())
            finally:
                os.close(descriptor)
            if received != size or digestor.hexdigest() != digest.split(":", 1)[1]:
                raise ValueError("registry_blob_digest_mismatch")
            self.downloaded += received
        return target

    def ubuntu(self):
        query = urlencode({"service": "registry.docker.io", "scope": "repository:library/ubuntu:pull"})
        token, _ = self._json("https://auth.docker.io/token?" + query)
        token = token["token"]
        accept = ", ".join(("application/vnd.oci.image.index.v1+json",
                            "application/vnd.docker.distribution.manifest.list.v2+json",
                            "application/vnd.oci.image.manifest.v1+json",
                            "application/vnd.docker.distribution.manifest.v2+json"))
        index, _ = self._json("https://registry-1.docker.io/v2/library/ubuntu/manifests/24.04",
                              token=token, accept=accept)
        manifests = index.get("manifests")
        if not isinstance(manifests, list): raise ValueError("registry_index_required")
        matches = [item for item in manifests if item.get("platform") == {"architecture":"amd64","os":"linux"}]
        if len(matches) != 1 or not DIGEST.fullmatch(matches[0].get("digest", "")):
            raise ValueError("registry_platform_ambiguous")
        manifest, declared = self._json(
            "https://registry-1.docker.io/v2/library/ubuntu/manifests/" + matches[0]["digest"],
            token=token, accept=accept)
        raw = canonical(manifest)
        actual = "sha256:" + hashlib.sha256(raw).hexdigest()
        # Registries may serialize insignificant whitespace differently. The
        # immutable response header and selected index descriptor must agree.
        if declared != matches[0]["digest"]:
            raise ValueError("registry_manifest_identity_mismatch")
        config = manifest.get("config", {})
        layers = manifest.get("layers")
        if (manifest.get("schemaVersion") != 2 or not DIGEST.fullmatch(config.get("digest", ""))
                or not isinstance(layers, list) or not layers):
            raise ValueError("registry_manifest_invalid")
        config_path = self.blob(token, config["digest"])
        base_config = json.loads(config_path.read_bytes())
        layer_rows = []
        for item in layers:
            if (not DIGEST.fullmatch(item.get("digest", "")) or type(item.get("size")) is not int
                    or item.get("mediaType") not in {
                        "application/vnd.oci.image.layer.v1.tar+gzip",
                        "application/vnd.docker.image.rootfs.diff.tar.gzip"}):
                raise ValueError("registry_layer_invalid")
            path = self.blob(token, item["digest"])
            if path.stat().st_size != item["size"]: raise ValueError("registry_layer_size_mismatch")
            layer_rows.append(dict(item, path=path))
        diff_ids = base_config.get("rootfs", {}).get("diff_ids")
        if not isinstance(diff_ids, list) or len(diff_ids) != len(layer_rows):
            raise ValueError("registry_rootfs_invalid")
        return {"manifest_digest": declared, "config": base_config,
                "config_digest": config["digest"], "config_path": config_path,
                "layers": layer_rows, "diff_ids": diff_ids,
                "downloaded_bytes": self.downloaded}


def load_prefetched_base(root: Path):
    root = safe_root(root)
    value = json.loads((root / "closure.json").read_bytes())
    if (type(value) is not dict or set(value) != {"schema", "manifest_digest", "config_digest", "layers", "diff_ids"}
            or value["schema"] != "mc.oci-base-closure/1"
            or not DIGEST.fullmatch(value["manifest_digest"])
            or not DIGEST.fullmatch(value["config_digest"])
            or type(value["layers"]) is not list or not value["layers"]
            or type(value["diff_ids"]) is not list or len(value["diff_ids"]) != len(value["layers"])):
        raise ValueError("prefetched_base_contract_invalid")
    registry = root / "registry"
    config_path = registry / value["config_digest"].split(":", 1)[1]
    if sha_file(config_path)[0] != value["config_digest"].split(":", 1)[1]:
        raise ValueError("prefetched_base_config_changed")
    config = json.loads(config_path.read_bytes())
    if config.get("rootfs", {}).get("diff_ids") != value["diff_ids"]:
        raise ValueError("prefetched_base_rootfs_changed")
    layers = []
    for row in value["layers"]:
        if (type(row) is not dict or set(row) != {"mediaType", "digest", "size"}
                or not DIGEST.fullmatch(row.get("digest", ""))
                or type(row.get("size")) is not int or row["size"] <= 0
                or row.get("mediaType") not in {"application/vnd.oci.image.layer.v1.tar+gzip",
                                                 "application/vnd.docker.image.rootfs.diff.tar.gzip"}):
            raise ValueError("prefetched_base_layer_invalid")
        path = registry / row["digest"].split(":", 1)[1]
        actual, size = sha_file(path)
        if actual != row["digest"].split(":", 1)[1] or size != row["size"]:
            raise ValueError("prefetched_base_layer_changed")
        layers.append(dict(row, path=path))
    return {"manifest_digest": value["manifest_digest"], "config": config,
            "config_digest": value["config_digest"], "config_path": config_path,
            "layers": layers, "diff_ids": value["diff_ids"], "downloaded_bytes": 0}


def make_layer(stage: Path, cache: Path, name: str):
    stage = safe_root(stage)
    source_digest = source_tree_digest(stage)
    directory = cache / "layers"
    directory.mkdir(mode=0o700, exist_ok=True)
    raw, compressed, metadata = (directory / (name + suffix)
                                 for suffix in (".tar", ".tar.zst", ".json"))
    if any(path.exists() for path in (raw, compressed, metadata)):
        raise ValueError("oci_layer_output_not_exclusive")
    subprocess.run(["tar", "--sort=name", "--format=posix",
                    "--pax-option=delete=atime,delete=ctime", "--mtime=@" + SOURCE_EPOCH,
                    "--owner=0", "--group=0", "--numeric-owner", "-cf", str(raw),
                    "-C", str(stage), "."], check=True, stdin=subprocess.DEVNULL)
    raw_sha, raw_size = sha_file(raw)
    subprocess.run(["zstd", "-T0", "-10", "--no-progress", "-o", str(compressed), str(raw)],
                   check=True, stdin=subprocess.DEVNULL)
    compressed_sha, compressed_size = sha_file(compressed)
    value = {"name": name, "source_digest": source_digest,
             "diff_id": "sha256:" + raw_sha, "uncompressed_bytes": raw_size,
             "digest": "sha256:" + compressed_sha, "bytes": compressed_size,
             "mediaType": "application/vnd.oci.image.layer.v1.tar+zstd"}
    exclusive(metadata, canonical(value) + b"\n")
    return dict(value, path=compressed)


def reuse_layer(cache: Path, name: str, *, expected_source_digest: str):
    cache = safe_root(cache)
    directory = cache / "layers"
    raw, compressed, metadata = (directory / (name + suffix)
                                 for suffix in (".tar", ".tar.zst", ".json"))
    value = json.loads(metadata.read_bytes())
    legacy_fields = {"name", "diff_id", "uncompressed_bytes", "digest", "bytes", "mediaType"}
    current_fields = legacy_fields | {"source_digest"}
    if (type(value) is not dict
            or frozenset(value) not in {frozenset(legacy_fields), frozenset(current_fields)}
            or value["name"] != name
            or ("source_digest" in value and not DIGEST.fullmatch(value["source_digest"]))
            or not DIGEST.fullmatch(value["diff_id"])
            or not DIGEST.fullmatch(value["digest"])
            or value["mediaType"] != "application/vnd.oci.image.layer.v1.tar+zstd"):
        raise ValueError("oci_reusable_layer_contract_invalid")
    raw_sha, raw_size = sha_file(raw)
    compressed_sha, compressed_size = sha_file(compressed)
    if (value["diff_id"] != "sha256:" + raw_sha
            or value["uncompressed_bytes"] != raw_size
            or value["digest"] != "sha256:" + compressed_sha
            or value["bytes"] != compressed_size):
        raise ValueError("oci_reusable_layer_changed")
    source_digest = value.get("source_digest") or tar_source_tree_digest(raw)
    if source_digest != expected_source_digest:
        raise ValueError("oci_reusable_layer_source_changed")
    value["source_digest"] = source_digest
    return dict(value, path=compressed)


def reuse_or_make_layer(stage: Path, cache: Path, name: str, reusable_cache: Path | None):
    if reusable_cache is None:
        return make_layer(stage, cache, name)
    expected_source_digest = source_tree_digest(stage)
    try:
        value = reuse_layer(reusable_cache, name,
                            expected_source_digest=expected_source_digest)
    except ValueError as error:
        if str(error) != "oci_reusable_layer_source_changed":
            raise
        return make_layer(stage, cache, name)
    source_directory = safe_root(reusable_cache) / "layers"
    target_directory = cache / "layers"
    target_directory.mkdir(mode=0o700, exist_ok=True)
    raw = target_directory / (name + ".tar")
    compressed = target_directory / (name + ".tar.zst")
    metadata = target_directory / (name + ".json")
    if any(path.exists() for path in (raw, compressed, metadata)):
        raise ValueError("oci_layer_output_not_exclusive")
    os.link(source_directory / (name + ".tar"), raw)
    os.link(source_directory / (name + ".tar.zst"), compressed)
    portable = {key: item for key, item in value.items() if key != "path"}
    exclusive(metadata, canonical(portable) + b"\n")
    return dict(portable, path=compressed)


def wheel_stage(context: Path, root: Path, environment_root: Path, profile: str,
                *, runtime_v0_packaging_wheel: Path | None = None):
    target = root / ("wheels-" + profile)
    if target.exists(): raise ValueError("wheel_stage_not_exclusive")
    python_roots = [path for path in (environment_root / profile / "opt/python/lib").glob("python*/site-packages")
                    if not path.parent.is_symlink() and any(path.iterdir())]
    if len(python_roots) != 1: raise ValueError("python_site_packages_ambiguous")
    relative_site = python_roots[0].relative_to(environment_root / profile)
    site = target / relative_site
    site.mkdir(parents=True)
    wheels = sorted((context / profile / "wheels").glob("*.whl"))
    if runtime_v0_packaging_wheel is not None:
        if profile != "runtime-v0": raise ValueError("packaging_repair_profile_invalid")
        wheel = safe_root(runtime_v0_packaging_wheel)
        actual, _size = sha_file(wheel)
        if wheel.name != PACKAGING_24_WHEEL or actual != PACKAGING_24_SHA256:
            raise ValueError("packaging_repair_wheel_invalid")
        # The adopted Python environment contains a partially overwritten
        # packaging distribution. OCI whiteouts replace both lower-layer
        # directories atomically before the exact locked wheel is applied.
        (site / ".wh.packaging").touch(mode=0o644)
        (site / ".wh.packaging-24.2.dist-info").touch(mode=0o644)
        wheels.append(wheel)
    if not wheels: raise ValueError("wheel_overlay_empty")
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            for info in archive.infolist():
                path = PurePosixPath(info.filename)
                if (path.is_absolute() or not path.parts or ".." in path.parts
                        or "\\" in info.filename or (info.external_attr >> 16) & 0o170000 == 0o120000):
                    raise ValueError("wheel_path_invalid")
                parts = list(path.parts)
                if len(parts) >= 3 and parts[0].endswith(".data") and parts[1] in {"purelib", "platlib"}:
                    parts = parts[2:]
                elif parts[0].endswith(".data"):
                    raise ValueError("wheel_data_role_unsupported")
                destination = site.joinpath(*parts)
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True); continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists(): raise ValueError("wheel_overlay_duplicate")
                with archive.open(info) as source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output)
                mode = (info.external_attr >> 16) & 0o777
                os.chmod(destination, mode or 0o644)
    # Build hosts use a private umask. Layer contents are nevertheless runtime
    # artifacts for uid/gid 1000 and must be traversable without leaking the
    # host's staging permissions into the image.
    for path in (target, *target.rglob("*")):
        if path.is_symlink():
            continue
        if path.is_dir():
            os.chmod(path, 0o755)
        elif path.is_file():
            mode = path.stat().st_mode & 0o777
            os.chmod(path, 0o755 if mode & 0o111 else 0o644)
    return target


def _distribution_identity(metadata: bytes) -> tuple[str, str]:
    fields = {}
    for line in metadata.decode("utf-8").splitlines():
        if not line:
            break
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.lower()] = value.strip()
    if not fields.get("name") or not fields.get("version"):
        raise ValueError("oci_distribution_metadata_invalid")
    name = re.sub(r"[-_.]+", "-", fields["name"].lower())
    return name, fields["version"]


def validate_locked_profile_requirements(candidate: Path, environment_stage: Path,
                                         wheel_overlay: Path, profile: str):
    requirements_by_profile = {"h3": candidate / "containers/video-models/requirements.txt"}
    requirements = requirements_by_profile.get(profile)
    if requirements is None:
        return
    expected = {}
    for line in requirements.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^ ]+) --hash=sha256:[0-9a-f]{64}", line)
        if not match:
            raise ValueError("oci_profile_requirement_invalid")
        expected[re.sub(r"[-_.]+", "-", match.group(1).lower())] = match.group(2)
    observed = {}
    for root in (environment_stage, wheel_overlay):
        layer_observed = {}
        metadata_files = sorted(root.glob("opt/python/lib/python*/site-packages/*.dist-info/METADATA"))
        for metadata in metadata_files:
            name, version = _distribution_identity(metadata.read_bytes())
            previous = layer_observed.get(name)
            if previous is not None and previous != version:
                raise ValueError("oci_profile_distribution_ambiguous")
            layer_observed[name] = version
        # OCI upper layers intentionally replace selected distributions from
        # the adopted environment; ambiguity is forbidden within one layer,
        # while the overlay's exact locked version has normal precedence.
        observed.update(layer_observed)
    missing = sorted(name for name, version in expected.items() if observed.get(name) != version)
    if missing:
        raise ValueError("oci_profile_requirement_missing:" + ",".join(missing))


def sdk_stage(candidate: Path, root: Path):
    target = root / "sdk"
    target.mkdir()
    copy_regular_tree(candidate / "mediacenter", target / "opt/mediacenter/mediacenter")
    return target


def cosy_stage(contexts: Path, root: Path):
    target = root / "cosyvoice"
    target.mkdir()
    source = contexts / "runtime-v0/sources"
    copy_regular_tree(
        source / "CosyVoice",
        target / "opt/cosyvoice",
        # Upstream Matcha-TTS carries a developer-machine training-data link.
        # It is not runtime code and must never be followed into an OCI layer.
        ignored_external_symlinks=("third_party/Matcha-TTS/data",),
    )
    matcha = target / "opt/cosyvoice/third_party/Matcha-TTS"
    if matcha.exists():
        # The source tree already contains the fixed submodule. Verify the
        # separately frozen copy instead of replacing or deleting it. The one
        # upstream developer-machine data link was deliberately excluded from
        # the runtime layer above and therefore from this path-set comparison.
        left_root = source / "Matcha-TTS"
        left = sorted(p for p in left_root.rglob("*")
                      if p.relative_to(left_root).as_posix() != "data")
        right = sorted(matcha.rglob("*"))
        if [p.relative_to(left_root) for p in left] != [p.relative_to(matcha) for p in right]:
            raise ValueError("cosyvoice_matcha_tree_mismatch")
    else:
        copy_regular_tree(source / "Matcha-TTS", matcha)
    return target


def cosy_inference_prune_stage(environment_root: Path, root: Path):
    """Remove training-only DeepSpeed from the CosyVoice inference image."""
    target = root / "cosyvoice-inference-prune"
    python_roots = [path for path in (environment_root / "runtime-v0/opt/python/lib").glob("python*/site-packages")
                    if not path.parent.is_symlink() and any(path.iterdir())]
    if len(python_roots) != 1: raise ValueError("python_site_packages_ambiguous")
    source = python_roots[0]
    names = ("deepspeed", "deepspeed-0.15.1.dist-info")
    if any(not (source / name).is_dir() for name in names):
        raise ValueError("cosyvoice_deepspeed_identity_changed")
    metadata = (source / names[1] / "METADATA").read_text(encoding="utf-8", errors="strict")
    if "\nName: deepspeed\n" not in "\n" + metadata or "\nVersion: 0.15.1\n" not in "\n" + metadata:
        raise ValueError("cosyvoice_deepspeed_identity_changed")
    site = target / source.relative_to(environment_root / "runtime-v0")
    site.mkdir(parents=True, mode=0o755)
    for name in names:
        (site / (".wh." + name)).touch(mode=0o644)
    for path in (target, *target.rglob("*")):
        if path.is_dir(): os.chmod(path, 0o755)
        elif path.is_file(): os.chmod(path, 0o644)
    return target


def verified_compiler_root(root: Path, evidence_path: Path):
    root = safe_root(root); evidence_path = safe_root(evidence_path)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    rows = evidence.get("files")
    if evidence.get("schema") != "mc.host-compiler-layer/1" or evidence.get("status") != "passed" or not isinstance(rows, list):
        raise ValueError("compiler_evidence_invalid")
    expected = set()
    for row in rows:
        if type(row) is not dict or row.get("type") not in {"file", "symlink"}:
            raise ValueError("compiler_evidence_invalid")
        path = PurePosixPath(row.get("path", ""))
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise ValueError("compiler_evidence_path_invalid")
        target = root.joinpath(*path.parts); expected.add(path.as_posix())
        if row["type"] == "file":
            if not target.is_file() or target.is_symlink() or sha_file(target) != (row.get("sha256"), row.get("bytes")):
                raise ValueError("compiler_evidence_changed")
        elif not target.is_symlink() or os.readlink(target) != row.get("target"):
            raise ValueError("compiler_evidence_changed")
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file() or path.is_symlink()}
    if actual != expected: raise ValueError("compiler_evidence_path_set_changed")
    return root, hashlib.sha256(evidence_path.read_bytes()).hexdigest()


def link_blob(layout: Path, path: Path, digest: str):
    target = layout / "blobs/sha256" / digest.split(":", 1)[1]
    if not target.exists(): os.link(path, target)
    return target


def image_config(base, image_id, layers, environment, entrypoint, command):
    value = json.loads(json.dumps(base))
    value.update(created=CREATED, architecture="amd64", os="linux")
    config = value.setdefault("config", {})
    for key in ("Volumes", "OnBuild", "ExposedPorts"):
        config.pop(key, None)
    config.update(Env=environment, Entrypoint=entrypoint or None, Cmd=command or None,
                  WorkingDir="/opt/mediacenter", User="1000:1000")
    value["rootfs"] = {"type": "layers", "diff_ids": list(base["rootfs"]["diff_ids"]) + [x["diff_id"] for x in layers]}
    history = list(base.get("history", []))
    history.extend({"created": CREATED, "created_by": "MediaCenter MC-044 " + image_id,
                    "comment": layer["name"]} for layer in layers)
    value["history"] = history
    return value


def assemble_image(output: Path, layout_root: Path, base, row, layers, environment):
    image_id = row["id"]
    layout = layout_root / image_id
    layout.mkdir(parents=True)
    (layout / "blobs/sha256").mkdir(parents=True)
    for layer in base["layers"]: link_blob(layout, layer["path"], layer["digest"])
    for layer in layers: link_blob(layout, layer["path"], layer["digest"])
    entrypoint = ENTRYPOINT_BY_IMAGE.get(image_id, [])
    command = ["/opt/python/bin/python", "-B"] if image_id.startswith("runtime-") else []
    config = image_config(base["config"], image_id, layers, environment, entrypoint, command)
    config_raw = canonical(config); config_digest = "sha256:" + hashlib.sha256(config_raw).hexdigest()
    exclusive(layout / "blobs/sha256" / config_digest.split(":",1)[1], config_raw)
    descriptors = [{k: layer[k] for k in ("mediaType", "digest", "size")}
                   if "size" in layer else {"mediaType":layer["mediaType"],"digest":layer["digest"],"size":layer["bytes"]}
                   for layer in base["layers"] + layers]
    manifest = {"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json",
                "config":{"mediaType":"application/vnd.oci.image.config.v1+json",
                          "digest":config_digest,"size":len(config_raw)},"layers":descriptors}
    manifest_raw=canonical(manifest); manifest_digest="sha256:"+hashlib.sha256(manifest_raw).hexdigest()
    exclusive(layout / "blobs/sha256" / manifest_digest.split(":",1)[1], manifest_raw)
    index={"schemaVersion":2,"mediaType":"application/vnd.oci.image.index.v1+json",
           "manifests":[{"mediaType":"application/vnd.oci.image.manifest.v1+json",
                         "digest":manifest_digest,"size":len(manifest_raw),
                         "platform":{"architecture":"amd64","os":"linux"}}]}
    exclusive(layout / "oci-layout", canonical({"imageLayoutVersion":"1.0.0"}))
    exclusive(layout / "index.json", canonical(index))
    archive=output/row["archive"]
    subprocess.run(["tar","--sort=name","--format=ustar","--mtime=@"+SOURCE_EPOCH,
                    "--owner=0","--group=0","--numeric-owner","-cf",str(archive),
                    "-C",str(layout),"oci-layout","index.json","blobs"],check=True)
    archive_sha,archive_bytes=sha_file(archive)
    return {"id":image_id,"manifest_digest":manifest_digest,"config_digest":config_digest,
            "archive":str(archive),"archive_sha256":archive_sha,"archive_bytes":archive_bytes,
            "entrypoint":entrypoint,"command":command,"environment":environment}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute",action="store_true",required=True)
    for name in ("matrix","staging_root","candidate_root","contexts_root","cache_root","output_root","evidence"):
        parser.add_argument("--"+name.replace("_","-"),dest=name,type=Path,required=True)
    parser.add_argument("--artifact-base-url",required=True)
    parser.add_argument("--prefetched-base",type=Path)
    parser.add_argument("--reusable-layer-cache",type=Path)
    parser.add_argument("--rebuild-sdk",action="store_true")
    parser.add_argument("--runtime-v0-packaging-wheel",type=Path,required=True)
    parser.add_argument("--compiler-root",type=Path,required=True)
    parser.add_argument("--compiler-evidence",type=Path,required=True)
    parser.add_argument("--maximum-download-bytes",type=int,default=1024**3)
    args=parser.parse_args()
    started=time.monotonic()
    matrix=json.loads(safe_root(args.matrix).read_text(encoding="utf-8"))
    if matrix.get("schema")!="mc.production-image-build-matrix/1" or len(matrix.get("images",[]))!=11:
        raise ValueError("oci_matrix_invalid")
    if set(PROFILE_BY_IMAGE)!=set(row["id"] for row in matrix["images"]):
        raise ValueError("oci_profile_matrix_invalid")
    parsed=urlsplit(args.artifact_base_url)
    if parsed.scheme!="https" or not parsed.hostname or parsed.query or parsed.fragment or not parsed.path.endswith("/"):
        raise ValueError("artifact_base_url_invalid")
    staging=safe_root(args.staging_root);candidate=safe_root(args.candidate_root);contexts=safe_root(args.contexts_root)
    cache=safe_root(args.cache_root,existing=False);output=safe_root(args.output_root)
    if cache.exists() or any(output.iterdir()) or args.evidence.exists(): raise ValueError("oci_output_not_exclusive")
    cache.mkdir(mode=0o700);(cache/"stages").mkdir();(cache/"layouts").mkdir()
    os.chmod(output,0o700)
    report={"schema":"mc.oci-assembly-evidence/1","status":"running","images":[],"releases":[]}
    exclusive(args.evidence,canonical(report)+b"\n")
    if args.prefetched_base is not None:
        base=load_prefetched_base(args.prefetched_base)
    else:
        registry=Registry(cache/"registry",args.maximum_download_bytes)
        base=registry.ubuntu()
    layers={}
    for profile in sorted(set(PROFILE_BY_IMAGE.values())):
        environment_stage=staging/profile
        overlay=wheel_stage(contexts,cache/"stages",staging,profile,
            runtime_v0_packaging_wheel=(args.runtime_v0_packaging_wheel
                if profile == "runtime-v0" else None))
        validate_locked_profile_requirements(candidate,environment_stage,overlay,profile)
        layers[profile+":env"]=reuse_or_make_layer(
            environment_stage,cache,"env-"+profile,args.reusable_layer_cache)
        layers[profile+":wheels"]=reuse_or_make_layer(
            overlay,cache,"wheels-"+profile,args.reusable_layer_cache)
    sdk_source=sdk_stage(candidate,cache/"stages")
    sdk=(reuse_or_make_layer(sdk_source,cache,"sdk",args.reusable_layer_cache)
         if not args.rebuild_sdk else make_layer(sdk_source,cache,"sdk"))
    cosy=make_layer(cosy_stage(contexts,cache/"stages"),cache,"cosyvoice-sources")
    cosy_prune=make_layer(cosy_inference_prune_stage(staging,cache/"stages"),cache,"cosyvoice-inference-prune")
    compiler_root,compiler_evidence_sha256=verified_compiler_root(args.compiler_root,args.compiler_evidence)
    compiler=make_layer(compiler_root,cache,"video-compiler")
    base_env=["PATH=/opt/python/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
              "LD_LIBRARY_PATH=/opt/python/lib","PYTHONPATH=/opt/mediacenter",
              "PYTHONNOUSERSITE=1","PYTHONDONTWRITEBYTECODE=1","PIP_NO_INDEX=1",
              "HF_HUB_OFFLINE=1","TRANSFORMERS_OFFLINE=1","HF_DATASETS_OFFLINE=1","HOME=/tmp"]
    by_id={}
    for row in matrix["images"]:
        profile=PROFILE_BY_IMAGE[row["id"]]
        selected=[layers[profile+":env"],layers[profile+":wheels"],sdk]
        environment=list(base_env)
        if row["id"]=="cosyvoice2-0.5b":
            selected.extend((cosy_prune,cosy));environment[2]="PYTHONPATH=/opt/mediacenter:/opt/cosyvoice:/opt/cosyvoice/third_party/Matcha-TTS"
        if row["id"]=="video-models":
            selected.append(compiler)
            environment.append("TRITON_CACHE_DIR=/mc-triton-cache")
        result=assemble_image(output,cache/"layouts",base,row,selected,environment)
        report["images"].append(result);by_id[row["id"]]=result
        args.evidence.write_bytes(canonical(report)+b"\n")
    catalog=json.loads((candidate/"deploy/model_catalog.json").read_text(encoding="utf-8"))
    adapters={item["catalog_key"]:item["worker_contract"]["adapter_id"] for item in catalog["models"]}
    releases=output/"releases";releases.mkdir(mode=0o700)
    sdk_digest=sdk["diff_id"].split(":",1)[1]
    for model,image_id in sorted(matrix["model_bindings"].items()):
        image=by_id[image_id]
        declaration={"schema":1,"release_id":"mc044-"+model,"adapter_id":adapters[model],"sdk_digest":sdk_digest,
            "image":{"reference":"mediacenter.local/" + image_id + "@" + image["manifest_digest"],"image_id":image["config_digest"],"platform":"linux/amd64",
                     "entrypoint":image["entrypoint"],"command":image["command"],"environment":image["environment"]},
            "artifact":{"format":"oci-layout-tar","url":args.artifact_base_url+Path(image["archive"]).name,
                        "sha256":image["archive_sha256"],"byte_size":image["archive_bytes"]}}
        raw=canonical(declaration);path=releases/(model+".json");exclusive(path,raw+b"\n")
        report["releases"].append({"model_key":model,"path":str(path),
                                   "release_digest":hashlib.sha256(raw).hexdigest(),
                                   "image_digest":image["manifest_digest"]})
    report.update(status="complete",base_manifest_digest=base["manifest_digest"],
                  runtime_v0_packaging_wheel_sha256=PACKAGING_24_SHA256,
                  compiler_evidence_sha256=compiler_evidence_sha256,
                  downloaded_bytes=base["downloaded_bytes"],elapsed_seconds=round(time.monotonic()-started,3))
    args.evidence.write_bytes(canonical(report)+b"\n")
    print(canonical(report).decode())
    return 0


if __name__=="__main__": raise SystemExit(main())
