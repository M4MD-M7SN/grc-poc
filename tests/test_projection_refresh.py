import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import grc


class ProjectionRefreshTests(unittest.TestCase):
    def test_task_only_refresh_reuses_existing_repository_scan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / ".grc"
            data.mkdir()
            (data / "project.json").write_text(json.dumps({"name": "Example", "dashboard": "index.html"}))
            (data / "state.json").write_text(json.dumps({"now": "x", "next": "y", "blocked": ""}))
            (data / "tasks.json").write_text("[]")
            (data / "scan.json").write_text(json.dumps({"files": 1, "directories": 1, "git": {}}))
            (data / "graph.json").write_text(json.dumps({"nodes": [], "edges": []}))
            (data / "events.jsonl").write_text("")
            with mock.patch("grc.scan", side_effect=AssertionError("unexpected repository scan")):
                grc.build(root, rescan=False)
            self.assertTrue((root / "index.html").exists())


if __name__ == "__main__":
    unittest.main()
