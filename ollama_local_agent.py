#!/usr/bin/env python3
"""Minimal Ollama tool loop for a local coding model inside a GRC worktree."""
from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class LocalAgentError(RuntimeError):
    pass


class WorkspaceTools:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _path(self, value: str, write: bool = False) -> Path:
        raw = Path(value)
        if raw.is_absolute() or ".." in raw.parts:
            raise LocalAgentError("Path must stay inside the worktree")
        path = (self.root / raw).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise LocalAgentError("Path escapes the worktree") from exc
        relative = path.relative_to(self.root)
        if relative.parts and relative.parts[0] in {".git", ".grc"}:
            raise LocalAgentError("Local agents cannot modify GRC or Git metadata")
        if write and (path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != self.root)):
            raise LocalAgentError("Refusing to write through a symlink")
        return path

    def list_files(self, pattern: str = "*") -> str:
        rows = []
        for path in self.root.rglob("*"):
            if len(rows) >= 1000:
                break
            if not path.is_file() or ".git" in path.parts or ".grc" in path.parts:
                continue
            relative = str(path.relative_to(self.root))
            if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(path.name, pattern):
                rows.append(relative)
        return "\n".join(rows)

    def read_file(self, path: str, start_line: int = 1, end_line: int = 400) -> str:
        target = self._path(path)
        if not target.is_file() or target.stat().st_size > 1_000_000:
            raise LocalAgentError("File is missing or exceeds 1 MB")
        lines = target.read_text(errors="replace").splitlines()
        start = max(1, int(start_line))
        end = min(len(lines), max(start, int(end_line)), start + 999)
        return "\n".join(f"{number}|{lines[number - 1]}" for number in range(start, end + 1))

    def search_files(self, query: str, path: str = ".") -> str:
        base = self._path(path)
        files = [base] if base.is_file() else base.rglob("*")
        rows = []
        for item in files:
            if len(rows) >= 200:
                break
            if not item.is_file() or ".git" in item.parts or ".grc" in item.parts or item.stat().st_size > 1_000_000:
                continue
            for number, line in enumerate(item.read_text(errors="replace").splitlines(), 1):
                if query.lower() in line.lower():
                    rows.append(f"{item.relative_to(self.root)}:{number}:{line[:500]}")
                    if len(rows) >= 200:
                        break
        return "\n".join(rows)

    def write_file(self, path: str, content: str) -> str:
        target = self._path(path, write=True)
        if target.exists():
            raise LocalAgentError("File already exists; use replace_in_file for a focused edit")
        payload = content.encode()
        if len(payload) > 1_000_000:
            raise LocalAgentError("Write exceeds 1 MB")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return f"wrote {len(payload)} bytes to {target.relative_to(self.root)}"

    def replace_in_file(self, path: str, old_string: str, new_string: str) -> str:
        target = self._path(path, write=True)
        if not target.is_file() or target.stat().st_size > 1_000_000:
            raise LocalAgentError("File is missing or exceeds 1 MB")
        content = target.read_text(errors="strict")
        count = content.count(old_string)
        if count != 1:
            raise LocalAgentError(f"old_string must occur exactly once; found {count}")
        updated = content.replace(old_string, new_string, 1)
        if len(updated.encode()) > 1_000_000:
            raise LocalAgentError("Updated file exceeds 1 MB")
        target.write_text(updated)
        return f"replaced one occurrence in {target.relative_to(self.root)}"

    def check_python_syntax(self, path: str) -> str:
        target = self._path(path)
        if not target.is_file() or target.stat().st_size > 1_000_000:
            raise LocalAgentError("File is missing or exceeds 1 MB")
        try:
            ast.parse(target.read_text(errors="strict"), filename=str(target))
        except (SyntaxError, UnicodeError) as exc:
            raise LocalAgentError(f"invalid Python syntax in {target.relative_to(self.root)}: {exc}") from exc
        return f"valid Python syntax: {target.relative_to(self.root)}"

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        if name not in {"list_files", "read_file", "search_files", "write_file", "replace_in_file", "check_python_syntax"}:
            raise LocalAgentError(f"Unknown tool: {name}")
        return str(getattr(self, name)(**arguments))


TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "list_files", "description": "List worktree files by glob pattern.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a bounded line range from a text file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "search_files", "description": "Search literal text in worktree files.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Create a new source file. This refuses to overwrite an existing file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "replace_in_file", "description": "Make a focused edit by replacing one exact, uniquely occurring string in an existing file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}}, "required": ["path", "old_string", "new_string"]}}},
    {"type": "function", "function": {"name": "check_python_syntax", "description": "Parse one Python file without executing it. Call this for every Python file you change before finishing.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}
]


def chat(endpoint: str, model: str, prompt: str, root: Path, max_turns: int = 30, request_timeout: int = 600) -> str:
    tools = WorkspaceTools(root)
    messages = [{"role": "system", "content": "You are a focused local coding agent. Inspect only what is needed and make the smallest edits that satisfy the assigned acceptance criteria. Preserve all existing code and tests. Use replace_in_file for existing files and write_file only to create a genuinely new file. After changing any Python file, you MUST call check_python_syntax on it and fix every reported error before finishing. Read changed code back when needed to verify the exact edit. Do not modify .git or .grc."}, {"role": "user", "content": prompt}]
    for _ in range(max_turns):
        body = json.dumps({"model": model, "messages": messages, "tools": TOOL_SCHEMAS, "stream": False, "options": {"temperature": 0.1}}).encode()
        request = urllib.request.Request(endpoint.rstrip("/") + "/api/chat", data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                payload = json.loads(response.read())
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise LocalAgentError(f"Ollama request failed: {exc}") from exc
        message = payload.get("message") or {}
        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            return str(message.get("content", ""))
        for call in calls:
            function = call.get("function") or {}
            name = str(function.get("name", ""))
            arguments = function.get("arguments") or {}
            try:
                result = tools.call(name, arguments)
            except Exception as exc:
                result = f"ERROR: {exc}"
            messages.append({"role": "tool", "tool_name": name, "content": result[:100000]})
    raise LocalAgentError(f"Local agent exceeded {max_turns} turns")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--endpoint", default=os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"))
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--request-timeout", type=int, default=600)
    args = parser.parse_args()
    prompt = Path(args.prompt_file).read_text()
    print(chat(args.endpoint, args.model, prompt, Path.cwd(), args.max_turns, args.request_timeout))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
