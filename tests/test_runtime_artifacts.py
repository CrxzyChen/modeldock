from __future__ import annotations

import copy
import hashlib
import http.client
import io
import json
import os
import tempfile
import tarfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from mediacenter.repository import Repository, InstallationOwnershipError
from mediacenter.runtime_artifacts import (
    RuntimeArtifactStore, UnixRuntimeImporter, import_source_path, verify_oci_archive,
)
from mediacenter.container_releases import RuntimeRelease, RuntimeContractError
from mediacenter.config import ContainerError


def image_fixture(directories=(), annotations=None):
    """Synthetic OCI metadata only; not an executable image or Engine proof."""
    blobs = {}
    def add(value, media):
        raw = value if isinstance(value, bytes) else json.dumps(value, separators=(',', ':')).encode()
        sha = hashlib.sha256(raw).hexdigest()
        blobs['blobs/sha256/' + sha] = raw
        return {'mediaType': media, 'digest': 'sha256:' + sha, 'size': len(raw)}
    layer = add(b'fixture-not-a-real-rootfs', 'application/vnd.oci.image.layer.v1.tar')
    config = add({'os': 'linux', 'architecture': 'amd64',
                  'config': {'Entrypoint': ['/fixture/worker'], 'Cmd': [], 'Env': []},
                  'rootfs': {'type': 'layers', 'diff_ids': [layer['digest']]}},
                 'application/vnd.oci.image.config.v1+json')
    target = add({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                  'config': config, 'layers': [layer]}, 'application/vnd.oci.image.manifest.v1+json')
    if annotations is not None:
        target['annotations'] = annotations
    blobs['oci-layout'] = b'{"imageLayoutVersion":"1.0.0"}'
    blobs['index.json'] = json.dumps({'schemaVersion': 2, 'manifests': [target]}).encode()
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w') as tar:
        for name, size in directories:
            member = tarfile.TarInfo(name); member.type = tarfile.DIRTYPE; member.mode = 0o755; member.size = size
            tar.addfile(member)
        for name, raw in blobs.items():
            member = tarfile.TarInfo(name); member.size = len(raw)
            tar.addfile(member, io.BytesIO(raw))
    archive = out.getvalue()
    declaration = {'schema': 1, 'release_id': 'test-runtime', 'adapter_id': 'test-adapter', 'sdk_digest': '1' * 64,
                   'image': {'reference': 'fixture/runtime@' + target['digest'], 'image_id': target['digest'],
                             'platform': 'linux/amd64', 'entrypoint': ['/fixture/worker'], 'command': [], 'environment': []},
                   'artifact': {'format': 'oci-layout-tar', 'url': 'https://fixtures.invalid/runtime.tar',
                                'sha256': hashlib.sha256(archive).hexdigest(), 'byte_size': len(archive)}}
    return declaration, archive


def installation(repository, name='install-one'):
    repository.insert_service_installation({'id': name, 'recipe_key': name, 'state': 'preflight',
        'current_step': 'preflight', 'progress': 0, 'options': {'deployment_id': name}, 'steps': [],
        'recipe_snapshot': {}, 'created_at': '2026-08-31', 'updated_at': '2026-08-31'})
    row = repository.get_service_installation(name)
    return repository.claim_installation_runner(name, row['current_attempt_id'])


class RuntimeArtifactTests(unittest.TestCase):
    def test_import_source_path_accepts_named_files_and_posix_descriptor_streams(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "runtime.oci.tar"
            path.write_bytes(b"oci")
            with path.open("rb") as stream:
                self.assertEqual(import_source_path(stream).resolve(), path.resolve())
            if os.name == "posix":
                with os.fdopen(os.open(path, os.O_RDONLY), "rb") as stream:
                    self.assertEqual(import_source_path(stream), path.resolve())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.repository = Repository(self.root / 'state.db')
        self.declaration, self.archive = image_fixture()
        self.release = RuntimeRelease(self.declaration)
        self.reads = []
        @contextmanager
        def source(release, offset, timeout):
            self.reads.append(offset)
            yield io.BytesIO(self.archive[offset:])
        self.store = RuntimeArtifactStore(self.repository, self.root / 'images', approved_digests=[self.release.digest], source=source)
        self.store.register(self.declaration)
        self.owner = installation(self.repository)

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_archive_is_verified_but_not_imported_or_model_ready(self):
        row = self.store.begin(self.owner, self.release.digest)
        result = self.store.download(self.owner, row['transfer_id'])
        self.assertEqual(result['phase'], 'verified')
        self.assertEqual(result['received_bytes'], len(self.archive))
        with self.repository._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM runtime_image_bindings').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM instance_installation_bindings').fetchone()[0], 0)
        self.assertEqual(self.store.begin(self.owner, self.release.digest)['transfer_id'], row['transfer_id'])
        self.store.download(self.owner, row['transfer_id'])
        self.assertEqual(self.reads, [0])
        self.assertEqual(self.repository.installation_resources(self.owner[1])[0]['kind'], 'runtime-transfer')

    def test_declared_local_archive_is_copied_verified_and_preferred_to_https(self):
        build_root = self.root / 'build-output'; build_root.mkdir()
        source = build_root / 'runtime.oci.tar'; source.write_bytes(self.archive)
        store = RuntimeArtifactStore(self.repository, self.root / 'local-images',
            approved_digests=[self.release.digest], local_artifact_roots=[build_root],
            local_artifacts={self.release.digest: str(source)},
            source=lambda *_: (_ for _ in ()).throw(AssertionError('HTTPS must not be used')))
        store.register(self.declaration)
        other = installation(self.repository, 'local-install')
        row = store.begin(other, self.release.digest)
        result = store.download(other, row['transfer_id'])
        target = store.root / result['local_path']
        self.assertEqual(result['phase'], 'verified')
        self.assertEqual(result['received_bytes'], len(self.archive))
        self.assertEqual(target.read_bytes(), self.archive)
        self.assertNotEqual(target.stat().st_ino, source.stat().st_ino)

    def test_local_adoption_rejects_undeclared_roots_and_retains_failed_copy(self):
        build_root = self.root / 'build-output'; build_root.mkdir()
        outside = self.root / 'outside.oci.tar'; outside.write_bytes(self.archive)
        store = RuntimeArtifactStore(self.repository, self.root / 'local-images',
            approved_digests=[self.release.digest], local_artifact_roots=[build_root])
        store.register(self.declaration)
        other = installation(self.repository, 'local-install')
        row = store.begin(other, self.release.digest)
        with self.assertRaisesRegex(RuntimeContractError, 'outside_roots'):
            store.adopt_local(other, row['transfer_id'], outside)
        self.assertEqual(store.get(row['transfer_id'])['phase'], 'queued')
        partial = build_root / 'partial.oci.tar'; partial.write_bytes(self.archive[:-1])
        with self.assertRaises(RuntimeContractError):
            store.adopt_local(other, row['transfer_id'], partial)
        failed = store.get(row['transfer_id'])
        self.assertEqual(failed['phase'], 'failed')
        self.assertTrue((store.root / failed['local_path']).exists())

    def test_independent_release_approval_and_mutations_fail_closed(self):
        changed = copy.deepcopy(self.declaration); changed['image']['command'] = ['different']
        with self.assertRaisesRegex(RuntimeContractError, 'not_approved'): self.store.register(changed)
        for path, value in [('schema', True), ('sdk_digest', 'bad'), ('unknown', 'x')]:
            changed = copy.deepcopy(self.declaration); changed[path] = value
            with self.assertRaises(RuntimeContractError): RuntimeRelease(changed)
        with patch('socket.getaddrinfo', side_effect=AssertionError('validation is offline')):
            RuntimeRelease(self.declaration)

    def test_wrong_digest_and_non_oci_bytes_are_not_accepted(self):
        with self.assertRaises(RuntimeContractError): verify_oci_archive(io.BytesIO(self.archive[:-1] + b'x'), self.release)
        bad = copy.deepcopy(self.declaration)
        bad['artifact'].update(byte_size=3, sha256=hashlib.sha256(b'bad').hexdigest())
        with self.assertRaises(RuntimeContractError): verify_oci_archive(io.BytesIO(b'bad'), RuntimeRelease(bad))

    def test_containerd_exporter_directories_and_strict_directory_rejections(self):
        declaration, archive = image_fixture((('blobs/', 0), ('blobs/sha256/', 0)))
        self.assertEqual(verify_oci_archive(io.BytesIO(archive), RuntimeRelease(declaration))['platform'], 'linux/amd64')
        for directories in [(('elsewhere/', 0),), (('blobs/', 1),), (('blobs/', 0), ('blobs/', 0)), (('./blobs/', 0),)]:
            with self.subTest(directories=directories):
                declaration, archive = image_fixture(directories)
                with self.assertRaises(RuntimeContractError): verify_oci_archive(io.BytesIO(archive), RuntimeRelease(declaration))

    def test_invalid_or_foreign_authority_creates_no_lock_files(self):
        before = sorted(str(p) for p in self.root.rglob('*'))
        for value in ('../outside-store', 'bad', 'rit_' + 'g' * 64):
            with self.assertRaisesRegex(RuntimeContractError, 'transfer_id_invalid'):
                self.store.download(self.owner, value)
        with self.assertRaises(InstallationOwnershipError):
            self.store.begin(('no-operation', 'no-attempt', 'no-token'), self.release.digest)
        other = installation(self.repository, 'other')
        with self.repository._connect() as db:
            db.execute("INSERT INTO runtime_image_transfers(transfer_id,operation_id,attempt_id,release_digest,image_digest,phase,local_path,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,'now','now')",
                       ('rit_' + 'a' * 64, self.owner[0], self.owner[1], self.release.digest, self.release.image_digest, 'rit_' + 'a' * 64 + '.oci.tar'))
        with self.assertRaises(InstallationOwnershipError): self.store.download(other, 'rit_' + 'a' * 64)
        self.assertEqual(sorted(str(p) for p in self.root.rglob('*')), before)

    def test_foreign_owner_cannot_mutate_or_truncate_download(self):
        row = self.store.begin(self.owner, self.release.digest)
        self.store.download(self.owner, row['transfer_id'])
        before = (self.store.root / row['local_path']).read_bytes()
        other = installation(self.repository, 'other')
        with self.assertRaises(InstallationOwnershipError): self.store.download(other, row['transfer_id'])
        self.assertEqual((self.store.root / row['local_path']).read_bytes(), before)

    def test_pause_and_resume_are_same_transfer_with_no_new_file(self):
        row = self.store.begin(self.owner, self.release.digest)
        self.store.control(self.owner, row['transfer_id'], 'pause')
        self.assertEqual(self.store.download(self.owner, row['transfer_id'])['phase'], 'paused')
        self.assertEqual(self.reads, [])
        self.store.control(self.owner, row['transfer_id'], 'resume')
        self.assertEqual(self.store.download(self.owner, row['transfer_id'])['phase'], 'verified')
        self.assertEqual(len(list(self.store.root.glob('*.oci.tar'))), 1)

    def test_import_is_once_owned_and_shared_readback_does_not_reimport(self):
        class Importer:
            engine_id = 'fixture-engine'
            loads = 0
            def preflight(self): pass
            def load(self, stream, release): self.loads += 1
            def inspect(self, release, verified):
                if not self.loads: raise RuntimeContractError('engine_object_missing')
                return {'engine_id': self.engine_id, 'image_id': release.image_digest,
                        'manifest_digest': release.image_digest, 'rootfs': verified['rootfs']}
        importer = Importer()
        first = self.store.begin(self.owner, self.release.digest)
        self.store.download(self.owner, first['transfer_id'])
        self.assertEqual(self.store.import_image(self.owner, first['transfer_id'], importer)['phase'], 'ready')
        self.store.import_image(self.owner, first['transfer_id'], importer)
        other = installation(self.repository, 'second-install')
        second = self.store.begin(other, self.release.digest)
        self.store.download(other, second['transfer_id'])
        self.store.import_image(other, second['transfer_id'], importer)
        self.assertEqual(importer.loads, 1)
        with self.repository._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM runtime_image_bindings').fetchone()[0], 1)
            refs = db.execute("SELECT created FROM installation_resources WHERE kind='runtime-image' ORDER BY created DESC").fetchall()
            self.assertEqual([r[0] for r in refs], [1, 0])

    def test_unknown_import_never_reissues_load_or_grants_binding(self):
        class Importer:
            engine_id = 'fixture-engine'
            loads = 0
            def preflight(self): pass
            def load(self, stream, release):
                self.loads += 1
                raise RuntimeContractError('runtime_import_stream_failed')
            def inspect(self, release, verified):
                if not self.loads: raise RuntimeContractError('engine_object_missing')
                raise AssertionError('failed load is not installed')
        importer = Importer()
        row = self.store.begin(self.owner, self.release.digest)
        self.store.download(self.owner, row['transfer_id'])
        for _ in range(3):
            with self.assertRaises(RuntimeContractError): self.store.import_image(self.owner, row['transfer_id'], importer)
        self.assertEqual(importer.loads, 1)
        self.assertEqual(self.store.get(row['transfer_id'])['phase'], 'import_unknown')
        other = installation(self.repository, 'retry-via-other-install')
        second = self.store.begin(other, self.release.digest)
        self.store.download(other, second['transfer_id'])
        with self.assertRaisesRegex(RuntimeContractError, 'runtime_image_import_unresolved'):
            self.store.import_image(other, second['transfer_id'], importer)
        self.assertEqual(importer.loads, 1)
        with self.repository._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM runtime_image_bindings').fetchone()[0], 0)

    def test_exact_preexisting_image_is_adopted_without_reloading_archive(self):
        class Importer:
            engine_id = 'fixture-engine'
            loads = 0
            def preflight(self): pass
            def load(self, stream, release): self.loads += 1
            def inspect(self, release, verified):
                return {'engine_id': self.engine_id, 'image_id': release.image_digest,
                        'manifest_digest': release.image_digest, 'rootfs': verified['rootfs']}
            inspect_existing = inspect
        importer = Importer()
        row = self.store.begin(self.owner, self.release.digest)
        self.store.download(self.owner, row['transfer_id'])
        result = self.store.import_image(self.owner, row['transfer_id'], importer)
        self.assertEqual(result['phase'], 'ready')
        self.assertEqual(importer.loads, 0)
        with self.repository._connect() as db:
            binding = db.execute('SELECT * FROM runtime_image_bindings').fetchone()
            resource = db.execute("SELECT created FROM installation_resources WHERE kind='runtime-image'").fetchone()
        self.assertEqual(binding['image_id'], self.release.image_digest)
        self.assertEqual(resource[0], 0)

    def test_missing_engine_image_is_translated_into_the_import_contract(self):
        class Engine:
            config = SimpleNamespace(
                engine_id="fixture-engine", image_store="overlay2",
                platform="linux/amd64",
            )

            def verify_engine(self):
                return None

            def _request(self, method, path):
                return {"Driver": "overlay2"}

            def inspect_image(self, approval):
                raise ContainerError("engine_object_missing")

        importer = UnixRuntimeImporter(Engine())
        with self.assertRaisesRegex(RuntimeContractError, "engine_object_missing"):
            importer.inspect_existing(self.release, {})

    def test_oci_image_name_annotations_cannot_retag_external_images(self):
        for key in ('io.containerd.image.name', 'org.opencontainers.image.ref.name', 'arbitrary-alias'):
            declaration, archive = image_fixture(annotations={key: 'foreign:latest'})
            with self.assertRaisesRegex(RuntimeContractError, 'annotations_rejected'):
                verify_oci_archive(io.BytesIO(archive), RuntimeRelease(declaration))

    def test_actual_http_response_framing_and_success_identity_are_required(self):
        class Socket:
            def __init__(self, wire): self.wire = wire
            def makefile(self, *args): return io.BytesIO(self.wire)
            def settimeout(self, value): pass
            def connect(self, path): pass
            def close(self): pass
        class Connection:
            def __init__(self, response): self.response = response
            def putrequest(self, *args): pass
            def putheader(self, *args): pass
            def endheaders(self): pass
            def send(self, value): pass
            def getresponse(self): return self.response
            def close(self): self.response.close()
        config = SimpleNamespace(total_timeout=5, io_timeout=1, response_limit=65536,
                                 socket_path='fixture-only', api_version='1.55', engine_id='fixture-engine')
        engine = SimpleNamespace(config=config, socket_identity={'fixture': 1})
        loaded = json.dumps({'stream': 'Loaded image ID: ' + self.release.image_digest + '\n'}).encode() + b'\n'
        for body, extra, success in [(loaded, 0, True), (loaded, 120, False), (b'{}\n', 0, False),
                                     (b'{"error":"unpack failed"}\n', 0, False),
                                     (b'{"stream":"Error unpacking image: full"}\n', 0, False)]:
            wire = b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body) + extra).encode() + b'\r\n\r\n' + body
            response = http.client.HTTPResponse(Socket(wire)); response.begin()
            with patch('mediacenter.runtime_artifacts.socket.AF_UNIX', 1, create=True), \
                 patch('mediacenter.runtime_artifacts.socket.socket', return_value=Socket(wire)), \
                 patch('mediacenter.runtime_artifacts.http.client.HTTPConnection', return_value=Connection(response)), \
                 patch('mediacenter.runtime_artifacts.object_identity', return_value=engine.socket_identity):
                if success:
                    self.assertEqual(UnixRuntimeImporter(engine).load(io.BytesIO(self.archive), self.release)['response_bytes'], len(body))
                else:
                    with self.assertRaises(RuntimeContractError): UnixRuntimeImporter(engine).load(io.BytesIO(self.archive), self.release)

    def test_import_readback_requires_exact_available_content_not_unpacked_claim(self):
        verified=verify_oci_archive(io.BytesIO(self.archive), self.release)
        descriptor={'digest':self.release.image_digest,'size':123,'mediaType':'application/vnd.oci.image.manifest.v1+json'}
        image={'Id':self.release.image_digest,'Descriptor':descriptor,'Config':verified['config'],
               'RootFS':{'Layers':verified['rootfs']['diff_ids']}}
        summary={'ID':self.release.image_digest,'Descriptor':descriptor,'Kind':'image','Available':True,
                 'ImageData':{'Platform':{'os':'linux','architecture':'amd64'},'Size':{'Unpacked':0}}}
        calls=[]; current=[summary]
        def request(method,path):
            calls.append((method,path))
            if path=='/info': return {'DriverStatus':[['driver-type','io.containerd.snapshotter.v1']]}
            self.assertTrue(path.endswith('/json?manifests=true'))
            self.assertNotIn('platform=',path)
            return {'Manifests':current}
        engine=SimpleNamespace(config=SimpleNamespace(engine_id='fixture-engine'),verify_engine=lambda:None,
                               inspect_image=lambda approval:copy.deepcopy(image),_request=request)
        importer=UnixRuntimeImporter(engine)
        result=importer.inspect(self.release,verified)
        self.assertTrue(result['content_present'])
        self.assertNotIn('env_checked',result)
        self.assertNotIn('unpacked',result)
        for changed in [dict(summary,Available=False),dict(summary,Available=1),dict(summary,ID='sha256:'+'0'*64),
                        dict(summary,Kind='index'),dict(summary,ImageData={'Platform':{'os':'linux','architecture':'arm64'}})]:
            current[:]=[changed]
            with self.assertRaisesRegex(RuntimeContractError,'runtime_image_content_unavailable'):
                importer.inspect(self.release,verified)
        current[:]=[]
        with self.assertRaises(RuntimeContractError): importer.inspect(self.release,verified)


if __name__ == '__main__': unittest.main()
