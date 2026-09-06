"""Trusted installation bindings and immutable per-epoch runtime provisioning.

This module belongs to the Server. It is never part of the Worker SDK. Release
approvals, publisher credentials and templates are constructor-supplied trusted
configuration, not fields accepted from a task or an installation request.
"""
from __future__ import annotations

import json
import hashlib
import hmac
import os
import re
import secrets
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timedelta, timezone

from .repository import INSTALL_ACTIVE, InstallationOwnershipError
from .container_releases import RuntimeContractError
from .worker_common import canonical, digest
from .task_state import now, TaskStateError
from .artifacts import checked_path, identity, RuntimeBoundaries
from .runtime_artifacts import exclusive_lock


def _execution_policy(value):
    """Fields that are immutable for one concrete container execution."""
    return {key: item for key, item in value.items()
            if key not in {"residency", "idle_seconds"}}


class _RetainedFD:
    """Pillow may close its fp; the authorization context alone owns this FD."""
    def __init__(self, stream): self.stream = stream
    def __getattr__(self, name): return getattr(self.stream, name)
    def close(self): pass


def _validate_png_structure(stream):
    """Reject oversized or structurally corrupt PNGs before tolerant decoders."""
    import binascii
    import struct

    stream.seek(0)
    if stream.read(8) != b'\x89PNG\r\n\x1a\n':
        raise RuntimeContractError('validation_media_failed')
    seen_ihdr = False
    seen_idat = False
    total = 8
    width = height = 0
    while True:
        header = stream.read(8)
        if len(header) != 8:
            raise RuntimeContractError('validation_media_failed')
        length, kind = struct.unpack('>I4s', header)
        total += 12 + length
        if length > 64 * 1024 ** 2 or total > 64 * 1024 ** 2:
            raise RuntimeContractError('validation_media_limit')
        data = stream.read(length)
        checksum = stream.read(4)
        if len(data) != length or len(checksum) != 4:
            raise RuntimeContractError('validation_media_failed')
        actual = binascii.crc32(data, binascii.crc32(kind)) & 0xffffffff
        if actual != int.from_bytes(checksum, 'big'):
            raise RuntimeContractError('validation_media_failed')
        if not seen_ihdr:
            if kind != b'IHDR' or length != 13:
                raise RuntimeContractError('validation_media_failed')
            width, height = struct.unpack('>II', data[:8])
            if not 1 <= width <= 4096 or not 1 <= height <= 4096 or width * height > 4096 ** 2:
                raise RuntimeContractError('validation_media_limit')
            seen_ihdr = True
        elif kind == b'IHDR':
            raise RuntimeContractError('validation_media_failed')
        if kind == b'IDAT':
            seen_idat = True
        if kind == b'IEND':
            if length or not seen_idat or stream.read(1):
                raise RuntimeContractError('validation_media_failed')
            stream.seek(0)
            return width, height


def _decode_validation_media(stream, relative_path, expected):
    """Fully decode the three production artifact types from the authorized FD."""
    suffix = Path(relative_path).suffix.lower()
    if suffix not in {'.png', '.wav', '.mp4'}:
        raise RuntimeContractError('validation_media_type_invalid')
    if os.name == 'posix':
        import subprocess
        png_size = _validate_png_structure(stream) if suffix == '.png' else None
        descriptor = stream.fileno()
        source = f'/proc/self/fd/{descriptor}'
        probe = ['/usr/bin/ffprobe','-v','error','-show_streams','-show_format','-of','json',source]
        decode = ['/usr/bin/ffmpeg','-v','error','-nostdin','-i',source,'-f','null','-']
        if any(not Path(command[0]).is_file() for command in (probe, decode)):
            raise RuntimeContractError('validation_decoder_unavailable')
        try:
            stream.seek(0)
            inspected = subprocess.run(probe, pass_fds=(descriptor,), capture_output=True,
                                       timeout=30, check=True)
            if len(inspected.stdout) > 1024 * 1024 or inspected.stderr:
                raise RuntimeContractError('validation_media_failed')
            metadata = json.loads(inspected.stdout)
            stream.seek(0)
            completed = subprocess.run(decode, pass_fds=(descriptor,), capture_output=True,
                                       timeout=60, check=True)
            if completed.stdout or completed.stderr:
                raise RuntimeContractError('validation_media_failed')
        except (subprocess.SubprocessError, json.JSONDecodeError):
            raise RuntimeContractError('validation_media_failed') from None
        streams = metadata.get('streams')
        if type(streams) is not list or not streams:
            raise RuntimeContractError('validation_media_failed')
        if suffix == '.png':
            video = next((item for item in streams if item.get('codec_type') == 'video'), None)
            width = int(video.get('width', 0)) if video else 0
            height = int(video.get('height', 0)) if video else 0
            if (not video or video.get('codec_name') != 'png' or not 1 <= width <= 4096
                    or not 1 <= height <= 4096 or width * height > 4096**2
                    or (width, height) != png_size
                    or expected.get('width') is not None and width != expected['width']
                    or expected.get('height') is not None and height != expected['height']):
                raise RuntimeContractError('validation_image_mismatch')
            return {'format':'PNG','width':width,'height':height,'decoder':'ffmpeg'}
        if suffix == '.wav':
            audio = next((item for item in streams if item.get('codec_type') == 'audio'), None)
            if not audio or audio.get('codec_name') not in {'pcm_s16le','pcm_s24le','pcm_s32le','pcm_u8'}:
                raise RuntimeContractError('validation_audio_mismatch')
            rate, channels = int(audio.get('sample_rate',0)), int(audio.get('channels',0))
            if not 8000 <= rate <= 192000 or not 1 <= channels <= 8:
                raise RuntimeContractError('validation_audio_mismatch')
            return {'format':'WAV','sample_rate':rate,'channels':channels,'decoder':'ffmpeg'}
        video = next((item for item in streams if item.get('codec_type') == 'video'), None)
        if not video or not 1 <= int(video.get('width',0)) <= 65536 or not 1 <= int(video.get('height',0)) <= 65536:
            raise RuntimeContractError('validation_video_mismatch')
        if expected.get('width') is not None and int(video['width']) != expected['width']:
            raise RuntimeContractError('validation_video_mismatch')
        if expected.get('height') is not None and int(video['height']) != expected['height']:
            raise RuntimeContractError('validation_video_mismatch')
        return {'format':'MP4','width':int(video['width']),'height':int(video['height']),
                'codec':video.get('codec_name'),'decoder':'ffmpeg'}
    if suffix == '.png':
        from PIL import Image
        proxy = _RetainedFD(stream); stream.seek(0)
        try:
            with Image.open(proxy) as image:
                width, height = image.size
                if (image.format != 'PNG' or not 1 <= width <= 4096 or not 1 <= height <= 4096
                        or width * height > 4096**2
                        or expected.get('width') is not None and width != expected['width']
                        or expected.get('height') is not None and height != expected['height']):
                    raise RuntimeContractError('validation_image_mismatch')
                image.verify()
            stream.seek(0)
            with Image.open(proxy) as image: image.load()
        except RuntimeContractError:
            raise
        except Image.DecompressionBombError:
            raise RuntimeContractError('validation_media_limit') from None
        except (Image.UnidentifiedImageError, OSError, ValueError):
            raise RuntimeContractError('validation_media_failed') from None
        return {'format':'PNG','width':width,'height':height,'decoder':'pillow'}
    if suffix == '.wav':
        import wave
        stream.seek(0)
        with wave.open(_RetainedFD(stream),'rb') as reader:
            rate, channels, frames = reader.getframerate(),reader.getnchannels(),reader.getnframes()
            while reader.readframes(min(rate, max(1, frames))): pass
        if not 8000 <= rate <= 192000 or not 1 <= channels <= 8 or frames <= 0:
            raise RuntimeContractError('validation_audio_mismatch')
        return {'format':'WAV','sample_rate':rate,'channels':channels,'decoder':'wave'}
    raise RuntimeContractError('validation_decoder_unavailable')


def _validation_intent(db, value, instance, kind):
    fields = {'validation_id','instance_id','incarnation','binding_digest','kind','expires_at','expected'}
    if type(value) is not dict or set(value) != fields or value['instance_id'] != instance or value['kind'] != kind:
        raise RuntimeContractError('validation_intent_invalid')
    installed = db.execute('SELECT * FROM instance_installation_bindings WHERE instance_id=?', (instance,)).fetchone()
    if not installed or installed['incarnation'] != value['incarnation'] or installed['binding_digest'] != value['binding_digest']:
        raise RuntimeContractError('validation_binding_changed')
    old = db.execute('SELECT * FROM runtime_validation_records WHERE validation_id=?', (value['validation_id'],)).fetchone()
    if old:
        if any(old[k] != value[k] for k in ('instance_id','incarnation','binding_digest','kind')) or old['expected_json'] != canonical(value['expected']):
            raise RuntimeContractError('validation_identity_conflict')
        return True
    if db.execute("SELECT 1 FROM runtime_validation_records WHERE instance_id=? AND kind=? AND state='pending'", (instance,kind)).fetchone():
        raise RuntimeContractError('validation_already_pending')
    return False


def _insert_validation(db, value, task=None):
    stamp = now()
    db.execute("INSERT INTO runtime_validation_records(validation_id,instance_id,incarnation,binding_digest,kind,state,task_id,expires_at,expected_json,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?,?,?,?)",
        (value['validation_id'],value['instance_id'],value['incarnation'],value['binding_digest'],value['kind'],task,
         value['expires_at'],canonical(value['expected']),stamp,stamp))


def register_load_validation(db, instance, state, policy, value):
    if _validation_intent(db, value, instance, 'env_checked'): return True
    if state != 'loaded' or not policy or db.execute("SELECT 1 FROM instance_claims WHERE instance_id=? AND state!='exited'", (instance,)).fetchone():
        raise RuntimeContractError('validation_requires_new_load')
    _insert_validation(db, value)
    return False


def register_task_validation(db, task_id, value):
    task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if not task or task['execution_mode'] != 'worker' or _validation_intent(db,value,task['model_key'],'generated_tested'):
        raise RuntimeContractError('validation_task_conflict')
    _insert_validation(db, value, task_id)


class RuntimeTemplate:
    def __init__(self, value):
        if (type(value) is not dict or set(value) != {'schema', 'recipe_key', 'recipe_digest', 'release_digest', 'limits', 'resources'}
                or type(value['schema']) is not int or value['schema'] != 1
                or not isinstance(value['recipe_key'], str) or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,127}', value['recipe_key'])
                or any(not isinstance(value[k], str) or not re.fullmatch(r'[0-9a-f]{64}', value[k]) for k in ('recipe_digest', 'release_digest'))):
            raise RuntimeContractError('runtime_template_invalid')
        limits = value['limits']
        if (type(limits) is not dict or set(limits) != {'uid', 'gid', 'memory_bytes', 'nano_cpus', 'pids_limit', 'tmpfs_bytes'}
                or any(type(v) is not int or v <= 0 for v in limits.values())):
            raise RuntimeContractError('runtime_template_limits_invalid')
        resources = value['resources']
        if (type(resources) is not dict or set(resources) != {'base_mib', 'task_mib', 'external_reserve_mib', 'sharing_mode', 'residency', 'idle_seconds'}
                or any(type(resources[k]) is not int or not 1 <= resources[k] <= 1048576 for k in ('base_mib', 'task_mib'))
                or type(resources['external_reserve_mib']) is not int or not 0 <= resources['external_reserve_mib'] <= 1048576
                or type(resources['idle_seconds']) is not int or not 0 <= resources['idle_seconds'] <= 86400
                or resources['sharing_mode'] not in ('shared', 'exclusive') or resources['residency'] not in ('on_demand', 'idle', 'resident')):
            raise RuntimeContractError('runtime_template_resources_invalid')
        self._json = canonical(value)

    @property
    def data(self): return json.loads(self._json)
    @property
    def digest(self): return digest(self.data)


class LoraAuthority:
    """Issue base-scoped permits from immutable compatibility evidence.

    The upload request never grants execution authority.  A permit is derived
    only from a ready managed LoRA revision, an exact ready base revision, an
    immutable Runtime Profile digest and a server-produced compatibility row.
    Descriptors are replace-only publications mounted read-only by Workers.
    """
    def __init__(self, repository, root):
        self.repository, self.root = repository, checked_path(root)
        self.identity = identity(self.root.stat())
        if not self.root.is_dir() or os.name == 'posix' and self.root.stat().st_mode & 0o022:
            raise RuntimeContractError('lora_authority_not_private')

    def verify(self, forbidden=()):
        if identity(checked_path(self.root).stat()) != self.identity:
            raise RuntimeContractError('lora_authority_changed')
        boundaries = RuntimeBoundaries(self.repository)
        denied = [*boundaries.writable(), *forbidden]
        with self.repository._connect() as db:
            denied += [row[0] for row in db.execute("SELECT source_path FROM runtime_boundaries WHERE role='sealed'")]
        for raw in denied:
            path = checked_path(raw)
            if path == self.root or path in self.root.parents or self.root in path.parents or os.path.samefile(path, self.root):
                raise RuntimeContractError('lora_authority_overlap')

    def _active(self, db):
        from .asset_compatibility import AssetCompatibilityError, AssetCompatibilityManager, CURRENT_DETECTOR_VERSION
        permits = {}
        for row in db.execute('SELECT * FROM runtime_lora_permits'):
            try:
                value = json.loads(row['descriptor_json'])
                asset = db.execute(
                    'SELECT * FROM model_assets WHERE id=?',
                    (value['asset_id'],)).fetchone()
                compatibility = AssetCompatibilityManager.require_in_transaction(
                    db, value['asset_id'], value['revision'],
                    value['base_asset_id'], value['base_revision'])
                scope = digest([value['base_asset_id'], value['base_revision'],
                                value['runtime_profile_digest']])
                active = bool(
                    digest(value) == row['permit_id'] and row['scope_digest'] == scope
                    and asset and asset['state'] == 'ready'
                    and asset['role'] == 'lora' and asset['format'] == 'safetensors'
                    and asset['media_kind'] == 'image'
                    and asset['revision'] == value['revision']
                    and asset['manifest_digest'] == value['manifest_digest']
                    and digest(asset['license_declared']) == value['license_digest']
                    and value['compatibility_detector_version'] == CURRENT_DETECTOR_VERSION
                    and compatibility and compatibility['verdict'] in {'exact', 'compatible'}
                    and compatibility['evidence_digest'] == value['compatibility_evidence_digest'])
            except (AssetCompatibilityError, KeyError, TypeError, json.JSONDecodeError):
                active = False
            if bool(row['active']) != active:
                db.execute('UPDATE runtime_lora_permits SET active=? WHERE permit_id=?', (int(active), row['permit_id']))
            if active:
                key = digest([value['asset_id'], value['revision'],
                              value['base_asset_id'], value['base_revision'],
                              value['runtime_profile_digest']])
                if key in permits and permits[key] != row['permit_id']:
                    raise RuntimeContractError('lora_authority_conflict')
                permits[key] = row['permit_id']
        return {'schema': 1, 'permits': permits}

    def synchronize(self):
        self.verify()
        with exclusive_lock(self.root / 'publication.lock'):
            with self.repository._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                value = self._active(db)
                from .adapters.sdxl import read_json
                if (self.root / 'active.json').exists() and read_json(self.root / 'active.json') == value:
                    return
                # Revoke in both domains before exposing subsequent commands.
                raw=canonical(value).encode();temporary=self.root/'.active.pending'
                try:
                    with temporary.open('xb') as stream:
                        stream.write(raw);stream.flush();os.fsync(stream.fileno())
                    temporary.chmod(0o644)  # Server replaceable; Worker mount is read-only.
                except FileExistsError:
                    checked_path(temporary)
                    flags=os.O_RDONLY | getattr(os,'O_NOFOLLOW',0) | getattr(os,'O_NONBLOCK',0)
                    descriptor=os.open(temporary,flags)
                    try:
                        info=os.fstat(descriptor);observed=os.read(descriptor,len(raw)+1)
                    finally:os.close(descriptor)
                    if (observed!=raw or not stat.S_ISREG(info.st_mode) or info.st_nlink!=1
                            or os.name=='posix' and (info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o022)):
                        raise RuntimeContractError('lora_publication_unknown') from None
                os.replace(temporary, self.root / 'active.json')
                self._fsync()

    def _fsync(self):
        if os.name == 'posix':
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)

    def bind(self, reference, *, base_asset_id, base_revision,
             runtime_profile_digest, assets):
        from .asset_compatibility import AssetCompatibilityError, AssetCompatibilityManager
        from .adapters.sdxl import verify_files
        from .task_state import TaskState
        self.verify()
        if (type(reference) is not dict
                or set(reference) != {'asset_id', 'revision', 'family', 'weight'}
                or reference.get('family') != 'sdxl'
                or type(reference.get('asset_id')) is not str
                or not re.fullmatch(r'mdl_[0-9a-f]{16}', reference['asset_id'])
                or type(reference.get('revision')) is not str
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}', reference['revision'])
                or type(base_asset_id) is not str
                or not re.fullmatch(r'mdl_[0-9a-f]{16}', base_asset_id)
                or type(base_revision) is not str
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}', base_revision)
                or type(runtime_profile_digest) is not str
                or not re.fullmatch(r'[0-9a-f]{64}', runtime_profile_digest)):
            raise RuntimeContractError('lora_binding_invalid')
        try:
            compatibility = AssetCompatibilityManager(self.repository).require_task_compatible(
                reference['asset_id'], reference['revision'],
                base_asset_id, base_revision)
        except AssetCompatibilityError as exc:
            raise RuntimeContractError(exc.code) from None
        root = assets.readonly_asset_path(reference['asset_id'], reference['revision'])
        asset = self.repository.get_model_asset(reference['asset_id'])
        if (asset is None or asset['state'] != 'ready' or asset['role'] != 'lora'
                or asset['format'] != 'safetensors' or asset['media_kind'] != 'image'
                or asset['architecture_family'] != 'sdxl'
                or asset['revision'] != reference['revision']):
            raise RuntimeContractError('lora_asset_changed')
        files = [{k: item[k] for k in ('relative_path','sha256','byte_size')} for item in asset['files']]
        if len(files) != 1 or not files[0]['relative_path'].endswith('.safetensors'):
            raise RuntimeContractError('lora_format_invalid')
        verify_files(root, files)
        value = dict(schema=1, asset_id=asset['id'], revision=asset['revision'], family='sdxl',
                     manifest_digest=asset['manifest_digest'],
                     license_digest=digest(asset['license_declared']),
                     base_asset_id=base_asset_id, base_revision=base_revision,
                     runtime_profile_digest=runtime_profile_digest,
                     compatibility_detector_version=compatibility['detector_version'],
                     compatibility_evidence_digest=compatibility['evidence_digest'],
                     path='/mc-models/assets/' + reference['asset_id'],
                     files=files, weight_name=files[0]['relative_path'])
        permit = digest(value)
        scope = digest([base_asset_id, base_revision, runtime_profile_digest])
        bound = dict(reference, permit_id=permit)
        with exclusive_lock(self.root / 'publication.lock'):
            with self.repository._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                old = db.execute(
                    'SELECT * FROM runtime_lora_permits WHERE asset_id=? AND revision=? AND scope_digest=?',
                    (reference['asset_id'], reference['revision'], scope)).fetchone()
                if old and old['permit_id'] != permit: raise RuntimeContractError('lora_descriptor_conflict')
                db.execute('''INSERT OR IGNORE INTO runtime_lora_permits(
                    permit_id,asset_id,revision,scope_digest,descriptor_json,active)
                    VALUES(?,?,?,?,?,1)''',
                    (permit, reference['asset_id'], reference['revision'], scope, canonical(value)))
                db.execute('UPDATE runtime_lora_permits SET active=1 WHERE permit_id=?', (permit,))
                TaskState._check_loras(db, [bound], binding={
                    'model_asset_id': base_asset_id,
                    'model_asset_revision': base_revision,
                    'recipe_revision': runtime_profile_digest})
                target = self.root / (permit + '.json')
                try:
                    with target.open('xb') as stream:
                        stream.write(canonical(value).encode()); stream.flush(); os.fsync(stream.fileno())
                    target.chmod(0o444)
                except FileExistsError:
                    from .adapters.sdxl import read_json
                    if read_json(target) != value: raise RuntimeContractError('lora_descriptor_changed')
                self._fsync()
        self.synchronize()
        return bound


class InstallationRuntime:
    """Atomically activate only a verified image + exact licensed asset binding."""
    def __init__(self, repository, images, templates, *, fault=lambda _: None):
        self.repository, self.images, self.fault = repository, images, fault
        self.lora_authority = None
        self._user_execution = threading.local()
        self.templates = {key: RuntimeTemplate(value) for key, value in templates.items()}
        if any(key != value.data['recipe_key'] for key, value in self.templates.items()):
            raise RuntimeContractError('runtime_template_key_mismatch')

    def resolve(self, entry):
        template = self.templates.get(entry['catalog_key'])
        if template is None:
            raise RuntimeContractError('runtime_release_unavailable')
        if template.data['recipe_digest'] != digest(entry):
            raise RuntimeContractError('runtime_recipe_changed')
        release = self.images.release(template.data['release_digest'])
        worker = entry.get('worker_contract')
        if worker is not None and (type(worker) is not dict or set(worker) != {'schema','release','target','adapter_id','module','class',
                'operation','lora_families','dependencies'} or worker['schema'] != 1
                or any(type(worker[key]) is not str or not worker[key] for key in
                       ('release','target','adapter_id','module','class','operation'))
                or type(worker['lora_families']) is not list or type(worker['dependencies']) is not list
                or len(worker['lora_families']) != len(set(worker['lora_families']))
                or len(worker['dependencies']) != len(set(worker['dependencies']))
                or release.data['adapter_id'] != worker['adapter_id']):
            raise RuntimeContractError('runtime_worker_contract_changed')
        return template, release

    @staticmethod
    def _dynamic_limits(required_vram_mib):
        gib = 1024 ** 3
        if required_vram_mib >= 40960:
            ram, cpus, pids, tmp = 120 * gib, 24, 2048, 8 * gib
        elif required_vram_mib >= 32768:
            ram, cpus, pids, tmp = 96 * gib, 20, 1536, 8 * gib
        elif required_vram_mib >= 16384:
            ram, cpus, pids, tmp = 64 * gib, 16, 1024, 4 * gib
        else:
            ram, cpus, pids, tmp = 32 * gib, 12, 768, 2 * gib
        return {'uid': 1000, 'gid': 1000, 'memory_bytes': ram,
                'nano_cpus': cpus * 10 ** 9, 'pids_limit': pids,
                'tmpfs_bytes': tmp}

    @staticmethod
    def _asset_files(db, asset_id):
        return sorted((row['relative_path'], row['byte_size'], row['sha256'])
                      for row in db.execute(
                          'SELECT * FROM model_asset_files WHERE asset_id=?',
                          (asset_id,)))

    def _dynamic_image(self, db, image_digest):
        matches = []
        for row in db.execute(
                'SELECT * FROM runtime_image_bindings WHERE image_digest=? ORDER BY engine_id',
                (image_digest,)):
            release = self.images.release(row['release_digest'])
            if release.image_digest != image_digest or release.data['adapter_id'] != 'sdxl-single-file':
                continue
            if digest(json.loads(row['verification_json'])) != row['verification_digest']:
                raise RuntimeContractError('runtime_image_binding_corrupt')
            matches.append((dict(row), release))
        if not matches:
            raise RuntimeContractError('runtime_image_not_imported')
        if len(matches) != 1:
            raise RuntimeContractError('runtime_image_engine_ambiguous')
        return matches[0]

    @contextmanager
    def user_operation_lock(self, operation_id):
        if not isinstance(operation_id, str) or not re.fullmatch(r'dop_[0-9a-f]{32}', operation_id):
            raise RuntimeContractError('deployment_operation_invalid')
        self.images._boundary()
        # This lock covers the whole deployment, not just its image transfer.
        # Holding it proves an abandoned runner token can be replaced safely.
        with exclusive_lock(self.images.root / (operation_id + '.operation.lock')):
            self._user_execution.operation_id = operation_id
            try:
                yield
            finally:
                self._user_execution.operation_id = None

    @staticmethod
    def _user_recipe(payload):
        profile = payload['runtime_profile']
        return {'schema': 1, 'kind': 'user-runtime-binding',
                'profile_id': profile['profile_id'],
                'profile_revision': profile['revision'],
                'profile_digest': profile['profile_digest']}

    def prepare_user_runtime(self, operation_id, importer, checkpoint):
        """Prepare the approved image under an already-held operation lock.

        The dop installation row is an internal ownership ledger only. It is
        not a catalog installation and must never be recovered by that runner.
        """
        if getattr(self._user_execution, 'operation_id', None) != operation_id:
            raise RuntimeContractError('deployment_operation_lock_required')
        checkpoint()
        stamp = now()
        with self.repository._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            operation = db.execute('SELECT * FROM deployment_operations WHERE id=?',
                                   (operation_id,)).fetchone()
            if operation is None or operation['state'] != 'preparing_runtime':
                raise RuntimeContractError('deployment_operation_changed')
            payload = json.loads(operation['payload_json'])
            reference = payload['runtime_profile']
            row = db.execute('SELECT * FROM runtime_profiles WHERE profile_id=? AND revision=?',
                             (reference['profile_id'], reference['revision'])).fetchone()
            if row is None:
                raise RuntimeContractError('runtime_profile_unavailable')
            profile = self.repository._runtime_profile(row)
            if ({key: profile[key] for key in reference} != reference
                    or profile['profile_id'] != 'sdxl-single-file'):
                raise RuntimeContractError('runtime_profile_changed')
            candidates = []
            for row in db.execute('SELECT release_digest FROM runtime_release_records'):
                if row['release_digest'] not in self.images.approved_digests:
                    continue
                release = self.images.release(row['release_digest'])
                if (release.image_digest == reference['image_digest']
                        and release.data['adapter_id'] == 'sdxl-single-file'):
                    candidates.append(release)
            if len(candidates) != 1:
                raise RuntimeContractError('runtime_release_unavailable' if not candidates
                                           else 'runtime_release_ambiguous')
            release = candidates[0]
            attempt = 'sia_' + digest([operation_id, operation['plan_digest']])[:32]
            recipe = canonical(self._user_recipe(payload))
            ledger = db.execute('SELECT * FROM service_installations WHERE id=?',
                                (operation_id,)).fetchone()
            if ledger is None:
                db.execute('''INSERT INTO service_installations(
                    id,recipe_key,state,current_step,progress,deployment_id,transfer_id,
                    asset_id,options_json,steps_json,error_code,error_message,created_at,
                    updated_at,current_attempt_id,recipe_json)
                    VALUES(?,?,'preparing','environment',0,?,NULL,?,?,?,NULL,NULL,?,?,?,?)''',
                    (operation_id, 'sdxl-single-file', payload['deployment_id'],
                     payload['base_asset']['asset_id'], canonical({
                         'deployment_id': payload['deployment_id'], 'license_accepted': True}),
                     canonical([]), stamp, stamp, attempt, recipe))
                db.execute("INSERT INTO installation_attempts VALUES(?,?,1,'preparing',NULL,NULL,?,?)",
                           (attempt, operation_id, stamp, stamp))
            elif (ledger['current_attempt_id'] != attempt or ledger['recipe_json'] != recipe
                    or ledger['deployment_id'] != payload['deployment_id']
                    or ledger['asset_id'] != payload['base_asset']['asset_id']
                    or ledger['state'] != 'preparing'):
                raise RuntimeContractError('runtime_installation_binding_conflict')
            # The OS operation lock, not a PID/timeout guess, fences recovery.
            db.execute('UPDATE installation_attempts SET runner_token=NULL,owner_pid=NULL WHERE id=?',
                       (attempt,))
        owner = self.repository.claim_installation_runner(operation_id, attempt)
        if owner is None:
            raise InstallationOwnershipError('installation_attempt_stale')
        try:
            checkpoint()
            image = self.images.begin(owner, release.digest)
            while image['phase'] in {'queued', 'downloading'}:
                checkpoint()
                image = self.images.download(owner, image['transfer_id'], seconds=2)
            checkpoint()
            # Even ready transfers must revalidate the exact Engine readback.
            # import_pending/import_unknown are quarantined by the store.
            self.images.import_image(owner, image['transfer_id'], importer)
            checkpoint()
        finally:
            self.repository.release_installation_runner(owner)

    def settle_user_operation(self, operation_id, state, error_code=None):
        """Settle bookkeeping only; never delete shared images or model assets."""
        if state not in {'failed', 'canceled'}:
            raise RuntimeContractError('deployment_operation_invalid')
        with self.repository._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT current_attempt_id FROM service_installations WHERE id=?',
                             (operation_id,)).fetchone()
            if row is None:
                return
            operation = db.execute('SELECT plan_digest FROM deployment_operations WHERE id=?',
                                   (operation_id,)).fetchone()
            if (operation is None or row['current_attempt_id'] !=
                    'sia_' + digest([operation_id, operation['plan_digest']])[:32]):
                raise RuntimeContractError('runtime_installation_binding_conflict')
            stamp = now()
            db.execute('UPDATE service_installations SET state=?,current_step=?,error_code=?,updated_at=? WHERE id=?',
                       (state, state, error_code, stamp, operation_id))
            db.execute('UPDATE installation_attempts SET state=?,updated_at=? WHERE id=?',
                       (state, stamp, row['current_attempt_id']))

    def commit_user(self, operation_id, *, gpu_indices=None):
        """Bind a verified user deployment to one approved reusable Runtime.

        The PH-8 DeploymentOperation is the public authority.  A completed
        installation/attempt row from preparation is reused; this is never a
        second command path and performs no download or model mutation.
        """
        if not isinstance(operation_id, str) or not re.fullmatch(
                r'dop_[0-9a-f]{32}', operation_id):
            raise RuntimeContractError('deployment_operation_invalid')
        stamp = now()
        with self.repository._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            operation = db.execute(
                'SELECT * FROM deployment_operations WHERE id=?',
                (operation_id,)).fetchone()
            if operation is None or operation['state'] not in {
                    'accepted', 'preparing_runtime', 'creating_container',
                    'starting_worker', 'loading_model', 'health_check'}:
                raise RuntimeContractError('deployment_operation_changed')
            payload = json.loads(operation['payload_json'])
            if (payload.get('plan_digest') != operation['plan_digest']
                    or payload.get('deployment_id') != operation['deployment_id']):
                raise RuntimeContractError('deployment_operation_changed')
            deployment = db.execute(
                'SELECT * FROM model_deployments WHERE id=?',
                (operation['deployment_id'],)).fetchone()
            if (deployment is None or deployment['catalog_key'] != 'sdxl-single-file'
                    or deployment['current_config_revision'] is None):
                raise RuntimeContractError('runtime_deployment_missing')
            context = self.repository.configuration_context_tx(db, operation_id)
            target_revision = deployment['current_config_revision']
            if context is not None:
                if (context['incarnation'] != deployment['incarnation']
                        or deployment['pending_config_revision'] != context['revision']['config_revision']):
                    raise RuntimeContractError('deployment_config_revision_conflict')
                if context['phase'] in {'prepared', 'applying'}:
                    return json.loads(context['candidate']['installation']['binding_json'])
                if context['phase'] != 'admitted':
                    raise RuntimeContractError('deployment_configuration_changed')
                if (type(gpu_indices) is not list or len(gpu_indices) != len(payload['gpu_uuids'])
                        or any(type(index) is not int or index < 0 for index in gpu_indices)):
                    raise RuntimeContractError('runtime_gpu_mapping_required')
                deployment = dict(deployment, **context['target_deployment'])
                deployment['gpu_indices_json'] = canonical(gpu_indices)
                target_revision = context['revision']['config_revision']
            revision = db.execute(
                'SELECT * FROM model_deployment_revisions '
                'WHERE deployment_id=? AND config_revision=?',
                (deployment['id'], target_revision)).fetchone()
            if revision is None:
                raise RuntimeContractError('runtime_deployment_revision_missing')
            profile_row = db.execute(
                'SELECT * FROM runtime_profiles WHERE profile_id=? AND revision=?',
                (revision['runtime_profile_id'], revision['runtime_profile_revision'])).fetchone()
            if profile_row is None:
                raise RuntimeContractError('runtime_profile_unavailable')
            profile = self.repository._runtime_profile(profile_row)
            if (profile['profile_id'] != 'sdxl-single-file'
                    or profile['profile_digest'] != revision['runtime_profile_digest']
                    or profile['image_digest'] != revision['runtime_image_digest']
                    or profile['loader'] != 'StableDiffusionXLPipeline.from_single_file'
                    or profile['trust_remote_code']
                    or payload.get('runtime_profile') != {
                        'profile_id': profile['profile_id'],
                        'revision': profile['revision'],
                        'image_digest': profile['image_digest'],
                        'profile_digest': profile['profile_digest']}):
                raise RuntimeContractError('runtime_profile_changed')
            asset = db.execute('SELECT * FROM model_assets WHERE id=?',
                               (revision['base_asset_id'],)).fetchone()
            if (asset is None or asset['state'] != 'ready'
                    or asset['revision'] != revision['base_asset_revision']
                    or asset['manifest_digest'] != revision['base_asset_manifest_digest']
                    or asset['role'] != 'checkpoint' or asset['format'] != 'safetensors'
                    or asset['media_kind'] != 'image'
                    or asset['architecture_family'] != 'sdxl'
                    or deployment['asset_id'] != asset['id']
                    or deployment['revision'] != asset['revision']):
                raise RuntimeContractError('runtime_asset_binding_changed')
            files = self._asset_files(db, asset['id'])
            if not files:
                raise RuntimeContractError('runtime_asset_manifest_changed')

            optional_assets = {}
            if revision['vae_asset_id'] is not None:
                if 'vae' not in profile['optional_deployment_roles']:
                    raise RuntimeContractError('runtime_vae_unsupported')
                vae = db.execute('SELECT * FROM model_assets WHERE id=?',
                                 (revision['vae_asset_id'],)).fetchone()
                if (vae is None or vae['state'] != 'ready'
                        or vae['revision'] != revision['vae_asset_revision']
                        or vae['manifest_digest'] != revision['vae_asset_manifest_digest']
                        or vae['role'] != 'vae' or vae['format'] != 'safetensors'
                        or vae['media_kind'] != 'image'
                        or vae['architecture_family'] != 'sdxl'
                        or not self._asset_files(db, vae['id'])):
                    raise RuntimeContractError('runtime_optional_asset_changed')
                compatibility = db.execute(
                    "SELECT verdict FROM asset_compatibility WHERE "
                    "subject_asset_id=? AND subject_revision=? AND base_asset_id=? "
                    "AND base_revision=? ORDER BY created_at DESC LIMIT 1",
                    (vae['id'], vae['revision'], asset['id'], asset['revision'])).fetchone()
                allowed = {'exact', 'compatible'}
                if revision['experimental_compatibility_accepted']:
                    allowed.add('experimental')
                if compatibility is None or compatibility['verdict'] not in allowed:
                    raise RuntimeContractError('runtime_optional_asset_incompatible')
                optional_assets['vae'] = {
                    'asset_id': vae['id'], 'revision': vae['revision'],
                    'manifest_digest': vae['manifest_digest'],
                    'file_contract_digest': digest(self._asset_files(db, vae['id'])),
                    'asset_source': {'type': vae['source_type'],
                                     'reference': vae['source_ref']},
                    'asset_contract': {key: vae[key] for key in
                                       ('role', 'format', 'media_kind')},
                }
            image_binding, release = self._dynamic_image(db, profile['image_digest'])
            task_mib = 1024 if revision['required_vram_mib'] <= 8192 else (
                2048 if revision['required_vram_mib'] <= 20480 else 4096)
            if revision['required_vram_mib'] <= task_mib:
                raise RuntimeContractError('runtime_vram_contract_invalid')
            template = RuntimeTemplate({
                'schema': 1, 'recipe_key': 'sdxl-single-file',
                'recipe_digest': profile['profile_digest'],
                'release_digest': release.digest,
                'limits': self._dynamic_limits(revision['required_vram_mib']),
                'resources': {
                    'base_mib': revision['required_vram_mib'] - task_mib,
                    'task_mib': task_mib,
                    'external_reserve_mib': deployment['external_reserve_mib'],
                    'sharing_mode': revision['sharing_mode'],
                    'residency': revision['residency'],
                    'idle_seconds': revision['idle_seconds'],
                },
            })
            attempt_id = 'sia_' + digest([operation_id, operation['plan_digest']])[:32]
            worker = {
                'schema': 1, 'release': release.data['release_id'],
                'target': 'sdxl-single-file', 'adapter_id': 'sdxl-single-file',
                'module': 'mediacenter.adapters.sdxl_single_file',
                'class': 'SDXLSingleFileAdapter', 'operation': 'image.generate',
                'lora_families': ['sdxl'], 'dependencies': [],
            }
            confirmation = json.loads(revision['license_confirmation_json'])
            binding = {
                'schema': 1, 'instance_id': deployment['id'],
                'incarnation': deployment['incarnation'],
                'operation_id': operation_id, 'attempt_id': attempt_id,
                'release_digest': release.digest, 'catalog_key': 'sdxl-single-file',
                'model_id': deployment['model_id'], 'license': deployment['license'],
                'engine_id': image_binding['engine_id'],
                'image_digest': release.image_digest,
                'recipe_digest': profile['profile_digest'],
                'asset_id': asset['id'], 'asset_revision': asset['revision'],
                'asset_manifest_digest': asset['manifest_digest'],
                'file_contract_digest': digest(files), 'dependencies': [],
                'worker_contract': worker,
                'asset_source': {'type': asset['source_type'],
                                 'reference': asset['source_ref']},
                'asset_contract': {key: asset[key] for key in
                                   ('role', 'format', 'media_kind')},
                'runtime_options': {
                    'sharing_mode': revision['sharing_mode'],
                    'external_reserve_mib': deployment['external_reserve_mib']},
                'runtime_profile': {
                    'profile_id': profile['profile_id'], 'revision': profile['revision'],
                    'profile_digest': profile['profile_digest'],
                    'image_digest': profile['image_digest']},
                'config_revision': revision['config_revision'],
                'config_digest': revision['config_digest'],
                'optional_assets': optional_assets,
                'license_digest': digest({
                    'confirmation': confirmation, 'base': asset['license_declared'],
                    'optional': {key: value['asset_id'] for key, value in
                                 optional_assets.items()}}),
            }
            previous = db.execute(
                'SELECT * FROM instance_installation_bindings WHERE instance_id=?',
                (deployment['id'],)).fetchone()
            if previous is not None:
                if context is not None:
                    if dict(previous) != context['previous']['installation']:
                        raise RuntimeContractError('runtime_installation_binding_conflict')
                elif (previous['binding_json'] == canonical(binding)
                        and previous['template_json'] == canonical(template.data)):
                    return binding
                else:
                    raise RuntimeContractError('runtime_installation_binding_conflict')
            steps = [{'id': value, 'label': label, 'state': 'succeeded'} for value, label in (
                ('preflight', '资产与许可'), ('verify', '不可变身份复核'),
                ('environment', '共享 Runtime'), ('health', '实例绑定'))]
            ledger = db.execute('SELECT * FROM service_installations WHERE id=?',
                                (operation_id,)).fetchone()
            if (ledger is None or ledger['state'] != 'preparing'
                    or ledger['current_attempt_id'] != attempt_id
                    or ledger['recipe_json'] != canonical(self._user_recipe(payload))
                    or ledger['deployment_id'] != deployment['id']
                    or ledger['asset_id'] != asset['id']):
                raise RuntimeContractError('runtime_preparation_required')
            db.execute("UPDATE service_installations SET state='ready',current_step='health',"
                       "progress=1,steps_json=?,updated_at=? WHERE id=?",
                       (canonical(steps), stamp, operation_id))
            db.execute("UPDATE installation_attempts SET state='ready',updated_at=? WHERE id=?",
                       (stamp, attempt_id))
            if context is not None:
                candidate_row = {
                    'instance_id': deployment['id'], 'incarnation': deployment['incarnation'],
                    'operation_id': operation_id, 'attempt_id': attempt_id,
                    'release_digest': release.digest, 'recipe_digest': profile['profile_digest'],
                    'asset_id': asset['id'], 'asset_revision': asset['revision'],
                    'asset_manifest_digest': asset['manifest_digest'], 'binding_json': canonical(binding),
                    'binding_digest': digest(binding), 'template_json': canonical(template.data),
                    'template_digest': template.digest, 'installed_at': stamp,
                }
                # Hot changes retain the proven Runtime binding/template and
                # concrete container. Only policy/config head will advance.
                if not context['requires_restart']:
                    candidate_row = context['previous']['installation']
                    binding = json.loads(candidate_row['binding_json'])
                old_digest = digest(context)
                context['candidate'] = {
                    'installation': candidate_row,
                    'deployment': {key: deployment[key] for key in self.repository.CONFIGURATION_DEPLOYMENT_FIELDS},
                }
                context['phase'] = 'prepared'
                self.repository.store_configuration_context_tx(db, context, expected_digest=old_digest)
                self.fault('binding.user.candidate_prepared')
                return binding
            db.execute('INSERT INTO instance_installation_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (deployment['id'], deployment['incarnation'], operation_id, attempt_id,
                 release.digest, profile['profile_digest'], asset['id'], asset['revision'],
                 asset['manifest_digest'], canonical(binding), digest(binding),
                 canonical(template.data), template.digest, stamp))
            for kind, resource_id, resource_identity, created in (
                    ('deployment', deployment['id'], deployment['incarnation'], True),
                    ('asset', asset['id'], asset['manifest_digest'], False),
                    ('runtime-binding', deployment['id'], digest(binding), True)):
                self.repository._record_installation_resource(
                    db, (operation_id, attempt_id, None), kind, resource_id,
                    resource_identity, created, stamp)
            for value in optional_assets.values():
                self.repository._record_installation_resource(
                    db, (operation_id, attempt_id, None), 'asset', value['asset_id'],
                    value['manifest_digest'], False, stamp)
            db.execute(
                "UPDATE model_deployments SET install_state='ready',last_error=NULL,"
                "updated_at=? WHERE id=? AND incarnation=?",
                (stamp, deployment['id'], deployment['incarnation']))
            self.fault('binding.user.activate')
        return binding

    def _template(self, db, binding):
        row = db.execute(
            'SELECT template_json,template_digest FROM instance_installation_bindings '
            'WHERE instance_id=?', (binding['instance_id'],)).fetchone()
        if row is None:
            raise RuntimeContractError('runtime_installation_binding_required')
        template = RuntimeTemplate(json.loads(row['template_json']))
        if template.digest != row['template_digest']:
            raise RuntimeContractError('runtime_template_changed')
        configured = self.templates.get(binding['catalog_key'])
        if configured is not None:
            if configured.digest != template.digest:
                raise RuntimeContractError('runtime_template_changed')
            return configured
        if binding['catalog_key'] != 'sdxl-single-file':
            raise RuntimeContractError('runtime_template_changed')
        profile = binding.get('runtime_profile')
        if not isinstance(profile, dict):
            raise RuntimeContractError('runtime_profile_changed')
        current = db.execute(
            'SELECT profile_digest,image_digest FROM runtime_profiles '
            'WHERE profile_id=? AND revision=?',
            (profile.get('profile_id'), profile.get('revision'))).fetchone()
        if (current is None or current['profile_digest'] != profile.get('profile_digest')
                or current['image_digest'] != profile.get('image_digest')
                or template.data['recipe_digest'] != profile.get('profile_digest')):
            raise RuntimeContractError('runtime_profile_changed')
        return template

    def commit(self, owner, deployment_id, transfer_id, entry):
        template, release = self.resolve(entry)
        stamp = now()
        with self.repository._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            operation = self.repository._assert_installation_owner(db, owner, INSTALL_ACTIVE | {'ready'})
            if json.loads(operation['recipe_json']) != entry:
                raise RuntimeContractError('runtime_recipe_changed')
            options = json.loads(operation['options_json'])
            if options.get('license_accepted') is not True or options.get('deployment_id') != deployment_id:
                raise RuntimeContractError('runtime_license_binding_missing')
            deployment = db.execute('SELECT * FROM model_deployments WHERE id=?', (deployment_id,)).fetchone()
            if deployment is None:
                raise RuntimeContractError('runtime_deployment_missing')
            self.repository._assert_deployment_owner(db, owner, deployment_id, deployment['incarnation'])
            asset = db.execute('SELECT * FROM model_assets WHERE id=?', (deployment['asset_id'],)).fetchone()
            if (not asset or asset['state'] != 'ready' or asset['revision'] != entry['recommended_revision']
                    or asset['role'] != entry['service_recipe']['role'] or asset['format'] != entry['service_recipe']['format']
                    or asset['media_kind'] != entry['kind']
                    or asset['license_declared'] != entry['license'] or deployment['catalog_key'] != entry['catalog_key']
                    or deployment['model_id'] != entry['model_id'] or deployment['revision'] != asset['revision']):
                raise RuntimeContractError('runtime_asset_binding_changed')
            actual_files = sorted((r['relative_path'], r['byte_size'], r['sha256']) for r in db.execute('SELECT * FROM model_asset_files WHERE asset_id=?', (asset['id'],)))
            expected_files = sorted((r['relative_path'], r['byte_size'], r['sha256']) for r in entry['service_recipe']['files'])
            if actual_files != expected_files:
                raise RuntimeContractError('runtime_asset_manifest_changed')
            transfer = db.execute('SELECT * FROM runtime_image_transfers WHERE transfer_id=? AND operation_id=? AND attempt_id=?',
                                  (transfer_id, owner[0], owner[1])).fetchone()
            if not transfer or transfer['phase'] != 'ready' or transfer['release_digest'] != release.digest:
                raise RuntimeContractError('runtime_image_not_imported')
            imported = db.execute('SELECT * FROM runtime_image_bindings WHERE engine_id=? AND image_digest=?',
                                  (transfer['engine_id'], release.image_digest)).fetchone()
            if not imported or digest(json.loads(imported['verification_json'])) != imported['verification_digest']:
                raise RuntimeContractError('runtime_image_binding_corrupt')
            dependencies = []
            for item in db.execute('SELECT * FROM model_deployment_dependencies WHERE deployment_id=? ORDER BY dependency_key', (deployment_id,)):
                dep = db.execute('SELECT * FROM model_deployments WHERE id=?', (item['dependency_deployment_id'],)).fetchone()
                binding = db.execute('SELECT * FROM instance_installation_bindings WHERE instance_id=?', (item['dependency_deployment_id'],)).fetchone()
                if (not dep or not binding or dep['install_state'] != 'ready'
                        or binding['incarnation'] != dep['incarnation'] or dep['asset_id'] != item['dependency_asset_id']
                        or dep['revision'] != item['dependency_revision']
                        or digest(json.loads(binding['binding_json'])) != binding['binding_digest']):
                    raise RuntimeContractError('runtime_dependency_binding_changed')
                # The dependency's old binding digest is not sufficient when its
                # current asset, approval or transitive dependency has drifted.
                self._binding(db, dep['id'], frozenset({deployment_id}))
                dependencies.append({'dependency_key': item['dependency_key'], 'deployment_id': dep['id'],
                    'incarnation': dep['incarnation'], 'asset_id': dep['asset_id'], 'revision': dep['revision'],
                    'binding_digest': binding['binding_digest']})
            if {item['dependency_key'] for item in dependencies} != set(entry['service_recipe'].get('prerequisites', [])):
                raise RuntimeContractError('runtime_dependency_binding_changed')
            binding = {'schema': 1, 'instance_id': deployment_id, 'incarnation': deployment['incarnation'],
                'operation_id': owner[0], 'attempt_id': owner[1], 'release_digest': release.digest,
                'catalog_key': entry['catalog_key'], 'model_id': entry['model_id'], 'license': entry['license'],
                'engine_id': transfer['engine_id'], 'image_digest': release.image_digest,
                'recipe_digest': digest(entry), 'asset_id': asset['id'], 'asset_revision': asset['revision'],
                'asset_manifest_digest': asset['manifest_digest'], 'file_contract_digest': digest(actual_files), 'dependencies': dependencies,
                'worker_contract': entry.get('worker_contract'),
                'asset_source': {'type':asset['source_type'], 'reference':asset['source_ref']},
                'asset_contract': {key: asset[key] for key in ('role', 'format', 'media_kind')},
                'runtime_options': {'sharing_mode':options.get('gpu_sharing_mode', template.data['resources']['sharing_mode']),
                                    'external_reserve_mib':options.get('external_reserve_mib', template.data['resources']['external_reserve_mib'])},
                'license_digest': digest({'model_id': entry['model_id'], 'revision': entry['recommended_revision'],
                                         'license': entry['license'], 'files': entry['service_recipe']['files']})}
            previous = db.execute('SELECT * FROM instance_installation_bindings WHERE instance_id=?', (deployment_id,)).fetchone()
            if previous:
                if previous['binding_json'] == canonical(binding) and previous['template_json'] == canonical(template.data):
                    if operation['state'] != 'ready':
                        raise RuntimeContractError('runtime_installation_state_corrupt')
                    return binding
                if options.get('adopt_existing') is not True:
                    raise RuntimeContractError('runtime_installation_binding_conflict')
                if (previous['incarnation'] != deployment['incarnation']
                        or previous['asset_id'] != asset['id']
                        or previous['asset_revision'] != asset['revision']
                        or previous['asset_manifest_digest'] != asset['manifest_digest']
                        or digest(json.loads(previous['binding_json'])) != previous['binding_digest']
                        or digest(json.loads(previous['template_json'])) != previous['template_digest']):
                    raise RuntimeContractError('runtime_installation_binding_corrupt')
                if db.execute("SELECT 1 FROM instance_claims WHERE instance_id=? AND state!='exited'",
                              (deployment_id,)).fetchone():
                    raise RuntimeContractError('runtime_installation_binding_active')
                db.execute('''UPDATE instance_installation_bindings SET
                    incarnation=?,operation_id=?,attempt_id=?,release_digest=?,recipe_digest=?,asset_id=?,
                    asset_revision=?,asset_manifest_digest=?,binding_json=?,binding_digest=?,template_json=?,
                    template_digest=?,installed_at=? WHERE instance_id=?''',
                    (deployment['incarnation'], owner[0], owner[1], release.digest, digest(entry), asset['id'],
                     asset['revision'], asset['manifest_digest'], canonical(binding), digest(binding),
                     canonical(template.data), template.digest, stamp, deployment_id))
            else:
                db.execute('INSERT INTO instance_installation_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (deployment_id, deployment['incarnation'], owner[0], owner[1], release.digest, digest(entry),
                     asset['id'], asset['revision'], asset['manifest_digest'], canonical(binding), digest(binding),
                     canonical(template.data), template.digest, stamp))
            self.repository._record_installation_resource(db, owner, 'asset', asset['id'], asset['manifest_digest'], False, stamp)
            self.repository._record_installation_resource(db, owner, 'runtime-binding', deployment_id, digest(binding), True, stamp)
            self.fault('binding.insert')
            default = bool(deployment['is_default']) or not db.execute(
                'SELECT 1 FROM model_deployments WHERE kind=? AND is_default=1 AND id<>?',
                (deployment['kind'], deployment_id)).fetchone()
            service_enabled = int(options.get('startup_policy') == 'auto')
            db.execute("UPDATE model_deployments SET install_state='ready',enabled=?,is_default=?,last_error=NULL,updated_at=? WHERE id=? AND incarnation=?",
                       (service_enabled, int(default), stamp, deployment_id, deployment['incarnation']))
            steps = [{'id': key, 'label': label, 'state': 'succeeded'} for key,label in
                     [('preflight','固定配方与许可'),('download','模型制品'),('verify','权重校验'),('environment','镜像内容导入'),('health','不可变安装绑定')]]
            db.execute("UPDATE service_installations SET state='ready',current_step='health',progress=1,asset_id=?,deployment_id=?,steps_json=?,error_code=NULL,error_message=NULL,updated_at=? WHERE id=?",
                       (asset['id'], deployment_id, canonical(steps), stamp, owner[0]))
            db.execute("UPDATE installation_attempts SET state='ready',updated_at=? WHERE id=?", (stamp, owner[1]))
            self.fault('binding.activate')
        return binding

    def get(self, instance):
        with self.repository._connect() as db:
            return self._binding(db, instance, frozenset())

    def template(self, instance):
        """Return the verified per-instance Runtime template."""
        with self.repository._connect() as db:
            binding = self._binding(db, instance, frozenset())
            if binding is None:
                raise RuntimeContractError('runtime_installation_binding_required')
            return self._template(db, binding)

    def _binding(self, db, instance, visiting):
        if instance in visiting:
            raise RuntimeContractError('runtime_dependency_cycle')
        row = db.execute('SELECT * FROM instance_installation_bindings WHERE instance_id=?', (instance,)).fetchone()
        if not row: return None
        binding = json.loads(row['binding_json'])
        if (digest(binding) != row['binding_digest'] or digest(json.loads(row['template_json'])) != row['template_digest']
                or any(binding[key] != row[key] for key in ('incarnation', 'operation_id', 'attempt_id', 'release_digest', 'recipe_digest', 'asset_id', 'asset_revision', 'asset_manifest_digest'))):
            raise RuntimeContractError('runtime_installation_binding_corrupt')
        template = self._template(db, binding)
        release = self.images.release(binding['release_digest'])
        if release.image_digest != binding['image_digest']:
            raise RuntimeContractError('runtime_image_binding_changed')
        if (binding['catalog_key'] == 'sdxl-single-file'
                and release.data['adapter_id'] != 'sdxl-single-file'):
            raise RuntimeContractError('runtime_image_binding_changed')
        imported = db.execute('SELECT * FROM runtime_image_bindings WHERE engine_id=? AND image_digest=?', (binding['engine_id'], binding['image_digest'])).fetchone()
        if not imported or digest(json.loads(imported['verification_json'])) != imported['verification_digest']:
            raise RuntimeContractError('runtime_image_binding_corrupt')
        deployment = db.execute('SELECT * FROM model_deployments WHERE id=?', (instance,)).fetchone()
        asset = db.execute('SELECT * FROM model_assets WHERE id=?', (row['asset_id'],)).fetchone()
        if (not deployment or deployment['incarnation'] != row['incarnation'] or deployment['asset_id'] != row['asset_id']
                or deployment['revision'] != row['asset_revision'] or deployment['catalog_key'] != binding['catalog_key']
                or deployment['model_id'] != binding['model_id'] or deployment['license'] != binding['license']
                or deployment['install_state'] != 'ready' or not asset or asset['state'] != 'ready'
                or asset['revision'] != row['asset_revision'] or asset['manifest_digest'] != row['asset_manifest_digest']
                or {'type':asset['source_type'], 'reference':asset['source_ref']} != binding['asset_source']
                or {key:asset[key] for key in ('role', 'format', 'media_kind')} != binding['asset_contract']
                or asset['license_declared'] != binding['license']):
            raise RuntimeContractError('runtime_installation_binding_changed')
        files = sorted((r['relative_path'], r['byte_size'], r['sha256']) for r in db.execute('SELECT * FROM model_asset_files WHERE asset_id=?', (asset['id'],)))
        if digest(files) != binding['file_contract_digest']:
            raise RuntimeContractError('runtime_asset_manifest_changed')
        if binding['catalog_key'] == 'sdxl-single-file':
            bound_revision = db.execute(
                'SELECT * FROM model_deployment_revisions WHERE deployment_id=? '
                'AND config_revision=?',
                (instance, binding.get('config_revision'))).fetchone()
            current_revision = db.execute(
                'SELECT * FROM model_deployment_revisions WHERE deployment_id=? '
                'AND config_revision=?',
                (instance, deployment['current_config_revision'])).fetchone()
            context = self.repository.active_configuration_context_tx(db, instance)
            if context is not None and context['phase'] == 'applying':
                policy = db.execute('SELECT configuration_state FROM instance_policies WHERE instance_id=?',
                                    (instance,)).fetchone()
                if (policy is None or policy['configuration_state'] not in {'applying', 'failed'}
                        or dict(row) != context['candidate']['installation']
                        or deployment['pending_config_revision'] != context['revision']['config_revision']):
                    raise RuntimeContractError('runtime_deployment_revision_changed')
                current_revision = db.execute(
                    'SELECT * FROM model_deployment_revisions WHERE deployment_id=? AND config_revision=?',
                    (instance, deployment['pending_config_revision'])).fetchone()
            immutable = (
                'runtime_profile_id', 'runtime_profile_revision',
                'runtime_profile_digest', 'runtime_image_digest',
                'base_asset_id', 'base_asset_revision', 'base_asset_manifest_digest',
                'vae_asset_id', 'vae_asset_revision', 'vae_asset_manifest_digest',
            )
            if (bound_revision is None or current_revision is None
                    or bound_revision['config_digest'] != binding.get('config_digest')
                    or bound_revision['runtime_profile_digest']
                        != binding['runtime_profile']['profile_digest']
                    or any(bound_revision[key] != current_revision[key] for key in immutable)):
                raise RuntimeContractError('runtime_deployment_revision_changed')
            optional = binding.get('optional_assets')
            expected_optional = {'vae'} if current_revision['vae_asset_id'] else set()
            if not isinstance(optional, dict) or set(optional) != expected_optional:
                raise RuntimeContractError('runtime_optional_asset_changed')
            for role, expected in optional.items():
                current = db.execute('SELECT * FROM model_assets WHERE id=?',
                                     (expected['asset_id'],)).fetchone()
                current_files = self._asset_files(db, expected['asset_id']) if current else []
                if (role != 'vae' or current is None or current['state'] != 'ready'
                        or current['revision'] != expected['revision']
                        or current['manifest_digest'] != expected['manifest_digest']
                        or digest(current_files) != expected['file_contract_digest']
                        or {'type': current['source_type'],
                            'reference': current['source_ref']} != expected['asset_source']
                        or {key: current[key] for key in ('role','format','media_kind')}
                            != expected['asset_contract']):
                    raise RuntimeContractError('runtime_optional_asset_changed')
        dependencies = db.execute('SELECT * FROM model_deployment_dependencies WHERE deployment_id=? ORDER BY dependency_key', (instance,)).fetchall()
        if len(dependencies) != len(binding['dependencies']):
            raise RuntimeContractError('runtime_dependency_binding_changed')
        for current, expected in zip(dependencies, binding['dependencies']):
            if (current['dependency_key'] != expected['dependency_key'] or current['dependency_deployment_id'] != expected['deployment_id']
                    or current['dependency_asset_id'] != expected['asset_id'] or current['dependency_revision'] != expected['revision']):
                raise RuntimeContractError('runtime_dependency_binding_changed')
            child = self._binding(db, expected['deployment_id'], visiting | {instance})
            if not child or digest(child) != expected['binding_digest']:
                raise RuntimeContractError('runtime_dependency_binding_changed')
        return binding

    def validation_intent(self, instance, kind, key, *, timeout_seconds=600, expected=None):
        if kind not in {'env_checked','generated_tested'} or type(key) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}', key):
            raise RuntimeContractError('validation_request_invalid')
        if type(timeout_seconds) is not int or not 10 <= timeout_seconds <= 3600:
            raise RuntimeContractError('validation_budget_invalid')
        binding = self.get(instance)
        if binding is None: raise RuntimeContractError('runtime_installation_binding_required')
        release = self.images.release(binding['release_digest'])
        if not release.data['adapter_id']: raise RuntimeContractError('validation_adapter_unavailable')
        target = dict(expected or {}, release_digest=binding['release_digest'], image_digest=binding['image_digest'],
                      sdk_digest=release.data['sdk_digest'], loads_gpu=kind=='env_checked')
        return dict(validation_id='val_'+digest([instance,kind,key]), instance_id=instance, incarnation=binding['incarnation'],
            binding_digest=digest(binding),kind=kind,expires_at=(datetime.now(timezone.utc)+timedelta(seconds=timeout_seconds)).isoformat(),
            expected=target)

    def validation(self, identifier):
        with self.repository._connect() as db:
            row = db.execute('SELECT * FROM runtime_validation_records WHERE validation_id=?', (identifier,)).fetchone()
        if not row: raise RuntimeContractError('validation_not_found')
        return {k:row[k] for k in ('validation_id','instance_id','kind','state','task_id','attempt_id','error_code','created_at','updated_at','version',
                                   'binding_digest','operation_id','claim_id','epoch','command_digest','evidence_digest')}

    def cancel_validation(self, identifier, version):
        if type(version) is not int or version < 1: raise RuntimeContractError('validation_version_invalid')
        with self.repository._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM runtime_validation_records WHERE validation_id=?', (identifier,)).fetchone()
            if not row or row['version'] != version: raise RuntimeContractError('validation_version_conflict')
            if row['state'] == 'pending':
                db.execute("UPDATE runtime_validation_records SET state='failed',error_code='validation_canceled',version=version+1,updated_at=? WHERE validation_id=?", (now(),identifier))
        return self.validation(identifier)

    def _validation_snapshot(self, db, row):
        from .task_state import TaskState
        from .protocol import validate_envelope
        if row['state'] != 'pending': return None
        if row['expires_at'] is None or row['expires_at'] <= now(): raise RuntimeContractError('validation_timed_out')
        binding = self._binding(db, row['instance_id'], frozenset())
        if binding is None or digest(binding) != row['binding_digest'] or binding['incarnation'] != row['incarnation']:
            raise RuntimeContractError('validation_binding_changed')
        if row['kind'] == 'env_checked' and not row['operation_id']:
            return None
        if row['kind'] == 'generated_tested' and not row['attempt_id']:
            task = db.execute('SELECT status FROM tasks WHERE id=?', (row['task_id'],)).fetchone()
            if task and task['status'] in {'failed','canceled','interrupted'}:
                raise RuntimeContractError('validation_generation_failed')
            return None
        claim = db.execute('SELECT * FROM instance_claims WHERE claim_id=?', (row['claim_id'],)).fetchone()
        if not claim or claim['instance_id'] != row['instance_id'] or claim['epoch'] != row['epoch']:
            raise RuntimeContractError('validation_execution_changed')
        base = {'validation_id':row['validation_id'],'version':row['version'],'binding_digest':row['binding_digest'],
                'claim_id':row['claim_id'],'epoch':row['epoch'],'command_digest':row['command_digest']}
        if row['kind'] == 'env_checked':
            policy = db.execute('SELECT * FROM instance_policies WHERE instance_id=?', (row['instance_id'],)).fetchone()
            if not policy or policy['desired_state'] != 'loaded' or policy['revision'] != row['policy_revision'] or claim['stop_reason'] or claim['state']=='exited':
                raise RuntimeContractError('validation_load_fenced')
            op = db.execute('SELECT * FROM model_operations WHERE operation_id=?', (row['operation_id'],)).fetchone()
            if not op or op['claim_id'] != row['claim_id'] or op['action'] != 'load' or op['command_digest'] != row['command_digest']:
                raise RuntimeContractError('validation_operation_changed')
            command = db.execute('SELECT * FROM task_outbox WHERE message_id=?', (op['command_id'],)).fetchone()
            message = validate_envelope(json.loads(command['envelope_json'])) if command else None
            if not message or digest(message) != row['command_digest'] or message['type'] != 'model.load':
                raise RuntimeContractError('validation_command_changed')
            if op['state'] in {'failed','canceled','interrupted'}: raise RuntimeContractError('validation_load_failed')
            if op['state'] != 'succeeded' or claim['state'] != 'loaded' or not claim['registered']: return None
            terminals = db.execute("SELECT * FROM instance_inbox WHERE scope_id=? AND claim_id=? AND result='applied' ORDER BY event_seq DESC LIMIT 2", (row['operation_id'],row['claim_id'])).fetchall()
            for item in terminals:
                event = validate_envelope(json.loads(item['envelope_json']))
                if digest(event) != item['digest']: raise RuntimeContractError('validation_event_corrupt')
                if event['type'] == 'model.terminal' and event['payload']['status'] == 'succeeded':
                    return dict(base,operation_id=row['operation_id'],event_id=item['message_id'],event_digest=item['digest'])
            return None
        task = db.execute('SELECT * FROM tasks WHERE id=?', (row['task_id'],)).fetchone()
        if not task or task['current_attempt_id'] != row['attempt_id'] or task['cancel_revision'] != row['cancel_revision']:
            raise RuntimeContractError('validation_attempt_changed')
        TaskState._check_loras(
            db, json.loads(task['lora_bindings_json']),
            binding=json.loads(task['binding_json'] or 'null'))
        if task['status'] in {'failed','canceled','interrupted'}: raise RuntimeContractError('validation_generation_failed')
        if task['status'] != 'succeeded': return None
        seal = db.execute("SELECT s.* FROM task_seals s JOIN task_artifacts a ON a.task_id=s.task_id AND a.attempt_id=s.attempt_id AND a.asset_id=s.asset_id AND a.revision=s.revision AND a.sha256=s.sha256 AND a.byte_size=s.byte_size AND a.relative_path=s.relative_path JOIN task_attempts t ON t.id=s.attempt_id WHERE s.task_id=? AND s.attempt_id=? AND a.origin='sealed' AND s.cancel_revision=? AND t.instance_id=? AND t.epoch=?",
            (row['task_id'],row['attempt_id'],row['cancel_revision'],row['instance_id'],row['epoch'])).fetchone()
        command = db.execute('SELECT * FROM task_outbox WHERE task_id=? AND attempt_id=? AND digest=?', (row['task_id'],row['attempt_id'],row['command_digest'])).fetchone()
        if not seal or not command or digest(json.loads(command['envelope_json'])) != row['command_digest']:
            raise RuntimeContractError('validation_seal_changed')
        return dict(base,task_id=row['task_id'],attempt_id=row['attempt_id'],seal=dict(seal),parameters=json.loads(command['envelope_json'])['payload']['parameters'])

    def reconcile_validations(self, artifacts, *, after='', limit=20):
        """Bounded exact-identity recovery, independent of a Redis delivery callback."""
        with self.repository._connect() as db:
            rows = db.execute("SELECT * FROM runtime_validation_records WHERE state='pending' AND validation_id>? ORDER BY validation_id LIMIT ?", (after,limit)).fetchall()
        for row in rows:
            identifier = row['validation_id']
            try:
                with self.repository._connect() as db: snapshot = self._validation_snapshot(db,row)
                if snapshot is None: continue
                evidence = dict(snapshot, expected=json.loads(row['expected_json']))
                if row['kind'] == 'generated_tested':
                    if artifacts is None: continue
                    from .artifacts import file_hash
                    import time
                    started = time.monotonic(); seal = snapshot['seal']
                    if not 0 < seal['byte_size'] <= 64*1024**2: raise RuntimeContractError('validation_media_limit')
                    with artifacts.authorize(seal['relative_path']) as authorized:
                        if authorized.origin != 'sealed' or authorized.sha256 != seal['sha256'] or authorized.size != seal['byte_size']:
                            raise RuntimeContractError('validation_authorization_changed')
                        stream = authorized.stream; stream.seek(0)
                        expected = snapshot['parameters']
                        evidence['decoded'] = _decode_validation_media(stream, seal['relative_path'], expected)
                        stream.seek(0)
                        if file_hash(stream,seal['byte_size']) != (seal['sha256'],seal['byte_size']) or time.monotonic()-started>10:
                            raise RuntimeContractError('validation_media_changed')
                self.fault('validation.before_commit')
                with self.repository._connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    latest = db.execute('SELECT * FROM runtime_validation_records WHERE validation_id=?', (identifier,)).fetchone()
                    if not latest or self._validation_snapshot(db,latest) != snapshot: continue
                    db.execute("UPDATE runtime_validation_records SET state='passed',evidence_json=?,evidence_digest=?,version=version+1,updated_at=? WHERE validation_id=? AND state='pending' AND version=?",
                               (canonical(evidence),digest(evidence),now(),identifier,row['version']))
                    self.fault('validation.committed')
            except (RuntimeContractError, TaskStateError, ValueError, OSError, ImportError, SyntaxError) as error:
                code = 'validation_decoder_unavailable' if isinstance(error,ImportError) else getattr(error,'code','validation_media_failed')
                with self.repository._connect() as db:
                    db.execute("UPDATE runtime_validation_records SET state=?,error_code=?,version=version+1,updated_at=? WHERE validation_id=? AND state='pending' AND version=?",
                               ('unknown' if code in {'validation_timed_out','validation_decoder_unavailable'} else 'failed',code,now(),identifier,row['version']))
        return rows[-1]['validation_id'] if len(rows)==limit else ''

    def levels(self, instance):
        binding = self.get(instance)
        result = {'installed': binding is not None, 'env_checked': False, 'model_ready': False, 'generated_tested': False}
        if binding is None: return result
        with self.repository._connect() as db:
            claim = db.execute("SELECT * FROM instance_claims WHERE instance_id=? AND state='loaded' AND stop_reason IS NULL", (instance,)).fetchone()
            result['model_ready'] = bool(claim and claim['incarnation'] == binding['incarnation'])
            for record in db.execute("SELECT * FROM runtime_validation_records WHERE instance_id=? AND incarnation=? AND binding_digest=? AND state='passed'",
                                     (instance, binding['incarnation'], digest(binding))):
                if not record['evidence_json'] or digest(json.loads(record['evidence_json'])) != record['evidence_digest']:
                    raise RuntimeContractError('runtime_validation_corrupt')
                if record['kind'] == 'generated_tested':
                    if not db.execute("SELECT 1 FROM tasks t JOIN task_artifacts a ON a.task_id=t.id WHERE t.id=? AND t.current_attempt_id=? AND t.status='succeeded' AND a.origin='sealed'", (record['task_id'], record['attempt_id'])).fetchone():
                        continue
                result[record['kind']] = True
        return result


class EpochPublisher:
    """Fixed ACL publisher with independent credentials and durable intent.

    Ordinary RedisTransport roles never receive this object or its endpoint.
    Unknown SETUSER is resolved by readback only, never by resetting the user.
    ACL SAVE may be repeated with identical memory readback to establish a
    successful durable acknowledgement; it does not rotate any credential.
    """
    TEMPLATE_SHA256 = '7539e36da401352fc4cc0469b93e664656c094d6d215e8aa906623f382725c10'

    def __init__(self, repository, server_id, endpoint, seed_file, state_root, *, client=None, fault=lambda _: None):
        from .transport import token
        from .redis_transport import _secret
        import redis
        token(server_id)
        if (endpoint.username == 'default' or endpoint.username.startswith(('mc_s_', 'mc_w_'))
                or not endpoint.unix_socket):
            raise RuntimeContractError('runtime_publisher_identity_invalid')
        self.repository, self.server_id, self.endpoint = repository, server_id, endpoint
        self.seed_file = checked_path(seed_file)
        self.seed_identity = identity(self.seed_file.stat())
        self.seed = _secret(self.seed_file).encode('ascii')
        self.secret_file = checked_path(endpoint.secret_file)
        self.secret_identity = identity(self.secret_file.stat())
        if self.seed_identity == self.secret_identity:
            raise RuntimeContractError('runtime_publisher_seed_not_separate')
        self.root = checked_path(state_root)
        if not self.root.is_dir() or os.name == 'posix' and self.root.stat().st_mode & 0o077:
            raise RuntimeContractError('runtime_publisher_root_not_private')
        self.root_identity = identity(self.root.stat())
        self.template = (Path(__file__).parent.parent / 'deploy' / 'redis-acl.template').read_bytes()
        if hashlib.sha256(self.template).hexdigest() != self.TEMPLATE_SHA256:
            raise RuntimeContractError('runtime_acl_template_changed')
        options = endpoint.options()  # validates private explicit manager secret
        self.secret_digest = hashlib.sha256(options['password'].encode('ascii')).hexdigest()
        self.authority_digest = digest({'server_id': server_id, 'username': endpoint.username,
            'socket': endpoint.unix_socket, 'seed': hashlib.sha256(self.seed).hexdigest(),
            'seed_identity': self.seed_identity, 'template': self.TEMPLATE_SHA256,
            'secret_identity': self.secret_identity})
        self.client = client if client is not None else redis.Redis(**options)
        self.fault = fault

    def close(self): self.client.close()

    def credentials(self, instance, epoch):
        from .transport import Identity
        value = Identity(self.server_id, instance, epoch)
        name = digest([value.server_id, value.instance_id, value.worker_epoch])
        return {role: {'username': 'mc_' + role[0] + '_' + name,
                       'password': hmac.new(self.seed, canonical([self.server_id, instance, epoch, role]).encode(), hashlib.sha256).hexdigest()}
                for role in ('server', 'worker')}

    def rules(self, instance, epoch):
        from .transport import Identity
        from .redis_transport import render_acl
        credentials = self.credentials(instance, epoch)
        body = render_acl(self.template.decode('utf-8'), Identity(self.server_id, instance, epoch),
            server_user=credentials['server']['username'], worker_user=credentials['worker']['username'],
            server_secret_sha256=hashlib.sha256(credentials['server']['password'].encode()).hexdigest(),
            worker_secret_sha256=hashlib.sha256(credentials['worker']['password'].encode()).hexdigest())
        result = {}
        for line in body.splitlines():
            if line == 'user default off': continue
            pieces = re.findall(r'\([^()]*\)|\S+', line)
            if pieces[:1] != ['user'] or pieces[1] not in {v['username'] for v in credentials.values()}:
                raise RuntimeContractError('runtime_acl_template_changed')
            result[pieces[1]] = pieces[2:]
        return result

    @staticmethod
    def _expected(rules):
        selectors = []
        for value in rules:
            if value.startswith('('):
                tokens = value[1:-1].split()
                selectors.append({'commands': sorted(['-@all'] + [v for v in tokens if v.startswith('+')]),
                                  'keys': sorted(v for v in tokens if v.startswith('~')),
                                  'channels': sorted(v for v in tokens if v.startswith('&'))})
        return {'flags': ['on', 'sanitize-payload'], 'passwords': sorted(v[1:] for v in rules if v.startswith('#')),
                'commands': sorted(v for v in rules if v.startswith(('+', '-'))), 'keys': [], 'channels': [],
                'selectors': sorted(selectors, key=canonical)}

    @staticmethod
    def _observed(value):
        if value is None: return None
        selectors = []
        for raw in value.get('selectors', []):
            row = dict(zip(raw[::2], raw[1::2])) if isinstance(raw, list) else raw
            selectors.append({'commands': sorted(row['commands'].split()), 'keys': sorted(row['keys'].split()),
                              'channels': sorted(row['channels'].split())})
        return {'flags': sorted(value['flags']), 'passwords': sorted(value['passwords']),
                'commands': sorted(value['commands'] + value.get('categories', [])),
                'keys': sorted(value['keys']), 'channels': sorted(value.get('channels', [])),
                'selectors': sorted(selectors, key=canonical)}

    def _source_unchanged(self):
        from .redis_transport import _secret
        try:
            return (identity(checked_path(self.root).stat()) == self.root_identity
                    and identity(checked_path(self.seed_file).stat()) == self.seed_identity
                    and _secret(self.seed_file).encode('ascii') == self.seed
                    and identity(checked_path(self.secret_file).stat()) == self.secret_identity
                    and hashlib.sha256(_secret(self.secret_file).encode('ascii')).hexdigest() == self.secret_digest)
        except (OSError, ValueError):
            return False

    def _raw_row(self, package_id, *, historical_server=False):
        from .transport import token
        with self.repository._connect() as db:
            row = db.execute('SELECT * FROM runtime_epoch_packages WHERE package_id=?', (package_id,)).fetchone()
        if not row:
            raise RuntimeContractError('runtime_publisher_authority_changed')
        record = json.loads(row['record_json'])
        expected = {'instance_id': row['instance_id'], 'worker_epoch': row['epoch']}
        identity_record = record.get('identity')
        if (digest(record) != row['record_digest'] or not isinstance(identity_record, dict)
                or any(identity_record.get(key) != value for key, value in expected.items())
                or not historical_server and identity_record.get('server_id') != self.server_id):
            raise RuntimeContractError('runtime_package_record_corrupt')
        try:
            token(identity_record.get('server_id'))
        except (TypeError, ValueError):
            raise RuntimeContractError('runtime_package_record_corrupt') from None
        return dict(row), record

    def _row(self, package_id):
        row, record = self._raw_row(package_id)
        if row['authority_digest'] != self.authority_digest:
            raise RuntimeContractError('runtime_publisher_authority_changed')
        return row, record

    def adoption_candidate(self, package_id):
        """Prove that an inode-only authority change is semantically equivalent.

        This is intentionally not a compatibility read path.  A package may be
        adopted only when its signed record is intact, current publisher sources
        have not changed since startup, and both live Redis ACL users exactly
        match the rules derived by the current authority.
        """
        row, record = self._raw_row(package_id)
        if row['authority_digest'] == self.authority_digest:
            return row, record, None
        if not self._source_unchanged():
            raise RuntimeContractError('runtime_publisher_authority_changed')
        rules = self.rules(row['instance_id'], row['epoch'])
        if record.get('acl', {}).get('rules_digest') != digest(rules):
            raise RuntimeContractError('runtime_publisher_authority_changed')
        for user, commands in rules.items():
            if self._observed(self.client.acl_getuser(user)) != self._expected(commands):
                raise RuntimeContractError('runtime_publisher_authority_changed')
        return row, record, row['authority_digest']

    def adopt_equivalent(self, package_id, expected_authority):
        with exclusive_lock(self.root / 'epoch-acl.lock'):
            row, record, previous = self.adoption_candidate(package_id)
            if previous is None:
                return row, record
            if previous != expected_authority:
                raise RuntimeContractError('runtime_package_changed')
            with self.repository._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                changed = db.execute(
                    'UPDATE runtime_epoch_packages SET authority_digest=?,updated_at=? '
                    'WHERE package_id=? AND authority_digest=? AND record_digest=? AND phase=?',
                    (self.authority_digest, now(), package_id, previous,
                     row['record_digest'], row['phase'])).rowcount
                if changed != 1:
                    raise RuntimeContractError('runtime_package_changed')
        return self._row(package_id)

    def _checkpoint(self, row, record, phase, error=None):
        with self.repository._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute('UPDATE runtime_epoch_packages SET phase=?,record_json=?,record_digest=?,error_code=?,updated_at=? WHERE package_id=? AND record_digest=? AND phase=?',
                (phase, canonical(record), digest(record), error, now(), row['package_id'], row['record_digest'], row['phase'])).rowcount
            if changed != 1: raise RuntimeContractError('runtime_package_changed')
        return self._row(row['package_id'])

    def publish(self, package_id):
        from .redis_transport import _secret
        from .transport import token
        token(package_id)
        if (identity(checked_path(self.root).stat()) != self.root_identity
                or identity(checked_path(self.seed_file).stat()) != self.seed_identity
                or _secret(self.seed_file).encode() != self.seed):
            raise RuntimeContractError('runtime_publisher_source_changed')
        row, record = self._row(package_id)
        with exclusive_lock(self.root / 'epoch-acl.lock'):
            row, record = self._row(package_id)
            if row['phase'] not in {'files_ready', 'acl_pending', 'acl_unknown', 'ready'}:
                raise RuntimeContractError('runtime_package_files_required')
            rules = self.rules(row['instance_id'], row['epoch'])
            expected_digest = digest(rules)
            if 'acl' not in record:
                record['acl'] = {'rules_digest': expected_digest, 'issued': [], 'saved': False}
                row, record = self._checkpoint(row, record, 'acl_pending')
            if record['acl']['rules_digest'] != expected_digest:
                raise RuntimeContractError('runtime_acl_contract_changed')
            try:
                for user, commands in rules.items():
                    observed = self._observed(self.client.acl_getuser(user))
                    if observed is not None:
                        if observed != self._expected(commands):
                            raise RuntimeContractError('runtime_acl_foreign_or_changed')
                        continue
                    if user in record['acl']['issued']:
                        raise RuntimeContractError('runtime_acl_creation_unknown')
                    record['acl']['issued'].append(user)
                    row, record = self._checkpoint(row, record, 'acl_pending')
                    self.fault('acl.intent')
                    self.client.execute_command('ACL SETUSER', user, *commands)
                    self.fault('acl.external')
                    if self._observed(self.client.acl_getuser(user)) != self._expected(commands):
                        raise RuntimeContractError('runtime_acl_readback_failed')
                if not record['acl']['saved']:
                    if self.client.acl_save() is not True:
                        raise RuntimeContractError('runtime_acl_save_unconfirmed')
                    self.fault('acl.saved')
                    record['acl']['saved'] = True
                row, record = self._checkpoint(row, record, 'ready')
                return row
            except Exception as error:
                self._checkpoint(row, record, 'acl_unknown', getattr(error, 'code', 'runtime_acl_outcome_unknown'))
                raise


class _RemovalRuntime:
    """Launch-disabled runtime authority for exact exited-container cleanup."""
    def __init__(self, instance_id, epoch, record_id, engine, policy, boundary_check, generation):
        self.instance_id, self.epoch, self.record_id = instance_id, epoch, record_id
        self.engine, self.policy, self.boundary_check = engine, policy, boundary_check
        self.generation = generation

    def verify(self):
        from .config import identifier
        for value in (self.instance_id, self.epoch, self.record_id):
            identifier(value)
        self.boundary_check()
        return {'instance_id': self.instance_id, 'epoch': self.epoch,
                'runtime_record_id': self.record_id, 'removal_only': True}

    def controller(self, repository):
        from .config import ContainerError
        from .container_runtime import UnixEngine, CgroupObserver
        from .runtime_controller import RuntimeController
        self.verify()
        def deny_launch(_db):
            raise ContainerError('runtime_historical_launch_forbidden')
        def removal_check(row):
            self.verify()
            from .config import READONLY_ROLES
            grants = [{"role": g.role, "source": g.source, "target": g.target,
                       "readonly": g.role in READONLY_ROLES, "identity": g.identity}
                      for g in self.policy.mounts]
            if (row['instance_id'] != self.instance_id or row['epoch'] != self.epoch
                    or row['generation'] != self.generation
                    or row['engine_id'] != self.engine.engine_id
                    or row['image_id'] != self.policy.image.image_id
                    or json.loads(row['mount_grants_json']) != grants):
                raise ContainerError('historical_removal_authority_mismatch')
        return RuntimeController(repository, UnixEngine(self.engine),
                                 CgroupObserver(self.engine), self.policy,
                                 launch_check=deny_launch,
                                 historical_removal_check=removal_check)


class EpochProvisioner:
    """Reserve each execution generation once, then materialize its fixed package.

    The publisher and package files do not claim GPU resources. The existing
    InstancePolicy transaction remains the only load admission authority.
    """
    def __init__(self, installations, publisher, engine, root, *, models_root, inputs_root, api_key_file, fault=lambda _: None):
        self.installations, self.publisher, self.engine = installations, publisher, engine
        self.repository = installations.repository
        self.root, self.models_root, self.inputs_root = (checked_path(p) for p in (root, models_root, inputs_root))
        self.api_key_file = checked_path(api_key_file)
        if (not self.root.is_dir() or os.name == 'posix' and self.root.stat().st_mode & 0o077
                or publisher.endpoint.unix_socket is None or publisher.server_id is None):
            raise RuntimeContractError('runtime_package_root_not_private')
        self.root_identity = identity(self.root.stat())
        self.boundaries = RuntimeBoundaries(self.repository)
        # Historical package records and sealed roots are append/update driven,
        # while a resident instance is reconciled every 500 ms.  Cache the
        # fully verified global deny-set behind a cheap table revision stamp;
        # the current package and every mount are still verified on every tick.
        self._forbidden_lock = threading.Lock()
        self._forbidden_revision = None
        self._forbidden_paths = ()
        self._worker_secrets = {}
        self._boundary_lock = threading.Lock()
        self._writable_boundary_revision = None
        # Conservative upper bound for all current/future Worker writable
        # subdirectories; Server-owned control files are still never mounted.
        self.boundaries.register('outputs', self.root)
        self.fault = fault
        if self.installations.lora_authority is not None:
            self.installations.lora_authority.verify((self.models_root, self.inputs_root, self.api_key_file,
                self.repository.path, self.publisher.seed_file, self.publisher.endpoint.secret_file))
            self.installations.lora_authority.synchronize()

    def reserve(self, instance, *, allow_stopped=False):
        from .instance_policy import InstancePolicy
        from .transport import token
        token(instance)
        if identity(checked_path(self.root).stat()) != self.root_identity:
            raise RuntimeContractError('runtime_package_root_changed')
        with self.repository._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.repository.assert_not_removing_tx(db, instance)
            if InstancePolicy.legacy_unreconciled(db):
                raise RuntimeContractError('runtime_previous_generation_unreconciled')
            binding = self.installations._binding(db, instance, frozenset())
            if binding is None: raise RuntimeContractError('runtime_installation_binding_required')
            raw = db.execute('SELECT * FROM instance_policies WHERE instance_id=?', (instance,)).fetchone()
            deployment = db.execute(
                'SELECT enabled,install_state FROM model_deployments WHERE id=?',
                (instance,),
            ).fetchone()
            if raw is None or raw['policy_json'] == 'null':
                raise RuntimeContractError('runtime_instance_configuration_required')
            # Service lifecycle and model residency are independent controls.
            # A started on-demand/idle service must own a running container even
            # while its weights remain unloaded; only a stopped service is
            # forbidden from creating a new execution generation.
            if (deployment is None or deployment['install_state'] != 'ready'
                    or not deployment['enabled'] and allow_stopped is not True):
                raise RuntimeContractError('runtime_service_start_required')
            policy = InstancePolicy._policy(raw)['policy']
            if policy['backend'] != 'container': raise RuntimeContractError('runtime_backend_mismatch')
            active = db.execute("SELECT * FROM instance_claims WHERE instance_id=? AND state!='exited'", (instance,)).fetchone()
            if active:
                package = db.execute('SELECT * FROM runtime_epoch_packages WHERE instance_id=? AND epoch=?', (instance, active['epoch'])).fetchone()
                if package is None: raise RuntimeContractError('runtime_package_missing_for_claim')
                return dict(package)
            if (db.execute("SELECT 1 FROM runtime_intents WHERE instance_id=? AND state!='exited'", (instance,)).fetchone()
                    or db.execute("SELECT 1 FROM runtime_effects e JOIN runtime_intents i USING(intent_id) WHERE i.instance_id=? AND (e.state LIKE '%pending' OR e.state LIKE '%unknown')", (instance,)).fetchone()
                    or db.execute("SELECT 1 FROM task_attempts WHERE instance_id=? AND exit_confirmed=0", (instance,)).fetchone()
                    or db.execute("SELECT 1 FROM task_inbox x JOIN task_attempts a ON a.id=x.attempt_id WHERE a.instance_id=? AND x.result='pending'", (instance,)).fetchone()
                    or db.execute("SELECT 1 FROM artifact_publications p JOIN task_attempts a ON a.id=p.attempt_id WHERE a.instance_id=? AND p.phase NOT IN ('committed','canceled')", (instance,)).fetchone()):
                raise RuntimeContractError('runtime_previous_generation_unreconciled')
            previous = db.execute('SELECT * FROM runtime_epoch_packages WHERE instance_id=? ORDER BY generation DESC LIMIT 1', (instance,)).fetchone()
            if previous:
                old_claim = db.execute('SELECT state FROM instance_claims WHERE instance_id=? AND epoch=?', (instance, previous['epoch'])).fetchone()
                if old_claim is None:
                    if previous['desired_revision'] == raw['revision'] and previous['incarnation'] == binding['incarnation']:
                        return dict(previous)
                    # The absence of any claim/intents above is authoritative:
                    # these files/ACLs never authorized an execution domain.
                    # Retain them, but fence late publishers and launch calls.
                    old_record = json.loads(previous['record_json'])
                    if digest(old_record) != previous['record_digest']:
                        raise RuntimeContractError('runtime_package_record_corrupt')
                    old_record['retired_before_claim'] = True
                    db.execute("UPDATE runtime_epoch_packages SET phase='failed',error_code='runtime_package_retired',record_json=?,record_digest=?,updated_at=? WHERE package_id=?",
                        (canonical(old_record), digest(old_record), now(), previous['package_id']))
                elif old_claim[0] != 'exited': raise RuntimeContractError('runtime_previous_generation_unreconciled')
            generation = max(previous['generation'] if previous else 0,
                             db.execute('SELECT COALESCE(MAX(generation),0) FROM runtime_intents WHERE instance_id=?', (instance,)).fetchone()[0]) + 1
            package_id, epoch = 'pkg_' + secrets.token_hex(16), 'epoch_' + secrets.token_hex(16)
            template = self.installations._template(db, binding)
            record = {'schema': 1, 'identity': {'server_id': self.publisher.server_id, 'instance_id': instance, 'worker_epoch': epoch},
                'binding_digest': digest(binding), 'policy': policy, 'template': template.data,
                'root': str(self.root / package_id), 'parent_identity': self.root_identity, 'objects': {},
                'models_root': str(self.models_root), 'inputs_root': str(self.inputs_root),
                'source_identities': {str(p): identity(p.stat()) for p in
                    (self.models_root, self.inputs_root, checked_path(Path(self.publisher.endpoint.unix_socket).parent))}}
            release = self.installations.images.release(binding['release_digest'])
            worker = binding.get('worker_contract')
            record['lora_families'] = list(worker['lora_families']) if worker else []
            if record['lora_families']:
                authority = self.installations.lora_authority
                if authority is None: raise RuntimeContractError('lora_authority_required')
                authority.verify(self._global_forbidden(package_id))
                record['lora_root'] = str(authority.root)
                record['source_identities'][str(authority.root)] = authority.identity
            from .model_assets import normalize_relative_path
            record['asset_bindings'] = {'main': None, 'dependencies': {}}
            if binding['catalog_key'] == 'sdxl-single-file':
                record['asset_bindings']['optional'] = {}
            references = [('main', binding['asset_id'], binding['asset_revision'])]
            references.extend((dep['dependency_key'], dep['asset_id'], dep['revision']) for dep in binding['dependencies'])
            references.extend((role, item['asset_id'], item['revision'])
                              for role, item in binding.get('optional_assets', {}).items())
            for key, asset_id, revision in references:
                asset = db.execute('SELECT * FROM model_assets WHERE id=? AND revision=? AND state=\'ready\'', (asset_id, revision)).fetchone()
                if asset is None: raise RuntimeContractError('runtime_asset_binding_changed')
                relative = normalize_relative_path(asset['storage_relpath'])
                source = checked_path(self.models_root / relative)
                if self.models_root not in source.parents or not source.is_dir():
                    raise RuntimeContractError('runtime_asset_binding_changed')
                record['source_identities'][str(source)] = identity(source.stat())
                mapping = {'asset_id': asset_id, 'revision': revision, 'manifest_digest': asset['manifest_digest'],
                           'path': '/mc-models/' + relative}
                if key == 'main':
                    record['asset_bindings']['main'] = mapping
                elif key in binding.get('optional_assets', {}):
                    record['asset_bindings']['optional'][key] = mapping
                else:
                    record['asset_bindings']['dependencies'][key] = mapping
            stamp = now()
            db.execute("INSERT INTO runtime_epoch_packages VALUES(?,?,?,?,?,?,?,?,'intent',?,?,NULL,?,?)",
                (package_id, instance, binding['incarnation'], epoch, raw['revision'], generation,
                 template.digest, self.publisher.authority_digest, canonical(record), digest(record), stamp, stamp))
            self.fault('epoch.intent')
        return self.publisher._row(package_id)[0]

    def _current(self, db, row, record):
        from .instance_policy import InstancePolicy
        binding = self.installations._binding(db, row['instance_id'], frozenset())
        policy = db.execute('SELECT * FROM instance_policies WHERE instance_id=?', (row['instance_id'],)).fetchone()
        deployment = db.execute(
            'SELECT enabled,install_state FROM model_deployments WHERE id=?',
            (row['instance_id'],),
        ).fetchone()
        claim = db.execute(
            "SELECT revision FROM instance_claims WHERE instance_id=? AND epoch=? AND state!='exited'",
            (row['instance_id'], row['epoch']),
        ).fetchone()
        # Before a claim exists, the package must match the current policy
        # revision exactly. After claiming, model load/unload transitions may
        # advance that revision without replacing the still-current container;
        # the immutable claim revision remains the execution identity.
        execution_revision = claim['revision'] if claim is not None else policy['revision'] if policy else None
        if (not binding or digest(binding) != record['binding_digest'] or not policy
                or not deployment or deployment['install_state'] != 'ready'
                or execution_revision != row['desired_revision']
                or _execution_policy(InstancePolicy._policy(policy)['policy']) != _execution_policy(record['policy'])
                or record.get('retired_before_claim')):
            raise RuntimeContractError('runtime_package_fenced')
        return binding

    @staticmethod
    def _fsync_directory(path):
        if os.name == 'posix':
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try: os.fsync(fd)
            finally: os.close(fd)

    def _objects(self, row, record):
        if (record['parent_identity'] != self.root_identity or identity(checked_path(self.root).stat()) != self.root_identity
                or record['root'] != str(self.root / row['package_id'])):
            raise RuntimeContractError('runtime_package_root_changed')
        for source, expected in record['source_identities'].items():
            if identity(checked_path(source).stat()) != expected:
                raise RuntimeContractError('runtime_package_source_changed')
        for relative, value in record['objects'].items():
            path = checked_path(Path(record['root']) / relative)
            info = path.stat()
            if (identity(info) != value['identity'] or os.name == 'posix' and
                    (info.st_uid != value['uid'] or info.st_gid != value['gid'] or info.st_mode & 0o777 != value['mode'])):
                raise RuntimeContractError('runtime_package_object_changed')
            if value.get('ready') and value.get('sha256') is not None:
                with path.open('rb') as handle: data = handle.read(65537)
                if len(data) > 65536 or hashlib.sha256(data).hexdigest() != value['sha256']:
                    raise RuntimeContractError('runtime_package_file_changed')
        # Registration rows are append-only.  Revalidate the complete historical
        # writable-root set when that durable registry advances, while the exact
        # current package objects above remain identity-checked on every pass.
        # Scanning every historical output/journal path for every live package on
        # every 500 ms tick otherwise turns old epochs into permanent idle I/O.
        self._verify_writable_boundaries()

    def _verify_writable_boundaries(self):
        with self._boundary_lock:
            with self.repository._connect() as db:
                revision = tuple(db.execute(
                    "SELECT COUNT(*),COALESCE(MAX(created_at),''),"
                    "COALESCE(MAX(boundary_id),'') FROM runtime_boundaries"
                ).fetchone())
            if revision != self._writable_boundary_revision:
                self.boundaries.writable()
                self._writable_boundary_revision = revision

    def _object(self, row, record, relative, *, data=None, worker=False):
        """Create one fixed object. Unknown unregistered objects are never adopted.

        A recorded inode may be rewritten only while this package is still an
        unpublished intent. No ready package, or old Worker journal, is reset.
        """
        self._objects(row, record)
        path = Path(record['root']) / relative
        uid = record['template']['limits']['uid'] if worker else (os.getuid() if os.name == 'posix' else 0)
        gid = record['template']['limits']['gid'] if worker else (os.getgid() if os.name == 'posix' else 0)
        mode = 0o700 if data is None else 0o600
        if os.name == 'posix' and os.getuid() not in {0, uid}:
            raise RuntimeContractError('runtime_worker_file_owner_unavailable')
        value = record['objects'].get(relative)
        if value is None:
            # Persist the exact name before the filesystem side effect. A crash
            # before inode registration leaves an explicit, non-adoptable slot.
            if record.get('creating') not in {None, relative}:
                raise RuntimeContractError('runtime_package_creation_unknown')
            record['creating'] = relative
            row, record = self.publisher._checkpoint(row, record, 'intent')
            self.fault('epoch.object_intent')
            if data is None:
                path.mkdir(mode=mode)
            else:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), mode)
                os.close(fd)
            self.fault('epoch.object_created')
            if os.name == 'posix': os.chown(path, uid, gid, follow_symlinks=False)
            path.chmod(mode)
            self._fsync_directory(path.parent)
            value = {'identity': identity(path.stat()), 'uid': uid, 'gid': gid, 'mode': mode, 'ready': False, 'sha256': None}
            record['objects'][relative] = value
            record.pop('creating', None)
            row, record = self.publisher._checkpoint(row, record, 'intent')
            value = record['objects'][relative]
        if value['ready']:
            if data is not None and value['sha256'] != hashlib.sha256(data).hexdigest():
                raise RuntimeContractError('runtime_package_content_changed')
            return row, record
        if data is not None:
            fd = os.open(path, os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'wb') as output:
                if identity(os.fstat(output.fileno())) != value['identity']:
                    raise RuntimeContractError('runtime_package_object_changed')
                output.truncate(0); output.write(data); output.flush(); os.fsync(output.fileno())
            value['sha256'] = hashlib.sha256(data).hexdigest()
        value['ready'] = True
        self.fault('epoch.object_written')
        return self.publisher._checkpoint(row, record, 'intent')

    def materialize(self, package_id):
        from .capabilities import worker_capability_for
        from .transport import token
        token(package_id)
        row, record = self.publisher._row(package_id)
        self._objects(row, record)
        with exclusive_lock(self.root / (package_id + '.lock')):
            row, record = self.publisher._row(package_id)
            with self.repository._connect() as db: binding = self._current(db, row, record)
            if row['phase'] == 'failed': raise RuntimeContractError(row['error_code'] or 'runtime_package_failed')
            if row['phase'] == 'intent':
                for relative in ('.', 'control', 'worker', 'outputs', 'journal'):
                    row, record = self._object(row, record, relative, worker=relative in {'outputs', 'journal'})
                credentials = self.publisher.credentials(row['instance_id'], row['epoch'])
                policy_binding = record['policy']['binding']
                release = self.installations.images.release(binding['release_digest'])
                capability = worker_capability_for(policy_binding['model_key'])
                worker_binding = {key: policy_binding[key] for key in ('model_key', 'recipe_revision', 'model_asset_id', 'model_asset_revision')}
                worker_binding.update(image_digest=release.image_digest, gpu_uuids=record['policy']['gpus'], capability_digest=digest(capability))
                bootstrap = dict(record['identity'], schema=1, binding=worker_binding, recovery_complete=True, clock_trusted=True,
                    journal='/mc-journal/worker.db', outputs='/mc-outputs', adapter_id=release.data['adapter_id'],
                    redis={'username': credentials['worker']['username'], 'secret_file': '/mc-worker-secret',
                           'unix_socket': '/mc-redis/' + Path(self.publisher.endpoint.unix_socket).name},
                    asset_bindings=record['asset_bindings'])
                bootstrap['lora_authority'] = {'path':'/mc-lora', 'families':record['lora_families']}
                for relative, data, worker in (
                        ('control/server.secret', credentials['server']['password'].encode(), False),
                        ('worker/worker.secret', credentials['worker']['password'].encode(), True),
                        ('worker/bootstrap.json', canonical(bootstrap).encode(), True)):
                    row, record = self._object(row, record, relative, data=data, worker=worker)
                for role in ('outputs', 'journal'):
                    self.boundaries.register(role, Path(record['root']) / role, package_id=package_id)
                self._objects(row, record)
                forbidden = self._global_forbidden(package_id)
                for grant in self._mounts(record):
                    grant.verify((str(self.repository.path), str(self.api_key_file), self.engine.socket_path,
                                  *forbidden))
                with self.repository._connect() as db: self._current(db, row, record)
                row, record = self.publisher._checkpoint(row, record, 'files_ready')
            self._objects(row, record)
            self.publisher.publish(package_id)
            return self.load(package_id)

    def load(self, package_id):
        """Read an existing package, including an old epoch needed for recovery."""
        from .config import ContainerPolicy, MountGrant, PreparedRuntime, HISTORICAL_SDXL_DIGEST
        from .capabilities import worker_capability_for
        from .runtime_artifacts import image_approval
        previous_authority = None
        try:
            row, record = self.publisher._row(package_id)
        except RuntimeContractError as error:
            if error.code != 'runtime_publisher_authority_changed':
                raise
            row, record, previous_authority = self.publisher.adoption_candidate(package_id)
        if row['phase'] != 'ready': raise RuntimeContractError('runtime_package_not_ready')
        self._objects(row, record)
        root = Path(record['root'])
        release = self.installations.images.release(record['template']['release_digest'])
        bootstrap = json.loads((root / 'worker/bootstrap.json').read_bytes())
        recorded_capability = bootstrap['binding']['capability_digest']
        current_capability = digest(worker_capability_for(record['policy']['binding']['model_key']))
        historical = recorded_capability != current_capability
        if historical and (record['policy']['binding']['model_key'] != 'sdxl-base-1.0' or recorded_capability != HISTORICAL_SDXL_DIGEST):
            raise RuntimeContractError('runtime_historical_capability_unknown')
        def historical_claim():
            if not historical: return
            expected = dict(package_id=record['policy']['package_id'],epoch=row['epoch'],binding_digest=digest(record['policy']['binding']),
                image_digest=release.image_digest,capability_digest=recorded_capability,
                bootstrap_sha256=record['objects']['worker/bootstrap.json']['sha256'],runtime_record_id=package_id,generation=row['generation'])
            with self.repository._connect() as db:
                claim = db.execute('SELECT * FROM instance_claims WHERE instance_id=? AND epoch=?', (row['instance_id'],row['epoch'])).fetchone()
            if (not claim or claim['incarnation'] != row['incarnation'] or claim['execution_digest'] != digest(expected)
                    or json.loads(claim['execution_json']) != expected or json.loads(claim['policy_json']) != record['policy']
                    or claim['policy_digest'] != digest(record['policy'])):
                raise RuntimeContractError('runtime_historical_claim_required')
        historical_claim()
        mounts = self._mounts(record)
        policy = ContainerPolicy(image_approval(release), mounts, str(self.repository.path), str(self.api_key_file), self.engine.socket_path,
            gpu_uuids=tuple(record['policy']['gpus']), additional_forbidden=self._global_forbidden(package_id),
            **record['template']['limits'])
        if previous_authority is not None:
            row, record = self.publisher.adopt_equivalent(package_id, previous_authority)
        credentials = self.publisher.credentials(row['instance_id'], row['epoch'])
        def boundary():
            latest, content = self.publisher._row(package_id)
            if latest['phase'] != 'ready' or content != record:
                raise RuntimeContractError('runtime_package_changed')
            self._objects(latest, content)
            historical_claim()
            forbidden = self._global_forbidden(package_id)
            from .config import compile_forbidden_sources
            denied = compile_forbidden_sources((str(self.repository.path),
                str(self.api_key_file), self.engine.socket_path, *forbidden))
            if 'lora_root' in record:
                authority = self.installations.lora_authority
                if authority is None or str(authority.root) != record['lora_root']:
                    raise RuntimeContractError('lora_authority_changed')
                authority.verify(forbidden)
            for grant in mounts:
                grant.verify(denied)
        def launch(db):
            if historical or recorded_capability != digest(worker_capability_for(record['policy']['binding']['model_key'])):
                raise RuntimeContractError('runtime_historical_launch_forbidden')
            latest = db.execute('SELECT * FROM runtime_epoch_packages WHERE package_id=?', (package_id,)).fetchone()
            if not latest or latest['phase'] != 'ready' or latest['record_digest'] != row['record_digest']:
                raise RuntimeContractError('runtime_package_fenced')
            self._current(db, latest, record)
            boundary()
        package = PreparedRuntime(record['policy']['package_id'], row['instance_id'], row['epoch'], record['policy']['binding'],
            self.engine, policy, {'username': credentials['server']['username'], 'secret_file': str(root / 'control/server.secret'),
                                 'unix_socket': self.publisher.endpoint.unix_socket},
            record['objects']['worker/bootstrap.json']['sha256'], 'worker.db', self.publisher.server_id,
            record_id=package_id, generation=row['generation'], boundary_check=boundary, launch_check=launch,
            recovery_capability_digest=recorded_capability if historical else None)
        package.verify()
        return package

    def load_for_removal(self, package_id):
        """Validate a historical package only for stopped-container removal."""
        from .config import ContainerPolicy
        from .container_releases import RuntimeRelease
        from .runtime_artifacts import image_approval, strict_json
        row, record = self.publisher._raw_row(package_id, historical_server=True)
        if row['phase'] != 'ready':
            raise RuntimeContractError('runtime_package_not_ready')
        self._objects(row, record)
        release_digest = record['template']['release_digest']
        with self.repository._connect() as db:
            release_row = db.execute(
                'SELECT contract_json FROM runtime_release_records WHERE release_digest=?',
                (release_digest,)).fetchone()
        if release_row is None:
            raise RuntimeContractError('runtime_release_unavailable')
        # Current approval is a launch boundary.  Removal instead uses the
        # immutable release record that originally produced this exact intent;
        # it remains structurally validated but can never reach a launch call.
        release = RuntimeRelease(strict_json(release_row[0]))
        if release.digest != release_digest:
            raise RuntimeContractError('runtime_release_integrity_error')
        mounts = self._mounts(record)
        policy = ContainerPolicy(
            image_approval(release), mounts, str(self.repository.path),
            str(self.api_key_file), self.engine.socket_path,
            gpu_uuids=tuple(record['policy']['gpus']),
            additional_forbidden=self._global_forbidden(package_id),
            **record['template']['limits'])

        def boundary():
            latest, content = self.publisher._raw_row(package_id, historical_server=True)
            if (latest['phase'] != 'ready' or latest['authority_digest'] != row['authority_digest']
                    or latest['record_digest'] != row['record_digest'] or content != record):
                raise RuntimeContractError('runtime_package_changed')
            self._objects(latest, content)
            forbidden = self._global_forbidden(package_id)
            from .config import compile_forbidden_sources
            denied = compile_forbidden_sources((str(self.repository.path),
                str(self.api_key_file), self.engine.socket_path, *forbidden))
            if 'lora_root' in record:
                authority = self.installations.lora_authority
                if authority is None or str(authority.root) != record['lora_root']:
                    raise RuntimeContractError('lora_authority_changed')
                authority.verify(forbidden)
            for grant in mounts:
                grant.verify(denied)

        runtime = _RemovalRuntime(
            row['instance_id'], row['epoch'], package_id, self.engine, policy, boundary, row['generation'])
        runtime.verify()
        return runtime

    def _mounts(self, record):
        from .config import MountGrant
        root = Path(record['root'])
        values = (
            ('models', record['models_root'], '/mc-models'), ('inputs', record['inputs_root'], '/mc-inputs'),
            ('bootstrap', root / 'worker/bootstrap.json', '/mc-bootstrap.json'),
            ('redis_credentials', root / 'worker/worker.secret', '/mc-worker-secret'),
            ('redis_socket', Path(self.publisher.endpoint.unix_socket).parent, '/mc-redis'),
            ('outputs', root / 'outputs', '/mc-outputs'), ('journal', root / 'journal', '/mc-journal'))
        if 'lora_root' in record: values += (('lora_authority', record['lora_root'], '/mc-lora'),)
        return tuple(MountGrant.capture(role, source, target) for role, source, target in values)

    def _global_forbidden(self, package_id):
        """All historical/control roots remain excluded, even after retirement."""
        with self._forbidden_lock:
            with self.repository._connect() as db:
                boundary_revision = tuple(db.execute(
                    "SELECT COUNT(*),COALESCE(MAX(created_at),''),"
                    "COALESCE(MAX(boundary_id),'') FROM runtime_boundaries "
                    "WHERE role='sealed'"
                ).fetchone())
                # Phase and heartbeat updates are deliberately excluded from
                # this revision.  Startup reconciliation may touch every
                # historical package; keying the cache by ``updated_at`` made
                # each harmless state update trigger another full filesystem
                # identity scan (O(n^2) on a populated server).  The forbidden
                # path set changes only when package membership or the sealed
                # record changes, both represented by this ordered digest.
                package_rows = db.execute(
                    "SELECT package_id,record_digest FROM runtime_epoch_packages "
                    "ORDER BY package_id"
                ).fetchall()
                package_revision = digest([
                    [row['package_id'], row['record_digest']]
                    for row in package_rows
                ])
                revision = (boundary_revision, package_revision)
                if revision != self._forbidden_revision:
                    paths = [str(self.publisher.seed_file),
                             str(self.publisher.endpoint.secret_file)]
                    worker_secrets = {}
                    for row in db.execute(
                            "SELECT * FROM runtime_boundaries WHERE role='sealed'"):
                        path, _ = RuntimeBoundaries._checked(row)
                        paths.append(str(path))
                    for row in db.execute('SELECT * FROM runtime_epoch_packages'):
                        record = json.loads(row['record_json'])
                        if digest(record) != row['record_digest']:
                            raise RuntimeContractError('runtime_package_record_corrupt')
                        for relative in ('control', 'worker/worker.secret'):
                            value = record.get('objects', {}).get(relative)
                            if value is None:
                                continue
                            path = checked_path(Path(record['root']) / relative)
                            if identity(path.stat()) != value['identity']:
                                raise RuntimeContractError('runtime_package_object_changed')
                            value_path = str(path)
                            paths.append(value_path)
                            if relative == 'worker/worker.secret':
                                worker_secrets[row['package_id']] = value_path
                    self._forbidden_paths = tuple(dict.fromkeys(paths))
                    self._worker_secrets = worker_secrets
                    self._forbidden_revision = revision
            own_secret = self._worker_secrets.get(package_id)
            return tuple(path for path in self._forbidden_paths if path != own_secret)

    def ensure(self, instance, *, allow_stopped=False):
        return self.materialize(self.reserve(instance, allow_stopped=allow_stopped)['package_id'])

    def validate_claim(self, db, package_id):
        package = self.load(package_id)
        package.launch_check(db)
        return package.verify()

    def for_epoch(self, instance, epoch):
        with self.repository._connect() as db:
            row = db.execute('SELECT package_id FROM runtime_epoch_packages WHERE instance_id=? AND epoch=?', (instance, epoch)).fetchone()
        if row is None: raise RuntimeContractError('runtime_package_missing_for_claim')
        return self.load(row[0])

    def for_epoch_removal(self, instance, epoch):
        with self.repository._connect() as db:
            row = db.execute(
                'SELECT package_id FROM runtime_epoch_packages WHERE instance_id=? AND epoch=?',
                (instance, epoch)).fetchone()
        if row is None:
            raise RuntimeContractError('runtime_package_missing_for_claim')
        return self.load_for_removal(row[0])

    def default_policy(self, instance, gpu_uuids, *, include_runtime_options=True):
        from .instance_policy import InstancePolicy
        with self.repository._connect() as db:
            installed = self.installations._binding(db, instance, frozenset())
            if not installed: raise RuntimeContractError('runtime_installation_binding_required')
            deployment = db.execute('SELECT * FROM model_deployments WHERE id=?', (instance,)).fetchone()
            binding = InstancePolicy.deployment_binding(db, deployment)
            template = self.installations._template(db, installed)
        resources = dict(template.data['resources'])
        if include_runtime_options:
            resources.update(installed['runtime_options'])
        return dict(resources, backend='container', binding=binding,
                    package_id='template_' + template.digest, gpus=list(gpu_uuids))
