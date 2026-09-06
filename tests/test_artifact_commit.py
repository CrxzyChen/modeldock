from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from mediacenter.artifacts import ArtifactStore, RuntimeBoundaries, identity, canonical
from mediacenter.repository import Repository
from mediacenter.task_state import TaskState, TaskStateError, digest
from tests import test_task_state

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + (2).to_bytes(4,"big") * 2 + b"fixture-not-a-full-decoder-test"


class ArtifactCommitTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_task_state.TaskStateTests();self.fixture.setUp()
        self.root=self.fixture.root;self.repo=self.fixture.repository;self.tasks=self.fixture.state
        self.outputs=self.root/"scratch";self.outputs.mkdir()
        self.store=ArtifactStore(self.tasks,self.root/"sealed",writable_roots=[self.outputs])
        self.task,self.command=self.fixture.dispatched()
        self.sha=hashlib.sha256(PNG).hexdigest()
        self.event=self.fixture.event(self.command,kind="task.terminal",payload={"status":"succeeded","error_code":None,
            "manifest":{"asset_id":"art-one","revision":"v1","sha256":self.sha}})
        self.directory=self.outputs/"tasks"/self.task["id"]/self.command["attempt_id"]
        self.directory.mkdir(parents=True)
        self.manifest={"schema":1,**{k:self.event[k] for k in ("task_id","attempt_id","instance_id","worker_epoch")},
            "command_message_id":self.command["message_id"],"command_digest":digest(self.command),
            **self.event["payload"]["manifest"],"byte_size":len(PNG),"media_type":"image/png"}
        self.write_manifest();(self.directory/"artifact.png").write_bytes(PNG)
        if getattr(self,'persist_event',True):self.assertEqual(self.tasks.receive(self.event),"pending")
    def tearDown(self):self.fixture.tearDown()
    def write_manifest(self): (self.directory/"manifest.json").write_text(json.dumps(self.manifest))
    def output(self):return self.repo.get_task(self.task["id"])["output"]["artifact_path"]
    def publication(self):
        with self.repo._connect() as db:
            return dict(db.execute('SELECT * FROM artifact_publications WHERE task_id=?', (self.task['id'],)).fetchone())
    def cut_at(self, point):
        self.store.fault = lambda p: (_ for _ in ()).throw(RuntimeError(p)) if p == point else None
        with self.assertRaisesRegex(RuntimeError, point):
            self.store.worker(self.event, self.outputs)
        self.store.fault = lambda _: None
    def test_actual_copy_terminal_and_replay_use_one_server_inode(self):
        source=self.directory/"artifact.png"
        alias=self.directory/"hardlink.png";os.link(source,alias)
        with source.open("r+b") as old:
            self.assertEqual(self.store.worker(self.event,self.outputs),"committed")
            target=self.store.root/self.output()
            self.assertNotEqual(source.stat().st_ino,target.stat().st_ino)
            old.write(b"corrupt");old.flush()
            alias.write_bytes(b"more changes")
        with self.store.authorize(self.output()) as allowed:self.assertEqual(allowed.stream.read(),PNG)
        with patch('mediacenter.artifacts.time.time',return_value=10**12):
            self.assertEqual(self.store.worker(self.event,self.outputs),"committed")
        self.assertEqual(self.fixture.count("task_artifacts"),1)
        self.assertEqual(self.fixture.count("artifact_publications"),1)
        self.assertEqual(list(self.store.root.glob('*.part')), [])
        self.assertIsNone(self.publication()['temporary_path'])
        before = self.repo.path.read_bytes()
        for _ in range(10):
            self.assertEqual(self.store.recover_cleanup(), {})
        self.assertEqual(self.repo.path.read_bytes(), before)

    def test_two_existing_stores_share_new_durable_write_roots_after_restart(self):
        second = ArtifactStore(TaskState(Repository(self.repo.path)), self.store.root)
        fresh = self.root / 'epoch-two-outputs'; fresh.mkdir()
        RuntimeBoundaries(Repository(self.repo.path)).register('outputs', fresh)
        self.assertIn(fresh, self.store.boundaries.writable())
        self.assertIn(fresh, second.boundaries.writable())
        self.assertIn(self.outputs, second.boundaries.writable())
        descendant = self.store.root / 'forbidden-epoch'; descendant.mkdir()
        with self.assertRaisesRegex(TaskStateError, 'artifact_writable_root_overlap'):
            RuntimeBoundaries(self.repo).register('journal', descendant)
        with self.assertRaisesRegex(TaskStateError, 'artifact_writable_root_overlap'):
            RuntimeBoundaries(self.repo).register('outputs', self.root)
        fresh.rename(self.root / 'preserved-epoch-two'); fresh.mkdir()
        for store in (self.store, second):
            with self.assertRaisesRegex(TaskStateError, 'runtime_boundary_changed'): store.check_boundary()
        with self.assertRaisesRegex(TaskStateError, 'runtime_boundary_changed'):
            ArtifactStore(TaskState(Repository(self.repo.path)), self.store.root)
    def test_cancel_on_another_connection_during_copy_wins_no_authorization(self):
        def fault(point):
            if point=="copy.block":TaskState(Repository(self.repo.path)).cancel(self.task["id"])
        self.store.fault=fault
        self.assertEqual(self.store.worker(self.event,self.outputs),"canceled")
        self.assertEqual(self.fixture.count("task_artifacts"),0)
        for path in self.store.root.glob("*.png"):
            with self.assertRaises(TaskStateError):self.store.authorize(path.name)
    def test_disk_full_retains_existing_user_file_and_no_success(self):
        user=self.store.root/"existing.png";user.write_bytes(PNG)
        with patch("mediacenter.artifacts.copy_snapshot",side_effect=OSError(28,"full")):
            with self.assertRaises(OSError):self.store.worker(self.event,self.outputs)
        self.assertEqual(user.read_bytes(),PNG)
        self.assertEqual(self.fixture.count("task_artifacts"),0)
        self.assertEqual(self.repo.get_task(self.task["id"])["status"],"assigned")
        with patch('mediacenter.artifacts.time.time',return_value=10**12):
            self.assertEqual(self.store.worker(self.event,self.outputs),"committed")
    def test_bad_content_and_transient_io_have_durable_finite_copy_budget(self):
        (self.directory/'artifact.png').write_bytes(PNG[:-1]+b'x')
        for _ in range(8):
            reopened=ArtifactStore(TaskState(Repository(self.repo.path)),self.store.root,writable_roots=[self.outputs])
            with self.assertRaises(TaskStateError):reopened.worker(self.event,self.outputs)
        self.assertEqual(len(list(self.store.root.glob('*.part'))),1)
        self.assertEqual(sum(p.stat().st_size for p in self.store.root.iterdir()),len(PNG))
        self.assertEqual(self.fixture.count('task_artifacts'),0)
        self.tearDown();self.setUp()
        def full(point):
            if point=='copy.block':raise OSError(28,'full')
        for index in range(8):
            reopened=ArtifactStore(TaskState(Repository(self.repo.path)),self.store.root,writable_roots=[self.outputs],fault=full)
            with patch('mediacenter.artifacts.time.time',return_value=10**10+100*index):
                with self.assertRaises((TaskStateError,OSError)):reopened.worker(self.event,self.outputs)
        self.assertEqual(len(list(self.store.root.glob('*.part'))),1)
        self.assertLessEqual(sum(p.stat().st_size for p in self.store.root.iterdir()),len(PNG))
        with patch('mediacenter.artifacts.time.time',return_value=10**12):
            self.assertEqual(self.store.worker(self.event,self.outputs),'committed')
    def test_manifest_identity_traversal_size_unknown_and_duplicate_rejected(self):
        for changes in ({"revision":"forged"},{"command_digest":"0"*64},{"task_id":"../bad"},
                        {"path":"/tmp/arbitrary"},{"byte_size":len(PNG)+1},{"byte_size":True},
                        {"media_type":"video/mp4"}):
            with self.subTest(changes=changes):
                saved=dict(self.manifest);self.manifest.update(changes);self.write_manifest()
                with self.assertRaises((TaskStateError,OSError)):self.store.worker(self.event,self.outputs)
                # Cases rejected after intent keep their evidence; use separate
                # task fixtures to avoid treating changed declarations as retry.
                self.manifest=saved
                with self.repo._connect() as db:db.execute("DELETE FROM artifact_publications")
        (self.directory/"manifest.json").write_text('{"schema":1,"schema":1}')
        with self.assertRaises(TaskStateError):self.store.worker(self.event,self.outputs)
        self.assertEqual(self.fixture.count("task_artifacts"),0)

    def test_video_metadata_is_exact_bounded_and_audio_tuple_is_atomic(self):
        from mediacenter.artifacts import validate_media_metadata
        valid={"width":832,"height":480,"frame_count":33,"fps_numerator":16,
               "fps_denominator":1,"duration_ms":2063}
        self.assertIsNone(validate_media_metadata("video/mp4",valid))
        with_audio={**valid,"audio_streams":1,"audio_sample_rate":24000,"audio_channels":2}
        self.assertIsNone(validate_media_metadata("video/mp4",with_audio))
        for media,value in (("image/png",valid),("video/mp4",{**valid,"extra":1}),
                            ("video/mp4",{**valid,"frame_count":0}),
                            ("video/mp4",{**valid,"duration_ms":86400001}),
                            ("video/mp4",{**with_audio,"audio_streams":0}),
                            ("video/mp4",{**with_audio,"audio_sample_rate":0}),
                            ("video/mp4",{**with_audio,"audio_channels":9})):
            with self.subTest(media=media,value=value),self.assertRaises(TaskStateError):
                validate_media_metadata(media,value)

    def test_wav_metadata_is_decoder_derived_exact_and_bounded(self):
        from mediacenter.artifacts import validate_media_metadata
        valid={"sample_rate":32000,"channels":1,"sample_count":3200,
               "duration_ms":100,"bits_per_sample":16}
        self.assertIsNone(validate_media_metadata("audio/wav",valid))
        for value in ({**valid,"sample_rate":0},{**valid,"channels":9},
                      {**valid,"sample_count":3300},{**valid,"bits_per_sample":12},
                      {**valid,"extra":1}):
            with self.subTest(value=value),self.assertRaises(TaskStateError):
                validate_media_metadata("audio/wav",value)

    def test_slot_creation_enospc_recovers_without_spending_orphan_budget(self):
        original=os.open
        def no_slot(path,flags,*args,**kwargs):
            if str(path).endswith('.part') and flags & os.O_CREAT:raise OSError(28,'inode full')
            return original(path,flags,*args,**kwargs)
        for index in range(5):
            with patch('mediacenter.artifacts.time.time',return_value=10**10+100*index),patch('mediacenter.artifacts.os.open',side_effect=no_slot):
                with self.assertRaises(OSError):self.store.worker(self.event,self.outputs)
        self.assertEqual(list(self.store.root.glob('*.part')),[])
        status=self.repo.get_task(self.task['id'])['publication']
        self.assertEqual(status['error_code'],'publication_io_error')
        self.assertGreater(status['retry_after'],0)
        with patch('mediacenter.artifacts.time.time',return_value=10**12):
            self.assertEqual(self.store.worker(self.event,self.outputs),'committed')

    def test_parallel_publishers_share_one_owned_slot(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        entered,release=threading.Event(),threading.Event()
        def hold(point):
            if point=='copy.block':entered.set();self.assertTrue(release.wait(3))
        self.store.fault=hold
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.store.worker,self.event,self.outputs)
            self.assertTrue(entered.wait(3))
            try:
                other=ArtifactStore(TaskState(Repository(self.repo.path)),self.store.root,writable_roots=[self.outputs])
                with self.assertRaises(OSError):other.worker(self.event,self.outputs)
            finally:release.set()
            self.assertEqual(future.result(timeout=3),'committed')
        self.assertEqual(len(list(self.store.root.glob('*.part'))),0)

    def test_unknown_creation_and_unreadable_outcome_never_refund_budget(self):
        original_open,original_lstat=os.open,Path.lstat
        def ambiguous(path,flags,*args,**kwargs):
            fd=original_open(path,flags,*args,**kwargs)
            if str(path).endswith('.part') and flags & os.O_CREAT:
                os.close(fd)
                raise OSError(5,'creation outcome unknown')
            return fd
        def unreadable(path,*args,**kwargs):
            if str(path).endswith('.part'):raise OSError(5,'cannot inspect outcome')
            return original_lstat(path,*args,**kwargs)
        with patch('mediacenter.artifacts.os.open',side_effect=ambiguous),patch.object(Path,'lstat',unreadable):
            with self.assertRaises(OSError):self.store.worker(self.event,self.outputs)
        for _ in range(4):
            reopened=ArtifactStore(TaskState(Repository(self.repo.path)),self.store.root,writable_roots=[self.outputs])
            with self.assertRaisesRegex(TaskStateError,'publication_slot_creation_unknown'):
                reopened.worker(self.event,self.outputs)
        with self.repo._connect() as db:
            row=db.execute('SELECT copy_attempts,error_code FROM artifact_publications').fetchone()
        self.assertEqual(tuple(row),(1,'publication_slot_creation_unknown'))
        self.assertEqual(len(list(self.store.root.glob('*.part'))),1)
        self.assertEqual(self.fixture.count('task_artifacts'),0)
    def test_real_process_crash_at_every_publication_boundary_recovers_original(self):
        script="""import json,os,sys
from pathlib import Path
from mediacenter.repository import Repository
from mediacenter.task_state import TaskState
from mediacenter.artifacts import ArtifactStore
root=Path(sys.argv[1]);event=json.loads(sys.argv[2]);point=sys.argv[3]
store=ArtifactStore(TaskState(Repository(root/'state.db')),root/'sealed',writable_roots=[root/'scratch'],fault=lambda p:os._exit(81) if p==point else None)
store.worker(event,root/'scratch')
"""
        # Each cut uses a fresh independent DB and real non-graceful exit.
        for point in ("publication.intent","copy.block","publication.copied","publication.staged",
                      "publication.linked","publication.published","publication.commit","publication.committed"):
            if point!="publication.intent":
                self.tearDown();self.setUp()
            child=subprocess.Popen([sys.executable,"-B","-c",script,str(self.root),json.dumps(self.event),point])
            code=child.wait(timeout=15)
            print(json.dumps({"fixture":"publication-crash","point":point,"pid":child.pid,"exitcode":code}),flush=True)
            self.assertEqual(code,81)
            reopened=ArtifactStore(TaskState(Repository(self.repo.path)),self.store.root,writable_roots=[self.outputs])
            self.assertEqual(reopened.worker(self.event,self.outputs),"committed")
            with reopened.authorize(self.output()) as allowed:self.assertEqual(allowed.stream.read(),PNG)
            self.assertEqual(self.fixture.count("task_artifacts"),1)
            self.assertEqual(self.fixture.count("task_attempts"),1)
            self.assertEqual(list(self.store.root.glob('*.part')), [])

    def test_cancel_after_real_link_crash_cleans_only_owned_unsealed_names(self):
        script = """import json,os,sys
from pathlib import Path
from mediacenter.repository import Repository
from mediacenter.task_state import TaskState
from mediacenter.artifacts import ArtifactStore
root=Path(sys.argv[1]);event=json.loads(sys.argv[2])
store=ArtifactStore(TaskState(Repository(root/'state.db')),root/'sealed',writable_roots=[root/'scratch'],fault=lambda p:os._exit(82) if p=='publication.linked' else None)
store.worker(event,root/'scratch')
"""
        source = self.directory/'artifact.png'
        neighbor = self.store.root/'user-kept.png'; neighbor.write_bytes(b'user content')
        child = subprocess.Popen([sys.executable, '-B', '-c', script, str(self.root), json.dumps(self.event)])
        self.assertEqual(child.wait(timeout=15), 82)
        row = self.publication(); final = self.store.root/json.loads(row['descriptor_json'])['relative_path']
        temporary = self.store.root/row['temporary_path']
        self.assertTrue(final.is_file() and temporary.is_file())
        TaskState(Repository(self.repo.path)).cancel(self.task['id'])
        reopened = ArtifactStore(TaskState(Repository(self.repo.path)), self.store.root, writable_roots=[self.outputs])
        self.assertEqual(reopened.recover_cleanup(), {})
        self.assertFalse(final.exists()); self.assertFalse(temporary.exists())
        self.assertEqual(source.read_bytes(), PNG); self.assertEqual(neighbor.read_bytes(), b'user content')
        self.assertIsNone(self.repo.authorized_artifact(final.name))
        with self.repo._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM task_reservations WHERE kind='task' AND released=0").fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT exit_confirmed FROM task_attempts WHERE id=?', (self.command['attempt_id'],)).fetchone()[0], 0)
        self.tasks.confirm_exit(self.task['id'], self.command['attempt_id'], instance_id='instance-one',
                                epoch='epoch-one', evidence='cleanup-probe-confirmed-exit')
        self.assertEqual(reopened.recover_cleanup(), {})
        self.assertIsNone(self.publication()['temporary_path'])

    def test_real_crash_after_each_unlink_and_completion_marker_recovers(self):
        script = """import os,sys
from pathlib import Path
from mediacenter.repository import Repository
from mediacenter.task_state import TaskState
from mediacenter.artifacts import ArtifactStore
root=Path(sys.argv[1]);point=sys.argv[2]
store=ArtifactStore(TaskState(Repository(root/'state.db')),root/'sealed',writable_roots=[root/'scratch'],fault=lambda p:os._exit(83) if p==point else None)
store.recover_cleanup()
"""
        for index, point in enumerate(('cleanup.final_removed', 'cleanup.temporary_removed', 'cleanup.synced', 'cleanup.recorded')):
            if index:
                self.tearDown(); self.setUp()
            self.cut_at('publication.linked')
            row = self.publication(); final = self.store.root/json.loads(row['descriptor_json'])['relative_path']
            temporary = self.store.root/row['temporary_path']
            self.tasks.cancel(self.task['id'])
            child = subprocess.Popen([sys.executable, '-B', '-c', script, str(self.root), point])
            self.assertEqual(child.wait(timeout=15), 83)
            reopened = ArtifactStore(TaskState(Repository(self.repo.path)), self.store.root, writable_roots=[self.outputs])
            self.assertEqual(reopened.recover_cleanup(), {})
            self.assertFalse(final.exists()); self.assertFalse(temporary.exists())
            self.assertEqual((self.directory/'artifact.png').read_bytes(), PNG)
            self.assertIsNone(self.publication()['temporary_path'])

    def test_committed_recovery_keeps_authorized_final_without_worker_source(self):
        self.cut_at('publication.committed')
        final = self.store.root/self.output()
        source = self.directory/'artifact.png'
        source.rename(self.directory/'preserved-worker-original.png')
        reopened = ArtifactStore(TaskState(Repository(self.repo.path)), self.store.root)
        self.assertEqual(reopened.recover_cleanup(), {})
        self.assertEqual(final.read_bytes(), PNG)
        with reopened.authorize(final.name) as allowed:
            self.assertEqual(allowed.stream.read(), PNG)
        self.assertEqual(list(self.store.root.glob('*.part')), [])

    def test_cleanup_refuses_replaced_final_and_keeps_surviving_committed_inode(self):
        self.cut_at('publication.committed')
        row = self.publication(); final = self.store.root/self.output()
        saved = final.with_suffix('.preserved'); final.rename(saved)
        final.write_bytes(b'not the publication inode')
        self.assertIn('publication_cleanup_object_changed', self.store.recover_cleanup().values())
        self.assertEqual(final.read_bytes(), b'not the publication inode')
        self.assertEqual(saved.read_bytes(), PNG)
        self.assertEqual((self.store.root/row['temporary_path']).read_bytes(), PNG)

    def test_missing_committed_final_preserves_the_only_remaining_name(self):
        self.cut_at('publication.committed')
        row = self.publication(); final = self.store.root/self.output()
        final.rename(final.with_suffix('.preserved'))
        self.assertIn('publication_cleanup_io_error', self.store.recover_cleanup().values())
        self.assertEqual((self.store.root/row['temporary_path']).read_bytes(), PNG)

    @unittest.skipUnless(os.name == 'posix', 'POSIX symlink boundary')
    def test_cleanup_does_not_follow_symlink_replacement(self):
        self.cut_at('publication.staged')
        row = self.publication(); temporary = self.store.root/row['temporary_path']
        temporary.rename(temporary.with_suffix('.preserved'))
        outside = self.root/'outside.png'; outside.write_bytes(PNG)
        temporary.symlink_to(outside)
        self.tasks.cancel(self.task['id'])
        self.assertIn('publication_cleanup_object_changed', self.store.recover_cleanup().values())
        self.assertTrue(temporary.is_symlink()); self.assertEqual(outside.read_bytes(), PNG)

    def seed_bad_cleanup_page(self):
        # Synthetic ledger rows exercise pagination, not real inference or GPU.
        plans = []
        for index in range(101):
            task_id, attempt_id, asset_id = f'cleanup-task-{index}', f'cleanup-attempt-{index}', f'cleanup-asset-{index}'
            pub_id = 'pub_' + digest([task_id, attempt_id, asset_id, 'v1'])
            plans.append((pub_id, task_id, attempt_id, asset_id))
        plans.sort()
        valid_id = plans[-1][0]
        owned = self.store.root/(valid_id + '.' + 'a'*32 + '.part'); owned.write_bytes(PNG)
        with self.repo._connect() as db:
            original = dict(db.execute('SELECT * FROM tasks WHERE id=?', (self.task['id'],)).fetchone())
            for index, (pub_id, task_id, attempt_id, asset_id) in enumerate(plans):
                values = {**original, 'id': task_id, 'current_attempt_id': attempt_id}
                db.execute('INSERT INTO tasks ('+','.join(values)+') VALUES ('+','.join('?' for _ in values)+')', tuple(values.values()))
                db.execute("INSERT INTO task_attempts(id,task_id,generation,mode,status,created_at,updated_at) VALUES(?,?,1,'local','canceled','fixture','fixture')", (attempt_id,task_id))
                value = {'event': {'task_id':task_id,'attempt_id':attempt_id},
                    'manifest': {'asset_id':asset_id,'revision':'v1','media_type':'image/png'},
                    'root_identity':self.store.root_identity,'relative_path':pub_id+'.png'}
                db.execute("INSERT INTO artifact_publications(publication_id,task_id,attempt_id,asset_id,revision,descriptor_json,descriptor_digest,phase,temporary_path,object_json) VALUES(?,?,?,?,?,?,?,'canceled',?,?)",
                    (pub_id, task_id, attempt_id, asset_id, 'v1', '{' if index < 50 else canonical(value), digest(value),
                     owned.name if pub_id == valid_id else '../not-owned', canonical(identity(owned.stat()))))
        return owned,valid_id

    def test_cleanup_pages_past_a_full_page_of_bad_records(self):
        owned,valid_id=self.seed_bad_cleanup_page()
        errors = self.store.recover_cleanup()
        self.assertEqual(len(errors), 100); self.assertTrue(owned.exists())
        self.assertEqual(set(errors.values()), {'publication_cleanup_path_invalid', 'publication_cleanup_integrity_error'})
        self.assertEqual(self.store.recover_cleanup(), {})
        self.assertFalse(owned.exists())
        with self.repo._connect() as db:
            self.assertIsNone(db.execute('SELECT temporary_path FROM artifact_publications WHERE publication_id=?', (valid_id,)).fetchone()[0])

    def test_stopped_small_cleanup_page_never_skips_unattempted_rows(self):
        owned,valid_id=self.seed_bad_cleanup_page()
        stop=threading.Event(); seen=[]
        original=self.store._resume
        def stop_after_first(publication_id):
            seen.append(publication_id)
            try: return original(publication_id)
            finally: stop.set()
        with patch.object(self.store,'_resume',side_effect=stop_after_first):
            errors=self.store.recover_cleanup(limit=8,stop_requested=stop.is_set)
        self.assertEqual(len(errors),1)
        self.assertEqual(self.store._cleanup_cursor,seen[0])
        self.assertEqual(self.store.recover_cleanup(limit=8,stop_requested=stop.is_set),{})
        self.assertEqual(self.store._cleanup_cursor,seen[0])
        all_errors=set(errors)
        for _ in range(13):
            all_errors.update(self.store.recover_cleanup(limit=8))
            if not owned.exists(): break
        self.assertFalse(owned.exists())
        self.assertEqual(len(all_errors),100)
        with self.repo._connect() as db:
            self.assertIsNone(db.execute('SELECT temporary_path FROM artifact_publications WHERE publication_id=?',(valid_id,)).fetchone()[0])

    def test_local_recovery_stops_between_items_and_resumes_exact_cursor(self):
        self.seed_bad_cleanup_page()
        with self.repo._connect() as db:
            db.execute("UPDATE artifact_publications SET phase='intent'")
        stopped=threading.Event(); visited=[]
        def attempt(publication_id):
            visited.append(publication_id); stopped.set()
        with patch.object(self.store,'_resume',side_effect=attempt):
            self.store.recover_local(limit=8,stop_requested=stopped.is_set)
        self.assertEqual(len(visited),1)
        self.assertEqual(self.store._local_cursor,visited[0])
        with patch.object(self.store,'_resume',side_effect=visited.append):
            self.store.recover_local(limit=8)
        self.assertEqual(len(visited),9)
        self.assertEqual(visited,sorted(set(visited)))

    def test_recovery_rejects_unbounded_or_invalid_page_limits(self):
        for value in (0,101,True,'8'):
            with self.subTest(limit=value):
                with self.assertRaisesRegex(ValueError,'publication_recovery_limit_invalid'):
                    self.store.recover_cleanup(limit=value)
                with self.assertRaisesRegex(ValueError,'publication_recovery_limit_invalid'):
                    self.store.recover_local(limit=value)

    def test_concurrent_cleanup_defers_while_publisher_owns_lock_and_cancel_wins(self):
        entered,release=threading.Event(),threading.Event()
        results,failures=[],[]
        def hold_link(point):
            if point=='publication.linked':
                entered.set()
                if not release.wait(15): raise RuntimeError('publication barrier timeout')
        self.store.fault=hold_link
        def publish():
            try: results.append(self.store.worker(self.event,self.outputs))
            except BaseException as exc: failures.append(exc)
        publisher=threading.Thread(target=publish)
        publisher.start()
        try:
            self.assertTrue(entered.wait(8))
            row=self.publication()
            final=self.store.root/json.loads(row['descriptor_json'])['relative_path']
            temporary=self.store.root/row['temporary_path']
            self.tasks.cancel(self.task['id'])
            errors=self.store.recover_cleanup(limit=8)
            self.assertIn(row['publication_id'],errors)
            self.assertTrue(publisher.is_alive())
            self.assertTrue(final.is_file() and temporary.is_file())
            self.assertIsNone(self.repo.authorized_artifact(final.name))
            with self.repo._connect() as db:
                self.assertEqual(db.execute('SELECT exit_confirmed FROM task_attempts WHERE id=?',(self.command['attempt_id'],)).fetchone()[0],0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM task_reservations WHERE kind='task' AND released=0").fetchone()[0],2)
        finally:
            release.set(); publisher.join(timeout=8)
        self.assertFalse(publisher.is_alive())
        self.assertEqual(failures,[]); self.assertEqual(results,['canceled'])
        self.assertEqual(self.store.recover_cleanup(),{})
        self.assertFalse(final.exists()); self.assertFalse(temporary.exists())
        self.assertEqual((self.directory/'artifact.png').read_bytes(),PNG)
        with self.repo._connect() as db:
            self.assertEqual(db.execute('SELECT exit_confirmed FROM task_attempts WHERE id=?',(self.command['attempt_id'],)).fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM task_reservations WHERE kind='task' AND released=0").fetchone()[0],2)

    def test_cleanup_refuses_replaced_temporary_and_unsafe_ledger_path(self):
        self.cut_at('publication.staged')
        row = self.publication(); temporary = self.store.root/row['temporary_path']
        saved = temporary.with_suffix('.preserved'); temporary.rename(saved)
        temporary.write_bytes(b'new user object')
        self.tasks.cancel(self.task['id'])
        self.assertIn('publication_cleanup_object_changed', self.store.recover_cleanup().values())
        self.assertEqual(temporary.read_bytes(), b'new user object'); self.assertEqual(saved.read_bytes(), PNG)
        outside = self.root/'not-owned.png'; outside.write_bytes(PNG)
        with self.repo._connect() as db:
            db.execute('UPDATE artifact_publications SET temporary_path=?', ('../not-owned.png',))
        self.assertIn('publication_cleanup_path_invalid', self.store.recover_cleanup().values())
        self.assertEqual(outside.read_bytes(), PNG)

    def test_canceled_permanent_copy_error_can_cleanup_without_recopied_source(self):
        (self.directory/'artifact.png').write_bytes(PNG[:-1]+b'x')
        with self.assertRaisesRegex(TaskStateError, 'artifact_content_changed'):
            self.store.worker(self.event, self.outputs)
        self.tasks.cancel(self.task['id'])
        with patch('mediacenter.artifacts.copy_snapshot', side_effect=AssertionError('must not copy after cancel')):
            self.assertEqual(self.store.recover_cleanup(), {})
        self.assertEqual(list(self.store.root.glob('*.part')), [])
        self.assertEqual(self.publication()['phase'], 'canceled')

    def test_cleanup_never_deletes_bytes_referenced_by_a_seal(self):
        self.cut_at('publication.committed')
        final = self.store.root/self.output()
        with self.repo._connect() as db:
            db.execute("UPDATE artifact_publications SET phase='canceled'")
        self.assertIn('publication_cleanup_authorized_conflict', self.store.recover_cleanup().values())
        with self.store.authorize(final.name) as allowed:
            self.assertEqual(allowed.stream.read(), PNG)
        self.assertEqual(len(list(self.store.root.glob('*.part'))), 1)
    def test_authorized_fd_survives_path_replacement_and_has_fixed_size(self):
        self.store.worker(self.event,self.outputs)
        allowed=self.store.authorize(self.output())
        try:
            target=self.store.root/self.output()
            if os.name=="posix":
                target.rename(target.with_suffix(".old"));target.write_bytes(b"replacement")
            self.assertEqual(allowed.stream.read(allowed.size),PNG)
        finally:allowed.close()
        self.assertTrue(allowed.stream.closed)


if __name__=="__main__":unittest.main()
