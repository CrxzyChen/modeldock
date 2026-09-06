from __future__ import annotations

import tempfile
import json
import hashlib
from dataclasses import replace
import unittest
from pathlib import Path
from types import SimpleNamespace

from mediacenter.kernel import DeploymentLifecycle
from mediacenter.repository import Repository
from mediacenter.instance_policy import InstancePolicy
from mediacenter.task_state import TaskStateError
from tests.test_gpu_reservations import Inventory
from tests.test_resident_policy import seed_deployment, policy_value
from tests.test_resident_policy import package_identity,fixture_capacity
from tests import test_task_state as task_fixture
from tests.test_runtime_controller import fixture_policy
from mediacenter.config import PreparedRuntime, EngineConfig, ContainerError
from mediacenter.capabilities import worker_capability_for
from mediacenter.task_state import digest


class KernelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.repo=Repository(self.root/'state.db')
        self.binding=seed_deployment(self.repo)
        self.lifecycle=DeploymentLifecycle(self.repo,None,self.root/'artifacts',gpu_indices=(0,),
            gpu_uuids=('GPU-one',),hardware=Inventory(),
            memory_provider=lambda:SimpleNamespace(available_bytes=64*1024**3,pressure_avg60=0))
        self.authority=self.lifecycle.authority
        self.authority.configure('instance-one',policy_value(self.binding,('GPU-one',)))
    def tearDown(self):
        self.lifecycle.stop();self.temp.cleanup()
    def test_composition_has_one_authority_and_no_generation_executor(self):
        self.assertIs(self.lifecycle.scheduler.authority,self.authority)
        self.assertIs(self.authority.repository,self.repo)
        self.assertFalse(hasattr(self.lifecycle,'legacy_pool'))
        self.assertFalse(hasattr(self.lifecycle,'_run'))
    def test_start_waits_without_package_and_never_claims_or_executes(self):
        task=self.authority.tasks.accept({'service':'image','model':'instance-one','prompt':'queued','options':{},'inputs':[]},scope='test',binding=self.binding)[0]
        self.lifecycle.request_load('instance-one')
        self.lifecycle.tick()
        self.assertEqual(self.authority.get('instance-one')['error_code'],'runtime_package_unavailable')
        self.assertEqual(self.repo.get_task(task['id'])['status'],'queued')
        with self.repo._connect() as db:
            for table in ('instance_claims','task_attempts','task_reservations','task_outbox'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
    def test_policy_and_gpu_persist_and_unload_only_sets_intent(self):
        self.lifecycle.request_load('instance-one')
        reopened=InstancePolicy(Repository(self.repo.path))
        self.assertEqual(reopened.get('instance-one')['policy']['gpus'],['GPU-one'])
        self.assertEqual(reopened.get('instance-one')['desired_state'],'loaded')
        self.lifecycle.request_unload('instance-one')
        self.assertEqual(reopened.get('instance-one')['desired_state'],'unloaded')
        self.assertEqual(reopened.get('instance-one')['status'],'waiting_runtime')
    def test_claim_cannot_follow_disabled_deployment(self):
        self.repo.update_deployment('instance-one',{'enabled':False})
        with self.assertRaises(TaskStateError):
            self.lifecycle.request_load('instance-one')
    def test_on_demand_new_queue_wakes_unloaded_intent_without_fake_runtime(self):
        value=policy_value(self.binding,('GPU-one',));value['residency']='on_demand'
        row=self.authority.configure('instance-one',value,expected_version=self.authority.get('instance-one')['version'])
        package=package_identity(value)
        self.authority.package_validator=lambda _db,record_id: package if record_id==package['runtime_record_id'] else None
        self.authority.claim_container('instance-one','epoch-one',expected_version=row['version'],backend='container',
            limits={'GPU-one':100},package_identity=package)
        self.assertFalse(self.authority.close_unstarted_claim('instance-one'))
        task=self.authority.tasks.accept({'service':'image','model':'instance-one','prompt':'wake','options':{},'inputs':[]},scope='test',binding=self.binding)[0]
        self.lifecycle.tick()
        row=self.authority.get('instance-one')
        self.assertEqual((row['desired_state'],row['status']),('loaded','waiting_runtime'))
        self.assertIsNotNone(row['claim'])
        self.assertEqual(self.repo.get_task(task['id'])['status'],'queued')
    def test_terminal_requires_authorized_manifest_not_driver_output_file(self):
        case=task_fixture.TaskStateTests();case.setUp()
        try:
            task,command=case.dispatched()
            (case.root/'orphan.png').write_bytes(b'orphan')
            manifest={'asset_id':'artifact-one','revision':'revision-one','sha256':'a'*64}
            event=case.event(command,kind='task.terminal',payload={'status':'succeeded','error_code':None,'manifest':manifest})
            self.assertEqual(case.state.receive(event),'pending')
            self.assertEqual(case.repository.get_task(task['id'])['status'],'assigned')
            case.state.record_sealed_artifact(task['id'],command['attempt_id'],cancel_revision=0,
                asset_id=manifest['asset_id'],revision=manifest['revision'],sha256=manifest['sha256'],
                relative_path='image/artifact.png',byte_size=12)
            self.assertEqual(case.repository.get_task(task['id'])['status'],'succeeded')
        finally:case.tearDown()

    def test_runtime_bootstrap_binds_real_server_and_excludes_control_secret(self):
        policy=fixture_policy(self.root,self.repo.path)
        control=self.root/'controller-redis-secret';control.write_bytes(b'a'*40)
        worker_binding={key:self.binding[key] for key in ('model_key','recipe_revision','model_asset_id','model_asset_revision')}
        worker_binding.update(image_digest='sha256:'+'a'*64,gpu_uuids=[],capability_digest=digest(worker_capability_for(self.binding['model_key'])))
        body=json.dumps({'server_id':'real-server','instance_id':'instance-one','worker_epoch':'epoch-one',
            'binding':worker_binding,'clock_trusted':True,'recovery_complete':True}).encode()
        (self.root/'bootstrap').write_bytes(body)
        package=PreparedRuntime('fixture-package','instance-one','epoch-one',self.binding,
            EngineConfig('/run/docker.sock','fixture-engine','/sys/fs/cgroup','/sys/fs/cgroup/mediacenter'),policy,
            {'secret_file':str(control)},hashlib.sha256(body).hexdigest(),'worker.db','real-server')
        self.assertEqual(package.verify()['epoch'],'epoch-one')
        with self.assertRaisesRegex(ContainerError,'bootstrap_binding_mismatch'):
            replace(package,server_id='different-server').verify()
        with self.assertRaises(ContainerError):
            replace(package,redis_options={'secret_file':str(self.root/'redis_credentials')}).verify()

if __name__=='__main__':unittest.main()
