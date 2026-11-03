"""Background checks and the project map (semantic network) for GRC.

Plain parsing only, no model and no network calls:
  health: what's in the folder, TODOs left in code, git state, risky files
  graph:  nodes (folders, files, concepts, tasks, agents) and the typed
          links between them (contains, imports, links_to, mentions,
          about, worked_on, changed)
"""
import ast
import json
import os
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

SKIP_DIRS = {
    ".git", ".grc", "node_modules", ".venv", "venv", "env", "__pycache__",
    "dist", "build", ".next", ".cache", "Pods", "DerivedData", ".idea",
    ".vscode", "target", ".mypy_cache", ".pytest_cache", "site-packages",
}
MAX_FILES = 3000
MAX_READ = 400_000
LARGE_FILE = 10 * 1024 * 1024

LANG = {
    ".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".swift": "Swift",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".rb": "Ruby",
    ".php": "PHP", ".c": "C", ".h": "C", ".cpp": "C++", ".cs": "C#", ".sh": "Shell",
    ".zsh": "Shell", ".html": "HTML", ".css": "CSS", ".sql": "SQL", ".md": "Markdown",
    ".txt": "Text", ".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML",
    ".csv": "CSV", ".ipynb": "Notebook", ".pdf": "PDF", ".docx": "Word", ".xlsx": "Excel",
}
CODE = {".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".swift", ".go", ".rs",
        ".java", ".kt", ".rb", ".php", ".c", ".h", ".cpp", ".cs", ".sh", ".zsh",
        ".html", ".css", ".sql"}
DOCS = {".md", ".txt", ".rst", ".pdf", ".docx"}
DATA = {".json", ".yaml", ".yml", ".toml", ".csv", ".xlsx", ".ipynb"}
TEXT = CODE | {".md", ".txt", ".rst"}

STOP = set("""
self this that with from into have will your what when then than them they their there
here were been being which while where about after before other some such only also just
very more most many much each every main test tests file files data true false none null
return import class function const string number value values list dict type types item
items index init setup config utils util helper helpers default args kwargs param params
error errors print path paths name names make makes using used user users todo fixme note
readme license http https www html json yaml text line lines code new old get set run
build resolve load format create start read parse check install open save normalize
handle update delete write show apply find fetch send process render compute convert
validate ensure should would could props result results response call calls add remove
""".split())

TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b[:\s-]*(.*)")
JS_IMPORT_RE = re.compile(r"""(?:from\s*|import\s*\(\s*|require\s*\(\s*|import\s+)['"](\.{1,2}/[^'"]+)['"]""")
MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s#]+)")
HTML_REF_RE = re.compile(r"""(?:src|href)\s*=\s*["']([^"'#?:]+)["']""")
DEF_RE = re.compile(r"\b(?:def|class|func|function|struct|interface|enum|type|fn)\s+([A-Za-z_][A-Za-z0-9_]*)")
HEADING_RE = re.compile(r"^#{1,4}\s+(.+)$", re.M)
CAMEL_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")

CAPABILITIES = (
    ("frontend", "Frontend"),
    ("backend", "Backend"),
    ("user_interface", "User interface"),
    ("data", "Data"),
    ("database_storage", "Database / storage"),
    ("ml_ai", "ML / AI"),
    ("servers_infrastructure", "Servers / infrastructure"),
    ("mcp", "MCP"),
    ("tools_integrations", "Tools / integrations"),
    ("bots", "Bots"),
)


def _git(root, *args):
    try:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def terms(text):
    """Split identifiers/headings into lowercase concept words."""
    words = []
    for chunk in re.split(r"[^A-Za-z0-9]+", text):
        for w in CAMEL_RE.findall(chunk):
            w = w.lower()
            if len(w) >= 4 and not w.isdigit() and w not in STOP:
                words.append(w)
    return words


def detect_capabilities(paths, texts=None, excluded_paths=None):
    """Detect capabilities (MCP, bots, APIs, ...) and the files that show them.

    Every key is always returned, with present=True/False. Mentions in docs
    alone don't count.
    """
    def canonical_path(path):
        return str(path).replace("\\", "/")

    texts = {canonical_path(path): text for path, text in (texts or {}).items()}
    excluded_paths = {canonical_path(path) for path in (excluded_paths or ())}
    normalized_paths = sorted({canonical_path(path) for path in paths})
    evidence = {capability_id: set() for capability_id, _ in CAPABILITIES}

    def mark(capability_id, path):
        evidence[capability_id].add(path)

    for path in normalized_paths:
        lower = path.lower()
        if path in excluded_paths or lower.startswith(".grc/context/"):
            continue
        wrapped = "/" + lower.strip("/") + "/"
        name = Path(lower).name
        suffix = Path(lower).suffix
        text = str(texts.get(path, "")).lower()
        segments = set(lower.split("/"))
        test_file = (
            bool(segments & {"test", "tests", "__tests__", "spec", "specs"})
            or name.startswith("test_") or ".test." in name or ".spec." in name
            or bool(re.search(r"(?:_test|_spec)\.[^.]+$", name))
        )
        if test_file:
            continue
        if segments & {"doc", "docs", "documentation"}:
            continue
        semantic_text = text
        implementation = suffix in CODE or suffix in DATA or name in {
            "dockerfile", "containerfile", "package.json", "pyproject.toml",
            "cargo.toml", "go.mod", "podfile", "gemfile",
        }

        frontend = (
            suffix in {".html", ".css", ".scss", ".sass", ".jsx", ".tsx", ".vue", ".svelte"}
            or (suffix in {".js", ".mjs", ".ts"} and any(part in wrapped for part in
                ("/frontend/", "/web/", "/client/", "/components/", "/pages/")))
            or (name == "package.json" and re.search(r'"(?:react|vue|svelte|next|vite|angular)"', semantic_text))
        )
        if frontend:
            mark("frontend", path)

        user_interface = frontend or (
            implementation and any(part in wrapped for part in
                ("/ui/", "/views/", "/screens/", "/dashboard/", "/templates/"))
        ) or (implementation and bool(re.search(r"\b(?:swiftui|uikit|appkit|reactdom)\b|<body\b", semantic_text)))
        if user_interface:
            mark("user_interface", path)

        backend = implementation and (
            any(part in wrapped for part in ("/backend/", "/server/", "/api/", "/routes/", "/controllers/", "/services/"))
            or bool(re.search(r"\b(?:fastapi|flask|django|aiohttp|uvicorn|express|nestjs|springframework)\b|http\.createserver|@app\.route", semantic_text))
        )
        if backend:
            mark("backend", path)

        data = (
            suffix in {".csv", ".xlsx", ".parquet", ".arrow", ".ipynb"}
            or (suffix in {".json", ".yaml", ".yml"} and any(part in wrapped for part in
                ("/data/", "/datasets/", "/evidence/", "/records/")))
            or (implementation and any(part in wrapped for part in ("/data/", "/datasets/", "/etl/", "/pipelines/", "/ingest/")))
        )
        if data:
            mark("data", path)

        database = (
            suffix == ".sql"
            or (implementation and any(part in wrapped for part in
                ("/migrations/", "/database/", "/db/", "/repositories/", "/storage/")))
            or (implementation and bool(re.search(r"\b(?:sqlite3|sqlalchemy|psycopg|postgresql|supabase|prisma|mongoose)\b", semantic_text)))
        )
        if database:
            mark("database_storage", path)
            mark("data", path)

        ml_ai = implementation and (
            any(part in wrapped for part in ("/ml/", "/ai/", "/training/", "/inference/", "/agents/"))
            or bool(re.search(r"\b(?:torch|tensorflow|sklearn|transformers|ollama|openai|anthropic|llama_cpp)\b", semantic_text))
        )
        if ml_ai:
            mark("ml_ai", path)

        infrastructure = (
            name in {
                "dockerfile", "containerfile", "compose.yml", "compose.yaml",
                "docker-compose.yml", "docker-compose.yaml", "nginx.conf",
            }
            or suffix == ".tf"
            or (implementation and any(part in wrapped for part in (
                "/infra/", "/infrastructure/", "/terraform/", "/k8s/",
                "/kubernetes/", "/deploy/", "/deployment/", "/helm/",
                "/nginx/", "/launchagents/", "/systemd/",
            )))
        )
        if infrastructure:
            mark("servers_infrastructure", path)

        mcp = implementation and (
            bool(re.search(r"(?:^|[/_.-])mcp(?:[/_.-]|$)", lower))
            or bool(re.search(r"\b(?:fastmcp|model context protocol|mcpserver|mcp_server)\b|@mcp\.tool", semantic_text))
        )
        if mcp:
            mark("mcp", path)
            mark("tools_integrations", path)

        tools = implementation and (
            any(part in wrapped for part in ("/tools/", "/integrations/", "/connectors/", "/adapters/", "/plugins/", "/cli/"))
            or bool(re.search(r"(?:^|[/_.-])(?:tool|connector|integration|plugin|cli)(?:[/_.-]|$)", lower))
        )
        if tools:
            mark("tools_integrations", path)

        # Long-running automated workers: a bots/ folder, *_bot.py style files, or Bot classes.
        bots = implementation and (
            any(part in wrapped for part in ("/bots/", "/bot/"))
            or bool(re.search(r"(?:^|[_.-])bot\.[a-z]+$", name))
            or bool(re.search(r"\bclass \w*bot\b", semantic_text))
        )
        if bots:
            mark("bots", path)

    return [
        {
            "id": capability_id,
            "name": name,
            "present": bool(evidence[capability_id]),
            "evidence": sorted(evidence[capability_id])[:12],
            "count": len(evidence[capability_id]),
        }
        for capability_id, name in CAPABILITIES
    ]


def walk(root):
    files, truncated = [], False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if name == ".DS_Store":
                continue
            files.append(Path(dirpath) / name)
            if len(files) >= MAX_FILES:
                return files, True
    return files, truncated


def resolve(root, base_dir, target, exts=("",)):
    target = target.split("?")[0]
    for ext in exts:
        p = (base_dir / (target + ext)).resolve()
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if p.is_file():
            return rel.as_posix()
    return None


def py_imports(root, path, text, known):
    out = set()
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return out
    rel_dir = path.parent.relative_to(root)
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):
            mods = [(a.name, 0) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            mods = [(base, node.level)] + [(f"{base}.{a.name}" if base else a.name, node.level) for a in node.names]
        for mod, level in mods:
            parts = [p for p in mod.split(".") if p]
            if not parts:
                continue
            if level == 0:
                bases = [rel_dir, Path(".")]
                # Common src-layout packages are imported by package name even
                # though their filesystem path is nested under */src/.
                for known_path in known:
                    parts_in_path = Path(known_path).parts
                    for i, part in enumerate(parts_in_path[:-1]):
                        if part == "src":
                            base = Path(*parts_in_path[:i + 1])
                            if base not in bases:
                                bases.append(base)
            else:
                b = rel_dir
                for _ in range(level - 1):
                    b = b.parent
                bases = [b]
            for b in bases:
                cand = (b / Path(*parts)).as_posix()
                for c in (cand + ".py", cand + "/__init__.py"):
                    c = c[2:] if c.startswith("./") else c
                    if c in known:
                        out.add(c)
    return out


def scan(root, project=None, tasks=None, events=None):
    root = Path(root).resolve()
    project, tasks, events = project or {}, tasks or [], events or []
    files, truncated = walk(root)
    ctx_dir = root / ".grc" / "context"
    ctx_files = [p for p in ctx_dir.rglob("*") if p.is_file() and p.name != ".DS_Store"] if ctx_dir.is_dir() else []
    pinned = set(project.get("pinned", []))

    all_files = files + ctx_files
    rels = {p: p.relative_to(root).as_posix() for p in all_files}
    known = set(rels.values())

    nodes, edges = {}, []

    def node(nid, label, kind, **extra):
        if nid not in nodes:
            nodes[nid] = {"id": nid, "label": label, "type": kind, **extra}
        return nid

    def edge(s, t, rel, w=1):
        if s != t:
            edges.append({"s": s, "t": t, "rel": rel, "w": w})

    node("project", project.get("name") or root.name, "project")

    langs, lines_total, todos, large, envs = Counter(), 0, [], [], []
    file_terms, capability_text = {}, {}

    for p in all_files:
        rel = rels[p]
        ext = p.suffix.lower()
        try:
            size = p.stat().st_size
        except OSError:
            continue
        in_ctx = rel.startswith(".grc/context/")
        kind = "context" if in_ctx else ("code" if ext in CODE else "doc" if ext in DOCS else "data" if ext in DATA else "other")
        if rel in pinned:
            kind = "context"
        fid = node("file:" + rel, p.name, kind, path=rel, size=size)
        if not in_ctx:
            langs[LANG.get(ext, "Other")] += 1

        # folders
        parent = Path(rel).parent
        prev = fid
        while parent.as_posix() not in (".", ""):
            did = "dir:" + parent.as_posix()
            existed = did in nodes
            node(did, parent.name + "/", "folder", path=parent.as_posix())
            edge(did, prev, "contains")
            if existed:
                break
            prev, parent = did, parent.parent
        else:
            edge("project", prev, "contains")

        if size >= LARGE_FILE:
            large.append({"path": rel, "mb": round(size / 1048576, 1)})
        if p.name.startswith(".env") and not p.name.endswith((".example", ".sample", ".template")):
            ignored = _git(root, "check-ignore", "-q", rel) is not None
            envs.append({"path": rel, "ignored": ignored})

        text = None
        manifest = p.name.lower() in {
            "package.json", "pyproject.toml", "cargo.toml", "go.mod", "podfile", "gemfile",
        }
        if not in_ctx and size <= MAX_READ and (ext in TEXT or manifest):
            try:
                text = p.read_text(errors="ignore")
                capability_text[rel] = text
            except OSError:
                pass
        if ext not in TEXT or size > MAX_READ:
            file_terms[fid] = set(terms(p.stem))
            continue
        if text is None:
            try:
                text = p.read_text(errors="ignore")
            except OSError:
                continue
        n_lines = text.count("\n") + 1
        lines_total += n_lines
        nodes[fid]["lines"] = n_lines

        if not in_ctx and len(todos) < 80:
            for i, line in enumerate(text.splitlines(), 1):
                m = TODO_RE.search(line)
                if m and len(line) < 400:
                    todos.append({"path": rel, "line": i, "kind": m.group(1), "text": m.group(2).strip()[:160]})
                    if len(todos) >= 80:
                        break

        # links between files
        targets = set()
        if ext == ".py":
            targets |= {(t, "imports") for t in py_imports(root, p, text, known)}
        if ext in {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"}:
            for t in JS_IMPORT_RE.findall(text):
                r = resolve(root, p.parent, t, ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", "/index.ts", "/index.js"))
                if r:
                    targets.add((r, "imports"))
        if ext in {".md", ".txt", ".rst"}:
            for t in MD_LINK_RE.findall(text):
                if "://" not in t:
                    r = resolve(root, p.parent, t)
                    if r:
                        targets.add((r, "links_to"))
        if ext == ".html":
            for t in HTML_REF_RE.findall(text):
                r = resolve(root, p.parent, t)
                if r:
                    targets.add((r, "links_to"))
        for t, rel_kind in targets:
            edge(fid, "file:" + t, rel_kind)

        # words this file is "about": its name, its definitions, its headings
        words = terms(p.stem)
        if ext in CODE:
            words += [w for d in DEF_RE.findall(text) for w in terms(d)]
        else:
            words += [w for h in HEADING_RE.findall(text) for w in terms(h)]
        file_terms[fid] = set(words)

    # concepts: words shared by several files, but not by almost all of them
    df = Counter(w for ws in file_terms.values() for w in ws)
    n = max(1, len(file_terms))
    cap = max(3, int(0.4 * n))
    concepts = [w for w, c in df.most_common() if 2 <= c <= cap][:30]
    cset = set(concepts)
    for fid, ws in file_terms.items():
        for w in ws & cset:
            edge(fid, "concept:" + w, "mentions")
    for w in concepts:
        node("concept:" + w, w, "concept", files=df[w])

    # flow stages from project.json become graph nodes linked to their files.
    # Older projects store stages as strings, newer ones as objects.
    previous = None
    for index, raw_stage in enumerate(project.get("flow") or [], 1):
        stage = raw_stage if isinstance(raw_stage, dict) else {"title": str(raw_stage)}
        title = str(stage.get("title") or stage.get("name") or f"Step {index}").strip()
        sid = node("stage:" + str(index), title, "stage",
                   order=index, desc=str(stage.get("desc") or ""),
                   status=stage.get("status") or stage.get("state") or "")
        edge("project", sid, "contains")
        if previous:
            edge(previous, sid, "flows_to")
        previous = sid
        for raw_path in stage.get("files") or []:
            path = str(raw_path).strip().replace("\\", "/")
            if "file:" + path in nodes:
                edge(sid, "file:" + path, "implemented_by")

    # tasks and agents
    by_name = defaultdict(list)
    for rel in known:
        name = Path(rel).name.lower()
        if len(name) >= 5:
            by_name[name].append(rel)
    for t in tasks:
        tid = node("task:" + str(t.get("id")), f"{t.get('id')} {t.get('title', '')}", "task", status=t.get("status"))
        blob = " ".join([t.get("title", ""), t.get("notes", "")] + [s.get("text", "") for s in t.get("subtasks", [])]).lower()
        for name, rs in by_name.items():
            if name in blob:
                for r in rs:
                    edge(tid, "file:" + r, "about")
        for w in set(terms(blob)) & cset:
            edge(tid, "concept:" + w, "about")
        agents = {t.get("last_agent")} | {s.get("agent") for s in t.get("subtasks", [])}
        for a in filter(None, agents):
            edge(node("agent:" + a, a, "agent"), tid, "worked_on")
    for e in events:
        if e.get("agent") and e.get("task") and "task:" + e["task"] in nodes:
            edge(node("agent:" + e["agent"], e["agent"], "agent"), "task:" + e["task"], "worked_on")

    git = {"repo": False}
    if _git(root, "rev-parse", "--git-dir") is not None:
        git["repo"] = True
        git["branch"] = (_git(root, "branch", "--show-current") or "").strip() or "(detached)"
        status = (_git(root, "status", "--porcelain") or "").splitlines()
        git["changed"] = sum(1 for s in status if not s.startswith("??"))
        git["untracked"] = sum(1 for s in status if s.startswith("??"))
        last = (_git(root, "log", "-1", "--pretty=%h%x1f%s%x1f%aI") or "").strip().split("\x1f")
        git["last_commit"] = {"sha": last[0], "text": last[1], "ts": last[2]} if len(last) == 3 else None
        log = _git(root, "log", "-n300", "--name-only", "--pretty=format:%x1e%an%x1f%(trailers:key=Agent,valueonly,separator=%x2C)")
        changed = Counter()
        for rec in (log or "").split("\x1e"):
            head, _, names = rec.partition("\n")
            if "\x1f" not in head:
                continue
            author, agent = head.split("\x1f", 1)
            who = agent.strip() or author.strip()
            for f in names.splitlines():
                if f.strip() and "file:" + f.strip() in nodes:
                    changed[(who, f.strip())] += 1
        for (who, f), c in changed.items():
            edge(node("agent:" + who, who, "agent"), "file:" + f, "changed", c)

    # de-duplicate edges, summing weights
    merged = {}
    for e in edges:
        if e["s"] in nodes and e["t"] in nodes:
            k = (e["s"], e["t"], e["rel"])
            merged[k] = merged.get(k, 0) + e["w"]
    edges = [{"s": s, "t": t, "rel": r, "w": w} for (s, t, r), w in merged.items()]

    areas = find_areas(nodes, edges, read_names(root))
    excluded_capability_paths = set()
    try:
        excluded_capability_paths.add(Path(__file__).resolve().relative_to(root.resolve()).as_posix())
    except ValueError:
        pass
    capabilities = detect_capabilities(known, capability_text, excluded_capability_paths)

    health = {
        "files": len(files), "truncated": truncated, "lines": lines_total,
        "languages": dict(langs.most_common()), "todos": todos, "large_files": large,
        "env_files": envs, "git": git,
        "context_files": [rels[p] for p in ctx_files] + sorted(pinned),
    }
    return health, {
        "nodes": list(nodes.values()), "edges": edges, "clusters": areas,
        "capabilities": capabilities,
    }


# ---------------------------------------------------------------- areas (clusters)

FILE_TYPES = {"code", "doc", "data", "other", "context"}


def read_names(root):
    """Cluster names the local model chose earlier (.grc/clusters.json), keyed by anchor file."""
    try:
        return json.loads((Path(root) / ".grc" / "clusters.json").read_text())
    except (OSError, ValueError):
        return {}


def find_areas(nodes, edges, names, max_areas=12):
    """Group related files (imports, links, shared words) into named areas.

    If clusters.json already lists two or more areas with files, those are used
    as-is, since a hand-written map beats anything guessed from imports.
    """
    path_nodes = {node.get("path"): nid for nid, node in nodes.items() if node.get("path")}
    authored = []
    for saved in names.values():
        if not isinstance(saved, dict) or not saved.get("name") or not saved.get("files"):
            continue
        paths = [path for path in saved["files"] if path in path_nodes]
        if paths:
            authored.append((saved, paths))
    if len(authored) >= 2:
        out = []
        assigned = set()
        for saved, paths in authored[:max_areas]:
            paths = [path for path in paths if path not in assigned]
            if not paths:
                continue
            cid = f"c{len(out) + 1}"
            assigned.update(paths)
            for path in paths:
                nodes[path_nodes[path]]["cluster"] = cid
            out.append({
                "id": cid,
                "name": saved["name"],
                "named_by": saved.get("by", "project"),
                "anchor": saved.get("anchor") or paths[0],
                "size": len(paths),
                "files": paths[:12],
                "concepts": saved.get("concepts", [])[:6],
            })
        if len(out) >= 2:
            return out

    # fallback: label propagation (deterministic, no model).
    # saved names are reused when they overlap a found area.
    def own_note(n):  # GRC's own notes (brief, handoff) mention everything and would glue all areas together
        path = n.get("path") or ""
        return path.startswith(".grc/") and not path.startswith(".grc/context/")
    ids = sorted(i for i, n in nodes.items()
                 if (n["type"] in FILE_TYPES and not own_note(n)) or n["type"] == "concept")
    keep = set(ids)
    nbrs = {i: Counter() for i in ids}
    for e in edges:
        if e["rel"] in ("imports", "links_to", "mentions") and e["s"] in keep and e["t"] in keep:
            # a direct import/link says more than sharing a word; common words say least
            w = 3.0 if e["rel"] != "mentions" else 2.0 / max(1, nodes[e["t"]].get("files", 1))
            nbrs[e["s"]][e["t"]] += w
            nbrs[e["t"]][e["s"]] += w
    label = {i: i for i in ids}
    for _ in range(20):
        moved = 0
        for i in ids:
            if not nbrs[i]:
                continue
            votes = Counter()
            for j, w in nbrs[i].items():
                votes[label[j]] += w
            best = max(sorted(votes), key=lambda k: votes[k])
            if best != label[i]:
                label[i], moved = best, moved + 1
        if not moved:
            break
    groups = defaultdict(list)
    for i, lab in label.items():
        groups[lab].append(i)
    areas = []
    for members in groups.values():
        files = [m for m in members if nodes[m]["type"] in FILE_TYPES]
        if len(files) < 3 or not any(nodes[f]["type"] in ("code", "doc", "context") for f in files):
            continue  # pure data/log piles aren't an "area" of the project
        files.sort(key=lambda m: (-sum(nbrs[m].values()), m))
        concepts = sorted((m for m in members if nodes[m]["type"] == "concept"),
                          key=lambda m: -nodes[m].get("files", 0))
        areas.append({"files": files, "concepts": concepts})
    areas.sort(key=lambda a: -len(a["files"]))
    out = []
    for n, a in enumerate(areas[:max_areas], 1):
        cid = f"c{n}"
        anchor = nodes[a["files"][0]].get("path") or nodes[a["files"][0]]["label"]
        paths = [nodes[f].get("path") or nodes[f]["label"] for f in a["files"]]
        saved = names.get(anchor) or {}
        overlap = len(set(saved.get("files", [])) & set(paths)) / max(1, len(paths))
        if saved.get("name") and overlap >= 0.5:
            name, by = saved["name"], saved.get("by", "model")
        else:
            words = [nodes[c]["label"] for c in a["concepts"][:2]]
            folders = Counter(str(Path(p).parent) for p in paths)
            top, count = folders.most_common(1)[0]
            name = " · ".join(words) if words else (top if top != "." and count > len(paths) / 2 else Path(anchor).stem)
            by = ""
        for m in a["files"] + a["concepts"]:
            nodes[m]["cluster"] = cid
        out.append({"id": cid, "name": name, "named_by": by, "anchor": anchor, "size": len(paths),
                    "files": paths[:12], "concepts": [nodes[c]["label"] for c in a["concepts"][:6]]})
    return out
