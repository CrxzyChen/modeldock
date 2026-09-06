"""Bounded transport orchestration, never an execution or task-state authority.

Redis ACK means the durable handler returned, not that a command was executed.
Lifecycle handlers and Worker journal implementations are supplied by their
owners; absence of a handler leaves delivery pending. Receipts have no receipts.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .protocol import EVENT_TYPES, TELEMETRY_TYPES, ProtocolError, validate_envelope
from .worker_common import TaskStateError, digest


class TransportError(RuntimeError):
    """Stable, credential/input-free diagnostic; no automatic retry loop."""

    def __init__(self, code: str, *, retryable=False, outcome_unknown=False):
        self.code = code
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown
        super().__init__(code)


def token(value):
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) or ".." in value:
        raise TransportError("invalid_identity")
    return value


@dataclass(frozen=True)
class Identity:
    server_id: str
    instance_id: str
    worker_epoch: str

    def __post_init__(self):
        for value in (self.server_id, self.instance_id, self.worker_epoch):
            token(value)

    @property
    def prefix(self):
        return f"mc:v1:{self.server_id}:worker:{self.instance_id}:{self.worker_epoch}"

    def matches(self, envelope):
        return all(envelope.get(key) == getattr(self, key) for key in ("server_id", "instance_id", "worker_epoch"))


def lane_for(kind):
    if kind in {"task.execute", "model.load", "model.unload"}:
        return "commands"
    if kind in {"task.cancel", "worker.snapshot.request", "event.receipt"}:
        return "control"
    if kind in EVENT_TYPES:
        return "events"
    if kind in TELEMETRY_TYPES:
        return "telemetry"
    raise TransportError("unknown_message_type")


@dataclass(frozen=True)
class Delivery:
    lane: str
    entry_id: str
    envelope: dict | None = field(default=None, repr=False)
    error_code: str | None = None
    fingerprint: str = ""


@dataclass(frozen=True)
class DurableResult:
    status: str
    receipts: tuple[dict, ...] = field(default=(), repr=False)


class DurableHandler(Protocol):
    def __call__(self, envelope: dict) -> DurableResult: ...


def receipt_matches(receipt, event):
    """Typed confirmation relationship only; journal persistence is separate."""
    if event["type"] not in EVENT_TYPES or receipt["type"] != "event.receipt":
        return False
    if any(receipt[key] != event[key] for key in ("server_id", "instance_id", "worker_epoch")):
        return False
    payload = {"event_message_id": event["message_id"], "event_seq": event["event_seq"]}
    if "task_id" in event:
        payload.update(subject="task", task_id=event["task_id"], attempt_id=event["attempt_id"])
    elif event["type"] == "worker.registered":
        payload.update(subject="worker")
    else:
        payload.update(subject="operation", operation_id=event["payload"]["operation_id"],
                       desired_revision=event["payload"]["desired_revision"])
    return receipt["payload"] == payload


class Transport(Protocol):
    identity: Identity
    def publish(self, envelope: dict) -> str: ...
    def read(self, lane: str, *, count=20, block_ms=100) -> list[Delivery]: ...
    def ack(self, delivery: Delivery) -> None: ...
    def quarantine(self, delivery: Delivery, code: str) -> None: ...


class TaskEventHandler:
    """MC028 transaction adapter. Does not invent lifecycle persistence.

    Existing receipts are read through the public indexed lookup rather than
    creating a new acknowledgement on every duplicate. A pending out-of-order
    event is already durable and may be transport-ACKed, but has no receipt yet.
    """

    def __init__(self, state):
        self.state = state

    def __call__(self, envelope):
        if envelope["type"] not in {"task.accepted", "task.terminal", "phase.changed"} or "task_id" not in envelope:
            raise TransportError("durable_handler_unavailable")
        status = self.state.receive(envelope)
        receipt = self.state.receipt_for_event(envelope)
        return DurableResult(status, (receipt,) if receipt else ())


class OutboxRelay:
    """One finite pass; caller schedules retries/backoff and owns replay cursor.

    On reconnect/Redis rollback start replay at zero. Sent records and receipts
    remain in SQLite, and retransmission never allocates an execution attempt.
    A relay is scoped to one instance/epoch; other envelopes are not marked sent.
    """

    def __init__(self, state, transport: Transport, *, fault: Callable = lambda _: None):
        self.state, self.transport, self.fault = state, transport, fault

    def flush(self, *, replay=False, after_sequence=0, page_size=100, max_pages=10):
        if type(max_pages) is not int or not 1 <= max_pages <= 100:
            raise TransportError("invalid_batch_limit")
        cursor, sent = after_sequence, 0
        for _ in range(max_pages):
            page = self.state.outbox_page(page_size, after_sequence=cursor, replay=replay, identity={
                "server_id": self.transport.identity.server_id,
                "instance_id": self.transport.identity.instance_id,
                "worker_epoch": self.transport.identity.worker_epoch,
            })
            for envelope in page["items"]:
                if not self.transport.identity.matches(envelope):
                    continue
                self.transport.publish(envelope)
                self.fault("relay.after_publish")
                try:
                    marked = self.state.mark_delivered(envelope["message_id"], digest(envelope))
                except Exception:
                    raise TransportError("outbox_commit_failed", retryable=True) from None
                if not marked:
                    raise TransportError("outbox_identity_conflict")
                sent += 1
            cursor = page["next_cursor"]
            if not page["has_more"]:
                break
        return {"sent": sent, "next_cursor": cursor, "has_more": page["has_more"]}


class DeliveryPump:
    """No background thread or retry forever. Control uses its own pump/lane."""

    def __init__(self, transport: Transport, handler: DurableHandler, *, clock=time.monotonic,
                 fault: Callable = lambda _: None):
        self.transport, self.handler, self.clock, self.fault = transport, handler, clock, fault

    def once(self, lane, *, count=20, block_ms=100):
        start = self.clock()
        applied = quarantined = 0
        for delivery in self.transport.read(lane, count=count, block_ms=block_ms):
            if delivery.error_code:
                self.transport.quarantine(delivery, delivery.error_code)
                quarantined += 1
                continue
            try:
                result = self.handler(delivery.envelope)
            except (ProtocolError, TaskStateError) as error:
                if isinstance(error, TaskStateError) and error.code == "old_epoch_unreconciled":
                    raise TransportError("awaiting_epoch_reconciliation", retryable=True) from None
                input_errors = {"unsupported_task_event", "wrong_server", "event_identity_conflict",
                                "event_sequence_conflict", "event_attempt_mismatch", "reservation_identity_conflict"}
                if isinstance(error, TaskStateError) and error.code not in input_errors:
                    raise TransportError("durable_integrity_failed") from None
                self.transport.quarantine(delivery, error.code)
                quarantined += 1
                continue
            except TransportError:
                raise
            except Exception:
                # Do not ACK if the durable receiver cannot commit. Never print
                # exception text which may contain SQL, message data or secrets.
                raise TransportError("durable_commit_failed", retryable=True) from None
            if not isinstance(result, DurableResult) or result.status not in {"applied", "pending", "stale", "journaled"}:
                raise TransportError("invalid_durable_result")
            if delivery.envelope["type"] not in EVENT_TYPES and result.receipts:
                raise TransportError("recursive_receipt_forbidden")
            for receipt in result.receipts:
                validate_envelope(receipt)
                if not receipt_matches(receipt, delivery.envelope):
                    raise TransportError("receipt_identity_conflict")
            self.fault("pump.after_commit")
            for receipt in result.receipts:
                self.transport.publish(receipt)
            self.transport.ack(delivery)
            applied += 1
        return {"applied": applied, "quarantined": quarantined, "elapsed_seconds": max(0, self.clock() - start)}
