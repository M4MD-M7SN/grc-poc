"""Runs the audit / fix / review loop with whichever agent CLIs are configured."""
from __future__ import annotations

import datetime as dt
import contextlib
import hashlib
import json
import os
import shlex
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Union

import genie
import grc
import workflow


class RunnerError(RuntimeError):
    pass


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _run(command: list[str], cwd: Path, timeout: int, env: Optional[Dict[str, str]] = None,
         sanitize: bool = False) -> subprocess.CompletedProcess:
    process_env = dict(os.environ)
    if sanitize:
        safe_names = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "OLLAMA_HOST", "NO_PROXY"}
        process_env = {key: value for key, value in process_env.items() if key in safe_names}
    process_env.update(env or {})
    process = None
    limit = 1_000_000
    try:
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stdout_file, \
                tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr_file:
            process = subprocess.Popen(
                command, cwd=cwd, text=True, stdout=stdout_file, stderr=stderr_file,
                env=process_env, start_new_session=True,
            )
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                size = os.fstat(stdout_file.fileno()).st_size + os.fstat(stderr_file.fileno()).st_size
                if size > limit:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    raise RunnerError("Agent output exceeded the 1 MB safety limit")
                if time.monotonic() >= deadline:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    raise RunnerError(f"Agent command timed out after {timeout}s")
                time.sleep(0.05)
            size = os.fstat(stdout_file.fileno()).st_size + os.fstat(stderr_file.fileno()).st_size
            if size > limit:
                raise RunnerError("Agent output exceeded the 1 MB safety limit")
            stdout_file.seek(0)
            stderr_file.seek(0)
            return subprocess.CompletedProcess(command, process.returncode, stdout_file.read(), stderr_file.read())
    except OSError as exc:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        raise RunnerError(f"Could not launch agent command: {exc}") from exc


def _expand(command: Iterable[str], values: dict[str, str]) -> list[str]:
    expanded = []
    for part in command:
        value = str(part)
        for key, replacement in values.items():
            value = value.replace("{" + key + "}", replacement)
        expanded.append(value)
    if not expanded:
        raise RunnerError("Agent command is empty")
    return expanded


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)
    if check and result.returncode:
        raise RunnerError(result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed")
    return result


def _json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _json_object(text: str) -> dict:
    """Accept strict JSON or a single fenced JSON object from a provider CLI."""
    raw = text.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if len(lines) > 2 and lines[-1].strip() == "```" else lines[1:])
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(raw[start:end + 1])
    if not isinstance(value, dict):
        raise json.JSONDecodeError("Expected JSON object", raw, 0)
    return value


class AgentRunner:
    """Run untrusted local implementation only in a Git worktree; keep superior passes read-only."""

    def __init__(self, root: Union[Path, str], timeout: int = 900):
        self.root = Path(root).expanduser().resolve()
        self.grc = self.root / ".grc"
        self.timeout = timeout
        if not self.grc.is_dir():
            raise RunnerError(f"No .grc project at {self.root}")
        if _git(self.root, "rev-parse", "--is-inside-work-tree", check=False).stdout.strip() != "true":
            raise RunnerError("Agent orchestration requires a Git repository")
        self.runs = self.grc / "runs"
        self.runs.mkdir(parents=True, exist_ok=True)

    def _assert_clean(self) -> None:
        dirty = _git(self.root, "status", "--porcelain").stdout.strip()
        if dirty:
            raise RunnerError("Project worktree must be clean before an agent run:\n" + dirty)

    def _run_path(self, run_id: str) -> Path:
        return self.runs / f"{run_id}.json"

    def _save_run(self, record: dict) -> None:
        path = self._run_path(record["run_id"])
        workflow.atomic_write_json(path, record)

    def _project(self) -> None:
        grc.build(self.root, rescan=False)

    def _commit_metadata(self, message: str) -> str:
        _git(self.root, "add", ".grc")
        if (self.root / "index.html").exists():
            _git(self.root, "add", "index.html")
        staged = _git(self.root, "diff", "--cached", "--quiet", check=False)
        if staged.returncode == 0:
            return _git(self.root, "rev-parse", "HEAD").stdout.strip()
        _git(self.root, "commit", "-m", message)
        return _git(self.root, "rev-parse", "HEAD").stdout.strip()

    def _agent_io(self, kind: str, run_id: str, prompt: str, command: Iterable[str], cwd: Path,
                  timeout: int | None = None) -> tuple[dict, subprocess.CompletedProcess]:
        run_dir = Path(tempfile.mkdtemp(prefix=f"grc-{kind}-{run_id}-"))
        prompt_file = run_dir / "prompt.md"
        result_file = run_dir / "result.json"
        prompt_file.write_text(prompt)
        values = {
            "prompt_file": str(prompt_file), "result_file": str(result_file),
            "project_dir": str(cwd), "run_id": run_id, "prompt": prompt,
            "runner_dir": str(Path(__file__).resolve().parent),
        }
        argv = _expand(command, values)
        protected_git = kind in {"audit", "review"} and _git(cwd, "rev-parse", "--is-inside-work-tree", check=False).stdout.strip() == "true"
        before = _git(cwd, "status", "--porcelain").stdout if protected_git else ""
        private_home = run_dir / "home"
        private_home.mkdir(mode=0o700)
        result = _run(
            argv, cwd, timeout or self.timeout,
            {"GRC_PROMPT_FILE": str(prompt_file), "GRC_RESULT_FILE": str(result_file), "GRC_RUN_ID": run_id,
             **({"HOME": str(private_home)} if kind == "local" else {})},
            sanitize=(kind == "local"),
        )
        if protected_git and _git(cwd, "status", "--porcelain").stdout != before:
            raise RunnerError(f"The {kind} agent modified the protected project worktree")
        if result.returncode:
            raise RunnerError(f"{kind} agent failed ({result.returncode}): {(result.stderr or result.stdout)[-2000:]}")
        if result_file.exists():
            payload = _json(result_file)
        else:
            if kind == "local":
                payload = {}
            else:
                try:
                    payload = _json_object(result.stdout)
                except json.JSONDecodeError as exc:
                    raise RunnerError(f"{kind} agent did not produce valid JSON") from exc
        if not isinstance(payload, dict):
            raise RunnerError(f"{kind} agent result must be a JSON object")
        return payload, result

    def run_audit(self, auditor: str, command: Iterable[str], max_findings: int = 100) -> list[str]:
        self._assert_clean()
        workflow.authorize_role(self.root, "superior_auditors", auditor, require_policy=True)
        run_id = "audit-" + uuid.uuid4().hex[:12]
        capsule = genie.context_capsule(self.root, max_bytes=24000, persist=False)
        prompt = "You are the superior read-only repository auditor. Inspect the repository and return JSON only.\n"
        prompt += 'Schema: {"findings":[{"title":"...","severity":"critical|high|medium|low","evidence":[{"path":"relative/file","line":1,"message":"..."}],"acceptance_criteria":["..."]}]}\n'
        prompt += "Every finding must have real file/line evidence and testable acceptance criteria. Do not edit files.\n\nContext capsule:\n"
        prompt += json.dumps(capsule, ensure_ascii=False)
        audit_parent = self.root.parent / ".grc-worktrees" / self.root.name
        audit_parent.mkdir(parents=True, exist_ok=True)
        audit_tree = audit_parent / run_id
        _git(self.root, "worktree", "add", "--detach", str(audit_tree), "HEAD")
        try:
            payload, _ = self._agent_io("audit", run_id, prompt, command, audit_tree)
        finally:
            if audit_tree.exists():
                _git(self.root, "worktree", "remove", "--force", str(audit_tree))
        findings = payload.get("findings")
        if not isinstance(findings, list) or len(findings) > max_findings:
            raise RunnerError(f"Audit findings must be a list of at most {max_findings}")
        task_ids = []
        for finding in findings:
            if not isinstance(finding, dict):
                raise RunnerError("Each audit finding must be an object")
            task, _created = workflow.audit_upsert_task(
                self.root, auditor, finding.get("title", ""), finding.get("evidence") or [],
                finding.get("acceptance_criteria") or [], severity=finding.get("severity", "medium"),
                base_revision=_git(self.root, "rev-parse", "HEAD").stdout.strip(),
                notes=finding.get("notes", ""),
            )
            task_ids.append(task["id"])
        record = {"run_id": run_id, "kind": "audit", "agent": auditor, "status": "completed", "tasks": task_ids, "at": _now()}
        self._save_run(record)
        grc.append_event(self.grc, auditor, f"Superior audit recorded {len(task_ids)} finding(s)", kind="audit")
        self._project()
        self._commit_metadata(f"GRC audit: record {len(task_ids)} finding(s)")
        self._assert_clean()
        return task_ids

    def run_local(self, task_id: str, agent: str, command: Iterable[str], tests: Iterable[Iterable[str]] = ()) -> dict:
        self._assert_clean()
        workflow.authorize_role(self.root, "local_implementers", agent, require_policy=True)
        run_id = "impl-" + uuid.uuid4().hex[:12]
        task = workflow.claim_task(self.grc / "tasks.json", task_id, agent, run_id=run_id)
        task_timeout = int(task.get("time_limit_seconds") or self.timeout)
        task_timeout = min(self.timeout, task_timeout)
        worktrees = self.root.parent / ".grc-worktrees" / self.root.name
        worktree = worktrees / run_id
        branch = f"grc/{task_id.lower()}-{run_id[-8:]}"
        record = {
            "run_id": run_id, "kind": "implementation", "task_id": task_id, "agent": agent,
            "branch": branch, "worktree": str(worktree), "status": "setup", "started_at": _now(),
            "base_sha": "", "lease_token": task["claim"]["lease_token"],
            "isolation": "git-worktree", "network_policy": "inherited",
        }
        self._save_run(record)
        try:
            grc.append_event(self.grc, agent, f"Claimed {task_id} for isolated run {run_id}", kind="claim", task=task_id)
            self._project()
            self._commit_metadata(f"GRC: {agent} claims {task_id}")
            base_sha = _git(self.root, "rev-parse", "HEAD").stdout.strip()
            worktrees.mkdir(parents=True, exist_ok=True)
            _git(self.root, "worktree", "add", "-b", branch, str(worktree), "HEAD")
            record.update({"status": "running", "base_sha": base_sha})
            self._save_run(record)
        except Exception as exc:
            record.update({"status": "failed", "error": f"setup failed: {exc}", "finished_at": _now()})
            self._save_run(record)
            with contextlib.suppress(workflow.WorkflowError):
                workflow.release_claim(self.grc / "tasks.json", task_id, agent, run_id, reason="setup-failed")
            with contextlib.suppress(Exception):
                self._project()
                self._commit_metadata(f"GRC: record failed setup for {task_id}")
            raise
        capsule = genie.context_capsule(self.root, focus=task_id, max_bytes=18000, persist=False)
        prompt = """You are the local implementation agent. Work only on the claimed task below, inside this isolated Git worktree.\nDo not modify .grc metadata, do not broaden scope, and do not commit. Implement the acceptance criteria and run useful checks.\n\nTask:\n"""
        prompt += json.dumps(task, ensure_ascii=False, indent=2) + "\n\nContext capsule:\n" + json.dumps(capsule, ensure_ascii=False)
        values = {"task_id": task_id}
        expanded = [str(part).replace("{task_id}", task_id) for part in command]
        try:
            _payload, result = self._agent_io("local", run_id, prompt, expanded, worktree, timeout=task_timeout)
            if _git(worktree, "rev-parse", "HEAD").stdout.strip() != base_sha:
                raise RunnerError("Local agent created Git commits; the runner requires an unchanged base HEAD")
            changed = [line for line in _git(worktree, "status", "--porcelain").stdout.splitlines() if line]
            if any(line[3:].startswith(".grc/") or line[3:] == ".grc" for line in changed):
                raise RunnerError("Local agent modified protected .grc metadata")
            if not changed:
                raise RunnerError("Local agent made no changes")
            test_results = []
            task_tests = task.get("verification_commands") or []
            for raw in [*tests, *task_tests]:
                argv = [str(part) for part in raw]
                checked = _run(argv, worktree, task_timeout)
                digest = hashlib.sha256((checked.stdout + checked.stderr).encode()).hexdigest()
                item = {"command": shlex.join(argv), "exit_code": checked.returncode, "output_sha256": digest,
                        "output_tail": (checked.stdout + checked.stderr)[-2000:]}
                test_results.append(item)
                if checked.returncode:
                    raise RunnerError(f"Test failed: {item['command']}\n{item['output_tail']}")
            _git(worktree, "add", "-A")
            _git(worktree, "commit", "-m", f"Fix {task_id}: {task.get('title', '')}",
                 "-m", f"GRC-Task: {task_id}\nGRC-Run: {run_id}\nGRC-Agent: {agent}")
            commit = _git(worktree, "rev-parse", "HEAD").stdout.strip()
            patch = _git(worktree, "diff", "--binary", base_sha, commit).stdout
            patch_sha256 = hashlib.sha256(patch.encode()).hexdigest()
            files = _git(worktree, "diff", "--name-only", base_sha, commit).stdout.splitlines()
            owned = [str(path).rstrip("/") for path in task.get("owned_paths") or []]
            forbidden = [str(path).rstrip("/") for path in task.get("forbidden_paths") or []]
            in_scope = lambda relative, scope: relative == scope or relative.startswith(scope + "/")
            if owned:
                outside = [relative for relative in files if not any(in_scope(relative, scope) for scope in owned)]
                if outside:
                    raise RunnerError("Local agent changed files outside owned paths: " + ", ".join(outside))
            prohibited = [relative for relative in files if any(in_scope(relative, scope) for scope in forbidden)]
            if prohibited:
                raise RunnerError("Local agent changed forbidden paths: " + ", ".join(prohibited))
            for relative in files:
                candidate = worktree / relative
                if candidate.is_symlink():
                    raise RunnerError(f"Local agent produced a symlink, which is outside the safe change contract: {relative}")
                mode = _git(worktree, "ls-tree", commit, "--", relative).stdout
                if mode.startswith("160000 "):
                    raise RunnerError(f"Local agent may not introduce or modify a Git submodule: {relative}")
            workflow.submit_for_review(
                self.grc / "tasks.json", task_id, agent,
                summary=f"Local agent completed {task.get('title', task_id)}",
                files=files, tests=test_results, commit=commit, run_id=run_id,
                lease_token=task["claim"]["lease_token"], base_sha=base_sha,
                head_sha=commit, patch_sha256=patch_sha256,
            )
            record.update({"status": "awaiting_review", "commit": commit, "files": files,
                           "head_sha": commit, "patch_sha256": patch_sha256,
                           "tests": test_results, "agent_exit_code": result.returncode, "finished_at": _now()})
            self._save_run(record)
            grc.append_event(self.grc, agent, f"Submitted {task_id} run {run_id} for superior review", kind="review-request", task=task_id)
            self._project()
            self._commit_metadata(f"GRC: submit {task_id} for review")
            self._assert_clean()
            return record
        except Exception as exc:
            record.update({"status": "failed", "error": str(exc), "finished_at": _now()})
            self._save_run(record)
            with contextlib.suppress(workflow.WorkflowError):
                workflow.release_claim(self.grc / "tasks.json", task_id, agent, run_id, reason="run-failed")
            grc.append_event(self.grc, agent, f"Implementation run {run_id} failed: {str(exc)[:300]}", kind="run-failed", task=task_id)
            self._project()
            self._commit_metadata(f"GRC: record failed run for {task_id}")
            raise

    def run_review(self, task_id: str, reviewer: str, command: Iterable[str]) -> dict:
        self._assert_clean()
        workflow.authorize_role(self.root, "superior_reviewers", reviewer, require_policy=True)
        tasks = _json(self.grc / "tasks.json", [])
        task = next((item for item in tasks if item.get("id") == task_id), None)
        if not task or task.get("status") != "review":
            raise RunnerError(f"Task {task_id} is not awaiting review")
        implementation = task.get("implementation") or {}
        run_id = implementation.get("run_id")
        record = _json(self._run_path(run_id), {}) if run_id else {}
        commit = implementation.get("commit")
        if not run_id or not commit or not record:
            raise RunnerError("Review task has no linked implementation run and commit")
        base_sha = implementation.get("base_sha")
        expected_head = implementation.get("head_sha") or commit
        expected_patch = implementation.get("patch_sha256")
        if expected_head != commit or not base_sha or not expected_patch:
            raise RunnerError("Implementation submission is missing immutable Git evidence")
        actual_patch = _git(self.root, "diff", "--binary", base_sha, commit).stdout
        if hashlib.sha256(actual_patch.encode()).hexdigest() != expected_patch:
            raise RunnerError("Implementation patch no longer matches the submitted digest")
        diff = actual_patch
        if len(diff.encode()) > 80_000:
            raise RunnerError("Implementation diff exceeds the 80 KB review limit")
        capsule = genie.context_capsule(self.root, focus=task_id, max_bytes=18000, persist=False)
        prompt = """You are the independent superior reviewer. Review the implementation against the evidence and acceptance criteria. Do not edit files. Return JSON only.\nSchema: {"decision":"accept|reject","findings":"specific review explanation","reviewed_head_sha":"exact submitted head","reviewed_patch_sha256":"exact submitted patch digest"}\nAccept only when the diff and tests are sufficient.\n\nTask:\n"""
        prompt += json.dumps(task, ensure_ascii=False, indent=2)
        prompt += "\n\nImplementation diff:\n```diff\n" + diff + "\n```\n\nContext capsule:\n" + json.dumps(capsule, ensure_ascii=False)
        review_run_id = "review-" + uuid.uuid4().hex[:12]
        review_parent = self.root.parent / ".grc-worktrees" / self.root.name
        review_parent.mkdir(parents=True, exist_ok=True)
        review_tree = review_parent / review_run_id
        _git(self.root, "worktree", "add", "--detach", str(review_tree), commit)
        try:
            payload, _ = self._agent_io("review", review_run_id, prompt, command, review_tree)
        finally:
            if review_tree.exists():
                _git(self.root, "worktree", "remove", "--force", str(review_tree))
        decision = payload.get("decision")
        findings = str(payload.get("findings", "")).strip()
        if decision not in {"accept", "reject"} or not findings:
            raise RunnerError("Reviewer must return decision accept|reject and non-empty findings")
        if payload.get("reviewed_head_sha") != expected_head or payload.get("reviewed_patch_sha256") != expected_patch:
            raise RunnerError("Reviewer decision is not bound to the submitted head and patch digest")
        integration_parent = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        try:
            if decision == "accept":
                picked = _git(self.root, "cherry-pick", commit, check=False)
                if picked.returncode:
                    _git(self.root, "cherry-pick", "--abort", check=False)
                    raise RunnerError(f"Reviewed patch could not be integrated cleanly: {picked.stderr[-1000:]}")
                integrated_patch = _git(self.root, "show", "--format=", "--binary", "HEAD").stdout
                if hashlib.sha256(integrated_patch.encode()).hexdigest() != expected_patch:
                    _git(self.root, "reset", "--hard", integration_parent)
                    raise RunnerError("Integrated patch differs from the reviewed patch")
            reviewed = workflow.review_task(
                self.grc / "tasks.json", task_id, reviewer, decision, findings, reviewer_tier="superior"
            )
        except Exception:
            git_dir = _git(self.root, "rev-parse", "--git-dir", check=False).stdout.strip()
            if git_dir and (self.root / git_dir / "CHERRY_PICK_HEAD").exists():
                _git(self.root, "cherry-pick", "--abort", check=False)
            if decision == "accept" and _git(self.root, "rev-parse", "HEAD").stdout.strip() != integration_parent:
                _git(self.root, "reset", "--hard", integration_parent)
            raise
        record.update({"status": "integrated" if decision == "accept" else "rejected",
                       "reviewer": reviewer, "review": {"decision": decision, "findings": findings},
                       "reviewed_at": _now()})
        self._save_run(record)
        grc.append_event(
            self.grc, reviewer,
            f"Superior review {decision} for {task_id} head {expected_head} patch {expected_patch}: {findings[:300]}",
            kind="review", task=task_id,
        )
        worktree = Path(record.get("worktree", ""))
        branch = record.get("branch")
        if worktree.exists():
            _git(self.root, "worktree", "remove", "--force", str(worktree))
        if decision == "accept" and branch:
            _git(self.root, "branch", "-D", branch)
        self._project()
        self._commit_metadata(f"GRC: {decision} {task_id}")
        self._assert_clean()
        return {"task": reviewed, "decision": decision, "findings": findings, "run": record}

    def recover_stale_runs(self, max_age_seconds: int = 3600) -> list[str]:
        """Mark abandoned implementation runs interrupted and release their exact claims."""
        recovered = []
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=max(60, max_age_seconds))
        runs_dir = self.grc / "runs"
        for path in sorted(runs_dir.glob("impl-*.json")) if runs_dir.exists() else []:
            record = _json(path, {})
            if record.get("status") not in {"setup", "running"}:
                continue
            try:
                started = dt.datetime.fromisoformat(str(record.get("started_at", "")).replace("Z", "+00:00"))
                if started.tzinfo is None:
                    started = started.replace(tzinfo=dt.timezone.utc)
            except ValueError:
                started = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
            if started > cutoff:
                continue
            with contextlib.suppress(workflow.WorkflowError):
                workflow.release_claim(
                    self.grc / "tasks.json", record.get("task_id", ""), record.get("agent", ""),
                    record.get("run_id", ""), reason="crash-recovery",
                )
            record.update({"status": "interrupted", "recovered_at": _now()})
            self._save_run(record)
            grc.append_event(
                self.grc, "grc/recovery", f"Recovered interrupted run {record.get('run_id')}",
                kind="recovery", task=record.get("task_id"),
            )
            recovered.append(record.get("run_id", path.stem))
        active_run_ids = {
            str(_json(path, {}).get("run_id"))
            for path in (sorted(runs_dir.glob("impl-*.json")) if runs_dir.exists() else [])
            if _json(path, {}).get("status") in {"setup", "running"}
        }
        now = dt.datetime.now(dt.timezone.utc)
        for task in _json(self.grc / "tasks.json", []):
            claim = task.get("claim") or {}
            if task.get("status") != "doing" or not claim:
                continue
            try:
                expires = dt.datetime.fromisoformat(str(claim.get("lease_expires_at", "")).replace("Z", "+00:00"))
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=dt.timezone.utc)
            except ValueError:
                expires = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
            run_id = str(claim.get("run_id") or "")
            if expires <= now and run_id not in active_run_ids:
                with contextlib.suppress(workflow.WorkflowError):
                    workflow.release_claim(
                        self.grc / "tasks.json", task.get("id", ""), claim.get("agent", ""),
                        run_id, reason="orphaned-expired-claim",
                    )
                    recovered.append(run_id or f"claim:{task.get('id')}")
        if recovered:
            self._project()
            self._commit_metadata(f"GRC: recover {len(recovered)} interrupted run(s)")
        return recovered
