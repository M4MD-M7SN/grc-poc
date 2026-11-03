import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import scan


class ManualProjectAreaTests(unittest.TestCase):
    def test_multiple_saved_architecture_areas_are_honored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".grc").mkdir()
            for name in ("runtime.py", "server.py", "storage.py", "models.py"):
                (root / name).write_text("class Example:\n    pass\n")
            (root / ".grc" / "clusters.json").write_text(json.dumps({
                "runtime.py": {"name": "Runtime", "files": ["runtime.py", "server.py"], "by": "test"},
                "storage.py": {"name": "Storage", "files": ["storage.py", "models.py"], "by": "test"},
            }))

            _, graph = scan.scan(root, {"name": "Example"}, [], [])

            self.assertEqual([area["name"] for area in graph["clusters"]], ["Runtime", "Storage"])
            assigned = {node.get("path"): node.get("cluster") for node in graph["nodes"] if node.get("path")}
            self.assertEqual(assigned["runtime.py"], "c1")
            self.assertEqual(assigned["models.py"], "c2")
    def test_dashboard_exposes_universal_and_project_specific_hubs(self):
        template = (Path(__file__).resolve().parents[1] / "template.html").read_text()
        self.assertIn("Universal capabilities", template)
        self.assertIn("Project-specific areas", template)
        self.assertIn("data-capability", template)
        self.assertIn("data-area", template)

    def test_dashboard_starts_with_a_project_and_conditional_architecture_hubs(self):
        template = (Path(__file__).resolve().parents[1] / "template.html").read_text()
        self.assertIn('project: "Project"', template)
        self.assertIn('capability: "Software capabilities"', template)
        self.assertIn('operation: "Operational groups"', template)
        self.assertIn('area: "Project-specific areas"', template)
        self.assertNotIn('label: `${projectNode.label} report`', template)
        self.assertIn('evidence: "capability"', template)
        self.assertIn('evidence: "project-area"', template)
        self.assertIn('const baseEdges = Array.isArray(rawGraph.edges) ? rawGraph.edges : []', template)
        self.assertIn('const CAPABILITIES = ALL_CAPABILITIES.filter(capability => capability.present)', template)
        self.assertIn('const AREAS = normalizeAreas(rawGraph.clusters)', template)
        self.assertIn('architectureMode = capabilityNodes.length + operationNodes.length + areaNodes.length > 0', template)
        self.assertIn('architectureMode ? new Set(detailTypes) : new Set(["other"])', template)
        self.assertIn('panel.innerHTML = architectureMode', template)
        self.assertIn('<h3>What is this?</h3>', template)
        self.assertIn('const usedNodeIds = new Set(rawNodes.map(n => String(n.id)))', template)
        self.assertIn('while (usedNodeIds.has(id)) id = `${stem}:${suffix++}`', template)
        self.assertIn('areaNodeIds.set(area, id)', template)
        self.assertIn('capabilityNodeIds.set(capability, id)', template)
        self.assertIn('const areaNode = byId.get(areaNodeIds.get(area))', template)
        self.assertIn('const capabilityNode = byId.get(capabilityNodeIds.get(capability))', template)

    def test_project_areas_are_nested_under_operational_backend_nodes(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for the JavaScript renderer regression test")
        template = (Path(__file__).resolve().parents[1] / "template.html").read_text()
        start = template.index("const OPERATIONAL_GROUPS")
        end = template.index("\n\nfunction renderMap", start)
        functions = template[start:end]
        assertion = r'''
const capabilities = [
  {id: "backend", present: true, evidence: [
    "services/telegram-ingestion/src/pipeline.py",
    "services/telegram-ingestion/scripts/midas-server.sh",
  ]},
  {id: "data", present: true, evidence: ["data/live.sqlite3"]},
];
const pipeline = classifyAreaPlacement({
  name: "Telegram ingestion pipeline",
  anchor: "services/telegram-ingestion/src/pipeline.py",
  files: ["services/telegram-ingestion/src/pipeline.py"],
}, capabilities);
if (pipeline.capability !== "backend" || pipeline.operation !== "pipelines") {
  throw new Error(`bad pipeline placement: ${JSON.stringify(pipeline)}`);
}
const runtime = classifyAreaPlacement({
  name: "Server runtime files",
  anchor: "data/live.sqlite3",
  files: ["data/live.sqlite3", "services/telegram-ingestion/scripts/midas-server.sh"],
}, capabilities);
if (runtime.capability !== "backend" || runtime.operation !== "runtime") {
  throw new Error(`bad runtime placement: ${JSON.stringify(runtime)}`);
}
process.stdout.write("ok");
'''
        result = subprocess.run(
            [node, "-e", functions + assertion],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout, "ok")

        self.assertNotIn('label: `${projectNode.label} report`', template)
        self.assertIn('operationNodeIds', template)
        self.assertIn('evidence: "operational-area"', template)

    def test_area_normalization_deduplicates_explicit_ids_at_runtime(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for the JavaScript renderer regression test")
        template = (Path(__file__).resolve().parents[1] / "template.html").read_text()
        start = template.index("function normalizeAreas(value)")
        end = template.index("\n\nfunction renderMap", start)
        function = template[start:end]
        assertion = r'''
const original = [
  {id: "same", name: "First"},
  {id: "same", name: "Duplicate"},
  {name: "Missing A"},
  {name: "Missing B"},
  null,
];
const normalized = normalizeAreas(original);
if (normalized.length !== 3) throw new Error(`expected 3 areas, got ${normalized.length}`);
if (normalized[0].name !== "First") throw new Error("first explicit area was not retained");
if (new Set(normalized.map(area => String(area.id))).size !== 3) throw new Error("area IDs are not unique");
if (original[2].id !== undefined || original[3].id !== undefined) throw new Error("input areas were mutated");
process.stdout.write("ok");
'''
        result = subprocess.run(
            [node, "-e", function + assertion],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout, "ok")


if __name__ == "__main__":
    unittest.main()
