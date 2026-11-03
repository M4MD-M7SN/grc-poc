# Connecting an AI agent to GRC

GRC doesn't ship config for any particular agent. Every tool keeps its MCP settings somewhere different, and new tools keep appearing, so the setup is left open: you tell your agent to configure itself.

## 1. Install GRC

```bash
./scripts/install.sh
```

This puts `grc` and `grc-mcp` in `~/.local/bin`. Check with `which grc-mcp`.

## 2. Paste this into your agent

Open a terminal in a project you want tracked, start your agent (Claude Code, Codex, Cursor, Gemini CLI, Hermes, anything that supports MCP), and paste:

```text
Set yourself up to use GRC in this project.

1. Register an MCP server named "grc" in your own configuration.
   Transport: stdio. Command: the absolute path printed by `which grc-mcp`.
   No arguments, no environment variables. Use user/global scope if you
   have one so it works in every project. If your config is a file,
   back it up before editing.
2. If this folder has no .grc/ directory yet, run `grc init .`
3. Restart or reload MCP if you need to, then call grc_context and show
   me the result so I can see the connection works.
4. Save these rules wherever you keep standing instructions for this
   project (CLAUDE.md, AGENTS.md, .cursorrules, or your memory):
   - Call grc_context before starting work.
   - Keep tasks current with grc_update_task, add new work with grc_add_task.
   - Log what you changed with grc_log.
   - Call grc_handoff before you stop, and as soon as you think you're
     close to a usage limit.
   - Identify yourself as tool/model, e.g. claude-code/opus-5.5 or
     codex/gpt-5.6.
```

That's it. The agent knows where its own config lives better than any install script would.

## 3. Check it

Ask the agent "what does GRC say about this project?". It should answer from `grc_context` without reading the repo. On the dashboard, the agent's name shows up in the timeline after its first `grc_log`.

## Manual setup

If you'd rather edit the config yourself:

| Agent | Command or file |
|---|---|
| Claude Code | `claude mcp add grc --scope user -- ~/.local/bin/grc-mcp` |
| Codex | `codex mcp add grc -- ~/.local/bin/grc-mcp` |
| Cursor | `~/.cursor/mcp.json` |
| Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Hermes | `mcp_servers:` in `~/.hermes/config.yaml` |
| Anything else | the client's MCP settings |

JSON-style clients:

```json
{
  "mcpServers": {
    "grc": { "command": "/Users/you/.local/bin/grc-mcp" }
  }
}
```

YAML-style clients (Hermes):

```yaml
mcp_servers:
  grc:
    command: /Users/you/.local/bin/grc-mcp
    enabled: true
```

## Project lead role

One tool, `grc_plan_task`, is only available when the server starts with `GRC_MCP_ROLE=project-lead`. The installer adds a wrapper for it:

```bash
claude mcp add grc-lead --scope user -- ~/.local/bin/grc-mcp-project-lead
```

Give this to the agent you want planning work. Other agents can't give themselves the role, because it's set when the server is launched, not by anything the agent sends.

## Commit attribution (optional)

```bash
grc hooks .
```

This installs a `post-commit` hook that rebuilds the dashboard and a `prepare-commit-msg` hook that adds `Agent: $GRC_AGENT` to commit messages. Set `GRC_AGENT` in each agent's environment (for example `GRC_AGENT=claude-code/opus-5.5`). Without it, GRC falls back to the `Co-Authored-By` line that Claude Code and Codex already add. The hooks won't overwrite hooks you already have, and they're skipped if `core.hooksPath` is set.

## Local model (optional)

Install [Ollama](https://ollama.com) and pull a model with tool calling:

```bash
ollama pull qwen3:8b
```

Genie uses it for the dashboard summary, the chat panel in the app, and for continuing work offline after another agent hits its limit. Nothing runs offline unless you say yes.
