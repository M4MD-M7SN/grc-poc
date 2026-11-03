"""Genie, the local GRC assistant.

Runs on Ollama, on this Mac. It answers questions about a project from its
GRC context, can search and read the project's files, and can change GRC's own
records (tasks, the resume card, the log). It never edits project code.

  context_pack(root, focus)  short, task-specific context for ANY agent
  ask(root, question)        one Genie turn; streams JSON lines on stdout
  brief(root)                background job: rewrite .grc/brief.md
"""
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

import workflow

OLLAMA = "http://127.0.0.1:11434"
PREFERRED = ["qwen3:8b", "hermes-local", "qwen3", "llama3.1:8b", "hermes-local-chat"]
NUM_CTX = 16384
MAX_STEPS = 8


def now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def emit(kind, **data):
    """One event for the app (or a terminal) to show."""
    sys.stdout.write(json.dumps({"t": kind, **data}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------- Ollama

def pick_model():
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=3) as r:
            # Offline only: skip Ollama cloud models, which run on Ollama's servers.
            names = [m["name"] for m in json.load(r).get("models", [])
                     if not m.get("remote_host") and "cloud" not in m["name"].lower()]
    except OSError:
        return None
    short = {n.replace(":latest", ""): n for n in names}
    for want in PREFERRED:
        if want in short:
            return short[want]
    return names[0] if names else None


def chat(model, messages, tools=None, stream=True, fmt=None):
    """Yield (content_piece, tool_calls) from Ollama's /api/chat."""
    body = {"model": model, "messages": messages, "stream": stream, "think": False,
            "options": {"num_ctx": NUM_CTX, "temperature": 0.3}}
    if tools:
        body["tools"] = tools
    if fmt:
        body["format"] = fmt
    req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        if not stream:
            msg = json.load(r).get("message", {})
            yield msg.get("content", ""), msg.get("tool_calls") or []
            return
        for line in r:
            if not line.strip():
                continue
            chunk = json.loads(line)
            if chunk.get("error"):
                raise RuntimeError(chunk["error"])
            msg = chunk.get("message", {})
            yield msg.get("content", ""), msg.get("tool_calls") or []


# ---------------------------------------------------------------- context

def _ago(ts):
    try:
        d = dt.datetime.now().astimezone() - dt.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return ""
    s = d.total_seconds()
    if s < 3600:
        return f"{int(s // 60)}m ago"
    if s < 86400:
        return f"{int(s // 3600)}h ago"
    return f"{int(s // 86400)}d ago"


def _percent(task):
    subs = task.get("subtasks") or []
    if subs:
        return round(100 * sum(1 for s in subs if s.get("done")) / len(subs))
    return 100 if task.get("status") == "done" else int(task.get("percent", 0))


def neighbourhood(graph, focus, limit=25):
    """What the map links to `focus` (a task id, file path or topic word)."""
    if not graph or not focus:
        return None
    f = focus.lower()
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    hit = next((i for i in (f"task:{focus.upper()}", f"file:{focus}", f"concept:{f}") if i in nodes), None)
    if not hit:
        hit = next((n["id"] for n in nodes.values()
                    if f == n.get("label", "").lower() or f in (n.get("path") or "").lower()), None)
    if not hit:
        return None
    rel_names = {"contains": ("contains", "inside"), "imports": ("uses code from", "used by"),
                 "links_to": ("links to", "linked from"), "mentions": ("about", "appears in"),
                 "about": ("about", "part of"), "worked_on": ("worked on", "worked on by"),
                 "changed": ("changed", "changed by")}
    groups = {}
    for e in graph.get("edges", []):
        if e["s"] == hit:
            other, name = e["t"], rel_names.get(e["rel"], (e["rel"],) * 2)[0]
        elif e["t"] == hit:
            other, name = e["s"], rel_names.get(e["rel"], (e["rel"],) * 2)[1]
        else:
            continue
        if other in nodes:
            groups.setdefault(name, []).append(nodes[other].get("path") or nodes[other]["label"])
    lines = [f"Map around {nodes[hit]['label']}:"]
    for name, items in groups.items():
        lines.append(f"- {name}: " + ", ".join(items[:limit]) + (f" (+{len(items) - limit} more)" if len(items) > limit else ""))
    return "\n".join(lines)


def context_pack(root, focus=None, budget=9000, with_brief=True):
    """A short briefing any agent can start from instead of re-reading everything."""
    root = Path(root)
    grc = root / ".grc"
    project = read_json(grc / "project.json", {})
    state = read_json(grc / "state.json", {})
    # same resolver as the dashboard, otherwise projects with an external
    # task board would show different tasks here
    try:
        import grc as grc_module
        tasks = grc_module.resolve_tasks(root, project)
    except (ImportError, AttributeError):
        tasks = read_json(grc / "resolved-tasks.json", read_json(grc / "tasks.json", []))
    health = read_json(grc / "scan.json", {})
    graph = read_json(grc / "graph.json", {})
    out = [f"# {project.get('name') or root.name}", f"Folder: {root}"]
    if project.get("goal"):
        out.append(f"Goal: {project['goal']}")
    flow = project.get("flow") or []
    if flow:
        out.append("\n## How it works")
        for index, raw_stage in enumerate(flow, 1):
            stage = raw_stage if isinstance(raw_stage, dict) else {"title": str(raw_stage)}
            title = str(stage.get("title") or stage.get("name") or f"Step {index}").strip()
            desc = str(stage.get("desc") or "").strip()
            files = [str(path).strip() for path in stage.get("files") or [] if str(path).strip()]
            line = f"{index}. {title}"
            if desc:
                line += f" — {desc}"
            if files:
                line += " (" + ", ".join(f"`{path}`" for path in files) + ")"
            out.append(line)
    out.append("\n## Resume here")
    out.append(f"- Now: {state.get('now', '?')}\n- Next: {state.get('next', '?')}\n- Blocked: {state.get('blocked') or 'nothing'}")
    if state.get("last_agent"):
        out.append(f"- Last worked on by {state['last_agent']} {_ago(state.get('updated'))}")

    if focus:
        nb = neighbourhood(graph, focus)
        if nb:
            out.append("\n## Focus\n" + nb)

    if tasks:
        out.append("\n## Tasks")
        for t in tasks:
            line = f"- {t.get('id')} [{t.get('status', 'todo')} {_percent(t)}%] {t.get('title', '')}"
            if t.get("last_agent"):
                line += f" (last: {t['last_agent']})"
            out.append(line)
            if t.get("status") in ("doing", "blocked") or (focus and str(focus).upper() == str(t.get("id")).upper()):
                for i, s in enumerate(t.get("subtasks", []), 1):
                    out.append(f"    {i}. [{'x' if s.get('done') else ' '}] {s.get('text', '')}")

    events = []
    p = grc / "events.jsonl"
    if p.exists():
        for line in p.read_text().splitlines()[-12:]:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    if events:
        out.append("\n## Recent activity")
        for e in reversed(events):
            out.append(f"- {_ago(e.get('ts'))} {e.get('agent', '?')}: {e.get('text', '')}" + (f" ({e['task']})" if e.get("task") else ""))

    if health:
        g = health.get("git", {})
        langs = ", ".join(f"{k} {v}" for k, v in list(health.get("languages", {}).items())[:4])
        out.append("\n## Health")
        out.append(f"- {health.get('files', 0)} files, {health.get('lines', 0)} lines ({langs})")
        out.append(f"- {len(health.get('todos', []))} TODO/FIXME notes in code")
        if g.get("repo"):
            lc = g.get("last_commit") or {}
            out.append(f"- git: branch {g.get('branch')}, {g.get('changed', 0)} changed, {g.get('untracked', 0)} untracked, last commit {_ago(lc.get('ts')) or 'none'}")
        bad = [e["path"] for e in health.get("env_files", []) if not e.get("ignored")]
        if bad:
            out.append(f"- WARNING: secrets file not git-ignored: {', '.join(bad)}")

    if graph.get("clusters"):
        out.append("\n## Areas of the project (files that belong together)")
        for c in graph["clusters"][:8]:
            out.append(f"- {c['name']} ({c['size']} files): " + ", ".join(c["files"][:5]))

    if graph.get("nodes"):
        deg = {}
        for e in graph.get("edges", []):
            if e["rel"] in ("imports", "links_to", "implemented_by"):
                deg[e["s"]] = deg.get(e["s"], 0) + 1
                deg[e["t"]] = deg.get(e["t"], 0) + 1
        names = {n["id"]: n for n in graph["nodes"]}
        file_types = {"code", "doc", "data", "other"}
        hubs = [node_id for node_id in sorted(deg, key=lambda node_id: deg[node_id], reverse=True)
                if node_id in names and names[node_id].get("type") in file_types][:10]
        if hubs:
            out.append("\n## Key files (most connected)")
            out.append(", ".join(names[h].get("path") or names[h]["label"] for h in hubs))
        concepts = [n["label"] for n in graph["nodes"] if n["type"] == "concept"]
        if concepts:
            out.append("Topics: " + ", ".join(concepts[:20]))

    brief = grc / "brief.md"
    if with_brief and brief.exists():
        out.append("\n## Project brief (auto-written, may be out of date)\n" + brief.read_text()[:2500])

    text = "\n".join(out)
    return text if len(text) <= budget else text[:budget] + "\n…(trimmed)"


def context_capsule(root, focus=None, max_bytes=12000, persist=False):
    """Build a size-capped context capsule from the existing .grc/ files.

    No repo scan happens here. The fingerprint lets a caller tell if it's stale.
    """
    root = Path(root).resolve()
    grc = root / ".grc"
    max_bytes = max(2000, min(int(max_bytes), 50000))
    source_names = (
        "project.json", "state.json", "tasks.json", "resolved-tasks.json",
        "scan.json", "graph.json", "brief.md", "handoff.md",
    )
    source_hashes = {}
    for name in source_names:
        path = grc / name
        if path.is_file():
            source_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    project = read_json(grc / "project.json", {})
    try:
        import grc as grc_module
        tasks = grc_module.resolve_tasks(root, project)
    except (ImportError, AttributeError):
        tasks = read_json(grc / "resolved-tasks.json", read_json(grc / "tasks.json", []))
    source_hashes["resolved-task-content"] = hashlib.sha256(
        json.dumps(tasks, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    try:
        git_head = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"], cwd=root, text=True,
            capture_output=True, timeout=3,
        ).stdout.strip()
        git_status_lines = subprocess.run(
            ["git", "status", "--porcelain=v1", "-uall"], cwd=root, text=True,
            capture_output=True, timeout=5,
        ).stdout.splitlines()
        git_status_lines = [line for line in git_status_lines if not line.endswith(".grc/context-capsule.json")]
        git_status = "\n".join(git_status_lines)
    except (OSError, subprocess.TimeoutExpired):
        git_head = ""
        git_status = ""
    if git_head:
        source_hashes["git-head"] = hashlib.sha256(git_head.encode()).hexdigest()
    if git_status:
        source_hashes["git-worktree-status"] = hashlib.sha256(git_status.encode()).hexdigest()
    source_fingerprint = hashlib.sha256(
        json.dumps(source_hashes, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    saved = read_json(grc / "context-capsule.json", {})
    if (persist and saved.get("source_fingerprint") == source_fingerprint
            and saved.get("focus") == (focus or "")
            and saved.get("max_bytes") == max_bytes):
        return saved

    previous_hashes = saved.get("source_hashes", {}) if isinstance(saved, dict) else {}
    changed_sources = sorted(
        name for name in set(previous_hashes) | set(source_hashes)
        if previous_hashes.get(name) != source_hashes.get(name)
    )

    state = read_json(grc / "state.json", {})
    counts = {}
    for task in tasks:
        status = str(task.get("status") or "todo")
        counts[status] = counts.get(status, 0) + 1
    review_queue = [
        {"id": task.get("id"), "title": task.get("title", ""),
         "implementer": (task.get("implementation") or {}).get("agent", "")}
        for task in tasks if task.get("status") == "review"
    ]
    health = read_json(grc / "scan.json", {})
    git = health.get("git") or {}
    last_commit = git.get("last_commit") or {}
    scan_head = str(last_commit.get("sha") or last_commit.get("hash") or last_commit.get("id") or "")
    briefing = context_pack(root, focus=focus, budget=max_bytes, with_brief=True)
    briefing = briefing.replace(f"Folder: {root}", "Folder: .", 1)
    capsule = {
        "schema": "grc.context-capsule/v1",
        "generated_at": now(),
        "project": {"name": project.get("name") or root.name, "goal": project.get("goal", "")},
        "focus": focus or "",
        "max_bytes": max_bytes,
        "source_fingerprint": source_fingerprint,
        "source_hashes": source_hashes,
        "freshness": {
            "git_head": git_head,
            "scan_git_head": scan_head,
            "repository_scan_current": bool(git_head and scan_head and git_head.startswith(scan_head) and not git_status),
            "worktree_dirty": bool(git_status),
            "changed_sources": changed_sources,
            "previous_fingerprint": saved.get("source_fingerprint", "") if isinstance(saved, dict) else "",
        },
        "git": {"branch": git.get("branch", ""),
                "head": scan_head,
                "changed": git.get("changed", 0), "untracked": git.get("untracked", 0)},
        "resume": {key: state.get(key, "") for key in ("now", "next", "blocked", "last_agent", "updated")},
        "task_counts": counts,
        "review_queue": review_queue,
        "briefing": briefing,
    }
    def seal(value):
        unsigned = {key: item for key, item in value.items() if key != "integrity"}
        value["integrity"] = "sha256:" + hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()

    seal(capsule)
    while len(json.dumps(capsule, ensure_ascii=False, separators=(",", ":")).encode()) > max_bytes:
        current = capsule["briefing"]
        if not current:
            raise ValueError(f"Context capsule metadata exceeds {max_bytes} bytes")
        capsule["briefing"] = current[:-min(128, len(current))]
        seal(capsule)
    if persist:
        write_json(grc / "context-capsule.json", capsule)
    return capsule


# ---------------------------------------------------------------- tools

TOOLS = [
    {"type": "function", "function": {
        "name": "search_files", "description": "Search the project's text files for a word or phrase (case-insensitive). Returns path:line: text.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read part of a file in the project.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "path relative to the project folder"},
            "start": {"type": "integer", "description": "first line, default 1"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "list_files", "description": "List files in a folder of the project.",
        "parameters": {"type": "object", "properties": {"folder": {"type": "string", "description": "default: project root"}}}}},
    {"type": "function", "function": {
        "name": "focus", "description": "Show what the project map connects to a task id (e.g. T5), a file path, or a topic word.",
        "parameters": {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}}},
    {"type": "function", "function": {
        "name": "add_task", "description": "Add a new task to the project's to-do list.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"}, "subtasks": {"type": "array", "items": {"type": "string"}},
            "notes": {"type": "string"}}, "required": ["title"]}}},
    {"type": "function", "function": {
        "name": "update_task", "description": "Change a task: its status, tick subtasks (1-based numbers), or add a note.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"}, "status": {"type": "string", "enum": ["todo", "doing", "done", "blocked"]},
            "done_subtasks": {"type": "array", "items": {"type": "integer"}}, "note": {"type": "string"}},
            "required": ["id"]}}},
    {"type": "function", "function": {
        "name": "set_resume", "description": "Update the Resume-here card: where we are now, what to do next, what's blocking.",
        "parameters": {"type": "object", "properties": {
            "now": {"type": "string"}, "next": {"type": "string"}, "blocked": {"type": "string"}}}}},
]

TEXT_EXT = {".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".swift", ".go", ".rs", ".java", ".kt", ".rb",
            ".php", ".c", ".h", ".cpp", ".cs", ".sh", ".zsh", ".html", ".css", ".sql", ".md", ".txt",
            ".json", ".yaml", ".yml", ".toml", ".csv", ".rst", ".ini", ".cfg", ".env.example"}
SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", ".cache"}


def _inside(root, rel):
    p = (root / (rel or ".")).resolve()
    try:
        p.relative_to(root.resolve())
    except ValueError:
        return None
    return p


def _iter_text(root):
    count = 0
    for p in sorted(root.rglob("*")):
        if any(part in SKIP for part in p.relative_to(root).parts[:-1]):
            continue
        if p.is_file() and p.suffix.lower() in TEXT_EXT and p.stat().st_size < 400_000 and not p.name.endswith(".tmp"):
            yield p
            count += 1
            if count > 3000:
                return


class Workbench:
    """The tools Genie may use. Writes touch only .grc/ files."""

    def __init__(self, root, agent):
        self.root = Path(root).resolve()
        self.grc = self.root / ".grc"
        self.agent = agent
        self.changed = []

    def run(self, name, args):
        fn = getattr(self, "t_" + name, None)
        if not fn:
            return f"Unknown tool {name}"
        try:
            return fn(**(args or {}))
        except TypeError as e:
            return f"Bad arguments for {name}: {e}"

    def t_search_files(self, query=""):
        q = str(query).lower().strip()
        if not q:
            return "Empty query."
        hits = []
        for p in _iter_text(self.root):
            try:
                for i, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
                    if q in line.lower():
                        hits.append(f"{p.relative_to(self.root)}:{i}: {line.strip()[:160]}")
                        if len(hits) >= 30:
                            return "\n".join(hits) + "\n(first 30 matches)"
            except OSError:
                continue
        return "\n".join(hits) or f"No matches for '{query}'."

    def t_read_file(self, path="", start=1):
        p = _inside(self.root, path)
        if not p or not p.is_file():
            return f"No such file in the project: {path}"
        lines = p.read_text(errors="ignore").splitlines()
        start = max(1, int(start or 1))
        chunk = lines[start - 1:start - 1 + 150]
        body = "\n".join(f"{start + i}: {l[:300]}" for i, l in enumerate(chunk))
        more = f"\n…({len(lines)} lines total; read again with start={start + 150} for more)" if start - 1 + 150 < len(lines) else ""
        return body + more

    def t_list_files(self, folder="."):
        p = _inside(self.root, folder)
        if not p or not p.is_dir():
            return f"No such folder: {folder}"
        items = sorted(x for x in p.iterdir() if x.name not in SKIP and x.name != ".DS_Store")
        return "\n".join(f"{x.relative_to(self.root)}{'/' if x.is_dir() else ''}" for x in items[:120]) or "(empty)"

    def t_focus(self, target=""):
        return neighbourhood(read_json(self.grc / "graph.json", {}), str(target)) or f"Nothing on the map matches '{target}'."

    def _tasks(self):
        return read_json(self.grc / "tasks.json", [])

    def t_add_task(self, title="", subtasks=None, notes=""):
        if not title:
            return "A task needs a title."
        def operation(tasks):
            nums = [int(m.group(1)) for t in tasks for m in [re.match(r"T(\d+)$", str(t.get("id", "")))] if m]
            tid = f"T{max(nums, default=0) + 1}"
            task = {"id": tid, "title": title, "status": "todo", "last_agent": self.agent,
                    "subtasks": [{"text": s, "done": False} for s in (subtasks or []) if str(s).strip()]}
            if notes:
                task["notes"] = notes
            tasks.append(task)
            return task
        task = workflow.mutate_tasks(self.grc / "tasks.json", operation)
        tid = task["id"]
        self.changed.append(f"Added task {tid}: {title}")
        return f"Added {tid}."

    def t_update_task(self, id="", status=None, done_subtasks=None, note=None):
        what = []
        project = read_json(self.grc / "project.json", {})
        review_required = bool((project.get("workflow") or {}).get("require_review"))
        def operation(tasks):
            task = next((t for t in tasks if str(t.get("id", "")).upper() == str(id).upper()), None)
            if not task:
                return None, [str(t.get("id", "?")) for t in tasks]
            if status == "done" and review_required and (task.get("review") or {}).get("status") != "accepted":
                return "refused", task
            if status:
                task["status"] = status
                what.append(f"status → {status}")
            for n in done_subtasks or []:
                subs = task.get("subtasks", [])
                if 1 <= int(n) <= len(subs):
                    subs[int(n) - 1]["done"] = True
                    subs[int(n) - 1]["agent"] = self.agent
                    what.append(f"ticked step {n}")
            if note:
                task["notes"] = (task.get("notes", "") + " " + note).strip()
                what.append("added a note")
            task["last_agent"] = self.agent
            return "updated", task
        outcome, task = workflow.mutate_tasks(self.grc / "tasks.json", operation)
        if outcome is None:
            return f"No task {id}. Existing: " + ", ".join(task)
        if outcome == "refused":
            return f"Refused: {task['id']} requires independent review before done. Use submit-for-review."
        self.changed.append(f"Updated {task['id']}: " + ", ".join(what or ["touched"]))
        return f"Updated {task['id']}: " + ", ".join(what or ["nothing changed"])

    def t_set_resume(self, now=None, next=None, blocked=None):
        state = read_json(self.grc / "state.json", {})
        if blocked is not None and blocked.strip().lower().strip(".") in ("nothing", "none", "n/a", "-", "no", "not blocked"):
            blocked = ""
        for k, v in (("now", now), ("next", next), ("blocked", blocked)):
            if v is not None:
                state[k] = v
        state["last_agent"] = self.agent
        state["updated"] = globals()["now"]()
        write_json(self.grc / "state.json", state)
        self.changed.append("Updated the Resume-here card")
        return "Resume card updated."


# ---------------------------------------------------------------- Genie

PROMPT = """You are Genie, the local GRC agent for the project below. You watch over the
project so people and other AI agents (Claude Code, Codex, Hermes) can pick up where
others stopped.

How to work:
- Answer in plain, short English. Use bullet points for lists. No preamble.
- Use the context below first. If it isn't enough, use search_files / read_file /
  list_files / focus to look — don't guess file names or contents.
- When asked to add, change or finish tasks, or update where things stand, use the
  tools, then say exactly what you changed.
- You cannot edit code or run commands. If the user asks for that, say which agent
  should do it (e.g. Claude Code for hard problems) and offer to add it as a task.

{context}"""


def history(grc, limit=10):
    p = grc / "genie.jsonl"
    if not p.exists():
        return []
    msgs = []
    for line in p.read_text().splitlines()[-limit:]:
        try:
            m = json.loads(line)
            if m.get("role") in ("user", "assistant"):
                msgs.append({"role": m["role"], "content": m.get("content", "")})
        except json.JSONDecodeError:
            pass
    return msgs


def remember(grc, role, content, **extra):
    with (grc / "genie.jsonl").open("a") as f:
        f.write(json.dumps({"ts": now(), "role": role, "content": content, **extra}, ensure_ascii=False) + "\n")


def ask(root, question, append_event, rebuild):
    root = Path(root).resolve()
    grc = root / ".grc"
    model = pick_model()
    if not model:
        emit("error", text="Ollama isn't running (or has no models). Open the Ollama app, then try again.")
        return 1
    emit("model", name=model)
    agent = f"grc-genie/{model.replace(':latest', '')}"
    bench = Workbench(root, agent)
    messages = [{"role": "system", "content": PROMPT.format(context=context_pack(root))}]
    messages += history(grc)
    messages.append({"role": "user", "content": question})
    remember(grc, "user", question)

    answer = ""
    try:
        for _ in range(MAX_STEPS):
            content, calls = "", []
            for piece, tool_calls in chat(model, messages, TOOLS):
                if piece:
                    content += piece
                    emit("token", text=piece)
                calls += tool_calls
            if not calls:
                answer = content
                break
            messages.append({"role": "assistant", "content": content, "tool_calls": calls})
            for call in calls:
                fn = call.get("function", {})
                name, args = fn.get("name", ""), fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                emit("tool", name=name, args=args)
                result = bench.run(name, args)
                emit("tool_done", name=name, summary=result.splitlines()[0][:140] if result else "")
                messages.append({"role": "tool", "content": result[:6000], "tool_name": name})
            if content:
                emit("token", text="\n\n")
        else:
            answer = content or "I ran out of steps — try a narrower question."
            if not content:
                emit("token", text=answer)
    except (OSError, RuntimeError) as e:
        emit("error", text=f"Ollama error: {e}")
        return 1

    remember(grc, "assistant", answer, model=model, changed=bench.changed)
    if bench.changed:
        append_event(grc, agent, "; ".join(bench.changed), kind="genie")
        rebuild(root)
        emit("changed", items=bench.changed)
    emit("done")
    return 0


def brief(root, append_event):
    """Background job: (re)write .grc/brief.md, a living summary of the project."""
    root = Path(root).resolve()
    grc = root / ".grc"
    model = pick_model()
    if not model:
        return "skipped: Ollama not available"
    out = grc / "brief.md"
    since = out.stat().st_mtime if out.exists() else 0
    changed = []
    for p in _iter_text(root):
        rel = p.relative_to(root).as_posix()
        if rel.startswith(".grc/") or rel == "index.html":
            continue
        if p.stat().st_mtime > since:
            changed.append(rel)
    if out.exists() and not changed:
        return "skipped: nothing changed"
    readme = next((p for p in (root / "README.md", root / "readme.md", root / "AGENTS.md") if p.exists()), None)
    material = context_pack(root, budget=7000, with_brief=False)
    if readme:
        material += "\n\n## README\n" + readme.read_text(errors="ignore")[:2500]
    if changed:
        material += "\n\n## Files changed since the last brief\n" + "\n".join(changed[:40])
    old = out.read_text()[:1500] if out.exists() else "(none yet)"
    prompt = f"""Write a short project brief in Markdown for someone (or an AI agent) who has never seen this project.
Use exactly these sections, max 220 words in total, plain English, no preamble:
## What this is
## How it works   (3-6 bullets naming the key files)
## Where it stands
## Recent changes  (based on the changed files and activity)
## Watch out for  (risks, blockers, open questions — or "Nothing notable")

Previous brief:
{old}

Material:
{material}"""
    content = "".join(piece for piece, _ in chat(model, [{"role": "user", "content": prompt}], stream=False))
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    if not content:
        return "skipped: empty answer"
    missing = brief_missing_paths(root, content)
    if missing:
        return "skipped: brief referenced missing paths: " + ", ".join(missing[:8])
    stamp = dt.datetime.now().strftime("%d %b %H:%M")
    out.write_text(f"<!-- written by grc-genie/{model} — automatic, may be wrong -->\n_Auto-written by Genie ({model}) · {stamp}_\n\n{content}\n")
    append_event(grc, f"grc-genie/{model.replace(':latest', '')}",
                  f"Refreshed the project brief ({len(changed)} file{'s' if len(changed) != 1 else ''} changed)" if changed else "Wrote the first project brief",
                  kind="brief")
    return "ok"


def brief_missing_paths(root, content):
    """Find backticked file paths in a summary that don't exist on disk.

    Small local models sometimes make up file names, and those end up in the
    map and handoffs. Only path-looking spans are checked.
    """
    root = Path(root)
    missing = []
    for token in re.findall(r"`([^`]+)`", content):
        value = token.strip().rstrip(".,:;")
        if not value or value.startswith(("http://", "https://")):
            continue
        if "/" not in value and not re.search(r"\.[A-Za-z0-9]{1,8}$", value):
            continue
        if any(char in value for char in ("*", "{", "}", "$", "\n")):
            continue
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        if not candidate.exists() and value not in missing:
            missing.append(value)
    return missing


def name_clusters(root):
    """Background job: give the map's areas plain-English names (.grc/clusters.json).

    Only asks the model about areas it hasn't named yet, or whose files changed a lot.
    Returns how many areas got a new name.
    """
    root = Path(root).resolve()
    grc = root / ".grc"
    graph = read_json(grc / "graph.json", {})
    todo = [c for c in graph.get("clusters", []) if not c.get("named_by")]
    if not todo:
        return 0
    model = pick_model()
    if not model:
        return 0
    listing = "\n".join(f"{c['id']}: files {', '.join(c['files'][:10])}" +
                         (f"; topics {', '.join(c['concepts'])}" if c["concepts"] else "") for c in todo)
    prompt = f"""These are groups of files from one software project that belong together.
Give each group a short plain-English name (2-4 words) saying what that part of the project does,
e.g. "Dashboard page", "Usage-limit watcher", "Database layer". Avoid generic words like "files" or "module".
Answer as JSON: {{"c1": "name", ...}}

{listing}"""
    try:
        raw = "".join(piece for piece, _ in chat(model, [{"role": "user", "content": prompt}], stream=False, fmt="json"))
        answer = json.loads(re.sub(r"<think>.*?</think>", "", raw, flags=re.S))
    except (OSError, ValueError, RuntimeError):
        return 0
    names = read_json(grc / "clusters.json", {})
    count = 0
    for c in todo:
        name = str(answer.get(c["id"], "")).strip().strip('"')[:40]
        if name:
            names[c["anchor"]] = {"name": name, "files": c["files"], "by": model.replace(":latest", "")}
            count += 1
    if count:
        write_json(grc / "clusters.json", names)
    return count


# ---------------------------------------------------------------- offline worker

WORK_TOOLS = TOOLS + [
    {"type": "function", "function": {
        "name": "edit_file", "description": "Replace one exact piece of text in a file. old_text must appear exactly once; include enough surrounding lines to be unique.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
            "required": ["path", "old_text", "new_text"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Create a new file, or replace a whole small file, with the given content.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
]

BLOCKED_PARTS = {".git", ".grc", "node_modules"}


class Worker(Workbench):
    """Workbench plus file editing. Every file is backed up before its first change."""

    def __init__(self, root, agent, run_id):
        super().__init__(root, agent)
        self.run_id = run_id
        self.ckpt = self.grc / "checkpoints" / run_id
        self.manifest = {}
        self.edited = []

    def _target(self, path):
        p = _inside(self.root, path)
        if not p:
            return None, f"Refused: {path} is outside the project."
        if any(part in BLOCKED_PARTS for part in p.relative_to(self.root).parts):
            return None, f"Refused: {path} is in a protected folder."
        return p, None

    def _backup(self, p):
        rel = p.relative_to(self.root).as_posix()
        if rel in self.manifest:
            return
        if p.exists():
            dest = self.ckpt / "files" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(p.read_bytes())
            self.manifest[rel] = "changed"
        else:
            self.manifest[rel] = "new"
        self.ckpt.mkdir(parents=True, exist_ok=True)
        write_json(self.ckpt / "manifest.json", {"run": self.run_id, "agent": self.agent, "files": self.manifest})

    def t_edit_file(self, path="", old_text="", new_text=""):
        p, err = self._target(path)
        if err:
            return err
        if not p.is_file():
            return f"No such file: {path}"
        text = p.read_text(errors="ignore")
        n = text.count(old_text) if old_text else 0
        if n != 1:
            return f"old_text appears {n} times in {path}; it must appear exactly once. Read the file and copy the text exactly."
        self._backup(p)
        p.write_text(text.replace(old_text, new_text, 1))
        if path not in self.edited:
            self.edited.append(path)
        return f"Edited {path}."

    def t_write_file(self, path="", content=""):
        p, err = self._target(path)
        if err:
            return err
        if len(content) > 200_000:
            return "Refused: content too large."
        self._backup(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        if path not in self.edited:
            self.edited.append(path)
        return f"Wrote {path}."


WORK_PROMPT = """You are the offline GRC worker, running on a small local model on the user's Mac.
{from_agent} had to stop (it ran out of usage). The user asked you to carry on.

Rules:
- Start from the handoff and the Resume card. Do the NEXT concrete step only — small, careful changes.
- Read a file before editing it. Copy old_text exactly. Never guess at code you haven't read.
- After finishing a step, tick it with update_task and update the Resume card (set_resume) so the next
  agent knows where things stand.
- If the step is unclear, risky or bigger than you can do well, stop and say what a stronger agent
  (Claude Code or Codex) should do — that's a good outcome.
- Finish with a short summary: what you changed (files), what's left.

{handoff}

{context}"""


def undo(root, run_id):
    root = Path(root).resolve()
    ckpt = root / ".grc" / "checkpoints" / run_id
    m = read_json(ckpt / "manifest.json", None)
    if not m:
        return f"No checkpoint {run_id}."
    restored = []
    for rel, kind in m["files"].items():
        p = root / rel
        if kind == "new":
            if p.exists():
                p.unlink()
        else:
            p.write_bytes((ckpt / "files" / rel).read_bytes())
        restored.append(rel)
    (ckpt / "manifest.json").rename(ckpt / "manifest.undone.json")
    return "Restored " + ", ".join(restored) if restored else "Nothing to restore."


def work(root, from_agent, append_event, rebuild, max_steps=30, minutes=15):
    """Carry on the work offline, after the user said yes."""
    import time as _time
    root = Path(root).resolve()
    grc = root / ".grc"
    model = pick_model()
    if not model:
        emit("error", text="Ollama isn't running (or has no models). Open the Ollama app, then try again.")
        return 1
    emit("model", name=model)
    agent = f"grc-offline/{model.replace(':latest', '')}"
    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bench = Worker(root, agent, run_id)
    handoff = (grc / "handoff.md").read_text()[:4000] if (grc / "handoff.md").exists() else "(no handoff file — use the Resume card)"
    state = read_json(grc / "state.json", {})
    doing = next((t for t in read_json(grc / "tasks.json", []) if t.get("status") == "doing"), None)
    messages = [
        {"role": "system", "content": WORK_PROMPT.format(
            from_agent=from_agent or "The previous agent", handoff="## Handoff\n" + handoff,
            context=context_pack(root, focus=doing.get("id") if doing else None, budget=7000))},
        {"role": "user", "content": f"Continue the work. Next step: {state.get('next', 'see the handoff')}"},
    ]
    remember(grc, "user", f"Continue offline from where {from_agent or 'the last agent'} stopped.")
    append_event(grc, agent, f"Started continuing offline from {from_agent or 'the last agent'} (user approved)", kind="offline")
    deadline = _time.time() + minutes * 60
    answer = ""
    try:
        for _ in range(max_steps):
            if _time.time() > deadline:
                answer += "\n\n(Stopped: time limit reached.)"
                break
            content, calls = "", []
            for piece, tool_calls in chat(model, messages, WORK_TOOLS):
                if piece:
                    content += piece
                    emit("token", text=piece)
                calls += tool_calls
            if not calls:
                answer = content
                break
            messages.append({"role": "assistant", "content": content, "tool_calls": calls})
            for call in calls:
                fn = call.get("function", {})
                name, args = fn.get("name", ""), fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                emit("tool", name=name, args=args)
                result = bench.run(name, args)
                emit("tool_done", name=name, summary=result.splitlines()[0][:140] if result else "")
                messages.append({"role": "tool", "content": result[:6000], "tool_name": name})
            if content:
                emit("token", text="\n\n")
    except (OSError, RuntimeError) as e:
        emit("error", text=f"Ollama error: {e}")
    changed = bench.changed + [f"Edited {p}" for p in bench.edited]
    remember(grc, "assistant", answer, model=model, changed=changed, checkpoint=run_id if bench.edited else None)
    note = f"Offline run finished: {len(bench.edited)} file(s) edited" + (f" ({', '.join(bench.edited[:5])}). Undo: grc.py undo --dir . {run_id}" if bench.edited else "")
    append_event(grc, agent, note, kind="offline")
    rebuild(root)
    if changed:
        emit("changed", items=changed)
    if bench.edited:
        emit("checkpoint", run=run_id)
    emit("done")
    return 0
