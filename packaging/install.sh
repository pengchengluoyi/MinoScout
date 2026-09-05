#!/usr/bin/env bash
# Install Mino Scout next to config.json and register a daemon.
#
# 分层安装：zip 里带哪几层就装哪几层，没带的层原地不动。
#
#   layers.txt + runtime/         → 替换 bin/mino-scout 与 bin/_internal
#   layers.txt + app/             → 替换 bin/app
#   layers.txt + browser/         → 替换 bin/ms-playwright
#   mino_scout/ + pyproject.toml  → 源码包（dev）：venv + pip，需要 Python 3.12+
#
# **刻意不再整体 rm -rf bin**：浏览器层实测 781 MB、占安装体积 78%，而它几乎从不变。
# 分层前每次更新都要连它一起重下重铺，这是包体问题的一半。
#
# **安装根刻意仍是 $PREFIX/bin**：Studio 的 scoutBinCandidates() 和 launchd plist
# 都硬编码了 $PREFIX/bin/mino-scout 与 $PREFIX/bin/ms-playwright，换根等于跨仓破坏。
# 层与目录的映射见 scripts/layers.py 的 PAYLOAD_DIRS —— 改那里要同步改这里。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"

if [[ "$(uname -s)" == "Darwin" ]]; then
  PREFIX="${MINO_SCOUT_HOME:-$HOME/Library/Application Support/MinoScout}"
  LOG_DIR="$HOME/Library/Logs/MinoScout"
else
  PREFIX="${MINO_SCOUT_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/minoscout}"
  LOG_DIR="$PREFIX/logs"
fi

DEST="$PREFIX/bin"
BIN=""
LAYER_NAMES="runtime app browser"

echo "Mino Scout → $PREFIX"
mkdir -p "$PREFIX" "$LOG_DIR"

# ---------------- 层清单读取 ----------------

# zip 根的 layers.txt：`<层> <指纹> [k=v ...]`，# 开头是注释。
zip_layer_key() {
  [[ -f "$ROOT/layers.txt" ]] || return 0
  awk -v l="$1" '/^#/ {next} $1==l {print $2; exit}' "$ROOT/layers.txt"
}

zip_layer_field() {
  [[ -f "$ROOT/layers.txt" ]] || return 0
  awk -v l="$1" -v k="$2" '
    /^#/ {next}
    $1==l { for (i=3; i<=NF; i++) { split($i, kv, "="); if (kv[1]==k) { print kv[2]; exit } } }
  ' "$ROOT/layers.txt"
}

# 安装后的 bin/layers.txt：只有 `<层> <指纹>` 两列，是已装状态。
installed_layer_key() {
  [[ -f "$DEST/layers.txt" ]] || return 0
  awk -v l="$1" '/^#/ {next} $1==l {print $2; exit}' "$DEST/layers.txt"
}

record_layer() {
  local layer="$1" key="$2" f="$DEST/layers.txt" tmp
  tmp="$(mktemp)"
  if [[ -f "$f" ]]; then
    grep -v -E "^${layer}[[:space:]]" "$f" > "$tmp" || true
  fi
  printf '%s %s\n' "$layer" "$key" >> "$tmp"
  LC_ALL=C sort "$tmp" -o "$tmp"
  mv "$tmp" "$f"
}

layer_target() {
  # 层 → bin/ 下的相对路径。runtime 平铺到 bin/ 根，所以是空串。
  case "$1" in
    runtime) printf '%s' "" ;;
    app)     printf '%s' "app" ;;
    browser) printf '%s' "ms-playwright" ;;
    *) echo "unknown layer: $1" >&2; return 1 ;;
  esac
}

# ---------------- 落地 ----------------

# 先拷到 .new 再 rename，把"目标已删、新的还没到位"的窗口压到一次 mv。
replace_path() {
  local src="$1" dst="$2"
  rm -rf "$dst.new" "$dst.old"
  cp -R "$src" "$dst.new"
  if [[ -e "$dst" ]]; then
    mv "$dst" "$dst.old"
  fi
  mv "$dst.new" "$dst"
  rm -rf "$dst.old"
}

harden() {
  # GitHub 的 zip 常把嵌套的 Playwright/adb 二进制的 +x 丢掉；macOS 还会加隔离属性。
  local path="$1"
  [[ -e "$path" ]] || return 0
  if [[ "$(uname -s)" == "Darwin" ]]; then
    xattr -cr "$path" >/dev/null 2>&1 || true
  fi
  if [[ -d "$path" ]]; then
    find "$path" -type f -exec chmod u+x {} + 2>/dev/null || true
  else
    chmod u+x "$path" 2>/dev/null || true
  fi
}

install_layer() {
  local layer="$1" src="$ROOT/$layer" rel target
  rel="$(layer_target "$layer")"
  if [[ -z "$rel" ]]; then
    # runtime：逐个顶层条目替换，这样将来多出别的顶层文件也不用改脚本。
    local entry
    for entry in "$src"/*; do
      [[ -e "$entry" ]] || continue
      target="$DEST/$(basename "$entry")"
      replace_path "$entry" "$target"
      harden "$target"
    done
  else
    target="$DEST/$rel"
    replace_path "$src" "$target"
    harden "$target"
  fi
  echo "  层 $layer → ${rel:-bin/}  ($(zip_layer_key "$layer"))"
}

# app 层单独更新时，必须确认本机 runtime 与它是同一套依赖 —— 否则新代码引用了
# 本机 runtime 里没有的依赖，换完就起不来，而症状是启动时一句 ImportError。
check_app_gate() {
  local need have
  need="$(zip_layer_field app requires_runtime)"
  [[ -n "$need" ]] || return 0
  have="$(installed_layer_key runtime)"
  if [[ -z "$have" ]]; then
    echo "app layer needs runtime $need but nothing is installed here." >&2
    echo "Run a full install (the combined MinoScout-<ver>-<os>-<arch>.zip) first." >&2
    exit 1
  fi
  if [[ "$have" != "$need" ]]; then
    echo "app layer needs runtime $need but $have is installed." >&2
    echo "Run a full install (the combined MinoScout-<ver>-<os>-<arch>.zip) instead." >&2
    exit 1
  fi
}

install_layers() {
  local layer present=0
  mkdir -p "$DEST"

  # app-only 增量：先过闸门，再动任何文件。
  if [[ -d "$ROOT/app" && ! -d "$ROOT/runtime" ]]; then
    check_app_gate
  fi

  for layer in $LAYER_NAMES; do
    [[ -d "$ROOT/$layer" ]] || continue
    install_layer "$layer"
    record_layer "$layer" "$(zip_layer_key "$layer")"
    present=$((present + 1))
  done

  if [[ "$present" -eq 0 ]]; then
    echo "layers.txt found but no payload directory (runtime/ app/ browser/)" >&2
    exit 1
  fi

  if [[ ! -f "$DEST/mino-scout" ]]; then
    echo "runtime layer missing: $DEST/mino-scout not present after install" >&2
    echo "Run a full install (the combined MinoScout-<ver>-<os>-<arch>.zip)." >&2
    exit 1
  fi
  chmod +x "$DEST/mino-scout" 2>/dev/null || true
  if [[ ! -f "$DEST/app/mino_scout/cli.py" ]]; then
    echo "app layer missing: $DEST/app/mino_scout/cli.py not present after install" >&2
    exit 1
  fi
  BIN="$DEST/mino-scout"
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

# ---------------- 停掉在跑的实例 ----------------

# 刻意在动载荷**之前**停：替换正在运行的可执行文件与 _internal 会让当前进程
# 崩在半路（分层前的脚本也有这个问题，顺手修掉）。
stop_running() {
  if [[ "$(uname -s)" == "Darwin" ]]; then
    local plist
    for plist in "$HOME/Library/LaunchAgents/com.mino.scout.plist" \
                 "/Library/LaunchDaemons/com.mino.scout.plist"; do
      [[ -f "$plist" ]] || continue
      launchctl unload "$plist" >/dev/null 2>&1 || true
    done
  elif command -v systemctl >/dev/null 2>&1; then
    if [[ "$(id -u)" -eq 0 ]]; then
      systemctl stop mino-scout.service >/dev/null 2>&1 || true
    else
      systemctl --user stop mino-scout.service >/dev/null 2>&1 || true
    fi
  fi
}

stop_running

if [[ -f "$ROOT/layers.txt" ]]; then
  install_layers
elif [[ -d "$ROOT/mino_scout" && -f "$ROOT/pyproject.toml" ]]; then
  install_source
else
  echo "installer payload not found (need layers.txt with runtime/ app/ browser/, or mino_scout/)" >&2
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

# CI 与本地自测只想验证"层有没有正确落地"，不该在跑测试的机器上留一个常驻服务。
if [[ -n "${MINO_SCOUT_SKIP_SERVICE:-}" ]]; then
  echo "MINO_SCOUT_SKIP_SERVICE set - not registering a daemon."
elif [[ "$(uname -s)" == "Darwin" ]]; then
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

if [[ -f "$DEST/layers.txt" ]]; then
  echo "Installed layers:"
  sed 's/^/  /' "$DEST/layers.txt"
fi
echo "Config file (written by Mino Studio): $PREFIX/config.json"
echo "Binary: $BIN"
echo "Control: $BIN status | $BIN stop"
