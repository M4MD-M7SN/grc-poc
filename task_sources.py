"""Local-only adapters for GRC task sources.

Adapters normalize files on the local machine into the existing GRC task
projection. No adapter performs network access or calls a hosted service.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Callable, Optional


STATUS_FILE = "task-source-status.json"
READ_ONLY_CAPABILITIES = ["read_tasks", "local_only"]


class TaskSourceError(RuntimeError):
    pass


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _status(raw: Any, labels: Optional[list[str]] = None) -> str:
    label_set = {label.strip().lower().replace("_", "-") for label in (labels or [])}
    if label_set & {"blocked", "status:blocked", "status/blocked"}:
        return "blocked"
    if label_set & {"review", "in-review", "status:review", "status/review"}:
        return "doing"
    if label_set & {"doing", "in-progress", "status:doing", "status/in-progress"}:
        return "doing"
    value = str(raw or "todo").strip().lower().replace("_", "-")
    if value in {"done", "complete", "completed", "closed", "merged", "cancelled"}:
        return "done"
    if value in {"active", "doing", "in-progress", "open", "review"}:
        return "doing" if value != "open" else "todo"
    if value in {"blocked", "todo", "backlog"}:
        return value
    return "todo"


def _write_status(root: Path, source: dict[str, Any], state: str, *, error: str = "", count: int = 0,
                  capabilities: Optional[list[str]] = None) -> None:
    path = root / ".grc" / STATUS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": source.get("format") or "local",
        "state": state,
        "read_only": "write_tasks" not in (capabilities or []),
        "capabilities": capabilities or [],
        "task_count": count,
        "synced_at": _now(),
        "error": error,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def _merge_local(external: list[dict[str, Any]], local: list[dict[str, Any]], source: dict[str, Any]) -> list[dict[str, Any]]:
    if not source.get("include_local"):
        return external
    external_ids = {str(task.get("id")) for task in external}
    return external + [task for task in local if str(task.get("id")) not in external_ids]


def _hermes_board(root: Path, source: dict[str, Any], local: list[dict[str, Any]], run: Callable[..., Any]) -> list[dict[str, Any]]:
    raw_path = str(source.get("path") or "").strip()
    if not raw_path:
        raise TaskSourceError("Hermes project board requires task_source.path")
    path = Path(raw_path).expanduser()
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise TaskSourceError(f"Task source not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise TaskSourceError(f"Task source is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise TaskSourceError("Hermes project board must contain a JSON object")
    terms = [str(term).strip().lower() for term in source.get("terms", []) if str(term).strip()]
    tasks = []
    for row in payload.get("projects", []):
        if not isinstance(row, dict):
            continue
        searchable = " ".join(str(row.get(key, "")) for key in ("name", "path", "next_action", "milestone")).lower()
        if terms and not any(term in searchable for term in terms):
            continue
        status = _status(row.get("status"))
        next_action = str(row.get("next_action") or "").strip()
        tasks.append({
            "id": str(row.get("id") or f"external-{len(tasks) + 1}"),
            "external_id": str(row.get("id") or ""),
            "title": str(row.get("name") or "Untitled project"),
            "status": status,
            "percent": 100 if status == "done" else row.get("percent", 0),
            "subtasks": ([{"text": next_action, "done": status == "done"}] if next_action else []),
            "milestone": row.get("milestone") or "",
            "blocker": row.get("blocker") or "",
            "path": row.get("path") or "",
            "updated_at": row.get("updated_at") or row.get("verified_at") or payload.get("updated_at") or "",
            "source": "Hermes project board",
            "source_format": "hermes-project-board",
            "provenance": {"path": str(path)},
        })
    return _merge_local(tasks, local, source)


MARKDOWN_TASK = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+(.+?)\s*$")


def _markdown_tasks(root: Path, source: dict[str, Any], local: list[dict[str, Any]], run: Callable[..., Any]) -> list[dict[str, Any]]:
    raw_path = str(source.get("path") or "").strip()
    if not raw_path:
        raise TaskSourceError("Markdown tasks require task_source.path")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = root / path
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError as exc:
        raise TaskSourceError(f"Local Markdown task source not found: {path}") from exc
    tasks = []
    for line_number, line in enumerate(lines, 1):
        match = MARKDOWN_TASK.match(line)
        if not match:
            continue
        done = match.group(1).lower() == "x"
        title = match.group(2).strip()
        explicit_id = re.match(r"([A-Za-z][A-Za-z0-9_-]*):\s+(.+)", title)
        task_id = explicit_id.group(1) if explicit_id else f"MD-{line_number}"
        title = explicit_id.group(2) if explicit_id else title
        tasks.append({
            "id": task_id,
            "external_id": f"{path}:{line_number}",
            "title": title,
            "status": "done" if done else "todo",
            "percent": 100 if done else 0,
            "subtasks": [],
            "source": "Local Markdown",
            "source_format": "markdown-tasks",
            "provenance": {"path": str(path), "line": line_number},
        })
    return _merge_local(tasks, local, source)


ADAPTERS: dict[str, tuple[Callable[..., list[dict[str, Any]]], list[str]]] = {
    "hermes-project-board": (_hermes_board, ["read_tasks"]),
    "markdown-tasks": (_markdown_tasks, READ_ONLY_CAPABILITIES),
}


def resolve(root: Path, source: dict[str, Any], local: list[dict[str, Any]], *, run: Callable[..., Any] = lambda *args, **kwargs: None) -> list[dict[str, Any]]:
    """Load tasks from the configured source and write its status file.

    If the adapter is unknown or fails, local tasks still show and the error
    goes to task-source-status.json.
    """
    format_name = str(source.get("format") or "").strip()
    if not format_name:
        return local
    entry = ADAPTERS.get(format_name)
    if not entry:
        _write_status(root, source, "error", error=f"Unsupported task source: {format_name}")
        return local
    adapter, capabilities = entry
    try:
        tasks = adapter(root, source, local, run)
    except (TaskSourceError, ValueError, TypeError) as exc:
        _write_status(root, source, "error", error=str(exc), capabilities=capabilities)
        return local
    _write_status(root, source, "ok", count=len(tasks), capabilities=capabilities)
    return tasks
