import json
import tempfile
import unittest
from pathlib import Path

import genie
import grc
import scan


class ProjectFlowTests(unittest.TestCase):
    def test_legacy_string_flow_is_mapped_as_sequential_stages(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".grc").mkdir()
            project = {"name": "Example", "flow": ["Capture", "Process", "Publish"]}

            _, graph = scan.scan(root, project, [], [])

            stages = [node for node in graph["nodes"] if node["type"] == "stage"]
            self.assertEqual([node["label"] for node in stages], ["Capture", "Process", "Publish"])
            self.assertEqual([node["status"] for node in stages], ["", "", ""])
            self.assertEqual(
                [(edge["s"], edge["t"]) for edge in graph["edges"] if edge["rel"] == "flows_to"],
                [("stage:1", "stage:2"), ("stage:2", "stage:3")],
            )

    def test_structured_flow_links_stages_to_real_implementation_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".grc").mkdir()
            (root / "capture.py").write_text("def capture():\n    pass\n")
            (root / "process.py").write_text("def process():\n    pass\n")
            project = {
                "name": "Example",
                "flow": [
                    {"title": "Capture", "state": "implemented", "files": ["capture.py"]},
                    {"title": "Process", "files": ["process.py", "missing.py"]},
                ],
            }

            _, graph = scan.scan(root, project, [], [])
            links = {(edge["s"], edge["t"], edge["rel"]) for edge in graph["edges"]}

            self.assertIn(("stage:1", "file:capture.py", "implemented_by"), links)
            self.assertIn(("stage:2", "file:process.py", "implemented_by"), links)
            stages = {node["id"]: node for node in graph["nodes"] if node["type"] == "stage"}
            self.assertEqual(stages["stage:1"]["status"], "implemented")
            self.assertNotIn(("stage:2", "file:missing.py", "implemented_by"), links)

    def test_context_pack_describes_flow_and_uses_resolved_tasks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            grc_dir = root / ".grc"
            grc_dir.mkdir()
            board = root / "board.json"
            board.write_text(json.dumps({"projects": [{"id": "P1", "name": "External work", "status": "active"}]}))
            (grc_dir / "project.json").write_text(json.dumps({
                "name": "Example",
                "flow": [{"title": "Capture", "desc": "Collect evidence", "files": ["capture.py"]}],
                "task_source": {"format": "hermes-project-board", "path": str(board)},
            }))
            (grc_dir / "state.json").write_text("{}")
            (grc_dir / "tasks.json").write_text(json.dumps([{"id": "T1", "title": "Hidden local task", "status": "todo"}]))
            (grc_dir / "scan.json").write_text("{}")
            (grc_dir / "graph.json").write_text("{}")

            text = genie.context_pack(root, with_brief=False)

            self.assertIn("## How it works", text)
            self.assertIn("1. Capture — Collect evidence (`capture.py`)", text)
            self.assertIn("P1 [doing", text)
            self.assertNotIn("Hidden local task", text)

    def test_src_layout_absolute_import_resolves_to_local_module(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".grc").mkdir()
            package = root / "service" / "src" / "example_pkg"
            tests = root / "service" / "tests"
            package.mkdir(parents=True)
            tests.mkdir(parents=True)
            (package / "worker.py").write_text("def work():\n    pass\n")
            (tests / "test_worker.py").write_text("from example_pkg.worker import work\n")

            _, graph = scan.scan(root, {"name": "Example"}, [], [])
            links = {(edge["s"], edge["t"], edge["rel"]) for edge in graph["edges"]}

            self.assertIn((
                "file:service/tests/test_worker.py",
                "file:service/src/example_pkg/worker.py",
                "imports",
            ), links)

    def test_task_percent_tolerates_malformed_external_values(self):
        self.assertEqual(grc.task_percent({"percent": "not-a-number"}), 0)
        self.assertEqual(grc.task_percent({"percent": 150}), 100)

    def test_brief_path_validation_rejects_invented_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "real.py").write_text("")
            content = "Use `real.py`, `missing/service.py`, and status `planned`."

            self.assertEqual(genie.brief_missing_paths(root, content), ["missing/service.py"])


if __name__ == "__main__":
    unittest.main()
