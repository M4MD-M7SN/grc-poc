import json
import tempfile
import unittest
from pathlib import Path

import genie
import mcp_server
import workflow


class ReviewWorkflowTests(unittest.TestCase):
    def _tasks(self, root: Path):
        grc = root / ".grc"
        grc.mkdir()
        path = grc / "tasks.json"
        path.write_text(json.dumps([{"id": "T1", "title": "Fix bug", "status": "todo", "subtasks": []}]))
        (grc / "project.json").write_text(json.dumps({
            "workflow": {
                "require_review": True,
                "roles": {
                    "local_implementers": ["local/qwen", "local/llama"],
                    "superior_reviewers": ["superior/claude", "superior/hermes"],
                },
            }
        }))
        return path

    def test_local_implementation_requires_independent_acceptance_before_done(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._tasks(Path(temp))
            claimed = workflow.claim_task(path, "T1", "local/qwen")
            with self.assertRaisesRegex(workflow.WorkflowError, "already claimed"):
                workflow.claim_task(path, "T1", "local/llama")
            workflow.submit_for_review(
                path, "T1", "local/qwen", "Fixed parser", ["parser.py"], ["pytest -q"], "abc123",
                lease_token=claimed["claim"]["lease_token"],
            )
            task = json.loads(path.read_text())[0]
            self.assertEqual(task["status"], "review")
            with self.assertRaisesRegex(workflow.WorkflowError, "not authorized"):
                workflow.review_task(path, "T1", "local/qwen", "accept")
            workflow.review_task(path, "T1", "superior/claude", "accept", "Tests and diff verified")
            task = json.loads(path.read_text())[0]
            self.assertEqual(task["status"], "done")
            self.assertEqual(task["review"]["status"], "accepted")
            self.assertIsNone(task["claim"])
            self.assertTrue(all("lease_token" not in item for item in task["claim_history"]))

    def test_rejection_reopens_task_for_a_new_implementer(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._tasks(Path(temp))
            claimed = workflow.claim_task(path, "T1", "local/qwen")
            workflow.submit_for_review(
                path, "T1", "local/qwen", "Attempted fix", lease_token=claimed["claim"]["lease_token"]
            )
            workflow.review_task(path, "T1", "superior/hermes", "reject", "Missing regression test")
            rejected = json.loads(path.read_text())[0]
            self.assertIsNone(rejected["claim"])
            self.assertTrue(all("lease_token" not in item for item in rejected["claim_history"]))
            task = workflow.claim_task(path, "T1", "local/llama")
            self.assertEqual(task["claim"]["agent"], "local/llama")
            self.assertEqual(task["review"]["status"], "rejected")

    def test_review_required_project_refuses_direct_done_transition(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._tasks(root)
            result = genie.Workbench(root, "local/qwen").t_update_task("T1", status="done")
            self.assertIn("requires independent review", result)
            self.assertEqual(json.loads((root / ".grc" / "tasks.json").read_text())[0]["status"], "todo")

    def test_mcp_exposes_only_canonical_privileged_runner_tools(self):
        tools = {item["name"] for item in mcp_server.TOOLS}
        self.assertTrue({"grc_run_audit", "grc_run_local", "grc_run_review", "grc_recover_runs"} <= tools)
        self.assertFalse({"grc_audit_upsert_task", "grc_submit_review", "grc_review_task"} & tools)
        context = next(tool for tool in mcp_server.TOOLS if tool["name"] == "grc_context")
        self.assertIn("capsule", context["inputSchema"]["properties"]["format"]["enum"])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._tasks(root)
            with self.assertRaisesRegex(mcp_server.ToolError, "not authenticated"):
                mcp_server.call("grc_review_task", {"project_dir": str(root), "agent": "superior/fake"})


if __name__ == "__main__":
    unittest.main()
