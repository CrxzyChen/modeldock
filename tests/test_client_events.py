from __future__ import annotations

import http.client
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mediacenter.client_events import GPUEventObserver, MAX_EVENT_BYTES, EventCursor, MemoryEventBroker, StateEventObserver
from mediacenter.server import Handler, MediaCenterHTTPServer


class _ProjectionRepository:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection


class ClientEventTests(unittest.TestCase):
    def test_broker_is_bounded_and_replays_from_cursor(self) -> None:
        broker = MemoryEventBroker(capacity=16, max_clients=2)
        origin, reset = broker.open(None)
        self.assertFalse(reset)
        for index in range(20):
            broker.publish("task.changed", f"task-{index}", version=index)
        self.assertEqual(len(broker._events), 16)
        self.assertEqual(broker.memory_contract,
                         {"capacity": 16, "max_event_bytes": MAX_EVENT_BYTES})
        events, cursor, reset = broker.wait(EventCursor(broker.stream_id, 16), timeout=0.01)
        self.assertFalse(reset)
        self.assertEqual([item["resource_id"] for item in events],
                         ["task-16", "task-17", "task-18", "task-19"])
        self.assertEqual(cursor.sequence, 20)
        _events, _cursor, reset = broker.wait(origin, timeout=0.01)
        self.assertTrue(reset)

    def test_full_client_pool_does_not_block_publishers(self) -> None:
        broker = MemoryEventBroker(capacity=16, max_clients=1)
        self.assertTrue(broker.acquire_client())
        self.assertFalse(broker.acquire_client())
        event = broker.publish("task.changed", "task-1")
        self.assertEqual(event["resource_id"], "task-1")
        broker.release_client()

    def test_volatile_gpu_events_retain_only_the_latest_snapshot(self) -> None:
        broker = MemoryEventBroker(capacity=16)
        cursor, reset = broker.open(None)
        self.assertFalse(reset)
        for used in range(20):
            broker.publish_latest("gpu.telemetry", "gpus", data={
                "telemetry_available": True,
                "gpus": [{"index": 0, "memory_used_mib": used}],
            })
        self.assertEqual(len(broker._events), 0)
        self.assertEqual(len(broker._latest), 1)
        events, cursor, reset = broker.wait(cursor, timeout=0.01)
        self.assertFalse(reset)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"]["gpus"][0]["memory_used_mib"], 19)

    def test_gpu_observer_is_memory_only_and_non_blocking_on_probe_failure(self) -> None:
        calls = 0

        def provider():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("probe unavailable")
            return {"telemetry_available": True, "gpus": [{"index": 0}]}

        broker = MemoryEventBroker(capacity=16)
        observer = GPUEventObserver(provider, broker, poll_seconds=0.5)
        failed = observer.sample_once()
        ready = observer.sample_once()
        self.assertFalse(failed["data"]["telemetry_available"])
        self.assertEqual(failed["data"]["telemetry_error"], "probe unavailable")
        self.assertTrue(ready["data"]["telemetry_available"])
        self.assertEqual(len(broker._latest), 1)

    def test_observer_emits_only_change_hints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            db = sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE tasks (id TEXT, version INTEGER, status TEXT, stage TEXT, progress REAL, updated_at TEXT);
                CREATE TABLE model_deployments (id TEXT, enabled INTEGER, desired_state TEXT, actual_state TEXT, install_state TEXT, runtime_updated_at TEXT, removal_operation_id TEXT);
                CREATE TABLE instance_policies (instance_id TEXT, version INTEGER, status TEXT, desired_state TEXT, error_code TEXT, last_activity TEXT);
                CREATE TABLE service_installations (id TEXT, state TEXT, current_step TEXT, progress REAL, updated_at TEXT);
                CREATE TABLE model_transfers (id TEXT, state TEXT, received_bytes INTEGER, updated_at TEXT);
                CREATE TABLE model_assets (id TEXT, state TEXT, updated_at TEXT);
                CREATE TABLE asset_compatibility (created_at TEXT);
                CREATE TABLE services (kind TEXT, enabled INTEGER, timeout_seconds INTEGER);
                CREATE TABLE deployment_operations (id TEXT, state TEXT, milestone TEXT, updated_at TEXT);
                INSERT INTO tasks VALUES ('task-1', 1, 'queued', 'queued', 0, 'now');
                INSERT INTO model_deployments VALUES ('user-model',0,'unloaded','unloaded','ready','now',NULL);
            """)
            db.commit()
            db.close()
            broker = MemoryEventBroker(capacity=16)
            observer = StateEventObserver(_ProjectionRepository(path), broker)
            self.assertEqual(observer.scan_once(), 0)
            db = sqlite3.connect(path)
            db.execute("UPDATE tasks SET version=2,status='running',stage='sampling' WHERE id='task-1'")
            db.commit()
            db.close()
            self.assertEqual(observer.scan_once(), 1)
            events, _cursor, reset = broker.wait(EventCursor(broker.stream_id, 0), timeout=0.01)
            self.assertFalse(reset)
            self.assertEqual(events[0]["type"], "task.changed")
            self.assertEqual(events[0]["version"], 2)
            # Accepting removal changes only its durable owner, not runtime or
            # policy status. Other clients must still lock this instance.
            with sqlite3.connect(path) as db:
                db.execute("UPDATE model_deployments SET removal_operation_id='dop-remove' WHERE id='user-model'")
            db.close()
            self.assertEqual(observer.scan_once(), 1)
            events, _cursor, reset = broker.wait(_cursor, timeout=0.01)
            self.assertFalse(reset)
            self.assertEqual([(event['type'], event['resource_id']) for event in events],
                             [('deployment.changed', 'user-model')])
            before = path.read_bytes()
            for _ in range(100):
                self.assertEqual(observer.scan_once(), 0)
            self.assertEqual(path.read_bytes(), before)

    def test_observer_thread_detects_commits_without_event_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            db = sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE tasks (id TEXT, version INTEGER, status TEXT, stage TEXT, progress REAL, updated_at TEXT);
                CREATE TABLE model_deployments (id TEXT, enabled INTEGER, desired_state TEXT, actual_state TEXT, install_state TEXT, runtime_updated_at TEXT, removal_operation_id TEXT);
                CREATE TABLE instance_policies (instance_id TEXT, version INTEGER, status TEXT, desired_state TEXT, error_code TEXT, last_activity TEXT);
                CREATE TABLE service_installations (id TEXT, state TEXT, current_step TEXT, progress REAL, updated_at TEXT);
                CREATE TABLE model_transfers (id TEXT, state TEXT, received_bytes INTEGER, updated_at TEXT);
                CREATE TABLE model_assets (id TEXT, state TEXT, updated_at TEXT);
                CREATE TABLE asset_compatibility (created_at TEXT);
                CREATE TABLE services (kind TEXT, enabled INTEGER, timeout_seconds INTEGER);
                CREATE TABLE deployment_operations (id TEXT, state TEXT, milestone TEXT, updated_at TEXT);
                INSERT INTO tasks VALUES ('task-1', 1, 'queued', 'queued', 0, 'now');
            """)
            db.commit()
            db.close()
            broker = MemoryEventBroker(capacity=16)
            observer = StateEventObserver(_ProjectionRepository(path), broker, poll_seconds=0.1)
            observer.start()
            time.sleep(0.15)
            cursor, reset = broker.open(None)
            self.assertFalse(reset)
            db = sqlite3.connect(path)
            db.execute("UPDATE tasks SET version=2,status='running' WHERE id='task-1'")
            db.commit()
            db.close()
            events = []
            deadline = time.monotonic() + 2
            while not events and time.monotonic() < deadline:
                events, cursor, reset = broker.wait(cursor, timeout=0.2)
                self.assertFalse(reset)
            observer.stop()
            self.assertEqual([(event["type"], event["resource_id"]) for event in events],
                             [("task.changed", "task-1")])
            db = sqlite3.connect(path)
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            db.close()
            self.assertNotIn("client_events", tables)

    def test_http_stream_is_authenticated_and_unbuffered(self) -> None:
        server = MediaCenterHTTPServer(("127.0.0.1", 0), Handler)
        server.api_key = "test-key"
        server.client_events = MemoryEventBroker(capacity=16, max_clients=1)
        server.client_events.publish_latest("gpu.telemetry", "gpus", data={
            "telemetry_available": True,
            "gpus": [{"index": 0, "memory_free_mib": 48178}],
        })
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            unauthorized = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            unauthorized.request("GET", "/api/v1/events")
            denied = unauthorized.getresponse()
            self.assertEqual(denied.status, 401)
            denied.read()
            unauthorized.close()
            connection.request("GET", "/api/v1/events", headers={"X-API-Key": "test-key"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(response.getheader("Content-Type").startswith("text/event-stream"))
            self.assertEqual(response.getheader("X-Accel-Buffering"), "no")
            hello = b""
            while b"\n\n" not in hello:
                hello += response.read(1)
            self.assertIn(b"event: hello", hello)
            telemetry = b""
            while b"\n\n" not in telemetry:
                telemetry += response.read(1)
            self.assertIn(b"event: gpu.telemetry", telemetry)
            self.assertIn(b'"memory_free_mib":48178', telemetry)
            overflow = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            overflow.request("GET", "/api/v1/events", headers={"X-API-Key": "test-key"})
            refused = overflow.getresponse()
            self.assertEqual(refused.status, 503)
            refused.read()
            overflow.close()
            server.client_events.publish("task.changed", "task-2", version=3)
            changed = b""
            while b"\n\n" not in changed:
                changed += response.read(1)
            self.assertIn(b"event: task.changed", changed)
            self.assertIn(b'"resource_id":"task-2"', changed)
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_fieldless_post_consumes_json_before_reusing_connection(self) -> None:
        class Center:
            @staticmethod
            def stop_deployment_worker():
                return True

            @staticmethod
            def cancel_task(task_id):
                return {"id": task_id, "status": "canceled"}

            @staticmethod
            def get_task(task_id):
                return {"id": task_id, "status": "canceled"}

        server = MediaCenterHTTPServer(("127.0.0.1", 0), Handler)
        server.center = Center()
        server.api_key = "test-key"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        headers = {"X-API-Key": "test-key", "Content-Type": "application/json"}
        try:
            connection.request("POST", "/api/v1/tasks/task-1/cancel", body="{}", headers=headers)
            canceled = connection.getresponse()
            self.assertEqual(canceled.status, 200)
            self.assertEqual(__import__('json').loads(canceled.read())["status"], "canceled")
            connection.request("GET", "/api/v1/tasks/task-1", headers={"X-API-Key": "test-key"})
            fetched = connection.getresponse()
            self.assertEqual(fetched.status, 200)
            self.assertEqual(__import__('json').loads(fetched.read())["id"], "task-1")
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
