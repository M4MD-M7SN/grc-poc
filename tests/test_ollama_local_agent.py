import tempfile
import unittest
from unittest import mock
from pathlib import Path

from ollama_local_agent import LocalAgentError, WorkspaceTools, chat


class OllamaLocalAgentSecurityTests(unittest.TestCase):
    def test_chat_allows_slow_local_inference_by_default(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"message":{"content":"done"}}'
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "ollama_local_agent.urllib.request.urlopen", return_value=response
        ) as urlopen:
            self.assertEqual(chat("http://ollama", "model", "prompt", Path(temp)), "done")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 600)

    def test_workspace_tools_reject_escape_metadata_and_symlink_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".grc").mkdir()
            outside = root.parent / f"{root.name}-outside.txt"
            outside.write_text("secret")
            self.addCleanup(outside.unlink, missing_ok=True)
            (root / "link").symlink_to(outside)
            tools = WorkspaceTools(root)
            for path in ("../outside", ".grc/tasks.json", "link"):
                with self.assertRaises(LocalAgentError):
                    tools.write_file(path, "bad")

    def test_workspace_tools_can_read_search_and_write_normal_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "app.py").write_text("value = 1\n")
            tools = WorkspaceTools(root)
            self.assertIn("app.py", tools.list_files("*.py"))
            self.assertIn("1|value = 1", tools.read_file("app.py"))
            self.assertIn("app.py:1", tools.search_files("value"))
            with self.assertRaisesRegex(LocalAgentError, "already exists"):
                tools.write_file("app.py", "value = 2\n")
            tools.replace_in_file("app.py", "value = 1", "value = 2")
            self.assertEqual((root / "app.py").read_text(), "value = 2\n")
            tools.write_file("new.py", "created = True\n")
            self.assertEqual((root / "new.py").read_text(), "created = True\n")

    def test_replace_in_file_requires_one_exact_match(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "app.py").write_text("same\nsame\n")
            tools = WorkspaceTools(root)
            with self.assertRaisesRegex(LocalAgentError, "exactly once"):
                tools.replace_in_file("app.py", "same", "changed")
            with self.assertRaisesRegex(LocalAgentError, "exactly once"):
                tools.replace_in_file("app.py", "missing", "changed")
            self.assertEqual((root / "app.py").read_text(), "same\nsame\n")

    def test_check_python_syntax_reports_errors_without_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tools = WorkspaceTools(root)
            (root / "valid.py").write_text("value = 1\n")
            (root / "invalid.py").write_text("except ValueError: as exc:\n    pass\n")
            self.assertEqual(tools.check_python_syntax("valid.py"), "valid Python syntax: valid.py")
            with self.assertRaisesRegex(LocalAgentError, "invalid Python syntax"):
                tools.check_python_syntax("invalid.py")


if __name__ == "__main__":
    unittest.main()
