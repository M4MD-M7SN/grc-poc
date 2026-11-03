import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import agent_cli
import workflow


ROOT = Path(__file__).resolve().parents[1]


class AgentCliErrorBoundaryTests(unittest.TestCase):
    def assert_workflow_error_is_concise(self, action, runner_method):
        stderr = io.StringIO()
        argv = ["--dir", str(ROOT), action]
        if action in {"implement", "review"}:
            argv.append("T24")
        with mock.patch.object(agent_cli, "AgentRunner") as runner_class, mock.patch.object(
            agent_cli, "profile", return_value=({}, ["fake-runner"])
        ), redirect_stderr(stderr):
            getattr(runner_class.return_value, runner_method).side_effect = workflow.WorkflowError(
                "unauthorized profile"
            )
            result = agent_cli.main(argv)
        self.assertEqual(result, 2)
        self.assertEqual(stderr.getvalue(), "GRC runner error: unauthorized profile\n")
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_unauthorized_audit_profile_has_stable_cli_error(self):
        self.assert_workflow_error_is_concise("audit", "run_audit")

    def test_unauthorized_implementation_profile_has_stable_cli_error(self):
        self.assert_workflow_error_is_concise("implement", "run_local")

    def test_unauthorized_review_profile_has_stable_cli_error(self):
        self.assert_workflow_error_is_concise("review", "run_review")

    def test_unexpected_programming_errors_are_not_swallowed(self):
        with mock.patch.object(agent_cli, "AgentRunner") as runner_class, mock.patch.object(
            agent_cli, "profile", return_value=({}, ["fake-runner"])
        ), self.assertRaisesRegex(ValueError, "programming fault"):
            runner_class.return_value.run_audit.side_effect = ValueError("programming fault")
            agent_cli.main(["--dir", str(ROOT), "audit"])


if __name__ == "__main__":
    unittest.main()
