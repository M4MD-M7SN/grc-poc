#!/bin/sh
# Run every dependency-free GRC verification check.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

PYTHON=${PYTHON:-python3}
"$PYTHON" -m unittest discover -s tests -v
"$PYTHON" -m py_compile grc.py scan.py genie.py limits.py mcp_server.py identity.py task_sources.py workflow.py agent_runner.py agent_cli.py ollama_local_agent.py
sh -n scripts/install.sh scripts/uninstall.sh scripts/doctor.sh scripts/test.sh scripts/package.sh
zsh -n app/build.sh

if command -v swiftc >/dev/null 2>&1; then
  TMP=$(mktemp -d "${TMPDIR:-/tmp}/grc-swift-tests.XXXXXX")
  trap 'rm -rf "$TMP"' EXIT HUP INT TERM
  swiftc -parse-as-library app/ConnectionLogic.swift app/ConnectionLogicTests.swift -o "$TMP/connection-tests"
  "$TMP/connection-tests"
else
  printf '%s\n' "Swift tests skipped: install Xcode Command Line Tools to test GRC.app"
fi
