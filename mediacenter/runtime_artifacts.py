"""Bounded installation-time image acquisition, separate from model weights.

This module never runs containers or pulls from a task-supplied registry. Image
approval, downloaded archive verification and Engine import are distinct facts.
"""
from __future__ import annotations

import hashlib
import errno
import http.client
import json
import os
import re
import socket
import ssl
import stat
import tarfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, quote

from .artifacts import checked_path, open_regular, identity, fsync_directory, file_hash
from .model_assets import validate_https_url
from .repository import INSTALL_ACTIVE, InstallationOwnershipError
from .container_releases import RuntimeRelease, RuntimeContractError
from .config import ContainerError, ImageApproval, object_identity
from .task_state import canonical, digest, now


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise RuntimeContractError("runtime_json_duplicate_key")
        result[key] = value
    return result


def strict_json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, RecursionError):
        raise RuntimeContractError("runtime_json_invalid") from None


def verify_oci_archive(stream, release):
    """Read a single-platform OCI layout without extracting any archive path."""
    contract = release.data
    expected = contract["artifact"]
    if file_hash(stream, expected["byte_size"]) != (expected["sha256"], expected["byte_size"]):
        raise RuntimeContractError("runtime_archive_digest_mismatch")
    stream.seek(0)
    entries, seen = {}, set()
    try:
        with tarfile.open(fileobj=stream, mode="r:") as archive:
            total = 0
            for member in archive:
                if len(seen) >= 4096 or member.name in seen:
                    raise RuntimeContractError("runtime_archive_members_invalid")
                seen.add(member.name)
                # containerd's exporter emits these two empty directories.
                # TarInfo strips their trailing slash; no other directory,
                # alias, link or special file is part of this layout contract.
                if member.isdir() and member.name in {"blobs", "blobs/sha256"} and member.size == 0:
                    continue
                if not member.isfile():
                    raise RuntimeContractError("runtime_archive_members_invalid")
                if member.name not in {"oci-layout", "index.json"} and not re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", member.name):
                    raise RuntimeContractError("runtime_archive_path_invalid")
                total += member.size
                if not 0 <= member.size <= expected["byte_size"] or total > expected["byte_size"]:
                    raise RuntimeContractError("runtime_archive_size_invalid")
                content = archive.extractfile(member)
                hashed = hashlib.sha256()
                with content:
                    for block in iter(lambda: content.read(1024 * 1024), b""):
                        hashed.update(block)
                if member.name.startswith("blobs/") and hashed.hexdigest() != member.name.rsplit("/", 1)[-1]:
                    raise RuntimeContractError("runtime_blob_digest_mismatch")
                entries[member.name] = member

            def metadata(name):
                member = entries.get(name)
                if member is None or member.size > 1024 * 1024:
                    raise RuntimeContractError("runtime_oci_metadata_missing")
                with archive.extractfile(member) as content:
                    return strict_json(content.read(1024 * 1024 + 1))

            def descriptor(value):
                if (type(value) is not dict or not isinstance(value.get("digest"), str)
                        or not re.fullmatch(r"sha256:[0-9a-f]{64}", value["digest"])
                        or type(value.get("size")) is not int):
                    raise RuntimeContractError("runtime_oci_descriptor_invalid")
                if value.get("annotations"):
                    raise RuntimeContractError("runtime_oci_annotations_rejected")
                name = "blobs/sha256/" + value["digest"].split(":")[1]
                if name not in entries or entries[name].size != value["size"]:
                    raise RuntimeContractError("runtime_oci_descriptor_mismatch")
                return name

            if metadata("oci-layout") != {"imageLayoutVersion": "1.0.0"}:
                raise RuntimeContractError("runtime_oci_layout_invalid")
            index = metadata("index.json")
            if index.get("annotations"):
                raise RuntimeContractError("runtime_oci_annotations_rejected")
            if index.get("schemaVersion") != 2 or type(index.get("manifests")) is not list or len(index["manifests"]) != 1:
                raise RuntimeContractError("runtime_oci_single_platform_required")
            target = index["manifests"][0]
            manifest = metadata(descriptor(target))
            if manifest.get("annotations"):
                raise RuntimeContractError("runtime_oci_annotations_rejected")
            if target["digest"] != release.image_digest or manifest.get("schemaVersion") != 2:
                raise RuntimeContractError("runtime_oci_target_mismatch")
            if target.get("mediaType") != "application/vnd.oci.image.manifest.v1+json":
                raise RuntimeContractError("runtime_oci_manifest_required")
            config = metadata(descriptor(manifest["config"]))
            layers = manifest["layers"]
            if type(layers) is not list or not 1 <= len(layers) <= 512:
                raise RuntimeContractError("runtime_oci_layers_invalid")
            for layer in layers:
                descriptor(layer)
                if layer.get("mediaType") not in {"application/vnd.oci.image.layer.v1.tar", "application/vnd.oci.image.layer.v1.tar+gzip", "application/vnd.oci.image.layer.v1.tar+zstd"}:
                    raise RuntimeContractError("runtime_oci_layer_type_invalid")
            platform = config.get("os", "") + "/" + config.get("architecture", "")
            if config.get("variant"):
                platform += "/" + config["variant"]
            image = contract["image"]
            if platform != image["platform"]:
                raise RuntimeContractError("runtime_oci_platform_mismatch")
            actual = config["config"]
            for field, key in (("Entrypoint", "entrypoint"), ("Cmd", "command"), ("Env", "environment")):
                if (actual.get(field) or []) != image[key]:
                    raise RuntimeContractError("runtime_oci_config_mismatch")
            if actual.get("Volumes") or actual.get("OnBuild") or actual.get("ExposedPorts"):
                raise RuntimeContractError("runtime_oci_implicit_side_effect")
            rootfs = config["rootfs"]
            if (rootfs.get("type") != "layers" or type(rootfs.get("diff_ids")) is not list
                    or len(rootfs["diff_ids"]) != len(layers)
                    or any(not isinstance(item, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", item) for item in rootfs["diff_ids"])):
                raise RuntimeContractError("runtime_oci_rootfs_invalid")
            return {"manifest_digest": target["digest"], "config_digest": manifest["config"]["digest"],
                    "platform": platform, "config": actual, "rootfs": rootfs,
                    "archive_sha256": expected["sha256"], "archive_bytes": expected["byte_size"]}
    except RuntimeContractError:
        raise
    except (tarfile.TarError, KeyError, TypeError, ValueError, AttributeError):
        raise RuntimeContractError("runtime_oci_invalid") from None
    finally:
        stream.seek(0)


class RuntimeLockBusy(BlockingIOError):
    """A live process owns a runtime operation; no failure/cleanup is implied."""


@contextmanager
def exclusive_lock(path):
    checked_path(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    held = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeContractError("runtime_lock_invalid")
        try:
            if os.name == "posix":
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise RuntimeLockBusy(exc.errno, 'runtime operation is owned by another process') from None
            raise
        held = True
        yield
    finally:
        if held:
            if os.name == "posix":
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def image_approval(release):
    value = release.data["image"]
    # OCI archives are imported without repository tags. Their immutable local
    # target is the approved single-platform manifest, not its config digest.
    return ImageApproval(value["reference"], value["image_id"], value["platform"],
                         tuple(value["entrypoint"]), tuple(value["command"]), tuple(value["environment"]))


def import_source_path(stream) -> Path:
    """Resolve the already-authorized archive FD for an offline conversion."""
    name = getattr(stream, "name", None)
    if isinstance(name, (str, os.PathLike)):
        return Path(name)
    if os.name != "posix" or not isinstance(name, int) or name != stream.fileno():
        raise RuntimeContractError("runtime_import_source_invalid")
    try:
        return Path(f"/proc/self/fd/{name}").resolve(strict=True)
    except OSError:
        raise RuntimeContractError("runtime_import_source_invalid") from None


class UnixRuntimeImporter:
    """Installation-only, fixed images/load operation; never pull/build/run."""
    def __init__(self, engine):
        self.engine = engine
        self.engine_id = engine.config.engine_id

    def preflight(self):
        self.engine.verify_engine()
        info = self.engine._request("GET", "/info")
        image_store = getattr(self.engine.config, "image_store", "containerd")
        if (image_store == "containerd"
                and ["driver-type", "io.containerd.snapshotter.v1"] not in info.get("DriverStatus", [])):
            raise RuntimeContractError("runtime_containerd_required")
        if image_store == "overlay2" and info.get("Driver") != "overlay2":
            raise RuntimeContractError("runtime_overlay2_required")

    def inspect(self, release, verified):
        self.preflight()
        image = self.engine.inspect_image(image_approval(release))
        if (image["RootFS"]["Layers"] != verified["rootfs"]["diff_ids"]
                or any((image["Config"].get(key) or []) != (verified["config"].get(key) or []) for key in ("Env", "Cmd", "Entrypoint"))):
            raise RuntimeContractError("runtime_import_readback_mismatch")
        # The fixed Engine API exposes local config/layer completeness separately
        # from the platform inspect. Neither it nor Loaded proves unpack/runtime
        # validation, and neither is used to clear an unknown load operation.
        if getattr(self.engine.config, "image_store", "containerd") == "containerd":
            local = self.engine._request('GET', '/images/' + quote(release.image_digest, safe='') + '/json?manifests=true')
            candidates = local.get('Manifests')
            expected_platform = dict(zip(('os', 'architecture', 'variant'), verified['platform'].split('/')))
            if (not isinstance(candidates, list) or len(candidates) != 1
                    or candidates[0].get('ID') != release.image_digest
                    or candidates[0].get('Descriptor') != image['Descriptor']
                    or candidates[0].get('Kind') != 'image' or candidates[0].get('Available') is not True
                    or candidates[0].get('ImageData', {}).get('Platform') != expected_platform):
                raise RuntimeContractError('runtime_image_content_unavailable')
        return {"engine_id": self.engine_id, "image_id": image["Id"],
                "manifest_digest": release.image_digest, "descriptor": image.get("Descriptor"),
                "platform": verified["platform"], "rootfs": verified["rootfs"],
                "config_digest": verified["config_digest"], "content_present": True}

    def inspect_existing(self, release, verified):
        """Explicit boundary for adopting an operator-preimported image."""
        try:
            return self.inspect(release, verified)
        except ContainerError as error:
            # RuntimeArtifactStore owns the import decision and recognizes its
            # own stable error vocabulary.  A missing Engine object is the
            # expected signal to continue with the one-shot archive import.
            raise RuntimeContractError(error.code) from None

    def load(self, stream, release):
        config = self.engine.config
        converted = None
        image_store = getattr(config, "image_store", "containerd")
        if image_store == "overlay2":
            from scripts.convert_oci_to_docker_archive import convert
            source = import_source_path(stream)
            converted = source.with_name(source.name + ".docker-" + release.data["image"]["image_id"].split(":", 1)[1] + ".tar")
            convert(source, converted)
            stream = converted.open("rb")
        deadline = time.monotonic() + getattr(config, "import_timeout", config.total_timeout)
        connection = http.client.HTTPConnection("localhost")
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError()
            client.settimeout(min(getattr(config, "import_io_timeout", config.io_timeout), value))
        try:
            if object_identity(config.socket_path) != self.engine.socket_identity:
                raise RuntimeContractError("runtime_engine_socket_changed")
            remaining(); client.connect(config.socket_path)
            if object_identity(config.socket_path) != self.engine.socket_identity:
                raise RuntimeContractError("runtime_engine_socket_changed")
            connection.sock = client
            parts = release.data["image"]["platform"].split("/")
            platform = {"os": parts[0], "architecture": parts[1]}
            if len(parts) == 3:
                platform["variant"] = parts[2]
            path = f"/v{config.api_version}/images/load?quiet=1&platform=" + quote(canonical(platform), safe="")
            remaining(); connection.putrequest("POST", path)
            connection.putheader("Content-Type", "application/x-tar")
            content_bytes = converted.stat().st_size if converted else release.data["artifact"]["byte_size"]
            connection.putheader("Content-Length", str(content_bytes))
            connection.putheader("Connection", "close"); connection.endheaders()
            stream.seek(0)
            unsent = content_bytes
            while unsent:
                block = stream.read(min(64 * 1024, unsent))
                if not block:
                    raise RuntimeContractError('runtime_import_source_short')
                remaining(); connection.send(block); unsent -= len(block)
            remaining(); response = connection.getresponse()
            headers = response.getheaders()
            lengths = [v for k,v in headers if k.lower() == 'content-length']
            encodings = [v for k,v in headers if k.lower() == 'transfer-encoding']
            if (sum(len(k) + len(v) + 4 for k,v in headers) > 32768
                    or len(lengths) > 1 or len(encodings) > 1
                    or encodings and (encodings != ['chunked'] or lengths)
                    or not encodings and (len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,10}', lengths[0]))):
                raise RuntimeContractError('runtime_import_framing_invalid')
            body = bytearray()
            while True:
                remaining(); block = response.read1(min(16384, config.response_limit + 1 - len(body)))
                if not block:
                    break
                body.extend(block)
                if len(body) > config.response_limit:
                    raise RuntimeContractError("runtime_import_response_too_large")
            if not response.chunked and response.length != 0:
                raise RuntimeContractError('runtime_import_response_truncated')
            if response.status != 200:
                raise RuntimeContractError("runtime_import_http_failed")
            records = [strict_json(line) for line in bytes(body).splitlines() if line.strip()]
            expected_loaded = release.data["image"]["image_id"] if image_store == "overlay2" else release.image_digest
            loaded = {f'Loaded image ID: {expected_loaded}\n', f'Loaded image: {expected_loaded}\n'}
            if not records or any(type(record) is not dict or set(record) != {'stream'}
                                  or record['stream'] not in loaded for record in records):
                raise RuntimeContractError("runtime_import_stream_failed")
            return {"response_sha256": hashlib.sha256(body).hexdigest(), "response_bytes": len(body)}
        except (OSError, TimeoutError, http.client.HTTPException):
            raise RuntimeContractError("runtime_import_outcome_unknown") from None
        finally:
            connection.close()
            client.close()
            if converted is not None:
                stream.close()
                if converted.exists():
                    converted.unlink()


class RuntimeArtifactStore:
    def __init__(self, repository, root, *, approved_digests=(), allowed_hosts=(),
                 local_artifact_roots=(), local_artifacts=None, source=None, fault=lambda _: None):
        self.repository, self.root = repository, Path(root).absolute()
        if not self.root.exists():
            checked_path(self.root.parent)
            self.root.mkdir(mode=0o700)
        self.root_identity = identity(checked_path(self.root).stat())
        self.approved_digests, self.allowed_hosts = frozenset(approved_digests), tuple(allowed_hosts)
        roots = []
        for value in local_artifact_roots:
            path = checked_path(value)
            info = path.stat()
            if not stat.S_ISDIR(info.st_mode):
                raise RuntimeContractError("runtime_local_artifact_root_invalid")
            roots.append((path, identity(info)))
        if len({str(path) for path, _ in roots}) != len(roots):
            raise RuntimeContractError("runtime_local_artifact_root_invalid")
        self.local_artifact_roots = tuple(roots)
        if local_artifacts is None:
            local_artifacts = {}
        if type(local_artifacts) is not dict or any(
                type(key) is not str or not re.fullmatch(r"[0-9a-f]{64}", key)
                or type(value) is not str for key, value in local_artifacts.items()):
            raise RuntimeContractError("runtime_local_artifact_mapping_invalid")
        self.local_artifacts = dict(local_artifacts)
        self.source, self.fault = source or self._https, fault

    def _boundary(self):
        if identity(checked_path(self.root).stat()) != self.root_identity:
            raise RuntimeContractError("runtime_store_changed")

    def _local_source(self, release_digest, supplied=None):
        value = supplied if supplied is not None else self.local_artifacts.get(release_digest)
        if value is None:
            return None
        source = checked_path(value)
        if not any(root in source.parents and identity(checked_path(root).stat()) == expected
                   for root, expected in self.local_artifact_roots):
            raise RuntimeContractError("runtime_local_artifact_outside_roots")
        return source

    def register(self, declaration):
        release = RuntimeRelease(declaration).require_approved(self.approved_digests)
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO runtime_release_records VALUES(?,?,?,?)",
                       (release.digest, release.data["release_id"], canonical(release.data), now()))
            row = db.execute("SELECT contract_json FROM runtime_release_records WHERE release_digest=?", (release.digest,)).fetchone()
            if row[0] != canonical(release.data):
                raise RuntimeContractError("runtime_release_integrity_error")
        return release.digest

    def release(self, release_digest):
        with self.repository._connect() as db:
            row = db.execute("SELECT contract_json FROM runtime_release_records WHERE release_digest=?", (release_digest,)).fetchone()
        if not row:
            raise RuntimeContractError("runtime_release_unavailable")
        release = RuntimeRelease(strict_json(row[0])).require_approved(self.approved_digests)
        if release.digest != release_digest:
            raise RuntimeContractError("runtime_release_integrity_error")
        return release

    def get(self, transfer_id):
        with self.repository._connect() as db:
            row = db.execute("SELECT * FROM runtime_image_transfers WHERE transfer_id=?", (transfer_id,)).fetchone()
        if row is None:
            raise RuntimeContractError("runtime_transfer_missing")
        return dict(row)

    def begin(self, owner, release_digest):
        self._boundary()
        release = self.release(release_digest)
        with self.repository._connect() as db:
            self.repository._assert_installation_owner(db, owner, INSTALL_ACTIVE)
        transfer_id = "rit_" + digest([owner[0], release.digest])
        with exclusive_lock(self.root / (transfer_id + ".lock")), self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.repository._assert_installation_owner(db, owner, INSTALL_ACTIVE)
            row = db.execute("SELECT * FROM runtime_image_transfers WHERE transfer_id=?", (transfer_id,)).fetchone()
            if row is None:
                timestamp = now()
                db.execute("INSERT INTO runtime_image_transfers(transfer_id,operation_id,attempt_id,release_digest,image_digest,phase,local_path,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?,?)",
                           (transfer_id, owner[0], owner[1], release.digest, release.image_digest, transfer_id + ".oci.tar", timestamp, timestamp))
            elif row["attempt_id"] != owner[1]:
                # The cross-process file lock proves the actual downloader is
                # absent. Only the same installation's new authoritative attempt
                # can succeed the reference; unknown import is not reissued.
                if row["operation_id"] != owner[0]:
                    raise InstallationOwnershipError("runtime_transfer_foreign_owner")
                db.execute("UPDATE installation_resources SET successor_attempt_id=? WHERE attempt_id=? AND kind='runtime-transfer' AND resource_id=?",
                           (owner[1], row["attempt_id"], transfer_id))
                db.execute("UPDATE runtime_image_transfers SET attempt_id=? WHERE transfer_id=?", (owner[1], transfer_id))
            self.repository._record_installation_resource(db, owner, "runtime-transfer", transfer_id, release.digest, True, now())
        return self.get(transfer_id)

    @contextmanager
    def _https(self, release, offset, timeout):
        artifact = release.data["artifact"]
        host, addresses = validate_https_url(artifact["url"], self.allowed_hosts)
        connection = _PinnedHTTPS(host, addresses[0], timeout)
        try:
            connection.request("GET", urlsplit(artifact["url"]).path,
                               headers={"Range": f"bytes={offset}-", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            total = artifact["byte_size"]
            if response.status != 206 or response.getheader("Content-Range") != f"bytes {offset}-{total-1}/{total}":
                raise RuntimeContractError("runtime_range_response_invalid")
            if response.getheader("Content-Length") != str(total-offset) or response.getheader("Content-Encoding", "identity") != "identity":
                raise RuntimeContractError("runtime_response_size_invalid")
            yield response
        finally:
            connection.close()

    def _owned(self, db, owner, transfer_id):
        self.repository._assert_installation_owner(db, owner, INSTALL_ACTIVE)
        row = db.execute("SELECT * FROM runtime_image_transfers WHERE transfer_id=? AND operation_id=? AND attempt_id=?",
                         (transfer_id, owner[0], owner[1])).fetchone()
        if not row:
            raise InstallationOwnershipError("runtime_transfer_owner_changed")
        return row

    def download(self, owner, transfer_id, *, seconds=10.0):
        if not 0 < seconds <= 60:
            raise RuntimeContractError("runtime_download_budget_invalid")
        self._boundary()
        self._preflight_owner(owner, transfer_id)
        initial = self.get(transfer_id)
        local_source = self._local_source(initial["release_digest"])
        if local_source is not None and initial["phase"] in {"queued", "downloading"}:
            return self.adopt_local(owner, transfer_id, local_source)
        deadline = time.monotonic() + seconds
        with exclusive_lock(self.root / (transfer_id + ".lock")):
            row = self.get(transfer_id)
            release = self.release(row["release_digest"])
            path = self.root / row["local_path"]
            if path.parent != self.root or path.name != transfer_id + ".oci.tar":
                raise RuntimeContractError("runtime_transfer_path_changed")
            if row['phase'] == 'queued' and row['object_json'] is None:
                with self.repository._connect() as db:
                    cached = db.execute("SELECT * FROM runtime_image_transfers WHERE transfer_id!=? AND release_digest=? AND phase IN ('verified','ready') AND object_json IS NOT NULL ORDER BY transfer_id LIMIT 1", (transfer_id, release.digest)).fetchone()
                if cached:
                    source_path = self.root / cached['local_path']
                    if source_path.parent != self.root or source_path.name != cached['transfer_id'] + '.oci.tar':
                        raise RuntimeContractError('runtime_transfer_path_changed')
                    with open_regular(source_path) as source:
                        expected = strict_json(cached['object_json'])
                        if identity(os.fstat(source.fileno())) != expected:
                            raise RuntimeContractError('runtime_transfer_object_changed')
                        verified = verify_oci_archive(source, release)
                        with self.repository._connect() as db:
                            db.execute('BEGIN IMMEDIATE')
                            current = self._owned(db, owner, transfer_id)
                            if current['phase'] != 'queued' or current['object_json'] is not None:
                                raise RuntimeContractError('runtime_transfer_state_changed')
                            # Immutable verified archives are shared, not fetched
                            # again. Each installation owns only its new link.
                            os.link(source_path, path, follow_symlinks=False)
                            with open_regular(path) as linked:
                                if identity(os.fstat(linked.fileno())) != expected:
                                    raise RuntimeContractError('runtime_transfer_object_changed')
                            fsync_directory(self.root)
                            db.execute("UPDATE runtime_image_transfers SET phase='verified',object_json=?,received_bytes=?,result_json=?,result_digest=?,updated_at=? WHERE transfer_id=?",
                                (canonical(expected), release.data['artifact']['byte_size'], canonical(verified), digest(verified), now(), transfer_id))
                    return self.get(transfer_id)
            with self.repository._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = self._owned(db, owner, transfer_id)
                if row["phase"] in {"verified", "import_pending", "import_unknown", "ready", "canceled", "paused"}:
                    return dict(row)
                if row["object_json"] is None:
                    try:
                        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    except FileExistsError:
                        raise RuntimeContractError("runtime_transfer_creation_unknown") from None
                    try:
                        object_id = identity(os.fstat(fd))
                    finally:
                        os.close(fd)
                    db.execute("UPDATE runtime_image_transfers SET object_json=? WHERE transfer_id=?", (canonical(object_id), transfer_id))
                else:
                    object_id = strict_json(row["object_json"])
                offset = row["received_bytes"]
                db.execute("UPDATE runtime_image_transfers SET phase='downloading',error_code=NULL WHERE transfer_id=?", (transfer_id,))
            fd = os.open(path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                if identity(os.fstat(fd)) != object_id or os.fstat(fd).st_size < offset:
                    raise RuntimeContractError("runtime_transfer_object_changed")
                os.ftruncate(fd, offset)
                os.lseek(fd, offset, os.SEEK_SET)
                with os.fdopen(fd, "wb", closefd=False) as output:
                    if offset < release.data["artifact"]["byte_size"]:
                        with self.source(release, offset, min(2.0, seconds)) as source:
                            while time.monotonic() < deadline and offset < release.data["artifact"]["byte_size"]:
                                with self.repository._connect() as db:
                                    current = self._owned(db, owner, transfer_id)
                                    if current["phase"] != "downloading":
                                        return dict(current)
                                amount = min(64 * 1024, release.data["artifact"]["byte_size"] - offset)
                                block = source.read(amount)
                                if not block or len(block) > amount:
                                    raise RuntimeContractError("runtime_download_short_or_oversize")
                                output.write(block); output.flush(); os.fsync(output.fileno())
                                self.fault("runtime.download.bytes")
                                with self.repository._connect() as db:
                                    db.execute("BEGIN IMMEDIATE")
                                    current = self._owned(db, owner, transfer_id)
                                    if current["phase"] != "downloading":
                                        return dict(current)
                                    offset += len(block)
                                    db.execute("UPDATE runtime_image_transfers SET received_bytes=?,version=version+1,updated_at=? WHERE transfer_id=?", (offset, now(), transfer_id))
                                self.fault("runtime.download.commit")
            finally:
                os.close(fd)
            if offset == release.data["artifact"]["byte_size"]:
                with open_regular(path) as stream:
                    result = verify_oci_archive(stream, release)
                fsync_directory(self.root)
                with self.repository._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    current = self._owned(db, owner, transfer_id)
                    if current["phase"] == "downloading":
                        db.execute("UPDATE runtime_image_transfers SET phase='verified',result_json=?,result_digest=?,updated_at=? WHERE transfer_id=?",
                                   (canonical(result), digest(result), now(), transfer_id))
            return self.get(transfer_id)

    def adopt_local(self, owner, transfer_id, source):
        """Copy one operator-declared OCI archive into the immutable store.

        The source must be below an explicit Server-owned root. It is never
        linked into the store, so later replacement of a build output cannot
        mutate an installation transfer that has already been verified.
        """
        self._boundary()
        self._preflight_owner(owner, transfer_id)
        row = self.get(transfer_id)
        release = self.release(row["release_digest"])
        source_path = self._local_source(release.digest, str(source))
        target = self.root / row["local_path"]
        if target.parent != self.root or target.name != transfer_id + ".oci.tar":
            raise RuntimeContractError("runtime_transfer_path_changed")
        with exclusive_lock(self.root / (transfer_id + ".lock")):
            with self.repository._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = self._owned(db, owner, transfer_id)
                if current["phase"] in {"verified", "ready"}:
                    return dict(current)
                if current["phase"] not in {"queued", "downloading"}:
                    raise RuntimeContractError("runtime_local_adoption_state_changed")
                if current["object_json"] is None:
                    if current['phase'] != 'queued':
                        raise RuntimeContractError('runtime_transfer_object_changed')
                    try:
                        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                                     getattr(os, "O_NOFOLLOW", 0), 0o600)
                    except FileExistsError:
                        raise RuntimeContractError("runtime_transfer_creation_unknown") from None
                else:
                    if current['phase'] != 'downloading':
                        raise RuntimeContractError('runtime_local_adoption_state_changed')
                    fd = os.open(target, os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0))
                try:
                    object_id = identity(os.fstat(fd))
                    if current['object_json'] is not None:
                        if (not stat.S_ISREG(os.fstat(fd).st_mode)
                                or object_id != strict_json(current['object_json'])):
                            raise RuntimeContractError('runtime_transfer_object_changed')
                        # Only this transfer's identity-checked private partial
                        # is reset. The approved source/other assets are untouched.
                        os.ftruncate(fd, 0)
                        os.lseek(fd, 0, os.SEEK_SET)
                    db.execute("UPDATE runtime_image_transfers SET phase='downloading',object_json=?,error_code=NULL,updated_at=? WHERE transfer_id=?",
                               (canonical(object_id), now(), transfer_id))
                except BaseException:
                    os.close(fd)
                    raise
            copied = 0
            try:
                with open_regular(source_path) as input_stream, os.fdopen(fd, "wb") as output:
                    source_id = identity(os.fstat(input_stream.fileno()))
                    while copied <= release.data["artifact"]["byte_size"]:
                        block = input_stream.read(min(1024 * 1024,
                            release.data["artifact"]["byte_size"] - copied + 1))
                        if not block:
                            break
                        copied += len(block)
                        if copied > release.data["artifact"]["byte_size"]:
                            raise RuntimeContractError("runtime_local_artifact_size_changed")
                        output.write(block)
                    output.flush(); os.fsync(output.fileno())
                    if copied != release.data["artifact"]["byte_size"] \
                            or identity(os.fstat(input_stream.fileno())) != source_id:
                        raise RuntimeContractError("runtime_local_artifact_size_changed")
                with open_regular(target) as stream:
                    if identity(os.fstat(stream.fileno())) != object_id:
                        raise RuntimeContractError("runtime_transfer_object_changed")
                    verified = verify_oci_archive(stream, release)
                fsync_directory(self.root)
                with self.repository._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    current = self._owned(db, owner, transfer_id)
                    if current["phase"] != "downloading" or strict_json(current["object_json"]) != object_id:
                        raise RuntimeContractError("runtime_transfer_state_changed")
                    db.execute("UPDATE runtime_image_transfers SET phase='verified',received_bytes=?,result_json=?,result_digest=?,updated_at=? WHERE transfer_id=?",
                               (copied, canonical(verified), digest(verified), now(), transfer_id))
                return self.get(transfer_id)
            except BaseException as error:
                try:
                    with self.repository._connect() as db:
                        db.execute("BEGIN IMMEDIATE")
                        current = self._owned(db, owner, transfer_id)
                        if current["phase"] == "downloading":
                            db.execute("UPDATE runtime_image_transfers SET phase='failed',received_bytes=?,error_code=?,updated_at=? WHERE transfer_id=?",
                                       (copied, getattr(error, "code", "runtime_local_adoption_failed"), now(), transfer_id))
                finally:
                    raise

    def _preflight_owner(self, owner, transfer_id):
        # Validate both the leaf name and authority before even creating a lock
        # file. The writer repeats authority checks under the lock/transaction.
        if not isinstance(transfer_id, str) or not re.fullmatch(r"rit_[0-9a-f]{64}", transfer_id):
            raise RuntimeContractError("runtime_transfer_id_invalid")
        with self.repository._connect() as db:
            return dict(self._owned(db, owner, transfer_id))

    def import_image(self, owner, transfer_id, importer):
        self._boundary()
        initial = self._preflight_owner(owner, transfer_id)
        approved = self.release(initial["release_digest"])
        image_lock = "image_" + digest([importer.engine_id, approved.image_digest]) + ".lock"
        with exclusive_lock(self.root / (transfer_id + ".lock")), exclusive_lock(self.root / image_lock):
            row = self.get(transfer_id)
            release = self.release(row["release_digest"])
            if row["engine_id"] and row["engine_id"] != importer.engine_id:
                raise RuntimeContractError("runtime_import_engine_changed")
            if row["phase"] in {"import_pending", "import_unknown"}:
                # Inspect alone cannot establish that a lost load request's
                # unpack finished. Keep the original operation quarantined.
                raise RuntimeContractError("runtime_import_outcome_unknown")
            if row["phase"] not in {"verified", "ready"}:
                raise RuntimeContractError("runtime_archive_not_verified")
            path = self.root / row["local_path"]
            if path.parent != self.root or path.name != transfer_id + ".oci.tar":
                raise RuntimeContractError("runtime_transfer_path_changed")
            with open_regular(path) as stream:
                if identity(os.fstat(stream.fileno())) != strict_json(row["object_json"]):
                    raise RuntimeContractError("runtime_transfer_object_changed")
                verified = verify_oci_archive(stream, release)
                importer.preflight()
                with self.repository._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    current = self._owned(db, owner, transfer_id)
                    if current["phase"] not in {"verified", "ready"}:
                        raise RuntimeContractError("runtime_import_state_conflict")
                    unresolved = db.execute("SELECT transfer_id FROM runtime_image_transfers WHERE engine_id=? AND image_digest=? AND phase IN ('import_pending','import_unknown')",
                                            (importer.engine_id, release.image_digest)).fetchone()
                    if unresolved:
                        raise RuntimeContractError('runtime_image_import_unresolved')
                    binding = db.execute("SELECT * FROM runtime_image_bindings WHERE engine_id=? AND image_digest=?",
                                         (importer.engine_id, release.image_digest)).fetchone()
                    if binding:
                        if binding["verification_digest"] != digest(strict_json(binding["verification_json"])):
                            raise RuntimeContractError("runtime_image_binding_corrupt")
                    else:
                        # A frozen operator import may already have populated the
                        # exact config/rootfs.  Inspecting the approved bytes is
                        # sufficient and avoids re-streaming a large archive.
                        # Any missing image still follows the durable unknown-
                        # outcome import protocol below.
                        inspect_existing = getattr(importer, "inspect_existing", None)
                        preexisting = False
                        if inspect_existing is not None:
                            try:
                                result = inspect_existing(release, verified)
                                preexisting = True
                            except RuntimeContractError as error:
                                if error.code != "engine_object_missing":
                                    raise
                        if not preexisting:
                            db.execute("UPDATE runtime_image_transfers SET phase='import_pending',engine_id=?,error_code=NULL,updated_at=? WHERE transfer_id=?",
                                       (importer.engine_id, now(), transfer_id))
                if not binding and not preexisting:
                    self.fault("runtime.import.intent")
                    try:
                        importer.load(stream, release)
                        self.fault("runtime.import.external")
                        result = importer.inspect(release, verified)
                    except Exception as error:
                        with self.repository._connect() as db:
                            db.execute("BEGIN IMMEDIATE")
                            self._owned(db, owner, transfer_id)
                            db.execute("UPDATE runtime_image_transfers SET phase='import_unknown',error_code=?,updated_at=? WHERE transfer_id=?",
                                       (getattr(error, "code", "runtime_import_outcome_unknown"), now(), transfer_id))
                        raise
                elif binding:
                    result = importer.inspect(release, verified)
                    if result != strict_json(binding["verification_json"]):
                        raise RuntimeContractError("runtime_image_binding_changed")
                if result.get("image_id") != release.data["image"]["image_id"] or result.get("engine_id") != importer.engine_id:
                    raise RuntimeContractError("runtime_import_readback_mismatch")
                with self.repository._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    self._owned(db, owner, transfer_id)
                    if not binding:
                        db.execute("INSERT INTO runtime_image_bindings VALUES(?,?,?,?,?,?,?)",
                                   (importer.engine_id, release.image_digest, release.digest, result["image_id"], canonical(result), digest(result), transfer_id))
                    self.repository._record_installation_resource(db, owner, "runtime-image",
                        importer.engine_id + "/" + release.image_digest, digest(result),
                        binding is None and not preexisting or
                        binding is not None and binding["transfer_id"] == transfer_id, now())
                    db.execute("UPDATE runtime_image_transfers SET phase='ready',engine_id=?,error_code=NULL,updated_at=? WHERE transfer_id=?",
                               (importer.engine_id, now(), transfer_id))
                    self.fault("runtime.import.commit")
            return self.get(transfer_id)

    def control(self, owner, transfer_id, action):
        if action not in {"pause", "resume", "cancel"}:
            raise RuntimeContractError("runtime_transfer_action_invalid")
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._owned(db, owner, transfer_id)
            allowed = {"pause": {"queued", "downloading"}, "resume": {"paused", "failed"},
                       "cancel": {"queued", "downloading", "paused", "failed"}}
            if row["phase"] not in allowed[action]:
                raise RuntimeContractError("runtime_transfer_action_conflict")
            state = {"pause": "paused", "resume": "queued", "cancel": "canceled"}[action]
            db.execute("UPDATE runtime_image_transfers SET phase=?,updated_at=? WHERE transfer_id=?", (state, now(), transfer_id))
        return self.get(transfer_id)
