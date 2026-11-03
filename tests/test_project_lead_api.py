import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mcp_server
import workflow


class ProjectLeadApiTests(unittest.TestCase):
    def _project(self, root: Path, require_review=False, authorized=True):
        grc = root / ".grc"
        grc.mkdir()
        (grc / "tasks.json").write_text("[]\n")
        roles = {
            "project_leads": ["project-lead/hermes"],
            "local_implementers": ["local/test"],
        } if authorized else {}
        (grc / "project.json").write_text(json.dumps({
            "workflow": {"require_review": require_review, "roles": roles}
        }))
        return grc / "tasks.json"

    def _spec(self, title, **overrides):
        spec = {
            "title": title,
            "purpose": f"Deliver {title}",
            "outputs": [f"{title.lower().replace(' ', '-')}.py"],
            "owned_paths": ["src", "tests"],
            "forbidden_paths": [".grc"],
            "acceptance_criteria": [f"{title} is verified"],
            "verification_commands": [["python3", "-m", "unittest"]],
            "source_strategy": "build",
            "time_limit_seconds": 900,
        }
        spec.update(overrides)
        return spec

    def test_structured_tasks_are_ordered_and_dependency_aware(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self._project(root)
            first = workflow.plan_task(root, "project-lead/hermes", self._spec("Core", position=2))
            second = workflow.plan_task(root, "project-lead/hermes", self._spec(
                "Adapter", position=3, dependencies=[first["id"]]))
            earlier = workflow.plan_task(root, "project-lead/hermes", self._spec("Contract", position=1))
            sheet = workflow.task_sheet(path)
            self.assertEqual([item["id"] for item in sheet], [earlier["id"], first["id"], second["id"]])
            self.assertEqual([item["workflow_state"] for item in sheet], ["ready", "ready", "blocked"])
            self.assertEqual(sheet[2]["blocked_by"], [first["id"]])
            stored = json.loads(path.read_text())
            self.assertEqual(stored[1]["verification_commands"], [["python3", "-m", "unittest"]])
            self.assertEqual(stored[1]["planning"]["project_lead"], "project-lead/hermes")

    def test_review_required_dependency_needs_accepted_review(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self._project(root, require_review=True)
            first = workflow.plan_task(root, "project-lead/hermes", self._spec("Core", position=1))
            second = workflow.plan_task(root, "project-lead/hermes", self._spec(
                "Adapter", position=2, dependencies=[first["id"]]))
            tasks = json.loads(path.read_text())
            tasks[0]["status"] = "done"
            path.write_text(json.dumps(tasks))
            self.assertIsNone(workflow.next_ready_task(path))
            with self.assertRaisesRegex(workflow.WorkflowError, "blocked by dependencies"):
                workflow.claim_task(path, second["id"], "local/test")
            tasks = json.loads(path.read_text())
            tasks[0]["review"] = {"status": "accepted"}
            path.write_text(json.dumps(tasks))
            self.assertEqual(workflow.next_ready_task(path)["id"], second["id"])

    def test_plan_requires_explicit_lead_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._project(root, authorized=False)
            with self.assertRaisesRegex(workflow.WorkflowError, "no actors for project_leads"):
                workflow.plan_task(root, "project-lead/hermes", self._spec("Core"))

    def test_plan_strictly_validates_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._project(root)
            cases = [
                ("Unknown dependency", {"dependencies": ["T99"]}),
                ("Time limit", {"time_limit_seconds": "forever"}),
                ("verification command", {"verification_commands": [[]]}),
                ("Unknown task spec fields", {"invented_policy": True}),
                ("project-relative", {"owned_paths": ["../escape"]}),
                ("values must be strings", {"inputs": [True]}),
            ]
            for message, overrides in cases:
                with self.subTest(message=message), self.assertRaisesRegex(workflow.WorkflowError, message):
                    workflow.plan_task(root, "project-lead/hermes", self._spec("Adapter", **overrides))

    def test_legacy_tasks_are_migrated_without_being_jump_queued(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self._project(root)
            path.write_text(json.dumps([
                {"id": "T1", "title": "Old one", "status": "todo"},
                {"id": "T2", "title": "Old two", "status": "todo"},
            ]))
            planned = workflow.plan_task(root, "project-lead/hermes", self._spec("New"))
            sheet = workflow.task_sheet(path)
            self.assertEqual([item["id"] for item in sheet], ["T1", "T2", planned["id"]])
            self.assertEqual([item["position"] for item in json.loads(path.read_text())], [1, 2, 3])

    def test_unknown_status_is_never_scheduled(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self._project(root)
            workflow.plan_task(root, "project-lead/hermes", self._spec("Core"))
            tasks = json.loads(path.read_text())
            tasks[0]["status"] = "corrupt"
            path.write_text(json.dumps(tasks))
            self.assertIsNone(workflow.next_ready_task(path))
            self.assertEqual(workflow.task_sheet(path)[0]["workflow_state"], "blocked")

    def test_mcp_surface_is_local_read_scheduler_without_run_next(self):
        tools = {item["name"]: item for item in mcp_server.TOOLS}
        self.assertTrue({"grc_plan_task", "grc_task_sheet", "grc_next_task"} <= set(tools))
        self.assertNotIn("grc_run_next", tools)
        plan = tools["grc_plan_task"]["inputSchema"]
        self.assertNotIn("agent", plan["properties"])
        self.assertFalse(plan["properties"]["spec"]["additionalProperties"])
        self.assertNotIn("github", json.dumps([tools[name] for name in (
            "grc_plan_task", "grc_task_sheet", "grc_next_task")]).lower())

    def test_mcp_plan_requires_trusted_server_role(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._project(root)
            args = {"project_dir": str(root), "spec": self._spec("Core", position=1)}
            with mock.patch.object(mcp_server, "SERVER_ROLE", "agent"):
                with self.assertRaisesRegex(mcp_server.ToolError, "GRC_MCP_ROLE"):
                    mcp_server.call("grc_plan_task", args)
            with mock.patch.object(mcp_server, "SERVER_ROLE", "project-lead"), \
                 mock.patch.object(mcp_server.grc, "append_event"), \
                 mock.patch.object(mcp_server.grc, "build"):
                planned = json.loads(mcp_server.call("grc_plan_task", args))
                sheet = json.loads(mcp_server.call("grc_task_sheet", {"project_dir": str(root)}))
            self.assertEqual(planned["planning"]["project_lead"], "project-lead/hermes")
            self.assertEqual(sheet[0]["workflow_state"], "ready")

    def test_canonical_schema_describes_enforced_contract(self):
        schema_path = Path(__file__).resolve().parents[1] / "schemas" / "task-workflow-v1.schema.json"
        properties = json.loads(schema_path.read_text())["properties"]
        expected = {"position", "dependencies", "owned_paths", "forbidden_paths",
                    "verification_commands", "source_strategy", "time_limit_seconds", "planning"}
        self.assertTrue(expected <= set(properties))
        self.assertFalse({"network_policy", "usage_budget", "retry_limit"} & set(properties))


if __name__ == "__main__":
    unittest.main()
