"""Actual isolated Redis tests, opt in with MC_REDIS_SERVER (Linux only).

Use the preverified extracted 7.2.16 tool, never download/install here. Every
test gets a private port-0 Unix socket, ACL and AOF directory, <=64 MiB Redis
memory /128 MiB address space. Stop only Popen's exact child; retain evidence.
The event file in rollback tests is a replay fixture, NOT MC030 Worker journal.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import redis
from redis import exceptions as redis_errors
from mediacenter.redis_transport import RedisEndpoint, RedisTransport, GROUPS, MAX_INGRESS_BYTES, render_acl
from mediacenter.transport import DeliveryPump, DurableResult, OutboxRelay, TaskEventHandler, TransportError
from mediacenter.task_state import digest
from tests import test_task_state as fixture
from tests.test_redis_transport import ROOT, IDENTITY


class PrivateRedis:
    def __init__(self):
        if os.name != "posix" or os.getuid() == 0:
            raise RuntimeError("Redis test requires non-root POSIX identity")
        self.binary = Path(os.environ["MC_REDIS_SERVER"])
        if not self.binary.is_absolute() or not self.binary.is_file():
            raise RuntimeError("Explicit Redis test binary required")
        self.directory = Path(tempfile.mkdtemp(prefix="mc029-redis-", dir="/var/tmp"))
        self.directory.chmod(0o700)
        self.socket = str(self.directory / "redis.sock")
        self.worker_secret = secrets.token_urlsafe(36)
        self.server_secret = secrets.token_urlsafe(36)
        self.admin_secret = secrets.token_urlsafe(36)
        for role, password in (("worker", self.worker_secret), ("server", self.server_secret)):
            path = self.directory / (role + ".secret")
            path.write_text(password, encoding="ascii")
            path.chmod(0o600)
        acl = render_acl((ROOT / "deploy/redis-acl.template").read_text(), IDENTITY,
                         worker_user="fixture-worker", server_user="fixture-server",
                         worker_secret_sha256=hashlib.sha256(self.worker_secret.encode()).hexdigest(),
                         server_secret_sha256=hashlib.sha256(self.server_secret.encode()).hexdigest())
        acl += "\nuser fixture-admin on #" + hashlib.sha256(self.admin_secret.encode()).hexdigest() + " ~* +@all\n"
        (self.directory / "users.acl").write_text(acl)
        (self.directory / "users.acl").chmod(0o600)
        self.config = self.directory / "redis.conf"
        self.config.write_text(f'port 0\nunixsocket {self.socket}\nunixsocketperm 700\nprotected-mode yes\n'
            f'dir {self.directory}\naclfile {self.directory / "users.acl"}\nappendonly yes\nappendfsync always\n'
            'save ""\nmaxmemory 64mb\nmaxmemory-policy noeviction\nauto-aof-rewrite-percentage 0\n'
            'proto-max-bulk-len 1mb\nclient-query-buffer-limit 2mb\n'
            'client-output-buffer-limit pubsub 1mb 256kb 1\n'
            'daemonize no\n')
        self.process = None
        self.log = None
        self.runs = []
        self.admin = None
        self.transports = []
        result = subprocess.run([str(self.binary), "--version"], capture_output=True, timeout=5, check=True, text=True)
        if "v=7.2.16 " not in result.stdout:
            raise RuntimeError("Unexpected Redis version")
        self.version = result.stdout.strip()
        self.start()

    def start(self):
        import resource
        def limits():
            resource.setrlimit(resource.RLIMIT_AS, (128 * 1024**2, 128 * 1024**2))
        self.log = (self.directory / f"redis-{len(self.runs)}.log").open("ab")
        self.process = subprocess.Popen([str(self.binary), str(self.config)], stdin=subprocess.DEVNULL,
                                        stdout=self.log, stderr=subprocess.STDOUT, preexec_fn=limits)
        self.runs.append({"pid": self.process.pid, "started": time.time(), "returncode": None})
        self.admin = redis.Redis(unix_socket_path=self.socket, username="fixture-admin", password=self.admin_secret,
                                 socket_timeout=1, socket_connect_timeout=1, lib_name=None, lib_version=None)
        deadline = time.monotonic() + 5
        try:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("Private Redis exited during startup; retained log " + str(self.directory))
                try:
                    if self.admin.ping():
                        return
                except (redis_errors.ConnectionError, redis_errors.TimeoutError):
                    time.sleep(0.02)
            raise RuntimeError("Private Redis startup timeout")
        except Exception:
            self.stop()
            raise

    def stop(self):
        for transport in self.transports:
            transport.close()
        self.transports.clear()
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()  # only this exact test child
                    self.process.wait(timeout=5)
            self.runs[-1]["returncode"] = self.process.returncode
            self.runs[-1]["stopped"] = time.time()
            self.process = None
        if self.admin:
            self.admin.close()
        if self.log:
            self.log.close()
        evidence = {"version": self.version, "uid": os.getuid(), "socket": self.socket, "port": 0,
                    "maxmemory": 64 * 1024**2, "address_limit": 128 * 1024**2,
                    "appendfsync": "always (deterministic test; production everysec)", "runs": self.runs}
        (self.directory / "process-evidence.json").write_text(json.dumps(evidence, indent=2))

    def transport(self, role, consumer):
        endpoint = RedisEndpoint("fixture-" + role, self.directory / (role + ".secret"), unix_socket=self.socket)
        transport = RedisTransport(IDENTITY, endpoint, role=role, consumer=consumer)
        self.transports.append(transport)
        return transport


@unittest.skipUnless(os.name == "posix" and os.environ.get("MC_REDIS_SERVER"), "explicit isolated Linux Redis tool required")
class RedisRecoveryTests(unittest.TestCase):
    accepted = fixture.TaskStateTests.accepted
    dispatched = fixture.TaskStateTests.dispatched
    event = fixture.TaskStateTests.event
    seal = fixture.TaskStateTests.seal
    count = fixture.TaskStateTests.count

    def setUp(self):
        fixture.TaskStateTests.setUp(self)
        self.servers = []
        self.broker = self.new_broker()
        self.server = self.broker.transport("server", "server-one")
        self.worker = self.broker.transport("worker", "worker-one")
        self.server.provision()

    def new_broker(self):
        broker = PrivateRedis()
        self.servers.append(broker)
        return broker

    def tearDown(self):
        for broker in self.servers:
            broker.stop()
            print("Redis evidence:", broker.directory)
        fixture.TaskStateTests.tearDown(self)

    def pending(self, lane):
        return self.broker.admin.xpending(self.server.key(lane), GROUPS[lane])["pending"]

    def test_real_pel_claim_ack_and_pending_before_new(self):
        _, command = self.dispatched()
        OutboxRelay(self.state, self.server).flush()
        first = self.worker.read("commands", block_ms=0)[0]
        self.assertEqual(first.envelope, command)
        self.assertEqual(self.pending("commands"), 1)
        duplicate = copy.deepcopy(command); duplicate["message_id"] = "second-transport-id"
        self.server.publish(duplicate)
        self.assertEqual(self.worker.read("commands", block_ms=0)[0].envelope, command)
        successor = self.broker.transport("worker", "worker-successor")
        claimed = successor.claim("commands", min_idle_ms=0)
        self.assertEqual(claimed[0].envelope, command)
        self.assertEqual(self.count("task_attempts"), 1)
        successor.ack(claimed[0])
        self.assertEqual(self.pending("commands"), 0)
        self.assertEqual(successor.read("commands", block_ms=0)[0].envelope, duplicate)

    def test_real_sqlite_commit_failure_and_postcommit_crash(self):
        _, command = self.dispatched()
        self.worker.publish(self.event(command))
        self.state.fault = lambda _: (_ for _ in ()).throw(sqlite3.OperationalError("full"))
        with self.assertRaisesRegex(TransportError, "durable_commit_failed"):
            DeliveryPump(self.server, TaskEventHandler(self.state)).once("events", block_ms=0)
        self.assertEqual(self.pending("events"), 1)
        self.assertEqual(self.count("task_inbox"), 0)
        self.state.fault = lambda _: None
        pump = DeliveryPump(self.server, TaskEventHandler(self.state), fault=lambda _: (_ for _ in ()).throw(RuntimeError("after commit")))
        with self.assertRaises(RuntimeError):
            pump.once("events", block_ms=0)
        self.assertEqual(self.pending("events"), 1)
        self.assertEqual(self.count("task_inbox"), 1)
        DeliveryPump(self.server, TaskEventHandler(self.state)).once("events", block_ms=0)
        self.assertEqual(self.pending("events"), 0)
        self.assertEqual(self.count("task_inbox"), 1)

    def test_real_bad_second_event_does_not_ack_or_isolate_valid_first(self):
        task, command = self.dispatched()
        bad = self.event(command, 2, payload={"reservation_id": "wrong", "reservation_generation": 999})
        good = self.event(command)
        self.worker.publish(bad)
        pump = DeliveryPump(self.server, TaskEventHandler(self.state))
        self.assertEqual(pump.once("events", block_ms=0)["quarantined"], 1)
        self.assertEqual(self.count("task_inbox"), 0)
        self.worker.publish(good)
        self.assertEqual(pump.once("events", block_ms=0)["applied"], 1)
        self.assertEqual(self.pending("events"), 0)
        self.assertEqual(self.count("task_inbox"), 1)
        self.assertEqual(self.repository.get_task(task["id"])["status"], "running")
        diagnostics = self.server.diagnostics()["items"]
        self.assertEqual([item["code"] for item in diagnostics], ["reservation_identity_conflict"])

    def test_actual_aof_restart_preserves_pending_and_stable_envelope(self):
        _, command = self.dispatched()
        self.server.publish(command)
        original = self.worker.read("commands", block_ms=0)[0]
        self.worker.publish(self.event(command))  # still in append-only ingress
        self.broker.stop()
        self.broker.start()
        self.server = self.broker.transport("server", "server-one")
        self.worker = self.broker.transport("worker", "worker-one")
        recovered = self.worker.read("commands", block_ms=0)[0]
        self.assertEqual(recovered.entry_id, original.entry_id)
        self.assertEqual(recovered.envelope, command)
        self.assertEqual(self.pending("commands"), 1)
        self.worker.ack(recovered)
        self.assertEqual(self.pending("commands"), 0)
        self.assertEqual(self.broker.admin.llen(self.server.key("ingress")), 1)
        self.assertEqual(self.server.read("events", block_ms=0)[0].envelope, self.event(command))

    def test_actual_process_exit_after_xadd_and_after_database_commit(self):
        _, command = self.dispatched()
        script = '''import os, sys
from pathlib import Path
from mediacenter.repository import Repository
from mediacenter.task_state import TaskState
from mediacenter.redis_transport import RedisEndpoint, RedisTransport
from mediacenter.transport import Identity, OutboxRelay, DeliveryPump, TaskEventHandler
state=TaskState(Repository(Path(sys.argv[1])))
transport=RedisTransport(Identity("mediacenter","instance-one","epoch-one"),
    RedisEndpoint("fixture-server",Path(sys.argv[2]),unix_socket=sys.argv[3]),role="server",consumer="server-one")
if sys.argv[4]=="send":
    OutboxRelay(state,transport,fault=lambda _:os._exit(73)).flush()
else:
    DeliveryPump(transport,TaskEventHandler(state),fault=lambda _:os._exit(74)).once("events",block_ms=0)
'''
        arguments = [sys.executable, "-B", "-c", script, str(self.repository.path),
                     str(self.broker.directory / "server.secret"), self.broker.socket]
        first = subprocess.run(arguments + ["send"], capture_output=True, timeout=15)
        self.assertEqual(first.returncode, 73, first.stderr.decode())
        self.assertEqual(self.state.outbox(), [command])
        OutboxRelay(self.state, self.server).flush()
        duplicates = self.worker.read("commands", block_ms=0)
        self.assertEqual([item.envelope for item in duplicates], [command, command])
        self.worker.publish(self.event(command))
        second = subprocess.run(arguments + ["receive"], capture_output=True, timeout=15)
        self.assertEqual(second.returncode, 74, second.stderr.decode())
        self.assertEqual(self.pending("events"), 1)
        self.assertEqual(self.count("task_inbox"), 1)
        DeliveryPump(self.server, TaskEventHandler(self.state)).once("events", block_ms=0)
        self.assertEqual(self.pending("events"), 0)
        self.assertEqual(self.count("task_attempts"), 1)
        self.assertEqual(self.count("task_inbox"), 1)

    def test_broker_record_loss_rebuilds_sqlite_outbox_and_unconfirmed_terminal(self):
        task, command = self.dispatched()
        OutboxRelay(self.state, self.server).flush()
        manifest = self.seal(command)
        terminal = self.event(command, kind="task.terminal", payload={"status": "succeeded", "error_code": None, "manifest": manifest})
        replay_fixture = self.broker.directory / "unconfirmed-event-fixture.json"
        replay_fixture.write_text(json.dumps(terminal))
        self.worker.publish(terminal)
        DeliveryPump(self.server, TaskEventHandler(self.state)).once("events", block_ms=0)
        receipt = self.state.receipt_for_event(terminal)
        self.assertEqual([item.envelope for item in self.worker.read("control", block_ms=0)], [receipt])
        self.assertEqual(receipt["payload"], {"subject":"task", "task_id":task["id"],
            "attempt_id":command["attempt_id"], "event_message_id":terminal["message_id"],
            "event_seq":terminal["event_seq"]})
        durable = self.state.outbox(replay=True)
        commands = [item for item in durable if item["type"] != "event.receipt"]
        control = [item for item in durable if item["type"] == "event.receipt"]
        # Formally completed model operations remain immutable history; they
        # must not be re-issued after broker loss (current outbox contract).
        self.assertEqual(commands, [command])
        self.assertEqual([item["payload"]["subject"] for item in control], ["worker", "operation", "operation", "task"])
        self.assertEqual(control[-1], receipt)
        with self.repository._connect() as db:
            model_operation = dict(db.execute("SELECT * FROM model_operations").fetchone())
            historical_load = json.loads(db.execute("SELECT envelope_json FROM task_outbox WHERE message_id=?",
                (model_operation["command_id"],)).fetchone()[0])
            instance_events = {row["message_id"]:json.loads(row["envelope_json"]) for row in db.execute("SELECT * FROM instance_inbox")}
        self.assertEqual(historical_load["payload"]["operation_id"], model_operation["operation_id"])
        self.assertEqual(historical_load["message_id"], model_operation["command_id"])
        for response in control[:-1]:
            event = instance_events[response["payload"]["event_message_id"]]
            self.assertEqual(response["payload"]["event_seq"], event["event_seq"])
            for key in ("server_id", "instance_id", "worker_epoch"):
                self.assertEqual(response[key], event[key])
            if response["payload"]["subject"] == "operation":
                self.assertEqual(response["payload"]["operation_id"], model_operation["operation_id"])
                self.assertEqual(response["payload"]["desired_revision"], model_operation["desired_revision"])
        # No Worker application receipt persisted. Discard this transport, not
        # SQLite or fixture. The replacement Redis is truly empty on disk.
        self.broker.stop()
        self.broker = self.new_broker()
        self.server = self.broker.transport("server", "server-one")
        self.worker = self.broker.transport("worker", "worker-one")
        self.server.provision()
        replayed = OutboxRelay(self.state, self.server).flush(replay=True)
        self.assertEqual(replayed["sent"], len(durable))
        self.assertEqual([item.envelope for item in self.worker.read("commands", block_ms=0)], commands)
        self.worker.publish(json.loads(replay_fixture.read_text()))
        DeliveryPump(self.server, TaskEventHandler(self.state)).once("events", block_ms=0)
        receipts = self.worker.read("control", block_ms=0)
        self.assertEqual([item.envelope for item in receipts], control + [receipt])
        self.assertEqual(self.repository.get_task(task["id"])["status"], "succeeded")
        self.assertEqual(self.count("task_attempts"), 1)
        self.assertEqual(self.count("task_artifacts"), 1)
        with self.repository._connect() as db:
            self.assertEqual([dict(row) for row in db.execute("SELECT * FROM model_operations")], [model_operation])
            self.assertEqual({row["message_id"]:json.loads(row["envelope_json"]) for row in db.execute("SELECT * FROM instance_inbox")}, instance_events)

    def test_real_disconnect_is_bounded_and_reconnect_does_not_recreate_attempt(self):
        _, command = self.dispatched()
        self.broker.stop()
        start = time.monotonic()
        with self.assertRaises(TransportError) as result:
            OutboxRelay(self.state, self.server).flush()
        self.assertIn(result.exception.code, {"redis_disconnected", "redis_timeout"})
        self.assertLess(time.monotonic() - start, 4)
        self.assertEqual(self.state.outbox(), [command])
        self.broker.start()
        # Pools reconnect after a transport outage; no new business task.
        self.broker.transports.extend((self.server, self.worker))
        OutboxRelay(self.state, self.server).flush()
        self.assertEqual(self.worker.read("commands", block_ms=0)[0].envelope, command)
        self.assertEqual(self.count("task_attempts"), 1)

    def test_real_noeviction_backpressure_retains_existing_stream_and_outbox(self):
        _, command = self.dispatched()
        first_id = self.server.publish(command)
        self.worker.read("commands", block_ms=0)
        self.broker.admin.config_set("maxmemory", 1)
        with self.assertRaisesRegex(TransportError, "redis_backpressure"):
            OutboxRelay(self.state, self.server).flush()
        self.assertTrue(self.server.backpressured)
        self.assertEqual(self.state.outbox(), [command])
        self.assertEqual(self.pending("commands"), 1)
        self.assertEqual(self.broker.admin.xrange(self.server.key("commands"))[0][0].decode(), first_id)
        self.broker.admin.config_set("maxmemory", 64 * 1024**2)
        OutboxRelay(self.state, self.server).flush()
        self.assertEqual(self.count("task_attempts"), 1)

    def test_real_acl_selector_command_key_cross_product_is_rejected(self):
        _, command = self.dispatched()
        self.server.publish(command)
        delivery = self.worker.read("commands", block_ms=0)[0]
        worker = self.worker.clients["commands"]
        own = IDENTITY.prefix
        foreign = own.replace("instance-one", "instance-other")
        forbidden = [
            ("XADD", own + ":commands", "*", "envelope", "{}"),
            ("XADD", own + ":control", "*", "envelope", "{}"),
            ("XADD", foreign + ":events", "*", "envelope", "{}"),
            ("XREADGROUP", "GROUP", GROUPS["events"], "evil", "STREAMS", own + ":events", ">"),
            ("XREADGROUP", "GROUP", GROUPS["commands"], "evil", "STREAMS", foreign + ":commands", ">"),
            ("XACK", foreign + ":events", GROUPS["events"], "1-0"),
            ("XDEL", own + ":commands", delivery.entry_id),
            ("XTRIM", own + ":commands", "MAXLEN", 0),
            ("XGROUP", "DESTROY", own + ":commands", GROUPS["commands"]),
            ("DEL", own + ":commands"),
            ("SET", own + ":events", "bad"),
            ("CONFIG", "GET", "*"),
            ("EVAL", "return 1", 0),
            ("LPOP", own + ":ingress"),
            ("LTRIM", own + ":ingress", 1, -1),
            ("DEL", own + ":ingress"),
            ("RPUSH", foreign + ":ingress", "{}"),
        ]
        for command_args in forbidden:
            with self.subTest(command=command_args[0]), self.assertRaises(redis_errors.NoPermissionError):
                worker.execute_command(*command_args)
        self.assertEqual(self.pending("commands"), 1)
        self.worker.ack(delivery)
        self.worker.publish(self.event(command))
        self.assertEqual(self.server.read("events", block_ms=0)[0].envelope, self.event(command))

    def test_real_control_read_is_independent_of_blocked_execute(self):
        task, command = self.dispatched()
        # Commands stream empty, so this call actually blocks in Redis.
        with ThreadPoolExecutor(max_workers=1) as pool:
            blocked = pool.submit(self.worker.read, "commands", block_ms=1000)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and self.broker.admin.info("clients")["blocked_clients"] < 1:
                time.sleep(0.01)
            self.assertGreaterEqual(self.broker.admin.info("clients")["blocked_clients"], 1)
            self.state.cancel(task["id"])
            cancel = next(item for item in self.state.outbox() if item["type"] == "task.cancel")
            self.server.publish(cancel)
            start = time.monotonic()
            received = self.worker.read("control", block_ms=0)[0]
            self.assertLess(time.monotonic() - start, 0.5)
            self.assertFalse(blocked.done())
            self.assertEqual(received.envelope, cancel)
            self.assertEqual(blocked.result(timeout=3), [])

    def test_real_worker_cannot_trim_pending_events_with_xadd_options(self):
        _, command = self.dispatched()
        self.worker.publish(self.event(command))
        event = self.server.read("events", block_ms=0)[0]
        key = self.server.key("events")
        client = self.worker.clients["events"]
        for arguments in (("MAXLEN", "=", "0"), ("MINID", "=", "999999999999999-0")):
            with self.subTest(option=arguments[0]), self.assertRaises(redis_errors.NoPermissionError):
                client.execute_command("XADD", key, *arguments, "*", "envelope", "{}")
        self.assertEqual(self.pending("events"), 1)
        self.assertEqual(self.broker.admin.xrange(key)[0][0].decode(), event.entry_id)

    def test_real_ingress_wrong_types_oom_and_isolation_failure_retain_head(self):
        _, command = self.dispatched()
        event = self.event(command)
        self.worker.publish(event)
        ingress, events = self.server.key("ingress"), self.server.key("events")
        self.broker.admin.delete(events)
        self.broker.admin.set(events, "wrongtype")
        with self.assertRaisesRegex(TransportError, "redis_command_rejected"):
            self.server.promote_events()
        self.assertEqual(self.broker.admin.llen(ingress), 1)
        self.broker.admin.delete(events)
        self.server.provision()
        self.broker.admin.config_set("maxmemory", 1)
        with self.assertRaises(TransportError):
            self.server.promote_events()
        self.assertEqual(self.broker.admin.llen(ingress), 1)
        self.broker.admin.config_set("maxmemory", 64 * 1024**2)
        self.assertEqual(self.server.promote_events()["moved"], 1)
        self.assertEqual(self.broker.admin.llen(ingress), 0)
        # A bad diagnostic key must not consume a malformed event.
        self.worker.clients["events"].rpush(ingress, '{"password":"do not echo"}')
        self.broker.admin.set(self.server.key("diagnostics"), "wrongtype")
        with self.assertRaisesRegex(TransportError, "redis_command_rejected"):
            self.server.promote_events()
        self.assertEqual(self.broker.admin.llen(ingress), 1)
        self.broker.admin.delete(self.server.key("diagnostics"))
        self.server.promote_events()
        self.assertEqual(self.broker.admin.llen(ingress), 0)
        self.assertNotIn("password", json.dumps(self.server.diagnostics()))

    def test_real_ingress_large_invalid_and_foreign_are_isolated_with_bounded_batch(self):
        _, command = self.dispatched()
        event = self.event(command)
        foreign = copy.deepcopy(event); foreign["instance_id"] = "another-instance"
        ingress = self.server.key("ingress")
        self.worker.clients["events"].rpush(ingress, "secret" * 50000, "{}", json.dumps(foreign))
        self.worker.publish(event)
        moved = self.server.promote_events(count=2)
        self.assertEqual(moved["moved"], 2)
        self.assertEqual(self.broker.admin.llen(ingress), 2)
        self.server.promote_events(count=2)
        self.assertEqual(self.broker.admin.llen(ingress), 0)
        self.assertEqual(self.broker.admin.xlen(self.server.key("events")), 1)
        errors = {item["code"] for item in self.server.diagnostics()["items"]}
        self.assertEqual(errors, {"message_too_large", "unsupported_protocol", "wrong_transport_identity"})
        self.assertNotIn("secret", json.dumps(self.server.diagnostics()))

    def test_real_script_error_after_append_duplicates_instead_of_losing_event(self):
        _, command = self.dispatched()
        event = self.event(command)
        self.worker.publish(event)
        acl_path = self.broker.directory / "users.acl"
        original = acl_path.read_text()
        # Deliberately remove the trusted Server's LPOP permission after its
        # append permission. Redis Lua does not roll back an earlier XADD.
        acl_path.write_text(original.replace("+lpop", "-lpop"))
        self.broker.admin.acl_load()
        with self.assertRaises(TransportError):
            self.server.promote_events(count=1)
        self.assertEqual(self.broker.admin.llen(self.server.key("ingress")), 1)
        self.assertEqual(self.broker.admin.xlen(self.server.key("events")), 1)
        acl_path.write_text(original)
        self.broker.admin.acl_load()
        self.server.promote_events(count=1)
        self.assertEqual(self.broker.admin.llen(self.server.key("ingress")), 0)
        self.assertEqual(self.broker.admin.xlen(self.server.key("events")), 2)
        DeliveryPump(self.server, TaskEventHandler(self.state)).once("events", block_ms=0)
        self.assertEqual(self.count("task_inbox"), 1)
        self.assertEqual(self.count("task_outbox"), 2)

    def test_real_raw_rpush_bulk_limit_and_maximum_poison_head_are_bounded(self):
        task, command = self.dispatched()
        ingress = self.server.key("ingress")
        client = self.worker.clients["events"]
        client.rpush(ingress, b"z" * MAX_INGRESS_BYTES)
        with self.assertRaises((redis_errors.ResponseError, redis_errors.ConnectionError)):
            client.rpush(ingress, b"z" * (MAX_INGRESS_BYTES + 1))
        self.assertEqual(self.broker.admin.llen(ingress), 1)
        self.worker.publish(self.event(command))
        start = time.monotonic()
        result = self.server.promote_events(max_bytes=MAX_INGRESS_BYTES)
        self.assertEqual(result, {"moved": 1, "bytes_examined": MAX_INGRESS_BYTES})
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(self.broker.admin.llen(ingress), 1)
        # The maximum-sized bad item cannot starve the control stream.
        self.state.cancel(task["id"])
        cancel = next(item for item in self.state.outbox() if item["type"] == "task.cancel")
        self.server.publish(cancel)
        self.assertEqual(self.worker.read("control", block_ms=0)[0].envelope, cancel)
        self.server.promote_events()
        self.assertEqual(self.broker.admin.llen(ingress), 0)

    def test_real_configuration_drift_is_detected_without_consuming_ingress(self):
        _, command = self.dispatched()
        self.worker.publish(self.event(command))
        self.broker.admin.config_set("proto-max-bulk-len", 2 * MAX_INGRESS_BYTES)
        with self.assertRaisesRegex(TransportError, "unsafe_redis_configuration"):
            self.server.promote_events()
        self.assertEqual(self.broker.admin.llen(self.server.key("ingress")), 1)
        with self.assertRaises(redis_errors.NoPermissionError):
            self.server.clients["events"].config_set("proto-max-bulk-len", MAX_INGRESS_BYTES)
        self.broker.admin.config_set("proto-max-bulk-len", MAX_INGRESS_BYTES)
        self.assertEqual(self.server.promote_events()["moved"], 1)

    def heartbeat(self, sequence=1):
        return {"protocol": "mc.worker/1", "type": "telemetry.heartbeat", "message_id": "heartbeat-fresh",
                "server_id": "mediacenter", "instance_id": "instance-one", "worker_epoch": "epoch-one",
                "correlation_id": "heartbeat-fresh", "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "telemetry_seq": sequence, "payload": {"state": "online_unloaded", "uptime_seconds": 10}}

    def await_telemetry(self, kind, expected):
        deadline = time.monotonic() + 3
        while self.server.telemetry(kind) != expected and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.server.telemetry(kind), expected)

    def test_telemetry_reconnect_same_transport_rejects_stale_and_wrong_epoch(self):
        self.assertTrue(self.server._telemetry_ready.wait(3))
        message = self.heartbeat()
        publisher = self.worker.clients["telemetry"]
        channel = self.server.key("telemetry:heartbeat")
        publisher.publish(channel, json.dumps(message))
        self.await_telemetry("heartbeat", message)
        self.broker.admin.client_kill_filter(_type="pubsub")
        self.await_telemetry("heartbeat", None)
        self.assertTrue(self.server._telemetry_ready.wait(3))
        stale = self.heartbeat(2); stale["created_at"] = "2020-01-01T00:00:00Z"
        publisher.publish(channel, json.dumps(stale))
        wrong = self.heartbeat(2); wrong["worker_epoch"] = "other-epoch"
        publisher.publish(channel, json.dumps(wrong))
        time.sleep(0.1)
        self.assertIsNone(self.server.telemetry("heartbeat"))
        fresh = self.heartbeat(2)
        publisher.publish(channel, json.dumps(fresh))
        self.await_telemetry("heartbeat", fresh)
        self.assertTrue(self.server.close())
        self.assertFalse(self.server._telemetry_thread.is_alive())

    def test_telemetry_progress_never_regresses_to_old_attempt(self):
        self.assertTrue(self.server._telemetry_ready.wait(3))
        publisher = self.worker.clients["telemetry"]
        value = self.heartbeat()
        value.pop("telemetry_seq")
        value.update(type="telemetry.progress", task_id="task-a", attempt_id="attempt-a", progress_seq=1,
                     payload={"phase":"generating", "completed":1, "total":10, "unit":"steps"})
        publisher.publish(self.server.key("telemetry:progress"), json.dumps(value))
        self.await_telemetry("progress", value)
        newer = copy.deepcopy(value)
        newer.update(task_id="task-b", attempt_id="attempt-b", progress_seq=2)
        publisher.publish(self.server.key("telemetry:progress"), json.dumps(newer))
        self.await_telemetry("progress", newer)
        publisher.publish(self.server.key("telemetry:progress"), json.dumps(value))
        time.sleep(0.1)
        self.assertEqual(self.server.telemetry("progress"), newer)

    def test_old_backlog_gets_only_remaining_freshness_not_another_full_ttl(self):
        self.assertTrue(self.server._telemetry_ready.wait(3))
        message = self.heartbeat()
        message['created_at'] = (datetime.now(timezone.utc) - timedelta(seconds=29)).isoformat().replace('+00:00', 'Z')
        self.worker.clients['telemetry'].publish(self.server.key('telemetry:heartbeat'), json.dumps(message))
        self.await_telemetry('heartbeat', message)
        time.sleep(1.1)
        self.assertIsNone(self.server.telemetry('heartbeat'))

    def test_slow_subscriber_flood_is_bounded_and_durable_control_still_works(self):
        self.assertTrue(self.server._telemetry_ready.wait(3))
        publisher = self.worker.clients["telemetry"]
        with self.server.clients["telemetry"].pubsub() as slow:
            slow.subscribe(self.server.key("telemetry:progress"))
            self.assertEqual(slow.get_message(timeout=1)["type"], "subscribe")
            before = self.broker.admin.info("persistence")["aof_current_size"]
            for _ in range(128):
                publisher.publish(self.server.key("telemetry:progress"), b"x" * 65536)
            self.assertEqual(self.broker.admin.info("persistence")["aof_current_size"], before)
            clients = self.broker.admin.client_list(_type="pubsub")
            self.assertTrue(all(int(row["omem"]) <= 1048576 for row in clients))
            task, command = self.dispatched()
            self.state.cancel(task["id"])
            cancel = next(item for item in self.state.outbox() if item["type"] == "task.cancel")
            started = time.monotonic()
            self.server.publish(cancel)
            self.assertEqual(self.worker.read("control", block_ms=0)[0].envelope, cancel)
            self.assertLess(time.monotonic() - started, 1)
            self.worker.publish(self.event(command))
            self.assertEqual(self.server.read("events", block_ms=0)[0].envelope, self.event(command))
            self.assertGreater(self.broker.admin.info("persistence")["aof_current_size"], before)

    def test_actual_acl_readback_matches_publisher_channel_contract(self):
        import re
        from mediacenter.runtime_provisioning import EpochPublisher
        for line in (self.broker.directory / "users.acl").read_text().splitlines():
            if not line.startswith(("user fixture-worker ", "user fixture-server ")):
                continue
            parts = re.findall(r'\([^()]*\)|\S+', line)
            self.assertEqual(EpochPublisher._observed(self.broker.admin.acl_getuser(parts[1])),
                             EpochPublisher._expected(parts[2:]))
        with self.server.clients["telemetry"].pubsub() as subscriber:
            subscriber.psubscribe(self.server.key("telemetry:heartbeat") + "*")
            with self.assertRaises(redis_errors.NoPermissionError):
                subscriber.get_message(timeout=1)

    def test_real_protocol_quarantine_and_monotonic_expiring_telemetry(self):
        self.broker.admin.xadd(self.server.key("events"), {"envelope": '{"secret":"not in diagnostics"}'})
        result = DeliveryPump(self.server, TaskEventHandler(self.state)).once("events", block_ms=0)
        self.assertEqual(result["quarantined"], 1)
        diagnostics = self.server.diagnostics()["items"]
        self.assertEqual(diagnostics[0]["code"], "unsupported_protocol")
        self.assertNotIn("secret", json.dumps(diagnostics))
        self.assertEqual(self.broker.admin.xlen(self.server.key("events")), 1)
        self.assertEqual(self.pending("events"), 0)
        heartbeat = {"protocol": "mc.worker/1", "type": "telemetry.heartbeat", "message_id": "heartbeat-one",
                     "server_id": "mediacenter", "instance_id": "instance-one", "worker_epoch": "epoch-one",
                     "correlation_id": "heartbeat-one", "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "telemetry_seq": 2,
                     "payload": {"state": "online_unloaded", "uptime_seconds": 10}}
        self.assertTrue(self.server._telemetry_ready.wait(3))
        before = self.broker.admin.info("persistence")["aof_current_size"]
        self.assertEqual(self.worker.publish(heartbeat), "queued")
        deadline = time.monotonic() + 3
        while self.server.telemetry("heartbeat") != heartbeat and time.monotonic() < deadline:
            time.sleep(0.01)
        old = copy.deepcopy(heartbeat); old["telemetry_seq"] = 1
        self.worker.publish(old)
        time.sleep(0.1)
        self.assertEqual(self.server.telemetry("heartbeat"), heartbeat)
        self.assertEqual(self.broker.admin.exists(self.server.key("telemetry:heartbeat")), 0)
        self.assertEqual(self.broker.admin.info("persistence")["aof_current_size"], before)
        publisher = self.worker.clients["telemetry"]
        for sequence in range(3, 1003):
            heartbeat["telemetry_seq"] = sequence
            publisher.publish(self.server.key("telemetry:heartbeat"), json.dumps(heartbeat))
        deadline = time.monotonic() + 5
        while self.server.telemetry("heartbeat") != heartbeat and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.server.telemetry("heartbeat"), heartbeat)
        self.assertEqual(self.broker.admin.info("persistence")["aof_current_size"], before)
        with self.assertRaises(redis_errors.NoPermissionError):
            publisher.set(self.server.key("telemetry:heartbeat"), "forbidden")
        with self.assertRaises(redis_errors.NoPermissionError):
            publisher.publish(self.server.key("telemetry:heartbeat") + ":other", "forbidden")
        with self.server.clients["telemetry"].pubsub() as subscriber:
            subscriber.subscribe(self.server.key("telemetry:heartbeat") + ":other")
            with self.assertRaises(redis_errors.NoPermissionError):
                subscriber.get_message(timeout=1)
        # A restart cannot recover volatile telemetry. Durable evidence above
        # remains governed by the separate stream restart tests.
        self.broker.stop()
        self.broker.start()
        restarted = self.broker.transport("server", "fresh-telemetry")
        restarted.provision()
        self.assertTrue(restarted._telemetry_ready.wait(3))
        self.assertIsNone(restarted.telemetry("heartbeat"))


if __name__ == "__main__":
    unittest.main()
