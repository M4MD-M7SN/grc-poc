#!/bin/zsh
# Build GRC.app from source. Requires only Xcode Command Line Tools.
set -euo pipefail

HERE=${0:A:h}
ROOT=${HERE:h}
OUT=$ROOT/build
APP=$OUT/GRC.app
RES=$APP/Contents/Resources
INSTALL_APP=1
INSTALL_DIR=${HOME}/Applications

usage() {
  cat <<'EOF'
Usage: ./app/build.sh [--output-only] [--install-dir PATH]

Builds build/GRC.app for the current Mac architecture. By default the app is
also copied to ~/Applications. --output-only leaves it only under build/.
EOF
}

while (( $# > 0 )); do
  case "$1" in
    --output-only)
      INSTALL_APP=0
      shift
      ;;
    --install-dir)
      (( $# >= 2 )) || { print -u2 "error: --install-dir needs a path"; exit 2; }
      INSTALL_DIR=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      print -u2 "error: unknown option: $1"
      usage >&2
      exit 2
      ;;
  esac
done

command -v swiftc >/dev/null || { print -u2 "error: install Xcode Command Line Tools with: xcode-select --install"; exit 1; }
ARCH=${GRC_ARCH:-$(uname -m)}
case "$ARCH" in
  arm64|x86_64) ;;
  *) print -u2 "error: unsupported architecture: $ARCH"; exit 1 ;;
esac
TARGET=${ARCH}-apple-macos14.0
VERSION=$(tr -d '[:space:]' < "$ROOT/VERSION")
case "$VERSION" in
  ''|*[!0-9A-Za-z._-]*) print -u2 "error: VERSION is empty or contains unsafe characters"; exit 1 ;;
esac

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$RES" "$OUT/icon.iconset"

swiftc -O -parse-as-library -swift-version 5 -target "$TARGET" \
  -o "$APP/Contents/MacOS/GRC" "$HERE/ConnectionLogic.swift" "$HERE/GRC.swift"

MANIFEST=$ROOT/packaging/runtime-files.txt
[[ -f "$MANIFEST" ]] || { print -u2 "error: missing runtime manifest"; exit 1; }
while IFS= read -r RELATIVE || [[ -n "$RELATIVE" ]]; do
  [[ -z "$RELATIVE" || "$RELATIVE" == \#* ]] && continue
  case "$RELATIVE" in
    /*|*../*|../*|*/..) print -u2 "error: unsafe manifest path: $RELATIVE"; exit 1 ;;
  esac
  SOURCE=$ROOT/$RELATIVE
  [[ -f "$SOURCE" ]] || { print -u2 "error: missing runtime file: $RELATIVE"; exit 1; }
  mkdir -p "$RES/${RELATIVE:h}"
  cp "$SOURCE" "$RES/$RELATIVE"
done < "$MANIFEST"

swiftc -O -o "$OUT/make-icon" "$HERE/make-icon.swift" 2>/dev/null
"$OUT/make-icon" "$OUT/icon-1024.png"
for s in 16 32 128 256 512; do
  sips -z $s $s "$OUT/icon-1024.png" --out "$OUT/icon.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$OUT/icon-1024.png" --out "$OUT/icon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$OUT/icon.iconset" -o "$RES/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>GRC</string>
  <key>CFBundleDisplayName</key><string>GRC</string>
  <key>CFBundleIdentifier</key><string>local.grc.app</string>
  <key>CFBundleExecutable</key><string>GRC</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundleVersion</key><string>13</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSApplicationCategoryType</key><string>public.app-category.developer-tools</string>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST

codesign --force --deep --sign - "$APP"
print "Built $APP ($ARCH, macOS 14+)"

if (( INSTALL_APP )); then
  mkdir -p "$INSTALL_DIR"
  rm -rf "$INSTALL_DIR/GRC.app"
  cp -R "$APP" "$INSTALL_DIR/GRC.app"
  print "Installed $INSTALL_DIR/GRC.app"
fi
