from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from mediacenter.capabilities import worker_capability_for
from mediacenter.protocol import (
    COMMAND_TYPES, EVENT_TYPES, TELEMETRY_TYPES, MAX_MESSAGE_BYTES, MAX_INTEGER,
    ProtocolError, parse_envelope, validate_envelope,
)


FIXTURE = Path(__file__).parent / "fixtures" / "worker-envelope-v1.json"
MESSAGES = {item["name"]: item["envelope"] for item in json.loads(FIXTURE.read_text(encoding="utf-8"))["messages"]}


class WorkerProtocolTests(unittest.TestCase):
    def test_execution_quiescence_is_strict_terminal_only_and_identity_bound(self):
        terminal = next(copy.deepcopy(row) for row in MESSAGES.values() if row['type']=='task.terminal')
        proof = {key: terminal[key] for key in ('server_id','instance_id','worker_epoch','task_id','attempt_id')}
        proof.update(kind='quiescent',command_message_id='command-original',command_digest='a'*64,
                     execution_token='run-original',child_token='child-original')
        terminal['extensions']={'execution_quiescence':proof}
        self.assertEqual(validate_envelope(terminal),terminal)
        for field in proof:
            bad=copy.deepcopy(terminal); del bad['extensions']['execution_quiescence'][field]
            self.reject(bad)
        for field in ('task_id','attempt_id','worker_epoch','instance_id','server_id'):
            bad=copy.deepcopy(terminal); bad['extensions']['execution_quiescence'][field]='other'
            self.reject(bad,'quiescence_identity_conflict')
        for field,value in (('kind',[]),('command_digest','bad'),('command_message_id','/tmp/path'),('unknown',True)):
            bad=copy.deepcopy(terminal); bad['extensions']['execution_quiescence'][field]=value
            self.reject(bad)
        command=self.message(); command['extensions']=terminal['extensions']
        self.reject(command,'invalid_quiescence_subject')

    def test_never_started_has_no_execution_tokens_and_cannot_claim_success(self):
        terminal=next(copy.deepcopy(row) for row in MESSAGES.values() if row['type']=='task.terminal')
        proof={key:terminal[key] for key in ('server_id','instance_id','worker_epoch','task_id','attempt_id')}
        proof.update(kind='never_started',command_message_id='original',command_digest='a'*64)
        terminal['extensions']={'execution_quiescence':proof}
        terminal['payload']={'status':'canceled','error_code':'canceled','manifest':None}
        validate_envelope(terminal)
        for field in ('execution_token','child_token'):
            bad=copy.deepcopy(terminal); bad['extensions']['execution_quiescence'][field]='forbidden'
            self.reject(bad)
        terminal['payload']={'status':'succeeded','error_code':None,'manifest':{'asset_id':'a','revision':'r1','sha256':'a'*64}}
        self.reject(terminal,'invalid_quiescence_status')

    def message(self, name: str = "execute.image") -> dict:
        return copy.deepcopy(MESSAGES[name])

    def reject(self, message, code: str | None = None) -> None:
        with self.assertRaises(ProtocolError) as caught:
            validate_envelope(message)
        self.assertEqual(str(caught.exception), caught.exception.code)
        if code:
            self.assertEqual(caught.exception.code, code)

    def test_fixture_covers_every_command_event_and_telemetry(self):
        self.assertEqual({msg["type"] for msg in MESSAGES.values()}, COMMAND_TYPES | EVENT_TYPES | TELEMETRY_TYPES)
        for name, value in MESSAGES.items():
            with self.subTest(name=name):
                self.assertEqual(parse_envelope(json.dumps(value)), value)
                self.assertEqual(parse_envelope(json.dumps(value).encode()), value)

    def test_every_envelope_field_is_required_and_unknown_fields_are_rejected(self):
        for name, value in MESSAGES.items():
            for field in value:
                with self.subTest(name=name, missing=field):
                    message = copy.deepcopy(value)
                    del message[field]
                    self.reject(message)
            message = copy.deepcopy(value)
            message["API_KEY_SUPER_SECRET"] = "do not echo"
            self.reject(message, "unknown_field")

    def test_every_payload_field_is_required_and_control_injection_rejected(self):
        for name, value in MESSAGES.items():
            for field in value["payload"]:
                with self.subTest(name=name, missing=field):
                    message = copy.deepcopy(value)
                    del message["payload"][field]
                    self.reject(message)
            for field in ("path", "url", "api_key", "shell", "pickle", "mounts"):
                message = copy.deepcopy(value)
                message["payload"][field] = "private-content"
                self.reject(message, "unknown_field")

    def test_unknown_major_type_and_old_snapshot_ambiguity_fail(self):
        for version in ("mc.worker/0", "mc.worker/2", "mc.worker/1.1", 1, None):
            message = self.message()
            message["protocol"] = version
            self.reject(message, "unsupported_protocol")
        for kind in ("worker.snapshot.reply", "task.exec", [], "__import__"):
            message = self.message()
            message["type"] = kind
            self.reject(message, "unknown_message_type")
        request = self.message("snapshot.request")
        request["type"] = "worker.snapshot"
        self.reject(request)

    def test_duplicate_json_keys_rejected_at_any_depth_without_echo(self):
        for raw in ('{"secret-key":1,"secret-key":2}',
                    '{"a":{"password":"secret","password":"other"}}'):
            with self.assertRaises(ProtocolError) as caught:
                parse_envelope(raw)
            self.assertEqual(str(caught.exception), "duplicate_key")

    def test_invalid_json_utf8_and_nonfinite_wire_values(self):
        for raw, code in (("{secret", "invalid_json"), (b"\xff", "invalid_utf8"),
                          ('{"x":NaN}', "non_finite_number"), ('{"x":Infinity}', "non_finite_number"),
                          ('{"x":-Infinity}', "non_finite_number"), ('{"x":1e999}', "non_finite_number"),
                          ('{"x":"\\ud800"}', "invalid_utf8"), ("\udfff", "invalid_utf8")):
            with self.subTest(raw=repr(raw)):
                with self.assertRaises(ProtocolError) as caught:
                    parse_envelope(raw)
                self.assertEqual(caught.exception.code, code)

    def test_wire_size_exact_boundary_and_unicode_bytes(self):
        raw = json.dumps(self.message()).encode()
        self.assertEqual(parse_envelope(raw + b" " * (MAX_MESSAGE_BYTES - len(raw))), self.message())
        with self.assertRaisesRegex(ProtocolError, "message_too_large"):
            parse_envelope(raw + b" " * (MAX_MESSAGE_BYTES - len(raw) + 1))
        with self.assertRaisesRegex(ProtocolError, "message_too_large"):
            parse_envelope("中" * (MAX_MESSAGE_BYTES // 3 + 1))

    def test_depth_nodes_cycles_and_non_json_python_objects(self):
        value = None
        for _ in range(18):
            value = [value]
        self.reject(value, "too_deep")
        with self.assertRaisesRegex(ProtocolError, "too_deep"):
            parse_envelope("[" * 1500 + "]" * 1500)
        self.reject([0] * 16385, "too_many_values")
        cyclic = []
        cyclic.append(cyclic)
        self.reject(cyclic, "too_deep")
        class CustomDict(dict):
            pass
        for value in (CustomDict(), (1, 2), b"json", {1: "key"}, object(), {"value": float("nan")},
                      {"value": float("inf")}, {"value": 2**10000}):
            with self.subTest(value=type(value)):
                self.reject(value)

    def test_no_wall_clock_expiry_drop_and_strict_utc_order(self):
        message = self.message()
        message["created_at"] = "2000-01-01T00:00:00Z"
        message["expires_at"] = "2000-01-01T00:00:01Z"
        self.assertEqual(validate_envelope(message), message)
        event = self.message("terminal.succeeded")
        event["created_at"] = "2000-01-01T00:00:00Z"
        self.assertEqual(validate_envelope(event), event)
        for timestamp in ("2026-02-30T12:00:00Z", "2026-08-31", "2026-08-31T12:00:00+08:00", 1):
            message = self.message()
            message["created_at"] = timestamp
            self.reject(message, "invalid_timestamp")
        for expiry in ("2026-08-31T12:00:00Z", "2026-08-31T11:59:59Z"):
            message = self.message()
            message["expires_at"] = expiry
            self.reject(message, "invalid_expiry")
        event["expires_at"] = "2000-01-01T00:00:01Z"
        self.reject(event, "unknown_field")

    def test_task_and_operation_phase_branches_are_exclusive(self):
        message = self.message("phase.task")
        message["payload"].update(operation_id="op-1", desired_revision=1)
        self.reject(message, "unknown_field")
        message = self.message("phase.operation")
        message.update(task_id="task-1", attempt_id="attempt-1")
        self.reject(message, "unknown_field")
        message = self.message("phase.operation")
        message["payload"]["phase"] = "generating"
        self.reject(message, "invalid_enum")
        for name in ("load", "unload", "snapshot.request", "registered", "heartbeat"):
            message = self.message(name)
            message.update(task_id="task-1", attempt_id="attempt-1")
            self.reject(message, "unknown_field")

    def test_receipt_identity_is_not_ambiguous(self):
        for name in ("receipt.task", "receipt.operation"):
            message = self.message(name)
            message["payload"].update(task_id="task-1", attempt_id="attempt-1", operation_id="op-1", desired_revision=1)
            self.reject(message, "unknown_field")
        message = self.message("receipt.task")
        message["payload"]["subject"] = "unknown"
        self.reject(message, "invalid_enum")

    def test_worker_receipt_closes_registered_event_without_mixed_identities(self):
        message = self.message("receipt.worker")
        self.assertEqual(validate_envelope(message), message)
        self.assertEqual(message["instance_id"], MESSAGES["registered"]["instance_id"])
        self.assertEqual(message["worker_epoch"], MESSAGES["registered"]["worker_epoch"])
        self.assertEqual(message["payload"]["event_message_id"], MESSAGES["registered"]["message_id"])
        self.assertEqual(message["payload"]["event_seq"], MESSAGES["registered"]["event_seq"])
        for field, value in (("task_id", "task-1"), ("attempt_id", "attempt-1"),
                             ("operation_id", "op-1"), ("desired_revision", 1)):
            message = self.message("receipt.worker")
            message["payload"][field] = value
            self.reject(message, "unknown_field")
        message = self.message("receipt.worker")
        message.update(task_id="task-1", attempt_id="attempt-1")
        self.reject(message, "unknown_field")

    def test_identity_cannot_be_a_path_url_or_credential(self):
        for field in ("message_id", "server_id", "instance_id", "worker_epoch", "task_id", "attempt_id"):
            for value in ("../private", "/tmp/asset", "C:\\secret", "https://host", "Bearer secret", "..", "", 0):
                message = self.message()
                message[field] = value
                self.reject(message, "invalid_identifier")
        for field in ("recipe_revision", "model_asset_id", "model_asset_revision", "reservation_id"):
            message = self.message()
            message["payload"][field] = "https://secret"
            self.reject(message, "invalid_identifier")

    def test_sequences_and_resource_generations_are_positive_safe_integers(self):
        for name, field in (("phase.task", "event_seq"), ("progress", "progress_seq"), ("heartbeat", "telemetry_seq")):
            for value in (0, -1, True, 1.0, MAX_INTEGER + 1):
                message = self.message(name)
                message[field] = value
                self.reject(message, "invalid_integer")
        for field in ("desired_revision", "reservation_generation"):
            for value in (0, -1, True, 1.0):
                message = self.message("load")
                message["payload"][field] = value
                self.reject(message, "invalid_integer")

    def test_model_load_requires_an_explicit_residency_mode(self):
        message = self.message("load")
        self.assertEqual(validate_envelope(message)["payload"]["residency"], "resident")
        for value in ("cpu", "warm", "", None, 1):
            with self.subTest(value=value):
                invalid = self.message("load")
                invalid["payload"]["residency"] = value
                self.reject(invalid, "invalid_enum")

    def test_extensions_are_small_explicit_metadata_not_an_escape_hatch(self):
        message = self.message()
        message["extensions"] = {"trace_id": "trace-1"}
        validate_envelope(message)
        for extensions, code in (({"parameters": {}}, "unknown_field"),
                                 ({"api_key": "secret"}, "unknown_field"),
                                 ({"trace_id": "x" * 4096}, "extensions_too_large"),
                                 ([], "invalid_object"), ({"trace_id": "/tmp"}, "invalid_identifier")):
            message["extensions"] = extensions
            self.reject(message, code)

    def test_unknown_model_operation_parameters_and_unsupported_features(self):
        for field, value, code in (("model_key", "unknown-model", "unknown_model"),
                                   ("operation", "video.generate", "unsupported_operation"),
                                   ("parameters", {"prompt": "x", "path": "/tmp"}, "unknown_parameter"),
                                   ("loras", [{"asset_id": "lora-1"}], "invalid_lora"),
                                   ("inputs", [{"asset_id": "input-1"}], "unsupported_inputs")):
            message = self.message()
            message["payload"][field] = value
            self.reject(message, code)

    def test_prompt_is_data_not_a_control_blacklist(self):
        message = self.message()
        message["payload"]["parameters"]["prompt"] = 'Draw /etc/passwd, https://host, API Key and __import__("os").'
        self.assertEqual(validate_envelope(message), message)

    def test_reference_asset_revision_type_and_count(self):
        for field in ("asset_id", "revision", "media_type"):
            message = self.message("execute.reference")
            del message["payload"]["inputs"][0][field]
            self.reject(message, "invalid_input")
        for media in ("audio/wav", "image/../../key", "image/", "image/png;token=secret"):
            message = self.message("execute.reference")
            message["payload"]["inputs"][0]["media_type"] = media
            self.reject(message, "unsupported_media")
        message = self.message("execute.reference")
        message["payload"]["inputs"] *= 2
        self.reject(message, "unsupported_inputs")

    def test_terminal_manifest_and_error_are_status_bound(self):
        message = self.message("terminal.succeeded")
        message["payload"]["error_code"] = "failure"
        self.reject(message, "invalid_terminal")
        message = self.message("terminal.failed")
        message["payload"]["manifest"] = self.message("terminal.succeeded")["payload"]["manifest"]
        self.reject(message, "invalid_terminal")
        message = self.message("terminal.succeeded")
        message["payload"]["manifest"]["path"] = "/tmp/result"
        self.reject(message, "unknown_field")
        message = self.message("terminal.failed")
        message["payload"]["error_code"] = "private error\nAPI key"
        self.reject(message, "invalid_identifier")

    def test_load_event_binding_and_unload_no_reservation(self):
        for name in ("accepted.load", "terminal.load"):
            message = self.message(name)
            del message["payload"]["reservation_generation"]
            self.reject(message, "missing_field")
        for name in ("accepted.unload", "terminal.unload"):
            message = self.message(name)
            message["payload"]["reservation_id"] = "res-1"
            self.reject(message, "unknown_field")

    def test_snapshot_records_bounded_unique_and_allow_old_epoch_for_reconciliation(self):
        message = self.message("snapshot")
        validate_envelope(message)
        for key in ("tasks", "operations"):
            message = self.message("snapshot")
            message["payload"][key] *= 2
            self.reject(message, "duplicate_snapshot_record")
            message["payload"][key] *= 65
            self.reject(message, "invalid_snapshot")
        message = self.message("snapshot")
        del message["payload"]["tasks"][0]["worker_epoch"]
        self.reject(message, "missing_field")

    def test_terminal_states_are_expressible_in_recovery_snapshots(self):
        for kind, name, records in (("model.terminal", "terminal.load", "operations"),
                                     ("model.terminal", "terminal.unload", "operations"),
                                     ("task.terminal", "terminal.succeeded", "tasks")):
            for status in ("succeeded", "failed", "canceled", "interrupted"):
                with self.subTest(kind=kind, action=name, status=status):
                    terminal = self.message(name)
                    terminal["payload"]["status"] = status
                    terminal["payload"]["error_code"] = None if status == "succeeded" else "worker_" + status
                    if kind == "task.terminal" and status != "succeeded":
                        terminal["payload"]["manifest"] = None
                    validate_envelope(terminal)
                    snapshot = self.message("snapshot")
                    snapshot["payload"][records][0]["state"] = status
                    validate_envelope(snapshot)

    def test_all_reliable_event_families_have_a_receipt_shape(self):
        for name, event in MESSAGES.items():
            if event["type"] not in EVENT_TYPES:
                continue
            with self.subTest(event=name):
                if "task_id" in event:
                    receipt = self.message("receipt.task")
                    receipt["payload"].update(task_id=event["task_id"], attempt_id=event["attempt_id"])
                elif "operation_id" in event["payload"]:
                    receipt = self.message("receipt.operation")
                    receipt["payload"].update(operation_id=event["payload"]["operation_id"],
                                              desired_revision=event["payload"]["desired_revision"])
                else:
                    receipt = self.message("receipt.worker")
                receipt.update(instance_id=event["instance_id"], worker_epoch=event["worker_epoch"])
                receipt["payload"].update(event_message_id=event["message_id"], event_seq=event["event_seq"])
                validate_envelope(receipt)

    def test_progress_actual_counts_and_independent_sequence(self):
        message = self.message("progress")
        message["payload"]["completed"] = 26
        self.reject(message, "invalid_progress")
        message = self.message("progress")
        message["payload"]["total"] = 0
        self.reject(message, "invalid_integer")
        message = self.message("progress")
        message["event_seq"] = 1
        self.reject(message, "unknown_field")

    def test_progress_accepts_every_media_adapter_unit(self):
        for unit in ("steps", "frames", "samples", "tokens", "audio", "chunks",
                     "references", "tiles"):
            with self.subTest(unit=unit):
                message = self.message("progress")
                message["payload"]["unit"] = unit
                validate_envelope(message)
        message = self.message("progress")
        message["payload"]["unit"] = "percent"
        self.reject(message, "invalid_enum")

    def test_progress_accepts_every_media_adapter_phase(self):
        for phase in ("preparing", "loading", "generating", "encoding", "saving",
                      "unloading", "sampling", "decoding", "synthesizing", "upscaling"):
            with self.subTest(phase=phase):
                message = self.message("progress")
                message["payload"]["phase"] = phase
                validate_envelope(message)
        message = self.message("progress")
        message["payload"]["phase"] = "complete"
        self.reject(message, "invalid_enum")

    def test_explicit_fake_lora_capability_is_detached_from_global_contract(self):
        contract = worker_capability_for("sdxl-base-1.0")
        contract["model_key"] = "fixture-model"
        contract["lora"] = {"supported": True, "maximum": 2, "minimum_weight": -1, "maximum_weight": 2}
        message = self.message()
        message["payload"]["model_key"] = "fixture-model"
        message["payload"]["loras"] = [{"asset_id": "lora-1", "revision": "r1", "family": "sdxl", "weight": 0.7}]
        self.reject(message, "unknown_model")
        self.assertEqual(validate_envelope(message, capabilities={"fixture-model": contract}), message)
        base = worker_capability_for("sdxl-base-1.0")["lora"]
        self.assertTrue(base["supported"])
        self.assertEqual(base["maximum"], 1)

    def test_return_value_is_detached_and_validation_does_not_mutate(self):
        message = self.message()
        validated = validate_envelope(message)
        validated["payload"]["parameters"]["prompt"] = "changed"
        self.assertEqual(message, self.message())

    def test_type_mutations_never_leak_native_exceptions_or_input(self):
        values = (None, False, [], {}, 0, 0.5, "sensitive-secret")
        for name, original in MESSAGES.items():
            for container in (None, "payload"):
                for key in original if container is None else original[container]:
                    for value in values:
                        message = copy.deepcopy(original)
                        target = message if container is None else message[container]
                        target[key] = copy.deepcopy(value)
                        with self.subTest(name=name, container=container, key=key, value=value):
                            try:
                                validate_envelope(message)
                            except ProtocolError as error:
                                self.assertEqual(str(error), error.code)
                                self.assertNotIn("sensitive-secret", str(error))

    def test_capability_registry_is_explicit_and_must_match_model(self):
        message = self.message()
        for registry in ({}, {"sdxl-base-1.0": None}, [], {"sdxl-base-1.0": {}}):
            with self.assertRaises(ProtocolError):
                validate_envelope(message, capabilities=registry)
        contract = worker_capability_for("sdxl-base-1.0")
        contract["model_key"] = "different-model"
        with self.assertRaisesRegex(ProtocolError, "model_mismatch"):
            validate_envelope(message, capabilities={"sdxl-base-1.0": contract})


if __name__ == "__main__":
    unittest.main()
