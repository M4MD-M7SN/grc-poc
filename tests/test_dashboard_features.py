import json
import tempfile
import unittest
from pathlib import Path

import grc


class DashboardFeatureSummaryTests(unittest.TestCase):
    def test_ai_description_and_project_features_render_at_top(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            grc_dir = root / ".grc"
            grc_dir.mkdir()
            (grc_dir / "project.json").write_text(json.dumps({
                "name": "Example",
                "goal": "Coordinate software work.",
                "dashboard": ".grc/dashboard.html",
                "features": ["Signed actor identity", "Git-native task history"],
            }))
            (grc_dir / "state.json").write_text("{}")
            (grc_dir / "tasks.json").write_text("[]")
            (grc_dir / "events.jsonl").write_text("")
            (grc_dir / "brief.md").write_text("## What this is\nAn AI-generated description.")

            output = grc.build(root)
            html = output.read_text()

            intro = html.index("AI-generated project description & features")
            resume = html.index('<section id="resume"')
            self.assertLess(intro, resume)
            self.assertIn("An AI-generated description.", html)
            self.assertIn("Signed actor identity", html)
            self.assertIn("Git-native task history", html)

    def test_review_queue_evidence_and_context_freshness_are_visible(self):
        template = (Path(__file__).parents[1] / "template.html").read_text()
        self.assertIn('review:"In review"', template)
        self.assertIn("Audit evidence", template)
        self.assertIn("Context ${", template)
        self.assertIn("t.implementation.tests", template)


if __name__ == "__main__":
    unittest.main()
