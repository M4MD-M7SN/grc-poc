import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import agent_runner
import grc
import workflow


class AgentRunnerEndToEndTests(unittest.TestCase):
    def _repo(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "GRC Test"], cwd=root, check=True)
        (root / "app.py").write_text("value = 1\n")
        grc.init(root)
        project_path = root / ".grc" / "project.json"
        project = json.loads(project_path.read_text())
        project.setdefault("workflow", {})["roles"] = {
            "superior_auditors": ["superior/test", "superior/auditor"],
            "superior_reviewers": ["superior/reviewer"],
            "local_implementers": ["local/test", "local/qwen"],
        }
        project_path.write_text(json.dumps(project))
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True, capture_output=True)
        return temporary, root

    def _script(self, root, name, source):
        path = root.parent / name
        path.write_text(textwrap.dedent(source))
        return path

    def _agents(self, root):
        audit = self._script(root, "audit_helper.py", """
            import json, sys
            json.dump({"findings": [{"title": "Update stale value", "severity": "medium", "evidence": [{"path": "app.py", "line": 1, "message": "stale"}], "acceptance_criteria": ["value is 2"]}]}, open(sys.argv[2], "w"))
        """)
        local = self._script(root, "local_helper.py", """
            from pathlib import Path
            Path("app.py").write_text("value = 2\\n")
        """)
        reviewer = self._script(root, "review_helper.py", """
            import json, re, sys
            prompt = open(sys.argv[1]).read()
            head = re.search(r'"head_sha":\\s*"([0-9a-f]+)"', prompt).group(1)
            patch = re.search(r'"patch_sha256":\\s*"([0-9a-f]+)"', prompt).group(1)
            json.dump({"decision": "accept", "findings": "verified", "reviewed_head_sha": head, "reviewed_patch_sha256": patch}, open(sys.argv[2], "w"))
        """)
        return audit, local, reviewer

    def test_full_audit_local_fix_superior_review_and_integration_loop(self):
        temporary, root = self._repo()
        self.addCleanup(temporary.cleanup)
        audit = self._script(root, "audit.py", """
            import json, sys
            json.dump({"findings": [{
              "title": "Update the stale value", "severity": "medium",
              "evidence": [{"path": "app.py", "line": 1, "message": "The value is stale"}],
              "acceptance_criteria": ["app.py sets value to 2"]
            }]}, open(sys.argv[2], "w"))
        """)
        local = self._script(root, "local.py", """
            from pathlib import Path
            Path("app.py").write_text("value = 2\\n")
        """)
        reviewer = self._script(root, "review.py", """
            import json, re, sys
            prompt = open(sys.argv[1]).read()
            head = re.search(r'"head_sha":\s*"([0-9a-f]+)"', prompt).group(1)
            patch = re.search(r'"patch_sha256":\s*"([0-9a-f]+)"', prompt).group(1)
            json.dump({"decision": "accept", "findings": "Diff and tests satisfy the criterion.",
                       "reviewed_head_sha": head, "reviewed_patch_sha256": patch}, open(sys.argv[2], "w"))
        """)

        runner = agent_runner.AgentRunner(root)
        ids = runner.run_audit(
            auditor="superior/test", command=[sys.executable, str(audit), "{prompt_file}", "{result_file}"]
        )
        self.assertEqual(len(ids), 1)
        task_id = ids[0]

        run = runner.run_local(
            task_id=task_id,
            agent="local/test",
            command=[sys.executable, str(local), "{prompt_file}", "{result_file}"],
            tests=[[sys.executable, "-m", "py_compile", "app.py"]],
        )
        self.assertEqual(run["status"], "awaiting_review")
        self.assertTrue(run["base_sha"])
        self.assertTrue(run["head_sha"])
        self.assertEqual(len(run["patch_sha256"]), 64)
        self.assertEqual((root / "app.py").read_text(), "value = 1\n")
        tasks = json.loads((root / ".grc" / "tasks.json").read_text())
        self.assertEqual(tasks[0]["status"], "review")
        self.assertEqual(tasks[0]["implementation"]["tests"][0]["exit_code"], 0)

        reviewed = runner.run_review(
            task_id=task_id,
            reviewer="superior/reviewer",
            command=[sys.executable, str(reviewer), "{prompt_file}", "{result_file}"],
        )
        self.assertEqual(reviewed["decision"], "accept")
        self.assertEqual((root / "app.py").read_text(), "value = 2\n")
        tasks = json.loads((root / ".grc" / "tasks.json").read_text())
        self.assertEqual(tasks[0]["status"], "done")
        self.assertEqual(tasks[0]["review"]["tier"], "superior")
        self.assertEqual(tasks[0]["accepted_implementation"]["head_sha"], run["head_sha"])
        self.assertEqual(subprocess.run(["git", "status", "--porcelain"], cwd=root, text=True, capture_output=True).stdout, "")

    def test_failed_tests_do_not_submit_for_review(self):
        temporary, root = self._repo()
        self.addCleanup(temporary.cleanup)
        tasks = [{"id": "T1", "title": "Break", "status": "todo", "revision": 1}]
        (root / ".grc" / "tasks.json").write_text(json.dumps(tasks))
        subprocess.run(["git", "add", ".grc/tasks.json"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "task"], cwd=root, check=True, capture_output=True)
        local = self._script(root, "local_fail.py", """
            from pathlib import Path
            Path("app.py").write_text("broken =\\n")
        """)
        runner = agent_runner.AgentRunner(root)
        with self.assertRaises(agent_runner.RunnerError):
            runner.run_local(
                "T1", "local/test", [sys.executable, str(local), "{prompt_file}", "{result_file}"],
                tests=[[sys.executable, "-m", "py_compile", "app.py"]],
            )
        task = json.loads((root / ".grc" / "tasks.json").read_text())[0]
        self.assertEqual(task["status"], "todo")
        self.assertIsNone(task.get("claim"))
        self.assertEqual((root / "app.py").read_text(), "value = 1\n")

    def test_reviewer_cannot_accept_a_different_head_or_patch(self):
        temporary, root = self._repo()
        self.addCleanup(temporary.cleanup)
        audit, local, _ = self._agents(root)
        bad_review = self._script(root, "bad_review.py", """
            import json, sys
            json.dump({"decision": "accept", "findings": "looks fine", "reviewed_head_sha": "wrong", "reviewed_patch_sha256": "wrong"}, open(sys.argv[2], "w"))
        """)
        runner = agent_runner.AgentRunner(root)
        task_id = runner.run_audit("superior/auditor", [sys.executable, str(audit), "{prompt_file}", "{result_file}"])[0]
        runner.run_local(task_id, "local/qwen", [sys.executable, str(local), "{prompt_file}", "{result_file}"], tests=[[sys.executable, "-m", "py_compile", "app.py"]])
        with self.assertRaisesRegex(agent_runner.RunnerError, "not bound"):
            runner.run_review(task_id, "superior/reviewer", [sys.executable, str(bad_review), "{prompt_file}", "{result_file}"])
        self.assertEqual((root / "app.py").read_text(), "value = 1\n")
        self.assertEqual(json.loads((root / ".grc" / "tasks.json").read_text())[0]["status"], "review")

    def test_stale_running_attempt_is_recovered_without_deleting_worktree_evidence(self):
        temporary, root = self._repo()
        self.addCleanup(temporary.cleanup)
        runner = agent_runner.AgentRunner(root)
        task = {"id": "T9", "title": "Interrupted", "status": "todo", "subtasks": []}
        (root / ".grc" / "tasks.json").write_text(json.dumps([task]))
        workflow.claim_task(root / ".grc" / "tasks.json", "T9", "local/qwen", run_id="impl-stale")
        evidence = root.parent / f"{root.name}-preserved-worktree"
        evidence.mkdir()
        self.addCleanup(shutil.rmtree, evidence, True)
        runner._save_run({"run_id": "impl-stale", "kind": "implementation", "task_id": "T9", "agent": "local/qwen", "status": "running", "started_at": "2000-01-01T00:00:00Z", "worktree": str(evidence)})
        runner._project()
        runner._commit_metadata("seed stale run")
        self.assertEqual(runner.recover_stale_runs(60), ["impl-stale"])
        recovered = json.loads((root / ".grc" / "tasks.json").read_text())[0]
        self.assertEqual(recovered["status"], "todo")
        self.assertIsNone(recovered.get("claim"))
        self.assertTrue(evidence.exists())

    def test_task_verification_commands_are_enforced(self):
        temporary, root = self._repo()
        self.addCleanup(temporary.cleanup)
        task = {
            "id": "T1", "title": "Verify", "status": "todo", "revision": 1,
            "verification_commands": [[sys.executable, "-c", "raise SystemExit(7)"]],
        }
        (root / ".grc" / "tasks.json").write_text(json.dumps([task]))
        subprocess.run(["git", "add", ".grc/tasks.json"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "task"], cwd=root, check=True, capture_output=True)
        local = self._script(root, "local_verify.py", """
            from pathlib import Path
            Path("app.py").write_text("value = 2\\n")
        """)
        with self.assertRaisesRegex(agent_runner.RunnerError, "Test failed"):
            agent_runner.AgentRunner(root).run_local(
                "T1", "local/test", [sys.executable, str(local), "{prompt_file}", "{result_file}"], tests=[]
            )

    def test_owned_and_forbidden_paths_are_enforced(self):
        temporary, root = self._repo()
        self.addCleanup(temporary.cleanup)
        task = {
            "id": "T1", "title": "Scoped", "status": "todo", "revision": 1,
            "owned_paths": ["src"], "forbidden_paths": [".grc"],
        }
        (root / ".grc" / "tasks.json").write_text(json.dumps([task]))
        subprocess.run(["git", "add", ".grc/tasks.json"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "task"], cwd=root, check=True, capture_output=True)
        local = self._script(root, "local_scope.py", """
            from pathlib import Path
            Path("app.py").write_text("value = 2\\n")
        """)
        with self.assertRaisesRegex(agent_runner.RunnerError, "outside owned paths"):
            agent_runner.AgentRunner(root).run_local(
                "T1", "local/test", [sys.executable, str(local), "{prompt_file}", "{result_file}"], tests=[]
            )

    def test_provider_output_is_stopped_at_the_hard_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(agent_runner.RunnerError, "1 MB"):
                agent_runner._run(
                    [sys.executable, "-c", "import sys; sys.stdout.write('x' * 1100000)"],
                    Path(temp), 10,
                )

    def test_local_provider_cannot_create_unreviewed_commits(self):
        temporary, root = self._repo()
        self.addCleanup(temporary.cleanup)
        (root / ".grc" / "tasks.json").write_text(json.dumps([
            {"id": "T8", "title": "No provider commits", "status": "todo", "revision": 1}
        ]))
        subprocess.run(["git", "add", ".grc/tasks.json"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "task"], cwd=root, check=True, capture_output=True)
        committer = self._script(root, "committer.py", """
            from pathlib import Path
            import subprocess
            Path("app.py").write_text("value = 3\\n")
            subprocess.run(["git", "add", "app.py"], check=True)
            subprocess.run(["git", "commit", "-m", "provider commit"], check=True)
        """)
        with self.assertRaisesRegex(agent_runner.RunnerError, "created Git commits"):
            agent_runner.AgentRunner(root).run_local(
                "T8", "local/test", [sys.executable, str(committer), "{prompt_file}", "{result_file}"]
            )
        task = json.loads((root / ".grc" / "tasks.json").read_text())[0]
        self.assertEqual(task["status"], "todo")
        self.assertIsNone(task.get("claim"))


if __name__ == "__main__":
    unittest.main()
