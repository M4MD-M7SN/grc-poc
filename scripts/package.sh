#!/bin/sh
# Build portable source and app archives from a clean committed tree.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
OUT=${1:-$ROOT/dist}
case "$OUT" in /*) ;; *) OUT=$ROOT/$OUT ;; esac

[ -f VERSION ] || { printf '%s\n' "error: VERSION is missing" >&2; exit 1; }
VERSION=$(tr -d '[:space:]' < VERSION)
[ -n "$VERSION" ] || { printf '%s\n' "error: VERSION is empty" >&2; exit 1; }
case "$VERSION" in
  *[!0-9A-Za-z._-]*) printf '%s\n' "error: VERSION contains unsafe characters" >&2; exit 1 ;;
esac

git diff --quiet && git diff --cached --quiet || {
  printf '%s\n' "error: commit changes before packaging" >&2
  exit 1
}

mkdir -p "$OUT"
SOURCE=$OUT/grc-$VERSION-source.zip
APP_ARCHIVE=$OUT/GRC-$VERSION-macOS-$(uname -m).zip
CHECKSUMS=$OUT/SHA256SUMS
rm -f "$SOURCE" "$APP_ARCHIVE" "$CHECKSUMS"

git archive --format=zip --prefix="grc-$VERSION/" -o "$SOURCE" HEAD

if [ "$(uname -s)" = "Darwin" ] && command -v swiftc >/dev/null 2>&1; then
  ./app/build.sh --output-only
  ditto -c -k --sequesterRsrc --keepParent build/GRC.app "$APP_ARCHIVE"
fi

(
  cd "$OUT"
  for FILE in "$(basename "$SOURCE")" "$(basename "$APP_ARCHIVE")"; do
    [ -f "$FILE" ] && shasum -a 256 "$FILE"
  done
) > "$CHECKSUMS"

printf 'Release artifacts: %s\n' "$OUT"
cat "$CHECKSUMS"
