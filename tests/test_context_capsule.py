import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import genie


class ContextCapsuleTests(unittest.TestCase):
    def _project(self, root: Path):
        grc = root / ".grc"
        grc.mkdir()
        (grc / "project.json").write_text(json.dumps({"name": "Example", "goal": "Ship safely"}))
        (grc / "state.json").write_text(json.dumps({"now": "Implement", "next": "Review", "blocked": ""}))
        (grc / "tasks.json").write_text(json.dumps([
            {"id": "T1", "title": "Fix issue", "status": "review",
             "implementation": {"agent": "local/qwen"}}
        ]))
        (grc / "scan.json").write_text(json.dumps({"files": 3, "git": {"branch": "main", "changed": 1}}))
        (grc / "graph.json").write_text(json.dumps({"nodes": [], "edges": []}))
        (grc / "brief.md").write_text("A" * 9000)
        return grc

    def test_capsule_is_portable_integrity_sealed_and_hard_budgeted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._project(root)
            capsule = genie.context_capsule(root, focus="T1", max_bytes=3000)

            encoded = json.dumps(capsule, ensure_ascii=False, separators=(",", ":")).encode()
            self.assertLessEqual(len(encoded), 3000)
            self.assertNotIn(str(root), capsule["briefing"])
            self.assertEqual(capsule["schema"], "grc.context-capsule/v1")
            self.assertEqual(capsule["review_queue"][0]["id"], "T1")
            unsigned = {key: value for key, value in capsule.items() if key != "integrity"}
            digest = hashlib.sha256(json.dumps(
                unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()).hexdigest()
            self.assertEqual(capsule["integrity"], f"sha256:{digest}")

    def test_persisted_capsule_is_reused_until_a_projection_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            grc = self._project(root)
            first = genie.context_capsule(root, max_bytes=4000, persist=True)
            second = genie.context_capsule(root, max_bytes=4000, persist=True)
            self.assertEqual(first, second)
            tasks = json.loads((grc / "tasks.json").read_text())
            tasks[0]["title"] = "Changed"
            (grc / "tasks.json").write_text(json.dumps(tasks))
            third = genie.context_capsule(root, max_bytes=4000, persist=True)
            self.assertNotEqual(first["source_fingerprint"], third["source_fingerprint"])
            self.assertIn("tasks.json", third["freshness"]["changed_sources"])
            self.assertEqual(third["freshness"]["previous_fingerprint"], first["source_fingerprint"])


if __name__ == "__main__":
    unittest.main()
