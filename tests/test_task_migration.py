from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from mediacenter.repository import Repository, _startup_file
from mediacenter.task_state import TaskState, TaskStateError, RUNTIME_SCHEMA_V2, SCHEMA_VERSION
from scripts.migrate_task_kernel import migrate, rollback


def remove_schema5_fixture(db):
    """Test-only downgrade of the fresh empty schema5+ additions."""
    policy_columns = {row[1] for row in db.execute("PRAGMA table_info(instance_policies)")}
    for column in ("configuration_error", "resume_after_apply", "pending_restart_recovery",
                   "pending_policy_digest", "pending_policy_json", "configuration_state"):
        if column in policy_columns:
            db.execute(f"ALTER TABLE instance_policies DROP COLUMN {column}")
    if "restart_recovery" in {row[1] for row in db.execute("PRAGMA table_info(instance_policies)")}:
        db.execute("ALTER TABLE instance_policies DROP COLUMN restart_recovery")
    db.execute('DROP TABLE runtime_lora_permits')
    db.execute('ALTER TABLE tasks DROP COLUMN lora_bindings_json')
    for table in ('runtime_validation_records', 'runtime_boundaries', 'runtime_epoch_packages',
                   'instance_installation_bindings', 'runtime_image_bindings', 'runtime_image_transfers', 'runtime_release_records'):
        db.execute('DROP TABLE ' + table)
    db.execute('DROP TABLE runtime_container_removals')
    if db.execute('SELECT COUNT(*) FROM runtime_intents').fetchone()[0]:
        raise AssertionError('fixture downgrade requires empty runtime tables')
    db.execute('DROP TABLE runtime_effects'); db.execute('DROP TABLE runtime_intents')
    for statement in RUNTIME_SCHEMA_V2.split(';'):
        if statement.strip(): db.execute(statement)


def schema_v1(path):
    """MC030 schema shape, including its persistent WAL journal mode."""
    Repository(path)
    with sqlite3.connect(path) as db:
        remove_schema5_fixture(db)
        db.execute("DROP TABLE artifact_publications")
        db.executescript("DROP TABLE instance_cancel_deadlines; DROP TABLE instance_inbox; DROP TABLE model_operations; DROP TABLE instance_claims; DROP TABLE instance_policies; DROP TABLE runtime_effects; DROP TABLE runtime_intents; DROP TABLE task_kernel_metadata;"
                         "CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=1));"
                         "INSERT INTO task_kernel_metadata VALUES(1);")
        db.execute("INSERT INTO audit_events(occurred_at,action,target,detail_json) VALUES('fixture','v1','task','{}')")
    db.close()


def table_snapshot(path):
    db = sqlite3.connect(Path(path).as_uri() + "?immutable=1", uri=True)
    try:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {name: (db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0],
                       db.execute('SELECT * FROM "' + name + '" ORDER BY rowid').fetchall()) for name in tables}
    finally:
        db.close()


def legacy_database(path):
    db = sqlite3.connect(path)
    try:
        db.executescript("""CREATE TABLE services(kind TEXT PRIMARY KEY,name TEXT,description TEXT,enabled INTEGER,timeout_seconds INTEGER);
        INSERT INTO services VALUES('image','image','legacy',1,900);
        CREATE TABLE tasks(id TEXT PRIMARY KEY,service TEXT,model_key TEXT,status TEXT,prompt TEXT,options_json TEXT,
          output_json TEXT,created_at TEXT,updated_at TEXT);
        CREATE TABLE audit_events(id INTEGER PRIMARY KEY,occurred_at TEXT,action TEXT,target TEXT,detail_json TEXT);
        INSERT INTO tasks VALUES('old-running','image','sdxl-base-1.0','running','legacy','{}',NULL,'2020','2020');
        INSERT INTO tasks VALUES('old-success','image','sdxl-base-1.0','succeeded','legacy','{}',
          '{"artifact_url":"/api/v1/artifacts/image/legacy.png","bytes":4}','2020','2020');""")
        db.commit()
    finally:
        db.close()


class TaskMigrationTests(unittest.TestCase):
    def assert_task_columns_preserved(self, before, after):
        self.assertEqual(after[0], before[0][:-1] + ", lora_bindings_json TEXT NOT NULL DEFAULT '[]')")
        self.assertEqual([row[:-1] for row in after[1]], before[1])
        self.assertTrue(all(row[-1] == '[]' for row in after[1]))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "legacy.db"
        legacy_database(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def test_startup_rejects_old_schema_before_any_mutation(self):
        before = self.source.read_bytes()
        with self.assertRaisesRegex(TaskStateError, "task_schema_migration_required"):
            Repository(self.source)
        self.assertEqual(before, self.source.read_bytes())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["legacy.db"])

    def test_migration_rollback_preserve_uncertain_running_and_media_bytes(self):
        model, artifact = self.root / "model.bin", self.root / "legacy.png"
        model.write_bytes(b"weights-do-not-touch"); artifact.write_bytes(b"data")
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.source, model, artifact)}
        output, backup = self.root / "new.db", self.root / "backup.db"
        report = migrate(self.source, output, backup, offline_confirmed=True)
        repository = Repository(output)
        state = TaskState(repository)
        running = repository.get_task("old-running")
        self.assertEqual((running["status"], running["current_attempt_id"], running["migration_source"]), ("running", None, "legacy-v0"))
        self.assertEqual(repository.get_task("old-success")["stage"], "completed")
        self.assertIsNotNone(repository.authorized_artifact("image/legacy.png"))
        from mediacenter.artifacts import ArtifactStore
        media=self.root/'media';(media/'image').mkdir(parents=True)
        (media/'image'/'legacy.png').write_bytes(artifact.read_bytes())
        with ArtifactStore(state,media).authorize('image/legacy.png') as allowed:
            self.assertEqual(allowed.origin,'legacy-readonly')
            self.assertIsNone(allowed.sha256)
            self.assertEqual(allowed.stream.read(),b'data')
        self.assertTrue(allowed.stream.closed)
        with self.assertRaisesRegex(TaskStateError, "legacy_execution_unreconciled"):
            state.cancel("old-running")
        with self.assertRaisesRegex(TaskStateError, "legacy_execution_unreconciled"):
            state.retry("old-success", 1)
        restored = self.root / "restored.db"
        rollback(str(output) + ".receipt.json", restored, offline_confirmed=True)
        self.assertEqual(restored.read_bytes(), backup.read_bytes())
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.source, model, artifact)})
        self.assertFalse(report["media_modified"])

    def test_no_confirmation_existing_output_or_foreign_backup_is_replaced(self):
        output, backup = self.root / "new.db", self.root / "backup.db"
        with self.assertRaisesRegex(TaskStateError, "offline_confirmation_required"):
            migrate(self.source, output, backup)
        self.assertFalse(output.exists())
        migrate(self.source, output, backup, offline_confirmed=True)
        before = output.read_bytes()
        with self.assertRaises(TaskStateError):
            migrate(self.source, output, backup, offline_confirmed=True)
        self.assertEqual(output.read_bytes(), before)
        with backup.open("ab") as stream:
            stream.write(b"foreign")
        with self.assertRaisesRegex(TaskStateError, "migration_backup_changed"):
            rollback(str(output) + ".receipt.json", self.root / "restore.db", offline_confirmed=True)
        self.assertFalse((self.root / "restore.db").exists())

    def test_v1_wal_mode_without_sidecars_startup_is_byte_and_directory_readonly(self):
        source = self.root / "v1.db"; schema_v1(source)
        self.assertFalse(Path(str(source) + "-wal").exists())
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        with self.assertRaisesRegex(TaskStateError, "task_schema_unsupported"):
            Repository(source)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_live_wal_latest_v1_rejected_without_source_or_shm_writes(self):
        source = self.root / "v1wal.db"; schema_v1(source)
        connection = sqlite3.connect(source)
        try:
            connection.execute("INSERT INTO audit_events(occurred_at,action,target,detail_json) VALUES('wal','latest','task','{}')")
            connection.commit()
            before = {p.name: p.read_bytes() for p in self.root.iterdir()}
            with self.assertRaisesRegex(TaskStateError, "task_schema_unsupported"):
                Repository(source)
            self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        finally:
            connection.close()

    def test_nonempty_wal_is_not_ignored_for_schema_version(self):
        source = self.root / "version-in-wal.db"; Repository(source)
        connection = sqlite3.connect(source)
        try:
            connection.executescript("DROP TABLE task_kernel_metadata; CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=1)); INSERT INTO task_kernel_metadata VALUES(1);")
            connection.commit()
            before = {p.name: p.read_bytes() for p in self.root.iterdir()}
            with self.assertRaisesRegex(TaskStateError, "task_schema_unsupported"):
                Repository(source)
            self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        finally:
            connection.close()

    def test_v1_upgrade_preserves_every_existing_table_and_exact_cli_rollback(self):
        source = self.root / "v1.db"; schema_v1(source)
        before_bytes, before_tables = source.read_bytes(), table_snapshot(source)
        output, backup = self.root / "v2.db", self.root / "v1-backup.db"
        report = migrate(source, output, backup, offline_confirmed=True)
        after = table_snapshot(output)
        self.assertEqual(report["schema"], SCHEMA_VERSION)
        self.assertFalse(report["container_ownership_inferred"])
        for name, content in before_tables.items():
            if name == 'tasks': self.assert_task_columns_preserved(content, after[name])
            elif name == "installation_resources":
                self.assertEqual(after[name][1], content[1])
            elif name != "task_kernel_metadata":
                self.assertEqual(after[name], content, name)
        self.assertEqual(after["runtime_intents"][1], [])
        self.assertEqual(after["runtime_effects"][1], [])
        self.assertEqual(source.read_bytes(), before_bytes)
        restored = self.root / "restored.db"
        result = subprocess.run([sys.executable, "-B", "scripts/migrate_task_kernel.py", "rollback", "--receipt",
                                 str(output) + ".receipt.json", "--output", str(restored), "--offline-confirmed"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(restored.read_bytes(), backup.read_bytes())

    def test_v1_upgrade_transaction_failure_retains_v1_and_no_partial_runtime_schema(self):
        source = self.root / "v1.db"; schema_v1(source)
        for index, fail_at in enumerate(("migration.runtime_schema", "migration.before_commit")):
            output, backup = self.root / f"v2-{index}.db", self.root / f"backup-{index}.db"
            def fault(point):
                if point == fail_at:
                    raise RuntimeError(point)
            with self.assertRaises(RuntimeError):
                migrate(source, output, backup, offline_confirmed=True, fault=fault)
            self.assertEqual(table_snapshot(source), table_snapshot(output))
            self.assertFalse(Path(str(output) + ".receipt.json").exists())

    def test_unstable_or_oversize_schema_source_has_bounded_fail_closed_probe(self):
        source = self.root / "v1.db"; schema_v1(source)
        with patch("mediacenter.repository._startup_file", side_effect=TaskStateError("task_schema_probe_source_changed")) as probe:
            with self.assertRaisesRegex(TaskStateError, "task_schema_probe_source_changed"):
                Repository(source)
            self.assertEqual(probe.call_count, 5)
        with patch("mediacenter.repository._startup_file", side_effect=TaskStateError("task_schema_probe_limit_exceeded")) as probe:
            with self.assertRaisesRegex(TaskStateError, "task_schema_probe_limit_exceeded"):
                Repository(source)
            self.assertEqual(probe.call_count, 1)

    def test_hash_budget_cannot_be_bypassed_by_growth_after_initial_stat(self):
        file = self.root / "growing.db"; file.write_bytes(b"12345")
        info = list(file.stat()); info[6] = 4
        with patch.object(Path, "lstat", return_value=os.stat_result(info)):
            with self.assertRaisesRegex(TaskStateError, "task_schema_probe_limit_exceeded"):
                _startup_file(file, 4)

    def test_populated_v2_upgrade_preserves_unknown_execution_and_single_ledger(self):
        from tests.test_task_state import TaskStateTests
        from mediacenter.instance_policy import InstancePolicy
        fixture = TaskStateTests(); fixture.setUp()
        try:
            task, command = fixture.dispatched()
            fixture.state.receive(fixture.event(command))
            fixture.state.cancel(task["id"])
            source = fixture.repository.path
            with sqlite3.connect(source) as db:
                remove_schema5_fixture(db)
                db.execute("DROP TABLE artifact_publications")
                db.executescript("DROP TABLE instance_cancel_deadlines; DROP TABLE instance_inbox; DROP TABLE model_operations; DROP TABLE instance_claims; DROP TABLE instance_policies; DROP TABLE task_kernel_metadata; CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=2)); INSERT INTO task_kernel_metadata VALUES(2);")
            db.close()
            before = table_snapshot(source)
            output, backup = self.root / "v3-populated.db", self.root / "v2-backup.db"
            migrate(source, output, backup, offline_confirmed=True)
            after = table_snapshot(output)
            for name, contents in before.items():
                if name == 'tasks': self.assert_task_columns_preserved(contents, after[name])
                elif name == 'installation_resources': self.assertEqual(after[name][1], contents[1])
                elif name not in {"task_kernel_metadata", "runtime_intents"}: self.assertEqual(after[name], contents, name)
            repository = Repository(output)
            self.assertEqual(repository.get_task(task["id"])["status"], "cancel_requested")
            with repository._connect() as db:
                self.assertTrue(InstancePolicy.legacy_unreconciled(db))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM instance_claims").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT SUM(released) FROM task_reservations").fetchone()[0], 0)
        finally:
            fixture.tearDown()

    def test_nonempty_v3_to_v4_preserves_every_old_row_and_rollback(self):
        from tests.test_task_state import TaskStateTests
        fixture=TaskStateTests();fixture.setUp()
        try:
            task,command=fixture.dispatched()
            fixture.state.receive(fixture.event(command));fixture.state.cancel(task["id"])
            source=fixture.repository.path
            with sqlite3.connect(source) as db:
                remove_schema5_fixture(db)
                db.executescript("DROP TABLE artifact_publications; DROP TABLE task_kernel_metadata; CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=3)); INSERT INTO task_kernel_metadata VALUES(3);")
            db.close()
            before=table_snapshot(source);raw=source.read_bytes()
            for table in ("instance_policies","instance_claims","model_operations","instance_inbox","instance_cancel_deadlines","task_inbox","task_outbox","task_reservations"):
                self.assertTrue(before[table][1],table)
            with self.assertRaisesRegex(TaskStateError,"task_schema_unsupported"):Repository(source)
            self.assertEqual(source.read_bytes(),raw)
            for fail in (True,False):
                output=self.root/('fault.db' if fail else 'v4.db');backup=self.root/('fault.backup' if fail else 'v3.backup')
                def fault(point):
                    if fail and point=="migration.before_commit":raise RuntimeError(point)
                if fail:
                    with self.assertRaises(RuntimeError):migrate(source,output,backup,offline_confirmed=True,fault=fault)
                    self.assertEqual(table_snapshot(output),before)
                else:
                    report=migrate(source,output,backup,offline_confirmed=True)
                    self.assertEqual((report['source_schema'],report['schema']),(3,SCHEMA_VERSION))
                    after=table_snapshot(output)
                    self.assertEqual(after['artifact_publications'][1],[])
                    for name,rows in before.items():
                        if name == 'tasks': self.assert_task_columns_preserved(rows, after[name])
                        elif name == 'instance_policies':
                            self.assertEqual([row[:-7] for row in after[name][1]], rows[1])
                            self.assertTrue(all(row[-7:] == (0, 'applied', None, None, None, 0, None)
                                                for row in after[name][1]))
                        elif name == 'installation_resources':self.assertEqual(after[name][1],rows[1])
                        elif name not in {'task_kernel_metadata','runtime_intents'}:self.assertEqual(after[name],rows,name)
                    restored=self.root/'restored-v3.db'
                    rollback(str(output)+'.receipt.json',restored,offline_confirmed=True)
                    self.assertEqual(restored.read_bytes(),backup.read_bytes())
            self.assertEqual(source.read_bytes(),raw)
        finally:fixture.tearDown()

    def test_schema4_unknown_effect_hashes_survive_generation_migration_and_rollback(self):
        from tests.test_runtime_controller import RuntimeControllerTests
        from mediacenter.runtime_controller import RuntimeController
        fixture = RuntimeControllerTests(); fixture.setUp()
        try:
            row = fixture.prepared()
            row = fixture.controller.create_domain(row['intent_id'], row['version'])
            def crash(point):
                if point == 'create.after_intent_commit': raise RuntimeError('fixture process-loss boundary')
            fixture.controller.fault = crash
            with self.assertRaises(RuntimeError): fixture.controller.create(row['intent_id'], row['version'])
            source = fixture.repository.path
            with sqlite3.connect(source) as db:
                db.row_factory = sqlite3.Row
                old = dict(db.execute('SELECT * FROM runtime_intents').fetchone())
                for key in ('generation', 'identity_version', 'claim_id'): old.pop(key)
                old['intent_digest'] = RuntimeController._intent_digest(old)
                effects = [dict(r) for r in db.execute('SELECT * FROM runtime_effects')]
                for effect in effects: effect['request_digest'] = old['intent_digest']
                db.execute('DELETE FROM runtime_effects'); db.execute('DELETE FROM runtime_intents')
                remove_schema5_fixture(db)
                db.execute('INSERT INTO runtime_intents(' + ','.join(old) + ') VALUES(' + ','.join('?' for _ in old) + ')', tuple(old.values()))
                for effect in effects:
                    db.execute('INSERT INTO runtime_effects VALUES(' + ','.join('?' for _ in effect) + ')', tuple(effect.values()))
                db.execute('DROP TABLE task_kernel_metadata')
                db.execute('CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=4))')
                db.execute('INSERT INTO task_kernel_metadata VALUES(4)')
            db.close()
            before, raw = table_snapshot(source), source.read_bytes()
            output, backup = self.root / 'five.db', self.root / 'four.backup'
            migrate(source, output, backup, offline_confirmed=True)
            with sqlite3.connect(output) as db:
                db.row_factory = sqlite3.Row
                migrated = dict(db.execute('SELECT * FROM runtime_intents').fetchone())
                self.assertEqual({key: migrated[key] for key in old}, old)
                self.assertEqual(migrated['generation'], old['desired_revision'])
                self.assertEqual((migrated['identity_version'], migrated['claim_id']), (1, None))
                self.assertEqual([dict(r) for r in db.execute('SELECT * FROM runtime_effects')], effects)
                self.assertEqual(migrated['state'], 'create_pending')
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            db.close()
            after = table_snapshot(output)
            for name, value in before.items():
                if name == 'tasks': self.assert_task_columns_preserved(value, after[name])
                elif name == 'instance_policies':
                    self.assertEqual([row[:-7] for row in after[name][1]], value[1])
                    self.assertTrue(all(row[-7:] == (0, 'applied', None, None, None, 0, None)
                                        for row in after[name][1]))
                elif name not in {'runtime_intents', 'task_kernel_metadata', 'installation_resources'}:
                    self.assertEqual(after[name], value, name)
            restored = self.root / 'four.restored'
            rollback(str(output) + '.receipt.json', restored, offline_confirmed=True)
            self.assertEqual(restored.read_bytes(), backup.read_bytes())
            self.assertEqual(source.read_bytes(), raw)
        finally: fixture.tearDown()

    def test_schema6_to_9_adds_lifecycle_and_deployment_binding_without_rewriting_evidence(self):
        from mediacenter.instance_policy import InstancePolicy
        from tests.test_resident_policy import (fixture_capacity, package_identity, policy_value,
                                                seed_deployment)
        source = self.root / "schema6.db"
        repository = Repository(source)
        binding = seed_deployment(repository)
        authority = InstancePolicy(repository)
        value = policy_value(binding)
        authority.configure("instance-one", value)
        row = authority.desire("instance-one", "loaded")
        package = package_identity(value)
        authority.package_validator = lambda _db, record_id: package if record_id == package["runtime_record_id"] else None
        authority.claim_container("instance-one", "epoch-one", expected_version=row["version"],
                             backend="container", limits={"GPU-one": 100, "GPU-two": 100},
                             package_identity=package)
        with sqlite3.connect(source) as db:
            db.row_factory = sqlite3.Row
            policy_before = dict(db.execute("SELECT * FROM instance_policies").fetchone())
            for column in ("restart_recovery", "configuration_state", "pending_policy_json",
                           "pending_policy_digest", "pending_restart_recovery",
                           "resume_after_apply", "configuration_error"):
                policy_before.pop(column)
            claim_before = dict(db.execute("SELECT * FROM instance_claims").fetchone())
            for column in ("configuration_error", "resume_after_apply", "pending_restart_recovery",
                           "pending_policy_digest", "pending_policy_json", "configuration_state",
                           "restart_recovery"):
                db.execute(f"ALTER TABLE instance_policies DROP COLUMN {column}")
            db.execute("DROP TABLE runtime_container_removals")
            db.execute("DROP TABLE task_kernel_metadata")
            db.execute("CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=6))")
            db.execute("INSERT INTO task_kernel_metadata VALUES(6)")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("PRAGMA journal_mode=DELETE")
        db.close()
        raw = source.read_bytes()
        output, backup = self.root / "schema8.db", self.root / "schema6.backup"
        report = migrate(source, output, backup, offline_confirmed=True)
        self.assertEqual((report["source_schema"], report["schema"]), (6, SCHEMA_VERSION))
        with sqlite3.connect(output) as db:
            db.row_factory = sqlite3.Row
            policy_after = dict(db.execute("SELECT * FROM instance_policies").fetchone())
            expected = {"restart_recovery": 0, "configuration_state": "applied",
                        "pending_policy_json": None, "pending_policy_digest": None,
                        "pending_restart_recovery": None, "resume_after_apply": 0,
                        "configuration_error": None}
            self.assertEqual({column: policy_after.pop(column) for column in expected}, expected)
            self.assertEqual(policy_after, policy_before)
            self.assertEqual(dict(db.execute("SELECT * FROM instance_claims").fetchone()), claim_before)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        db.close()
        restored = self.root / "schema6.restored"
        rollback(str(output) + ".receipt.json", restored, offline_confirmed=True)
        self.assertEqual(restored.read_bytes(), backup.read_bytes())
        self.assertEqual(source.read_bytes(), raw)

    def test_schema8_to_9_adds_null_deployment_revision_without_inference(self):
        source = self.root / "schema8.db"
        repository = Repository(source)
        from mediacenter.task_state import TaskState
        task = TaskState(repository).accept(
            {"service": "image", "model": "legacy-model", "prompt": "legacy",
             "options": {}, "inputs": []},
            scope="migration-test",
        )[0]
        with sqlite3.connect(source) as db:
            before = tuple(db.execute(
                "SELECT id,service,model_key,status,prompt,request_digest FROM tasks WHERE id=?",
                (task["id"],),
            ).fetchone())
            db.execute("ALTER TABLE tasks DROP COLUMN deployment_config_revision")
            db.execute("ALTER TABLE tasks DROP COLUMN deployment_id")
            db.execute("DROP TABLE task_kernel_metadata")
            db.execute(
                "CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=8))")
            db.execute("INSERT INTO task_kernel_metadata VALUES(8)")
            db.commit()
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("PRAGMA journal_mode=DELETE")
        output, backup = self.root / "schema9.db", self.root / "schema8.backup"
        db.close()
        report = migrate(source, output, backup, offline_confirmed=True)
        self.assertEqual((report["source_schema"], report["schema"]), (8, SCHEMA_VERSION))
        with sqlite3.connect(output) as db:
            after = tuple(db.execute(
                "SELECT id,service,model_key,status,prompt,request_digest FROM tasks WHERE id=?",
                (task["id"],),
            ).fetchone())
            binding = db.execute(
                "SELECT deployment_id,deployment_config_revision FROM tasks WHERE id=?",
                (task["id"],),
            ).fetchone()
        self.assertEqual(after, before)
        self.assertEqual(binding, (None, None))
        db.close()

    def test_schema10_private_configuration_context_copy_and_rollback(self):
        source = self.root / 'schema10-config.db'
        Repository(source)
        with sqlite3.connect(source) as db:
            db.execute("INSERT INTO deployment_operations(id,server_profile_id,authenticated_principal,route,"
                       "idempotency_key,request_digest,deployment_id,state,payload_json,plan_digest,"
                       "confirmations_json,milestone,created_at,updated_at) "
                       "VALUES('dop-old','server','admin','route','key','digest','instance','ready','{}','plan','{}','ready','then','then')")
            db.execute("INSERT INTO runtime_lora_permits VALUES('scoped','lora','r1','scope','{}',1)")
            for column in ('configuration_context_json', 'configuration_context_digest'):
                db.execute('ALTER TABLE deployment_operations DROP COLUMN '+column)
            columns = [row[1] for row in db.execute('PRAGMA table_info(deployment_operations)')]
            previous = tuple(db.execute('SELECT * FROM deployment_operations').fetchone())
            permits = tuple(db.execute('SELECT * FROM runtime_lora_permits').fetchone())
            db.execute('DROP TABLE task_kernel_metadata')
            db.execute('CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=10))')
            db.execute('INSERT INTO task_kernel_metadata VALUES(10)')
            db.commit()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute('PRAGMA journal_mode=DELETE')
        db.close()
        source_bytes = source.read_bytes()
        output, backup = self.root/'schema11-config.db', self.root/'schema10-config.backup'
        report = migrate(source, output, backup, offline_confirmed=True)
        self.assertEqual((report['source_schema'], report['schema']), (10, SCHEMA_VERSION))
        with sqlite3.connect(output) as db:
            self.assertEqual(tuple(db.execute('SELECT '+','.join(columns)+' FROM deployment_operations').fetchone()), previous)
            self.assertEqual(db.execute('SELECT configuration_context_json,configuration_context_digest FROM deployment_operations').fetchone(), (None, None))
            self.assertEqual(tuple(db.execute('SELECT * FROM runtime_lora_permits').fetchone()), permits)
        db.close()
        self.assertEqual(source.read_bytes(), source_bytes)
        restored = self.root/'schema10-config.restored'
        rollback(str(output)+'.receipt.json', restored, offline_confirmed=True)
        self.assertEqual(restored.read_bytes(), backup.read_bytes())
        # Failure rolls back the copied DB transaction, never partially upgrades
        # the source, context fields or metadata version.
        def fault(point):
            if point == 'migration.runtime_schema':
                raise RuntimeError('migration-test')
        failed = self.root/'schema11-config-failed.db'
        with self.assertRaisesRegex(RuntimeError, 'migration-test'):
            migrate(source, failed, self.root/'schema10-config-failed.backup', offline_confirmed=True, fault=fault)
        with sqlite3.connect(failed) as db:
            self.assertEqual(db.execute('SELECT version FROM task_kernel_metadata').fetchone()[0], 10)
            self.assertNotIn('configuration_context_json', {row[1] for row in db.execute('PRAGMA table_info(deployment_operations)')})
        db.close()
        self.assertEqual(source.read_bytes(), source_bytes)

    def test_schema11_rejects_existing_operation_table_missing_private_columns_without_writes(self):
        source = self.root/'schema11-incomplete.db'
        Repository(source)
        with sqlite3.connect(source) as db:
            db.execute('ALTER TABLE deployment_operations DROP COLUMN configuration_context_digest')
            db.commit()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute('PRAGMA journal_mode=DELETE')
        db.close()
        before = source.read_bytes()
        with self.assertRaisesRegex(TaskStateError, 'task_schema_unsupported'):
            Repository(source)
        self.assertEqual(source.read_bytes(), before)

    def test_schema11_removal_pointer_copy_is_null_and_failure_rolls_back(self):
        source = self.root/'schema11-removal.db'
        Repository(source)
        with sqlite3.connect(source) as db:
            db.execute("""INSERT INTO model_deployments(
                id,catalog_key,kind,label,model_id,revision,manifest_digest,enabled,is_default,
                gpu_indices_json,model_path,license,required_files_json,required_vram_mib,
                install_state,created_at,updated_at) VALUES(
                'preserved','user/preserved','image','Keep config','preserved','r1','digest',0,0,
                '[]','/read-only-model','declared','[]',1024,'ready','before','before')""")
            db.execute('ALTER TABLE model_deployments DROP COLUMN removal_operation_id')
            db.execute('DROP TABLE task_kernel_metadata')
            db.execute('CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=11))')
            db.execute('INSERT INTO task_kernel_metadata VALUES(11)')
            db.commit()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute('PRAGMA journal_mode=DELETE')
        db.close()
        before = source.read_bytes()
        output, backup = self.root/'schema12.db', self.root/'schema11.backup'
        report = migrate(source, output, backup, offline_confirmed=True)
        self.assertEqual((report['source_schema'], report['schema']), (11, SCHEMA_VERSION))
        with sqlite3.connect(output) as db:
            columns = {r[1] for r in db.execute('PRAGMA table_info(model_deployments)')}
            self.assertIn('removal_operation_id', columns)
            self.assertEqual(db.execute('SELECT count(*) FROM model_deployments WHERE removal_operation_id IS NOT NULL').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT id,label,model_path,install_state,removal_operation_id FROM model_deployments').fetchone(),
                             ('preserved','Keep config','/read-only-model','ready',None))
        db.close()
        self.assertEqual(source.read_bytes(), before)
        def fault(point):
            if point == 'migration.before_commit':
                raise RuntimeError('removal_migration_fault')
        failed = self.root/'schema12-failed.db'
        with self.assertRaisesRegex(RuntimeError, 'removal_migration_fault'):
            migrate(source, failed, self.root/'schema11-failed.backup', offline_confirmed=True, fault=fault)
        with sqlite3.connect(failed) as db:
            self.assertEqual(db.execute('SELECT version FROM task_kernel_metadata').fetchone()[0],11)
            self.assertNotIn('removal_operation_id', {r[1] for r in db.execute('PRAGMA table_info(model_deployments)')})
        db.close()
        self.assertEqual(source.read_bytes(), before)

    def test_current_schema_without_removal_pointer_is_rejected_read_only(self):
        source = self.root/'incomplete-removal.db'
        Repository(source)
        with sqlite3.connect(source) as db:
            db.execute('ALTER TABLE model_deployments DROP COLUMN removal_operation_id')
            db.commit()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute('PRAGMA journal_mode=DELETE')
        db.close()
        before = source.read_bytes()
        with self.assertRaisesRegex(TaskStateError, 'task_schema_unsupported'):
            Repository(source)
        self.assertEqual(source.read_bytes(), before)

    def test_unversioned_copy_adds_removal_column_without_rewriting_existing_deployment(self):
        source = self.root/'legacy-removal.db'
        legacy_database(source)
        with sqlite3.connect(source) as db:
            db.execute('CREATE TABLE model_deployments(id TEXT PRIMARY KEY,label TEXT)')
            db.execute("INSERT INTO model_deployments VALUES('old-model','Keep me')")
        db.close()
        before = source.read_bytes()
        output = self.root/'legacy-removal-current.db'
        migrate(source, output, self.root/'legacy-removal.backup', offline_confirmed=True)
        with sqlite3.connect(output) as db:
            self.assertEqual(db.execute('SELECT id,label,removal_operation_id FROM model_deployments').fetchone(),
                             ('old-model', 'Keep me', None))
            self.assertEqual(db.execute('SELECT version FROM task_kernel_metadata').fetchone()[0], SCHEMA_VERSION)
        db.close()
        self.assertEqual(source.read_bytes(), before)

    def test_schema9_scopes_permits_and_preserves_source_and_rollback(self):
        source = self.root/'schema9-populated.db'
        Repository(source)
        db = sqlite3.connect(source)
        try:
            db.execute('DROP TABLE runtime_lora_permits')
            db.execute('''CREATE TABLE runtime_lora_permits(
                permit_id TEXT PRIMARY KEY,asset_id TEXT,revision TEXT,
                descriptor_json TEXT,active INTEGER,UNIQUE(asset_id,revision))''')
            db.execute("INSERT INTO runtime_lora_permits VALUES('old','lora','r1','{}',1)")
            db.execute('DROP TABLE task_kernel_metadata')
            db.execute('CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=9))')
            db.execute('INSERT INTO task_kernel_metadata VALUES(9)')
            db.commit()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute('PRAGMA journal_mode=DELETE')
        finally:
            db.close()
        before = source.read_bytes()
        output, backup = self.root/'schema10.db', self.root/'schema9.backup'
        report = migrate(source, output, backup, offline_confirmed=True)
        self.assertEqual((report['source_schema'], report['schema']), (9, SCHEMA_VERSION))
        db = sqlite3.connect(output)
        try:
            self.assertEqual(db.execute('SELECT permit_id,asset_id,revision,descriptor_json,active FROM runtime_lora_permits').fetchone(),
                             ('old','lora','r1','{}',0))
            self.assertEqual(len(db.execute('SELECT scope_digest FROM runtime_lora_permits').fetchone()[0]),64)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
        finally:
            db.close()
        restored = self.root/'schema9-restored.db'
        rollback(str(output)+'.receipt.json', restored, offline_confirmed=True)
        self.assertEqual(restored.read_bytes(), backup.read_bytes())
        self.assertEqual(source.read_bytes(), before)

    def test_schema12_deadline_copy_preserves_history_source_and_exact_rollback(self):
        source = self.root / 'deadline-source.db'
        repository = Repository(source)
        state = TaskState(repository)
        local = state.accept({'service':'image','model':'local','prompt':'retained','options':{},'inputs':[]},
                             scope='fixture', mode='local')[0]
        with sqlite3.connect(source) as db:
            db.execute('ALTER TABLE task_attempts DROP COLUMN execution_deadline_at')
            db.execute('ALTER TABLE task_attempts DROP COLUMN termination_reason')
            db.execute('DROP TABLE task_kernel_metadata')
            db.execute('CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=12))')
            db.execute('INSERT INTO task_kernel_metadata VALUES(12)')
            db.commit()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute('PRAGMA journal_mode=DELETE')
            old_attempts = db.execute('SELECT * FROM task_attempts').fetchall()
        db.close()
        before = source.read_bytes()
        with self.assertRaisesRegex(TaskStateError, 'task_schema_unsupported'):
            Repository(source)
        self.assertEqual(source.read_bytes(), before)
        output, backup = self.root / 'deadline-current.db', self.root / 'deadline-backup.db'
        report = migrate(source, output, backup, offline_confirmed=True)
        self.assertEqual((report['source_schema'], report['schema']), (12, SCHEMA_VERSION))
        with sqlite3.connect(output) as db:
            attempts = db.execute('SELECT * FROM task_attempts').fetchall()
            self.assertEqual([row[:-2] for row in attempts], old_attempts)
            self.assertTrue(all(row[-2:] == (None, None) for row in attempts))
        db.close()
        self.assertEqual(Repository(output).get_task(local['id'])['status'], 'running')
        restored = self.root / 'deadline-restored.db'
        rollback(str(output) + '.receipt.json', restored, offline_confirmed=True)
        self.assertEqual(restored.read_bytes(), backup.read_bytes())
        self.assertEqual(source.read_bytes(), before)
        def fault(point):
            if point == 'migration.before_commit':
                raise RuntimeError('deadline-migration-fault')
        failed = self.root / 'deadline-failed.db'
        with self.assertRaisesRegex(RuntimeError, 'deadline-migration-fault'):
            migrate(source, failed, self.root / 'deadline-failed-backup.db', offline_confirmed=True, fault=fault)
        with sqlite3.connect(failed) as db:
            self.assertEqual(db.execute('SELECT version FROM task_kernel_metadata').fetchone()[0], 12)
            self.assertNotIn('execution_deadline_at', {r[1] for r in db.execute('PRAGMA table_info(task_attempts)')})
        db.close()
        self.assertEqual(source.read_bytes(), before)

    def test_current_schema_without_attempt_deadline_columns_is_rejected_read_only(self):
        for column in ('execution_deadline_at', 'termination_reason'):
            source = self.root / (column + '.db')
            Repository(source)
            with sqlite3.connect(source) as db:
                db.execute('ALTER TABLE task_attempts DROP COLUMN ' + column)
                db.commit()
                db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                db.execute('PRAGMA journal_mode=DELETE')
            db.close()
            before = source.read_bytes()
            with self.assertRaisesRegex(TaskStateError, 'task_schema_unsupported'):
                Repository(source)
            self.assertEqual(source.read_bytes(), before)

    def test_schema13_copy_does_not_authorize_historical_transfer_cleanup(self):
        from mediacenter.model_assets import ModelAssetManager
        from tests.test_model_assets import safetensors_bytes
        source = self.root/'cleanup-v13.db'
        repo = Repository(source)
        manager = ModelAssetManager(repo, self.root/'cleanup-model-store')
        data = safetensors_bytes()
        transfer = manager.create_upload({'display_name': 'historical', 'media_kind': 'image',
            'role': 'checkpoint', 'format': 'safetensors', 'revision': 'fixture',
            'license_declared': 'fixture', 'files': [{'relative_path': 'model.safetensors',
            'byte_size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}]})
        manager.append_upload_chunk(transfer['id'], transfer['files'][0]['id'], 0, data)
        manager.complete_upload(transfer['id'])
        with sqlite3.connect(source) as db:
            db.execute('DROP TABLE model_transfer_cleanup')
            db.execute('DROP TABLE task_kernel_metadata')
            db.execute('CREATE TABLE task_kernel_metadata(version INTEGER PRIMARY KEY CHECK(version=13))')
            db.execute('INSERT INTO task_kernel_metadata VALUES(13)')
            db.commit()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute('PRAGMA journal_mode=DELETE')
        db.close()
        before = source.read_bytes()
        with self.assertRaisesRegex(TaskStateError, 'task_schema_unsupported'):
            Repository(source)
        self.assertEqual(source.read_bytes(), before)
        output, backup = self.root/'cleanup-v14.db', self.root/'cleanup-v13-backup.db'
        report = migrate(source, output, backup, offline_confirmed=True)
        self.assertEqual((report['source_schema'], report['schema']), (13, 14))
        upgraded = Repository(output)
        self.assertEqual(upgraded.get_model_transfer(transfer['id'])['state'], 'succeeded')
        self.assertIsNone(upgraded.get_transfer_cleanup(transfer['id']))
        self.assertEqual(ModelAssetManager(upgraded, manager.storage_root).cleanup_completed_transfers(), 0)
        self.assertTrue((manager.storage_root/'quarantine'/transfer['id']/'model.safetensors').is_file())
        restored = self.root/'cleanup-restored.db'
        rollback(str(output)+'.receipt.json', restored, offline_confirmed=True)
        self.assertEqual(restored.read_bytes(), backup.read_bytes())
        self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
