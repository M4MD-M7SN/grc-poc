#!/usr/bin/env python3
"""GRC (General Repo Context).

Keeps a small `.grc/` folder inside a project (state, tasks, events) and turns
it into one dashboard HTML page that any person or AI agent can read to pick up
where the last one stopped. Standard library only; no model needed.

  grc.py init  [DIR]                       create .grc/ for a project
  grc.py build [DIR]                       re-check the project and rebuild the dashboard
  grc.py add   --dir DIR FILE...           add files/folders to the project's context
  grc.py log   --agent A [--dir DIR] "message"   record what an agent just did
  grc.py context [--dir DIR] [--focus T5]         short briefing for any AI agent
  grc.py ask   [--dir DIR] "question"             ask Genie, the local agent (Ollama)
  grc.py brief [--dir DIR]                        let Genie refresh .grc/brief.md
  grc.py limits PROJECT...                        detect agents that ran out of usage; write handoffs
  grc.py continue [--dir DIR] [--from AGENT]      carry on offline with the local model
  grc.py undo  [--dir DIR] RUN                    undo the files an offline run changed
  grc.py hooks [DIR]                              git hooks: rebuild after commits, tag commits with $GRC_AGENT
  grc.py overview                                 one page linking every GRC project
"""
import argparse
import datetime as dt
import os
from html import escape as html_escape
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from scan import scan  # noqa: E402
import genie  # noqa: E402
import limits  # noqa: E402
import task_sources  # noqa: E402
import identity  # noqa: E402

TEMPLATE = HERE / "template.html"


def now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def read_events(grc):
    path = grc / "events.jsonl"
    if not path.exists():
        return []
    events = []
    actors = read_json(grc / "actors.json", {}).get("actors", [])
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                event = json.loads(line)
                state, reason = identity.verify_event(event, actors)
                event["signature_state"] = state
                event["signature_reason"] = reason
                events.append(event)
            except json.JSONDecodeError:
                pass
    return events


def append_event(grc, agent, text, kind="note", task=None):
    try:
        event, actor = identity.signed_event(agent, text, kind, task)
        identity.project_actor_projection(grc, actor)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        event = {"ts": now(), "agent": agent, "type": kind, "text": text,
                 "signature_state": "unavailable", "signature_error": str(exc)[:300]}
        if task:
            event["task"] = task
    with (grc / "events.jsonl").open("a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def git_commits(root, limit=60):
    """Recent commits, with the agent taken from an `Agent:` commit trailer."""
    sep = "\x1f"
    fmt = sep.join(["%H", "%an", "%aI", "%s", "%(trailers:key=Agent,valueonly,separator=%x2C)",
                    "%(trailers:key=Co-authored-by,valueonly,separator=%x2C)",
                    "%(trailers:key=GRC-Actor-ID,valueonly,separator=%x2C)", "%G?", "%GF"])
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "log", f"-n{limit}", f"--pretty=format:{fmt}%x1e"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    commits = []
    for rec in out.stdout.split("\x1e"):
        parts = rec.strip("\n").split(sep)
        if len(parts) != 9:
            continue
        sha, author, ts, subject, agent, coauthors, actor_id, signature_state, signer_fingerprint = parts
        if not agent.strip():
            # no Agent: trailer, fall back to the Co-Authored-By line
            co = coauthors.lower()
            agent = "claude-code" if "claude" in co else "codex" if "codex" in co or "openai" in co else ""
        commits.append({
            "ts": ts, "type": "commit", "sha": sha[:7], "text": subject,
            "agent": agent.strip() or author,
            "actor_id": actor_id.strip(),
            "git_signature_state": signature_state.strip(),
            "actor_fingerprint": signer_fingerprint.strip(),
        })
    return commits


def task_percent(task):
    subs = task.get("subtasks") or []
    if subs:
        return round(100 * sum(1 for s in subs if s.get("done")) / len(subs))
    if task.get("status") == "done":
        return 100
    try:
        value = int(task.get("percent", 0) or 0)
    except (TypeError, ValueError):
        value = 0
    return max(0, min(100, value))


def resolve_tasks(root, project):
    """Read this project's canonical task source, falling back to local GRC tasks."""
    local = read_json(root / ".grc" / "tasks.json", [])
    source = project.get("task_source") or {}
    return task_sources.resolve(root, source, local)


CUSTOM_DASHBOARD = ".grc/custom-dashboard.html"

STARTER_DASHBOARD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} · Dashboard</title>
<style>
:root {{ --bg:#f6f5f2; --card:#fff; --ink:#1d1d1b; --muted:#6b6a66; --line:#e4e2dc; --accent:#2f7d6b; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#141514; --card:#1e201f; --ink:#ecebe7; --muted:#9b9a95; --line:#2d302e; --accent:#5fbf9f; }} }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }}
.wrap {{ max-width:1040px; margin:0 auto; padding:28px 16px 64px; }}
a {{ color:var(--accent); }}
h1 {{ margin:0 0 4px; font-size:30px; }}
.sub {{ color:var(--muted); margin:0 0 24px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:20px 22px; }}
</style></head>
<body><div class="wrap">
  <p><a href="{back}">← Project overview</a></p>
  <h1>{name}</h1>
  <p class="sub">Project dashboard</p>
  <div class="card">
    <p>This page is waiting for its design. Edit <code>.grc/custom-dashboard.html</code>
    to decide what it shows — GRC never overwrites it.</p>
  </div>
</div></body></html>
"""


def custom_dashboard(root, project, dashboard_out):
    """Make sure the project's custom dashboard exists; return its link relative to the main dashboard."""
    page = root / CUSTOM_DASHBOARD
    builder = project.get("custom_dashboard_build")  # optional project script that writes the page
    if builder:
        try:
            subprocess.run([sys.executable, str(root / builder)], cwd=root, timeout=60,
                           capture_output=True, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    if not page.exists():
        back = os.path.relpath(dashboard_out, page.parent)
        name = html_escape(project.get("name") or root.name)
        page.write_text(STARTER_DASHBOARD.format(name=name, back=html_escape(back)))
    return os.path.relpath(page, dashboard_out.parent)


def build(root, rescan=True):
    """Rebuild projections and dashboard; reuse repository scan for state-only changes."""
    grc = root / ".grc"
    if not grc.is_dir():
        sys.exit(f"No .grc/ in {root} — run: grc.py init {root}")
    project = read_json(grc / "project.json", {})
    state = read_json(grc / "state.json", {})
    tasks = resolve_tasks(root, project)
    for t in tasks:
        t["percent"] = task_percent(t)
    write_json(grc / "resolved-tasks.json", tasks)
    events = read_events(grc)
    timeline = events + git_commits(root)
    timeline.sort(key=lambda e: e.get("ts", ""), reverse=True)
    if rescan or not (grc / "scan.json").exists() or not (grc / "graph.json").exists():
        health, graph = scan(root, project, tasks, events)
        write_json(grc / "scan.json", health)
        write_json(grc / "graph.json", graph)
    else:
        health = read_json(grc / "scan.json", {})
        graph = read_json(grc / "graph.json", {})

    capsule = genie.context_capsule(root, max_bytes=4000, persist=False)
    context_status = {
        "schema": capsule.get("schema", ""),
        "fingerprint": capsule.get("source_fingerprint", ""),
        "bytes": len(json.dumps(capsule, ensure_ascii=False, separators=(",", ":")).encode()),
        "freshness": capsule.get("freshness", {}),
        "review_queue": capsule.get("review_queue", []),
    }

    out = root / project.get("dashboard", ".grc/dashboard.html")
    data = {
        "custom_dashboard": custom_dashboard(root, project, out),
        "bots": read_json(root / project["bots_status"], None) if project.get("bots_status") else None,
        "project": project, "state": state, "tasks": tasks,
        "timeline": timeline, "built_at": now(), "root": str(root),
        "health": health, "graph": graph,
        "task_source": read_json(grc / task_sources.STATUS_FILE, {}),
        "actors": read_json(grc / "actors.json", {"version": 2, "actors": []}),
        "context_status": context_status,
        "brief": (grc / "brief.md").read_text() if (grc / "brief.md").exists() else "",
        "overall": round(sum(t["percent"] for t in tasks) / len(tasks)) if tasks else 0,
    }
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.read_text().replace("/*GRC_DATA*/null", blob)
    name = project.get("name") or root.name
    html = html.replace("<!--GRC_TITLE-->", name if "GRC" in name else name + " · GRC")
    out.write_text(html)
    return out


GITIGNORE = """# made by GRC: rebuilt from the project, so not worth committing
dashboard.html
scan.json
graph.json
genie*.jsonl
resolved-tasks.json
task-source-status.json
checkpoints/
"""

REGISTRY = Path.home() / "Library/Application Support/GRC/projects.json"
OVERVIEW = Path.home() / "Library/Application Support/GRC/overview.html"


def overview(paths=None):
    """One page for every project GRC.app watches: progress, where it stands, what needs attention."""
    if paths is None:
        paths = [p.get("path") for p in read_json(REGISTRY, []) if p.get("path")]
    cards = []
    for raw in paths:
        root = Path(raw)
        grc = root / ".grc"
        if not grc.is_dir():
            continue
        project = read_json(grc / "project.json", {})
        state = read_json(grc / "state.json", {})
        tasks = resolve_tasks(root, project)
        health = read_json(grc / "scan.json", {})
        counts = {}
        for t in tasks:
            counts[t.get("status", "todo")] = counts.get(t.get("status", "todo"), 0) + 1
        events = read_events(grc)
        last = max(events, key=lambda e: e.get("ts", "")) if events else {}
        alerts = []
        exposed = [e for e in health.get("env_files", []) if not e.get("ignored")]
        if exposed:
            alerts.append(f"{len(exposed)} .env file{'s' if len(exposed) > 1 else ''} not ignored by git")
        if (grc / "handoff.md").exists() and last.get("type") in ("limit", "handoff"):
            alerts.append("Handoff waiting")
        if state.get("blocked"):
            alerts.append("Blocked: " + state["blocked"][:120])
        git = health.get("git") or {}
        cards.append({
            "name": project.get("name") or root.name, "goal": project.get("goal", ""),
            "status": project.get("status", "active"), "path": str(root),
            "dashboard": str(root / project.get("dashboard", ".grc/dashboard.html")),
            "overall": round(sum(task_percent(t) for t in tasks) / len(tasks)) if tasks else 0,
            "counts": counts, "now": state.get("now", ""), "next": state.get("next", ""),
            "last_agent": last.get("agent") or state.get("last_agent", ""),
            "updated": last.get("ts") or state.get("updated", ""),
            "uncommitted": (git.get("changed") or 0) + (git.get("untracked") or 0),
            "alerts": alerts,
        })
    cards.sort(key=lambda c: c["updated"], reverse=True)
    data = {"projects": cards, "built_at": now()}
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = (HERE / "overview.html").read_text().replace("/*GRC_DATA*/null", blob)
    OVERVIEW.parent.mkdir(parents=True, exist_ok=True)
    OVERVIEW.write_text(html)
    return OVERVIEW


HOOK_MARK = "# >>> grc >>>"
HOOKS = {
    # run in the background so the commit isn't slowed down
    "post-commit": f'''{HOOK_MARK}
GRC_PY="{HERE / 'grc.py'}"
ROOT="$(git rev-parse --show-toplevel)"
if [ -f "$GRC_PY" ] && [ -d "$ROOT/.grc" ]; then
  (/usr/bin/python3 "$GRC_PY" build "$ROOT" >/dev/null 2>&1 &)
fi
# <<< grc <<<
''',
    # GRC_AGENT / GRC_ACTOR_ID become commit trailers
    "prepare-commit-msg": f'''{HOOK_MARK}
if [ -n "$GRC_AGENT" ] && ! grep -qi '^Agent:' "$1"; then
  git interpret-trailers --in-place --if-exists doNothing --trailer "Agent: $GRC_AGENT" "$1"
fi
if [ -n "$GRC_ACTOR_ID" ] && ! grep -qi '^GRC-Actor-ID:' "$1"; then
  git interpret-trailers --in-place --if-exists doNothing --trailer "GRC-Actor-ID: $GRC_ACTOR_ID" "$1"
fi
# <<< grc <<<
''',
}


def install_hooks(root):
    """Add GRC's git hooks. Never touches a hook someone else wrote."""
    def git(*a):
        r = subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None
    if git("rev-parse", "--is-inside-work-tree") != "true":
        return ["not a git repo — skipped"]
    if git("config", "core.hooksPath"):
        return ["core.hooksPath is set (husky or similar) — skipped; add the hooks there by hand"]
    hooks = Path(root, git("rev-parse", "--git-path", "hooks"))
    hooks.mkdir(parents=True, exist_ok=True)
    out = []
    for name, body in HOOKS.items():
        f = hooks / name
        old = f.read_text() if f.exists() else ""
        if HOOK_MARK in old and "# <<< grc <<<\n" in old:
            new = old.split(HOOK_MARK)[0] + body + old.split("# <<< grc <<<\n", 1)[1]
            out.append(f"{name}: updated")
        elif not old.strip():
            new = "#!/bin/sh\n" + body
            out.append(f"{name}: installed")
        else:
            out.append(f"{name}: you already have one — skipped")
            continue
        f.write_text(new)
        f.chmod(0o755)
    return out


def init(root):
    grc = root / ".grc"
    grc.mkdir(parents=True, exist_ok=True)
    if not (grc / "project.json").exists():
        write_json(grc / "project.json", {
            "name": root.name,
            "goal": "Describe in one sentence what this project is for.",
            "status": "active",
            "dashboard": ".grc/dashboard.html",
            "flow": [],
            "sections": [],
        })
    if not (grc / "state.json").exists():
        write_json(grc / "state.json", {
            "now": "Project registered with GRC.",
            "next": "Fill in the goal and the first tasks.",
            "blocked": "",
            "last_agent": "grc",
            "updated": now(),
        })
    if not (grc / "tasks.json").exists():
        write_json(grc / "tasks.json", [])
    if not (grc / ".gitignore").exists():
        (grc / ".gitignore").write_text(GITIGNORE)
    if not (grc / "events.jsonl").exists():
        append_event(grc, "grc", f"GRC initialised for {root.name}", kind="init")
    return build(root)


def add(root, paths, agent="you"):
    """Bring dropped files into the project's context.

    Files already inside the project are pinned in place; anything from
    outside is copied into .grc/context/ so the context travels with it.
    """
    grc = root / ".grc"
    if not grc.is_dir():
        init(root)
    project = read_json(grc / "project.json", {})
    pinned = project.setdefault("pinned", [])
    ctx = grc / "context"
    added = []
    for raw in paths:
        src = Path(raw).expanduser().resolve()
        if not src.exists():
            continue
        try:
            rel = src.relative_to(root).as_posix()
        except ValueError:
            rel = None
        if rel is not None and not rel.startswith(".grc/"):
            if rel not in pinned:
                pinned.append(rel)
            added.append(src.name)
            continue
        ctx.mkdir(exist_ok=True)
        dest, i = ctx / src.name, 2
        while dest.exists():
            dest, i = ctx / f"{src.stem}-{i}{src.suffix}", i + 1
        if src.is_dir():
            shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git", "node_modules", ".DS_Store"))
        else:
            shutil.copy2(src, dest)
        added.append(dest.name)
    write_json(grc / "project.json", project)
    if added:
        append_event(grc, agent, "Added to context: " + ", ".join(added), kind="context")
    return build(root)


def main():
    p = argparse.ArgumentParser(description="General Repo Context")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("init", "build"):
        s = sub.add_parser(name)
        s.add_argument("dir", nargs="?", default=".")
    s = sub.add_parser("add")
    s.add_argument("--dir", default=".")
    s.add_argument("--agent", default="you")
    s.add_argument("paths", nargs="+")
    s = sub.add_parser("limits")
    s.add_argument("projects", nargs="*")
    s.add_argument("--no-handoff", action="store_true")
    for name in ("context", "ask", "brief", "continue", "undo"):
        s = sub.add_parser(name)
        s.add_argument("--dir", default=".")
        if name == "context":
            s.add_argument("--focus", help="task id, file path or topic")
        if name == "ask":
            s.add_argument("question")
        if name == "continue":
            s.add_argument("--from", dest="from_agent", default="")
        if name == "undo":
            s.add_argument("run")
    s = sub.add_parser("hooks")
    s.add_argument("dir", nargs="?", default=".")
    s = sub.add_parser("overview")
    s.add_argument("projects", nargs="*", help="default: the projects GRC.app watches")
    s = sub.add_parser("actor")
    s.add_argument("name", help="human or AI-agent identity, e.g. alex or hermes/gpt-5.6-sol")
    s.add_argument("--kind", choices=("user", "agent", "service"))
    s.add_argument("--dir", default=".", help="project receiving the public actor projection")
    s = sub.add_parser("log")
    s.add_argument("--dir", default=".")
    s.add_argument("--agent", required=True, help="who did it, e.g. codex/gpt-5.6-sol")
    s.add_argument("--task", help="task id this relates to")
    s.add_argument("--type", default="note")
    s.add_argument("message")
    args = p.parse_args()
    if args.cmd == "limits":
        for hit in limits.detect(args.projects):
            if not args.no_handoff:
                try:
                    limits.write_handoff(hit["project"], hit, genie, append_event, build)
                    hit["handoff"] = True
                except Exception as e:  # report the hit even if the handoff failed
                    hit["handoff_error"] = str(e)
            print(json.dumps(hit, ensure_ascii=False), flush=True)
        return
    if args.cmd == "overview":
        print(overview(args.projects or None))
        return
    if args.cmd == "actor":
        actor = identity.ensure_actor(args.name, args.kind)
        root = Path(args.dir).expanduser().resolve()
        if (root / ".grc").is_dir():
            identity.project_actor_projection(root / ".grc", actor)
            build(root)
        print(json.dumps({key: value for key, value in actor.items() if key != "public_key"}, indent=2))
        return
    root = Path(args.dir).expanduser().resolve()

    if args.cmd == "init":
        print(init(root))
    elif args.cmd == "build":
        print(build(root))
    elif args.cmd == "hooks":
        print("\n".join(install_hooks(root)))
    elif args.cmd == "context":
        if not (root / ".grc" / "scan.json").exists():
            build(root)
        print(genie.context_pack(root, args.focus))
    elif args.cmd == "ask":
        sys.exit(genie.ask(root, args.question, append_event, build))
    elif args.cmd == "continue":
        sys.exit(genie.work(root, args.from_agent, append_event, build))
    elif args.cmd == "undo":
        print(genie.undo(root, args.run))
        build(root)
    elif args.cmd == "brief":
        result = genie.brief(root, append_event)
        named = genie.name_clusters(root)
        if result == "ok" or named:
            build(root)
        print(result + (f"; named {named} area{'s' if named != 1 else ''} of the map" if named else ""))
    elif args.cmd == "add":
        print(add(root, args.paths, args.agent))
    elif args.cmd == "log":
        append_event(root / ".grc", args.agent, args.message, args.type, args.task)
        print(build(root))


if __name__ == "__main__":
    main()
