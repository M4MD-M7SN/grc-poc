# MCP tools

`grc-mcp` speaks MCP over stdio (newline-delimited JSON-RPC 2.0). Every tool takes an optional `project_dir`. If it's left out, the server uses the current folder or the nearest parent that has a `.grc/` folder. Tools that write also take `agent`, the caller's name in `tool/model` form.

## Reading

| Tool | What it returns |
|---|---|
| `grc_context` | Project briefing: resume card, open tasks, recent events, health, key files, summary. `focus` narrows it to a task id, file or topic. `format: "capsule"` returns a size-capped JSON version with a fingerprint, and `persist: true` writes it to `.grc/context-capsule.json`. |
| `grc_list_tasks` | All tasks with status, percent done and steps. |
| `grc_task_sheet` | Tasks in order, with which ones are ready and which are waiting on dependencies. |
| `grc_next_task` | The first ready task. Doesn't claim it. |
| `grc_connections` | Tools, MCP servers and APIs registered in GRC.app that every agent can use. Names and metadata only, never keys. |

## Writing

| Tool | What it does |
|---|---|
| `grc_add_task` | Add a task, optionally with steps and notes. |
| `grc_update_task` | Change status (`todo`, `doing`, `done`, `blocked`), tick steps by number, or add a note. |
| `grc_claim_task` | Claim a task with a time-limited lease, so two agents don't work on it at once. |
| `grc_set_resume` | Update the resume card (`now`, `next`, `blocked`). |
| `grc_log` | Add an entry to the timeline. |
| `grc_handoff` | Write `.grc/handoff.md` with what's done, what's left, the next step and the files that matter. |
| `grc_init` | Start tracking a folder. |

## Audit / fix / review loop

These run commands that you set up in `.grc/project.json` under `workflow.runners`. GRC never makes up a command. If a runner isn't configured, the tool returns an error.

| Tool | What it does |
|---|---|
| `grc_run_audit` | Runs the audit command (usually a stronger hosted model) to find problems and turn them into tasks. |
| `grc_run_local` | Runs the local implementer on one task inside its own git worktree, then runs the configured tests. |
| `grc_run_review` | Runs a separate reviewer. The work is merged only if the review accepts it. The implementer can't review its own work. |
| `grc_recover_runs` | Releases claims left behind by runs that crashed. |
| `grc_plan_task` | Adds a structured task with inputs, outputs, owned paths, dependencies and acceptance criteria. Only works when the server was started with `GRC_MCP_ROLE=project-lead`. |

Example runner config:

```json
{
  "workflow": {
    "require_review": true,
    "implementer_may_self_review": false,
    "roles": {
      "superior_auditors": ["superior/claude"],
      "superior_reviewers": ["superior/codex"],
      "local_implementers": ["local/qwen"],
      "project_leads": ["project-lead/claude"]
    },
    "runners": {
      "audit":  { "agent": "superior/claude", "command": ["claude", "-p", "{prompt}"] },
      "local":  { "agent": "local/qwen",
                  "command": ["python3", "{runner_dir}/ollama_local_agent.py", "--model", "qwen3:8b",
                              "--prompt-file", "{prompt_file}", "--max-turns", "30"],
                  "tests": [["python3", "-m", "unittest", "discover", "-s", "tests"]] },
      "review": { "agent": "superior/codex", "command": ["codex", "exec", "{prompt}"] }
    }
  }
}
```

Commands are argv arrays and run without a shell.
