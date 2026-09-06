"""Control Supervisor and one persistent inference subprocess.

No container/GPU authority is inferred here. Controller admission, grants and
full descendant-exit attestation are explicit journal hooks. Local child death
leaves uncertain work for that Controller; there is no restart/replay fallback.
Private child IPC is bounded JSON; Adapter selection is trusted configuration.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import queue
import secrets
import sys
import threading
import time

from .adapter import AdapterFactory
from .protocol import MAX_MESSAGE_BYTES, PROTOCOL, validate_envelope
from .worker_common import digest
from .transport import DeliveryPump, TransportError
from .worker_journal import CommandAuthority, JournalError, WorkerJournal


def _send(connection, value):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_MESSAGE_BYTES:
        raise ValueError("child_message_too_large")
    connection.send_bytes(data)


def _receive(connection):
    value = json.loads(connection.recv_bytes(MAX_MESSAGE_BYTES).decode("utf-8"))
    if type(value) is not dict:
        raise ValueError("invalid_child_message")
    return value


def _execution_error_code(error):
    # Inspect the adapter's already-loaded library only; the Supervisor must
    # never import torch or initialize CUDA. PyTorch aliases CUDA's OOM class
    # to its general OutOfMemoryError, so the type does not prove a device.
    torch = sys.modules.get("torch")
    error_type = vars(torch).get("OutOfMemoryError") if torch is not None else None
    if isinstance(error_type, type) and issubclass(error_type, Exception) and isinstance(error, error_type):
        return "model_out_of_memory"
    return "adapter_execution_failed"


def _write_failure_diagnostic(data):
    os.write(2, data)


class _FailureDiagnostics:
    """Best-effort first-cause evidence, never an execution/exit authority.

    One writer and one queued record bound memory even when container logging
    blocks. No retries, flush, persistent file, or exception text. A blocked
    writer cannot delay reset or the terminal IPC. Records may be lost on exit.
    """
    def __init__(self):
        self.pending = queue.Queue(maxsize=1)
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._run, name="mc-inference-diagnostics", daemon=True)
        self.thread.start()

    def submit(self, envelope, execution_token):
        try:
            record = {"schema": "mc.worker.failure/1", "error_code": "model_out_of_memory",
                      "exception_type": "torch.OutOfMemoryError", "phase": "adapter.execute",
                      "command_digest": digest(envelope), "execution_token": execution_token}
            for key in ("task_id", "attempt_id", "server_id", "instance_id", "worker_epoch", "message_id"):
                record[key] = envelope[key]
            data = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
            if len(data) <= 4096 and not self.closed.is_set():
                self.pending.put_nowait(data)
        except Exception:
            pass  # Diagnostics must not change cleanup or cause retry loops.

    def _run(self):
        while True:
            data = self.pending.get()
            if data is None or self.closed.is_set():
                return
            try:
                _write_failure_diagnostic(data)
            except Exception:
                pass

    def close(self):
        self.closed.set()  # Never join a potentially blocked logging sink.
        try:
            self.pending.put_nowait(None)
        except queue.Full:
            pass  # The queued record wakes the writer; it observes closed.


def _inference_child(factory, connection, cancellation):
    """Only this process imports and constructs model adapters."""
    diagnostics = None
    try:
        adapter = factory.create()
        try:
            diagnostics = _FailureDiagnostics()
        except Exception:
            pass  # Exhausted diagnostic thread resources do not disable inference.
        _send(connection, {"kind": "ready", "capability_digest": digest(adapter.describe_capabilities())})
        while True:
            message = _receive(connection)
            if message == {"kind": "stop"}:
                return
            if set(message) != {"kind", "execution_token", "envelope"} or message["kind"] != "execute":
                raise ValueError("invalid_child_command")
            envelope, execution_token = message["envelope"], message["execution_token"]
            capability = adapter.describe_capabilities()
            validate_envelope(envelope, capabilities={capability["model_key"]: capability})
            kind = envelope["type"]
            status, error, manifest, clean = "succeeded", None, None, True
            executing = False
            def progress(value):
                _send(connection, {"kind": "progress", "execution_token": execution_token, "payload": value})
            try:
                if kind == "model.load":
                    adapter.load(envelope["payload"])
                elif kind == "model.unload":
                    adapter.unload()
                elif kind == "task.execute":
                    adapter.validate_request(envelope)
                    executing = True
                    manifest = adapter.execute(envelope, progress, cancellation)
                else:
                    raise ValueError("invalid_child_command")
            except Exception as failure:
                status, error, manifest = "failed", "adapter_execution_failed", None
                if executing:
                    error = _execution_error_code(failure)
                    if error == "model_out_of_memory" and diagnostics is not None:
                        diagnostics.submit(envelope, execution_token)
                if kind != "task.execute":
                    clean = False  # Partial model load/unload cannot release base.
            finally:
                if kind == "task.execute":
                    try:
                        adapter.reset_task_state()
                    except Exception:
                        clean = False
            _send(connection, {"kind": "finished", "execution_token": execution_token,
                               "status": status, "error_code": error, "manifest": manifest, "clean": clean})
    except (EOFError, BrokenPipeError, ConnectionResetError):
        pass
    finally:
        if diagnostics is not None:
            diagnostics.close()
        connection.close()


class WorkerRuntime:
    def __init__(self, identity, binding, journal: WorkerJournal, factory: AdapterFactory, transport=None, *,
                 heartbeat_seconds=5, poll_seconds=0.05, cancel_grace_seconds=60, boot_timeout=10,
                 event_replay_seconds=5, clock=time.monotonic, fault=lambda _: None, command_authority=None):
        for value in (heartbeat_seconds, poll_seconds, cancel_grace_seconds, boot_timeout, event_replay_seconds):
            if type(value) not in (int, float) or not 0.01 <= value <= 300:
                raise JournalError("invalid_runtime_limits")
        if transport is not None and transport.identity != identity:
            raise JournalError("transport_identity_mismatch")
        if command_authority is not None and (not isinstance(command_authority, CommandAuthority)
                or command_authority.identity != identity or command_authority.binding_digest != digest(binding)):
            raise JournalError("command_authority_mismatch")
        self.command_authority = command_authority
        self.identity, self.binding, self.journal, self.factory, self.transport = identity, dict(binding), journal, factory, transport
        self.heartbeat_seconds, self.poll_seconds = heartbeat_seconds, poll_seconds
        self.cancel_grace_seconds, self.boot_timeout = cancel_grace_seconds, boot_timeout
        self.event_replay_seconds, self.clock, self.fault = event_replay_seconds, clock, fault
        self._context = multiprocessing.get_context("spawn")
        self._cancel = self._context.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._admission_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._threads = []
        self._active = None
        self._process = None
        self._child_token = None
        self._verified_child = None
        self._connection = None
        self._loaded = False
        self._quarantined = False
        self._started = self.clock()
        self._heartbeat_seq = self._progress_seq = self._event_cursor = 0
        self._event_replay_at = 0.0
        self._cancel_seen = None
        self._stop_requests = []
        self.errors = {}
        self.journal.register(identity, binding)

    def admit(self, *, evidence, recovery_complete, clock_trusted):
        # Reject reentrancy rather than waiting on a long describe call. This
        # mutex is never acquired by command/control/heartbeat processing.
        if not self._admission_lock.acquire(blocking=False):
            raise JournalError("admission_in_progress")
        try:
            if self._stop.is_set():
                raise JournalError("runtime_closed")
            arguments = dict(evidence=evidence, recovery_complete=recovery_complete, clock_trusted=clock_trusted)
            self.journal.check_admission(self.identity, self.binding, **arguments)
            try:
                self._ensure_child()
                with self._lifecycle_lock:
                    self._require_verified_child()
                    self.journal.admit(self.identity, self.binding, child_token=self._child_token, **arguments)
            except Exception as error:
                self._isolate(getattr(error, "code", "capability_handshake_failed"))
                raise
        finally:
            self._admission_lock.release()

    def _require_verified_child(self):
        if self._stop.is_set():
            raise JournalError("runtime_closed")
        if self._process is None or not self._process.is_alive():
            raise JournalError("child_exit_unconfirmed")
        if self._verified_child != (self.identity, self._child_token, self._process.pid):
            raise JournalError("child_capability_unverified")

    def handle(self, envelope, *, authority=None):
        if not self.identity.matches(envelope):
            raise TransportError("wrong_runtime_identity")
        if self._stop.is_set() and envelope["type"] in {"task.execute", "model.load", "model.unload"}:
            raise TransportError("runtime_closed")
        try:
            result = self.journal.receive(envelope, authority=authority)
        except JournalError as error:
            raise TransportError(error.code) from None
        if envelope["type"] == "task.cancel":
            with self._lock:
                active = self._active
                if active and active["envelope"].get("attempt_id") == envelope["attempt_id"] and active["envelope"].get("task_id") == envelope["task_id"]:
                    self._cancel.set()
                    if self._cancel_seen is None:
                        self._cancel_seen = self.clock()
        return result

    def _isolate(self, code):
        self._quarantined = True
        self.errors["execution"] = code
        try:
            self.journal.quarantine(self.identity)
        except Exception:
            pass  # Local admission remains closed even when storage is full.

    def _ensure_child(self):
        with self._lifecycle_lock:
            if self._process is not None:
                self._require_verified_child()
                return
            if self._stop.is_set():
                raise JournalError("runtime_closed")
            child_token = self.journal.plan_child(self.identity)
            self._child_token = child_token
            self._verified_child = None
            self.fault("runtime.after_child_intent")
            parent, child = self._context.Pipe(duplex=True)
            self._connection = parent
            self._process = self._context.Process(target=_inference_child, args=(self.factory, child, self._cancel), daemon=False)
            self._process.start()
            child.close()
            self.fault("runtime.after_spawn")
            self.journal.child_started(self.identity, child_token, self._process.pid)
        deadline = self.clock() + self.boot_timeout
        while self.clock() < deadline and not self._stop.is_set():
            if parent.poll(0.05):
                ready = _receive(parent)
                if ready != {"kind": "ready", "capability_digest": self.binding["capability_digest"]}:
                    raise JournalError("child_capability_mismatch")
                with self._lifecycle_lock:
                    if self._stop.is_set() or self._process is None or not self._process.is_alive():
                        raise JournalError("runtime_closed")
                    self._verified_child = (self.identity, child_token, self._process.pid)
                return
            if not self._process.is_alive():
                raise JournalError("child_exit_unconfirmed")
        raise JournalError("child_boot_timeout")

    def execute_once(self):
        if self._quarantined or self._stop.is_set():
            return False
        work = self.journal.claim(self.identity)
        if work is None:
            return False
        with self._lock:
            self._active, self._cancel_seen = work, None
            self._cancel.clear()
            envelope = work["envelope"]
            if envelope["type"] == "task.execute" and self.journal.canceled(self.identity, envelope["task_id"], envelope["attempt_id"]):
                self._cancel.set()
                self._cancel_seen = self.clock()
        self.fault("runtime.after_executing")
        try:
            self._ensure_child()
            phase = {"task.execute": "generating", "model.load": "loading", "model.unload": "unloading"}[envelope["type"]]
            self.journal.phase(self.identity, work["work_key"], work["execution_token"], phase)
            _send(self._connection, {"kind": "execute", "execution_token": work["execution_token"], "envelope": envelope})
            self.fault("runtime.after_dispatch")
            while not self._stop.is_set():
                if not self._process.is_alive():
                    raise JournalError("child_exit_unconfirmed")
                if not self._connection.poll(0.05):
                    continue
                result = _receive(self._connection)
                if result.get("execution_token") != work["execution_token"]:
                    raise JournalError("child_result_identity_conflict")
                if result.get("kind") == "progress" and set(result) == {"kind", "execution_token", "payload"}:
                    self._progress(result["payload"], envelope)
                    continue
                if set(result) != {"kind", "execution_token", "status", "error_code", "manifest", "clean"} or result["kind"] != "finished":
                    raise JournalError("invalid_child_result")
                self.fault("runtime.before_terminal")
                self.journal.finish(self.identity, work["work_key"], work["execution_token"], status=result["status"],
                                    error_code=result["error_code"], manifest=result["manifest"], clean=result["clean"])
                self.fault("runtime.after_terminal")
                if not result["clean"]:
                    self._isolate("adapter_reset_failed" if envelope["type"] == "task.execute" else "model_cleanup_unconfirmed")
                elif result["status"] == "succeeded":
                    if envelope["type"] == "model.load":
                        self._loaded = True
                    elif envelope["type"] == "model.unload":
                        self._loaded = False
                with self._lock:
                    self._active = None
                return True
            self._isolate("supervisor_stopping_unconfirmed")
        except Exception as error:
            self._isolate(getattr(error, "code", "execution_or_journal_failure"))
            raise
        return False

    def _telemetry(self, kind, payload, *, envelope=None):
        if kind == "telemetry.heartbeat":
            self._heartbeat_seq += 1
            sequence = {"telemetry_seq": self._heartbeat_seq}
        else:
            self._progress_seq += 1
            sequence = {"progress_seq": self._progress_seq, "task_id": envelope["task_id"], "attempt_id": envelope["attempt_id"]}
        message = {"protocol": PROTOCOL, "type": kind, "message_id": "tel_" + secrets.token_hex(16),
                   "server_id": self.identity.server_id, "instance_id": self.identity.instance_id,
                   "worker_epoch": self.identity.worker_epoch, "correlation_id": envelope["task_id"] if envelope else "worker",
                   "created_at": self.journal._now().isoformat().replace("+00:00", "Z"), "payload": payload, **sequence}
        return validate_envelope(message, capabilities=self.journal.capabilities)

    def _progress(self, payload, envelope):
        message = self._telemetry("telemetry.progress", payload, envelope=envelope)
        if self.transport:
            try:
                self.transport.publish(message)
            except TransportError as error:
                self.errors["progress"] = error.code  # lossy telemetry, not a failed generation

    def heartbeat_once(self):
        state = "error" if self._quarantined else "busy" if self._active else "ready" if self._loaded else "online_unloaded"
        message = self._telemetry("telemetry.heartbeat", {"state": state, "uptime_seconds": int(max(0, self.clock() - self._started))})
        if self.transport:
            self.transport.publish(message)
        return message

    def emit_once(self):
        if not self.transport:
            return 0
        current = self.clock()
        if self._event_cursor == 0 and current < self._event_replay_at:
            return 0
        page = self.journal.pending_events(epoch=self.identity.worker_epoch, after_sequence=self._event_cursor, limit=50)
        try:
            for message in page["items"]:
                self.transport.publish(message)
        except TransportError:
            # An unknown publish outcome is retried with the same stable event
            # identity.  Pace that retry as well: a disconnected or blocked
            # Controller must not turn the durable journal into a hot loop.
            self._event_replay_at = self.clock() + self.event_replay_seconds
            raise
        self._event_cursor = page["next_cursor"] if page["has_more"] else 0
        if not page["has_more"]:
            # Receipts can be delayed for minutes while the Controller waits
            # for a synchronous cold model load.  Five-second replay preserves
            # at-least-once delivery without appending the same event 20 times
            # per second for the whole load interval.
            self._event_replay_at = self.clock() + self.event_replay_seconds
        return len(page["items"])

    def _receive_lane(self, lane):
        if lane == "control":
            self._cancel_deadline()
        if self.transport:
            # Only this authenticated Server commands lane may convert wire
            # reservations into journal grants. A parsed dict is not authority.
            handler = (lambda envelope: self.handle(envelope, authority=self.command_authority)) if lane == "commands" else self.handle
            DeliveryPump(self.transport, handler).once(lane, count=1, block_ms=100)

    def _cancel_deadline(self):
        # Local cancellation supervision cannot depend on Redis availability.
        with self._lock:
            if (self._active and self._cancel_seen is not None and self.clock() - self._cancel_seen >= self.cancel_grace_seconds
                    and not any(item["work_key"] == self._active["work_key"] for item in self._stop_requests)):
                self._stop_requests.append({"work_key": self._active["work_key"], "execution_token": self._active["execution_token"],
                                            "epoch": self.identity.worker_epoch, "reason": "cancel_grace_elapsed"})

    def stop_requests(self):
        with self._lock:
            return [dict(item) for item in self._stop_requests]

    def _loop(self, name, operation, interval):
        while not self._stop.is_set():
            try:
                operation()
            except Exception as error:
                self.errors[name] = getattr(error, "code", "worker_storage_or_runtime_failure")
                if (name == "execution" and (not isinstance(error, JournalError) or error.code != "admission_required")
                        or name == "events" and not isinstance(error, TransportError)):
                    self._isolate(self.errors[name])
            self._stop.wait(interval)

    def start(self):
        if self._threads or self._stop.is_set():
            raise JournalError("runtime_already_started")
        loops = [("commands", lambda: self._receive_lane("commands"), self.poll_seconds),
                 ("control", lambda: self._receive_lane("control"), self.poll_seconds),
                 ("events", self.emit_once, max(0.05, self.poll_seconds)),
                 ("heartbeat", self.heartbeat_once, self.heartbeat_seconds),
                 ("execution", self.execute_once, self.poll_seconds)]
        for name, operation, interval in loops:
            thread = threading.Thread(target=self._loop, args=(name, operation, interval), name="mc-worker-" + name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def close(self):
        self._stop.set()
        with self._lifecycle_lock:
            self._verified_child = None
        self._cancel.set()
        for thread in self._threads:
            thread.join(timeout=3)
        if self._process is not None:
            if self._process.is_alive():
                try:
                    _send(self._connection, {"kind": "stop"})
                except (OSError, EOFError, BrokenPipeError):
                    pass
                self._process.join(timeout=1)
            if self._process.is_alive():
                self._process.terminate()  # own exact child, never an external model process
                self._process.join(timeout=3)
            if self._process.is_alive():
                self._process.kill()
                self._process.join(timeout=3)
            self._process.close()
            self._process = None
        if self._connection is not None:
            self._connection.close()
        # Direct child join is not full descendant-exit evidence. Preserve
        # journal state for the Controller rather than inventing interruption.
        try:
            self.journal.quarantine(self.identity)
        except Exception:
            pass
