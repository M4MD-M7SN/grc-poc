#!/bin/sh
set -eu
PREFIX=${HOME}/.local
REMOVE_APP=0

usage() {
    printf '%s\n' "Usage: ./scripts/uninstall.sh [--prefix PATH] [--app]"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --prefix)
            [ "$#" -ge 2 ] || { printf '%s\n' "error: --prefix needs a path" >&2; exit 2; }
            PREFIX=$2
            shift 2
            ;;
        --app)
            REMOVE_APP=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'error: unknown option: %s\n' "$1" >&2
            exit 2
            ;;
    esac
done

case "$PREFIX" in
    ""|/) printf '%s\n' "error: --prefix must not be empty or /" >&2; exit 2 ;;
    /*) ;;
    *) PREFIX=$(pwd)/$PREFIX ;;
esac

rm -f "$PREFIX/bin/grc" "$PREFIX/bin/grc-mcp" "$PREFIX/bin/grc-mcp-project-lead"
rm -rf "$PREFIX/share/grc"
if [ "$REMOVE_APP" -eq 1 ]; then
    rm -rf "$HOME/Applications/GRC.app"
fi
printf 'Removed GRC from %s\n' "$PREFIX"
