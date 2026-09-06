from __future__ import annotations

import io
import copy
import json
import os
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from mediacenter.runtime_artifacts import RuntimeArtifactStore
from mediacenter.container_releases import RuntimeRelease, RuntimeContractError
from mediacenter.runtime_provisioning import InstallationRuntime, EpochPublisher, EpochProvisioner
from mediacenter.task_state import now
from mediacenter.worker_common import digest, canonical
from tests.test_runtime_artifacts import image_fixture
from tests import test_service_installer


class LoraAuthorityTests(unittest.TestCase):
    def setUp(self):
        from tests.test_ph8_contracts import PH8ContractTests
        from mediacenter.asset_compatibility import AssetCompatibilityManager
        from mediacenter.runtime_provisioning import LoraAuthority
        self.fixture = PH8ContractTests(); self.fixture.setUp()
        self.manager, self.repo = self.fixture.assets, self.fixture.repository
        self.base = self.fixture.metadata(self.fixture.publish('wai', 'checkpoint', 1), family='sdxl')
        self.asset = self.fixture.metadata(self.fixture.publish('shadow', 'lora', 2),
            family='sdxl', declared_base_identity=self.base['files'][0]['sha256'])
        self.asset_id = self.asset['id']
        AssetCompatibilityManager(self.repo).assess(self.asset_id, self.base['id'])
        self.root = self.fixture.root / 'lora-authority'; self.root.mkdir(mode=0o755)
        self.authority = LoraAuthority(self.repo,self.root)
        self.reference = dict(asset_id=self.asset_id,revision=self.asset['revision'],family='sdxl',weight=0.7)
        self.binding = dict(model_asset_id=self.base['id'], model_asset_revision=self.base['revision'], recipe_revision='a'*64)

    def bind(self, reference=None, profile='a'*64):
        return self.authority.bind(reference or self.reference, base_asset_id=self.base['id'],
            base_revision=self.base['revision'], runtime_profile_digest=profile, assets=self.manager)

    def tearDown(self): self.fixture.tearDown()

    def test_public_upload_permission_publish_repeat_and_revoke(self):
        from mediacenter.adapters.sdxl import lora_descriptor
        from mediacenter.runtime_provisioning import LoraAuthority
        from mediacenter.task_state import TaskState, TaskStateError
        bound = self.bind()
        path = self.root / (bound['permit_id']+'.json'); inode=path.stat().st_ino; body=path.read_bytes()
        self.assertEqual(self.bind(),bound)
        self.assertEqual((path.stat().st_ino,path.read_bytes()),(inode,body))
        observed=lora_descriptor(self.root,self.reference,model_binding=self.binding,models_root=self.manager.storage_root)
        self.assertEqual(observed['family'],'sdxl')
        self.assertEqual(self.repo.get_model_asset(self.asset_id)['source_type'],'upload')
        with self.repo._connect() as db:
            TaskState._check_loras(db,[bound],binding=self.binding)
            db.execute("UPDATE model_assets SET state='archived' WHERE id=?", (self.base['id'],))
        self.authority.synchronize()
        with self.assertRaisesRegex(ValueError,'lora_not_approved'): lora_descriptor(self.root,self.reference,model_binding=self.binding,models_root=self.manager.storage_root)
        with self.repo._connect() as db:
            with self.assertRaisesRegex(TaskStateError,'lora_not_approved'): TaskState._check_loras(db,[bound])
        self.assertEqual(path.read_bytes(),body)
        with self.repo._connect() as db:
            db.execute("UPDATE model_assets SET state='ready' WHERE id=?", (self.base['id'],))
        self.authority.synchronize()
        lora_descriptor(self.root,self.reference,model_binding=self.binding,models_root=self.manager.storage_root)
        with self.repo._connect() as db:
            TaskState._check_loras(db,[bound],binding=self.binding)
            with self.assertRaisesRegex(TaskStateError,'lora_asset_changed'):
                TaskState._check_loras(db,[bound],binding=dict(self.binding,recipe_revision='b'*64))

    def test_unapproved_family_classification_drift_and_root_overlap_fail(self):
        from mediacenter.artifacts import RuntimeBoundaries
        with self.assertRaisesRegex(RuntimeContractError,'lora_binding_invalid'):
            self.bind(dict(self.reference,family='wan'))
        with self.repo._connect() as db: db.execute("UPDATE model_assets SET role='checkpoint' WHERE id=?",(self.asset_id,))
        with self.assertRaisesRegex(ValueError,'compatibility_evidence_changed'):
            self.bind()
        RuntimeBoundaries(self.repo).register('outputs',self.root)
        with self.assertRaisesRegex(RuntimeContractError,'lora_authority_overlap'): self.authority.synchronize()

    def test_allowlist_stable_owned_stage_recovers_or_fences_without_file_growth(self):
        self.authority.synchronize();active=self.root/'active.json';pending=self.root/'.active.pending'
        os.replace(active,pending);inode=pending.stat().st_ino
        self.authority.synchronize()
        self.assertEqual(active.stat().st_ino,inode);self.assertFalse(pending.exists())
        os.replace(active,pending);pending.write_bytes(b'partial')
        names=sorted(path.name for path in self.root.iterdir())
        for _ in range(3):
            with self.assertRaisesRegex(RuntimeContractError,'lora_publication_unknown'):self.authority.synchronize()
            self.assertEqual(sorted(path.name for path in self.root.iterdir()),names)

    def test_profile_scoping_and_readiness_recheck(self):
        from mediacenter.task_state import TaskState, TaskStateError
        first, second = self.bind(), self.bind(profile='b'*64)
        self.assertNotEqual(first['permit_id'], second['permit_id'])
        with self.repo._connect() as db:
            TaskState._check_loras(db, [first], binding=self.binding)
            with self.assertRaisesRegex(TaskStateError, 'lora_asset_changed'):
                TaskState._check_loras(db, [second], binding=self.binding)
            db.execute("UPDATE asset_compatibility SET evidence_json='{}'")
            with self.assertRaisesRegex(TaskStateError, 'compatibility_evidence_changed'):
                TaskState._check_loras(db, [first], binding=self.binding)
        self.authority.synchronize()
        self.assertEqual(json.loads((self.root/'active.json').read_text())['permits'], {})

    def test_stable_authority_does_not_rewrite_disk(self):
        self.bind()
        active = self.root/'active.json'
        before = active.stat().st_mtime_ns
        with self.repo._connect() as db:
            version = db.execute('PRAGMA data_version').fetchone()[0]
            self.authority.synchronize()
            self.assertEqual(db.execute('PRAGMA data_version').fetchone()[0], version)
        self.assertEqual(active.stat().st_mtime_ns, before)


class ValidationProducerTests(unittest.TestCase):
    """Real installation/claim/inbox transactions with synthetic OCI/ACL I/O."""
    def setUp(self):
        original = image_fixture
        def sdxl_image():
            declaration, archive = original(); declaration['adapter_id']='sdxl'
            return declaration, archive
        with patch(__name__+'.image_fixture',side_effect=sdxl_image):
            self.fixture=EpochPackageTests(); self.fixture.setUp()
        self.fixture.capability.stop()
        from mediacenter.capabilities import worker_capability_for
        self.capability=worker_capability_for('sdxl-base-1.0');self.capability['model_key']='test-image'
        self.fixture.capability=patch('mediacenter.capabilities.worker_capability_for',return_value=self.capability)
        self.fixture.capability.start()
        self.protocol_capability=patch('mediacenter.protocol.worker_capability_for',return_value=self.capability)
        self.protocol_capability.start()
        self.fixture.authority.tasks.capabilities={'test-image':self.capability}
        self.runtime=self.fixture.runtime; self.authority=self.fixture.authority; self.repo=self.fixture.repo
        from mediacenter.runtime_provisioning import LoraAuthority
        root=self.fixture.root/'lora';root.mkdir(mode=0o755)
        self.runtime.lora_authority=LoraAuthority(self.repo,root)
        self.runtime.lora_authority.synchronize()

    def tearDown(self):
        self.protocol_capability.stop(); self.fixture.tearDown()

    def register(self,key='check-one'):
        value=self.runtime.validation_intent('installed-image','env_checked',key)
        row=self.authority.get('installed-image')
        self.authority.desire('installed-image','loaded',expected_version=row['version'],validation=value)
        return value

    def claim(self):
        from tests.test_resident_policy import fixture_capacity, worker_registered_event
        package=self.fixture.provisioner.ensure('installed-image')
        self.package=package
        identity=package.verify()
        self.authority.package_validator=self.fixture.provisioner.validate_claim
        row=self.authority.get('installed-image')
        claim=self.authority.claim_container('installed-image',package.epoch,expected_version=row['version'],backend='container',
            limits={gpu:100000 for gpu in self.fixture.policy['gpus']},package_identity=identity)
        policy=self.authority.get('installed-image')['policy']
        self.authority.receive(worker_registered_event(
            self.authority, 'installed-image', package.epoch, policy['binding'],
            identity, policy['gpus']))
        self.authority.load_model('installed-image',observe_capacity=fixture_capacity)
        with self.repo._connect() as db:
            operation=db.execute('SELECT operation_id FROM model_operations WHERE claim_id=?',(claim['claim_id'],)).fetchone()[0]
        self.command=self.authority.command(operation)
        return claim

    def terminal(self):
        from tests.test_resident_policy import model_event
        command=self.command
        self.authority.receive(model_event(command,1,'model.accepted'))
        self.authority.receive(model_event(command,2,'model.terminal'))

    def test_registered_new_operation_only_applied_terminal_and_idempotent_producer(self):
        value=self.register(); claim=self.claim()
        self.runtime.reconcile_validations(None)
        self.assertEqual(self.runtime.validation(value['validation_id'])['state'],'pending')
        self.terminal();self.runtime.reconcile_validations(None)
        result=self.runtime.validation(value['validation_id']);self.assertEqual(result['state'],'passed')
        self.runtime.reconcile_validations(None);self.assertEqual(self.runtime.validation(value['validation_id']),result)
        self.assertTrue(self.runtime.levels('installed-image')['env_checked'])
        with self.assertRaisesRegex(RuntimeContractError,'validation_requires_new_load'):self.register('new-check')
        with self.repo._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM model_operations').fetchone()[0],1)
            row=db.execute('SELECT * FROM runtime_validation_records').fetchone()
            self.assertEqual(row['claim_id'],claim['claim_id']);self.assertEqual(row['command_digest'],digest(self.command))

    def test_pending_load_validation_fences_on_demand_idle_unload(self):
        self.register()
        with self.repo._connect() as db:
            db.execute("UPDATE instance_policies SET status='loaded',last_activity='2000-01-01T00:00:00+00:00'")
        self.assertFalse(self.authority.idle_unload('installed-image'))
        self.assertEqual(self.authority.get('installed-image')['desired_state'], 'loaded')

    def test_stale_success_and_canceled_check_never_upgrade(self):
        value=self.register();self.claim()
        self.authority.desire('installed-image','unloaded')
        self.terminal();self.runtime.reconcile_validations(None)
        self.assertEqual(self.runtime.validation(value['validation_id'])['state'],'failed')
        self.assertFalse(self.runtime.levels('installed-image')['env_checked'])

    def generation(self, data, key='image-check'):
        from mediacenter.artifacts import ArtifactStore
        from tests.test_resident_policy import fixture_capacity
        import hashlib
        if not hasattr(self, 'package'):
            self.claim();self.terminal()
        intent=self.runtime.validation_intent('installed-image','generated_tested',key)
        tasks=self.authority.tasks
        request=dict(service='image',model='installed-image',prompt='fixture',options={'width':512,'height':512},inputs=[],validation_id=intent['validation_id'])
        task,_=tasks.accept(request,scope='validation',key=key,binding=self.fixture.policy['binding'],validation=intent)
        command=tasks.dispatch(task['id'],task['version'],'installed-image',self.package.epoch,
            {gpu:20 for gpu in self.fixture.policy['gpus']},observe_capacity=fixture_capacity)
        base={k:command[k] for k in ('protocol','server_id','instance_id','worker_epoch','task_id','attempt_id','created_at','correlation_id')}
        accepted=dict(base,type='task.accepted',message_id=command['message_id']+'-accepted',event_seq=1,
                      payload={k:command['payload'][k] for k in ('reservation_id','reservation_generation')})
        tasks.receive(accepted)
        sha=hashlib.sha256(data).hexdigest();manifest=dict(asset_id='artifact-'+task['id'],revision='v1',sha256=sha)
        event=dict(base,type='task.terminal',message_id=command['message_id']+'-success',event_seq=2,payload=dict(status='succeeded',error_code=None,manifest=manifest))
        event['extensions']={'execution_quiescence':dict(kind='quiescent',command_message_id=command['message_id'],
            command_digest=digest(command),execution_token='fixture-execution',child_token='fixture-child',
            **{k:command[k] for k in ('server_id','instance_id','worker_epoch','task_id','attempt_id')})}
        self.assertEqual(tasks.receive(event),'pending')
        self.runtime.reconcile_validations(None)
        self.assertEqual(self.runtime.validation(intent['validation_id'])['state'],'pending')
        outputs=self.package.outputs_path();folder=outputs/'tasks'/task['id']/command['attempt_id'];folder.mkdir(parents=True)
        (folder/'artifact.png').write_bytes(data)
        descriptor=dict(manifest,schema=1,**{k:command[k] for k in ('task_id','attempt_id','instance_id','worker_epoch')},
            command_message_id=command['message_id'],command_digest=digest(command),byte_size=len(data),media_type='image/png')
        (folder/'manifest.json').write_text(json.dumps(descriptor))
        store=ArtifactStore(tasks,self.fixture.root/'sealed-results')
        self.assertEqual(store.worker(event,outputs),'committed')
        return intent,store

    def png(self):
        try:
            from PIL import Image, __version__
        except ImportError:
            self.skipTest('Pillow unavailable; real PNG positive decode not executed')
        output=io.BytesIO();Image.new('RGB',(512,512),'red').save(output,format='PNG')
        print('MC036 actual CPU PNG decoder Pillow='+__version__+'; production pin 11.3.0 not inferred')
        return output.getvalue()

    def test_explicit_generation_same_authorized_fd_decode_and_final_cancel_cas(self):
        intent,store=self.generation(self.png())
        self.runtime.fault=lambda point:self.runtime.cancel_validation(intent['validation_id'],1) if point=='validation.before_commit' else None
        self.runtime.reconcile_validations(store)
        self.assertEqual(self.runtime.validation(intent['validation_id'])['state'],'failed')
        self.assertFalse(self.runtime.levels('installed-image')['generated_tested'])

    def test_canceled_generation_before_dispatch_fails_validation(self):
        if not hasattr(self, 'package'):
            self.claim();self.terminal()
        intent=self.runtime.validation_intent('installed-image','generated_tested','canceled-before-dispatch')
        tasks=self.authority.tasks
        request=dict(service='image',model='installed-image',prompt='fixture',
                     options={'width':512,'height':512},inputs=[],validation_id=intent['validation_id'])
        task,_=tasks.accept(request,scope='validation',key='canceled-before-dispatch',
                            binding=self.fixture.policy['binding'],validation=intent)
        tasks.cancel(task['id'],task['version'])
        self.runtime.reconcile_validations(object())
        result=self.runtime.validation(intent['validation_id'])
        self.assertEqual((result['state'],result['error_code']),
                         ('failed','validation_generation_failed'))

    def test_explicit_generation_passes_only_after_real_decode(self):
        intent,store=self.generation(self.png())
        self.runtime.reconcile_validations(store)
        self.assertEqual(self.runtime.validation(intent['validation_id'])['state'],'passed')
        self.assertTrue(self.runtime.levels('installed-image')['generated_tested'])

    def test_missing_iend_is_not_a_passed_generation(self):
        intent,store=self.generation(self.png()[:-12])
        self.runtime.reconcile_validations(store)
        self.assertEqual(self.runtime.validation(intent['validation_id'])['state'],'failed')

    def test_wav_validation_decoder_reads_every_frame(self):
        import tempfile, wave
        from mediacenter.runtime_provisioning import _decode_validation_media
        with tempfile.TemporaryFile() as stream:
            with wave.open(stream,'wb') as writer:
                writer.setnchannels(1);writer.setsampwidth(2);writer.setframerate(24000)
                writer.writeframes(b'\0\0' * 2400)
            stream.seek(0)
            decoded = _decode_validation_media(stream, 'fixture.wav', {})
        self.assertEqual((decoded['format'],decoded['sample_rate'],decoded['channels']),('WAV',24000,1))

    def test_png_validation_decoder_uses_authorized_descriptor(self):
        import binascii, struct, tempfile, zlib
        from mediacenter.runtime_provisioning import _decode_validation_media
        def chunk(kind, payload):
            return struct.pack('>I',len(payload))+kind+payload+struct.pack('>I',binascii.crc32(kind+payload)&0xffffffff)
        png=(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',2,2,8,2,0,0,0))
             +chunk(b'IDAT',zlib.compress(b'\0\xff\0\0\0\xff\0\0\0\0\xff\xff\xff\xff'))+chunk(b'IEND',b''))
        with tempfile.TemporaryFile() as stream:
            stream.write(png);stream.seek(0)
            decoded=_decode_validation_media(stream,'fixture.png',{'width':2,'height':2})
        self.assertEqual((decoded['format'],decoded['width'],decoded['height']),('PNG',2,2))
        with tempfile.TemporaryFile() as stream:
            stream.write(png);stream.seek(0)
            decoded=_decode_validation_media(stream,'fixture.png',{})
        self.assertEqual((decoded['format'],decoded['width'],decoded['height']),('PNG',2,2))

    def test_bad_crc_closes_authorized_fd_and_does_not_abort_batch(self):
        data=bytearray(self.png()); offset=data.index(b'IDAT'); length=int.from_bytes(data[offset-4:offset],'big')
        data[offset+4+length] ^= 1
        intent,store=self.generation(bytes(data))
        streams=[]; original=store.authorize
        @contextmanager
        def tracked(path):
            with original(path) as authorized:
                streams.append(authorized.stream); yield authorized
        with patch.object(store,'authorize',side_effect=tracked):
            self.runtime.reconcile_validations(store)
        self.assertEqual(self.runtime.validation(intent['validation_id'])['state'],'failed')
        self.assertTrue(streams and all(stream.closed for stream in streams))
        # A following record on the same instance is still processed normally.
        following,store=self.generation(self.png(),key='following-good')
        self.runtime.reconcile_validations(store)
        self.assertEqual(self.runtime.validation(following['validation_id'])['state'],'passed')

    def test_truncated_pixels_with_valid_crc_fail_full_decode(self):
        import zlib
        source=self.png(); chunks=[]; offset=8
        while offset<len(source):
            size=int.from_bytes(source[offset:offset+4],'big'); kind=source[offset+4:offset+8]
            data=source[offset+8:offset+8+size]
            if kind==b'IDAT': data=data[:max(1,len(data)//3)]
            chunks.append(len(data).to_bytes(4,'big')+kind+data+(zlib.crc32(kind+data)&0xffffffff).to_bytes(4,'big'))
            offset+=12+size
        intent,store=self.generation(source[:8]+b''.join(chunks))
        self.runtime.reconcile_validations(store)
        self.assertEqual(self.runtime.validation(intent['validation_id'])['state'],'failed')

    def test_decompression_bomb_is_failed_once_and_following_check_completes(self):
        import zlib
        data=bytearray(self.png());data[16:24]=(50000).to_bytes(4,'big')*2
        data[29:33]=(zlib.crc32(data[12:29])&0xffffffff).to_bytes(4,'big')
        intent,store=self.generation(bytes(data))
        self.runtime.reconcile_validations(store)
        first=self.runtime.validation(intent['validation_id'])
        self.assertEqual((first['state'],first['error_code']),('failed','validation_media_limit'))
        self.runtime.reconcile_validations(store);self.assertEqual(self.runtime.validation(intent['validation_id']),first)
        following,store=self.generation(self.png(),key='after-bomb')
        self.runtime.reconcile_validations(store)
        self.assertEqual(self.runtime.validation(following['validation_id'])['state'],'passed')


class InstallationBindingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_service_installer.ServiceInstallerTests(); self.fixture.setUp()
        self.repo, self.root = self.fixture.repository, self.fixture.root
        self.entry = self.fixture.installer.recipes['test-image']
        self.asset = self.fixture.ready_asset()
        declaration, archive = image_fixture()
        release = RuntimeRelease(declaration)
        @contextmanager
        def source(release, offset, timeout): yield io.BytesIO(archive[offset:])
        self.images = RuntimeArtifactStore(self.repo, self.root / 'images', approved_digests=[release.digest], source=source)
        self.images.register(declaration)
        self.repo.insert_service_installation({'id': 'installation', 'recipe_key': 'test-image', 'state': 'preflight',
            'current_step': 'preflight', 'progress': 0, 'steps': [], 'options': {'deployment_id': 'installed-image', 'license_accepted': True},
            'recipe_snapshot': self.entry, 'created_at': 'fixture', 'updated_at': 'fixture'})
        row = self.repo.get_service_installation('installation')
        self.owner = self.repo.claim_installation_runner(row['id'], row['current_attempt_id'])
        self.transfer = self.images.begin(self.owner, release.digest)['transfer_id']
        self.images.download(self.owner, self.transfer)
        class Importer:
            engine_id = 'fixture-engine'
            def preflight(self): pass
            def load(self, stream, release): pass
            def inspect(self, release, verified): return {'engine_id': self.engine_id, 'image_id': release.image_digest}
        self.images.import_image(self.owner, self.transfer, Importer())
        self.fixture.deployments.create({'deployment_id': 'installed-image', 'asset_id': self.asset,
            'catalog_key': 'test-image', 'gpu_indices': [0], 'enabled': True}, owner=self.owner)
        self.template = {'schema': 1, 'recipe_key': 'test-image', 'recipe_digest': digest(self.entry), 'release_digest': release.digest,
            'resources': {'base_mib': 10, 'task_mib': 20, 'external_reserve_mib': 0, 'sharing_mode': 'shared', 'residency': 'on_demand', 'idle_seconds': 300},
            'limits': {'uid': 1000, 'gid': 1000, 'memory_bytes': 1024**3, 'nano_cpus': 10**9, 'pids_limit': 64, 'tmpfs_bytes': 16 * 1024**2}}
        self.runtime = InstallationRuntime(self.repo, self.images, {'test-image': self.template})

    def tearDown(self):
        self.repo.release_installation_runner(self.owner)
        self.fixture.tearDown()

    def commit(self): return self.runtime.commit(self.owner, 'installed-image', self.transfer, self.entry)

    def test_binding_activation_is_atomic_and_does_not_claim_health_or_generation(self):
        for point in ('binding.insert', 'binding.activate'):
            def fault(current):
                if current == point: raise RuntimeError(point)
            self.runtime.fault = fault
            with self.assertRaises(RuntimeError): self.commit()
            with self.repo._connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM instance_installation_bindings').fetchone()[0], 0)
            self.assertFalse(self.repo.get_deployment('installed-image')['enabled'])
        self.runtime.fault = lambda _: None
        self.commit()
        self.assertEqual(self.runtime.levels('installed-image'),
                         {'installed': True, 'env_checked': False, 'model_ready': False, 'generated_tested': False})
        self.assertEqual(self.repo.get_service_installation('installation')['state'], 'ready')
        self.assertFalse(self.repo.get_deployment('installed-image')['enabled'])

    def test_concurrent_commit_is_same_binding_not_two_installations(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.commit(), range(2)))
        self.assertEqual(results[0], results[1])
        with self.repo._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM instance_installation_bindings').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM instance_claims').fetchone()[0], 0)

    def test_license_and_immutable_asset_drift_do_not_activate(self):
        with self.repo._connect() as db:
            db.execute("UPDATE model_assets SET license_declared='different license' WHERE id=?", (self.asset,))
        with self.assertRaisesRegex(RuntimeContractError, 'runtime_asset_binding_changed'): self.commit()
        self.assertFalse(self.repo.get_deployment('installed-image')['enabled'])

    def test_readback_closes_on_deployment_drift_or_withdrawn_release_approval(self):
        self.commit()
        with self.repo._connect() as db:
            db.execute("UPDATE model_deployments SET revision='drifted-revision' WHERE id='installed-image'")
        with self.assertRaisesRegex(RuntimeContractError, 'runtime_installation_binding_changed'):
            self.runtime.levels('installed-image')
        with self.repo._connect() as db:
            db.execute("UPDATE model_deployments SET revision=? WHERE id='installed-image'", (self.entry['recommended_revision'],))
        self.images.approved_digests = frozenset()
        with self.assertRaisesRegex(RuntimeContractError, 'runtime_release_not_approved'):
            self.runtime.levels('installed-image')

    def test_asset_role_format_and_modality_are_rechecked_at_commit_and_readback(self):
        expected = {'role': self.entry['service_recipe']['role'],
                    'format': self.entry['service_recipe']['format'], 'media_kind': self.entry['kind']}
        for field, changed in [('role', 'lora'), ('format', 'safetensors'), ('media_kind', 'speech')]:
            with self.subTest(field=field):
                with self.repo._connect() as db:
                    db.execute(f'UPDATE model_assets SET {field}=? WHERE id=?', (changed, self.asset))
                with self.assertRaisesRegex(RuntimeContractError, 'runtime_asset_binding_changed'):
                    self.commit()
                self.assertFalse(self.repo.get_deployment('installed-image')['enabled'])
                with self.repo._connect() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM instance_installation_bindings').fetchone()[0], 0)
                    db.execute(f'UPDATE model_assets SET {field}=? WHERE id=?', (expected[field], self.asset))
        binding = self.commit()
        self.assertEqual(binding['asset_contract'], expected)
        self.assertEqual(binding['asset_source'], {'type': 'upload', 'reference': 'client-upload'})
        for field, changed in [('role', 'lora'), ('format', 'safetensors'), ('media_kind', 'speech')]:
            with self.repo._connect() as db:
                db.execute(f'UPDATE model_assets SET {field}=? WHERE id=?', (changed, self.asset))
            with self.assertRaisesRegex(RuntimeContractError, 'runtime_installation_binding_changed'):
                self.runtime.levels('installed-image')
            with self.repo._connect() as db:
                db.execute(f'UPDATE model_assets SET {field}=? WHERE id=?', (expected[field], self.asset))


class PublisherTests(unittest.TestCase):
    def setUp(self):
        InstallationBindingTests.setUp(self)
        InstallationBindingTests.commit(self)
        from mediacenter.redis_transport import RedisEndpoint
        self.manager_secret, self.seed_file = self.root / 'acl-manager.secret', self.root / 'epoch-seed.secret'
        self.manager_secret.write_text('manager-secret-that-is-never-mounted', encoding='ascii'); self.manager_secret.chmod(0o600)
        self.seed_file.write_text('separate-immutable-random-seed-value', encoding='ascii'); self.seed_file.chmod(0o600)
        self.private = self.root / 'publisher-state'; self.private.mkdir(mode=0o700)
        class Client:
            def __init__(self): self.users = {}; self.sets = []; self.saves = 0; self.fail_set = False; self.fail_save = False
            def close(self): pass
            def acl_getuser(self, user): return copy.deepcopy(self.users.get(user))
            def execute_command(self, command, user, *rules):
                assert command == 'ACL SETUSER'
                self.sets.append(user)
                value = EpochPublisher._expected(rules)
                for selector in value['selectors']:
                    for key in ('commands', 'keys', 'channels'): selector[key] = ' '.join(selector[key])
                self.users[user] = value
                if self.fail_set:
                    self.fail_set = False
                    raise RuntimeError('timeout after actual SETUSER')
                return True
            def acl_save(self):
                self.saves += 1
                if self.fail_save: raise RuntimeError('ACL file is not writable')
                return True
        self.client = Client()
        endpoint = RedisEndpoint('dedicated-publisher', self.manager_secret, unix_socket=str(self.root / 'test.sock'))
        self.publisher = EpochPublisher(self.repo, 'server', endpoint, self.seed_file, self.private, client=self.client)
        self.record = {'identity': {'server_id': 'server', 'instance_id': 'installed-image', 'worker_epoch': 'epoch-one'}}
        with self.repo._connect() as db:
            db.execute("INSERT INTO runtime_epoch_packages VALUES(?,?,?,?,?,?,?,?,'files_ready',?,?,NULL,'now','now')",
                ('package-one', 'installed-image', self.repo.get_deployment('installed-image')['incarnation'], 'epoch-one', 1, 1,
                 digest(self.template), self.publisher.authority_digest, canonical(self.record), digest(self.record)))

    def tearDown(self):
        self.publisher.close()
        InstallationBindingTests.tearDown(self)

    def test_unknown_set_readback_keeps_credentials_and_never_resets_user(self):
        initial = self.publisher.credentials('installed-image', 'epoch-one')
        self.client.fail_set = True
        with self.assertRaises(RuntimeError): self.publisher.publish('package-one')
        self.assertEqual(self.publisher._row('package-one')[0]['phase'], 'acl_unknown')
        self.assertEqual(self.publisher.publish('package-one')['phase'], 'ready')
        self.assertEqual(len(self.client.sets), 2)
        self.assertEqual(len(set(self.client.sets)), 2)
        self.assertEqual(self.publisher.credentials('installed-image', 'epoch-one'), initial)
        self.publisher.publish('package-one')
        self.assertEqual((len(self.client.sets), self.client.saves), (2, 1))

    def test_memory_acl_is_not_ready_until_save_and_foreign_users_are_never_reset(self):
        self.client.fail_save = True
        with self.assertRaises(RuntimeError): self.publisher.publish('package-one')
        self.assertEqual(self.publisher._row('package-one')[0]['phase'], 'acl_unknown')
        self.client.fail_save = False
        self.publisher.publish('package-one')
        self.assertEqual(len(self.client.sets), 2)
        user = self.client.sets[0]
        self.client.users[user]['commands'].append('+@all')
        with self.assertRaisesRegex(RuntimeContractError, 'runtime_acl_foreign_or_changed'):
            self.publisher.publish('package-one')
        self.assertEqual(len(self.client.sets), 2)
class EpochPackageTests(unittest.TestCase):
    def setUp(self):
        PublisherTests.setUp(self)
        from mediacenter.redis_transport import RedisEndpoint
        from mediacenter.instance_policy import InstancePolicy
        self.authority = InstancePolicy(self.repo)
        with self.repo._connect() as db:
            db.execute("DELETE FROM runtime_epoch_packages WHERE package_id='package-one'")
            deployment = db.execute("SELECT * FROM model_deployments WHERE id='installed-image'").fetchone()
            binding = self.authority.deployment_binding(db, deployment)
        self.sockets = self.root / 'sockets'; self.sockets.mkdir()
        (self.sockets / 'redis.sock').touch()
        self.publisher.close()
        self.publisher = EpochPublisher(self.repo, 'server',
            RedisEndpoint('dedicated-publisher', self.manager_secret, unix_socket=str(self.sockets / 'redis.sock')),
            self.seed_file, self.private, client=self.client)
        self.packages = self.root / 'packages'; self.packages.mkdir(mode=0o700)
        self.models = self.fixture.assets.storage_root
        self.inputs = self.root / 'readonly-inputs'; self.inputs.mkdir()
        self.key, self.engine_file = self.root / 'api.key', self.root / 'engine.sock'
        self.key.write_text('only-server'); self.engine_file.touch()
        self.engine = SimpleNamespace(socket_path=str(self.engine_file))
        self.policy = dict(self.template['resources'], backend='container', package_id='template_' + digest(self.template),
            gpus=['GPU-12345678-1234-1234-1234-123456789abc'], binding=binding)
        configured = self.authority.configure('installed-image', self.policy)
        started = self.authority.set_service('installed-image', True,
                                             expected_version=configured['version'])
        self.authority.desire('installed-image', 'loaded',
                              expected_version=started['version'])
        self.provisioner = EpochProvisioner(self.runtime, self.publisher, self.engine, self.packages,
            models_root=self.models, inputs_root=self.inputs, api_key_file=self.key)
        self.capability = patch('mediacenter.capabilities.worker_capability_for', return_value={'model_key': 'test-image'})
        self.capability.start()

    def tearDown(self):
        self.capability.stop()
        PublisherTests.tearDown(self)

    def test_immutable_package_and_credentials_resume_without_new_epoch(self):
        package = self.provisioner.ensure('installed-image')
        second = self.provisioner.ensure('installed-image')
        self.assertEqual((package.epoch, package.record_id, package.generation),
                         (second.epoch, second.record_id, second.generation))
        self.assertEqual(len(self.client.sets), 2)
        bootstrap = next(g for g in package.policy.mounts if g.role == 'bootstrap')
        body = json.loads(Path(bootstrap.source).read_bytes())
        self.assertEqual(len(body['binding']), 7)
        self.assertNotIn('only-server', Path(bootstrap.source).read_text())
        spec = package.policy.spec(name='owned', instance_id=package.instance_id, epoch=package.epoch,
                                   intent_id='intent-test', cgroup_parent='/owned')
        self.assertEqual(spec['HostConfig']['NetworkMode'], 'none')
        mounted = {m['Source'] for m in spec['HostConfig']['Mounts']}
        for secret in (self.key, self.manager_secret, self.seed_file, self.engine_file):
            self.assertNotIn(str(secret), mounted)
        with self.repo._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM instance_claims').fetchone()[0], 0)

    def test_started_on_demand_service_can_prepare_container_while_model_is_unloaded(self):
        current = self.authority.get('installed-image')
        self.authority.desire('installed-image', 'unloaded', expected_version=current['version'])
        package = self.provisioner.ensure('installed-image')
        self.assertEqual(package.instance_id, 'installed-image')
        with self.repo._connect() as db:
            policy = db.execute(
                'SELECT desired_state FROM instance_policies WHERE instance_id=?',
                ('installed-image',),
            ).fetchone()
        self.assertEqual(policy['desired_state'], 'unloaded')

    def test_stopped_service_cannot_prepare_new_container(self):
        current = self.authority.get('installed-image')
        stopped = self.authority.set_service(
            'installed-image', False, expected_version=current['version']
        )
        self.assertEqual(stopped['desired_state'], 'unloaded')
        with self.assertRaisesRegex(RuntimeContractError, 'runtime_service_start_required'):
            self.provisioner.ensure('installed-image')

    def test_runtime_boundary_snapshot_is_shared_within_each_verification(self):
        package = self.provisioner.ensure('installed-image')
        with patch.object(self.provisioner, '_global_forbidden',
                          wraps=self.provisioner._global_forbidden) as forbidden:
            loaded = self.provisioner.load(package.record_id)
            # One snapshot builds ContainerPolicy and one verifies the package
            # boundary.  Mount count and historical package count do not
            # multiply the number of full boundary scans.
            self.assertEqual(forbidden.call_count, 2)
            loaded.verify()
            self.assertEqual(forbidden.call_count, 3)

    def test_global_forbidden_history_is_cached_until_registry_revision_changes(self):
        from mediacenter.artifacts import RuntimeBoundaries
        package = self.provisioner.ensure('installed-image')
        first_boundary = self.root / 'first-sealed-boundary'
        first_boundary.mkdir()
        RuntimeBoundaries(self.repo).register('sealed', first_boundary)
        self.provisioner._forbidden_revision = None
        with patch.object(RuntimeBoundaries, '_checked',
                          wraps=RuntimeBoundaries._checked) as checked:
            first = self.provisioner._global_forbidden(package.record_id)
            scans = checked.call_count
            self.assertGreater(scans, 0)
            self.assertEqual(self.provisioner._global_forbidden(package.record_id), first)
            self.assertEqual(checked.call_count, scans)
            added = self.root / 'new-sealed-boundary'
            added.mkdir()
            RuntimeBoundaries(self.repo).register('sealed', added)
            after_registration = checked.call_count
            refreshed = self.provisioner._global_forbidden(package.record_id)
            self.assertIn(str(added.absolute()), refreshed)
            self.assertGreater(checked.call_count, after_registration)

    def test_global_forbidden_cache_ignores_package_state_only_updates(self):
        package = self.provisioner.ensure('installed-image')
        self.provisioner._forbidden_revision = None
        with patch('mediacenter.runtime_provisioning.identity',
                   wraps=__import__('mediacenter.runtime_provisioning',
                                    fromlist=['identity']).identity) as checked:
            first = self.provisioner._global_forbidden(package.record_id)
            scans = checked.call_count
            self.assertGreater(scans, 0)
            with self.repo._connect() as db:
                db.execute(
                    "UPDATE runtime_epoch_packages SET phase='intent',updated_at=? "
                    "WHERE package_id=?",
                    (now(), package.record_id),
                )
            self.assertEqual(
                self.provisioner._global_forbidden(package.record_id), first
            )
            self.assertEqual(checked.call_count, scans)

    def test_writable_boundary_history_is_cached_until_registry_revision_changes(self):
        from mediacenter.artifacts import RuntimeBoundaries
        package = self.provisioner.ensure('installed-image')
        self.provisioner._writable_boundary_revision = None
        with patch.object(RuntimeBoundaries, '_checked',
                          wraps=RuntimeBoundaries._checked) as checked:
            self.provisioner._objects(*self.publisher._row(package.record_id))
            scans = checked.call_count
            self.assertGreater(scans, 0)
            self.provisioner._objects(*self.publisher._row(package.record_id))
            self.assertEqual(checked.call_count, scans)
            added = self.root / 'new-writable-boundary'
            added.mkdir()
            RuntimeBoundaries(self.repo).register('outputs', added)
            after_registration = checked.call_count
            self.provisioner._objects(*self.publisher._row(package.record_id))
            self.assertGreater(checked.call_count, after_registration)

    def test_compiled_forbidden_set_avoids_per_mount_history_stat_scans(self):
        package = self.provisioner.ensure('installed-image')
        loaded = self.provisioner.load(package.record_id)
        forbidden = loaded.policy.forbidden_sources()
        self.assertIs(loaded.policy.forbidden_sources(), forbidden)
        grant = loaded.policy.mounts[0]
        with patch('mediacenter.config.checked_path',
                   wraps=__import__('mediacenter.config', fromlist=['checked_path']).checked_path) as checked:
            grant.verify(forbidden)
        self.assertLess(checked.call_count, 8)

    def test_equivalent_replaced_authority_files_are_adopted_once(self):
        from mediacenter.redis_transport import RedisEndpoint
        package = self.provisioner.ensure('installed-image')
        with self.repo._connect() as db:
            old_digest = db.execute(
                'SELECT authority_digest FROM runtime_epoch_packages WHERE package_id=?',
                (package.record_id,)).fetchone()[0]
        for path in (self.seed_file, self.manager_secret):
            replacement = path.with_suffix(path.suffix + '.replacement')
            replacement.write_bytes(path.read_bytes())
            replacement.chmod(0o600)
            os.replace(replacement, path)
        publisher = EpochPublisher(self.repo, 'server',
            RedisEndpoint('dedicated-publisher', self.manager_secret,
                          unix_socket=str(self.sockets / 'redis.sock')),
            self.seed_file, self.private, client=self.client)
        provisioner = EpochProvisioner(self.runtime, publisher, self.engine, self.packages,
            models_root=self.models, inputs_root=self.inputs, api_key_file=self.key)
        try:
            self.assertNotEqual(publisher.authority_digest, old_digest)
            with self.assertRaisesRegex(RuntimeContractError, 'runtime_publisher_authority_changed'):
                publisher._row(package.record_id)
            recovered = provisioner.load(package.record_id)
            self.assertEqual(recovered.record_id, package.record_id)
            with self.repo._connect() as db:
                adopted = db.execute(
                    'SELECT authority_digest FROM runtime_epoch_packages WHERE package_id=?',
                    (package.record_id,)).fetchone()[0]
            self.assertEqual(adopted, publisher.authority_digest)
            self.assertEqual(publisher._row(package.record_id)[0]['authority_digest'], adopted)
        finally:
            publisher.close()

    def test_changed_seed_cannot_adopt_old_authority(self):
        from mediacenter.redis_transport import RedisEndpoint
        package = self.provisioner.ensure('installed-image')
        with self.repo._connect() as db:
            old_digest = db.execute(
                'SELECT authority_digest FROM runtime_epoch_packages WHERE package_id=?',
                (package.record_id,)).fetchone()[0]
        replacement = self.seed_file.with_suffix('.changed')
        replacement.write_text('different-valid-random-seed-value-for-test', encoding='ascii')
        replacement.chmod(0o600)
        os.replace(replacement, self.seed_file)
        publisher = EpochPublisher(self.repo, 'server',
            RedisEndpoint('dedicated-publisher', self.manager_secret,
                          unix_socket=str(self.sockets / 'redis.sock')),
            self.seed_file, self.private, client=self.client)
        provisioner = EpochProvisioner(self.runtime, publisher, self.engine, self.packages,
            models_root=self.models, inputs_root=self.inputs, api_key_file=self.key)
        try:
            with self.assertRaisesRegex(RuntimeContractError, 'runtime_publisher_authority_changed'):
                provisioner.load(package.record_id)
            with self.repo._connect() as db:
                unchanged = db.execute(
                    'SELECT authority_digest FROM runtime_epoch_packages WHERE package_id=?',
                    (package.record_id,)).fetchone()[0]
            self.assertEqual(unchanged, old_digest)
        finally:
            publisher.close()

    def test_historical_server_identity_is_removal_only(self):
        from mediacenter.redis_transport import RedisEndpoint
        package = self.provisioner.ensure('installed-image')
        publisher = EpochPublisher(self.repo, 'replacement-server',
            RedisEndpoint('dedicated-publisher', self.manager_secret,
                          unix_socket=str(self.sockets / 'redis.sock')),
            self.seed_file, self.private, client=self.client)
        provisioner = EpochProvisioner(self.runtime, publisher, self.engine, self.packages,
            models_root=self.models, inputs_root=self.inputs, api_key_file=self.key)
        try:
            with self.assertRaisesRegex(RuntimeContractError, 'runtime_package_record_corrupt'):
                provisioner.load(package.record_id)
            self.runtime.images.approved_digests = frozenset()
            removal = provisioner.load_for_removal(package.record_id)
            self.assertEqual(removal.verify(), {
                'instance_id': 'installed-image', 'epoch': package.epoch,
                'runtime_record_id': package.record_id, 'removal_only': True})
            with self.repo._connect() as db:
                authority = db.execute(
                    'SELECT authority_digest FROM runtime_epoch_packages WHERE package_id=?',
                    (package.record_id,)).fetchone()[0]
            self.assertEqual(authority, self.publisher.authority_digest)
        finally:
            publisher.close()

    def test_exact_historical_capability_requires_original_claim_and_cannot_launch(self):
        from mediacenter.config import historical_sdxl_capability, HISTORICAL_SDXL_DIGEST
        from mediacenter.capabilities import worker_capability_for
        from mediacenter.instance_policy import InstancePolicy
        from tests.test_resident_policy import fixture_capacity
        self.capability.stop()
        current=worker_capability_for('sdxl-base-1.0'); old=historical_sdxl_capability()
        self.assertEqual(digest(old),HISTORICAL_SDXL_DIGEST);self.assertNotEqual(digest(current),digest(old))
        # Fixed model catalog boundary only; package, claim and files are real.
        binding=dict(self.policy['binding'],model_key='sdxl-base-1.0')
        self.policy=dict(self.policy,binding=binding)
        with patch.object(InstancePolicy,'deployment_binding',return_value=binding):
            configured = self.authority.configure('installed-image',self.policy,expected_version=self.authority.get('installed-image')['version'])
            self.authority.desire('installed-image', 'loaded', expected_version=configured['version'])
            with patch('mediacenter.capabilities.worker_capability_for',return_value=old):
                package=self.provisioner.ensure('installed-image')
                before=Path(next(g.source for g in package.policy.mounts if g.role=='bootstrap')).read_bytes()
                self.authority.package_validator=self.provisioner.validate_claim
                state=self.authority.get('installed-image')
                claim=self.authority.claim_container('installed-image',package.epoch,expected_version=state['version'],backend='container',
                    limits={gpu:100000 for gpu in self.policy['gpus']},package_identity=package.verify())
        recovered=self.provisioner.for_epoch('installed-image',package.epoch)
        self.assertEqual(recovered.recovery_capability(),old)
        self.assertEqual(recovered.verify()['capability_digest'],HISTORICAL_SDXL_DIGEST)
        self.assertEqual(recovered.outputs_path(),Path(next(g.source for g in package.policy.mounts if g.role=='outputs')))
        with self.repo._connect() as db:
            with self.assertRaisesRegex(RuntimeContractError,'runtime_historical_launch_forbidden'): recovered.launch_check(db)
            with self.assertRaisesRegex(RuntimeContractError,'runtime_historical_launch_forbidden'): self.provisioner.validate_claim(db,package.record_id)
        self.assertEqual(self.provisioner.reserve('installed-image')['epoch'],package.epoch)
        self.assertEqual(Path(next(g.source for g in recovered.policy.mounts if g.role=='bootstrap')).read_bytes(),before)

    def test_unclaimed_canceled_package_is_retired_and_new_load_gets_generation(self):
        old = self.provisioner.ensure('installed-image')
        self.authority.desire('installed-image', 'unloaded')
        self.authority.desire('installed-image', 'loaded')
        new = self.provisioner.ensure('installed-image')
        self.assertNotEqual(old.epoch, new.epoch)
        self.assertEqual(new.generation, old.generation + 1)
        with self.assertRaises(RuntimeContractError): old.verify()
        with self.assertRaises(RuntimeContractError): self.publisher.publish(old.record_id)
        self.assertEqual(len(self.client.sets), 4)
        self.assertTrue((self.packages / old.record_id).is_dir())

    def test_recorded_write_failure_reuses_inode_unrecorded_creation_is_unknown(self):
        fired = []
        def fault(point):
            if point == 'epoch.object_written' and not fired:
                fired.append(point); raise OSError('injected IO failure')
        self.provisioner.fault = fault
        with self.assertRaises(OSError): self.provisioner.ensure('installed-image')
        row = self.provisioner.reserve('installed-image')
        first = (self.packages / row['package_id']).stat().st_ino
        self.provisioner.fault = lambda _: None
        package = self.provisioner.ensure('installed-image')
        self.assertEqual((self.packages / package.record_id).stat().st_ino, first)
        self.authority.desire('installed-image', 'unloaded'); self.authority.desire('installed-image', 'loaded')
        def unknown(point):
            if point == 'epoch.object_created': raise OSError('crash before inode registration')
        self.provisioner.fault = unknown
        with self.assertRaises(OSError): self.provisioner.ensure('installed-image')
        self.provisioner.fault = lambda _: None
        with self.assertRaises(FileExistsError): self.provisioner.ensure('installed-image')
        self.assertEqual(len(self.client.sets), 2)

    def test_all_sealed_and_old_epoch_control_roots_are_forbidden_before_acl(self):
        from mediacenter.artifacts import RuntimeBoundaries
        from mediacenter.config import ContainerError
        RuntimeBoundaries(self.repo).register('sealed', self.models)
        with self.assertRaisesRegex(ContainerError, 'mount_forbidden_source'):
            self.provisioner.ensure('installed-image')
        self.assertEqual(self.client.sets, [])

    def test_old_server_secret_cannot_be_remounted_as_models(self):
        from mediacenter.config import ContainerError
        old = self.provisioner.ensure('installed-image')
        self.authority.desire('installed-image', 'unloaded'); self.authority.desire('installed-image', 'loaded')
        second = EpochProvisioner(self.runtime, self.publisher, self.engine, self.packages,
            models_root=self.packages / old.record_id / 'control', inputs_root=self.inputs, api_key_file=self.key)
        relative = self.repo.get_model_asset(self.asset)['storage_relpath']
        target = self.packages / old.record_id / 'control' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.models / relative, target)
        with self.assertRaisesRegex(ContainerError, 'mount_forbidden_source'):
            second.ensure('installed-image')
        self.assertEqual(len(self.client.sets), 2)

    def test_launch_rechecks_same_inode_bootstrap_not_only_database_snapshot(self):
        package = self.provisioner.ensure('installed-image')
        path = Path(next(g.source for g in package.policy.mounts if g.role == 'bootstrap'))
        inode = path.stat().st_ino
        body = json.loads(path.read_bytes()); body['recovery_complete'] = False
        path.write_text(canonical(body), encoding='utf-8')
        self.assertEqual(path.stat().st_ino, inode)
        with self.repo._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            with self.assertRaisesRegex(RuntimeContractError, 'runtime_package_file_changed'):
                package.launch_check(db)


if __name__ == '__main__': unittest.main()
