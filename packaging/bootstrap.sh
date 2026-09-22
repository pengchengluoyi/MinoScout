#!/usr/bin/env bash
# Mino Scout 远程安装：从 GitHub Release 拉 manifest + zip，install.sh 注册服务。
# Studio 复制的一行 curl | bash -s -- --token … --nexus-url … --studio-id …
# Nexus 不参与安装流量；token 在 Scout REGISTER 时校验。
set -euo pipefail

MINO_GITHUB_REPO="${MINO_SCOUT_GITHUB_REPO:-pengchengluoyi/MinoScout}"
TOKEN=""
NEXUS_URL="http://mino.local:10104"
STUDIO_ID=""
RELEASE_VERSION=""
MANIFEST_URL=""

usage() {
  echo "Usage: bootstrap.sh --token <install-token> [--nexus-url URL] [--studio-id ID] [--release-version VER]" >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --token) TOKEN="${2:-}"; shift 2 ;;
    --nexus-url) NEXUS_URL="${2:-}"; shift 2 ;;
    --studio-id) STUDIO_ID="${2:-}"; shift 2 ;;
    --release-version) RELEASE_VERSION="${2:-}"; shift 2 ;;
    --manifest-url) MANIFEST_URL="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

if [[ -z "$TOKEN" ]]; then
  echo "缺少 --token（在 Mino Studio 复制安装命令）" >&2
  exit 2
fi

if [[ -z "$MANIFEST_URL" ]]; then
  if [[ -n "$RELEASE_VERSION" ]]; then
    ver="${RELEASE_VERSION#v}"
    MANIFEST_URL="https://github.com/${MINO_GITHUB_REPO}/releases/download/v${ver}/manifest.json"
  else
    MANIFEST_URL="https://github.com/${MINO_GITHUB_REPO}/releases/latest/download/manifest.json"
  fi
fi

if [[ "$(uname -s)" == "Darwin" ]]; then
  PREFIX="${MINO_SCOUT_HOME:-$HOME/Library/Application Support/MinoScout}"
else
  PREFIX="${MINO_SCOUT_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/minoscout}"
fi

if ! command -v python3 >/dev/null 2>&1 && ! command -v jq >/dev/null 2>&1; then
  echo "需要 jq 或 python3 解析 manifest / 写入 config（推荐：brew install jq）" >&2
  exit 1
fi

OS="$(uname -s)"
case "$OS" in
  Darwin) SCOUT_OS="darwin" ;;
  Linux) SCOUT_OS="linux" ;;
  MINGW*|MSYS*|CYGWIN*) SCOUT_OS="win32" ;;
  *) echo "暂不支持 OS: $OS" >&2; exit 1 ;;
esac

MACHINE="$(uname -m)"
case "$MACHINE" in
  arm64|aarch64) SCOUT_ARCH="arm64" ;;
  x86_64|amd64) SCOUT_ARCH="x64" ;;
  *) echo "暂不支持 arch: $MACHINE" >&2; exit 1 ;;
esac

echo "→ manifest ${MANIFEST_URL}"
MANIFEST_JSON="$(mktemp)"
trap 'rm -f "$MANIFEST_JSON" "${ZIP_PATH:-}"' EXIT
curl -fsSL "$MANIFEST_URL" -o "$MANIFEST_JSON"

if command -v jq >/dev/null 2>&1; then
  IFS=$'\t' read -r ZIP_URL ZIP_SHA ZIP_NAME APP_VER <<<"$(jq -r --arg os "$SCOUT_OS" --arg arch "$SCOUT_ARCH" '
    (.items // [.])[]
    | select((.os|ascii_downcase)==($os|ascii_downcase) and ((.arch|ascii_downcase)==($arch|ascii_downcase) or (.arch|ascii_downcase)=="x64"))
    | [.url, (.sha256//""), (.filename//"scout-installer"), (.version//"")]
    | @tsv' "$MANIFEST_JSON" | head -1)"
else
IFS=$'\t' read -r ZIP_URL ZIP_SHA ZIP_NAME APP_VER <<<"$(python3 - "$MANIFEST_JSON" "$SCOUT_OS" "$SCOUT_ARCH" <<'PY'
import json, sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
want_os, want_arch = sys.argv[2], sys.argv[3]
version = str(manifest.get("version") or "")

def norm_os(v):
    v = str(v or "").lower()
    if v in ("darwin", "macos", "mac"):
        return "darwin"
    if v in ("win32", "windows"):
        return "win32"
    return v

def norm_arch(v):
    v = str(v or "").lower()
    if v in ("arm64", "aarch64"):
        return "arm64"
    return "x64"

def pick(row):
    if not row or not row.get("url"):
        return None
    return row

items = manifest.get("items")
row = None
if isinstance(items, list):
    for it in items:
        if norm_os(it.get("os")) == want_os and norm_arch(it.get("arch")) == want_arch:
            row = pick(it)
            break
else:
    row = pick(manifest)

if not row:
    raise SystemExit(f"manifest 里没有 {want_os}-{want_arch} 安装包")

url = str(row["url"])
sha = str(row.get("sha256") or "")
name = str(row.get("filename") or url.split("?")[0].split("/")[-1])
ver = str(row.get("version") or version)
print("\t".join([url, sha, name, ver]))
PY
)"
fi

if [[ -z "$ZIP_URL" ]]; then
  echo "无法从 manifest 解析安装包 URL" >&2
  exit 1
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR" "$MANIFEST_JSON" "${ZIP_PATH:-}"' EXIT
ZIP_PATH="${WORKDIR}/package.zip"

echo "→ 下载 ${ZIP_NAME}"
curl -fL "$ZIP_URL" -o "$ZIP_PATH"

if [[ -n "$ZIP_SHA" ]]; then
  if command -v shasum >/dev/null 2>&1; then
    ACTUAL="$(shasum -a 256 "$ZIP_PATH" | awk '{print $1}')"
  else
    ACTUAL="$(openssl dgst -sha256 "$ZIP_PATH" | awk '{print $NF}')"
  fi
  if [[ "$ACTUAL" != "$ZIP_SHA" ]]; then
    echo "sha256 不匹配：期望 ${ZIP_SHA}，得到 ${ACTUAL}" >&2
    exit 1
  fi
  echo "→ sha256 OK"
fi

EXTRACT="${WORKDIR}/extract"
mkdir -p "$EXTRACT"
unzip -q "$ZIP_PATH" -d "$EXTRACT"
INSTALL_ROOT="$(find "$EXTRACT" -name install.sh -type f | head -1)"
if [[ -z "$INSTALL_ROOT" ]]; then
  echo "zip 内找不到 install.sh" >&2
  exit 1
fi
INSTALL_DIR="$(dirname "$INSTALL_ROOT")"

echo "→ 安装到 ${PREFIX}"
( cd "$INSTALL_DIR" && ./install.sh )

CONFIG_PATH="${PREFIX}/config.json"
mkdir -p "$PREFIX"
if command -v jq >/dev/null 2>&1; then
  CFG_BASE="{}"
  [[ -f "$CONFIG_PATH" ]] && CFG_BASE="$(cat "$CONFIG_PATH")"
  STUDIO_CLEAN=""
  if [[ -n "$STUDIO_ID" ]]; then
    STUDIO_CLEAN="$(printf '%s' "$STUDIO_ID" | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]')"
  fi
  jq --arg nexus "$NEXUS_URL" --arg token "$TOKEN" --arg ver "$APP_VER" --arg studio "$STUDIO_CLEAN" '
    .nexus_url = ($nexus | rtrimstr("/"))
    | .token = $token
    | .version = $ver
    | (if $studio != "" then .studio_id = $studio else . end)
  ' <<<"$CFG_BASE" > "${CONFIG_PATH}.tmp"
  mv "${CONFIG_PATH}.tmp" "$CONFIG_PATH"
else
python3 - "$CONFIG_PATH" "$NEXUS_URL" "$TOKEN" "$STUDIO_ID" "$APP_VER" <<'PY'
import json, sys, datetime
from pathlib import Path

path, nexus, token, studio, version = sys.argv[1:6]
cfg = {}
p = Path(path)
if p.is_file():
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cfg = {}
cfg["nexus_url"] = nexus.rstrip("/")
cfg["token"] = token
if studio:
    cfg["studio_id"] = "".join(c for c in studio.lower() if c.isalnum())
cfg["version"] = version
cfg["updated_at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
text = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
tmp = p.with_suffix(".json.tmp")
tmp.write_text(text, encoding="utf-8")
tmp.replace(p)
PY
fi

echo "→ 配置已写入 ${CONFIG_PATH}"

SCOUT_BIN="${PREFIX}/bin/mino-scout"
if [[ -x "$SCOUT_BIN" ]]; then
  "$SCOUT_BIN" stop >/dev/null 2>&1 || true
  if [[ "$(uname -s)" == "Darwin" ]]; then
    uid="$(id -u)"
    target="gui/${uid}/com.mino.scout"
    launchctl kickstart -k "$target" >/dev/null 2>&1 \
      || launchctl bootstrap "gui/${uid}" "$HOME/Library/LaunchAgents/com.mino.scout.plist" 2>/dev/null \
      || true
  elif command -v systemctl >/dev/null 2>&1; then
    systemctl --user restart mino-scout.service >/dev/null 2>&1 || true
  fi
fi

echo "→ 执行 ${SCOUT_BIN} status 查看服务状态"
echo "Mino Scout ${APP_VER} 安装完成（runtime+app）。Chromium / ADB Keyboard 将在 Scout 首次连上 Nexus 后自动下载。"
