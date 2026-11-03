import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DistributionTests(unittest.TestCase):
    def test_runtime_manifest_contains_every_required_asset(self):
        manifest = ROOT / "packaging" / "runtime-files.txt"
        self.assertTrue(manifest.is_file())
        entries = [
            line.strip()
            for line in manifest.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertIn("grc.py", entries)
        self.assertIn("mcp_server.py", entries)
        self.assertIn("template.html", entries)
        self.assertIn("schemas/task-workflow-v1.schema.json", entries)
        self.assertEqual(entries, list(dict.fromkeys(entries)), "manifest entries must be unique")
        for relative in entries:
            self.assertFalse(relative.startswith("/"))
            self.assertNotIn("..", Path(relative).parts)
            self.assertTrue((ROOT / relative).is_file(), relative)

    def test_release_archive_excludes_project_runtime_state(self):
        attributes = (ROOT / ".gitattributes").read_text().splitlines()
        self.assertIn(".grc export-ignore", attributes)
        self.assertIn("index.html export-ignore", attributes)
        self.assertIn("index.html", (ROOT / ".gitignore").read_text().splitlines())

    def test_minimal_installer_produces_working_cli_and_mcp(self):
        with tempfile.TemporaryDirectory() as temp:
            prefix = Path(temp) / 'prefix $(touch PWNED); with spaces and "quotes"'
            marker = Path(temp) / "selected-python-used"
            selected_python = Path(temp) / 'selected python "quoted"'
            selected_python.write_text(
                "#!/bin/sh\n"
                f"printf used >> {shlex.quote(str(marker))}\n"
                f"exec {shlex.quote(sys.executable)} \"$@\"\n"
            )
            selected_python.chmod(0o755)
            env = os.environ.copy()
            env["HOME"] = str(Path(temp) / "home")
            env["PYTHON"] = str(selected_python)
            subprocess.run(
                ["sh", str(ROOT / "scripts" / "install.sh"), "--prefix", str(prefix)],
                cwd=ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            cli = prefix / "bin" / "grc"
            mcp = prefix / "bin" / "grc-mcp"
            lead_mcp = prefix / "bin" / "grc-mcp-project-lead"
            runtime = prefix / "share" / "grc"
            self.assertTrue(cli.is_file() and os.access(cli, os.X_OK))
            self.assertTrue(mcp.is_file() and os.access(mcp, os.X_OK))
            self.assertTrue(lead_mcp.is_file() and os.access(lead_mcp, os.X_OK))
            self.assertEqual((runtime / "python-command").read_text().strip(), str(selected_python))
            self.assertFalse((runtime / ".grc").exists())

            marker.unlink()
            help_run = subprocess.run(
                [str(cli), "--help"],
                cwd=temp,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("General Repo Context", help_run.stdout)
            self.assertTrue(marker.exists(), "installed launcher did not reuse the validated PYTHON")
            self.assertFalse((Path(temp) / "PWNED").exists(), "prefix triggered shell evaluation")

            request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
            mcp_run = subprocess.run(
                [str(mcp)],
                input=request,
                cwd=temp,
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            response = json.loads(mcp_run.stdout.splitlines()[0])
            self.assertEqual(response["result"]["serverInfo"]["name"], "grc")

            project = Path(temp) / "project"
            grc_dir = project / ".grc"
            grc_dir.mkdir(parents=True)
            (grc_dir / "tasks.json").write_text("[]\n")
            (grc_dir / "project.json").write_text(json.dumps({
                "workflow": {"require_review": False, "roles": {
                    "project_leads": ["project-lead/test"]
                }}
            }))
            plan = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "grc_plan_task", "arguments": {
                    "project_dir": str(project),
                    "spec": {"title": "Privileged plan", "acceptance_criteria": ["Verified"]},
                }},
            }
            plan_input = request + json.dumps(plan) + "\n"
            ordinary = subprocess.run(
                [str(mcp)], input=plan_input, env=env, check=True,
                capture_output=True, text=True, timeout=10,
            )
            ordinary_result = json.loads(ordinary.stdout.splitlines()[1])["result"]
            self.assertTrue(ordinary_result["isError"])
            self.assertIn("GRC_MCP_ROLE=project-lead", ordinary_result["content"][0]["text"])

            privileged = subprocess.run(
                [str(lead_mcp)], input=plan_input, env=env, check=True,
                capture_output=True, text=True, timeout=10,
            )
            privileged_result = json.loads(privileged.stdout.splitlines()[1])["result"]
            self.assertFalse(privileged_result["isError"])
            planned = json.loads(privileged_result["content"][0]["text"])
            self.assertEqual(planned["id"], "T1")
            self.assertEqual(planned["planning"]["project_lead"], "project-lead/test")


if __name__ == "__main__":
    unittest.main()
