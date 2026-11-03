"""Notice when an AI agent runs out of usage, and write the handoff for it.

Sources (read incrementally; old history is never re-reported):
  Claude Code  ~/.claude/projects/*/*.jsonl   synthetic API-error messages
  Codex        ~/.codex/sessions/**/*.jsonl   error events
  Hermes       ~/.hermes/logs/*.log           "HTTP 429: The usage limit has been reached"

An agent that hit its limit can't write a handoff, so GRC writes one from the
session's last messages plus the git diff. If Ollama is running the local model
summarises it, otherwise the raw version is kept.
"""
import datetime as dt
import json
import re
import sqlite3
import subprocess
import time
from pathlib import Path

HOME = Path.home()
STATE = HOME / "Library/Application Support/GRC/limits.json"
LIMIT_RE = re.compile(
    r"usage limit (has been )?reached|hit your usage limit|limit reached.{0,40}reset|"
    r"out of extra usage|usage_limit_reached|rate limit reached for|quota exceeded", re.I)
RESET_RE = re.compile(r"(?:resets?\s+(?:at\s+|in\s+)?|try again in\s+)([0-9:apm\s]+(?:hours?|minutes?|mins?|h|m)?(?:\s*\([^)]*\))?|\w+ \d+)", re.I)
RECENT = 3 * 86400
COOLDOWN = 30 * 60


def _load():
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"offsets": {}, "cwd": {}, "last_hit": {}, "started": time.time()}


def _save(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE)


def _new_lines(state, path, first_run):
    """Lines appended to `path` since we last looked. On first sight, skip history."""
    key = str(path)
    try:
        size = path.stat().st_size
    except OSError:
        return []
    start = state["offsets"].get(key)
    if start is None:
        born = getattr(path.stat(), "st_birthtime", path.stat().st_mtime)
        # files that existed before GRC started watching: skip their history
        state["offsets"][key] = size if first_run or born < state["started"] else 0
        if state["offsets"][key] == size:
            return []
        start = 0
    if size < start:
        start = 0
    if size == start:
        return []
    with path.open("rb") as f:
        f.seek(start)
        data = f.read()
    end = data.rfind(b"\n")
    if end < 0:
        return []
    state["offsets"][key] = start + end + 1
    return data[:end].decode("utf-8", "ignore").splitlines()


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") in ("text", "output_text", "input_text"))
    return ""


def _recent(paths):
    cutoff = time.time() - RECENT
    return [p for p in paths if p.is_file() and p.stat().st_mtime > cutoff]


def scan_claude(state, first_run):
    for f in _recent((HOME / ".claude/projects").glob("*/*.jsonl")):
        for line in _new_lines(state, f, first_run):
            if '"assistant"' not in line or not LIMIT_RE.search(line):
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = r.get("message") or {}
            synthetic = r.get("isApiErrorMessage") or msg.get("model") == "<synthetic>" or r.get("error")
            text = _text_of(msg.get("content"))
            if r.get("type") == "assistant" and synthetic and LIMIT_RE.search(text):
                yield {"agent": "claude-code", "cwd": r.get("cwd"), "ts": r.get("timestamp"),
                       "text": text[:300], "session": str(f)}


def scan_codex(state, first_run):
    for f in _recent((HOME / ".codex/sessions").glob("**/rollout-*.jsonl")):
        key = str(f)
        lines = _new_lines(state, f, first_run)
        if key not in state["cwd"]:
            try:
                with f.open() as fh:
                    meta = json.loads(fh.readline())
                state["cwd"][key] = (meta.get("payload") or {}).get("cwd")
            except (OSError, json.JSONDecodeError):
                state["cwd"][key] = None
        for line in lines:
            if not LIMIT_RE.search(line):
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = r.get("payload") or {}
            if r.get("type") == "event_msg" and p.get("type") in ("error", "stream_error", "turn_aborted", "warning"):
                text = p.get("message") or json.dumps(p)[:300]
                if LIMIT_RE.search(text):
                    yield {"agent": "codex", "cwd": state["cwd"].get(key), "ts": r.get("timestamp"),
                           "text": text[:300], "session": key}


HERMES_RE = re.compile(r"^(\S+ \S+) ERROR \[([^\]]+)\] agent\.conversation_loop: API call failed after \d+ retries\. "
                       r"HTTP 429: (.*usage limit.*?) \| provider=(\S+) model=(\S+)")


def scan_hermes(state, first_run):
    db = HOME / ".hermes/state.db"
    for f in _recent((HOME / ".hermes/logs").glob("*.log")):
        for line in _new_lines(state, f, first_run):
            m = HERMES_RE.search(line)
            if not m:
                continue
            ts, session, text, provider, model = m.groups()
            cwd = None
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
                row = con.execute("select cwd from sessions where id = ?", (session,)).fetchone()
                con.close()
                cwd = row[0] if row else None
            except sqlite3.Error:
                pass
            yield {"agent": f"hermes/{model}", "cwd": cwd, "ts": ts.replace(",", "."), "text": text[:300],
                   "session": session, "source": "hermes"}


def find_project(cwd, projects):
    if not cwd:
        return None
    c = Path(cwd).resolve()
    best = None
    for p in projects:
        p = Path(p).resolve()
        if c == p or p in c.parents:
            if best is None or len(str(p)) > len(str(best)):
                best = p
    return best


def detect(projects):
    """Return new out-of-usage events inside registered GRC projects."""
    state = _load()
    first_run = not state["offsets"]
    hits = []
    for scanner in (scan_claude, scan_codex, scan_hermes):
        try:
            for hit in scanner(state, first_run):
                root = find_project(hit.get("cwd"), projects)
                if not root:
                    continue
                key = f"{hit['agent'].split('/')[0]}|{root}"
                if time.time() - state["last_hit"].get(key, 0) < COOLDOWN:
                    continue
                state["last_hit"][key] = time.time()
                hit["project"] = str(root)
                m = RESET_RE.search(hit["text"])
                hit["reset"] = m.group(0).strip() if m else ""
                hits.append(hit)
        except OSError:
            continue
    _save(state)
    return hits


# ---------------------------------------------------------------- handoff

def transcript_tail(hit, limit=14):
    """The last few real messages of the session that stopped."""
    out = []
    src = hit.get("session", "")
    try:
        if hit["agent"].startswith("hermes"):
            con = sqlite3.connect(f"file:{HOME / '.hermes/state.db'}?mode=ro", uri=True, timeout=2)
            rows = con.execute("select role, content from messages where session_id = ? and role in ('user','assistant') "
                               "and content is not null and content != '' order by id desc limit ?", (src, limit)).fetchall()
            con.close()
            out = [(r, c) for r, c in reversed(rows)]
        elif src.endswith(".jsonl"):
            for line in Path(src).read_text(errors="ignore").splitlines()[-400:]:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if hit["agent"] == "codex":
                    p = r.get("payload") or {}
                    if r.get("type") == "response_item" and p.get("type") == "message" and p.get("role") in ("user", "assistant"):
                        t = _text_of(p.get("content"))
                        if t and not t.lstrip().startswith("<"):
                            out.append((p["role"], t))
                elif r.get("type") in ("user", "assistant") and not r.get("isApiErrorMessage"):
                    t = _text_of((r.get("message") or {}).get("content"))
                    if t:
                        out.append((r["type"], t))
            out = out[-limit:]
    except (OSError, sqlite3.Error):
        pass
    return [(role, text.strip()[:1200]) for role, text in out]


def git_changes(root):
    def run(*a):
        try:
            r = subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, timeout=10)
            return r.stdout.strip() if r.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""
    return run("status", "--short")[:2000], run("diff", "--stat")[:2000]


def write_handoff(root, hit, genie, append_event, build):
    root = Path(root)
    grc = root / ".grc"
    agent = hit["agent"]
    tail = transcript_tail(hit)
    status, stat = git_changes(root)
    last_user = next((t for r, t in reversed(tail) if r == "user"), "")
    when = dt.datetime.now().strftime("%d %b %Y %H:%M")

    summary = {}
    model = genie.pick_model()
    if model and tail:
        convo = "\n\n".join(f"{r.upper()}: {t}" for r, t in tail)[-9000:]
        prompt = f"""An AI coding agent ({agent}) just ran out of usage in the middle of work. From its last messages
and the file changes, write a handoff as JSON with keys:
"now" (one sentence: where the work stands), "next" (one sentence: the exact next step),
"done" (list of short strings), "remaining" (list of short strings), "files" (list of file paths that matter).
Only use facts from the material.

LAST MESSAGES:
{convo}

GIT STATUS:
{status or '(no changes or not a git repo)'}
{stat}"""
        try:
            raw = "".join(p for p, _ in genie.chat(model, [{"role": "user", "content": prompt}], stream=False, fmt="json"))
            summary = json.loads(raw)
        except (OSError, ValueError, RuntimeError):
            summary = {}

    def bullets(items):
        return "\n".join(f"- {x}" for x in items if str(x).strip()) or "- (unknown)"

    text = f"# Handoff: {agent} ran out of usage\n_{when} · written by GRC from outside the session" + \
           (f" · summarised by {model}" if summary else "") + "_\n\n"
    if hit.get("reset"):
        text += f"**Limit message:** {hit['text'][:200]}\n\n"
    if summary:
        text += f"## Where it stands\n{summary.get('now', '')}\n\n## Next step\n{summary.get('next', '')}\n\n"
        text += f"## Done\n{bullets(summary.get('done', []))}\n\n## Remaining\n{bullets(summary.get('remaining', []))}\n\n"
        if summary.get("files"):
            text += "## Files that matter\n" + "\n".join(f"- `{f}`" for f in summary["files"]) + "\n\n"
    if last_user:
        text += f"## What the user last asked\n> {last_user[:800].replace(chr(10), chr(10) + '> ')}\n\n"
    if tail:
        text += "## Last messages\n" + "\n".join(f"- **{r}:** {t[:300].replace(chr(10), ' ')}" for r, t in tail[-6:]) + "\n\n"
    if status:
        text += f"## Uncommitted changes\n```\n{status}\n```\n"
    (grc / "handoff.md").write_text(text)

    bench = genie.Workbench(root, agent)
    bench.t_set_resume(
        now=(summary.get("now") or f"{agent} ran out of usage mid-task.")[:400],
        next=(summary.get("next") or (f"Continue: {last_user[:200]}" if last_user else "Read .grc/handoff.md and continue."))[:400],
    )
    append_event(grc, agent, f"Ran out of usage{(' — ' + hit['reset']) if hit.get('reset') else ''}. GRC wrote the handoff.", kind="limit")
    build(root)
    return text
