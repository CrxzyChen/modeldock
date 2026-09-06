from __future__ import annotations
import multiprocessing
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import sys
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from mediacenter.adapter import AdapterFactory
from mediacenter.transport import Delivery, DeliveryPump, TransportError
from mediacenter.worker_journal import CommandAuthority, JournalError, WorkerJournal
from mediacenter.worker_runtime import WorkerRuntime, _execution_error_code, _FailureDiagnostics, _inference_child
from mediacenter.task_state import digest
from tests.test_worker_journal import BINDING, CAP, IDENTITY, NOW, envelope, receipt


def crashing_supervisor(path, point, report, cancellation):
    journal=WorkerJournal(path,IDENTITY.instance_id,{CAP['model_key']:CAP},clock=lambda:NOW)
    # Parent owns the test cancellation primitive; an intentionally killed
    # Supervisor must not leak newly named POSIX semaphores during this test.
    context=multiprocessing.get_context('spawn')
    with patch.object(context,'Event',return_value=cancellation):
        runtime=WorkerRuntime(IDENTITY,BINDING,journal,AdapterFactory('tests.fake_model_adapter','FakeAdapter'))
    def crash(seen):
        if seen == point:
            if point == 'runtime.after_spawn' and not runtime._connection.poll(10):
                raise RuntimeError('inference bootstrap barrier timeout')
            report.send(runtime._process.pid if runtime._process else None)
            report.recv()  # parent has acquired an exact process handle/pidfd
            os._exit(73)
    runtime.fault=crash
    runtime.admit(evidence='fixture-proof',recovery_complete=True,clock_trusted=True)
    journal.grant(IDENTITY,'reservation-1',1,kind='task',target_id='attempt-1')
    runtime.handle(envelope())
    runtime.execute_once()


def child_exit_watcher(pid):
    """Hold an OS process identity, never signal a recycled integer PID."""
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        kernel.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x100000|0x1000|1,False,pid)
        if not handle: raise RuntimeError('cannot acquire test child handle')
        def wait():
            try:
                exited=kernel.WaitForSingleObject(handle,10000)==0
                if not exited:
                    kernel.TerminateProcess(handle,99); kernel.WaitForSingleObject(handle,3000)
                return exited
            finally: kernel.CloseHandle(handle)
        return wait
    import select
    import signal
    descriptor=os.pidfd_open(pid)
    def wait():
        try:
            exited=bool(select.select([descriptor],[],[],10)[0])
            if not exited:
                signal.pidfd_send_signal(descriptor,signal.SIGKILL)
                select.select([descriptor],[],[],3)
            return exited
        finally: os.close(descriptor)
    return wait


class MemoryTransport:
    identity = IDENTITY
    def __init__(self):
        self.lanes = {'commands': [], 'control': []}
        self.sent, self.acks = [], []
        self.lock = threading.Lock()
        self.heartbeat = threading.Event()
        self.acked = threading.Event()
        self.fail_publish = self.fail_read = False

    def read(self, lane, **kwargs):
        if self.fail_read: raise TransportError('redis_disconnected')
        with self.lock:
            return self.lanes[lane][:1]

    def enqueue(self, lane, value):
        with self.lock:
            self.lanes[lane].append(Delivery(lane, value['message_id'], value))

    def publish(self, value):
        if self.fail_publish: raise TransportError('connection_error', outcome_unknown=True)
        with self.lock: self.sent.append(value)
        if value['type'] == 'telemetry.heartbeat': self.heartbeat.set()

    def ack(self, delivery):
        with self.lock:
            self.acks.append(delivery.entry_id)
            self.lanes[delivery.lane] = [row for row in self.lanes[delivery.lane] if row.entry_id != delivery.entry_id]
        self.acked.set()

    def quarantine(self, *args):
        raise AssertionError('durability failures must never be quarantined')


class WorkerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mc030-runtime-')
        self.journal = WorkerJournal(Path(self.temp.name)/'journal.db', IDENTITY.instance_id, {CAP['model_key']:CAP}, clock=lambda: NOW)
        self.context = multiprocessing.get_context('spawn')
        self.counter, self.resets = self.context.Value('i',0), self.context.Value('i',0)
        self.runtime = None

    def tearDown(self):
        self.close_runtime()
        self.temp.cleanup()

    def close_runtime(self):
        if not self.runtime: return
        runtime=self.runtime
        process=runtime._process
        threads=list(runtime._threads)
        try:
            if process is None:
                runtime.close()
            else:
                pid=process.pid
                with patch.object(process,'close'):
                    runtime.close()
                self.assertFalse(process.is_alive())
                self.assertIsNotNone(process.exitcode)
                print('MC030 child pid=%s exitcode=%s joined=true' % (pid,process.exitcode),flush=True)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            if threads: print('MC030 runtime_threads=%s alive=0' % len(threads),flush=True)
        finally:
            if process: process.close()
            self.runtime=None

    def make(self, transport=None, **options):
        factory = AdapterFactory('tests.fake_model_adapter', 'FakeAdapter', dict(counter=self.counter, resets=self.resets, **options))
        self.runtime = WorkerRuntime(IDENTITY, BINDING, self.journal, factory, transport,
                                     heartbeat_seconds=.05, poll_seconds=.02, cancel_grace_seconds=.05,
                                     command_authority=CommandAuthority(IDENTITY,digest(BINDING),'fixture-bootstrap'))
        self.runtime.admit(evidence='controller-proof', recovery_complete=True, clock_trusted=True)
        return self.runtime

    def accept(self, value=None):
        value = value or envelope()
        if value['type'] in {'task.execute','model.load'}:
            self.journal.grant(IDENTITY,value['payload']['reservation_id'],value['payload']['reservation_generation'],
                              kind='task' if value['type']=='task.execute' else 'load',target_id=value.get('attempt_id') or value['payload']['operation_id'])
        self.runtime.handle(value)

    def work(self): return self.journal.recovery()['work']

    def test_one_resident_child_handles_load_and_two_tasks_without_replay(self):
        runtime = self.make()
        load = envelope('load'); load['payload']['reservation_id']='load-reservation'
        self.accept(load); self.assertTrue(runtime.execute_once())
        pid = runtime._process.pid
        self.accept(); self.assertTrue(runtime.execute_once())
        first = self.work()[-1]
        self.assertIn('adapter_quiescent', first['quiescence_json'])
        self.assertEqual(self.journal.epoch_state(IDENTITY)['child_state'], 'alive')
        self.runtime.handle(envelope()); self.assertFalse(runtime.execute_once())
        second=envelope(suffix='second'); second.update(task_id='task-2',attempt_id='attempt-2')
        second['payload']['reservation_id']='reservation-2'
        self.accept(second); self.assertTrue(runtime.execute_once())
        self.assertEqual(runtime._process.pid,pid)
        self.assertEqual((self.counter.value,self.resets.value),(2,2))
        self.assertTrue(runtime._loaded)

    @unittest.skipUnless(os.name=='nt' or hasattr(os,'pidfd_open'),'requires exact OS process identity')
    def test_real_supervisor_crash_before_spawn_after_spawn_and_terminal(self):
        for number,point in enumerate(('runtime.after_executing','runtime.after_child_intent','runtime.after_spawn','runtime.after_terminal')):
            path=Path(self.temp.name)/('crash-'+str(number)+'.db')
            parent,child=self.context.Pipe()
            cancel=self.context.Event()
            process=self.context.Process(target=crashing_supervisor,args=(str(path),point,child,cancel))
            process.start(); child.close()
            watcher=None
            try:
                self.assertTrue(parent.poll(10),point)
                child_pid=parent.recv()
                if child_pid: watcher=child_exit_watcher(child_pid)
                parent.send(True); process.join(10)
                self.assertEqual(process.exitcode,73)
                if watcher:
                    self.assertTrue(watcher(),'idle inference child must observe closed Supervisor pipe')
                    watcher=None
                recovered=WorkerJournal(path,IDENTITY.instance_id,{CAP['model_key']:CAP},clock=lambda:NOW)
                rows=recovered.recovery()['work']
                boot=point in {'runtime.after_child_intent','runtime.after_spawn'}
                self.assertEqual(len(rows),0 if boot else 1)
                if not boot:
                    self.assertEqual(rows[0]['state'],'succeeded' if point=='runtime.after_terminal' else 'executing')
                    self.assertEqual(rows[0]['uncertain'],point!='runtime.after_terminal')
                    self.assertIsNone(recovered.claim(IDENTITY))
                else:
                    self.assertFalse(recovered.epoch_state(IDENTITY)['admitted'])
                    with self.assertRaisesRegex(JournalError,'admission_required'): recovered.claim(IDENTITY)
                print('MC030 crash=%s supervisor=%s supervisor_exitcode=%s child=%s observed_exit=true' % (point,process.pid,process.exitcode,child_pid),flush=True)
            finally:
                parent.close()
                if process.is_alive(): process.kill(); process.join(3)
                if watcher: watcher()
                process.close()

    def test_blocked_child_control_heartbeat_and_grace_are_independent(self):
        entered, release = self.context.Event(), self.context.Event()
        transport = MemoryTransport()
        runtime = self.make(transport,entered=entered,release=release,cooperative=False)
        transport.enqueue('commands',envelope())
        runtime.start()
        self.assertTrue(entered.wait(10))
        transport.heartbeat.clear()
        transport.enqueue('control',envelope('cancel'))
        deadline=time.monotonic()+3
        while time.monotonic()<deadline and not runtime.stop_requests():
            transport.acked.wait(.03); transport.acked.clear()
        self.assertTrue(transport.heartbeat.wait(1))
        self.assertEqual(len(runtime.stop_requests()),1)
        self.assertEqual((self.work()[0]['state'],self.work()[0]['exit_confirmed']),('cancel_requested',0))
        release.set()
        deadline=time.monotonic()+3
        while time.monotonic()<deadline and self.work()[0]['state']=='cancel_requested':
            transport.heartbeat.wait(.03); transport.heartbeat.clear()
        self.assertEqual(self.work()[0]['state'],'canceled')
        self.assertEqual(self.counter.value,1)

    def test_default_gpu_cancel_grace_allows_slow_cooperative_callback(self):
        import inspect
        self.assertEqual(inspect.signature(WorkerRuntime).parameters['cancel_grace_seconds'].default, 60)

    def test_cancel_first_has_zero_execution_and_no_extra_spawn(self):
        runtime=self.make()
        bootstrap_pid=runtime._process.pid
        runtime.handle(envelope('cancel'))
        self.accept()
        self.assertFalse(runtime.execute_once())
        self.assertEqual(runtime._process.pid,bootstrap_pid)
        self.assertEqual(self.counter.value,0)
        proof=self.journal.pending_events()['items'][-1]['extensions']['execution_quiescence']
        self.assertEqual(proof['kind'],'never_started')
        self.assertNotIn('child_token',proof)

    def test_actual_adapter_capability_mismatch_does_not_admit_or_accept(self):
        with self.assertRaisesRegex(JournalError,'child_capability_mismatch'):
            self.make(capability_mismatch=True)
        self.assertFalse(self.journal.epoch_state(IDENTITY)['admitted'])
        with self.assertRaisesRegex(TransportError,'admission_required'): self.runtime.handle(envelope())
        self.assertEqual(self.work(),[])
        self.assertEqual(self.counter.value,0)

    def test_handshake_barrier_does_not_temporarily_admit_or_accept(self):
        entered,release=self.context.Event(),self.context.Event()
        transport=MemoryTransport()
        factory=AdapterFactory('tests.fake_model_adapter','FakeAdapter',dict(counter=self.counter,capability_entered=entered,capability_release=release))
        runtime=WorkerRuntime(IDENTITY,BINDING,self.journal,factory,transport,
                              command_authority=CommandAuthority(IDENTITY,digest(BINDING),'bootstrap-proof'))
        self.runtime=runtime
        errors=[]
        def admit():
            try: runtime.admit(evidence='proof',recovery_complete=True,clock_trusted=True)
            except Exception as error: errors.append(error)
        thread=threading.Thread(target=admit)
        thread.start()
        try:
            self.assertTrue(entered.wait(10))
            transport.enqueue('commands',envelope())
            with self.assertRaisesRegex(TransportError,'admission_required'): runtime._receive_lane('commands')
            self.assertEqual((self.work(),transport.acks,self.counter.value),([],[],0))
        finally:
            release.set(); thread.join(10)
        self.assertFalse(thread.is_alive()); self.assertEqual(errors,[])
        runtime._receive_lane('commands')
        self.assertEqual(self.work()[0]['state'],'accepted')
        with self.assertRaisesRegex(JournalError,'admission_already_completed'):
            runtime.admit(evidence='another',recovery_complete=True,clock_trusted=True)

    def _concurrent_admission_case(self, mismatch):
        entered,release=self.context.Event(),self.context.Event()
        transport=MemoryTransport()
        factory=AdapterFactory('tests.fake_model_adapter','FakeAdapter',dict(counter=self.counter,
                               capability_entered=entered,capability_release=release,capability_mismatch=mismatch))
        runtime=WorkerRuntime(IDENTITY,BINDING,self.journal,factory,transport,
                              command_authority=CommandAuthority(IDENTITY,digest(BINDING),'bootstrap-proof'))
        self.runtime=runtime
        errors=[]
        def admit():
            try: runtime.admit(evidence='proof',recovery_complete=True,clock_trusted=True)
            except Exception as error: errors.append(error)
        first=threading.Thread(target=admit)
        first.start()
        try:
            self.assertTrue(entered.wait(10))
            for _ in range(2):
                with self.assertRaisesRegex(JournalError,'admission_in_progress'):
                    runtime.admit(evidence='second-proof',recovery_complete=True,clock_trusted=True)
            self.assertFalse(runtime._quarantined,'reentry must not poison the first valid handshake')
            self.assertFalse(self.journal.epoch_state(IDENTITY)['admitted'])
            transport.enqueue('commands',envelope())
            with self.assertRaisesRegex(TransportError,'admission_required'): runtime._receive_lane('commands')
            self.assertEqual((self.work(),transport.acks,self.counter.value),([],[],0))
            # These loops never acquire the admission mutex.
            self.assertEqual(runtime.heartbeat_once()['type'],'telemetry.heartbeat')
            transport.enqueue('control',envelope('snapshot.request'))
            runtime._receive_lane('control')
            self.assertEqual(transport.acks,[envelope('snapshot.request')['message_id']])
        finally:
            release.set(); first.join(10)
        self.assertFalse(first.is_alive())
        self.assertEqual(self.journal.epoch_state(IDENTITY)['admitted'],not mismatch)
        if mismatch:
            self.assertEqual([error.code for error in errors],['child_capability_mismatch'])
            with self.assertRaisesRegex(TransportError,'admission_required'): runtime._receive_lane('commands')
            self.assertEqual((self.work(),self.counter.value),([],0))
            self.assertIsNone(runtime._verified_child)
        else:
            self.assertEqual(errors,[])
            runtime._receive_lane('commands')
            self.assertEqual(self.work()[0]['state'],'accepted')

    def test_concurrent_admission_cannot_bypass_blocked_handshake(self):
        self._concurrent_admission_case(False)

    def test_concurrent_admission_cannot_bypass_eventual_capability_mismatch(self):
        self._concurrent_admission_case(True)

    def test_verified_child_identity_is_not_inherited_by_replacement_or_close(self):
        runtime=self.make()
        self.assertIsNotNone(runtime._verified_child)
        runtime._child_token='replacement-child'
        with self.assertRaisesRegex(JournalError,'child_capability_unverified'): runtime._ensure_child()
        self.close_runtime()
        self.assertIsNone(runtime._verified_child)
        with self.assertRaisesRegex(JournalError,'runtime_closed'):
            runtime.admit(evidence='new-proof',recovery_complete=True,clock_trusted=True)
        with self.assertRaisesRegex(TransportError,'runtime_closed'): runtime.handle(envelope())

    def test_adapter_cannot_inject_quiescence_and_partial_load_is_isolated(self):
        runtime=self.make(forge_proof=True)
        self.accept()
        with self.assertRaises(ValueError): runtime.execute_once()
        self.assertTrue(self.work()[0]['uncertain'])
        self.assertFalse(any(event['type']=='task.terminal' for event in self.journal.pending_events()['items']))

    def test_partial_load_error_does_not_claim_base_cleanup(self):
        runtime=self.make(load_fail=True)
        self.accept(envelope('load')); runtime.execute_once()
        self.assertEqual(self.work()[0]['exit_confirmed'],0)
        self.assertFalse(runtime.execute_once())
        self.assertFalse(runtime._loaded)

    def test_cancel_deadline_survives_transport_failure_once(self):
        transport=MemoryTransport(); runtime=self.make(transport)
        self.accept(); runtime._active=self.journal.claim(IDENTITY)
        current=[0.0]; runtime.clock=lambda:current[0]
        runtime.handle(envelope('cancel')); current[0]=1
        transport.fail_read=True
        for _ in range(3):
            with self.assertRaises(TransportError): runtime._receive_lane('control')
        self.assertEqual(len(runtime.stop_requests()),1)
        self.assertEqual(self.work()[0]['exit_confirmed'],0)

    def test_reset_failure_has_no_quiescence_and_blocks_next_task(self):
        runtime=self.make(reset_fail=True)
        self.accept(); runtime.execute_once()
        work=self.work()[0]
        self.assertEqual((work['state'],work['exit_confirmed']),('failed',0))
        self.assertIsNone(work['quiescence_json'])
        before=len(self.journal.pending_events()['items'])
        self.assertFalse(runtime.execute_once())
        self.close_runtime()
        self.journal.confirm_epoch_exit(IDENTITY,'controller-full-exit')
        self.assertEqual(len(self.journal.pending_events()['items']),before)
        self.assertEqual(self.work()[0]['exit_confirmed'],1)

    def test_typed_model_oom_resets_and_next_task_reuses_the_same_child(self):
        runtime = self.make(execution_oom='typed')
        pid = runtime._process.pid
        self.accept(); self.assertTrue(runtime.execute_once())
        work = self.work()[0]
        self.assertEqual((work['state'], work['error_code'], work['exit_confirmed']),
                         ('failed', 'model_out_of_memory', 1))
        self.assertIn('adapter_quiescent', work['quiescence_json'])
        terminal = self.journal.pending_events()['items'][-1]
        self.assertEqual(terminal['payload']['error_code'], 'model_out_of_memory')
        self.assertNotIn('secret', json.dumps(terminal))
        self.assertFalse(runtime._quarantined)
        second = envelope(suffix='after-oom'); second.update(task_id='task-2', attempt_id='attempt-2')
        second['payload']['reservation_id'] = 'reservation-2'
        self.accept(second); self.assertTrue(runtime.execute_once())
        self.assertEqual(self.work()[-1]['state'], 'succeeded')
        self.assertEqual(runtime._process.pid, pid)
        self.assertEqual((self.counter.value, self.resets.value), (2, 2))

    def test_oom_words_in_an_ordinary_exception_do_not_classify_as_oom(self):
        runtime = self.make(execution_oom='text')
        self.accept(); runtime.execute_once()
        self.assertEqual(self.work()[0]['error_code'], 'adapter_execution_failed')
        self.assertEqual(self.work()[0]['exit_confirmed'], 1)

    def test_oom_reset_failure_still_quarantines_without_an_exit_proof(self):
        runtime = self.make(execution_oom='typed', reset_fail=True)
        self.accept(); runtime.execute_once()
        work = self.work()[0]
        self.assertEqual((work['state'], work['error_code'], work['exit_confirmed']),
                         ('failed', 'adapter_reset_failed', 0))
        self.assertIsNone(work['quiescence_json'])
        terminal = self.journal.pending_events()['items'][-1]
        self.assertNotIn('execution_quiescence', terminal.get('extensions', {}))
        self.assertTrue(runtime._quarantined)
        self.assertFalse(runtime.execute_once())

    def test_prior_user_cancel_is_not_replaced_by_later_oom(self):
        entered, release = self.context.Event(), self.context.Event()
        runtime = self.make(execution_oom='typed', entered=entered, release=release, cooperative=False)
        self.accept()
        errors = []
        def execute():
            try: runtime.execute_once()
            except Exception as error: errors.append(error)
        thread = threading.Thread(target=execute); thread.start()
        try:
            self.assertTrue(entered.wait(10))
            runtime.handle(envelope('cancel'))
        finally:
            release.set(); thread.join(10)
        self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
        self.assertEqual((self.work()[0]['state'], self.work()[0]['error_code'], self.work()[0]['exit_confirmed']),
                         ('canceled', 'canceled', 1))

    def test_terminal_write_failure_retains_uncertain_and_isolates(self):
        runtime=self.make()
        self.accept()
        self.journal.fault=lambda point: (_ for _ in ()).throw(sqlite3.OperationalError('disk full')) if point=='finish.state' else None
        with self.assertRaises(sqlite3.OperationalError): runtime.execute_once()
        self.assertTrue(self.work()[0]['uncertain'])
        self.assertEqual(self.work()[0]['state'],'executing')
        self.assertFalse(runtime.execute_once())
        self.assertEqual(self.counter.value,1)

    def test_journal_failure_never_acks_and_authenticated_grant_rolls_back(self):
        transport=MemoryTransport(); runtime=self.make(transport)
        transport.enqueue('commands',envelope())
        self.journal.fault=lambda point: (_ for _ in ()).throw(sqlite3.OperationalError('read only')) if point=='receive.command' else None
        with self.assertRaises(TransportError): runtime._receive_lane('commands')
        self.assertEqual(transport.acks,[])
        self.assertEqual(self.work(),[])
        with self.journal._connect() as db: self.assertEqual(db.execute('SELECT count(*) FROM journal_grants').fetchone()[0],0)

    def test_actual_sqlite_readonly_and_full_do_not_ack(self):
        transport=MemoryTransport(); runtime=self.make(transport)
        original=self.journal._connect
        @contextmanager
        def readonly():
            with original() as db:
                db.execute('PRAGMA query_only=ON')
                yield db
        transport.enqueue('commands',envelope())
        self.journal._connect=readonly
        with self.assertRaises(TransportError): runtime._receive_lane('commands')
        self.journal._connect=original
        self.assertEqual(transport.acks,[])
        with original() as db: maximum=db.execute('PRAGMA page_count').fetchone()[0]
        @contextmanager
        def full():
            with original() as db:
                db.execute('PRAGMA max_page_count='+str(maximum))
                yield db
        self.journal._connect=full
        failed=False
        try:
            for number in range(100):
                value=envelope('cancel',suffix=str(number)); value['attempt_id']='attempt-'+str(number)
                transport.enqueue('control',value)
                before=len(transport.acks)
                try: runtime._receive_lane('control')
                except TransportError:
                    self.assertEqual(len(transport.acks),before); failed=True; break
        finally:
            self.journal._connect=original
        self.assertTrue(failed,'SQLite must enforce the explicit fixed-page capacity')

    def test_plain_dictionary_does_not_create_resource_authority(self):
        runtime=self.make()
        with self.assertRaisesRegex(TransportError,'resource_grant_required'): runtime.handle(envelope())
        self.assertEqual(self.work(),[])

    def test_transport_uncertainty_replays_same_event_until_precise_receipt(self):
        transport=MemoryTransport(); runtime=self.make(transport)
        current=[100.0]; runtime.clock=lambda:current[0]
        self.accept(); runtime.execute_once()
        original=self.journal.pending_events()['items']
        proof=original[-1]['extensions']['execution_quiescence']
        self.assertEqual(proof['kind'],'quiescent')
        self.assertEqual(proof['command_digest'],digest(envelope()))
        transport.fail_publish=True
        with self.assertRaises(TransportError): runtime.emit_once()
        self.assertEqual(self.journal.pending_events()['items'],original)
        transport.fail_publish=False
        self.assertEqual(runtime.emit_once(),0)
        current[0] += runtime.event_replay_seconds
        runtime.emit_once()
        self.assertEqual(runtime.emit_once(),0)
        current[0] += runtime.event_replay_seconds
        runtime.emit_once()
        for event in original: runtime.handle(receipt(event))
        self.assertEqual(self.journal.pending_events()['items'],[])
        self.assertEqual(len(transport.sent),2*len(original))

    def test_delayed_controller_receipt_paces_durable_event_replay(self):
        transport=MemoryTransport(); runtime=self.make(transport)
        current=[50.0]; runtime.clock=lambda:current[0]
        self.accept(); runtime.execute_once()
        original=self.journal.pending_events()['items']
        self.assertEqual(runtime.emit_once(),len(original))
        for _ in range(100):
            current[0] += runtime.poll_seconds
            self.assertEqual(runtime.emit_once(),0)
        self.assertEqual(len(transport.sent),len(original))
        current[0] += runtime.event_replay_seconds
        self.assertEqual(runtime.emit_once(),len(original))
        self.assertEqual(len(transport.sent),2*len(original))


class FailureDiagnosticTests(unittest.TestCase):
    def run_child(self, adapter):
        value = envelope()
        class Connection:
            def __init__(self):
                self.commands = iter([{'kind': 'execute', 'execution_token': 'execution-exact', 'envelope': value}, {'kind': 'stop'}])
                self.sent = []
            def recv_bytes(self, limit): return json.dumps(next(self.commands)).encode()
            def send_bytes(self, data): self.sent.append(json.loads(data))
            def close(self): pass
        connection = Connection()
        with patch.dict(sys.modules):
            _inference_child(SimpleNamespace(create=lambda: adapter), connection, threading.Event())
        return connection.sent

    def test_supervisor_import_does_not_import_a_model_framework(self):
        result = subprocess.run([sys.executable, '-c',
            'import sys; import mediacenter.worker_runtime; assert "torch" not in sys.modules'],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_type_classification_does_not_guess_device_or_inspect_exception_text(self):
        from tests.fake_model_adapter import FixtureOutOfMemoryError
        class PrivateError(FixtureOutOfMemoryError):
            def __str__(self): raise AssertionError('must not read exception text')
        with patch.dict(sys.modules, {'torch': SimpleNamespace(OutOfMemoryError=FixtureOutOfMemoryError)}):
            self.assertEqual(_execution_error_code(PrivateError()), 'model_out_of_memory')
            self.assertEqual(_execution_error_code(RuntimeError('CUDA out of memory')), 'adapter_execution_failed')
            self.assertEqual(_execution_error_code(MemoryError()), 'adapter_execution_failed')
        with patch.dict(sys.modules, {'torch': None}):
            self.assertEqual(_execution_error_code(PrivateError()), 'adapter_execution_failed')

    def test_diagnostic_is_allowlisted_and_bound_to_the_exact_execution(self):
        output, written = [], threading.Event()
        def write(data): output.append(data); written.set()
        with patch('mediacenter.worker_runtime._write_failure_diagnostic', side_effect=write):
            sink = _FailureDiagnostics()
            try:
                value = envelope(); value['payload']['prompt'] = 'private-prompt'
                sink.submit(value, 'execution-exact')
                self.assertTrue(written.wait(2))
            finally: sink.close(); sink.thread.join(2)
        self.assertFalse(sink.thread.is_alive())
        self.assertEqual(len(output), 1); self.assertLessEqual(len(output[0]), 4096)
        record = json.loads(output[0])
        self.assertEqual(set(record), {'schema', 'error_code', 'exception_type', 'phase', 'command_digest',
            'execution_token', 'task_id', 'attempt_id', 'server_id', 'instance_id', 'worker_epoch', 'message_id'})
        self.assertEqual(record['command_digest'], digest(value))
        self.assertEqual(record['execution_token'], 'execution-exact')
        for key in ('task_id', 'attempt_id', 'server_id', 'instance_id', 'worker_epoch', 'message_id'):
            self.assertEqual(record[key], value[key])
        self.assertNotIn(b'private-prompt', output[0]); self.assertNotIn(b'traceback', output[0])

    def test_blocked_logging_is_bounded_and_does_not_delay_submission_or_close(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(data): entered.set(); release.wait(5)
        with patch('mediacenter.worker_runtime._write_failure_diagnostic', side_effect=blocked):
            sink = _FailureDiagnostics()
            try:
                sink.submit(envelope(), 'first'); self.assertTrue(entered.wait(2))
                before = time.monotonic()
                for _ in range(100): sink.submit(envelope(), 'next')
                sink.close()
                self.assertLess(time.monotonic() - before, 1)
                self.assertLessEqual(sink.pending.qsize(), 1)
                self.assertTrue(sink.thread.is_alive())
            finally: release.set(); sink.close(); sink.thread.join(2)
        self.assertFalse(sink.thread.is_alive())

    def test_write_failure_is_best_effort_without_retry(self):
        written = threading.Event()
        def failed(data): written.set(); raise OSError('full or closed stderr')
        with patch('mediacenter.worker_runtime._write_failure_diagnostic', side_effect=failed) as writer:
            sink = _FailureDiagnostics()
            try:
                sink.submit(envelope(), 'execution-exact'); self.assertTrue(written.wait(2))
            finally: sink.close(); sink.thread.join(2)
            self.assertEqual(writer.call_count, 1)

    def test_child_records_first_cause_before_reset_but_keeps_cleanup_failure(self):
        from tests.fake_model_adapter import FakeAdapter
        order = []
        adapter = FakeAdapter(execution_oom='typed')
        def reset(): order.append('reset'); raise RuntimeError('secret reset text')
        adapter.reset_task_state = reset
        value = envelope()
        sink = SimpleNamespace(submit=lambda command, token: order.append((command, token)), close=lambda: None)
        with patch('mediacenter.worker_runtime._FailureDiagnostics', return_value=sink):
            sent = self.run_child(adapter)
        self.assertEqual(order, [(value, 'execution-exact'), 'reset'])
        self.assertEqual(sent[-1]['error_code'], 'model_out_of_memory')
        self.assertFalse(sent[-1]['clean'])
        self.assertNotIn('secret', json.dumps(sent))

    def test_diagnostic_thread_start_failure_does_not_disable_inference_or_reset(self):
        from tests.fake_model_adapter import FakeAdapter
        adapter = FakeAdapter(execution_oom='typed')
        with patch.object(adapter, 'reset_task_state') as reset, patch.object(threading.Thread, 'start', side_effect=RuntimeError('no threads')):
            sent = self.run_child(adapter)
        self.assertEqual(reset.call_count, 1)
        self.assertEqual(sent[0]['kind'], 'ready')
        self.assertEqual((sent[-1]['status'], sent[-1]['error_code'], sent[-1]['clean']),
                         ('failed', 'model_out_of_memory', True))

    def test_blocked_stderr_does_not_block_real_child_cleanup_and_finished_message(self):
        from tests.fake_model_adapter import FakeAdapter
        entered, release = threading.Event(), threading.Event()
        def blocked(data): entered.set(); release.wait(5)
        adapter = FakeAdapter(execution_oom='typed')
        sinks = []
        def create_sink():
            sink = _FailureDiagnostics(); sinks.append(sink); return sink
        def reset(): self.assertTrue(entered.wait(2))
        with patch('mediacenter.worker_runtime._write_failure_diagnostic', side_effect=blocked), \
                patch('mediacenter.worker_runtime._FailureDiagnostics', side_effect=create_sink), \
                patch.object(adapter, 'reset_task_state', side_effect=reset) as reset_call:
            try:
                sent = self.run_child(adapter)
                self.assertEqual(reset_call.call_count, 1)
                self.assertEqual((sent[-1]['status'], sent[-1]['error_code'], sent[-1]['clean']),
                                 ('failed', 'model_out_of_memory', True))
                self.assertTrue(sinks[0].thread.is_alive(), 'logger is still blocked after terminal was sent')
            finally:
                release.set()
                for sink in sinks: sink.close(); sink.thread.join(2)
        self.assertTrue(all(not sink.thread.is_alive() for sink in sinks))


@unittest.skipUnless(os.name=='posix' and os.environ.get('MC_REDIS_SERVER'),'requires private Redis tool')
class RedisWorkerRuntimeTests(unittest.TestCase):
    setUp = WorkerRuntimeTests.setUp
    tearDown = WorkerRuntimeTests.tearDown
    make = WorkerRuntimeTests.make
    work = WorkerRuntimeTests.work
    close_runtime = WorkerRuntimeTests.close_runtime
    def test_real_redis_blocked_child_cancel_disconnect_and_replay(self):
        from tests.test_transport_recovery import PrivateRedis
        broker=PrivateRedis()
        entered,release=self.context.Event(),self.context.Event()
        try:
            server=broker.transport('server','controller'); worker=broker.transport('worker','worker')
            server.provision()
            self.assertTrue(server._telemetry_ready.wait(3))
            runtime=self.make(worker,entered=entered,release=release,cooperative=False)
            # Journal expiry is a fixed historical fixture. Live transport
            # telemetry needs an actual emission timestamp, not that clock.
            original_telemetry = runtime._telemetry
            def fresh_telemetry(*args, **kwargs):
                from datetime import datetime, timezone
                message = original_telemetry(*args, **kwargs)
                message['created_at'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
                return message
            runtime._telemetry = fresh_telemetry
            runtime.cancel_grace_seconds=.2
            server.publish(envelope()); runtime.start()
            self.assertTrue(entered.wait(10))
            server.publish(envelope('cancel'))
            deadline=time.monotonic()+3
            while time.monotonic()<deadline and not self.journal.canceled(IDENTITY,'task-1','attempt-1'):
                threading.Event().wait(.02)
            self.assertTrue(self.journal.canceled(IDENTITY,'task-1','attempt-1'))
            deadline=time.monotonic()+3
            while server.telemetry('heartbeat') is None and time.monotonic()<deadline:
                threading.Event().wait(.01)
            self.assertIsNotNone(server.telemetry('heartbeat'))
            # Real Redis unavailable while the already-journaled cancellation
            # reaches its grace deadline. No success/exit may be invented.
            broker.stop()
            deadline=time.monotonic()+3
            while time.monotonic()<deadline and not runtime.stop_requests(): threading.Event().wait(.02)
            self.assertEqual(len(runtime.stop_requests()),1)
            self.assertEqual(self.work()[0]['exit_confirmed'],0)
            release.set()
            deadline=time.monotonic()+3
            while time.monotonic()<deadline and self.work()[0]['state']=='cancel_requested': threading.Event().wait(.02)
            self.assertEqual(self.work()[0]['state'],'canceled')
            original=self.journal.pending_events()['items']
            runtime._stop.set()
            for thread in runtime._threads: thread.join(3)
            self.assertTrue(all(not thread.is_alive() for thread in runtime._threads))
            broker.start()
            server=broker.transport('server','recovery-controller'); runtime.transport=broker.transport('worker','recovery-worker')
            server.provision(); runtime.emit_once()
            self.assertEqual(self.journal.pending_events()['items'],original)
            self.assertEqual(self.counter.value,1)
        finally:
            release.set()
            self.close_runtime()
            broker.stop()
            print('MC030 redis_evidence='+str(broker.directory/'process-evidence.json'),flush=True)

    def test_real_redis_commit_ack_and_receipt_roundtrip(self):
        from tests.test_transport_recovery import PrivateRedis
        broker=PrivateRedis()
        try:
            server=broker.transport('server','controller')
            worker=broker.transport('worker','worker')
            server.provision()
            runtime=self.make(worker)
            server.publish(envelope())
            runtime._receive_lane('commands')
            self.assertEqual(self.work()[0]['state'],'accepted')
            runtime.execute_once(); runtime.emit_once()
            deliveries=server.read('events',count=10,block_ms=1)
            self.assertEqual(len(deliveries),4)
            for delivery in deliveries:
                server.publish(receipt(delivery.envelope)); server.ack(delivery)
            for _ in deliveries: runtime._receive_lane('control')
            self.assertEqual(self.journal.pending_events()['items'],[])
        finally:
            self.close_runtime()
            broker.stop()
            print('MC030 redis_evidence='+str(broker.directory/'process-evidence.json'),flush=True)
