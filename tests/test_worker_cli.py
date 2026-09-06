import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mediacenter.capabilities import worker_capability_for
from mediacenter.worker_cli import read_bootstrap
from mediacenter.worker_common import digest


class WorkerCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.path = Path(self.temp.name) / 'bootstrap.json'
        self.value = dict(schema=1, server_id='server', instance_id='instance', worker_epoch='epoch', recovery_complete=True,
            clock_trusted=True, journal='/mc-journal/worker.db', outputs='/mc-outputs', adapter_id='sdxl',
            redis=dict(username='mc_w_'+digest(['server','instance','epoch']), secret_file='/mc-worker-secret', unix_socket='/mc-redis/redis.sock'),
            lora_authority=dict(path='/mc-lora', families=['sdxl']),
            binding=dict(model_key='sdxl-base-1.0', recipe_revision='r1', model_asset_id='mdl_1', model_asset_revision='r1',
                         image_digest='sha256:'+'a'*64, gpu_uuids=['GPU-'+'1'*36], capability_digest=digest(worker_capability_for('sdxl-base-1.0'))),
            asset_bindings=dict(main=dict(asset_id='mdl_1',revision='r1',manifest_digest='b'*64,path='/mc-models/assets/mdl_1'), dependencies={}))

    def tearDown(self): self.temp.cleanup()

    def save(self):
        self.path.write_text(json.dumps(self.value)); self.path.chmod(0o600)

    def test_fixed_bootstrap_and_no_receipt_admission(self):
        self.save(); value, identity, sha = read_bootstrap(self.path)
        self.assertEqual(identity.worker_epoch, 'epoch'); self.assertEqual(len(sha), 64)
        for key, wrong in [('adapter_id','user.module:execute'), ('journal','/server.db'), ('recovery_complete',False)]:
            old = self.value[key]; self.value[key] = wrong; self.save()
            with self.assertRaises(ValueError): read_bootstrap(self.path)
            self.value[key] = old

    def test_duplicate_unknown_fields_and_private_mode(self):
        self.value['secret'] = 'unexpected'; self.save()
        with self.assertRaises(ValueError): read_bootstrap(self.path)
        self.path.write_text('{"schema":1,"schema":1}'); self.path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'duplicate'): read_bootstrap(self.path)

    def test_isolated_import_does_not_import_models_or_server(self):
        command = [sys.executable, '-B', '-c', "import sys; import mediacenter.worker_cli; assert not set(('torch','diffusers','peft','mediacenter.repository','mediacenter.config','mediacenter.server','mediacenter.runtime_provisioning')) & set(sys.modules)"]
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try: out, error = child.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill(); out, error = child.communicate(timeout=5); self.fail('import timed out')
        print(f'MC036 CLI import pid={child.pid} exitcode={child.returncode} joined=true')
        self.assertEqual(child.returncode, 0, error.decode())

    def exercise_runtime(self, broker=None):
        """Map fixed container paths at fixture boundaries; never mock SDK/admit."""
        import multiprocessing
        import time
        from datetime import datetime, timedelta, timezone
        from mediacenter import worker_cli
        from mediacenter.adapter import AdapterFactory
        from mediacenter.redis_transport import RedisEndpoint
        from tests.test_worker_runtime import MemoryTransport
        from tests.test_worker_journal import BINDING, IDENTITY, envelope, receipt
        self.value.update(server_id=IDENTITY.server_id,instance_id=IDENTITY.instance_id,worker_epoch=IDENTITY.worker_epoch)
        self.value['binding']=dict(BINDING,gpu_uuids=['GPU-'+'1'*36])
        self.value['asset_bindings']['main'].update(asset_id=BINDING['model_asset_id'],revision=BINDING['model_asset_revision'],
                                                  path='/mc-models/assets/'+BINDING['model_asset_id'])
        self.value['redis']['username']='mc_w_'+digest([IDENTITY.server_id,IDENTITY.instance_id,IDENTITY.worker_epoch])
        self.save()
        original_read=worker_cli.read_bootstrap; original_checked=worker_cli.checked
        root=Path(self.temp.name); (root/'outputs').mkdir(); (root/'lora').mkdir()
        secret=root/'worker.secret';secret.write_text('cpu-fixture-private-secret-value-0123456789');secret.chmod(0o600)
        def mapped_read(path):
            value,identity,sha=original_read(path)
            value.update(journal=str(root/'journal.db'),outputs=str(root/'outputs'))
            return value,identity,sha
        def mapped_checked(path):return original_checked(root/'lora' if str(path)=='/mc-lora' else path)
        class Memory(MemoryTransport):
            def close(self):pass
        transport=broker.transport('worker','cli-worker') if broker else Memory()
        endpoint=transport.endpoint if broker else RedisEndpoint('fixture-worker',secret,unix_socket=str(root/'redis.sock'))
        server=broker.transport('server','cli-server') if broker else None
        if server:server.provision()
        counter=multiprocessing.get_context('spawn').Value('i',0)
        def factory(module,name,options):
            self.assertEqual((module,name),('mediacenter.adapters.sdxl','SDXLAdapter'))
            self.assertEqual(options['binding'],self.value['binding'])
            return AdapterFactory('tests.fake_model_adapter','FakeAdapter',{'counter':counter})
        runtime=None; process=None; threads=[]
        try:
            with patch.object(worker_cli,'read_bootstrap',side_effect=mapped_read), \
                 patch.object(worker_cli,'checked',side_effect=mapped_checked), \
                 patch.object(worker_cli,'AdapterFactory',side_effect=factory), \
                 patch('mediacenter.redis_transport.RedisEndpoint',return_value=endpoint), \
                 patch('mediacenter.redis_transport.RedisTransport',return_value=transport):
                runtime,returned=worker_cli.create_runtime(self.path)
            self.assertIs(returned,transport)
            self.assertEqual(runtime.journal.epoch_state(IDENTITY)['admitted'],1)
            process=runtime._process; self.assertTrue(process.is_alive())
            runtime.poll_seconds=.02; runtime.heartbeat_seconds=.05;runtime.start();threads=list(runtime._threads)
            def send(value,lane='commands'):
                stamp=datetime.now(timezone.utc)
                value['created_at']=stamp.isoformat().replace('+00:00','Z');value['expires_at']=(stamp+timedelta(seconds=30)).isoformat().replace('+00:00','Z')
                if server:server.publish(value)
                else:transport.enqueue(lane,value)
            def wait_terminal(kind):
                deadline=time.monotonic()+15;seen=[]
                while time.monotonic()<deadline:
                    if server:
                        for delivery in server.read('events',count=20,block_ms=25):
                            event=delivery.envelope;seen.append(event);send(receipt(event),'control');server.ack(delivery)
                    else:
                        with transport.lock: seen=list(transport.sent)
                    terminal=next((event for event in seen if event['type']==kind),None)
                    if terminal:return terminal
                    time.sleep(.02)
                self.fail('real CLI/SDK loop did not produce '+kind+' errors='+repr(runtime.errors))
            load=envelope('load');load['payload']['reservation_id']='cli-load-reservation';send(load)
            self.assertEqual(wait_terminal('model.terminal')['payload']['status'],'succeeded')
            send(envelope())
            terminal=wait_terminal('task.terminal')
            self.assertEqual(terminal['payload']['status'],'succeeded')
            self.assertEqual(counter.value,1);self.assertEqual(process.pid,runtime._process.pid)
            self.assertIn('execution_quiescence',terminal['extensions'])
        finally:
            if runtime:
                if process:
                    with patch.object(process,'close'):runtime.close()
                    self.assertFalse(process.is_alive());self.assertIsNotNone(process.exitcode)
                    print(f'MC036 CLI SDK pid={process.pid} exitcode={process.exitcode} joined=true threads_alive={sum(t.is_alive() for t in threads)}')
                    process.close()
                else:runtime.close()
                self.assertTrue(all(not t.is_alive() for t in threads))
            transport.close()

    def test_create_runtime_real_handshake_command_loops_and_close(self):
        self.exercise_runtime()

    @unittest.skipUnless(os.name=='posix' and os.environ.get('MC_REDIS_SERVER'),'explicit isolated Linux Redis tool required')
    def test_create_runtime_actual_private_redis_roundtrip(self):
        from tests.test_transport_recovery import PrivateRedis
        broker=PrivateRedis()
        try:self.exercise_runtime(broker)
        finally:
            broker.stop();print('MC036 CLI redis_evidence='+str(broker.directory/'process-evidence.json'))


if __name__ == '__main__': unittest.main()
