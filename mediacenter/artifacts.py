"""Server-owned file snapshots and durable, cancel-fenced publication.

Worker scratch is untrusted. It is never linked/renamed into the store. All
hashing, copying and HTTP reads use the checked open descriptor, not a reopen.
Magic checks identify a container format only, not a complete media decoder.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .protocol import validate_envelope
from .worker_common import TaskStateError, canonical, digest

MAX_FILE = 2 * 1024**3
EXTENSIONS = {"image/png":"png", "image/jpeg":"jpg", "image/webp":"webp",
              "video/mp4":"mp4", "video/webm":"webm", "audio/wav":"wav",
              "audio/mpeg":"mp3", "audio/flac":"flac", "audio/mp4":"m4a"}


def validate_media_metadata(media_type, value):
    """Validate decoder-derived facts without enlarging the terminal envelope."""
    if value is None:
        return
    wav = {"sample_rate", "channels", "sample_count", "duration_ms", "bits_per_sample"}
    if media_type == "audio/wav":
        if type(value) is not dict or set(value) != wav:
            raise TaskStateError("artifact_media_metadata_invalid", 400)
        if (any(type(value[key]) is not int or value[key] <= 0 for key in wav)
                or not 8000 <= value["sample_rate"] <= 192000 or not 1 <= value["channels"] <= 8
                or value["sample_count"] > value["sample_rate"] * 86400
                or value["duration_ms"] > 86400000 or value["bits_per_sample"] not in (8, 16, 24, 32)
                or abs(value["duration_ms"] - round(value["sample_count"] * 1000 / value["sample_rate"])) > 1):
            raise TaskStateError("artifact_media_metadata_invalid", 400)
        return
    base = {"width", "height", "frame_count", "fps_numerator", "fps_denominator", "duration_ms"}
    audio = {"audio_streams", "audio_sample_rate", "audio_channels"}
    if media_type != "video/mp4" or type(value) is not dict or set(value) not in (base, base | audio):
        raise TaskStateError("artifact_media_metadata_invalid", 400)
    for key in ("width", "height", "frame_count", "fps_numerator",
                "fps_denominator", "duration_ms"):
        if type(value[key]) is not int or value[key] <= 0:
            raise TaskStateError("artifact_media_metadata_invalid", 400)
    if (value["width"] > 65536 or value["height"] > 65536
            or value["frame_count"] > 1000000 or value["fps_numerator"] > 1000000
            or value["fps_denominator"] > 1000000 or value["duration_ms"] > 86400000):
        raise TaskStateError("artifact_media_metadata_invalid", 400)
    if audio <= set(value):
        if (type(value["audio_streams"]) is not int or value["audio_streams"] not in (0, 1)
                or type(value["audio_sample_rate"]) is not int or type(value["audio_channels"]) is not int
                or (value["audio_streams"] == 0) != (value["audio_sample_rate"] == value["audio_channels"] == 0)
                or value["audio_streams"] == 1 and not (8000 <= value["audio_sample_rate"] <= 192000
                                                         and 1 <= value["audio_channels"] <= 8)):
            raise TaskStateError("artifact_media_metadata_invalid", 400)


def component(value):
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise TaskStateError("artifact_identity_invalid", 400)
    return value


def identity(info):
    return {"device":info.st_dev, "inode":info.st_ino, "mode":stat.S_IFMT(info.st_mode)}


def checked_path(value):
    path = Path(value).absolute()
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise TaskStateError("artifact_path_rejected", 400)
    return path


@contextmanager
def open_regular(value):
    """POSIX walks every component via directory FDs with O_NOFOLLOW.

    Windows additionally rejects reparse points and checks all parent/object
    identities before/after open; the final FD is retained for the operation.
    """
    path = checked_path(value)
    parents = [(p, identity(p.stat())) for p in path.parents]
    fd = None
    directory = None
    try:
        if os.name == "posix":
            directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
            for name in path.parts[1:-1]:
                next_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory); directory = next_fd
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        else:
            fd = os.open(path, os.O_RDONLY | os.O_BINARY)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or identity(info) != identity(checked_path(path).stat()):
            raise TaskStateError("artifact_file_rejected")
        if any(identity(checked_path(p).stat()) != old for p, old in parents):
            raise TaskStateError("artifact_parent_changed")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            yield stream
    finally:
        if fd is not None: os.close(fd)
        if directory is not None: os.close(directory)


def fsync_directory(path):
    if os.name == "posix":
        fd = os.open(checked_path(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(fd)
        finally: os.close(fd)


def magic(media_type, data):
    checks = {"image/png":data.startswith(b"\x89PNG\r\n\x1a\n"), "image/jpeg":data.startswith(b"\xff\xd8\xff"),
        "image/webp":data.startswith(b"RIFF") and data[8:12] == b"WEBP", "video/mp4":data[4:8] == b"ftyp",
        "video/webm":data.startswith(b"\x1aE\xdf\xa3"), "audio/wav":data.startswith(b"RIFF") and data[8:12] == b"WAVE",
        "audio/mpeg":data.startswith((b"ID3",b"\xff\xfb",b"\xff\xf3",b"\xff\xf2")),
        "audio/flac":data.startswith(b"fLaC"), "audio/mp4":data[4:8] == b"ftyp"}
    if not checks.get(media_type, False):
        raise TaskStateError("artifact_media_signature_invalid", 400)


def file_hash(stream, limit):
    result, size = hashlib.sha256(), 0
    while True:
        block = stream.read(min(1024 * 1024, limit - size + 1))
        if not block: break
        size += len(block)
        if size > limit: raise TaskStateError("artifact_size_exceeded", 413)
        result.update(block)
    stream.seek(0)
    return result.hexdigest(), size


def copy_snapshot(source, target, *, sha256, byte_size, media_type, fault=lambda _:None, owned_identity=None):
    """Exclusive copy into a fresh Server inode. Partial outputs are retained."""
    if type(byte_size) is not int or not 0 < byte_size <= MAX_FILE:
        raise TaskStateError("artifact_size_exceeded", 413)
    target = Path(target).absolute()
    checked_path(target.parent)
    before = os.fstat(source.fileno()) if hasattr(source, "fileno") and not isinstance(source, io.BytesIO) else None
    source.seek(0)
    header = source.read(32); magic(media_type, header); source.seek(0)
    flags = os.O_WRONLY | getattr(os,"O_BINARY",0) | getattr(os,"O_NOFOLLOW",0)
    fd = os.open(target, flags | (os.O_CREAT | os.O_EXCL if owned_identity is None else 0), 0o600)
    if owned_identity is not None:
        if identity(os.fstat(fd)) != owned_identity or identity(checked_path(target).stat()) != owned_identity:
            os.close(fd)
            raise TaskStateError("publication_object_changed")
        os.ftruncate(fd,0)  # Only our unpublished, identity-checked staging inode.
    with os.fdopen(fd, "wb") as output:
        result, size = hashlib.sha256(), 0
        while True:
            block = source.read(min(1024 * 1024, byte_size - size + 1))
            if not block: break
            size += len(block)
            if size > byte_size: raise TaskStateError("artifact_size_changed")
            result.update(block); output.write(block)
            fault("copy.block")
        if size != byte_size or result.hexdigest() != sha256:
            raise TaskStateError("artifact_content_changed")
        if before is not None:
            after = os.fstat(source.fileno())
            if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns) != (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                raise TaskStateError("artifact_source_changed")
        output.flush(); os.fsync(output.fileno())
        result_identity = identity(os.fstat(output.fileno()))
    fsync_directory(target.parent)
    return result_identity


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result: raise TaskStateError("artifact_manifest_duplicate_key")
        result[key] = value
    return result


@dataclass
class AuthorizedFile:
    stream: object
    size: int
    name: str
    sha256: str | None
    origin: str

    def close(self):
        self.stream.close()
        context = getattr(self,"_context",None)
        if context is not None:
            self._context = None
            context.__exit__(None,None,None)
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class RuntimeBoundaries:
    """One durable boundary registry shared by all stores and epoch publishers.

    Entries are retained with old packages. Reopening a replaced pathname never
    grants that new object the original authority. Registration is serialized
    with other registrations; reads always use the current committed set.
    """
    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def _checked(row):
        value = json.loads(row["object_json"])
        path = checked_path(row["source_path"])
        if digest(value) != row["object_digest"] or identity(path.stat()) != value:
            raise TaskStateError("runtime_boundary_changed")
        return path, value

    @staticmethod
    def _overlap(left, right):
        if left == right or left in right.parents or right in left.parents:
            return True
        left_id, right_id = identity(left.stat()), identity(right.stat())
        return (any(identity(p.stat()) == left_id for p in (right, *right.parents))
                or any(identity(p.stat()) == right_id for p in left.parents))

    def register(self, role, source, *, package_id=None):
        if role not in {"sealed", "outputs", "journal"}:
            raise TaskStateError("runtime_boundary_role_invalid")
        path = checked_path(Path(source).absolute())
        if not path.is_dir():
            raise TaskStateError("runtime_boundary_directory_required")
        object_id = identity(path.stat())
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM runtime_boundaries").fetchall()
            existing = None
            for row in rows:
                other, _ = self._checked(row)
                if other == path and (row["role"] == role or package_id is None and role != "sealed" and row["role"] != "sealed"):
                    if package_id is not None and row["package_id"] != package_id:
                        raise TaskStateError("runtime_boundary_owner_changed")
                    existing = row["boundary_id"]
                    continue
                if self._overlap(path, other) and (role == "sealed" or row["role"] == "sealed"
                                                  or package_id is not None and row["package_id"] is not None and row["package_id"] != package_id):
                    raise TaskStateError("artifact_writable_root_overlap")
            if existing is not None:
                return existing
            key = "boundary_" + digest([role, str(path)])
            db.execute("INSERT INTO runtime_boundaries VALUES(?,?,?,?,?,?,?)",
                       (key, role, str(path), canonical(object_id), digest(object_id), package_id, datetime.now(timezone.utc).isoformat()))
        return key

    def writable(self):
        with self.repository._connect() as db:
            rows = db.execute("SELECT * FROM runtime_boundaries ORDER BY boundary_id").fetchall()
        return tuple(self._checked(row)[0] for row in rows if row["role"] != "sealed")


class ArtifactStore:
    def __init__(self, tasks, root, *, writable_roots=(), boundary_check=None, fault=None):
        self.tasks, self.repository = tasks, tasks.repository
        self.root = Path(root).absolute()
        # A dedicated Server directory, never a Worker output directory.
        if not self.root.exists():
            checked_path(self.root.parent)
            self.root.mkdir(mode=0o700)
        checked_path(self.root)
        self.root_identity = identity(self.root.stat())
        self.writable_roots = tuple(Path(p).absolute() for p in writable_roots)
        self.boundaries = RuntimeBoundaries(self.repository)
        self.boundaries.register("sealed", self.root)
        for source in self.writable_roots:
            self.boundaries.register("outputs", source)
        self.boundary_check = boundary_check or (lambda:None)
        self.fault = fault or (lambda _:None)
        self.check_boundary()

    def check_boundary(self):
        if identity(checked_path(self.root).stat()) != self.root_identity:
            raise TaskStateError("artifact_root_changed")
        self.boundary_check()
        for root in self.boundaries.writable():
            other = checked_path(root)
            if (other == self.root or other in self.root.parents or self.root in other.parents
                    or any(identity(p.stat()) == self.root_identity for p in (other,*other.parents))
                    or any(identity(p.stat()) == identity(other.stat()) for p in self.root.parents)):
                raise TaskStateError("artifact_writable_root_overlap")

    def _command(self, event):
        with self.repository._connect() as db:
            rows = db.execute("SELECT envelope_json,digest FROM task_outbox WHERE task_id=? AND attempt_id=?", (event["task_id"],event["attempt_id"])).fetchall()
        commands = []
        for row in rows:
            command = validate_envelope(json.loads(row[0]), capabilities=self.tasks.capabilities)
            if digest(command) != row[1]: raise TaskStateError("artifact_command_corrupt")
            if command["type"] == "task.execute": commands.append(command)
        if len(commands) != 1: raise TaskStateError("artifact_command_missing")
        command = commands[0]
        if any(command[k] != event[k] for k in ("server_id","instance_id","worker_epoch","task_id","attempt_id")):
            raise TaskStateError("artifact_command_identity_mismatch")
        return command

    def worker(self, raw_event, outputs):
        self.check_boundary()
        event = validate_envelope(raw_event, capabilities=self.tasks.capabilities)
        if event["type"] != "task.terminal" or event["payload"]["status"] != "succeeded":
            raise TaskStateError("artifact_success_event_required")
        task_id, attempt_id = component(event["task_id"]), component(event["attempt_id"])
        command = self._command(event)
        expected = event["payload"]["manifest"]
        publication_id = "pub_" + digest([task_id,attempt_id,expected["asset_id"],expected["revision"]])
        with self.repository._connect() as db:
            old = db.execute("SELECT * FROM artifact_publications WHERE publication_id=?", (publication_id,)).fetchone()
        if old:
            descriptor = self._checked(old)
            if descriptor["event"] != event or descriptor["command_digest"] != digest(command) or descriptor["source_root"] != str(Path(outputs).absolute()):
                raise TaskStateError("publication_identity_conflict")
            return self._resume(publication_id)
        manifest_path = Path(outputs) / "tasks" / task_id / attempt_id / "manifest.json"
        with open_regular(manifest_path) as stream:
            raw = stream.read(65537)
        if len(raw) > 65536: raise TaskStateError("artifact_manifest_too_large")
        try:
            manifest = json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, RecursionError, UnicodeError):
            raise TaskStateError("artifact_manifest_invalid") from None
        keys = {"schema","task_id","attempt_id","instance_id","worker_epoch","command_message_id","command_digest","asset_id","revision","sha256","byte_size","media_type"}
        if (type(manifest) is not dict or set(manifest) not in (keys, keys | {"media_metadata"})
                or type(manifest["schema"]) is not int or manifest["schema"] != 1):
            raise TaskStateError("artifact_manifest_invalid")
        for key in ("task_id","attempt_id","instance_id","worker_epoch"):
            if manifest[key] != event[key]: raise TaskStateError("artifact_manifest_identity_mismatch")
        if (any(manifest[k] != expected[k] for k in expected) or manifest["command_message_id"] != command["message_id"]
                or manifest["command_digest"] != digest(command)):
            raise TaskStateError("artifact_manifest_identity_mismatch")
        if manifest["media_type"] not in EXTENSIONS: raise TaskStateError("artifact_media_type_invalid")
        validate_media_metadata(manifest["media_type"], manifest.get("media_metadata"))
        service = self.repository.get_task(task_id)["service"]
        if manifest["media_type"].split("/")[0] != {"image":"image","video":"video","speech":"audio","music":"audio"}[service]:
            raise TaskStateError("artifact_media_type_invalid")
        task = self.repository.get_task(task_id)
        descriptor = {"event":event, "command_digest":digest(command), "manifest":manifest,
            "cancel_revision":task["cancel_revision"], "source_root":str(Path(outputs).absolute()),
            "source_root_identity":identity(checked_path(outputs).stat()),
            "source_path":str(manifest_path.parent / ("artifact." + EXTENSIONS[manifest["media_type"]])),
            "root_identity":self.root_identity,
            "relative_path":publication_id + "." + EXTENSIONS[manifest["media_type"]]}
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self.tasks._task(db, task_id)
            if current["current_attempt_id"] != attempt_id or current["cancel_requested"]:
                raise TaskStateError("seal_attempt_canceled_or_stale")
            inbox = db.execute("SELECT digest FROM task_inbox WHERE message_id=? AND result='pending'", (event["message_id"],)).fetchone()
            if not inbox or inbox[0] != digest(event): raise TaskStateError("artifact_pending_event_required")
            db.execute("INSERT OR IGNORE INTO artifact_publications(publication_id,task_id,attempt_id,asset_id,revision,descriptor_json,descriptor_digest,phase) VALUES(?,?,?,?,?,?,?,'intent')",
                (publication_id,task_id,attempt_id,expected["asset_id"],expected["revision"],canonical(descriptor),digest(descriptor)))
        self.fault("publication.intent")
        return self._resume(publication_id)

    def local(self, task, source, asset, execution):
        """A local edit uses the same publication transaction and copy primitive."""
        task_id,attempt_id=component(task["id"]),component(task["current_attempt_id"])
        asset_id="art_"+digest([task_id,attempt_id])
        publication_id="pub_"+digest([task_id,attempt_id,asset_id,asset["sha256"]])
        manifest={"asset_id":asset_id,"revision":asset["sha256"],"sha256":asset["sha256"],
            "byte_size":asset["byte_size"],"media_type":asset["media_type"]}
        value={"event":{"task_id":task_id,"attempt_id":attempt_id},"manifest":manifest,
            "local_execution":execution,"command_digest":digest([task_id,attempt_id,asset["id"],asset["sha256"]]),
            "cancel_revision":task["cancel_revision"],"source_root":str(Path(source).parent.absolute()),
            "source_root_identity":identity(checked_path(Path(source).parent).stat()),"source_path":str(Path(source).absolute()),
            "root_identity":self.root_identity,"relative_path":publication_id+"."+EXTENSIONS[asset["media_type"]]}
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current=self.tasks._task(db,task_id)
            if current["current_attempt_id"]!=attempt_id or current["cancel_requested"]:
                raise TaskStateError("seal_attempt_canceled_or_stale")
            db.execute("INSERT OR IGNORE INTO artifact_publications(publication_id,task_id,attempt_id,asset_id,revision,descriptor_json,descriptor_digest,phase) VALUES(?,?,?,?,?,?,?,'intent')",
                (publication_id,task_id,attempt_id,asset_id,asset["sha256"],canonical(value),digest(value)))
            old=db.execute("SELECT descriptor_digest FROM artifact_publications WHERE publication_id=?",(publication_id,)).fetchone()
            if not old or old[0]!=digest(value):raise TaskStateError("publication_identity_conflict")
        self.fault("publication.intent")
        return self._resume(publication_id)

    @staticmethod
    def _recovery_limit(limit):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('publication_recovery_limit_invalid')
        return limit

    def _recover_page(self, rows, cursor_name, limit, stop_requested):
        errors, attempted = {}, 0
        for row in rows:
            if stop_requested():
                break
            try:
                self._resume(row[0])
            except Exception as exc:
                # Corrupt or inaccessible rows cannot starve the tail. No
                # identity check or failure is bypassed: keep that row intact.
                fallback = 'publication_cleanup_io_error' if isinstance(exc, OSError) else 'publication_cleanup_integrity_error'
                errors[row[0]] = getattr(exc, 'code', fallback)
            attempted += 1
            setattr(self, cursor_name, row[0])
        # A stop midway through a page must not jump over its unattempted rows.
        if attempted == len(rows) and len(rows) < limit:
            setattr(self, cursor_name, '')
        return errors

    def recover_local(self, *, limit=100, stop_requested=lambda: False):
        limit = self._recovery_limit(limit)
        cursor=getattr(self,"_local_cursor","")
        with self.repository._connect() as db:
            rows=db.execute("SELECT p.publication_id FROM artifact_publications p JOIN task_attempts a ON a.id=p.attempt_id WHERE a.mode='local' AND p.phase NOT IN ('committed','canceled') AND p.publication_id>? ORDER BY p.publication_id LIMIT ?",(cursor,limit)).fetchall()
        return self._recover_page(rows, '_local_cursor', limit, stop_requested)

    def recover_cleanup(self, *, limit=100, stop_requested=lambda: False):
        """Bounded recovery independent of inbox ACKs and Worker availability.

        Only ledger-owned publication slots are candidates. No directory scan,
        orphan guessing, Worker scratch cleanup, or authorized-media deletion.
        A cleared temporary_path is the durable completion marker; object_json
        remains historical identity evidence. Steady state is read-only.
        """
        limit = self._recovery_limit(limit)
        cursor = getattr(self, '_cleanup_cursor', '')
        with self.repository._connect() as db:
            rows = db.execute("""SELECT p.publication_id FROM artifact_publications p
                JOIN tasks t ON t.id=p.task_id WHERE p.publication_id>? AND
                ((p.phase IN ('committed','canceled') AND p.temporary_path IS NOT NULL)
                 OR (p.phase NOT IN ('committed','canceled') AND
                     (t.cancel_requested=1 OR t.current_attempt_id!=p.attempt_id
                      OR t.status IN ('succeeded','failed','canceled','interrupted'))))
                ORDER BY p.publication_id LIMIT ?""", (cursor,limit)).fetchall()
        return self._recover_page(rows, '_cleanup_cursor', limit, stop_requested)

    def _cancel_obsolete(self, row, value):
        with self.repository._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self.tasks._task(db, row['task_id'])
            obsolete = (current['cancel_requested'] or current['cancel_revision'] != value['cancel_revision']
                        or current['current_attempt_id'] != row['attempt_id']
                        or current['status'] in {'succeeded', 'failed', 'canceled', 'interrupted'})
            if not obsolete:
                return False
            # Even an inconsistent terminal task never grants deletion of a seal.
            if db.execute('SELECT 1 FROM task_seals WHERE asset_id=? AND revision=?',
                          (row['asset_id'], row['revision'])).fetchone():
                raise TaskStateError('publication_cleanup_authorized_conflict')
            db.execute("UPDATE artifact_publications SET phase='canceled' WHERE publication_id=? AND phase NOT IN ('committed','canceled')",
                       (row['publication_id'],))
            if ('local_execution' in value and current['current_attempt_id'] == row['attempt_id']
                    and current['status'] == 'cancel_requested'):
                self.tasks.finish_local(row['task_id'], row['attempt_id'], error='local_edit_canceled', _connection=db)
        return True

    def _cleanup_terminal(self, publication_id):
        self.check_boundary()
        with self.repository._connect() as db:
            row = db.execute('SELECT * FROM artifact_publications WHERE publication_id=?', (publication_id,)).fetchone()
            value = self._checked(row)
            if row['phase'] not in {'committed', 'canceled'}:
                raise TaskStateError('publication_cleanup_not_terminal')
            if row['temporary_path'] is None:
                return row['phase']
            final_name = publication_id + '.' + EXTENSIONS[value['manifest']['media_type']]
            if (value['relative_path'] != final_name or
                    not re.fullmatch(re.escape(publication_id) + r'\.[0-9a-f]{32}\.part', row['temporary_path'])):
                raise TaskStateError('publication_cleanup_path_invalid')
            expected = json.loads(row['object_json'])
            if (type(expected) is not dict or set(expected) != {'device', 'inode', 'mode'}
                    or any(type(v) is not int for v in expected.values()) or expected['mode'] != stat.S_IFREG):
                raise TaskStateError('publication_cleanup_identity_invalid')
            if row['phase'] == 'canceled':
                for table in ('task_seals', 'task_artifacts'):
                    if db.execute(f'SELECT 1 FROM {table} WHERE relative_path=? OR (asset_id=? AND revision=?)',
                                  (final_name, row['asset_id'], row['revision'])).fetchone():
                        raise TaskStateError('publication_cleanup_authorized_conflict')
            else:
                seal = db.execute('SELECT * FROM task_seals WHERE asset_id=? AND revision=?',
                                  (row['asset_id'], row['revision'])).fetchone()
                if not seal or seal['relative_path'] != final_name or seal['attempt_id'] != row['attempt_id']:
                    raise TaskStateError('publication_cleanup_seal_missing')
                # Never remove the only surviving name of a committed artifact.
                with open_regular(self.root/final_name) as stream:
                    if identity(os.fstat(stream.fileno())) != expected:
                        raise TaskStateError('publication_cleanup_object_changed')
        # Exact, precomputed names within the checked Server-owned directory.
        targets = ([final_name] if row['phase'] == 'canceled' else []) + [row['temporary_path']]
        directory = None
        try:
            if os.name == 'posix':
                directory = os.open(checked_path(self.root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                if identity(os.fstat(directory)) != self.root_identity:
                    raise TaskStateError('artifact_root_changed')
            for name in targets:
                self.check_boundary()
                try:
                    info = (os.stat(name, dir_fd=directory, follow_symlinks=False) if directory is not None
                            else checked_path(self.root/name).lstat())
                except FileNotFoundError:
                    continue  # A previous unlink may have committed before a crash.
                if identity(info) != expected or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise TaskStateError('publication_cleanup_object_changed')
                if directory is not None:
                    os.unlink(name, dir_fd=directory)
                else:
                    (self.root/name).unlink()
                self.fault('cleanup.final_removed' if name == final_name else 'cleanup.temporary_removed')
            if directory is not None:
                os.fsync(directory)
            self.check_boundary()
            self.fault('cleanup.synced')
            with self.repository._connect() as db:
                db.execute("UPDATE artifact_publications SET temporary_path=NULL WHERE publication_id=? AND phase=? AND temporary_path=? AND object_json=?",
                           (publication_id, row['phase'], row['temporary_path'], row['object_json']))
            self.fault('cleanup.recorded')
        finally:
            if directory is not None:
                os.close(directory)
        return row['phase']

    def _checked(self, row):
        value = json.loads(row["descriptor_json"])
        if digest(value) != row["descriptor_digest"] or value["root_identity"] != self.root_identity:
            raise TaskStateError("publication_integrity_error")
        event, manifest = value["event"], value["manifest"]
        if (row["publication_id"] != "pub_" + digest([row["task_id"],row["attempt_id"],row["asset_id"],row["revision"]])
                or event["task_id"] != row["task_id"] or event["attempt_id"] != row["attempt_id"]
                or manifest["asset_id"] != row["asset_id"] or manifest["revision"] != row["revision"]):
            raise TaskStateError("publication_integrity_error")
        return value

    def _resume(self, publication_id):
        component(publication_id)
        self.check_boundary()
        lock_path=self.root/(publication_id+'.lock')
        fd=os.open(lock_path,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
        held=False
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):raise TaskStateError('publication_lock_invalid')
            if os.name=='posix':
                import fcntl
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            else:
                import msvcrt
                msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
            held=True
            return self._resume_locked(publication_id)
        finally:
            if held:
                if os.name=='posix':fcntl.flock(fd,fcntl.LOCK_UN)
                else:
                    os.lseek(fd,0,os.SEEK_SET);msvcrt.locking(fd,msvcrt.LK_UNLCK,1)
            os.close(fd)

    def _resume_locked(self, publication_id):
        self.check_boundary()
        with self.repository._connect() as db:
            row = db.execute("SELECT * FROM artifact_publications WHERE publication_id=?", (publication_id,)).fetchone()
        value = self._checked(row)
        if row["phase"] in {"committed","canceled"}:
            return self._cleanup_terminal(publication_id)
        if self._cancel_obsolete(row, value):
            return self._cleanup_terminal(publication_id)
        manifest, event = value["manifest"], value["event"]
        final = self.root / value["relative_path"]
        if row["phase"] == "intent":
            if row['error_code'] and row['error_code']!='publication_io_error':
                raise TaskStateError(row['error_code'])
            if row['retry_after']>time.time():raise TaskStateError('publication_retry_deferred')
            if identity(checked_path(value["source_root"]).stat()) != value["source_root_identity"]:
                raise TaskStateError("publication_source_root_changed")
            if row['temporary_path'] is None:
                # Only pre-registration empty-file crashes consume this budget.
                with self.repository._connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    changed=db.execute("UPDATE artifact_publications SET copy_attempts=copy_attempts+1 WHERE publication_id=? AND phase='intent' AND copy_attempts<3",(publication_id,)).rowcount
                    if not changed:raise TaskStateError('publication_copy_budget_exhausted')
                temporary=self.root/(publication_id+'.'+secrets.token_hex(16)+'.part')
                try:
                    fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                except OSError:
                    # Refund only when a strict lookup proves no inode exists.
                    # It is not a crash-unknown orphan and must remain retryable.
                    with self.repository._connect() as db:
                        db.execute('BEGIN IMMEDIATE')
                        unknown=True
                        try:temporary.lstat()
                        except FileNotFoundError:
                            try:unknown=identity(checked_path(self.root).stat())!=self.root_identity
                            except OSError:pass
                        except OSError:pass
                        db.execute("UPDATE artifact_publications SET copy_attempts=copy_attempts-?,error_code=?,io_failures=io_failures+1,retry_after=? WHERE publication_id=?",(0 if unknown else 1,'publication_slot_creation_unknown' if unknown else 'publication_io_error',time.time()+min(60,2**min(row['io_failures'],6)),publication_id))
                    raise
                try:
                    object_id=identity(os.fstat(fd))
                finally:os.close(fd)
                with self.repository._connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute('UPDATE artifact_publications SET temporary_path=?,object_json=? WHERE publication_id=?',(temporary.name,canonical(object_id),publication_id))
            else:
                temporary=self.root/row['temporary_path'];object_id=json.loads(row['object_json'])
            try:
                with open_regular(value["source_path"]) as source:
                    copy_snapshot(source, temporary, sha256=manifest["sha256"], byte_size=manifest["byte_size"], media_type=manifest["media_type"], fault=self.fault,owned_identity=object_id)
            except (TaskStateError,OSError) as exc:
                with self.repository._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("UPDATE artifact_publications SET error_code=?,io_failures=io_failures+1,retry_after=? WHERE publication_id=?",(getattr(exc,"code","publication_io_error"),time.time()+min(60,2**min(row['io_failures'],6)),publication_id))
                raise
            self.fault("publication.copied")
            with self.repository._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE artifact_publications SET phase='staged',temporary_path=?,object_json=?,error_code=NULL,retry_after=0 WHERE publication_id=? AND phase='intent'", (temporary.name,canonical(object_id),publication_id))
            self.fault("publication.staged")
            return self._resume_locked(publication_id)
        temporary = self.root / row["temporary_path"]
        with open_regular(temporary) as source:
            if identity(os.fstat(source.fileno())) != json.loads(row["object_json"]) or file_hash(source, manifest["byte_size"]) != (manifest["sha256"],manifest["byte_size"]):
                raise TaskStateError("publication_object_changed")
        try:
            # Only a Server-created inode is linked; never a Worker source.
            os.link(temporary, final, follow_symlinks=False)
        except FileExistsError:
            pass
        with open_regular(final) as source:
            if identity(os.fstat(source.fileno())) != json.loads(row["object_json"]) or file_hash(source, manifest["byte_size"]) != (manifest["sha256"],manifest["byte_size"]):
                raise TaskStateError("publication_destination_conflict")
        fsync_directory(self.root)
        self.fault("publication.linked")
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE artifact_publications SET phase='published' WHERE publication_id=? AND phase='staged'", (publication_id,))
        self.fault("publication.published")
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute('SELECT phase FROM artifact_publications WHERE publication_id=?',(publication_id,)).fetchone()[0]=='committed':return 'committed'
            current = self.tasks._task(db, event["task_id"])
            if current["cancel_requested"] or current["cancel_revision"] != value["cancel_revision"] or current["current_attempt_id"] != event["attempt_id"]:
                db.execute("UPDATE artifact_publications SET phase='canceled' WHERE publication_id=?", (publication_id,))
                if "local_execution" in value and current["current_attempt_id"]==event["attempt_id"] and current["status"]=="cancel_requested":
                    self.tasks.finish_local(event["task_id"],event["attempt_id"],error="local_edit_canceled",_connection=db)
            else:
                self.tasks.record_sealed_artifact(event["task_id"],event["attempt_id"],cancel_revision=value["cancel_revision"],
                    asset_id=manifest["asset_id"],revision=manifest["revision"],sha256=manifest["sha256"],byte_size=manifest["byte_size"],
                    relative_path=value["relative_path"],execution=value.get("local_execution",{"provider":"worker","command_digest":value["command_digest"]}),_connection=db)
                if "local_execution" in value:
                    self.tasks.finish_local(event["task_id"],event["attempt_id"],asset_id=manifest["asset_id"],revision=manifest["revision"],_connection=db)
                db.execute("UPDATE artifact_publications SET phase='committed' WHERE publication_id=?", (publication_id,))
                self.fault("publication.commit")
        self.fault("publication.committed")
        return self._cleanup_terminal(publication_id)

    def authorize(self, relative):
        self.check_boundary()
        path = PurePosixPath(relative)
        if not relative or path.is_absolute() or str(path) != relative or ".." in path.parts or "\\" in relative or ":" in relative:
            raise TaskStateError("artifact_not_found", 404)
        record = self.repository.authorized_artifact(relative)
        if not record or record["output"].get("artifact_url") != "/api/v1/artifacts/" + relative:
            raise TaskStateError("artifact_not_found", 404)
        context = open_regular(self.root.joinpath(*path.parts))
        stream = context.__enter__()
        try:
            if os.fstat(stream.fileno()).st_size != record["bytes"]:
                raise TaskStateError("artifact_not_found", 404)
            if record["sha256"] is not None and file_hash(stream,record["bytes"])[0] != record["sha256"]:
                raise TaskStateError("artifact_not_found", 404)
            # Keep the context alive until AuthorizedFile.close closes the FD.
            result = AuthorizedFile(stream,record["bytes"],path.name,record["sha256"],record["origin"])
            result._context = context
            return result
        except BaseException:
            context.__exit__(None,None,None)
            raise
