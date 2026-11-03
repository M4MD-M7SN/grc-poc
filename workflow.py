"""Audit, implement, review: task lifecycle with atomic writes."""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any, Callable, List, Optional, TypeVar


class WorkflowError(ValueError):
    """Raised when a workflow transition violates the project contract."""


T = TypeVar("T")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _parse_time(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def _read_tasks(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Cannot read task store: {exc}") from exc
    if not isinstance(data, list):
        raise WorkflowError("Task store must contain a JSON array")
    return data


def _atomic_write_tasks(path: Path, tasks: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(tasks, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)


def atomic_write_json(path: Path, value: Any) -> None:
    """Atomically persist a JSON projection such as a run record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)


def _mutate(path: Path, operation: Callable[[list[dict[str, Any]]], T]) -> T:
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        tasks = _read_tasks(path)
        result = operation(tasks)
        _atomic_write_tasks(path, tasks)
        return result


def mutate_tasks(path: Path, operation: Callable[[list[dict[str, Any]]], Any]) -> Any:
    """Apply any local task-store mutation under the canonical lock."""
    return _mutate(Path(path), operation)


def _task(tasks: list[dict[str, Any]], task_id: str) -> dict[str, Any]:
    for item in tasks:
        if item.get("id") == task_id:
            return item
    raise WorkflowError(f"Unknown task {task_id}")


def _bump(task: dict[str, Any]) -> None:
    task["revision"] = int(task.get("revision") or 0) + 1
    task["updated_at"] = _now()


def _lease_active(task: dict[str, Any], now: Optional[dt.datetime] = None) -> bool:
    claim = task.get("claim") or {}
    expires = _parse_time(claim.get("lease_expires_at"))
    current = now or dt.datetime.now(dt.timezone.utc)
    return bool(task.get("status") == "doing" and expires and expires > current)


def _claim_audit(claim: dict[str, Any]) -> dict[str, Any]:
    """Keep claim provenance without persisting the bearer lease credential."""
    return {key: value for key, value in claim.items() if key != "lease_token"}


def _validate_relative_evidence(root: Path, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not evidence:
        raise WorkflowError("Audit evidence must contain at least one file/line finding")
    normalized = []
    root = root.resolve()
    for item in evidence:
        relative = Path(str(item.get("path") or ""))
        line = item.get("line")
        message = str(item.get("message") or "").strip()
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise WorkflowError(f"Evidence path must be project-relative: {relative}")
        resolved = (root / relative).resolve()
        if root not in resolved.parents and resolved != root:
            raise WorkflowError(f"Evidence path escapes project: {relative}")
        if not resolved.is_file():
            raise WorkflowError(f"Evidence file does not exist: {relative}")
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            raise WorkflowError(f"Evidence line must be a positive integer: {relative}")
        line_count = len(resolved.read_text(errors="replace").splitlines())
        if line > line_count:
            raise WorkflowError(f"Evidence line {line} exceeds {relative} ({line_count} lines)")
        if not message:
            raise WorkflowError(f"Evidence message is required: {relative}:{line}")
        normalized.append({"path": relative.as_posix(), "line": line, "message": message})
    return sorted(normalized, key=lambda item: (item["path"], item["line"], item["message"]))


def _finding_fingerprint(title: str, evidence: list[dict[str, Any]]) -> str:
    payload = {"title": " ".join(title.lower().split()), "evidence": evidence}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _authorize(root: Path, role: str, actor: str, require_policy: bool = False) -> None:
    """Apply local capability policy when a project declares one."""
    try:
        project = json.loads((root / ".grc" / "project.json").read_text())
    except (OSError, json.JSONDecodeError):
        if require_policy:
            raise WorkflowError(f"Project has no valid authorization policy for {role}")
        return
    allowed = (((project.get("workflow") or {}).get("roles") or {}).get(role)) or []
    if require_policy and not allowed:
        raise WorkflowError(f"Project authorization policy has no actors for {role}")
    if allowed and actor not in allowed:
        raise WorkflowError(f"Actor {actor} is not authorized for role {role}")


def authorize_role(root: Path, role: str, actor: str, require_policy: bool = False) -> None:
    """Public policy check for orchestrators before expensive or side-effecting work."""
    _authorize(Path(root).resolve(), role, actor, require_policy=require_policy)


def _review_required(root: Path, require_policy: bool = False) -> bool:
    try:
        project = json.loads((root / ".grc" / "project.json").read_text())
    except (OSError, json.JSONDecodeError):
        if require_policy:
            raise WorkflowError("Project has no valid review policy")
        return False
    workflow_policy = project.get("workflow") or {}
    value = workflow_policy.get("require_review", False)
    if not isinstance(value, bool):
        raise WorkflowError("workflow.require_review must be a boolean")
    return value


def _dependency_complete(root: Path, task: dict[str, Any], require_policy: bool = False) -> bool:
    if task.get("status") != "done":
        return False
    return not _review_required(root, require_policy=require_policy) or (task.get("review") or {}).get("status") == "accepted"


def _next_id(tasks: list[dict[str, Any]]) -> str:
    numbers = []
    for task in tasks:
        value = str(task.get("id") or "")
        if value.startswith("T") and value[1:].isdigit():
            numbers.append(int(value[1:]))
    return f"T{max(numbers, default=0) + 1}"


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise WorkflowError(f"{field} must be an array")
    result = []
    for item in value:
        if not isinstance(item, str):
            raise WorkflowError(f"{field} values must be strings")
        text = item.strip()
        if not text:
            raise WorkflowError(f"{field} cannot contain empty values")
        result.append(text)
    return list(dict.fromkeys(result))


def _command_list(value: Any) -> list[list[str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise WorkflowError("Verification commands must be an array")
    commands = []
    for command in value:
        if isinstance(command, list) and command and all(isinstance(part, str) and part for part in command):
            commands.append(command)
            continue
        raise WorkflowError("Each verification command must be a non-empty argv array")
    return commands


def _safe_paths(value: Any, field: str) -> list[str]:
    paths = _string_list(value, field)
    for raw in paths:
        candidate = Path(raw)
        if candidate.is_absolute() or ".." in candidate.parts or raw.startswith("~"):
            raise WorkflowError(f"{field} must contain project-relative paths")
    return paths


def _string_field(spec: dict[str, Any], field: str, *, required: bool = False, default: str = "") -> str:
    value = spec.get(field, default)
    if not isinstance(value, str):
        raise WorkflowError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise WorkflowError(f"{field} is required")
    return value


def plan_task(root: Path, project_lead: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Add a task with its acceptance criteria and dependencies."""
    root = Path(root).resolve()
    if not isinstance(project_lead, str) or not project_lead.strip():
        raise WorkflowError("Project lead identity is required")
    _authorize(root, "project_leads", project_lead, require_policy=True)
    if not isinstance(spec, dict):
        raise WorkflowError("Task spec must be an object")
    allowed_fields = {
        "title", "position", "parent_goal", "purpose", "inputs", "outputs", "owned_paths",
        "forbidden_paths", "dependencies", "interface_contract", "acceptance_criteria",
        "verification_commands", "source_strategy", "time_limit_seconds", "integration_order", "notes",
    }
    unknown_fields = sorted(set(spec) - allowed_fields)
    if unknown_fields:
        raise WorkflowError(f"Unknown task spec fields: {', '.join(unknown_fields)}")
    title = _string_field(spec, "title", required=True)
    criteria = _string_list(spec.get("acceptance_criteria"), "Acceptance criteria")
    if not criteria:
        raise WorkflowError("At least one acceptance criterion is required")
    strategy = _string_field(spec, "source_strategy", default="build")
    if strategy not in {"build", "reuse", "research"}:
        raise WorkflowError("Source strategy must be build, reuse, or research")
    time_limit = spec.get("time_limit_seconds")
    if time_limit is not None and (
        not isinstance(time_limit, int) or isinstance(time_limit, bool) or time_limit < 1
    ):
        raise WorkflowError("Time limit must be a positive integer")
    dependencies = _string_list(spec.get("dependencies"), "Dependencies")
    path = root / ".grc" / "tasks.json"

    def operation(tasks: list[dict[str, Any]]) -> dict[str, Any]:
        # migrate old stores in one pass so row order is preserved
        positions = [item.get("position") for item in tasks]
        valid_positions = all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 1
            for value in positions
        )
        if not valid_positions or len(set(positions)) != len(positions):
            for index, existing in enumerate(tasks, 1):
                existing["position"] = index
        known = {str(task.get("id")) for task in tasks}
        for dependency in dependencies:
            if dependency not in known:
                raise WorkflowError(f"Unknown dependency {dependency}")
        existing_positions = [task["position"] for task in tasks]
        position = spec.get("position")
        if position is None:
            position = max(existing_positions, default=0) + 1
        if not isinstance(position, int) or isinstance(position, bool) or position < 1:
            raise WorkflowError("Position must be a positive integer")
        if "position" in spec:
            for existing in tasks:
                if existing["position"] >= position:
                    existing["position"] += 1
        integration_order = spec.get("integration_order", position)
        if not isinstance(integration_order, int) or isinstance(integration_order, bool) or integration_order < 1:
            raise WorkflowError("Integration order must be a positive integer")
        now = _now()
        task = {
            "id": _next_id(tasks),
            "title": title,
            "status": "todo",
            "position": position,
            "parent_goal": _string_field(spec, "parent_goal"),
            "purpose": _string_field(spec, "purpose"),
            "inputs": _string_list(spec.get("inputs"), "Inputs"),
            "outputs": _string_list(spec.get("outputs"), "Outputs"),
            "owned_paths": _safe_paths(spec.get("owned_paths"), "Owned paths"),
            "forbidden_paths": _safe_paths(spec.get("forbidden_paths"), "Forbidden paths"),
            "dependencies": dependencies,
            "interface_contract": _string_field(spec, "interface_contract"),
            "acceptance_criteria": criteria,
            "verification_commands": _command_list(spec.get("verification_commands")),
            "source_strategy": strategy,
            "integration_order": integration_order,
            "notes": _string_field(spec, "notes"),
            "subtasks": [{"text": item, "done": False} for item in criteria],
            "done": [],
            "planning": {"project_lead": project_lead, "created_at": now},
            "revision": 1,
            "created_at": now,
            "updated_at": now,
        }
        if time_limit is not None:
            task["time_limit_seconds"] = time_limit
        tasks.append(task)
        return task.copy()

    return _mutate(path, operation)


def task_sheet(path: Path) -> list[dict[str, Any]]:
    """Return ordered task projections with dependency-derived readiness."""
    tasks = _read_tasks(Path(path))
    root = Path(path).resolve().parent.parent
    _review_required(root, require_policy=True)
    by_id = {str(item.get("id")): item for item in tasks}
    projected = []
    for index, source in enumerate(tasks, 1):
        item = source.copy()
        dependencies = [str(value) for value in item.get("dependencies") or []]
        blocked_by = [
            task_id for task_id in dependencies
            if task_id not in by_id or not _dependency_complete(root, by_id[task_id])
        ]
        status = item.get("status", "todo")
        if status == "done":
            state = "accepted" if _dependency_complete(root, item) else "blocked"
        elif status == "review":
            state = "review"
        elif status == "doing":
            state = "leased"
        elif status == "blocked" or blocked_by:
            state = "blocked"
        elif status == "todo":
            state = "ready"
        else:
            state = "blocked"
        item["dependencies"] = dependencies
        item["blocked_by"] = blocked_by
        item["ready"] = state == "ready"
        item["workflow_state"] = state
        item["_source_index"] = index
        projected.append(item)

    def order(item: dict[str, Any]) -> tuple[int, int, int]:
        position = item.get("position")
        if not isinstance(position, int) or isinstance(position, bool):
            position = 1_000_000 + item["_source_index"]
        integration = item.get("integration_order")
        if not isinstance(integration, int) or isinstance(integration, bool):
            integration = position
        return position, integration, item["_source_index"]

    projected.sort(key=order)
    for item in projected:
        item.pop("_source_index", None)
    return projected


def next_ready_task(path: Path) -> Optional[dict[str, Any]]:
    """Select, but do not claim, the first ready work object."""
    return next((task for task in task_sheet(path) if task["ready"]), None)


def audit_upsert_task(
    root: Path,
    auditor: str,
    title: str,
    evidence: list[dict[str, Any]],
    acceptance_criteria: list[str],
    *,
    severity: str = "medium",
    base_revision: str = "",
    notes: str = "",
) -> tuple[dict[str, Any], bool]:
    """Create or merge an evidence-backed finding using a stable fingerprint."""
    root = Path(root).resolve()
    if not auditor.strip():
        raise WorkflowError("Auditor identity is required")
    _authorize(root, "superior_auditors", auditor)
    if not title.strip():
        raise WorkflowError("Finding title is required")
    criteria = [str(item).strip() for item in acceptance_criteria if str(item).strip()]
    if not criteria:
        raise WorkflowError("At least one acceptance criterion is required")
    if severity not in {"critical", "high", "medium", "low"}:
        raise WorkflowError("Severity must be critical, high, medium, or low")
    normalized = _validate_relative_evidence(root, evidence)
    fingerprint = _finding_fingerprint(title, normalized)
    path = root / ".grc" / "tasks.json"

    def operation(tasks: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
        for task in tasks:
            if (task.get("audit") or {}).get("fingerprint") == fingerprint:
                merged = list(dict.fromkeys((task.get("acceptance_criteria") or []) + criteria))
                task["acceptance_criteria"] = merged
                known = {item.get("text") for item in task.get("subtasks", []) if isinstance(item, dict)}
                task.setdefault("subtasks", []).extend({"text": item, "done": False} for item in merged if item not in known)
                task.setdefault("audit", {}).setdefault("observations", []).append(
                    {"auditor": auditor, "at": _now(), "base_revision": base_revision}
                )
                _bump(task)
                return task.copy(), False
        now = _now()
        task = {
            "id": _next_id(tasks),
            "title": title.strip(),
            "status": "todo",
            "severity": severity,
            "notes": notes.strip(),
            "acceptance_criteria": criteria,
            "subtasks": [{"text": item, "done": False} for item in criteria],
            "done": [],
            "audit_evidence": normalized,
            "audit": {
                "tier": "superior",
                "auditor": auditor,
                "fingerprint": fingerprint,
                "base_revision": base_revision,
                "created_at": now,
                "observations": [{"auditor": auditor, "at": now, "base_revision": base_revision}],
            },
            "revision": 1,
            "created_at": now,
            "updated_at": now,
        }
        tasks.append(task)
        return task.copy(), True

    return _mutate(path, operation)


def claim_task(
    path: Path,
    task_id: str,
    agent: str,
    *,
    expected_revision: Optional[int] = None,
    run_id: str = "",
    lease_seconds: int = 3600,
) -> dict[str, Any]:
    if not isinstance(agent, str) or not agent.strip():
        raise WorkflowError("Agent identity is required")
    root = Path(path).resolve().parent.parent
    _authorize(root, "local_implementers", agent, require_policy=True)
    _review_required(root, require_policy=True)
    if lease_seconds < 60 or lease_seconds > 86400:
        raise WorkflowError("Lease must be between 60 and 86400 seconds")

    def operation(tasks: list[dict[str, Any]]) -> dict[str, Any]:
        now = dt.datetime.now(dt.timezone.utc)
        task = _task(tasks, task_id)
        by_id = {str(item.get("id")): item for item in tasks}
        blocked_by = [
            str(dependency) for dependency in task.get("dependencies") or []
            if str(dependency) not in by_id or not _dependency_complete(
                root, by_id[str(dependency)], require_policy=True
            )
        ]
        if blocked_by:
            raise WorkflowError(f"Task {task_id} is blocked by dependencies: {', '.join(blocked_by)}")
        if expected_revision is not None and int(task.get("revision") or 0) != expected_revision:
            raise WorkflowError(
                f"Task {task_id} revision conflict: expected {expected_revision}, found {int(task.get('revision') or 0)}"
            )
        for other in tasks:
            if other.get("id") != task_id and (other.get("claim") or {}).get("agent") == agent and _lease_active(other, now):
                raise WorkflowError(f"Agent {agent} already has active task {other.get('id')}")
        current = task.get("claim") or {}
        if task.get("status") == "doing" and _lease_active(task, now):
            if current.get("agent") == agent:
                raise WorkflowError(f"Task {task_id} is already claimed by {agent}")
            raise WorkflowError(f"Task {task_id} is already claimed by {current.get('agent')}")
        if task.get("status") not in {"todo", "doing"}:
            raise WorkflowError(f"Task {task_id} cannot be claimed from status {task.get('status')}")
        expires = now + dt.timedelta(seconds=lease_seconds)
        task["status"] = "doing"
        task["claim"] = {
            "agent": agent,
            "run_id": run_id,
            "lease_token": secrets.token_urlsafe(24),
            "claimed_at": now.replace(microsecond=0).isoformat(),
            "lease_expires_at": expires.replace(microsecond=0).isoformat(),
            "base_revision": int(task.get("revision") or 0),
        }
        task.setdefault("claim_history", []).append(_claim_audit(task["claim"]))
        _bump(task)
        return task.copy()

    return _mutate(Path(path), operation)


def submit_for_review(
    path: Path,
    task_id: str,
    agent: str,
    summary: str,
    files: Optional[List[str]] = None,
    tests: Optional[List[Any]] = None,
    commit: str = "",
    run_id: str = "",
    lease_token: str = "",
    base_sha: str = "",
    head_sha: str = "",
    patch_sha256: str = "",
) -> dict[str, Any]:
    if not summary.strip():
        raise WorkflowError("Implementation summary is required")

    def operation(tasks_data: list[dict[str, Any]]) -> dict[str, Any]:
        task = _task(tasks_data, task_id)
        claim = task.get("claim") or {}
        if task.get("status") != "doing":
            raise WorkflowError(f"Task {task_id} is not being implemented")
        if claim.get("agent") != agent:
            raise WorkflowError(f"Task {task_id} is claimed by {claim.get('agent')}, not {agent}")
        current_token = str(claim.get("lease_token") or "")
        if not current_token or not lease_token or not secrets.compare_digest(current_token, str(lease_token)):
            raise WorkflowError(f"Task {task_id} submission has an expired or superseded lease token")
        expiry = _parse_time(claim.get("lease_expires_at"))
        if not expiry or expiry <= dt.datetime.now(dt.timezone.utc):
            raise WorkflowError(f"Task {task_id} claim lease expired")
        if run_id and claim.get("run_id") and claim.get("run_id") != run_id:
            raise WorkflowError(f"Task {task_id} belongs to another run")
        submitted = {
            "agent": agent,
            "run_id": run_id or claim.get("run_id", ""),
            "summary": summary.strip(),
            "files": sorted(set(files or [])),
            "tests": tests or [],
            "commit": commit,
            "base_sha": base_sha,
            "head_sha": head_sha or commit,
            "patch_sha256": patch_sha256,
            "submitted_at": _now(),
            "base_task_revision": int(task.get("revision") or 0),
        }
        task["implementation"] = submitted
        task.setdefault("implementations", []).append(submitted.copy())
        task["status"] = "review"
        task["review"] = {"status": "pending", "submitted_at": submitted["submitted_at"]}
        _bump(task)
        return task.copy()

    return _mutate(Path(path), operation)


def release_claim(path: Path, task_id: str, agent: str, run_id: str = "", reason: str = "interrupted") -> dict[str, Any]:
    """Release only the matching implementation claim, used for crash recovery."""
    def operation(tasks: list[dict[str, Any]]) -> dict[str, Any]:
        task = _task(tasks, task_id)
        claim = task.get("claim") or {}
        if task.get("status") != "doing" or claim.get("agent") != agent:
            raise WorkflowError(f"Task {task_id} is not actively claimed by {agent}")
        if run_id and claim.get("run_id") != run_id:
            raise WorkflowError(f"Task {task_id} belongs to a different run")
        task.setdefault("claim_history", []).append(
            {**_claim_audit(claim), "released_at": _now(), "reason": reason}
        )
        task["claim"] = None
        task["status"] = "todo"
        _bump(task)
        return task.copy()

    return _mutate(Path(path), operation)


def review_task(
    path: Path,
    task_id: str,
    reviewer: str,
    decision: str,
    findings: str = "",
    *,
    reviewer_tier: str = "superior",
) -> dict[str, Any]:
    if decision not in {"accept", "reject"}:
        raise WorkflowError("Decision must be accept or reject")
    if reviewer_tier != "superior":
        raise WorkflowError("Review requires a superior reviewer")
    root = Path(path).resolve().parent.parent
    _authorize(root, "superior_reviewers", reviewer, require_policy=True)

    def operation(tasks: list[dict[str, Any]]) -> dict[str, Any]:
        task = _task(tasks, task_id)
        implementation = task.get("implementation") or {}
        if task.get("status") != "review":
            raise WorkflowError(f"Task {task_id} is not waiting for review")
        if not implementation:
            raise WorkflowError(f"Task {task_id} has no submitted implementation")
        if implementation.get("agent") == reviewer:
            raise WorkflowError("The implementing agent cannot approve its own work")
        reviewed = {
            "status": "accepted" if decision == "accept" else "rejected",
            "decision": decision,
            "reviewer": reviewer,
            "tier": reviewer_tier,
            "reviewer_tier": reviewer_tier,
            "findings": findings.strip(),
            "reviewed_at": _now(),
            "implementation_revision": implementation.get("base_task_revision"),
        }
        task["review"] = reviewed
        task.setdefault("reviews", []).append(reviewed.copy())
        task["claim"] = None
        task["claim_history"] = [
            _claim_audit(item) for item in task.get("claim_history") or [] if isinstance(item, dict)
        ]
        if decision == "accept":
            task["status"] = "done"
            task["accepted_implementation"] = implementation.copy()
            if task.get("subtasks"):
                task["done"] = list(range(1, len(task["subtasks"]) + 1))
        else:
            task["status"] = "todo"
        _bump(task)
        return task.copy()

    return _mutate(Path(path), operation)
