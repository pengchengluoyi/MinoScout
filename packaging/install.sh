#!/usr/bin/env bash
# Install Mino Scout next to config.json and register a daemon.
# Frozen zip: copy onedir, no Python required.
# Source zip (dev): venv + pip, needs Python 3.12+.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"

if [[ "$(uname -s)" == "Darwin" ]]; then
  PREFIX="${MINO_SCOUT_HOME:-$HOME/Library/Application Support/MinoScout}"
  LOG_DIR="$HOME/Library/Logs/MinoScout"
else
  PREFIX="${MINO_SCOUT_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/minoscout}"
  LOG_DIR="$PREFIX/logs"
fi

BIN=""

echo "Mino Scout → $PREFIX"
mkdir -p "$PREFIX" "$LOG_DIR"

install_frozen() {
  local src="$1"
  local dest="$PREFIX/bin"
  rm -rf "$dest"
  mkdir -p "$dest"
  cp -R "$src/." "$dest/"
  if [[ -x "$dest/mino-scout" ]]; then
    BIN="$dest/mino-scout"
  elif [[ -f "$dest/mino-scout" ]]; then
    chmod +x "$dest/mino-scout"
    BIN="$dest/mino-scout"
  else
    echo "frozen payload missing mino-scout in $src" >&2
    exit 1
  fi
  # GitHub zip often drops +x on nested Playwright/adb binaries.
  if [[ "$(uname -s)" == "Darwin" ]]; then
    xattr -cr "$dest" >/dev/null 2>&1 || true
  fi
  find "$dest" -type f -exec chmod u+x {} +
}

install_source() {
  local app="$PREFIX/app"
  local venv="$PREFIX/venv"
  if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
    echo "This zip is a source payload and needs Python 3.12+. Prefer the frozen GitHub Release." >&2
    exit 1
  fi
  mkdir -p "$app"
  rm -rf "$app/mino_scout"
  cp -R "$ROOT/mino_scout" "$app/mino_scout"
  cp "$ROOT/pyproject.toml" "$app/pyproject.toml"
  python3 -m venv "$venv"
  "$venv/bin/python" -m pip install -U pip
  "$venv/bin/python" -m pip install "$app"
  BIN="$venv/bin/mino-scout"
  if [[ ! -x "$BIN" ]]; then
    echo "install failed: $BIN not created" >&2
    exit 1
  fi
}

if [[ -x "$ROOT/mino-scout/mino-scout" || -f "$ROOT/mino-scout/mino-scout" ]]; then
  install_frozen "$ROOT/mino-scout"
elif [[ -x "$ROOT/mino-scout" && ! -d "$ROOT/mino-scout" ]]; then
  mkdir -p "$PREFIX/bin"
  cp "$ROOT/mino-scout" "$PREFIX/bin/mino-scout"
  chmod +x "$PREFIX/bin/mino-scout"
  BIN="$PREFIX/bin/mino-scout"
elif [[ -d "$ROOT/mino_scout" && -f "$ROOT/pyproject.toml" ]]; then
  install_source
else
  echo "installer payload not found (need mino-scout/ or mino_scout/)" >&2
  exit 1
fi

write_launchd() {
  local plist="$1"
  mkdir -p "$(dirname "$plist")"
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.mino.scout</string>
  <key>ProgramArguments</key>
  <array>
    <string>${BIN}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${PREFIX}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key>
    <string>${HOME}</string>
    <key>PLAYWRIGHT_BROWSERS_PATH</key>
    <string>${PREFIX}/bin/ms-playwright</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>Crashed</key>
    <true/>
  </dict>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/scout.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/scout.err.log</string>
</dict>
</plist>
EOF
}

if [[ "$(uname -s)" == "Darwin" ]]; then
  if [[ "$(id -u)" -eq 0 ]]; then
    PLIST="/Library/LaunchDaemons/com.mino.scout.plist"
    write_launchd "$PLIST"
    launchctl unload "$PLIST" >/dev/null 2>&1 || true
    launchctl load "$PLIST"
    echo "Registered LaunchDaemon: $PLIST"
  else
    PLIST="$HOME/Library/LaunchAgents/com.mino.scout.plist"
    write_launchd "$PLIST"
    launchctl unload "$PLIST" >/dev/null 2>&1 || true
    launchctl load "$PLIST"
    echo "Registered LaunchAgent: $PLIST"
  fi
elif command -v systemctl >/dev/null 2>&1; then
  if [[ "$(id -u)" -eq 0 ]]; then
    UNIT="/etc/systemd/system/mino-scout.service"
  else
    UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    mkdir -p "$UNIT_DIR"
    UNIT="$UNIT_DIR/mino-scout.service"
  fi
  cat > "$UNIT" <<EOF
[Unit]
Description=Mino Scout
After=network-online.target

[Service]
Type=simple
ExecStart=${BIN}
WorkingDirectory=${PREFIX}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
  if [[ "$(id -u)" -eq 0 ]]; then
    systemctl daemon-reload
    systemctl enable --now mino-scout.service
    echo "Registered systemd unit: $UNIT"
  else
    systemctl --user daemon-reload
    systemctl --user enable --now mino-scout.service
    echo "Registered systemd user unit: $UNIT"
  fi
else
  echo "No launchd/systemd. Start manually: $BIN"
fi

echo "Config file (written by Mino Studio): $PREFIX/config.json"
echo "Binary: $BIN"
echo "Control: $BIN status | $BIN stop"
