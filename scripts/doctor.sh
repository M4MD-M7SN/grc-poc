#!/bin/sh
# Check the minimum and optional dependencies without changing the system.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ERRORS=0

printf '%s\n' "GRC doctor"
printf '%s\n' "----------"

if [ "$(uname -s)" = "Darwin" ]; then
  MACOS_VERSION=$(sw_vers -productVersion)
  MACOS_MAJOR=${MACOS_VERSION%%.*}
  if [ "$MACOS_MAJOR" -ge 14 ] 2>/dev/null; then
    printf 'required  macOS:       OK (%s %s)\n' "$MACOS_VERSION" "$(uname -m)"
  else
    printf 'required  macOS:       UNSUPPORTED (%s; need macOS 14+)\n' "$MACOS_VERSION"
    ERRORS=1
  fi
else
  printf 'required  macOS:       MISSING (GRC CLI may work, but this release targets macOS)\n'
  ERRORS=1
fi

PYTHON=${PYTHON:-python3}
if command -v "$PYTHON" >/dev/null 2>&1 && "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
  printf 'required  Python:      OK (%s)\n' "$("$PYTHON" -c 'import platform; print(platform.python_version())')"
else
  printf '%s\n' "required  Python:      MISSING (need Python 3.9+)"
  ERRORS=1
fi

if [ -f "$ROOT/packaging/runtime-files.txt" ]; then
  MISSING=0
  while IFS= read -r RELATIVE || [ -n "$RELATIVE" ]; do
    case "$RELATIVE" in ''|'#'*) continue ;; esac
    [ -f "$ROOT/$RELATIVE" ] || { printf 'missing runtime file: %s\n' "$RELATIVE"; MISSING=1; }
  done < "$ROOT/packaging/runtime-files.txt"
  if [ "$MISSING" -eq 0 ]; then printf '%s\n' "required  Runtime:     OK"; else ERRORS=1; fi
else
  printf '%s\n' "required  Runtime:     MISSING manifest"
  ERRORS=1
fi

for ITEM in "swiftc:desktop app" "ollama:local AI fallback" "hermes:Hermes integration" "git:version control"; do
  COMMAND=${ITEM%%:*}
  LABEL=${ITEM#*:}
  if command -v "$COMMAND" >/dev/null 2>&1; then
    printf 'optional  %-12s OK\n' "$LABEL:"
  else
    printf 'optional  %-12s not installed\n' "$LABEL:"
  fi
done

if [ "$ERRORS" -ne 0 ]; then
  printf '%s\n' "Doctor found missing required components."
  exit 1
fi
printf '%s\n' "Minimum GRC requirements are satisfied."
