import json
import tempfile
import unittest
from pathlib import Path

import task_sources


class TaskSourceAdapterTests(unittest.TestCase):
    def test_local_markdown_tasks_map_into_grc_tasks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            board = root / "TASKS.md"
            board.write_text("# Work\n- [ ] T7: Ship v2\n- [x] Write local tests\n")
            tasks = task_sources.resolve(root, {"format": "markdown-tasks", "path": "TASKS.md"}, [])
            self.assertEqual(tasks[0]["id"], "T7")
            self.assertEqual(tasks[0]["status"], "todo")
            self.assertEqual(tasks[0]["source"], "Local Markdown")
            self.assertEqual(tasks[1]["status"], "done")
            status = json.loads((root / ".grc" / task_sources.STATUS_FILE).read_text())
            self.assertEqual(status["state"], "ok")
            self.assertIn("local_only", status["capabilities"])

    def test_online_task_source_is_rejected_without_running_any_command(self):
        local = [{"id": "T1", "title": "Local", "status": "todo"}]
        calls = []
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tasks = task_sources.resolve(
                root,
                {"format": "github-issues", "repo": "org/repo"},
                local,
                run=lambda *args, **kwargs: calls.append(args),
            )
            self.assertEqual(tasks, local)
            self.assertEqual(calls, [])
            status = json.loads((root / ".grc" / task_sources.STATUS_FILE).read_text())
            self.assertEqual(status["state"], "error")
            self.assertIn("Unsupported task source", status["error"])


if __name__ == "__main__":
    unittest.main()
