import json
import tempfile
import threading
import unittest
from pathlib import Path

import workflow


class AuditAndClaimContractTests(unittest.TestCase):
    def _project(self):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        (root / ".grc").mkdir()
        (root / ".grc" / "tasks.json").write_text("[]")
        (root / ".grc" / "project.json").write_text(json.dumps({
            "workflow": {
                "require_review": True,
                "roles": {
                    "local_implementers": ["local/qwen", "local/a", "local/b"],
                    "superior_auditors": ["superior/claude", "superior/hermes"],
                    "superior_reviewers": ["superior/claude", "superior/hermes", "superior/approved"],
                },
            }
        }))
        (root / "parser.py").write_text("def parse():\n    return None\n")
        return temp, root

    def test_superior_audit_upsert_deduplicates_evidence_fingerprint(self):
        temp, root = self._project()
        with temp:
            evidence = [{"path": "parser.py", "line": 2, "message": "Parser returns no value"}]
            first, created = workflow.audit_upsert_task(
                root, "superior/claude", "Parser silently returns None", evidence,
                ["Parser returns a structured result"], severity="high", base_revision="abc",
            )
            second, created_again = workflow.audit_upsert_task(
                root, "superior/hermes", "Parser silently returns None", evidence,
                ["Regression test passes"], severity="high", base_revision="abc",
            )
            self.assertTrue(created)
            self.assertFalse(created_again)
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(len(json.loads((root / ".grc" / "tasks.json").read_text())), 1)
            self.assertIn("Regression test passes", second["acceptance_criteria"])
            self.assertEqual(second["audit"]["tier"], "superior")

    def test_audit_rejects_missing_or_out_of_range_evidence(self):
        temp, root = self._project()
        with temp:
            for evidence in (
                [{"path": "missing.py", "line": 1, "message": "Missing"}],
                [{"path": "parser.py", "line": 99, "message": "Too far"}],
                [{"path": "/tmp/parser.py", "line": 1, "message": "Absolute"}],
            ):
                with self.subTest(evidence=evidence), self.assertRaises(workflow.WorkflowError):
                    workflow.audit_upsert_task(root, "superior/claude", "Bad finding", evidence, ["Fix it"])

    def test_one_agent_cannot_hold_two_implementation_claims(self):
        temp, root = self._project()
        with temp:
            path = root / ".grc" / "tasks.json"
            path.write_text(json.dumps([
                {"id": "T1", "title": "One", "status": "todo"},
                {"id": "T2", "title": "Two", "status": "todo"},
            ]))
            workflow.claim_task(path, "T1", "local/qwen")
            with self.assertRaisesRegex(workflow.WorkflowError, "already has active task T1"):
                workflow.claim_task(path, "T2", "local/qwen")

    def test_claim_revision_prevents_stale_claim(self):
        temp, root = self._project()
        with temp:
            path = root / ".grc" / "tasks.json"
            path.write_text(json.dumps([{"id": "T1", "title": "One", "status": "todo", "revision": 3}]))
            with self.assertRaisesRegex(workflow.WorkflowError, "revision conflict"):
                workflow.claim_task(path, "T1", "local/qwen", expected_revision=2)
            claimed = workflow.claim_task(path, "T1", "local/qwen", expected_revision=3)
            self.assertEqual(claimed["revision"], 4)
            self.assertIn("lease_expires_at", claimed["claim"])
            self.assertTrue(claimed["claim"]["lease_token"])

    def test_submission_requires_the_current_fencing_token(self):
        temp, root = self._project()
        with temp:
            path = root / ".grc" / "tasks.json"
            path.write_text(json.dumps([{"id": "T1", "title": "One", "status": "todo"}]))
            claimed = workflow.claim_task(path, "T1", "local/qwen", run_id="r1")
            with self.assertRaises(workflow.WorkflowError):
                workflow.submit_for_review(path, "T1", "local/qwen", "done", lease_token="wrong")
            submitted = workflow.submit_for_review(
                path, "T1", "local/qwen", "done", run_id="r1",
                lease_token=claimed["claim"]["lease_token"],
            )
            self.assertEqual(submitted["status"], "review")

    def test_concurrent_claims_have_exactly_one_winner(self):
        temp, root = self._project()
        with temp:
            path = root / ".grc" / "tasks.json"
            path.write_text(json.dumps([{"id": "T1", "title": "One", "status": "todo"}]))
            barrier = threading.Barrier(2)
            outcomes = []

            def claim(agent):
                barrier.wait()
                try:
                    workflow.claim_task(path, "T1", agent)
                    outcomes.append("won")
                except workflow.WorkflowError:
                    outcomes.append("lost")

            threads = [threading.Thread(target=claim, args=(agent,)) for agent in ("local/a", "local/b")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertCountEqual(outcomes, ["won", "lost"])
            self.assertTrue(path.read_text().startswith("["))

    def test_only_superior_tier_can_review_and_history_is_preserved(self):
        temp, root = self._project()
        with temp:
            path = root / ".grc" / "tasks.json"
            path.write_text(json.dumps([{"id": "T1", "title": "One", "status": "todo"}]))
            claimed = workflow.claim_task(path, "T1", "local/qwen")
            workflow.submit_for_review(path, "T1", "local/qwen", "First attempt", lease_token=claimed["claim"]["lease_token"])
            with self.assertRaisesRegex(workflow.WorkflowError, "superior reviewer"):
                workflow.review_task(path, "T1", "local/other", "accept", reviewer_tier="local")
            workflow.review_task(path, "T1", "superior/hermes", "reject", "Need tests")
            claimed = workflow.claim_task(path, "T1", "local/qwen")
            workflow.submit_for_review(path, "T1", "local/qwen", "Second attempt", lease_token=claimed["claim"]["lease_token"])
            accepted = workflow.review_task(path, "T1", "superior/claude", "accept", "Verified")
            self.assertEqual([item["decision"] for item in accepted["reviews"]], ["reject", "accept"])
            self.assertEqual(accepted["status"], "done")

    def test_declared_superior_role_cannot_be_self_asserted(self):
        temp, root = self._project()
        with temp:
            path = root / ".grc" / "tasks.json"
            path.write_text(json.dumps([{"id": "T1", "title": "One", "status": "todo"}]))
            (root / ".grc" / "project.json").write_text(json.dumps({
                "workflow": {
                    "require_review": True,
                    "roles": {
                        "local_implementers": ["local/a"],
                        "superior_reviewers": ["superior/approved"],
                    },
                }
            }))
            claimed = workflow.claim_task(path, "T1", "local/a")
            workflow.submit_for_review(
                path, "T1", "local/a", "done", lease_token=claimed["claim"]["lease_token"]
            )
            with self.assertRaisesRegex(workflow.WorkflowError, "not authorized"):
                workflow.review_task(path, "T1", "superior/spoofed", "accept", reviewer_tier="superior")


if __name__ == "__main__":
    unittest.main()
