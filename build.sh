#!/usr/bin/env bash
# Builds "Token Meter.app" from source. Needs only the Xcode Command Line
# Tools (xcode-select --install) — no Xcode, no Apple Developer account.
#
#   ./build.sh                     universal (arm64 + x86_64) app in build/
#   ./build.sh --native            this Mac's architecture only (faster)
#   ./build.sh --dmg               also package build/TokenMeter.dmg
#   ./build.sh --install           also copy it to ~/Applications and launch it
#   ./build.sh --import-db PATH    seed the app's database from the legacy
#                                  Python dashboard's usage.sqlite3 (only if the
#                                  app has no database yet), keeping limit history
#
# VERSION=1.2.0 ./build.sh --dmg   sets the version (CI passes the release tag).
set -euo pipefail

CALLER_DIR="$(pwd)"
cd "$(dirname "$0")"
REPO_ROOT="$(pwd)"
PACKAGE="$REPO_ROOT/macpapp"
APP_NAME="Token Meter"
EXECUTABLE="TokenMeter"
BUNDLE_ID="${BUNDLE_ID:-io.github.avaakash.token-meter}"
VERSION="${VERSION:-1.1.0}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/Applications}"
APP_DB_DIR="$HOME/Library/Application Support/TokenMeter"

archs=(--arch arm64 --arch x86_64)
expected_archs="x86_64 arm64"
install=false
dmg=false
import_db=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --native) archs=(); expected_archs="$(uname -m)" ;;
    --install) install=true ;;
    --dmg) dmg=true ;;
    --import-db)
      [[ $# -ge 2 ]] || { echo "--import-db needs a path" >&2; exit 2; }
      # Relative to where the script was run from.
      case "$2" in /*) import_db="$2" ;; *) import_db="$CALLER_DIR/$2" ;; esac
      # sqlite3 silently creates (and "backs up") a missing file as an empty DB.
      [[ -f "$import_db" ]] || { echo "no database at $import_db" >&2; exit 2; }
      shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

echo "Building $APP_NAME $VERSION ($expected_archs)..."
swift build --package-path "$PACKAGE" -c release --product "$EXECUTABLE" ${archs[@]+"${archs[@]}"}
bin_dir="$(swift build --package-path "$PACKAGE" -c release --product "$EXECUTABLE" ${archs[@]+"${archs[@]}"} --show-bin-path)"

actual_archs="$(lipo -archs "$bin_dir/$EXECUTABLE")"
for arch in $expected_archs; do
  if [[ " $actual_archs " != *" $arch "* ]]; then
    echo "binary is missing $arch (has: $actual_archs)" >&2
    exit 1
  fi
done

app="build/$APP_NAME.app"
rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources/web"
cp "$bin_dir/$EXECUTABLE" "$app/Contents/MacOS/"
cp "$PACKAGE"/Resources/web/* "$app/Contents/Resources/web/"
# Regenerate with: swift scripts/make-icon.swift
cp "$PACKAGE/Resources/AppIcon.icns" "$app/Contents/Resources/"
cp "$PACKAGE"/Resources/pricing_*.json "$app/Contents/Resources/"

cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>$APP_NAME</string>
    <key>CFBundleDisplayName</key><string>$APP_NAME</string>
    <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
    <key>CFBundleExecutable</key><string>$EXECUTABLE</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundleVersion</key><string>$VERSION</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <key>LSUIElement</key><true/>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# Ad-hoc signature: required for arm64 code and for the login item. Enough for
# a locally built app; a downloaded one still needs "Open Anyway" once.
codesign --force --sign - "$app"
echo "Built $app ($actual_archs)"

if $dmg; then
  image="build/TokenMeter.dmg"
  staging="$(mktemp -d)"
  cp -R "$app" "$staging/"
  ln -s /Applications "$staging/Applications"
  rm -f "$image"
  hdiutil create -volname "$APP_NAME" -srcfolder "$staging" -ov -format UDZO "$image" >/dev/null
  rm -rf "$staging"
  echo "Packaged $image"
fi

if [[ -n "$import_db" ]]; then
  if [[ -e "$APP_DB_DIR/usage.sqlite3" ]]; then
    echo "Skipping import: $APP_DB_DIR/usage.sqlite3 already exists."
  else
    mkdir -p "$APP_DB_DIR"
    # .backup takes a consistent copy even while the Python server is writing.
    sqlite3 "$import_db" ".backup '$APP_DB_DIR/usage.sqlite3'"
    echo "Imported $import_db into $APP_DB_DIR"
  fi
fi

if $install; then
  mkdir -p "$INSTALL_DIR"
  pkill -x "$EXECUTABLE" 2>/dev/null || true
  for _ in $(seq 1 50); do pgrep -x "$EXECUTABLE" >/dev/null || break; sleep 0.1; done
  rm -rf "$INSTALL_DIR/$APP_NAME.app"
  cp -R "$app" "$INSTALL_DIR/"
  open "$INSTALL_DIR/$APP_NAME.app"
  echo "Installed to $INSTALL_DIR/$APP_NAME.app — look for the gauge in the menu bar."
fi
