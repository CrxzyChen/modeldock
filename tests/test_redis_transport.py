from __future__ import annotations
from tests.test_resident_policy import fixture_capacity

import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

from redis import exceptions as redis_errors
from mediacenter.protocol import ProtocolError
from mediacenter.redis_transport import RedisEndpoint, RedisTransport, GROUPS, classify, render_acl
from mediacenter.transport import (Delivery, DeliveryPump, DurableResult, Identity,
                                   OutboxRelay, TaskEventHandler, TransportError, lane_for)
from mediacenter.task_state import digest
from tests import test_task_state as fixture

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = Identity("mediacenter", "instance-one", "epoch-one")


@unittest.skipUnless(os.name=='posix' and os.environ.get('MC_REDIS_SERVER'),'explicit isolated Linux Redis tool required')
class ArtifactRedisTests(unittest.TestCase):
    def test_real_redis_acked_success_restart_seals_original_without_new_attempt(self):
        from types import SimpleNamespace
        from tests.test_transport_recovery import PrivateRedis
        from tests.test_artifact_commit import ArtifactCommitTests, PNG
        from mediacenter.reconciler import Reconciler, InstanceEventHandler
        from mediacenter.repository import Repository
        from mediacenter.instance_policy import InstancePolicy
        f=ArtifactCommitTests();f.persist_event=False;f.setUp()
        broker=None
        try:
            broker=PrivateRedis();server=broker.transport('server','artifact-controller');worker=broker.transport('worker','artifact-worker')
            server.provision();worker.publish(f.event)
            DeliveryPump(server,InstanceEventHandler(f.fixture.authority)).once('events',block_ms=0)
            self.assertEqual(f.fixture.count('artifact_publications'),0)
            self.assertEqual(f.fixture.count('task_inbox'),1)
            self.assertEqual(worker.read('control',block_ms=0),[])
            self.assertEqual(server.clients['events'].xpending(server.key('events'),GROUPS['events'])['pending'],0)
            # The broker has ACKed, but no publication existed. Recover only
            # from SQLite+the original fixed scratch, without another event.
            broker.stop()
            repository=Repository(f.repo.path)
            scheduler=SimpleNamespace(authority=InstancePolicy(repository))
            package=SimpleNamespace(instance_id='instance-one',epoch='epoch-one',record_id='fixture',outputs_path=lambda:f.outputs)
            provider=SimpleNamespace(for_epoch=lambda instance,epoch:package,
                validate_claim=lambda db,record_id:None)
            reconciler=Reconciler(repository,None,scheduler,package_provider=provider,artifact_root=f.store.root)
            reconciler.seal_pending()
            self.assertEqual(repository.get_task(f.task['id'])['status'],'succeeded')
            self.assertEqual(f.fixture.count('task_attempts'),1)
            with reconciler.artifacts.authorize(f.output()) as stream:self.assertEqual(stream.stream.read(),PNG)
            broker.start();server=broker.transport('server','artifact-restart');worker=broker.transport('worker','artifact-replay')
            self.assertEqual(OutboxRelay(scheduler.authority.tasks,server).flush()['sent'],1)
            receipt=scheduler.authority.tasks.receipt_for_event(f.event)
            sent=worker.read('control',block_ms=0)
            self.assertEqual([item.envelope for item in sent],[receipt])
            for item in sent:worker.ack(item)
            worker.publish(f.event)
            deliveries=[];read=server.read
            def capture(*args,**kwargs):
                values=read(*args,**kwargs);deliveries.extend(values);return values
            with patch.object(server,'read',side_effect=capture):
                result=DeliveryPump(server,InstanceEventHandler(scheduler.authority)).once('events',block_ms=0)
            # AOF recovery may replay the original record as well. Every
            # transport delivery must remain exactly the original event.
            self.assertGreaterEqual(len(deliveries),1)
            self.assertEqual([item.envelope for item in deliveries],[f.event]*len(deliveries))
            self.assertEqual((result['applied'],result['quarantined']),(len(deliveries),0))
            receipts=[item.envelope for item in worker.read('control',block_ms=0)]
            receipt=scheduler.authority.tasks.receipt_for_event(f.event)
            self.assertEqual(receipts,[receipt]*len(deliveries))
            self.assertEqual(f.fixture.count('task_artifacts'),1)
            self.assertEqual(f.fixture.count('artifact_publications'),1)
        finally:
            if broker:
                broker.stop();print('Redis artifact evidence:',broker.directory,flush=True)
                self.assertTrue(all(run['returncode']==0 for run in broker.runs))
            f.tearDown()


class RecordingTransport:
    identity = IDENTITY

    def __init__(self):
        self.deliveries = []
        self.sent = []
        self.acked = []
        self.quarantined = []
        self.publish_error = None

    def publish(self, message):
        self.sent.append(copy.deepcopy(message))
        if self.publish_error:
            raise self.publish_error
        return "1-0"

    def read(self, *args, **kwargs):
        return list(self.deliveries)

    def ack(self, item):
        self.acked.append(item.entry_id)

    def quarantine(self, item, code):
        self.quarantined.append((item.entry_id, code))


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.secret = self.root / "redis.secret"
        self.secret.write_text("a" * 48, encoding="ascii")
        self.secret.chmod(0o600)
        self.endpoint = RedisEndpoint("worker-one", self.secret, tls=False)
        self.clients = []

    def tearDown(self):
        self.temp.cleanup()

    def transport(self, role="worker"):
        def factory(**kwargs):
            client = MagicMock()
            client.options = kwargs
            client.xreadgroup.return_value = []
            client.pubsub.return_value.get_message.return_value = None
            client.xadd.return_value = b"1-0"
            client.config_get.return_value = {"proto-max-bulk-len": "1048576", "client-query-buffer-limit": "2097152",
                "maxmemory": "67108864", "maxmemory-policy": "noeviction", "appendonly": "yes", "appendfsync": "everysec",
                "client-output-buffer-limit": "normal 0 0 0 replica 268435456 67108864 60 pubsub 1048576 262144 1"}
            self.clients.append(client)
            return client
        transport = RedisTransport(IDENTITY, self.endpoint, role=role, consumer="consumer-one", client_factory=factory)
        self.addCleanup(transport.close)
        return transport

    def test_telemetry_read_is_memory_only_and_expires(self):
        transport = self.transport("server")
        with patch.object(transport, "_start_telemetry"):
            transport._telemetry_ready.set()
            message = {"payload": {"state": "online_unloaded"}}
            transport._telemetry_latest["heartbeat"] = (100, message)
            with patch("mediacenter.redis_transport.time.monotonic", return_value=110):
                result = transport.telemetry("heartbeat")
                self.assertEqual(result, message)
                result["payload"]["state"] = "changed"
                self.assertEqual(transport.telemetry("heartbeat"), message)
            with patch("mediacenter.redis_transport.time.monotonic", return_value=130):
                self.assertIsNone(transport.telemetry("heartbeat"))
        transport.clients["telemetry"].get.assert_not_called()

    def test_telemetry_queue_coalesces_without_network_on_calling_thread(self):
        transport = self.transport()
        with patch.object(transport, "_start_telemetry"):
            for sequence in range(1000):
                transport._telemetry({"type": "telemetry.heartbeat", "telemetry_seq": sequence})
            self.assertEqual(len(transport._telemetry_pending), 1)
            self.assertEqual(transport._telemetry_pending["heartbeat"]["telemetry_seq"], 999)
            self.assertEqual(transport._telemetry({"type": "telemetry.heartbeat", "telemetry_seq": 1}), "coalesced")
        transport.clients["telemetry"].publish.assert_not_called()
        transport.clients["telemetry"].set.assert_not_called()

    def test_identity_and_lane_are_strict_and_never_alias(self):
        for bad in ("a:b", "*", "../other", "", "space id", "ok..bad"):
            with self.subTest(bad=bad), self.assertRaises(TransportError):
                Identity("server", bad, "epoch")
        self.assertEqual(lane_for("worker.snapshot.request"), "control")
        self.assertEqual(lane_for("worker.snapshot"), "events")
        self.assertEqual(lane_for("model.load"), "commands")
        with self.assertRaises(TransportError):
            lane_for("worker.unknown")

    def test_bounded_independent_connections_and_no_block_zero(self):
        transport = self.transport()
        self.assertEqual(len({id(item) for item in transport.clients.values()}), 5)
        self.assertEqual(transport.clients["commands"].options["max_connections"], 2)
        self.assertFalse(transport.clients["control"].options["retry_on_timeout"])
        transport.read("control", block_ms=0)
        calls = transport.clients["control"].xreadgroup.call_args_list
        self.assertEqual(calls[0].args[2], {IDENTITY.prefix + ":control": "0-0"})
        self.assertEqual(calls[1].args[2], {IDENTITY.prefix + ":control": ">"})
        self.assertNotIn("block", calls[1].kwargs)
        for invalid in (-1, 2000, True):
            with self.assertRaises(TransportError):
                transport.read("control", block_ms=invalid)
        transport.close()
        for client in transport.clients.values():
            client.close.assert_called_once()

    def test_own_pending_precedes_new_and_claim_preserves_identity(self):
        transport = self.transport()
        transport.clients["commands"].xreadgroup.return_value = [(b"stream", [(b"2-0", {b"envelope": b"{}"})])]
        deliveries = transport.read("commands")
        self.assertEqual(deliveries[0].entry_id, "2-0")
        self.assertEqual(deliveries[0].error_code, "unsupported_protocol")
        self.assertEqual(transport.clients["commands"].xreadgroup.call_count, 1)
        transport.clients["commands"].xpending_range.return_value = [{"message_id": b"2-0"}]
        transport.clients["commands"].xclaim.return_value = [(b"2-0", {b"envelope": b"{}"})]
        claimed = transport.claim("commands", min_idle_ms=100)
        self.assertEqual(claimed[0].entry_id, "2-0")
        self.assertEqual(transport.clients["commands"].xclaim.call_args.args[2], "consumer-one")

    def test_role_and_unproven_trim_fail_before_any_redis_call(self):
        worker = self.transport()
        with self.assertRaisesRegex(TransportError, "role_forbidden"):
            worker.read("events")
        with self.assertRaisesRegex(TransportError, "role_forbidden"):
            worker.provision()
        with self.assertRaisesRegex(TransportError, "reliable_retention_unproven"):
            worker.trim_reliable("commands", application_confirmed=True, all_groups_empty=True)
        for client in worker.clients.values():
            client.xdel.assert_not_called()
            client.xtrim.assert_not_called()

    def test_error_categories_do_not_leak_secrets(self):
        cases = [(redis_errors.AuthenticationError, "redis_authentication_failed", False),
                 (redis_errors.NoPermissionError, "redis_permission_denied", False),
                 (redis_errors.OutOfMemoryError, "redis_backpressure", False),
                 (redis_errors.TimeoutError, "redis_timeout", True),
                 (redis_errors.ConnectionError, "redis_disconnected", True),
                 (redis_errors.ResponseError, "redis_command_rejected", False)]
        for cls, code, unknown in cases:
            with self.subTest(code=code):
                error = classify(cls("secret-user:password@host"), write=True)
                self.assertEqual(str(error), code)
                self.assertEqual(error.outcome_unknown, unknown)
                self.assertNotIn("password", str(error))

    def test_quarantine_keeps_stream_and_ack_only_after_diagnostic_write(self):
        transport = self.transport()
        item = Delivery("commands", "1-0", error_code="unsupported_protocol", fingerprint="a" * 64)
        transport.clients["diagnostics"].hset.side_effect = redis_errors.OutOfMemoryError("secret")
        with self.assertRaisesRegex(TransportError, "redis_backpressure"):
            transport.quarantine(item, item.error_code)
        transport.clients["commands"].xack.assert_not_called()
        transport.clients["diagnostics"].hset.side_effect = None
        transport.quarantine(item, item.error_code)
        transport.clients["commands"].xack.assert_called_once()
        transport.clients["commands"].xdel.assert_not_called()
        saved = json.loads(transport.clients["diagnostics"].hset.call_args.args[2])
        self.assertEqual(set(saved), {"code", "sha256", "entry_id", "lane"})

    def test_connection_secret_validation_and_no_remote_plaintext(self):
        with self.assertRaisesRegex(TransportError, "plaintext_remote_forbidden"):
            RedisEndpoint("user", self.secret, host="10.0.0.1", tls=False).options()
        self.secret.write_text("not a valid password\n", encoding="ascii")
        with self.assertRaisesRegex(TransportError, "invalid_secret_file"):
            self.endpoint.options()
        with self.assertRaises(TransportError):
            RedisEndpoint("user", Path("relative-secret")).options()

    @unittest.skipUnless(os.name == "posix", "POSIX private-file permissions")
    def test_secret_group_readable_or_symlink_is_rejected(self):
        self.secret.chmod(0o644)
        with self.assertRaisesRegex(TransportError, "invalid_secret_file"):
            self.endpoint.options()
        self.secret.chmod(0o600)
        link = self.root / "linked-secret"
        link.symlink_to(self.secret)
        with self.assertRaisesRegex(TransportError, "invalid_secret_file"):
            RedisEndpoint("user", link).options()

    def test_complete_runtime_lock_and_acl_no_global_key_union(self):
        lock = (ROOT / "requirements/control-runtime.lock").read_text()
        self.assertIn('python_full_version < "3.11.3"', lock)
        self.assertIn('typing_extensions==4.16.0 ; python_version < "3.11"', lock)
        self.assertEqual(lock.count("--hash=sha256:"), 4)
        self.assertIn('"redis==5.3.1"', (ROOT / "pyproject.toml").read_text())
        data = json.loads((ROOT / "deploy/redis-runtime.json").read_text())
        self.assertEqual(data["redis_version"], "7.2.16")
        self.assertEqual(data["resources"], {
            "maxmemory_bytes": 2 * 1024**3,
            "container_memory_bytes": 4 * 1024**3,
            "container_memory_swap_bytes": 4 * 1024**3,
            "pids_limit": 128,
        })
        redis_unit = (ROOT / "deploy/mediacenter-redis.service").read_text()
        self.assertIn("docker update --memory 4g --memory-swap 4g mediacenter-redis", redis_unit)
        template = (ROOT / "deploy/redis-acl.template").read_text()
        self.assertNotIn("+@all", template)
        self.assertNotIn("+xdel", template)
        self.assertNotIn("+xtrim", template)
        self.assertIn("+xgroup|create", template)
        self.assertIn("maxmemory-policy noeviction", (ROOT / "deploy/redis.conf").read_text())

    def test_acl_render_validates_identity_hashes_and_removes_comments(self):
        template = (ROOT / "deploy/redis-acl.template").read_text()
        result = render_acl(template, IDENTITY, server_user="server", worker_user="worker",
                            server_secret_sha256="a" * 64, worker_secret_sha256="b" * 64)
        self.assertTrue(all(line.startswith("user ") for line in result.splitlines()))
        worker_line = next(line for line in result.splitlines() if line.startswith("user worker "))
        self.assertNotIn("+xadd", worker_line)
        self.assertIn("+rpush", worker_line)
        self.assertNotIn("+eval", worker_line)
        for bad in ("*", "user other on", "default"):
            with self.assertRaises(TransportError):
                render_acl(template, IDENTITY, server_user="server", worker_user=bad,
                           server_secret_sha256="a" * 64, worker_secret_sha256="b" * 64)

    def test_promotion_is_bounded_and_worker_cannot_invoke_it(self):
        worker = self.transport()
        with self.assertRaisesRegex(TransportError, "role_forbidden"):
            worker.promote_events()
        server = self.transport("server")
        server.clients["events"].eval.side_effect = [
            [b"raw", b"2", b"{}"], 1, [b"raw", b"2", b"{}"], 1,
        ]
        self.assertEqual(server.promote_events(count=2)["moved"], 2)
        self.assertEqual(server.clients["events"].eval.call_count, 4)
        for call in server.clients["events"].eval.call_args_list:
            self.assertIn(IDENTITY.prefix + ":ingress", call.args)
        with self.assertRaisesRegex(TransportError, "invalid_promotion_limits"):
            server.promote_events(count=1001)

    def test_live_runtime_limits_fail_closed_before_lua_or_group_creation(self):
        server = self.transport("server")
        server.clients["events"].config_get.return_value["proto-max-bulk-len"] = "2097152"
        with self.assertRaisesRegex(TransportError, "unsafe_redis_configuration"):
            server.provision()
        with self.assertRaisesRegex(TransportError, "unsafe_redis_configuration"):
            server.promote_events()
        server.clients["events"].eval.assert_not_called()
        server.clients["events"].xgroup_create.assert_not_called()


class TaskTransportTests(unittest.TestCase):
    setUp = fixture.TaskStateTests.setUp
    tearDown = fixture.TaskStateTests.tearDown
    accepted = fixture.TaskStateTests.accepted
    dispatched = fixture.TaskStateTests.dispatched
    event = fixture.TaskStateTests.event
    seal = fixture.TaskStateTests.seal
    count = fixture.TaskStateTests.count

    def test_xadd_success_then_local_crash_republishes_same_command(self):
        _, command = self.dispatched()
        transport = RecordingTransport()
        relay = OutboxRelay(self.state, transport, fault=lambda _: (_ for _ in ()).throw(RuntimeError("crash")))
        with self.assertRaises(RuntimeError):
            relay.flush()
        self.assertEqual(self.state.outbox(), [command])
        OutboxRelay(self.state, transport).flush()
        self.assertEqual(transport.sent, [command, command])
        self.assertEqual(self.count("task_attempts"), 1)

    def test_timeout_unknown_keeps_outbox_and_no_new_attempt(self):
        _, command = self.dispatched()
        transport = RecordingTransport()
        transport.publish_error = TransportError("redis_timeout", retryable=True, outcome_unknown=True)
        with self.assertRaises(TransportError) as result:
            OutboxRelay(self.state, transport).flush()
        self.assertTrue(result.exception.outcome_unknown)
        self.assertEqual(self.state.outbox(), [command])
        self.assertEqual(self.count("task_attempts"), 1)

    def test_mark_delivered_database_failure_is_stable_and_keeps_original_message(self):
        _, command = self.dispatched()
        transport = RecordingTransport()
        with patch.object(self.state, "mark_delivered", side_effect=sqlite3.OperationalError("database full secret")):
            with self.assertRaisesRegex(TransportError, "outbox_commit_failed"):
                OutboxRelay(self.state, transport).flush()
        self.assertEqual(self.state.outbox(), [command])
        OutboxRelay(self.state, transport).flush()
        self.assertEqual(transport.sent, [command, command])

    def test_receipt_sequence_and_subject_must_match_before_ack(self):
        _, command = self.dispatched()
        event = self.event(command)
        receipt = TaskEventHandler(self.state)(event).receipts[0]
        transport = RecordingTransport()
        transport.deliveries = [Delivery("events", "1-0", event)]
        bad = copy.deepcopy(receipt); bad["payload"]["event_seq"] += 1
        with self.assertRaisesRegex(TransportError, "receipt_identity_conflict"):
            DeliveryPump(transport, lambda _: DurableResult("applied", (bad,))).once("events")
        self.assertEqual(transport.acked, [])
        self.assertEqual(transport.sent, [])

    def test_corrupt_receipt_is_storage_failure_not_quarantined_or_acked(self):
        _, command = self.dispatched()
        event = self.event(command)
        receipt = TaskEventHandler(self.state)(event).receipts[0]
        with self.repository._connect() as db:
            db.execute("UPDATE task_outbox SET digest='corrupt' WHERE message_id=?", (receipt["message_id"],))
        transport = RecordingTransport()
        transport.deliveries = [Delivery("events", "1-0", event)]
        with self.assertRaisesRegex(TransportError, "durable_integrity_failed"):
            DeliveryPump(transport, TaskEventHandler(self.state)).once("events")
        self.assertEqual(transport.acked, [])
        self.assertEqual(transport.quarantined, [])
        with self.repository._connect() as db:
            db.execute("UPDATE task_outbox SET digest=? WHERE message_id=?", (digest(receipt), receipt["message_id"]))
        DeliveryPump(transport, TaskEventHandler(self.state)).once("events")
        self.assertEqual(transport.sent, [receipt])
        self.assertEqual(transport.acked, ["1-0"])

    def test_bad_reservation_seq_two_cannot_poison_later_valid_seq_one(self):
        task, command = self.dispatched()
        bad = self.event(command, 2, payload={"reservation_id": "wrong-reservation", "reservation_generation": 1})
        good = self.event(command)
        transport = RecordingTransport()
        transport.deliveries = [Delivery("events", "2-0", bad)]
        pump = DeliveryPump(transport, TaskEventHandler(self.state))
        self.assertEqual(pump.once("events")["quarantined"], 1)
        self.assertEqual(self.count("task_inbox"), 0)
        transport.deliveries = [Delivery("events", "1-0", good)]
        self.assertEqual(pump.once("events")["applied"], 1)
        self.assertEqual(transport.quarantined, [("2-0", "reservation_identity_conflict")])
        self.assertEqual(transport.acked, ["1-0"])
        self.assertEqual(self.repository.get_task(task["id"])["status"], "running")
        self.assertEqual(self.count("task_inbox"), 1)

    def test_existing_poison_pending_is_internal_failure_and_current_delivery_is_retained(self):
        task, command = self.dispatched()
        bad = self.event(command, 2, payload={"reservation_id": "wrong-reservation", "reservation_generation": 1})
        # Simulate a prior version's already durable poison row. This is test
        # data corruption/recovery, not a production repair path.
        with self.repository._connect() as db:
            db.execute("INSERT INTO task_inbox VALUES(?,?,?,?,?,?,?,?)", (bad["message_id"], task["id"], command["attempt_id"],
                       2, json.dumps(bad), digest(bad), "pending", "2026-08-31T00:00:00Z"))
        transport = RecordingTransport()
        good = self.event(command)
        transport.deliveries = [Delivery("events", "1-0", good)]
        pump = DeliveryPump(transport, TaskEventHandler(self.state))
        with self.assertRaisesRegex(TransportError, "durable_integrity_failed"):
            pump.once("events")
        self.assertEqual(transport.acked, [])
        self.assertEqual(transport.quarantined, [])
        self.assertEqual(self.count("task_inbox"), 1)
        self.assertEqual(self.repository.get_task(task["id"])["status"], "assigned")
        repaired = self.event(command, 2)
        with self.repository._connect() as db:
            db.execute("UPDATE task_inbox SET envelope_json=?,digest=? WHERE message_id=?",
                       (json.dumps(repaired), digest(repaired), bad["message_id"]))
        pump.once("events")
        self.assertEqual(transport.acked, ["1-0"])
        self.assertEqual(self.count("task_inbox"), 2)
        self.assertEqual(self.count("task_outbox"), 3)

    def test_other_stored_pending_identity_or_protocol_faults_never_quarantine_current_event(self):
        _, command = self.dispatched()
        second = self.event(command, 2, "phase.changed", {"phase": "generating"})
        self.state.receive(second)
        mutations = [dict(second, server_id="foreign"), dict(second, payload={"phase": "unknown"}), dict(second, event_seq=7)]
        transport = RecordingTransport()
        transport.deliveries = [Delivery("events", "1-0", self.event(command))]
        for changed in mutations:
            with self.subTest(changed=changed):
                with self.repository._connect() as db:
                    db.execute("UPDATE task_inbox SET envelope_json=?,digest=? WHERE message_id=?",
                               (json.dumps(changed), digest(changed), second["message_id"]))
                with self.assertRaisesRegex(TransportError, "durable_integrity_failed"):
                    DeliveryPump(transport, TaskEventHandler(self.state)).once("events")
                self.assertEqual(transport.acked, [])
                self.assertEqual(transport.quarantined, [])
                self.assertEqual(self.count("task_inbox"), 1)

    def test_duplicate_after_receipt_marked_delivered_resends_original_receipt(self):
        _, command = self.dispatched()
        event = self.event(command)
        handler = TaskEventHandler(self.state)
        first = handler(event)
        receipt = first.receipts[0]
        self.state.mark_delivered(receipt["message_id"], digest(receipt))
        transport = RecordingTransport()
        transport.deliveries = [Delivery("events", "1-0", event)]
        self.assertEqual(DeliveryPump(transport, handler).once("events")["applied"], 1)
        self.assertEqual(transport.sent, [receipt])
        self.assertEqual(transport.acked, ["1-0"])
        self.assertEqual(self.count("task_outbox"), 2)
        self.state.outbox_page = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unbounded scan"))
        self.assertEqual(handler(event).receipts, (receipt,))

    def test_db_failure_leaves_pending_and_after_commit_crash_replays(self):
        _, command = self.dispatched()
        event = self.event(command)
        transport = RecordingTransport()
        transport.deliveries = [Delivery("events", "1-0", event)]
        self.state.fault = lambda _: (_ for _ in ()).throw(sqlite3.OperationalError("full secret"))
        with self.assertRaisesRegex(TransportError, "durable_commit_failed"):
            DeliveryPump(transport, TaskEventHandler(self.state)).once("events")
        self.assertEqual(transport.acked, [])
        self.assertEqual(self.count("task_inbox"), 0)
        self.state.fault = lambda _: None
        pump = DeliveryPump(transport, TaskEventHandler(self.state), fault=lambda _: (_ for _ in ()).throw(RuntimeError("crash")))
        with self.assertRaises(RuntimeError):
            pump.once("events")
        self.assertEqual(transport.acked, [])
        self.assertEqual(self.count("task_inbox"), 1)
        DeliveryPump(transport, TaskEventHandler(self.state)).once("events")
        self.assertEqual(transport.acked, ["1-0"])
        self.assertEqual(self.count("task_inbox"), 1)

    def test_out_of_order_committed_pending_is_ackable_not_success(self):
        task, command = self.dispatched()
        manifest = self.seal(command)
        late = self.event(command, 3, "task.terminal", {"status": "succeeded", "error_code": None, "manifest": manifest})
        transport = RecordingTransport()
        transport.deliveries = [Delivery("events", "3-0", late)]
        DeliveryPump(transport, TaskEventHandler(self.state)).once("events")
        self.assertEqual(transport.acked, ["3-0"])
        self.assertEqual(transport.sent, [])
        self.assertEqual(self.repository.get_task(task["id"])["status"], "assigned")
        self.state.receive(self.event(command))
        self.state.receive(self.event(command, 2, "phase.changed", {"phase": "generating"}))
        self.assertEqual(self.repository.get_task(task["id"])["status"], "succeeded")
        OutboxRelay(self.state, transport).flush(replay=True)
        self.assertTrue(any(item.get("payload", {}).get("event_message_id") == late["message_id"] for item in transport.sent))

    def test_old_epoch_waits_for_authority_not_quarantined_or_acked(self):
        _, command = self.dispatched()
        self.authority.quarantine("instance-one", "epoch-one", "fixture_lost_epoch")
        transport = RecordingTransport()
        transport.deliveries = [Delivery("events", "1-0", self.event(command))]
        with self.assertRaisesRegex(TransportError, "awaiting_epoch_reconciliation"):
            DeliveryPump(transport, TaskEventHandler(self.state)).once("events")
        self.assertEqual(transport.acked, [])
        self.assertEqual(transport.quarantined, [])

    def test_receipt_has_no_receipt_and_missing_lifecycle_handler_retains_pending(self):
        _, command = self.dispatched()
        receipt = TaskEventHandler(self.state)(self.event(command)).receipts[0]
        transport = RecordingTransport()
        transport.deliveries = [Delivery("control", "1-0", receipt)]
        with self.assertRaisesRegex(TransportError, "recursive_receipt_forbidden"):
            DeliveryPump(transport, lambda _: DurableResult("journaled", (receipt,))).once("control")
        self.assertEqual(transport.acked, [])
        DeliveryPump(transport, lambda _: DurableResult("journaled")).once("control")
        self.assertEqual(transport.acked, ["1-0"])
        with self.assertRaisesRegex(TransportError, "durable_handler_unavailable"):
            TaskEventHandler(self.state)(receipt)

    def test_recovery_pages_do_not_starve_records_after_first_hundred(self):
        _, command = self.dispatched()
        with self.repository._connect() as db:
            for number in range(104):
                item = copy.deepcopy(command)
                item["message_id"] = f"recover-{number}"
                self.state._outbox(db, item, item["task_id"], item["attempt_id"])
        transport = RecordingTransport()
        relay = OutboxRelay(self.state, transport)
        first = relay.flush(page_size=30, max_pages=2)
        self.assertTrue(first["has_more"])
        second = relay.flush(page_size=30, max_pages=2, after_sequence=first["next_cursor"])
        self.assertFalse(second["has_more"])
        self.assertEqual(len(transport.sent), 105)
        expected_ids = {command['message_id']} | {f'recover-{number}' for number in range(104)}
        with self.repository._connect() as db:
            receipt_ids = {row[0] for row in db.execute(
                "SELECT message_id FROM task_outbox WHERE json_extract(envelope_json,'$.type')='event.receipt'")}
        self.assertEqual({item['message_id'] for item in transport.sent}, expected_ids)
        relay.flush(replay=True)
        self.assertEqual(len(transport.sent), 210 + len(receipt_ids))
        self.assertEqual({item['message_id'] for item in transport.sent}, expected_ids | receipt_ids)
        self.assertEqual({item['message_id'] for item in transport.sent[105:]}, expected_ids | receipt_ids)
        self.assertEqual(self.count("task_attempts"), 1)


if __name__ == "__main__":
    unittest.main()
