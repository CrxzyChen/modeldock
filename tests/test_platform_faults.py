from __future__ import annotations
import json,unittest
from pathlib import Path
from scripts.verify_refactor_release import GATE_FIELDS,load_plan

ROOT=Path(__file__).parents[1]

class PlatformFaultMatrixTests(unittest.TestCase):
    def test_matrix_covers_durable_command_result_artifact_and_recovery_faults(self):
        plan=load_plan(ROOT/"deploy/refactor-verification-plan.json")
        modules={m for suite in plan["offline_suites"] for m in suite["modules"]}
        required={"tests.test_task_state","tests.test_task_outbox","tests.test_worker_journal",
                  "tests.test_worker_runtime","tests.test_runtime_controller","tests.test_artifact_commit",
                  "tests.test_transport_recovery","tests.test_reconciliation"}
        self.assertTrue(required<=modules)
        self.assertEqual(set(plan["real_resource_gate"]["requires"]),GATE_FIELDS)

    def test_real_resource_plan_is_explicitly_unprepared_not_silently_skipped(self):
        raw=json.loads((ROOT/"deploy/refactor-verification-plan.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["real_resource_gate"]["status"],"unprepared")
        self.assertIn("rollback_actions",raw["real_resource_gate"]["requires"])
        self.assertIn("stop_external_process",raw["real_resource_gate"]["forbids"])

