from __future__ import annotations
from tests.test_resident_policy import fixture_capacity

import copy
import unittest
from unittest.mock import patch
from tests import test_task_state as fixture
from mediacenter.task_state import TaskStateError, digest
from mediacenter.capabilities import worker_capability_for


class TaskOutboxTests(unittest.TestCase):
    setUp = fixture.TaskStateTests.setUp
    tearDown = fixture.TaskStateTests.tearDown
    accepted = fixture.TaskStateTests.accepted
    dispatched = fixture.TaskStateTests.dispatched
    event = fixture.TaskStateTests.event
    seal = fixture.TaskStateTests.seal
    count = fixture.TaskStateTests.count

    def test_out_of_order_terminal_waits_for_gap_and_commits_once(self):
        task, command = self.dispatched()
        manifest = self.seal(command)
        terminal = self.event(command, 3, "task.terminal", {"status": "succeeded", "error_code": None, "manifest": manifest})
        self.assertEqual(self.state.receive(terminal), "pending")
        self.assertIsNone(self.repository.authorized_artifact("image/one.png"))
        self.assertEqual(len(self.state.outbox()), 1)
        phase = self.event(command, 2, "phase.changed", {"phase": "generating"})
        self.assertEqual(self.state.receive(phase), "pending")
        self.state.receive(self.event(command))
        self.assertEqual(self.repository.get_task(task["id"])["status"], "succeeded")
        self.assertEqual(self.count("task_artifacts"), 1)
        self.assertEqual(self.count("task_outbox"), 4)
        self.state.receive(terminal)
        self.assertEqual(self.count("task_outbox"), 4)
        self.assertEqual(self.count("task_artifacts"), 1)

    def test_untrusted_worker_manifest_waits_for_controller_seal(self):
        task, command = self.dispatched()
        manifest = {"asset_id": "art-one", "revision": "v1", "sha256": "a" * 64}
        terminal = self.event(command, kind="task.terminal", payload={"status": "succeeded", "error_code": None, "manifest": manifest})
        self.assertEqual(self.state.receive(terminal), "pending")
        self.assertEqual(self.repository.get_task(task["id"])["status"], "assigned")
        self.assertEqual(self.count("task_artifacts"), 0)
        self.state.record_sealed_artifact(task["id"], command["attempt_id"], cancel_revision=0,
            asset_id="art-one", revision="v1", sha256="a" * 64, relative_path="image/one.png", byte_size=2)
        self.assertEqual(self.repository.get_task(task["id"])["status"], "succeeded")
        self.assertEqual(self.count("task_artifacts"), 1)

    def test_every_event_transaction_failure_preserves_retriability(self):
        task, command = self.dispatched()
        manifest = self.seal(command)
        terminal = self.event(command, kind="task.terminal", payload={"status": "succeeded", "error_code": None, "manifest": manifest})
        for point in ("event.inbox", "event.manifest", "event.terminal", "event.receipt", "event.applied"):
            with self.subTest(point=point):
                self.state.fault = lambda stage: (_ for _ in ()).throw(RuntimeError(stage)) if stage == point else None
                with self.assertRaises(RuntimeError):
                    self.state.receive(terminal)
                self.assertEqual(self.repository.get_task(task["id"])["status"], "assigned")
                self.assertEqual(self.count("task_inbox"), 0)
                self.assertEqual(self.count("task_artifacts"), 0)
                self.assertEqual(self.count("task_outbox"), 1)
        self.state.fault = lambda _: None
        self.assertEqual(self.state.receive(terminal), "applied")

    def test_event_content_id_sequence_and_epoch_mismatch_fail_closed(self):
        _, command = self.dispatched()
        original = self.event(command)
        self.state.receive(original)
        changed = copy.deepcopy(original)
        changed["created_at"] = "2021-01-01T00:00:00Z"
        with self.assertRaisesRegex(TaskStateError, "event_identity_conflict"):
            self.state.receive(changed)
        changed = copy.deepcopy(original); changed["message_id"] = "new-id"
        with self.assertRaisesRegex(TaskStateError, "event_sequence_conflict"):
            self.state.receive(changed)
        changed = self.event(command, 2); changed["worker_epoch"] = "imposter"
        with self.assertRaisesRegex(TaskStateError, "event_attempt_mismatch"):
            self.state.receive(changed)

    def test_broker_send_is_not_business_ack_and_replay_is_stable(self):
        _, command = self.dispatched()
        self.assertTrue(self.state.mark_delivered(command["message_id"], digest(command)))
        self.assertEqual(self.state.outbox(), [])
        self.assertEqual([item for item in self.state.outbox(replay=True) if item.get("task_id") == command["task_id"]], [command])
        with self.repository._connect() as db:
            self.assertIsNone(db.execute("SELECT acknowledged_at FROM task_outbox").fetchone()[0])
        self.state.receive(self.event(command))
        with self.repository._connect() as db:
            self.assertIsNotNone(db.execute("SELECT acknowledged_at FROM task_outbox WHERE message_id=?", (command["message_id"],)).fetchone()[0])
        self.assertEqual(self.count("task_attempts"), 1)

    def test_replay_pages_over_one_hundred_same_timestamp_records(self):
        task, command = self.dispatched()
        with patch("mediacenter.task_state.now", return_value="2030-01-01T00:00:00Z"):
            with self.repository._connect() as db:
                for index in range(105):
                    message = copy.deepcopy(command)
                    message["message_id"] = f"replay-message-{index}"
                    self.state._outbox(db, message, task["id"], command["attempt_id"])
        with self.repository._connect() as db:
            db.execute("UPDATE task_outbox SET delivered_at='sent'")
            # Initial ready_instance includes a completed model.load. Preserve
            # it as history, but only commands still needing execution and all
            # durable receipts belong in the replay stream.
            expected_ids = {command['message_id']} | {f'replay-message-{index}' for index in range(105)}
            expected_ids.update(row[0] for row in db.execute(
                "SELECT message_id FROM task_outbox WHERE json_extract(envelope_json,'$.type')='event.receipt'"))
            last_sequence = db.execute('SELECT MAX(sequence) FROM task_outbox').fetchone()[0]
            original_rows = [tuple(row) for row in db.execute('SELECT * FROM task_outbox ORDER BY sequence')]
        cursor, messages = 0, []
        while True:
            page = self.state.outbox_page(30, after_sequence=cursor, replay=True)
            messages.extend(page["items"])
            cursor = page["next_cursor"]
            if not page["has_more"]:
                break
        self.assertEqual({item['message_id'] for item in messages}, expected_ids)
        self.assertEqual(len(messages), len(expected_ids))
        self.assertEqual(cursor, last_sequence)
        self.assertEqual(self.state.outbox_page(after_sequence=cursor, replay=True)["items"], [])
        with self.repository._connect() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM task_outbox ORDER BY sequence')], original_rows)

    def test_outbox_page_filters_identity_before_pagination(self):
        _, command = self.dispatched()
        with self.repository._connect() as db:
            cursor = db.execute("SELECT MAX(sequence) FROM task_outbox").fetchone()[0]
            for index in range(105):
                unrelated = copy.deepcopy(command)
                unrelated["message_id"] = f"unrelated-identity-{index}"
                unrelated["instance_id"] = "another-instance"
                self.state._outbox(db, unrelated, unrelated["task_id"], unrelated["attempt_id"])
            matching = copy.deepcopy(command)
            matching["message_id"] = "matching-after-unrelated-history"
            self.state._outbox(db, matching, matching["task_id"], matching["attempt_id"])
        identity = {key: command[key] for key in ("server_id", "instance_id", "worker_epoch")}
        page = self.state.outbox_page(1, after_sequence=cursor, replay=True, identity=identity)
        self.assertEqual(page["items"], [matching])
        self.assertFalse(page["has_more"])

    def test_receipt_lookup_is_stable_indexed_and_independent_of_unrelated_history(self):
        _, command = self.dispatched()
        event = self.event(command)
        self.assertIsNone(self.state.receipt_for_event(event))
        self.state.receive(event)
        receipt = self.state.receipt_for_event(event)
        self.state.mark_delivered(receipt["message_id"], digest(receipt))
        with self.repository._connect() as db:
            for index in range(1200):
                item = copy.deepcopy(command)
                item["message_id"] = f"unrelated-{index}"
                self.state._outbox(db, item, item["task_id"], item["attempt_id"])
            # Exact lookup must use the existing message_id unique index, not
            # JSON extraction, a table scan, or a cursor chasing active writers.
            for table in ("task_inbox", "task_outbox"):
                plan = db.execute(f"EXPLAIN QUERY PLAN SELECT * FROM {table} WHERE message_id=?", ("missing",)).fetchall()
                self.assertTrue(any("SEARCH" in row[3] and "INDEX" in row[3] for row in plan))
        with patch.object(self.state, "outbox_page", side_effect=AssertionError("must not scan history")):
            self.state.receive(event)
            self.assertEqual(self.state.receipt_for_event(event), receipt)
        self.assertEqual(self.count("task_outbox"), 1202)
        changed = copy.deepcopy(event); changed["created_at"] = "2022-01-01T00:00:00Z"
        with self.assertRaisesRegex(TaskStateError, "event_identity_conflict"):
            self.state.receipt_for_event(changed)

    def test_pending_event_lookup_cannot_fabricate_a_receipt(self):
        _, command = self.dispatched()
        event = self.event(command, 2, "phase.changed", {"phase": "generating"})
        self.assertEqual(self.state.receive(event), "pending")
        self.assertIsNone(self.state.receipt_for_event(event))
        self.assertEqual(self.count("task_outbox"), 1)
        self.state.receive(self.event(command))
        receipt = self.state.receipt_for_event(event)
        self.assertEqual(receipt["payload"]["event_message_id"], event["message_id"])
        with self.repository._connect() as db:
            db.execute("UPDATE task_outbox SET digest='corrupt' WHERE message_id=?", (receipt["message_id"],))
        with self.assertRaisesRegex(TaskStateError, "receipt_integrity_error"):
            self.state.receipt_for_event(event)

    def test_accepted_reservation_identity_and_generation_are_checked_before_inbox(self):
        _, command = self.dispatched()
        original = self.event(command, 2)
        for field, value in (("reservation_id", "foreign"), ("reservation_generation", 999)):
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed["payload"][field] = value
                with patch.object(self.state, "_drain", side_effect=AssertionError("must reject before drain")):
                    with self.assertRaisesRegex(TaskStateError, "reservation_identity_conflict"):
                        self.state.receive(changed)
                self.assertEqual(self.count("task_inbox"), 0)
        self.assertEqual(self.state.receive(original), "pending")

    def test_per_task_inputs_are_not_instance_binding_or_previous_task_inputs(self):
        capability = worker_capability_for("sdxl-base-1.0")
        capability["reference_media"] = {"supported": True, "minimum": 1, "maximum": 1, "accept": ["image/"]}
        self.state.capabilities = {"sdxl-base-1.0": capability}
        for suffix in ("first", "second"):
            refs = [{"asset_id": "input-" + suffix, "revision": "rev-" + suffix, "media_type": "image/png"}]
            task = self.state.accept(dict(self.request, inputs=[refs[0]["asset_id"]]), scope="admin", binding=self.binding, input_bindings=refs)[0]
            command = self.state.dispatch(task["id"], 1, "instance-one", "epoch-one", {key: 20 for key in self.gpus}, observe_capacity=fixture_capacity)
            self.assertEqual(command["payload"]["inputs"], refs)
            self.assertNotIn("inputs", self.binding)
            self.assertNotIn("dependencies", command["payload"])
            self.state.confirm_exit(task["id"], command["attempt_id"], instance_id="instance-one", epoch="epoch-one", evidence="done-" + suffix)

    def test_pending_old_epoch_cannot_be_applied_by_seal_or_duplicate_delivery(self):
        task, command = self.dispatched()
        manifest = {"asset_id": "art-one", "revision": "v1", "sha256": "a" * 64}
        terminal = self.event(command, kind="task.terminal", payload={"status": "succeeded", "error_code": None, "manifest": manifest})
        self.assertEqual(self.state.receive(terminal), "pending")
        self.authority.quarantine("instance-one", "epoch-one", "fixture_lost_epoch")
        self.state.record_sealed_artifact(task["id"], command["attempt_id"], cancel_revision=0,
            asset_id="art-one", revision="v1", sha256="a" * 64, relative_path="image/old.png", byte_size=1)
        self.assertEqual(self.repository.get_task(task["id"])["status"], "assigned")
        self.assertEqual(self.state.receive(terminal), "pending")
        self.assertIsNone(self.repository.authorized_artifact("image/old.png"))
        self.state.authorize_recovery(task["id"], command["attempt_id"], "epoch-one", "old-journal-confirmed")
        self.assertEqual(self.repository.get_task(task["id"])["status"], "succeeded")

    def test_cancel_fault_and_exit_fault_leave_atomic_state(self):
        task, command = self.dispatched()
        for point in ("cancel.task", "cancel.outbox"):
            self.state.fault = lambda stage: (_ for _ in ()).throw(RuntimeError(stage)) if stage == point else None
            with self.assertRaises(RuntimeError):
                self.state.cancel(task["id"])
            self.assertEqual(self.repository.get_task(task["id"])["status"], "assigned")
            self.assertEqual(self.count("task_outbox"), 1)
        self.state.fault = lambda stage: (_ for _ in ()).throw(RuntimeError(stage)) if stage == "exit.release" else None
        with self.assertRaises(RuntimeError):
            self.state.confirm_exit(task["id"], command["attempt_id"], instance_id="instance-one", epoch="epoch-one", evidence="proof")
        self.assertEqual(self.repository.get_task(task["id"])["status"], "assigned")
        with self.repository._connect() as db:
            self.assertEqual(db.execute("SELECT SUM(released) FROM task_reservations").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
