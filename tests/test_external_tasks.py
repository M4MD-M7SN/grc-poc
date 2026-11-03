import json
import tempfile
import unittest
from pathlib import Path

import grc
import mcp_server


class ExternalTaskSourceTests(unittest.TestCase):
    def test_hermes_project_board_is_filtered_and_mapped_to_grc_tasks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            board = root / "board.json"
            board.write_text(json.dumps({
                "projects": [
                    {"id": "P1", "name": "Example trading app", "status": "active", "next_action": "Connect broker", "milestone": "MCP visibility", "blocker": "", "path": "/tmp/example-trading"},
                    {"id": "P2", "name": "Unrelated", "status": "done", "next_action": "None", "path": "/tmp/elsewhere"},
                ]
            }))
            project = {"task_source": {"format": "hermes-project-board", "path": str(board), "terms": ["example trading", "broker"]}}

            tasks = grc.resolve_tasks(root, project)

            self.assertEqual([task["id"] for task in tasks], ["P1"])
            self.assertEqual(tasks[0]["title"], "Example trading app")
            self.assertEqual(tasks[0]["status"], "doing")
            self.assertIn("Connect broker", tasks[0]["subtasks"][0]["text"])

    def test_external_board_can_be_combined_with_local_project_roadmap(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".grc").mkdir()
            local = [{"id": "EX1", "title": "Example app roadmap", "status": "todo", "subtasks": []}]
            (root / ".grc" / "tasks.json").write_text(json.dumps(local))
            board = root / "board.json"
            board.write_text(json.dumps({"projects": [{"id": "P10", "name": "trading", "status": "active"}]}))
            project = {"task_source": {
                "format": "hermes-project-board", "path": str(board),
                "terms": ["trading"], "include_local": True,
            }}

            tasks = grc.resolve_tasks(root, project)

            self.assertEqual([task["id"] for task in tasks], ["P10", "EX1"])

    def test_local_tasks_remain_default(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".grc").mkdir()
            expected = [{"id": "T1", "title": "Local", "status": "todo", "percent": 0}]
            (root / ".grc" / "tasks.json").write_text(json.dumps(expected))
            self.assertEqual(grc.resolve_tasks(root, {}), expected)

    def test_external_task_source_refuses_split_brain_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".grc").mkdir()
            (root / ".grc" / "project.json").write_text(json.dumps({
                "task_source": {"format": "hermes-project-board", "path": "/tmp/board.json"}
            }))

            with self.assertRaisesRegex(mcp_server.ToolError, "read-only"):
                mcp_server.ensure_task_write_allowed(root, "add")
            with self.assertRaisesRegex(mcp_server.ToolError, "split-brain"):
                mcp_server.ensure_task_write_allowed(root, "update", "P1")

    def test_combined_source_allows_only_local_task_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".grc").mkdir()
            (root / ".grc" / "project.json").write_text(json.dumps({
                "task_source": {"format": "hermes-project-board", "include_local": True}
            }))
            (root / ".grc" / "tasks.json").write_text(json.dumps([
                {"id": "T1", "title": "Local", "status": "todo"}
            ]))

            mcp_server.ensure_task_write_allowed(root, "add")
            mcp_server.ensure_task_write_allowed(root, "update", "T1")
            with self.assertRaisesRegex(mcp_server.ToolError, "read-only"):
                mcp_server.ensure_task_write_allowed(root, "update", "P1")


if __name__ == "__main__":
    unittest.main()
