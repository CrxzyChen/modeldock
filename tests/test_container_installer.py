"""Formal installation flow with synthetic OCI bytes, never a real Engine."""
from __future__ import annotations

import io
import copy
import json
import threading
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from pathlib import Path
from urllib.request import Request, urlopen
from unittest.mock import patch

from mediacenter.runtime_artifacts import RuntimeArtifactStore
from mediacenter.runtime_provisioning import InstallationRuntime, RuntimeTemplate
from mediacenter.container_releases import RuntimeRelease
from mediacenter.worker_common import digest
from tests.test_runtime_artifacts import image_fixture


class StaticHardware:
    def __init__(self, total_mib=49140):
        self.total_mib = total_mib

    def gpu_snapshot(self, timeout_seconds=0.5):
        return {"available": True, "items": [{
            "uuid": "GPU-12345678-1234-1234-1234-123456789abc",
            "memory_used_mib": 0, "memory_total_mib": self.total_mib,
            "processes": [],
        }]}


def available_memory():
    return SimpleNamespace(pressure_avg60=0.0, available_bytes=128 * 1024 ** 3)


def attach_runtime_fixture(fixture):
    declaration, archive = image_fixture()
    release = RuntimeRelease(declaration)
    fixture.image_reads = []
    @contextmanager
    def source(contract, offset, timeout):
        fixture.image_reads.append(offset)
        yield io.BytesIO(archive[offset:])
    store = RuntimeArtifactStore(fixture.repository, fixture.root / 'installation-images',
        approved_digests=[release.digest], source=source)
    store.register(declaration)
    templates = {key: {'schema':1, 'recipe_key':key, 'recipe_digest':digest(entry), 'release_digest':release.digest,
        'limits':{'uid':1000,'gid':1000,'memory_bytes':1024**3,'nano_cpus':10**9,'pids_limit':64,'tmpfs_bytes':1024**2},
        'resources':{'base_mib':1024,'task_mib':256,'external_reserve_mib':8192,'sharing_mode':'shared','residency':'on_demand','idle_seconds':300}}
        for key, entry in fixture.installer.recipes.items()}
    class Importer:
        engine_id = 'fixture-engine'
        loads = 0
        def preflight(self): pass
        def load(self, stream, contract):
            self.loads += 1
        def inspect(self, contract, verified):
            return {'engine_id':self.engine_id,'image_id':contract.image_digest,'content_present':True}
    runtime, importer = InstallationRuntime(fixture.repository, store, templates), Importer()
    fixture.installer.installation_runtime = runtime
    fixture.installer.runtime_importer = importer
    return runtime, importer


class ContainerInstallerTests(unittest.TestCase):
    def setUp(self):
        from tests.test_service_installer import ServiceInstallerTests
        self.fixture = ServiceInstallerTests(); self.fixture.setUp()
        self.runtime = self.fixture.installer.installation_runtime
        self.importer = self.fixture.installer.runtime_importer

    def tearDown(self): self.fixture.tearDown()

    def test_reused_weights_no_conda_probe_and_image_binding_is_not_runtime_test(self):
        f = self.fixture
        asset = f.ready_asset()
        with (patch.object(f.assets, 'create_catalog_download', side_effect=AssertionError('duplicate weight download')),
             patch.object(f.installer, 'health_check', side_effect=AssertionError('implicit GPU probe'))):
            item = f.installer.start({'recipe_key':'test-image','gpu_indices':[0],'license_accepted':True})
            result = f.wait_terminal(item['id'])
        self.assertEqual(result['state'], 'ready', result)
        self.assertEqual(result['asset_id'], asset)
        binding = self.runtime.get('test-image')
        self.assertEqual(binding['asset_source'], {'type':'upload', 'reference':'client-upload'})
        self.assertEqual(result['runtime_image']['phase'], 'ready')
        self.assertEqual(self.importer.loads, 1)
        self.assertEqual(self.runtime.levels('test-image'),
            {'installed':True,'env_checked':False,'model_ready':False,'generated_tested':False})
        with f.repository._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM instance_claims').fetchone()[0], 0)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='environment_preparations'").fetchone())

    def test_unapproved_release_unavailable_before_weight_or_image_effect(self):
        f = self.fixture
        self.runtime.images.approved_digests = frozenset()
        with self.assertRaisesRegex(Exception, 'runtime_release_not_approved'):
            f.installer.start({'recipe_key':'test-image','gpu_indices':[0],'license_accepted':True})
        self.assertEqual(self.importer.loads, 0)
        self.assertEqual(f.image_reads, [])
        self.assertFalse(f.installer.catalog()[0]['runtime_available'])

    def test_existing_deployment_can_be_non_destructively_adopted(self):
        f = self.fixture
        asset = f.ready_asset()
        f.deployments.create({'deployment_id':'test-image','catalog_key':'test-image',
            'asset_id':asset,'gpu_indices':[0],'enabled':True})
        f.deployments.mark_install_state('test-image', 'ready')
        with f.repository._connect() as db:
            db.execute("UPDATE model_deployments SET is_default=1 WHERE id='test-image'")
        original = f.repository.get_deployment('test-image')
        item = f.installer.start({'recipe_key':'test-image','gpu_indices':[0],
            'license_accepted':True,'adopt_existing':True})
        result = f.wait_terminal(item['id'])
        self.assertEqual(result['state'], 'ready', result)
        current = f.repository.get_deployment('test-image')
        self.assertEqual(current['incarnation'], original['incarnation'])
        self.assertEqual(self.runtime.get('test-image')['operation_id'], result['id'])
        with f.repository._connect() as db:
            resource = db.execute("SELECT * FROM installation_resources WHERE attempt_id=? AND kind='deployment'",
                                  (result['current_attempt_id'],)).fetchone()
            self.assertIsNotNone(resource)
            self.assertEqual(resource['created'], 0)

    def test_inactive_deployment_can_atomically_upgrade_runtime_binding(self):
        from mediacenter.runtime_provisioning import EpochProvisioner
        from mediacenter.kernel import DeploymentLifecycle
        from mediacenter.model_registry import ModelRegistry
        f = self.fixture
        asset = f.ready_asset()
        f.deployments.create({'deployment_id':'test-image','catalog_key':'test-image',
            'asset_id':asset,'gpu_indices':[0],'enabled':True})
        f.deployments.mark_install_state('test-image', 'ready')
        first = f.wait_terminal(f.installer.start({'recipe_key':'test-image','gpu_indices':[0],
            'license_accepted':True,'adopt_existing':True,'external_reserve_mib':12288,
            'startup_policy':'auto'})['id'])
        old = self.runtime.get('test-image')
        provider = EpochProvisioner.__new__(EpochProvisioner)
        provider.installations, provider.repository = self.runtime, f.repository
        registry = ModelRegistry(f.deployments, installation_runtime=self.runtime)
        hardware = StaticHardware()
        lifecycle = DeploymentLifecycle(f.repository, registry, f.root/'sealed', gpu_indices=(0,),
            gpu_uuids=('GPU-12345678-1234-1234-1234-123456789abc',), package_provider=provider,
            hardware=hardware, memory_provider=available_memory)
        self.addCleanup(lifecycle.stop)
        original_policy = lifecycle.install_instance('test-image')
        f.installer.on_installed = lifecycle.install_instance
        changed = copy.deepcopy(self.runtime.templates['test-image'].data)
        changed['resources']['idle_seconds'] += 1
        changed['resources']['external_reserve_mib'] = 4096
        self.runtime.templates['test-image'] = RuntimeTemplate(changed)
        # The former explicit reservation no longer fits this GPU.  A template
        # upgrade projects the new safe default instead of leaving the instance
        # permanently unreconcilable.
        hardware.total_mib = 10000
        second = f.wait_terminal(f.installer.start({'recipe_key':'test-image','gpu_indices':[0],
            'license_accepted':True,'adopt_existing':True,'external_reserve_mib':8192,
            'startup_policy':'auto'})['id'])
        self.assertEqual((first['state'], second['state']), ('ready', 'ready'), second)
        current = self.runtime.get('test-image')
        self.assertNotEqual(current['operation_id'], old['operation_id'])
        self.assertEqual(current['operation_id'], second['id'])
        upgraded_policy = lifecycle.authority.get('test-image')
        self.assertNotEqual(upgraded_policy['policy']['package_id'], original_policy['policy']['package_id'])
        self.assertEqual(upgraded_policy['policy']['package_id'],
                         'template_' + self.runtime.templates['test-image'].digest)
        self.assertEqual(upgraded_policy['policy']['external_reserve_mib'], 4096)
        with f.repository._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM instance_installation_bindings WHERE instance_id=?',
                                        ('test-image',)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT is_default FROM model_deployments WHERE id='test-image'").fetchone()[0], 1)

    def test_explicit_user_reservation_survives_automatic_policy_projection(self):
        from mediacenter.runtime_provisioning import EpochProvisioner
        from mediacenter.kernel import DeploymentLifecycle
        from mediacenter.model_registry import ModelRegistry
        from mediacenter.task_state import TaskStateError
        f = self.fixture; f.ready_asset()
        item = f.installer.start({'recipe_key':'test-image','gpu_indices':[0],'license_accepted':True,
                                 'gpu_sharing_mode':'exclusive','external_reserve_mib':12288,
                                 'startup_policy':'auto'})
        result = f.wait_terminal(item['id'])
        self.assertEqual(result['state'], 'ready', result)
        # Only policy projection is exercised here; no publisher/Engine exists.
        provider = EpochProvisioner.__new__(EpochProvisioner)
        provider.installations, provider.repository = self.runtime, f.repository
        registry = ModelRegistry(f.deployments, installation_runtime=self.runtime)
        runtime = DeploymentLifecycle(f.repository, registry, f.root/'sealed', gpu_indices=(0,),
            gpu_uuids=('GPU-12345678-1234-1234-1234-123456789abc',), package_provider=provider,
            hardware=StaticHardware(), memory_provider=available_memory)
        row = runtime.install_instance('test-image')
        self.assertEqual((row['policy']['sharing_mode'], row['policy']['external_reserve_mib']), ('exclusive',12288))
        self.assertIsNone(row['claim'])
        changed = dict(row['policy'], base_mib=1)
        with self.assertRaisesRegex(TaskStateError, 'runtime_template_policy_mismatch'):
            runtime.authority.configure('test-image', changed, expected_version=row['version'])


class ServerInstallationTests(unittest.TestCase):
    def test_trusted_configuration_http_install_restart_and_formal_claim(self):
        from tests.test_service_installer import ServiceInstallerTests
        from mediacenter.server import create_server
        from mediacenter.runtime_provisioning import EpochPublisher
        from mediacenter.capabilities import worker_capability_for, MODEL_CAPABILITIES, WORKER_MODELS
        capability=worker_capability_for('sdxl-base-1.0'); capability['model_key']='test-image'
        MODEL_CAPABILITIES['test-image'] = copy.deepcopy(MODEL_CAPABILITIES['sdxl-base-1.0'])
        WORKER_MODELS['test-image'] = WORKER_MODELS['sdxl-base-1.0']
        self.addCleanup(MODEL_CAPABILITIES.pop, 'test-image')
        self.addCleanup(WORKER_MODELS.pop, 'test-image')
        f = ServiceInstallerTests(); f.setUp()
        self.addCleanup(f.tearDown)
        asset = f.ready_asset()
        declaration, archive = image_fixture()
        release = RuntimeRelease(declaration)
        directories = {name:f.root/name for name in ('publisher','packages','sockets')}
        for path in directories.values(): path.mkdir(mode=0o700)
        key = f.root/'api.key'; key.write_text('server-test-key',encoding='ascii'); key.chmod(0o600)
        seed = f.root/'seed.secret'; seed.write_text('separate-private-test-seed-not-production',encoding='ascii'); seed.chmod(0o600)
        manager = f.root/'manager.secret'; manager.write_text('private-manager-test-secret-not-production',encoding='ascii'); manager.chmod(0o600)
        socket_file=directories['sockets']/'redis.sock'; socket_file.touch()
        engine_socket=f.root/'engine.sock'; engine_socket.touch()
        config = {'server_id':'test-server','gpu_uuids':['GPU-12345678-1234-1234-1234-123456789abc'],
            'engine':{'socket_path':str(engine_socket)}, 'installation':{
                'releases':[declaration], 'approved_release_digests':[release.digest],
                'templates':{key:value.data for key,value in f.installer.installation_runtime.templates.items()},
                'image_store':str(f.root/'server-images'),'download_hosts':['fixtures.example'],
                'publisher':{'endpoint':{'username':'private-publisher','secret_file':str(manager),'unix_socket':str(socket_file)},
                             'seed_file':str(seed),'state_root':str(directories['publisher'])},
                'package_root':str(directories['packages'])}}
        config_path=f.root/'runtime.json'; config_path.write_text(json.dumps(config),encoding='utf-8'); config_path.chmod(0o600)
        class ACL:
            def __init__(self): self.users={}; self.sets=[]; self.saves=0
            def close(self): pass
            def acl_getuser(self,user): return copy.deepcopy(self.users.get(user))
            def execute_command(self,command,user,*rules):
                assert command=='ACL SETUSER'
                self.sets.append(user)
                value=EpochPublisher._expected(rules)
                for selector in value['selectors']:
                    for key in ('commands','keys','channels'): selector[key]=' '.join(selector[key])
                self.users[user]=value
                return True
            def acl_save(self): self.saves+=1; return True
        acl=ACL()
        @contextmanager
        def source(contract,offset,timeout): yield io.BytesIO(archive[offset:])
        servers=[]; threads=[]
        def stop(server):
            server.shutdown(); server.server_close()
            thread=threads[servers.index(server)]; thread.join(3)
            self.assertFalse(thread.is_alive())
        def open_server():
            server=create_server('127.0.0.1',0,f.repository.path,'server-test-key',
                catalog_path=f.catalog,model_store_root=f.assets.storage_root,artifact_root=f.root/'sealed',
                gpu_indices=(0,),runtime_config_path=config_path,api_key_file=key,start_kernel=False)
            installer=server.center.service_installer
            server.lifecycle.authority.tasks.capabilities={'test-image':capability}
            installer.installation_runtime.images.source=source
            installer.runtime_importer=f.installer.runtime_importer  # Engine effect boundary only.
            thread=threading.Thread(target=server.serve_forever); thread.start()
            servers.append(server); threads.append(thread)
            return server
        def request(server,path,payload=None):
            data=json.dumps(payload).encode() if payload is not None else None
            with urlopen(Request(f'http://127.0.0.1:{server.server_address[1]}'+path,data=data,
                         headers={'X-API-Key':'server-test-key','Content-Type':'application/json'}),timeout=3) as response:
                return json.loads(response.read())
        # Real config/Server/HTTP/SQLite/files; fake only host Engine constructor,
        # image I/O and ACL peer. This does not claim actual container execution.
        with patch('mediacenter.config.EngineConfig',side_effect=lambda **values:SimpleNamespace(**values)), \
             patch('mediacenter.container_runtime.UnixEngine',return_value=SimpleNamespace(config=SimpleNamespace(engine_id='fixture-engine'))), \
             patch('mediacenter.server.HardwareProbe',return_value=StaticHardware()), \
             patch('redis.Redis',return_value=acl), \
             patch('mediacenter.capabilities.worker_capability_for',return_value=capability):
            server=open_server()
            try:
                installer=server.center.service_installer
                with patch.object(installer,'_spawn'):
                    item=request(server,'/api/v1/service-installations',{'recipe_key':'test-image','gpu_indices':[0],
                        'license_accepted':True,'startup_policy':'auto','gpu_sharing_mode':'exclusive','external_reserve_mib':12288})
                installer._run_guarded(item['id'])
                result=request(server,'/api/v1/service-installations/'+item['id'])
                self.assertEqual((result['state'],result['asset_id']),('ready',asset),result)
                policy=server.lifecycle.authority.get('test-image')
                self.assertEqual(policy['desired_state'],'unloaded')
                self.assertTrue(server.center.repository.get_deployment('test-image')['enabled'])
                self.assertEqual((policy['policy']['sharing_mode'],policy['policy']['external_reserve_mib']),('exclusive',12288))
                self.assertIsNone(policy['claim'])
                with patch('subprocess.run',side_effect=AssertionError('GET cannot probe')):
                    request(server,'/api/v1/service-catalog'); request(server,'/api/v1/services'); request(server,'/healthz')
                self.assertEqual(acl.sets,[])
                server.lifecycle.request_load('test-image')
                policy=server.lifecycle.authority.get('test-image')
                package,identity=server.lifecycle._package(policy)
                scheduler=server.lifecycle.scheduler
                scheduler.memory_provider=lambda:SimpleNamespace(available_bytes=128*1024**3,pressure_avg60=0)
                observed={'available':True,'items':[{'uuid':policy['policy']['gpus'][0],'index':0,
                    'memory_total_mib':100000,'memory_used_mib':0,'processes':[]}]}
                with patch.object(scheduler.hardware,'gpu_snapshot',return_value=observed) as telemetry:
                    claim=scheduler.start_container('test-image',identity)
                    telemetry.assert_called_once_with(timeout_seconds=0.5)
                self.assertEqual(len(acl.sets),2)
                bootstrap=Path(next(m for m in package.policy.mounts if m.role=='bootstrap').source)
                self.assertEqual(len(json.loads(bootstrap.read_text())['binding']),7)
                old_epoch,old_record=package.epoch,package.record_id
            finally: stop(server)
            restarted=open_server()
            try:
                policy=restarted.lifecycle.authority.get('test-image')
                package,_=restarted.lifecycle._package(policy)
                self.assertEqual((package.epoch,package.record_id),(old_epoch,old_record))
                self.assertEqual(len(acl.sets),2)
                with restarted.center.repository._connect() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM instance_claims').fetchone()[0],1)
                    self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='environment_preparations'").fetchone())
                levels=restarted.center.service_installer.installation_runtime.levels('test-image')
                self.assertEqual(levels,{'installed':True,'env_checked':False,'model_ready':False,'generated_tested':False})
            finally: stop(restarted)


if __name__ == '__main__': unittest.main()
