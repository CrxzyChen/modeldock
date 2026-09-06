from __future__ import annotations
import copy
import json
import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

from mediacenter.capabilities import worker_capability_for
from mediacenter.task_state import canonical, digest
from mediacenter.transport import Identity
from mediacenter.worker_journal import CommandAuthority, JournalError, WorkerJournal

FIXTURES = {row['name']: row['envelope'] for row in json.loads((Path(__file__).parent / 'fixtures/worker-envelope-v1.json').read_text())['messages']}
NOW = datetime(2026, 8, 31, 12, 0, 30, tzinfo=timezone.utc)
IDENTITY = Identity('mediacenter', 'instance-one', 'epoch-one')
CAP = worker_capability_for('sdxl-base-1.0')
BINDING = dict(model_key='sdxl-base-1.0', recipe_revision='recipe-r1', model_asset_id='asset-sdxl',
               model_asset_revision='weights-r1', image_digest='sha256:' + 'a'*64, gpu_uuids=['GPU-one'], capability_digest=digest(CAP))


def envelope(name='execute.image', *, identity=IDENTITY, suffix=''):
    value = copy.deepcopy(FIXTURES[name])
    value.update(server_id=identity.server_id, instance_id=identity.instance_id, worker_epoch=identity.worker_epoch)
    value['message_id'] += suffix
    return value


def receipt(event):
    value = envelope('receipt.worker', identity=Identity(event['server_id'], event['instance_id'], event['worker_epoch']), suffix=event['message_id'])
    payload = dict(subject='worker', event_message_id=event['message_id'], event_seq=event['event_seq'])
    if 'task_id' in event:
        payload.update(subject='task', task_id=event['task_id'], attempt_id=event['attempt_id'])
    elif 'operation_id' in event['payload']:
        payload.update(subject='operation', operation_id=event['payload']['operation_id'], desired_revision=event['payload']['desired_revision'])
    value['payload'] = payload
    return value


def crash_journal(path, point, action):
    journal = WorkerJournal(path, IDENTITY.instance_id, {CAP['model_key']: CAP}, clock=lambda: NOW,
                            fault=lambda seen: os._exit(71) if point == seen else None)
    if action == 'receive':
        journal.receive(envelope())
    elif action == 'claim':
        journal.claim(IDENTITY)
    if point == 'after.commit':
        os._exit(72)


class WorkerJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mc030-journal-')
        self.path = Path(self.temp.name) / 'journal.db'
        self.now = NOW
        self.journal = self.open()
        self.journal.register(IDENTITY, BINDING)
        self.admit()
        self.grant()

    def tearDown(self):
        self.temp.cleanup()

    def open(self):
        return WorkerJournal(self.path, IDENTITY.instance_id, {CAP['model_key']: CAP}, clock=lambda: self.now)

    def admit(self, identity=IDENTITY):
        self.journal.admit(identity, BINDING, evidence='controller-proof', recovery_complete=True, clock_trusted=True)

    def grant(self, value=None):
        value = value or envelope()
        self.journal.grant(IDENTITY, value['payload']['reservation_id'], value['payload']['reservation_generation'],
                           kind='task' if value['type']=='task.execute' else 'load',
                           target_id=value.get('attempt_id') or value['payload']['operation_id'])

    def work(self):
        return self.journal.recovery()['work']

    def test_duplicate_and_alias_execute_do_not_duplicate_work(self):
        value = envelope()
        self.journal.receive(value)
        self.now += timedelta(days=1)
        self.journal.receive(value)
        self.journal.receive(envelope(suffix='alias'))
        self.assertEqual(len(self.work()), 1)
        changed = envelope(suffix='alias2')
        changed['payload']['parameters']['prompt'] = 'different'
        with self.assertRaisesRegex(JournalError, 'work_identity_conflict'):
            self.journal.receive(changed)
        changed['message_id'] = value['message_id']
        with self.assertRaisesRegex(JournalError, 'command_identity_conflict'):
            self.journal.receive(changed)

    def test_first_expired_rejected_and_cancel_not_dropped(self):
        self.now += timedelta(days=1)
        with self.assertRaisesRegex(JournalError, 'expired'):
            self.journal.receive(envelope())
        self.journal.receive(envelope('cancel'))
        self.now = NOW
        self.journal.receive(envelope())
        self.assertEqual(self.work()[0]['state'], 'canceled')
        self.assertIsNone(self.journal.claim(IDENTITY))

    def test_cancel_does_not_claim_exit_before_execution_finishes(self):
        self.journal.receive(envelope())
        work = self.journal.claim(IDENTITY)
        self.journal.receive(envelope('cancel'))
        self.assertEqual((self.work()[0]['state'], self.work()[0]['exit_confirmed']), ('cancel_requested', 0))
        token=self.journal.plan_child(IDENTITY)
        self.journal.child_started(IDENTITY,token,12345)
        self.journal.finish(IDENTITY, work['work_key'], work['execution_token'], status='succeeded', manifest={'asset_id':'a','revision':'r','sha256':'a'*64})
        terminal = self.journal.pending_events()['items'][-1]
        self.assertEqual(terminal['payload']['status'], 'canceled')
        self.assertIsNone(terminal['payload']['manifest'])

    def test_single_flight_across_connections(self):
        self.journal.receive(envelope())
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.open().claim(IDENTITY), range(2)))
        self.assertEqual(sum(value is not None for value in results), 1)

    def test_corrupt_work_never_claimed(self):
        self.journal.receive(envelope())
        original = self.work()[0]['envelope_json']
        for change in ('prompt', 'identity', 'structure'):
            value = json.loads(original)
            if change == 'prompt': value['payload']['parameters']['prompt'] = 'valid but corrupted'
            elif change == 'identity': value['attempt_id'] = 'other-attempt'
            else: value['payload']['unknown'] = True
            with self.journal._connect() as db:
                db.execute('UPDATE journal_work SET envelope_json=?', (canonical(value),))
            with self.assertRaisesRegex(JournalError, 'journal_work_corrupt'):
                self.journal.claim(IDENTITY)
            with self.assertRaisesRegex(JournalError, 'journal_work_corrupt'):
                self.work()
            with self.journal._connect() as db:
                self.assertEqual(db.execute('SELECT state FROM journal_work').fetchone()[0], 'accepted')

    def test_replaced_grant_quarantines_without_head_retry(self):
        self.journal.receive(envelope())
        self.journal.grant(IDENTITY, 'reservation-1', 2, kind='task', target_id='other')
        self.assertIsNone(self.journal.claim(IDENTITY))
        self.assertEqual(self.work()[0]['error_code'], 'resource_grant_changed')
        with self.assertRaisesRegex(JournalError, 'admission_required'):
            self.journal.claim(IDENTITY)

    def test_revision_conflict_and_newer_unload_blocks_old_load(self):
        unload = envelope('unload')
        unload['payload']['desired_revision'] = 2
        self.journal.receive(unload)
        load = envelope('load')
        load['payload']['operation_id'] = 'operation-old'
        load['payload']['reservation_id'] = 'load-reservation'
        self.grant(load)
        self.journal.receive(load)
        self.assertEqual(self.work()[1]['error_code'], 'stale_desired_revision')
        conflict = envelope('unload', suffix='conflict')
        conflict['payload'].update(operation_id='other-op', desired_revision=2)
        with self.assertRaisesRegex(JournalError, 'desired_revision_conflict'):
            self.journal.receive(conflict)

    def test_registration_receipt_never_admits_and_late_receipt_valid(self):
        newer = Identity(IDENTITY.server_id, IDENTITY.instance_id, 'epoch-two')
        self.journal.register(newer, BINDING)
        registered = self.journal.pending_events(epoch=newer.worker_epoch)['items'][0]
        self.now += timedelta(days=1)
        self.journal.receive(receipt(registered))
        self.assertFalse(self.journal.epoch_state(newer)['admitted'])
        self.assertEqual(self.journal.pending_events(epoch=newer.worker_epoch)['items'], [])

    def test_recovery_preserves_uncertain_until_controller_exit(self):
        self.journal.receive(envelope())
        self.journal.claim(IDENTITY)
        self.journal = self.open()
        self.assertTrue(self.work()[0]['uncertain'])
        newer = Identity(IDENTITY.server_id, IDENTITY.instance_id, 'epoch-two')
        self.journal.register(newer, BINDING)
        with self.assertRaisesRegex(JournalError, 'recovery_required'):
            self.admit(newer)
        self.journal.confirm_epoch_exit(IDENTITY, 'controller-descendant-exit')
        self.admit(newer)
        self.assertEqual(self.work()[0]['state'], 'interrupted')
        self.assertEqual(self.journal.pending_events()['items'][-1]['worker_epoch'], IDENTITY.worker_epoch)
        self.assertIsNone(self.journal.claim(newer))

    def test_atomic_authenticated_grant_and_acceptance(self):
        with self.journal._connect() as db: db.execute('DELETE FROM journal_grants')
        authority = CommandAuthority(IDENTITY, digest(BINDING), 'bootstrap-proof')
        self.journal.fault = lambda point: (_ for _ in ()).throw(sqlite3.OperationalError('disk full')) if point == 'receive.work' else None
        with self.assertRaises(sqlite3.OperationalError): self.journal.receive(envelope(), authority=authority)
        with self.journal._connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM journal_grants').fetchone()[0], 0)
        self.journal.fault = lambda _: None
        self.journal.receive(envelope(), authority=authority)
        self.assertIsNotNone(self.journal.claim(IDENTITY))

    def test_bad_admission_binding_clock_and_authority_never_accept(self):
        for field,value in (('image_digest','sha256:'+'b'*64),('gpu_uuids',['GPU-other']),('recipe_revision','other')):
            wrong=copy.deepcopy(BINDING); wrong[field]=value
            with self.assertRaisesRegex(JournalError,'binding_mismatch'):
                self.journal.admit(IDENTITY,wrong,evidence='proof',recovery_complete=True,clock_trusted=True)
        with self.assertRaisesRegex(JournalError,'admission_required'):
            self.journal.admit(IDENTITY,BINDING,evidence='proof',recovery_complete=True,clock_trusted=False)
        wrong=envelope(); wrong['payload']['recipe_revision']='wrong'
        with self.assertRaisesRegex(JournalError,'model_binding_mismatch'): self.journal.receive(wrong)
        with self.assertRaisesRegex(JournalError,'command_authority_mismatch'):
            self.journal.receive(envelope(),authority=CommandAuthority(IDENTITY,'a'*64,'proof'))
        self.assertEqual(self.work(),[])

    def test_snapshot_alias_and_receipt_identity_are_exact(self):
        self.journal.receive(envelope())
        self.journal.claim(IDENTITY)
        self.journal.receive(envelope('snapshot.request'))
        self.journal.receive(envelope('snapshot.request',suffix='alias'))
        events=self.journal.pending_events()['items']
        snapshots=[row for row in events if row['type']=='worker.snapshot']
        self.assertEqual(len(snapshots),1)
        self.assertEqual(snapshots[0]['payload']['tasks'][0]['state'],'running')
        wrong=receipt(events[-1]); wrong['payload']['event_seq']+=1
        with self.assertRaisesRegex(JournalError,'receipt_identity_conflict'): self.journal.receive(wrong)
        self.assertEqual(self.journal.pending_events()['items'],events)
        with self.journal._connect() as db:
            db.execute("UPDATE journal_events SET digest='corrupt' WHERE message_id=?",(events[-1]['message_id'],))
        with self.assertRaisesRegex(JournalError,'journal_event_corrupt'): self.journal.receive(receipt(events[-1]))

    def test_real_process_crash_transaction_and_committed_claim(self):
        context = multiprocessing.get_context('spawn')
        for point, action, expected in [('receive.command','receive',0), ('receive.work','receive',0), ('after.commit','receive',1), ('claim.executing','claim',1), ('after.commit','claim',1)]:
            process = context.Process(target=crash_journal, args=(str(self.path), point, action))
            process.start(); process.join(10)
            if process.is_alive(): process.kill(); process.join(3)
            self.assertIn(process.exitcode, (71,72))
            print('MC030 journal_crash=%s action=%s pid=%s exitcode=%s joined=true' % (point,action,process.pid,process.exitcode),flush=True)
            process.close()
            self.journal = self.open()
            self.assertEqual(len(self.work()), expected)
        self.assertTrue(self.work()[0]['uncertain'])

    def test_pages_do_not_truncate_history_or_unconfirmed_events(self):
        for number in range(135):
            value = envelope('unload', suffix=str(number))
            value['payload'].update(operation_id='op-'+str(number), desired_revision=number+1)
            self.journal.receive(value)
            work = self.journal.claim(IDENTITY)
            self.journal.finish(IDENTITY, work['work_key'], work['execution_token'], status='succeeded')
        for method, key in ((self.journal.recovery,'work'), (self.journal.pending_events,'items')):
            cursor, rows = 0, []
            while True:
                page = method(after_sequence=cursor, limit=50)
                rows += page[key]; cursor = page['next_cursor']
                if not page['has_more']: break
            self.assertEqual(len(rows), 135 if key=='work' else 271)

    def test_corrupt_database_rejected_readonly(self):
        other = Path(self.temp.name)/'corrupt.db'
        other.write_bytes(b'not sqlite')
        with self.assertRaisesRegex(JournalError, 'journal_corrupt'):
            WorkerJournal(other, IDENTITY.instance_id, {CAP['model_key']:CAP})
        self.assertEqual(other.read_bytes(), b'not sqlite')
