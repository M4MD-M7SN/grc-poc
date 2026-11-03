#!/usr/bin/env python3
"""MCP server (stdio) exposing GRC to any agent.

Any MCP client (Claude Code, Codex, Cursor, Hermes...) can launch it and use
the same tools: read context, update tasks and the resume card, log work, and
write a handoff before stopping.

Standard library only. Speaks newline-delimited JSON-RPC 2.0 (MCP stdio).
"""
import datetime as dt
import json
import os
import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import genie  # noqa: E402
import grc  # noqa: E402
import workflow  # noqa: E402
import agent_runner  # noqa: E402

VERSION = "0.9"
CLIENT = {"name": "agent"}
SERVER_ROLE = os.environ.get("GRC_MCP_ROLE", "agent")
CONNECTION_REGISTRY = Path.home() / "Library" / "Application Support" / "GRC" / "connections.json"


def shared_connections(path=CONNECTION_REGISTRY):
    """Read the agent-neutral catalog without returning secrets or private agent config."""
    try:
        records = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    safe = []
    for item in records if isinstance(records, list) else []:
        if not isinstance(item, dict) or item.get("agentID"):
            continue
        safe.append({
            "id": item.get("id", ""),
            "name": item.get("name", ""),
            "target": item.get("target", ""),
            "detail": item.get("detail", ""),
            "kind": item.get("kind", ""),
            "state": item.get("state", ""),
            "tags": item.get("tags", []),
            "credential_stored": bool(item.get("secretName")),
        })
    return safe

INSTRUCTIONS = """GRC (General Repo Context) keeps each project's shared state in a .grc/ folder so
any AI agent can pick up where another stopped.
- Call grc_connections to discover the same shared Tools, MCP servers, and APIs available to every agent.
- At the start of project work, call grc_context (pass focus=<task id> when you know the task).
- A server launched with GRC_MCP_ROLE=project-lead may use grc_plan_task; ordinary clients cannot self-assert that role.
- Project leads inspect grc_task_sheet and grc_next_task, then call grc_run_local for the chosen id; the claim boundary rechecks dependencies atomically.
- While working, keep tasks honest with grc_update_task; add new work with grc_add_task.
- Before you stop — and as soon as you suspect you're near a usage limit — call grc_handoff.
project_dir defaults to the current folder (or the nearest parent with a .grc/ folder)."""

_dir = {"type": "string", "description": "Project folder. Default: current folder or nearest parent with .grc/"}
_agent = {"type": "string", "description": "Who you are, as tool/model, e.g. claude-code/opus or codex/gpt-5. Default: the MCP client name."}
_task_spec = {
    "type": "object",
    "description": "One bounded work object designed by the project lead.",
    "properties": {
        "title": {"type": "string"},
        "position": {"type": "integer", "minimum": 1},
        "parent_goal": {"type": "string"},
        "purpose": {"type": "string"},
        "inputs": {"type": "array", "items": {"type": "string"}},
        "outputs": {"type": "array", "items": {"type": "string"}},
        "owned_paths": {"type": "array", "items": {"type": "string"}},
        "forbidden_paths": {"type": "array", "items": {"type": "string"}},
        "dependencies": {"type": "array", "items": {"type": "string"}},
        "interface_contract": {"type": "string"},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "verification_commands": {"type": "array", "items": {
            "type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}},
        "source_strategy": {"type": "string", "enum": ["build", "reuse", "research"]},
        "time_limit_seconds": {"type": "integer", "minimum": 1},
        "integration_order": {"type": "integer", "minimum": 1},
        "notes": {"type": "string"},
    },
    "required": ["title", "acceptance_criteria"],
    "additionalProperties": False,
}

TOOLS = [
    {"name": "grc_connections",
     "description": "List the shared agent-neutral Tools, MCP servers, and APIs available to every AI agent. Returns safe metadata only; credentials are never exposed.",
     "inputSchema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["Tools", "MCP", "API"], "description": "Optional resource type filter"}}}},
    {"name": "grc_context",
     "description": "Get a compact project briefing or versioned JSON context capsule. The capsule is portable, integrity-sealed, size-budgeted and built from GRC projections without rescanning the repository. Call this first.",
     "inputSchema": {"type": "object", "properties": {
         "project_dir": _dir, "focus": {"type": "string", "description": "Optional task id (T5), file path or topic to zoom in on"},
         "format": {"type": "string", "enum": ["markdown", "capsule"], "description": "Default markdown; capsule returns provider-neutral JSON"},
         "max_bytes": {"type": "integer", "minimum": 2000, "maximum": 50000, "description": "Hard capsule size budget"},
         "persist": {"type": "boolean", "description": "Write .grc/context-capsule.json for another environment"}}}},

    {"name": "grc_list_tasks", "description": "List the project's tasks with status, % done and steps.",
     "inputSchema": {"type": "object", "properties": {"project_dir": _dir}}},
    {"name": "grc_plan_task", "description": "Project lead API: add one structured, dependency-aware work object to the local task sheet.",
     "inputSchema": {"type": "object", "properties": {
         "spec": _task_spec, "project_dir": _dir}, "required": ["spec"]}},
    {"name": "grc_task_sheet", "description": "Return the ordered local task sheet with computed readiness and dependency blockers.",
     "inputSchema": {"type": "object", "properties": {"project_dir": _dir}}},
    {"name": "grc_next_task", "description": "Select the next ready local task without claiming or running it.",
     "inputSchema": {"type": "object", "properties": {"project_dir": _dir}}},
    {"name": "grc_add_task", "description": "Add a task to the project's to-do list.",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string"}, "subtasks": {"type": "array", "items": {"type": "string"}},
         "notes": {"type": "string"}, "project_dir": _dir, "agent": _agent}, "required": ["title"]}},
    {"name": "grc_update_task", "description": "Update a task: status (todo/doing/done/blocked), tick steps by number (1-based), or add a note.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "status": {"type": "string", "enum": ["todo", "doing", "done", "blocked"]},
         "done_subtasks": {"type": "array", "items": {"type": "integer"}}, "note": {"type": "string"},
         "project_dir": _dir, "agent": _agent}, "required": ["id"]}},
    {"name": "grc_claim_task", "description": "Atomically claim one task for an implementing agent using a lease and optional revision check.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "expected_revision": {"type": "integer", "minimum": 0},
         "run_id": {"type": "string"}, "lease_seconds": {"type": "integer", "minimum": 60, "maximum": 86400},
         "project_dir": _dir, "agent": _agent}, "required": ["id"]}},

    {"name": "grc_run_audit", "description": "Run the approved superior-audit command configured in project workflow.runners.audit.",
     "inputSchema": {"type": "object", "properties": {"project_dir": _dir}}},
    {"name": "grc_run_local", "description": "Run the approved local implementation agent in an isolated Git worktree for one task.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "project_dir": _dir}, "required": ["id"]}},

    {"name": "grc_run_review", "description": "Run the approved independent superior-review command and integrate only accepted work.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "project_dir": _dir}, "required": ["id"]}},
    {"name": "grc_recover_runs", "description": "Release claims held by abandoned local-agent runs after the configured age.",
     "inputSchema": {"type": "object", "properties": {"age_seconds": {"type": "integer", "minimum": 60}, "project_dir": _dir}}},
    {"name": "grc_set_resume", "description": "Update the Resume-here card: where things stand now, the next step, and what's blocking.",
     "inputSchema": {"type": "object", "properties": {
         "now": {"type": "string"}, "next": {"type": "string"}, "blocked": {"type": "string"},
         "project_dir": _dir, "agent": _agent}}},
    {"name": "grc_log", "description": "Record what you just did on the project's timeline.",
     "inputSchema": {"type": "object", "properties": {
         "message": {"type": "string"}, "task": {"type": "string"}, "project_dir": _dir, "agent": _agent},
         "required": ["message"]}},
    {"name": "grc_handoff",
     "description": "Leave a handoff so another agent (or the offline model) can continue: what's done, what remains, the exact next step. Call before stopping or when near a usage limit.",
     "inputSchema": {"type": "object", "properties": {
         "done": {"type": "string", "description": "What you finished"},
         "remaining": {"type": "string", "description": "What is left, as concrete steps"},
         "next": {"type": "string", "description": "The single next step to take"},
         "blocked": {"type": "string"}, "task": {"type": "string", "description": "Task id this is about"},
         "files": {"type": "array", "items": {"type": "string"}, "description": "Files that matter for the next step"},
         "project_dir": _dir, "agent": _agent}, "required": ["done", "remaining", "next"]}},
    {"name": "grc_init", "description": "Start tracking a folder with GRC (creates .grc/ and its dashboard).",
     "inputSchema": {"type": "object", "properties": {"project_dir": {"type": "string"}}, "required": ["project_dir"]}},
]


class ToolError(Exception):
    pass


def project(args):
    raw = args.get("project_dir") or os.environ.get("GRC_PROJECT") or os.getcwd()
    p = Path(raw).expanduser().resolve()
    for d in [p, *p.parents]:
        if (d / ".grc").is_dir():
            return d
    raise ToolError(f"No GRC project at {p} or above it. Pass project_dir, or call grc_init with the project folder.")


def who(args):
    if args.get("agent"):
        return args["agent"]
    name = CLIENT.get("name", "agent").lower()
    for key, label in (("claude", "claude-code"), ("codex", "codex"), ("hermes", "hermes"), ("cursor", "cursor")):
        if key in name:
            return label
    return name


def configured_project_lead(root: Path) -> str:
    """Return a lead identity from trusted server launch state plus project policy."""
    if SERVER_ROLE != "project-lead":
        raise ToolError("grc_plan_task requires GRC_MCP_ROLE=project-lead on the MCP server")
    project_data = genie.read_json(root / ".grc" / "project.json", {})
    allowed = (((project_data.get("workflow") or {}).get("roles") or {}).get("project_leads")) or []
    if not isinstance(allowed, list) or not allowed or not all(isinstance(actor, str) and actor for actor in allowed):
        raise ToolError("Project authorization policy has no project_leads")
    return allowed[0]


def ensure_task_write_allowed(root, action, task_id=""):
    """Block writes that an external (read-only) task source would ignore.

    With include_local set, local tasks can still be added and edited.
    """
    project_data = genie.read_json(root / ".grc" / "project.json", {})
    source = project_data.get("task_source") or {}
    if not source.get("format"):
        return
    include_local = bool(source.get("include_local"))
    if action == "add" and include_local:
        return
    if action == "update" and include_local:
        local = genie.read_json(root / ".grc" / "tasks.json", [])
        if any(str(task.get("id")) == str(task_id) for task in local):
            return
    raise ToolError(
        f"Task source '{source.get('format')}' is read-only in GRC; refusing a local {action} "
        "that would create split-brain task state. Configure include_local for local-only tasks "
        "or install a write-capable provider adapter."
    )


def approved_runner_profile(root, name):
    project_data = genie.read_json(root / ".grc" / "project.json", {})
    profile = ((((project_data.get("workflow") or {}).get("runners") or {}).get(name)) or {})
    command = profile.get("command")
    if isinstance(command, str):
        command = shlex.split(command)
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        raise ToolError(f"Configure workflow.runners.{name}.command as an argv array in .grc/project.json")
    tests = []
    for item in profile.get("tests") or []:
        tests.append(shlex.split(item) if isinstance(item, str) else item)
    return profile, command, tests


def call(name, args):
    if name == "grc_connections":
        records = shared_connections()
        if args.get("kind"):
            records = [item for item in records if item.get("kind") == args["kind"]]
        return json.dumps(records, indent=2, ensure_ascii=False)
    if name == "grc_init":
        root = Path(args["project_dir"]).expanduser().resolve()
        if not root.is_dir():
            raise ToolError(f"Not a folder: {root}")
        grc.init(root)
        return f"GRC is now tracking {root}. Dashboard: {root / '.grc/dashboard.html'}"

    root = project(args)
    agent = who(args)
    bench = genie.Workbench(root, agent)

    if name in {"grc_audit_upsert_task", "grc_submit_review", "grc_review_task"}:
        replacement = {"grc_audit_upsert_task": "grc_run_audit", "grc_submit_review": "grc_run_local", "grc_review_task": "grc_run_review"}[name]
        raise ToolError(
            f"{name} is disabled for untrusted MCP callers because caller-supplied identities are not authenticated. "
            f"Use {replacement}, which uses the project's approved runner profile and immutable Git evidence."
        )

    if name in {"grc_run_audit", "grc_run_local", "grc_run_review", "grc_recover_runs"}:
        runner = agent_runner.AgentRunner(root)
        try:
            if name == "grc_run_audit":
                profile, command, _ = approved_runner_profile(root, "audit")
                result = runner.run_audit(profile.get("agent", "superior/auditor"), command)
            elif name == "grc_run_local":
                profile, command, tests = approved_runner_profile(root, "local")
                result = runner.run_local(args["id"], profile.get("agent", "local/agent"), command, tests)
            elif name == "grc_run_review":
                profile, command, _ = approved_runner_profile(root, "review")
                result = runner.run_review(args["id"], profile.get("agent", "superior/reviewer"), command)
            else:
                result = runner.recover_stale_runs(args.get("age_seconds", 3600))
        except (agent_runner.RunnerError, workflow.WorkflowError) as exc:
            raise ToolError(str(exc)) from exc
        return json.dumps(result, indent=2, ensure_ascii=False)

    if name == "grc_context":
        if not (root / ".grc" / "scan.json").exists():
            grc.build(root, rescan=False)
        if args.get("format") == "capsule":
            capsule = genie.context_capsule(
                root, args.get("focus"), args.get("max_bytes", 12000), bool(args.get("persist"))
            )
            return json.dumps(capsule, ensure_ascii=False, separators=(",", ":"))
        return genie.context_pack(root, args.get("focus"))
    if name == "grc_list_tasks":
        project_data = genie.read_json(root / ".grc" / "project.json", {})
        tasks = grc.resolve_tasks(root, project_data)
        lines = []
        for t in tasks:
            lines.append(f"{t.get('id')} [{t.get('status', 'todo')} {genie._percent(t)}%] {t.get('title', '')}" +
                         (f" (last: {t['last_agent']})" if t.get("last_agent") else ""))
            for i, s in enumerate(t.get("subtasks", []), 1):
                lines.append(f"   {i}. [{'x' if s.get('done') else ' '}] {s.get('text', '')}")
        return "\n".join(lines) or "No tasks yet."
    if name == "grc_plan_task":
        ensure_task_write_allowed(root, "add")
        try:
            lead = configured_project_lead(root)
            task = workflow.plan_task(root, lead, args.get("spec") or {})
        except workflow.WorkflowError as exc:
            raise ToolError(str(exc)) from exc
        message = f"Planned {task['id']}: {task['title']}"
        grc.append_event(root / ".grc", lead, message, kind="plan", task=task["id"])
        grc.build(root, rescan=False)
        return json.dumps(task, indent=2, ensure_ascii=False)
    if name == "grc_task_sheet":
        return json.dumps(workflow.task_sheet(root / ".grc" / "tasks.json"), indent=2, ensure_ascii=False)
    if name == "grc_next_task":
        return json.dumps(workflow.next_ready_task(root / ".grc" / "tasks.json"), indent=2, ensure_ascii=False)
    if name == "grc_audit_upsert_task":
        ensure_task_write_allowed(root, "add")
        try:
            task, created = workflow.audit_upsert_task(
                root, args.get("auditor") or agent, args.get("title", ""), args.get("evidence") or [],
                args.get("acceptance_criteria") or [], severity=args.get("severity", "medium"),
                base_revision=args.get("base_revision", ""), notes=args.get("notes", ""),
            )
        except workflow.WorkflowError as exc:
            raise ToolError(str(exc)) from exc
        action = "Created" if created else "Updated duplicate"
        message = f"{action} audited finding {task['id']}: {task['title']}"
        grc.append_event(root / ".grc", args.get("auditor") or agent, message, kind="audit", task=task["id"])
        grc.build(root, rescan=False)
        return message
    if name == "grc_add_task":
        ensure_task_write_allowed(root, "add")
        out = bench.t_add_task(args.get("title", ""), args.get("subtasks"), args.get("notes", ""))
    elif name == "grc_update_task":
        ensure_task_write_allowed(root, "update", args.get("id", ""))
        out = bench.t_update_task(args.get("id", ""), args.get("status"), args.get("done_subtasks"), args.get("note"))
    elif name in {"grc_claim_task", "grc_submit_review", "grc_review_task"}:
        ensure_task_write_allowed(root, "update", args.get("id", ""))
        task_path = root / ".grc" / "tasks.json"
        try:
            if name == "grc_claim_task":
                task = workflow.claim_task(
                    task_path, args["id"], agent, expected_revision=args.get("expected_revision"),
                    run_id=args.get("run_id", ""), lease_seconds=args.get("lease_seconds", 3600),
                )
                message = json.dumps({"task": task["id"], "agent": agent, "revision": task.get("revision"),
                                      "lease_token": task["claim"]["lease_token"],
                                      "lease_expires_at": task["claim"]["lease_expires_at"]})
                event_kind = "claim"
            elif name == "grc_submit_review":
                task = workflow.submit_for_review(
                    task_path, args["id"], agent, args["summary"],
                    args.get("files"), args.get("tests"), args.get("commit", ""), args.get("run_id", ""),
                    args.get("lease_token", ""),
                )
                message = f"Submitted {task['id']} for independent review."
                event_kind = "review-request"
            else:
                task = workflow.review_task(
                    task_path, args["id"], agent, args["decision"], args.get("findings", "")
                )
                message = f"Review {task['review']['status']} for {task['id']} by {agent}."
                event_kind = "review"
        except workflow.WorkflowError as exc:
            raise ToolError(str(exc)) from exc
        grc.append_event(root / ".grc", agent, message, kind=event_kind, task=task["id"])
        grc.build(root, rescan=False)
        return message
    elif name == "grc_set_resume":
        out = bench.t_set_resume(args.get("now"), args.get("next"), args.get("blocked"))
    elif name == "grc_log":
        grc.append_event(root / ".grc", agent, args["message"], kind="note", task=args.get("task"))
        grc.build(root, rescan=False)
        return "Logged."
    elif name == "grc_handoff":
        write_handoff(root, agent, args)
        return "Handoff saved. The next agent will see it in grc_context and on the dashboard."
    else:
        raise ToolError(f"Unknown tool {name}")
    if bench.changed:
        grc.append_event(root / ".grc", agent, "; ".join(bench.changed), kind="mcp")
        grc.build(root, rescan=False)
    return out


def write_handoff(root, agent, a):
    stamp = dt.datetime.now().astimezone()
    files = "\n".join(f"- `{f}`" for f in a.get("files") or [])
    text = f"""# Handoff from {agent}
_{stamp.strftime('%d %b %Y %H:%M')} · written by the agent itself_

## Done
{a.get('done', '').strip()}

## Remaining
{a.get('remaining', '').strip()}

## Next step
{a.get('next', '').strip()}
"""
    if a.get("blocked"):
        text += f"\n## Blocked on\n{a['blocked'].strip()}\n"
    if files:
        text += f"\n## Files that matter\n{files}\n"
    if a.get("task"):
        text += f"\nTask: {a['task']}\n"
    (root / ".grc" / "handoff.md").write_text(text)
    bench = genie.Workbench(root, agent)
    bench.t_set_resume(now=a.get("done", "").strip()[:400], next=a.get("next", "").strip()[:400], blocked=a.get("blocked", ""))
    grc.append_event(root / ".grc", agent, "Handed off: " + a.get("next", "").strip()[:200], kind="handoff", task=a.get("task"))
    grc.build(root, rescan=False)


def reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(msg):
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        CLIENT.update(params.get("clientInfo") or {})
        reply(msg_id, {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "grc", "version": VERSION},
            "instructions": INSTRUCTIONS,
        })
    elif method == "tools/list":
        reply(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        try:
            text = call(name, args)
            reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except ToolError as e:
            reply(msg_id, {"content": [{"type": "text", "text": str(e)}], "isError": True})
        except Exception as e:  # never kill the server over one bad call
            reply(msg_id, {"content": [{"type": "text", "text": f"GRC error: {type(e).__name__}: {e}"}], "isError": True})
    elif method == "ping":
        reply(msg_id, {})
    elif msg_id is not None and not str(method).startswith("notifications/"):
        reply(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})


def main():
    # stdout is the protocol channel; keep everything else (prints from builds) off it
    real_stdout = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        sys.stdout = real_stdout
        try:
            handle(msg)
        finally:
            sys.stdout = real_stdout


if __name__ == "__main__":
    main()
