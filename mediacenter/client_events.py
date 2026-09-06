"""Bounded, memory-only client events for the Electron SSE control channel.

Events are hints, never durable state.  SQLite remains authoritative and a
client that misses this in-memory window obtains one fresh HTTP snapshot.
Publishing never performs disk or socket I/O.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import secrets
import threading
import time
from typing import Any, Callable


MAX_EVENT_BYTES = 16 * 1024


@dataclass(frozen=True)
class EventCursor:
    stream_id: str
    sequence: int


class MemoryEventBroker:
    def __init__(self, *, capacity: int = 1024, max_clients: int = 32):
        if type(capacity) is not int or not 16 <= capacity <= 16384:
            raise ValueError("event capacity must be between 16 and 16384")
        if type(max_clients) is not int or not 1 <= max_clients <= 256:
            raise ValueError("event client limit must be between 1 and 256")
        self.capacity = capacity
        self.stream_id = secrets.token_hex(12)
        self._events: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._latest: dict[tuple[str, str], dict[str, Any]] = {}
        self._sequence = 0
        self._condition = threading.Condition()
        self._clients = threading.BoundedSemaphore(max_clients)
        self._closed = False

    @property
    def memory_contract(self) -> dict[str, int]:
        return {"capacity": self.capacity, "max_event_bytes": MAX_EVENT_BYTES}

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def publish(self, kind: str, resource_id: str = "", *, version: int | None = None,
                data: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(kind, str) or not kind or len(kind) > 80:
            raise ValueError("invalid client event kind")
        if not isinstance(resource_id, str) or len(resource_id) > 256:
            raise ValueError("invalid client event resource")
        payload = {
            "protocol": "mc.client/1", "type": kind, "resource_id": resource_id,
            "version": version, "occurred_at": time.time(), "data": data or {},
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_EVENT_BYTES:
            raise ValueError("client event exceeds memory contract")
        with self._condition:
            if self._closed:
                return payload
            self._sequence += 1
            payload = {**payload, "id": f"{self.stream_id}:{self._sequence}"}
            self._events.append(payload)
            self._condition.notify_all()
        return payload

    def publish_latest(self, kind: str, resource_id: str = "", *,
                       data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Publish a volatile snapshot while retaining only its newest value."""
        if not isinstance(kind, str) or not kind or len(kind) > 80:
            raise ValueError("invalid client event kind")
        if not isinstance(resource_id, str) or len(resource_id) > 256:
            raise ValueError("invalid client event resource")
        payload = {
            "protocol": "mc.client/1", "type": kind, "resource_id": resource_id,
            "version": None, "occurred_at": time.time(), "data": data or {},
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_EVENT_BYTES:
            raise ValueError("client event exceeds memory contract")
        with self._condition:
            if self._closed:
                return payload
            self._sequence += 1
            payload = {**payload, "id": f"{self.stream_id}:{self._sequence}"}
            self._latest[(kind, resource_id)] = payload
            self._condition.notify_all()
        return payload

    def latest(self, kind: str, resource_id: str = "") -> dict[str, Any] | None:
        with self._condition:
            payload = self._latest.get((kind, resource_id))
            return dict(payload) if payload is not None else None

    def acquire_client(self) -> bool:
        return self._clients.acquire(blocking=False)

    def release_client(self) -> None:
        self._clients.release()

    def open(self, last_event_id: str | None) -> tuple[EventCursor, bool]:
        with self._condition:
            current = EventCursor(self.stream_id, self._sequence)
            if not last_event_id:
                return current, False
            try:
                stream_id, raw_sequence = last_event_id.rsplit(":", 1)
                sequence = int(raw_sequence)
                if sequence < 0:
                    raise ValueError
            except (ValueError, AttributeError):
                return current, True
            if stream_id != self.stream_id or sequence > self._sequence:
                return current, True
            oldest = self._events[0]["id"].rsplit(":", 1)[1] if self._events else None
            if oldest is not None and sequence < int(oldest) - 1:
                return current, True
            return EventCursor(self.stream_id, sequence), False

    def wait(self, cursor: EventCursor, *, timeout: float = 15.0) -> tuple[list[dict[str, Any]], EventCursor, bool]:
        if not 0 < timeout <= 60:
            raise ValueError("invalid event wait timeout")
        with self._condition:
            if cursor.stream_id != self.stream_id:
                return [], EventCursor(self.stream_id, self._sequence), True
            def ready() -> bool:
                return self._closed or self._sequence > cursor.sequence
            if not ready():
                self._condition.wait_for(ready, timeout)
            if self._closed:
                return [], cursor, False
            if self._events:
                oldest = int(self._events[0]["id"].rsplit(":", 1)[1])
                if cursor.sequence < oldest - 1:
                    return [], EventCursor(self.stream_id, self._sequence), True
            events = [event for event in (*self._events, *self._latest.values())
                      if int(event["id"].rsplit(":", 1)[1]) > cursor.sequence]
            events.sort(key=lambda event: int(event["id"].rsplit(":", 1)[1]))
            sequence = (int(events[-1]["id"].rsplit(":", 1)[1])
                        if events else cursor.sequence)
            return events, EventCursor(self.stream_id, sequence), False

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()


class StateEventObserver:
    """Read compact state projections on a daemon thread and publish changes.

    The observer performs no writes.  It is deliberately detached from task
    transactions, so an unavailable or slow SSE client cannot delay execution.
    """
    QUERIES = {
        "task.changed": "SELECT id,version,status,stage,progress,updated_at FROM tasks ORDER BY id",
        "deployment.changed": "SELECT id,enabled,desired_state,actual_state,install_state,runtime_updated_at,removal_operation_id FROM model_deployments ORDER BY id",
        "deployment-operation.changed": "SELECT id,state,milestone,updated_at FROM deployment_operations ORDER BY id",
        "instance.changed": "SELECT instance_id,version,status,desired_state,error_code,last_activity FROM instance_policies ORDER BY instance_id",
        "installation.changed": "SELECT id,state,current_step,progress,updated_at FROM service_installations ORDER BY id",
        "transfer.changed": "SELECT id,state,received_bytes,updated_at FROM model_transfers ORDER BY id",
        "asset.changed": "SELECT id,state,updated_at FROM model_assets ORDER BY id",
        "compatibility.changed": "SELECT 'assets' AS id,COUNT(*) AS records,MAX(created_at) AS updated_at FROM asset_compatibility",
        "service.changed": "SELECT kind,enabled,timeout_seconds FROM services ORDER BY kind",
    }
    IDS = {
        "task.changed": "id", "deployment.changed": "id", "instance.changed": "instance_id",
        "deployment-operation.changed": "id",
        "installation.changed": "id", "transfer.changed": "id", "asset.changed": "id",
        "service.changed": "kind",
        "compatibility.changed": "id",
    }

    def __init__(self, repository, broker: MemoryEventBroker, *, poll_seconds: float = 0.5):
        if not 0.1 <= poll_seconds <= 10:
            raise ValueError("invalid client event observer interval")
        self.repository, self.broker, self.poll_seconds = repository, broker, poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot: dict[str, dict[str, tuple[Any, ...]]] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="client-event-observer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(2.0, self.poll_seconds * 3))

    @staticmethod
    def _version(row: dict[str, Any]) -> int | None:
        value = row.get("version")
        return value if type(value) is int else None

    def scan_once(self, *, emit: bool = True) -> int:
        changed = 0
        with self.repository._connect() as db:
            for kind, query in self.QUERIES.items():
                rows = [dict(row) for row in db.execute(query)]
                identity = self.IDS[kind]
                current = {row[identity]: tuple(row.values()) for row in rows}
                previous = self._snapshot.get(kind)
                self._snapshot[kind] = current
                if previous is None or not emit:
                    continue
                row_by_id = {row[identity]: row for row in rows}
                for resource_id in sorted(set(previous) | set(current)):
                    if previous.get(resource_id) == current.get(resource_id):
                        continue
                    row = row_by_id.get(resource_id)
                    self.broker.publish(kind, resource_id, version=self._version(row or {}),
                                        data={"deleted": row is None})
                    changed += 1
        return changed

    def _run(self) -> None:
        try:
            # A long-lived read connection makes SQLite's data_version a cheap
            # cross-connection change signal.  Full projections are read only
            # after a commit, rather than twice per second while the system is idle.
            with self.repository._connect() as watch:
                data_version = int(watch.execute("PRAGMA data_version").fetchone()[0])
                self.scan_once(emit=False)
                while not self._stop.wait(self.poll_seconds):
                    try:
                        next_version = int(watch.execute("PRAGMA data_version").fetchone()[0])
                        if next_version == data_version:
                            continue
                        data_version = next_version
                        self.scan_once()
                    except Exception:
                        # SSE is advisory.  State execution must survive observation faults.
                        continue
        finally:
            self._thread = None


class GPUEventObserver:
    """Sample the latest GPU view into the in-memory SSE channel only."""

    def __init__(self, provider: Callable[[], dict[str, Any]], broker: MemoryEventBroker,
                 *, poll_seconds: float = 2.0):
        if not 0.5 <= poll_seconds <= 30:
            raise ValueError("invalid GPU telemetry interval")
        self.provider, self.broker, self.poll_seconds = provider, broker, poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="gpu-event-observer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(2.0, self.poll_seconds * 2))

    def sample_once(self) -> dict[str, Any]:
        try:
            payload = self.provider()
            if not isinstance(payload, dict):
                raise TypeError("GPU telemetry provider must return an object")
        except Exception as exc:
            payload = {"configured_gpu_indices": [], "configured_gpu_uuids": [],
                       "policy": "durable-base-and-task-reservations",
                       "telemetry_available": False, "telemetry_error": str(exc), "gpus": []}
        return self.broker.publish_latest("gpu.telemetry", "gpus", data=payload)

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.sample_once()
                except Exception:
                    # GPU observation is advisory and cannot block task execution.
                    pass
                if self._stop.wait(self.poll_seconds):
                    break
        finally:
            self._thread = None
