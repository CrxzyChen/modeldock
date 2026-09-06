"""MediaCenter Worker Envelope v1, JSON-only and transport-independent.

This module validates structure, values and declared capabilities, not authority.
It does not authorize reservations, fence epochs, persist sequence/operation
numbers, deduplicate messages or look up assets. A valid message still needs all
of those runtime checks. Expiry limits first acceptance, not journal queries;
parsing never compares timestamps with the wall clock or drops late events.
Registered capability_digest is shape-checked only; matching it against the
trusted bound capability belongs to the later registration handshake.
"""
from __future__ import annotations

import copy
import json
import math
import re
from datetime import datetime
from typing import Any

from .capabilities import (WorkerCapabilityError, validate_worker_capability,
                           validate_worker_request, worker_capability_for)

PROTOCOL = "mc.worker/1"
MAX_MESSAGE_BYTES = 256 * 1024
MAX_DEPTH = 16
MAX_NODES = 16384
MAX_EXTENSION_BYTES = 4096
MAX_INTEGER = 2**53 - 1
COMMAND_TYPES = frozenset({"task.execute", "model.load", "model.unload", "task.cancel",
                           "worker.snapshot.request", "event.receipt"})
EVENT_TYPES = frozenset({"worker.registered", "task.accepted", "phase.changed", "task.terminal",
                        "model.accepted", "model.terminal", "worker.snapshot"})
TELEMETRY_TYPES = frozenset({"telemetry.heartbeat", "telemetry.progress"})
_COMMON = {"protocol", "type", "message_id", "server_id", "instance_id", "worker_epoch",
           "correlation_id", "created_at", "payload"}
_TASK_TYPES = {"task.execute", "task.cancel", "task.accepted", "task.terminal", "telemetry.progress"}
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_GPU = re.compile(r"GPU-[A-Za-z0-9-]{1,80}\Z")
_PHASES = {"preparing", "loading", "generating", "encoding", "saving", "unloading"}
_PROGRESS_PHASES = _PHASES | {"sampling", "decoding", "synthesizing", "upscaling"}
_WORKER_STATES = {"starting", "online_unloaded", "loading", "ready", "busy", "unloading",
                  "stopping", "stopped", "error"}
_TASK_STATES = {"accepted", "running", "cancel_requested", "succeeded", "failed", "canceled", "interrupted"}


class ProtocolError(ValueError):
    """A stable code without input values, field names, paths or credentials."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ProtocolError(code)


def _object(value: Any, required: set[str], optional: set[str] | None = None) -> None:
    _require(type(value) is dict, "invalid_object")
    _require(required <= value.keys(), "missing_field")
    _require(value.keys() <= required | (optional or set()), "unknown_field")


def _token(value: Any) -> None:
    _require(type(value) is str and _TOKEN.fullmatch(value) is not None
             and ".." not in value, "invalid_identifier")


def _integer(value: Any, minimum: int = 0, maximum: int = MAX_INTEGER) -> None:
    _require(type(value) is int and minimum <= value <= maximum, "invalid_integer")


def _enum(value: Any, choices: set[str] | frozenset[str]) -> None:
    _require(type(value) is str and value in choices, "invalid_enum")


def _timestamp(value: Any) -> datetime:
    _require(type(value) is str and _TIME.fullmatch(value) is not None, "invalid_timestamp")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ProtocolError("invalid_timestamp") from None


def _json_tree(value: Any) -> None:
    # Iterative, bounded validation also catches cycles, subclasses, bytes,
    # non-string keys and arbitrarily large integers before serialization.
    stack = [(value, 1)]
    nodes = 0
    text_bytes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        _require(nodes <= MAX_NODES, "too_many_values")
        _require(depth <= MAX_DEPTH, "too_deep")
        kind = type(item)
        if kind is dict:
            _require(len(item) <= MAX_NODES, "too_many_values")
            for key, child in item.items():
                _require(type(key) is str, "non_json_value")
                stack.append((key, depth + 1))
                stack.append((child, depth + 1))
        elif kind is list:
            _require(len(item) <= MAX_NODES, "too_many_values")
            stack.extend((child, depth + 1) for child in item)
        elif kind is str:
            try:
                text_bytes += len(item.encode("utf-8"))
            except UnicodeError:
                raise ProtocolError("invalid_utf8") from None
            _require(text_bytes <= MAX_MESSAGE_BYTES, "message_too_large")
        elif kind is float:
            _require(math.isfinite(item), "non_finite_number")
        elif kind is int:
            _require(abs(item) <= MAX_INTEGER, "invalid_integer")
        else:
            _require(kind in (bool, type(None)), "non_json_value")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, "duplicate_key")
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise ProtocolError("non_finite_number")


def parse_envelope(raw: str | bytes, *, capabilities: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Parse strict UTF-8 JSON. Optional capabilities are trusted caller inputs.

    Never source that mapping from an envelope or untrusted model metadata.
    Error text intentionally excludes the JSON decoder's input-bearing details.
    """
    _require(type(raw) in (str, bytes), "non_json_value")
    try:
        data = raw.encode("utf-8") if type(raw) is str else raw
        _require(len(data) <= MAX_MESSAGE_BYTES, "message_too_large")
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except UnicodeError:
        raise ProtocolError("invalid_utf8") from None
    except RecursionError:
        raise ProtocolError("too_deep") from None
    except (ValueError, OverflowError) as error:
        if isinstance(error, ProtocolError):
            raise
        raise ProtocolError("invalid_json") from None
    return validate_envelope(value, capabilities=capabilities)


def _operation(payload: dict[str, Any]) -> None:
    _token(payload["operation_id"])
    _integer(payload["desired_revision"], 1)


def _reservation(payload: dict[str, Any]) -> None:
    _token(payload["reservation_id"])
    _integer(payload["reservation_generation"], 1)


def _model(payload: dict[str, Any], capabilities: dict[str, dict[str, Any]] | None) -> dict[str, Any]:
    for key in ("model_key", "recipe_revision", "model_asset_id", "model_asset_revision"):
        _token(payload[key])
    if capabilities is None:
        return worker_capability_for(payload["model_key"])
    _require(type(capabilities) is dict and payload["model_key"] in capabilities, "unknown_model")
    contract = capabilities[payload["model_key"]]
    validate_worker_capability(contract)
    _require(contract["model_key"] == payload["model_key"], "model_mismatch")
    return contract


def _terminal(payload: dict[str, Any], *, model: bool = False) -> None:
    _enum(payload["status"], {"succeeded", "failed", "canceled", "interrupted"})
    error = payload["error_code"]
    if payload["status"] == "succeeded":
        _require(error is None, "invalid_terminal")
    else:
        _token(error)
    if not model:
        manifest = payload["manifest"]
        if payload["status"] == "succeeded":
            _object(manifest, {"asset_id", "revision", "sha256"})
            _token(manifest["asset_id"])
            _token(manifest["revision"])
            _require(type(manifest["sha256"]) is str and _SHA.fullmatch(manifest["sha256"]) is not None,
                     "invalid_digest")
        else:
            _require(manifest is None, "invalid_terminal")


def _payload(message: dict[str, Any], capabilities: dict[str, dict[str, Any]] | None) -> None:
    kind, payload = message["type"], message["payload"]
    operation = {"operation_id", "desired_revision"}
    reservation = {"reservation_id", "reservation_generation"}
    model = {"model_key", "recipe_revision", "model_asset_id", "model_asset_revision"}
    if kind == "task.execute":
        _object(payload, model | reservation | {"operation", "parameters", "inputs", "loras"})
        _reservation(payload)
        contract = _model(payload, capabilities)
        validate_worker_request(payload["model_key"], payload["operation"], payload["parameters"],
                                payload["inputs"], payload["loras"], capability=contract)
    elif kind == "model.load":
        _object(payload, operation | reservation | model | {"residency"})
        _operation(payload)
        _reservation(payload)
        _model(payload, capabilities)
        _enum(payload["residency"], {"on_demand", "idle", "resident"})
    elif kind in {"model.unload", "worker.snapshot.request"}:
        _object(payload, operation)
        _operation(payload)
    elif kind == "task.cancel":
        _object(payload, {"cancel_revision"})
        _integer(payload["cancel_revision"], 1)
    elif kind == "event.receipt":
        _require(type(payload) is dict, "invalid_object")
        _enum(payload.get("subject"), {"task", "operation", "worker"})
        identity = ({"task_id", "attempt_id"} if payload["subject"] == "task"
                    else operation if payload["subject"] == "operation" else set())
        _object(payload, {"subject", "event_message_id", "event_seq"} | identity)
        _token(payload["event_message_id"])
        _integer(payload["event_seq"], 1)
        if payload["subject"] == "task":
            _token(payload["task_id"])
            _token(payload["attempt_id"])
        elif payload["subject"] == "operation":
            _operation(payload)
        # Worker receipts acknowledge instance-level reliable events (e.g.
        # registered), bound by top-level instance_id/worker_epoch. Runtime
        # must match journal event type, instance and epoch before persisting
        # acknowledgement. This validator does not perform that lookup.
    elif kind == "worker.registered":
        _object(payload, {"model_key", "recipe_revision", "image_digest", "capability_digest", "gpu_uuids"})
        _token(payload["model_key"])
        _token(payload["recipe_revision"])
        contract = (worker_capability_for(payload["model_key"]) if capabilities is None
                    else capabilities.get(payload["model_key"]))
        _require(contract is not None, "unknown_model")
        validate_worker_capability(contract)
        _require(contract["model_key"] == payload["model_key"], "model_mismatch")
        image = payload["image_digest"]
        _require(type(image) is str and image.startswith("sha256:")
                 and _SHA.fullmatch(image[7:]) is not None, "invalid_digest")
        digest = payload["capability_digest"]
        _require(type(digest) is str and _SHA.fullmatch(digest) is not None, "invalid_digest")
        uuids = payload["gpu_uuids"]
        _require(type(uuids) is list and 1 <= len(uuids) <= 16, "invalid_gpu_binding")
        _require(all(type(uuid) is str and _GPU.fullmatch(uuid) is not None for uuid in uuids), "invalid_gpu_binding")
        _require(len(set(uuids)) == len(uuids), "invalid_gpu_binding")
    elif kind == "task.accepted":
        _object(payload, reservation)
        _reservation(payload)
    elif kind == "model.accepted":
        _object(payload, operation | {"action"}, reservation)
        _operation(payload)
        _enum(payload["action"], {"load", "unload"})
        if payload["action"] == "load":
            _require(reservation <= payload.keys(), "missing_field")
            _reservation(payload)
        else:
            _require(not reservation & payload.keys(), "unknown_field")
    elif kind == "phase.changed":
        is_task = "task_id" in message
        _object(payload, {"phase"} | (set() if is_task else operation))
        _enum(payload["phase"], _PHASES)
        if not is_task:
            _operation(payload)
            _enum(payload["phase"], {"loading", "unloading"})
    elif kind == "task.terminal":
        _object(payload, {"status", "error_code", "manifest"})
        _terminal(payload)
    elif kind == "model.terminal":
        _object(payload, operation | {"action", "status", "error_code"}, reservation)
        _operation(payload)
        _enum(payload["action"], {"load", "unload"})
        if payload["action"] == "load":
            _require(reservation <= payload.keys(), "missing_field")
            _reservation(payload)
        else:
            _require(not reservation & payload.keys(), "unknown_field")
        _terminal(payload, model=True)
    elif kind == "worker.snapshot":
        _object(payload, operation | {"state", "tasks", "operations"})
        _operation(payload)
        _enum(payload["state"], _WORKER_STATES)
        for key in ("tasks", "operations"):
            records = payload[key]
            _require(type(records) is list and len(records) <= 128, "invalid_snapshot")
            seen = set()
            for record in records:
                identity = {"task_id", "attempt_id"} if key == "tasks" else operation
                _object(record, identity | {"worker_epoch", "state", "event_seq"})
                _token(record["worker_epoch"])
                _integer(record["event_seq"], 1)
                if key == "tasks":
                    _token(record["task_id"])
                    _token(record["attempt_id"])
                    _enum(record["state"], _TASK_STATES)
                    pair = (record["worker_epoch"], record["task_id"], record["attempt_id"])
                else:
                    _operation(record)
                    _enum(record["state"], {"accepted", "loading", "unloading", "succeeded", "failed", "canceled", "interrupted"})
                    pair = (record["worker_epoch"], record["operation_id"])
                _require(pair not in seen, "duplicate_snapshot_record")
                seen.add(pair)
    elif kind == "telemetry.heartbeat":
        _object(payload, {"state", "uptime_seconds"})
        _enum(payload["state"], _WORKER_STATES)
        _integer(payload["uptime_seconds"])
    elif kind == "telemetry.progress":
        _object(payload, {"phase", "completed", "total", "unit"})
        _enum(payload["phase"], _PROGRESS_PHASES)
        _enum(payload["unit"], {"steps", "frames", "samples", "tokens", "audio",
                                "chunks", "references", "tiles"})
        _integer(payload["completed"])
        _integer(payload["total"], 1)
        _require(payload["completed"] <= payload["total"], "invalid_progress")


def validate_execution_quiescence(proof: Any, terminal: dict) -> None:
    """Typed assertion only, not Controller authorization or process proof.

    The trusted Controller must additionally match its original command digest
    and active epoch/attempt. Adapters cannot supply this Supervisor assertion.
    """
    _require(terminal.get("type") == "task.terminal", "invalid_quiescence_subject")
    _require(type(proof) is dict, "invalid_quiescence")
    kind = proof.get("kind")
    _require(type(kind) is str and kind in {"never_started", "quiescent"}, "invalid_quiescence")
    fields = {"kind", "command_message_id", "command_digest", "server_id", "instance_id", "worker_epoch", "task_id", "attempt_id"}
    if kind == "quiescent":
        fields |= {"execution_token", "child_token"}
    _object(proof, fields)
    for field in fields - {"kind", "command_digest"}:
        _token(proof[field])
    _require(type(proof["command_digest"]) is str and bool(_SHA.fullmatch(proof["command_digest"])), "invalid_quiescence")
    _require(all(proof[key] == terminal[key] for key in ("server_id", "instance_id", "worker_epoch", "task_id", "attempt_id")), "quiescence_identity_conflict")
    if kind == "never_started":
        _require(terminal["payload"]["status"] in {"canceled", "failed"}, "invalid_quiescence_status")


def validate_envelope(value: Any, *, capabilities: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Validate and return a detached JSON object; no mutation or side effects."""
    _json_tree(value)
    _require(capabilities is None or type(capabilities) is dict, "invalid_capability")
    _require(type(value) is dict, "invalid_object")
    _require(value.get("protocol") == PROTOCOL, "unsupported_protocol")
    kind = value.get("type")
    _require(type(kind) is str and kind in COMMAND_TYPES | EVENT_TYPES | TELEMETRY_TYPES,
             "unknown_message_type")
    fields = set(_COMMON)
    if kind in _TASK_TYPES or kind == "phase.changed" and ("task_id" in value or "attempt_id" in value):
        fields |= {"task_id", "attempt_id"}
    if kind in COMMAND_TYPES:
        fields.add("expires_at")
    elif kind in EVENT_TYPES:
        fields.add("event_seq")
    else:
        fields.add("progress_seq" if kind == "telemetry.progress" else "telemetry_seq")
    _object(value, fields, {"extensions"})
    for field in {"message_id", "server_id", "instance_id", "worker_epoch", "correlation_id", "task_id", "attempt_id"} & fields:
        _token(value[field])
    created = _timestamp(value["created_at"])
    if kind in COMMAND_TYPES:
        _require(_timestamp(value["expires_at"]) > created, "invalid_expiry")
    for field in {"event_seq", "progress_seq", "telemetry_seq"} & fields:
        _integer(value[field], 1)
    if "extensions" in value:
        extensions = value["extensions"]
        _require(len(json.dumps(extensions, ensure_ascii=False).encode("utf-8")) <= MAX_EXTENSION_BYTES,
                 "extensions_too_large")
        # v1 reserves an object but only explicitly specified metadata is legal.
        # No extension can override parameters, asset resolution or authority.
        _object(extensions, set(), {"trace_id", "execution_quiescence"})
        if "trace_id" in extensions:
            _token(extensions["trace_id"])
    _require(len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= MAX_MESSAGE_BYTES,
             "message_too_large")
    try:
        _payload(value, capabilities)
    except WorkerCapabilityError as error:
        raise ProtocolError(error.code) from None
    if "execution_quiescence" in value.get("extensions", {}):
        validate_execution_quiescence(value["extensions"]["execution_quiescence"], value)
    return copy.deepcopy(value)
