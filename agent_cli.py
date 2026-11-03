#!/usr/bin/env python3
"""Command line entry for the audit / fix / review loop."""
import argparse
import json
import shlex
import sys
from pathlib import Path

import workflow
from agent_runner import AgentRunner, RunnerError


def load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def profile(project, name):
    data = (((project.get("workflow") or {}).get("runners") or {}).get(name) or {})
    command = data.get("command")
    if isinstance(command, str):
        command = shlex.split(command)
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        raise RunnerError(f"Configure workflow.runners.{name}.command as an argv array in .grc/project.json")
    return data, command


def tests_for(data):
    tests = []
    for item in data.get("tests") or []:
        tests.append(shlex.split(item) if isinstance(item, str) else item)
    return tests


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=".")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("audit")
    local = sub.add_parser("implement")
    local.add_argument("task")
    review = sub.add_parser("review")
    review.add_argument("task")
    loop = sub.add_parser("loop")
    loop.add_argument("--limit", type=int, default=1)
    loop.add_argument("--audit-first", action="store_true")
    recover = sub.add_parser("recover")
    recover.add_argument("--age", type=int, default=3600)
    args = parser.parse_args(argv)

    root = Path(args.dir).expanduser().resolve()
    project = load(root / ".grc" / "project.json", {})
    runner = AgentRunner(root)
    try:
        if args.action == "audit":
            data, command = profile(project, "audit")
            result = runner.run_audit(data.get("agent", "superior/auditor"), command)
        elif args.action == "implement":
            data, command = profile(project, "local")
            result = runner.run_local(args.task, data.get("agent", "local/agent"), command, tests_for(data))
        elif args.action == "review":
            data, command = profile(project, "review")
            result = runner.run_review(args.task, data.get("agent", "superior/reviewer"), command)
        elif args.action == "recover":
            result = runner.recover_stale_runs(args.age)
        else:
            if args.audit_first:
                data, command = profile(project, "audit")
                runner.run_audit(data.get("agent", "superior/auditor"), command)
            completed = []
            for _ in range(max(0, args.limit)):
                tasks = load(root / ".grc" / "tasks.json", [])
                candidates = [item for item in tasks if item.get("status") == "todo"]
                candidates.sort(key=lambda item: ({"critical": 0, "high": 1, "medium": 2, "low": 3}.get(item.get("severity"), 4), item.get("id", "")))
                if not candidates:
                    break
                task_id = candidates[0]["id"]
                local_data, local_command = profile(project, "local")
                runner.run_local(task_id, local_data.get("agent", "local/agent"), local_command, tests_for(local_data))
                review_data, review_command = profile(project, "review")
                outcome = runner.run_review(task_id, review_data.get("agent", "superior/reviewer"), review_command)
                completed.append({"task": task_id, "decision": outcome["decision"]})
            result = completed
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (RunnerError, workflow.WorkflowError) as exc:
        print(f"GRC runner error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
