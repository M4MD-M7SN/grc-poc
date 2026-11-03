#!/bin/sh
# Install the dependency-free GRC CLI and MCP server.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PREFIX=${HOME}/.local
INSTALL_APP=0

usage() {
    printf '%s\n' \
        "Usage: ./scripts/install.sh [--prefix PATH] [--app]" \
        "" \
        "Installs:" \
        "  PREFIX/bin/grc       GRC command-line interface" \
        "  PREFIX/bin/grc-mcp   GRC MCP stdio server" \
        "  PREFIX/share/grc     Private runtime files" \
        "" \
        "Defaults to PREFIX=~/.local. --app also builds ~/Applications/GRC.app."
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --prefix)
            [ "$#" -ge 2 ] || { printf '%s\n' "error: --prefix needs a path" >&2; exit 2; }
            PREFIX=$2
            shift 2
            ;;
        --app)
            INSTALL_APP=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'error: unknown option: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

case "$PREFIX" in
    ""|/) printf '%s\n' "error: --prefix must not be empty or /" >&2; exit 2 ;;
    /*) ;;
    *) PREFIX=$(pwd)/$PREFIX ;;
esac

PYTHON=${PYTHON:-python3}
PYTHON_PATH=$(command -v "$PYTHON" 2>/dev/null || true)
[ -n "$PYTHON_PATH" ] || { printf '%s\n' "error: Python 3.9 or newer is required" >&2; exit 1; }
case "$PYTHON_PATH" in
    /*) ;;
    *) PYTHON_PATH=$(CDPATH= cd -- "$(dirname -- "$PYTHON_PATH")" && pwd)/$(basename -- "$PYTHON_PATH") ;;
esac
"$PYTHON_PATH" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' || {
    printf '%s\n' "error: Python 3.9 or newer is required" >&2
    exit 1
}

MANIFEST=$ROOT/packaging/runtime-files.txt
[ -f "$MANIFEST" ] || { printf '%s\n' "error: missing runtime manifest" >&2; exit 1; }

STAGE=$(mktemp -d "${TMPDIR:-/tmp}/grc-install.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT HUP INT TERM
STAGED_RUNTIME=$STAGE/grc
mkdir -p "$STAGED_RUNTIME"

while IFS= read -r RELATIVE || [ -n "$RELATIVE" ]; do
    case "$RELATIVE" in
        ''|'#'*) continue ;;
        /*|*../*|../*|*/..) printf 'error: unsafe manifest path: %s\n' "$RELATIVE" >&2; exit 1 ;;
    esac
    SOURCE=$ROOT/$RELATIVE
    [ -f "$SOURCE" ] || { printf 'error: missing runtime file: %s\n' "$RELATIVE" >&2; exit 1; }
    mkdir -p "$STAGED_RUNTIME/$(dirname "$RELATIVE")"
    cp "$SOURCE" "$STAGED_RUNTIME/$RELATIVE"
done < "$MANIFEST"

BIN=$PREFIX/bin
SHARE=$PREFIX/share
DEST=$SHARE/grc
mkdir -p "$BIN" "$SHARE"
rm -rf "$DEST.new" "$DEST.previous"
mv "$STAGED_RUNTIME" "$DEST.new"
if [ -e "$DEST" ]; then mv "$DEST" "$DEST.previous"; fi
mv "$DEST.new" "$DEST"
rm -rf "$DEST.previous"
printf '%s\n' "$PYTHON_PATH" > "$DEST/python-command"

cat > "$BIN/grc" <<'EOF'
#!/bin/sh
BASE=$(CDPATH= cd -- "$(dirname -- "$0")/../share/grc" && pwd)
IFS= read -r PYTHON < "$BASE/python-command"
[ -n "$PYTHON" ] || { printf '%s\n' "error: missing installed Python command" >&2; exit 1; }
exec "$PYTHON" "$BASE/grc.py" "$@"
EOF
cat > "$BIN/grc-mcp" <<'EOF'
#!/bin/sh
BASE=$(CDPATH= cd -- "$(dirname -- "$0")/../share/grc" && pwd)
IFS= read -r PYTHON < "$BASE/python-command"
[ -n "$PYTHON" ] || { printf '%s\n' "error: missing installed Python command" >&2; exit 1; }
exec "$PYTHON" "$BASE/mcp_server.py" "$@"
EOF
cat > "$BIN/grc-mcp-project-lead" <<'EOF'
#!/bin/sh
BIN=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
GRC_MCP_ROLE=project-lead
export GRC_MCP_ROLE
exec "$BIN/grc-mcp" "$@"
EOF
chmod 755 "$BIN/grc" "$BIN/grc-mcp" "$BIN/grc-mcp-project-lead"

if [ "$INSTALL_APP" -eq 1 ]; then
    command -v swiftc >/dev/null 2>&1 || {
        printf '%s\n' "error: --app requires Xcode Command Line Tools (xcode-select --install)" >&2
        exit 1
    }
    "$ROOT/app/build.sh"
fi

printf '%s\n' "GRC installed successfully."
printf '  CLI: %s\n' "$BIN/grc"
printf '  MCP: %s\n' "$BIN/grc-mcp"
printf '  Project-lead MCP (privileged): %s\n' "$BIN/grc-mcp-project-lead"
case ":${PATH:-}:" in
    *":$BIN:"*) ;;
    *) printf '  Add to PATH: export PATH="%s:$PATH"\n' "$BIN" ;;
esac
