import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import scan


EXPECTED_CAPABILITIES = {
    "frontend",
    "backend",
    "user_interface",
    "data",
    "database_storage",
    "ml_ai",
    "servers_infrastructure",
    "mcp",
    "tools_integrations",
    "bots",
}


class UniversalCapabilityTests(unittest.TestCase):
    def test_empty_project_emits_every_capability_as_absent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".grc").mkdir()

            _, graph = scan.scan(root, {"name": "Empty"}, [], [])

            capabilities = {item["id"]: item for item in graph["capabilities"]}
            self.assertEqual(set(capabilities), EXPECTED_CAPABILITIES)
            self.assertTrue(all(item["present"] is False for item in capabilities.values()))
            self.assertTrue(all(item["evidence"] == [] for item in capabilities.values()))

    def test_software_capabilities_are_detected_with_file_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".grc").mkdir()
            files = {
                "web/App.tsx": "export function App() { return <main>Hello</main>; }\n",
                "backend/api.py": "from fastapi import FastAPI\napp = FastAPI()\n",
                "data/prices.csv": "symbol,price\nGC,1\n",
                "migrations/001_init.sql": "create table prices (symbol text);\n",
                "ml/model.py": "import torch\n",
                "Dockerfile": "FROM python:3.11\n",
                "mcp_server.py": "from mcp.server.fastmcp import FastMCP\nmcp = FastMCP('test')\n",
                "integrations/broker.py": "class BrokerConnector:\n    pass\n",
                "bots/strategy_bot.py": "class StrategyBot:\n    pass\n",
                "tests/test_fake_stack.py": "FastAPI FastMCP sqlite3 torch ReactDOM Dockerfile\n",
            }
            for relative, content in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)

            _, graph = scan.scan(root, {"name": "Full stack"}, [], [])

            capabilities = {item["id"]: item for item in graph["capabilities"]}
            self.assertEqual(set(capabilities), EXPECTED_CAPABILITIES)
            self.assertTrue(all(capabilities[key]["present"] for key in EXPECTED_CAPABILITIES))
            all_evidence = {path for item in capabilities.values() for path in item["evidence"]}
            self.assertNotIn("tests/test_fake_stack.py", all_evidence)
            self.assertIn("web/App.tsx", capabilities["frontend"]["evidence"])
            self.assertIn("web/App.tsx", capabilities["user_interface"]["evidence"])
            self.assertIn("backend/api.py", capabilities["backend"]["evidence"])
            self.assertIn("data/prices.csv", capabilities["data"]["evidence"])
            self.assertIn("migrations/001_init.sql", capabilities["database_storage"]["evidence"])
            self.assertIn("ml/model.py", capabilities["ml_ai"]["evidence"])
            self.assertIn("Dockerfile", capabilities["servers_infrastructure"]["evidence"])
            self.assertIn("mcp_server.py", capabilities["mcp"]["evidence"])
            self.assertIn("integrations/broker.py", capabilities["tools_integrations"]["evidence"])

    def test_documentation_mentions_and_docs_assets_do_not_enable_capabilities(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".grc").mkdir()
            (root / "docs").mkdir()
            (root / "README.md").write_text(
                "FastAPI SQLAlchemy torch FastMCP ReactDOM Docker and integrations\n"
            )
            (root / "docs" / "index.html").write_text("<body>Documentation</body>\n")
            (root / "docs" / "style.css").write_text("body { color: black; }\n")

            _, graph = scan.scan(root, {"name": "Documentation"}, [], [])

            self.assertTrue(all(item["present"] is False for item in graph["capabilities"]))

    def test_common_test_file_conventions_do_not_enable_capabilities(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".grc").mkdir()
            files = {
                "worker_test.py": "FastAPI SQLAlchemy torch FastMCP\n",
                "service_spec.py": "FastAPI SQLAlchemy torch FastMCP\n",
                "pkg/__tests__/App.tsx": "export const App = () => <main />;\n",
                "spec/server.py": "from fastapi import FastAPI\n",
                "specs/model.py": "import torch\n",
                "web/widget.test.tsx": "export const Widget = () => <button />;\n",
            }
            for relative, content in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)

            _, graph = scan.scan(root, {"name": "Tests only"}, [], [])

            self.assertTrue(all(item["present"] is False for item in graph["capabilities"]))

    def test_production_detect_capabilities_function_is_not_suppressed_by_name(self):
        paths = {"capability_scanner.py"}
        texts = {"capability_scanner.py": "def detect_capabilities():\n    return FastAPI()\n"}

        capabilities = {item["id"]: item for item in scan.detect_capabilities(paths, texts)}
        excluded = {
            item["id"]: item
            for item in scan.detect_capabilities(paths, texts, excluded_paths=paths)
        }

        self.assertTrue(capabilities["backend"]["present"])
        self.assertIn("capability_scanner.py", capabilities["backend"]["evidence"])
        self.assertFalse(excluded["backend"]["present"])

    def test_docs_in_infrastructure_paths_and_domain_models_do_not_enable_capabilities(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".grc").mkdir()
            files = {
                "k8s/README.md": "Deployment documentation only.\n",
                "app/models/user.py": "class User:\n    pass\n",
            }
            for relative, content in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)

            _, graph = scan.scan(root, {"name": "Domain model"}, [], [])

            self.assertTrue(all(item["present"] is False for item in graph["capabilities"]))

    def test_capability_inputs_normalize_windows_keys_and_mixed_path_types(self):
        paths = [Path("web/App.tsx"), "src\\service.py", Path("data/prices.csv")]
        texts = {
            "web\\App.tsx": "export const App = () => <main />;\n",
            "src\\service.py": "from fastapi import FastAPI\n",
            "data\\prices.csv": "symbol,price\nGOLD,1\n",
        }

        capabilities = {item["id"]: item for item in scan.detect_capabilities(paths, texts)}

        self.assertEqual(set(capabilities), EXPECTED_CAPABILITIES)
        self.assertTrue(capabilities["frontend"]["present"])
        self.assertTrue(capabilities["backend"]["present"])
        self.assertTrue(capabilities["data"]["present"])
        self.assertIn("src/service.py", capabilities["backend"]["evidence"])

    def test_renderer_filters_absent_capabilities_at_runtime(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for the JavaScript renderer regression test")
        template = (Path(__file__).resolve().parents[1] / "template.html").read_text()
        start = template.index("function normalizeCapabilities(value)")
        end = template.index("\n\nfunction normalizeAreas", start)
        functions = template[start:end]
        assertion = r'''
const source = [
  {id: "frontend", name: "Frontend", present: true, evidence: ["web/app.tsx"], count: "<img src=x>"},
  {id: "backend", name: "Backend", present: false, evidence: []},
];
const visible = visibleCapabilities(source);
if (visible.length !== 1 || visible[0].id !== "frontend") throw new Error("absent capability rendered");
if (visible[0].count !== 1) throw new Error("capability count was not normalized");
if (source.length !== 2 || source[1].present !== false) throw new Error("input capabilities mutated");
process.stdout.write("ok");
'''
        result = subprocess.run(
            [node, "-e", functions + assertion],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout, "ok")


if __name__ == "__main__":
    unittest.main()
