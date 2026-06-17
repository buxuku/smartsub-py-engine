#!/usr/bin/env bash
# 在「国内机器」上手动执行：从 GitHub latest 拉取产物，再推送到 GitCode。
#
# 设计目标（应对 GitHub 境外 runner → GitCode 国内上传慢的问题）：
#   - 上传 GitCode 这一段在你本机/国内网络完成 → 域内传输快且稳；
#   - GitHub 下载这一段可选走 ghproxy 镜像加速（USE_GHPROXY=1）；
#   - 先比对 GitHub 与 GitCode 的 manifest.gitSha：一致且文件齐全则跳过，不重复传；
#   - 仅在上传成功后删除本地下载的临时文件。
#
# 用法：
#   export GITCODE_TOKEN=<你的 GitCode 个人令牌>
#   bash scripts/sync-gitcode-from-github.sh            # 直连 GitHub 下载
#   USE_GHPROXY=1 bash scripts/sync-gitcode-from-github.sh   # 走 ghproxy 加速下载
#
# 依赖：curl、jq（上传复用 scripts/sync-gitcode-release.sh，需要 bash）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

GH_REPO="${GH_REPO:-buxuku/smartsub-py-engine}"
GH_TAG="${GH_TAG:-latest}"
GITCODE_OWNER="${GITCODE_OWNER:-buxuku1}"
GITCODE_REPO="${GITCODE_REPO:-smartsub-py-engine}"
GITCODE_TAG="${GITCODE_TAG:-latest}"
USE_GHPROXY="${USE_GHPROXY:-0}"
GHPROXY_BASE="${GHPROXY_BASE:-https://ghfast.top}"
WORKDIR="${WORKDIR:-$REPO_ROOT/.gitcode-sync-tmp}"

# 需要同步的产物（与 sync-gitcode-release.sh 的 SYNC_FILES 保持一致）
FILES=(
  "smartsub-faster-whisper-windows-x64.tar.gz"
  "smartsub-faster-whisper-macos-arm64.tar.gz"
  "smartsub-faster-whisper-macos-x64.tar.gz"
  "smartsub-faster-whisper-linux-x64.tar.gz"
  "manifest.json"
  "checksums.sha256"
)

log() { echo "[sync] $*"; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "缺少命令: $1" >&2
    exit 1
  }
}

gh_url() {
  local base="https://github.com/${GH_REPO}/releases/download/${GH_TAG}/$1"
  if [ "$USE_GHPROXY" = "1" ]; then echo "${GHPROXY_BASE}/${base}"; else echo "$base"; fi
}

gitcode_url() {
  echo "https://gitcode.com/${GITCODE_OWNER}/${GITCODE_REPO}/releases/download/${GITCODE_TAG}/$1"
}

# 读取远端 manifest.json 的 gitSha（取不到则返回空，不报错）
remote_gitsha() {
  curl -fsSL --connect-timeout 20 --max-time 120 "$1" 2>/dev/null | jq -r '.gitSha // empty' 2>/dev/null || true
}

# GitCode 上 6 个文件是否都在（用 HEAD 探测，任一缺失即返回非 0）
gitcode_has_all_files() {
  local name
  for name in "${FILES[@]}"; do
    curl -fsI --connect-timeout 20 --max-time 60 "$(gitcode_url "$name")" >/dev/null 2>&1 || return 1
  done
  return 0
}

verify_checksums() {
  (
    cd "$WORKDIR"
    if command -v sha256sum >/dev/null 2>&1; then
      sha256sum -c checksums.sha256
    else
      shasum -a 256 -c checksums.sha256
    fi
  )
}

cleanup_local() {
  local name
  for name in "${FILES[@]}"; do rm -f "$WORKDIR/$name"; done
  rmdir "$WORKDIR" 2>/dev/null || true
}

main() {
  require_cmd curl
  require_cmd jq
  : "${GITCODE_TOKEN:?需要设置 GITCODE_TOKEN（GitCode 个人令牌）}"

  log "读取 GitHub manifest.gitSha ..."
  local gh_sha
  gh_sha="$(remote_gitsha "$(gh_url manifest.json)")"
  [ -n "$gh_sha" ] || {
    echo "无法获取 GitHub manifest（检查网络，或加 USE_GHPROXY=1 走镜像）" >&2
    exit 1
  }
  log "GitHub  gitSha = $gh_sha"

  log "读取 GitCode manifest.gitSha ..."
  local gc_sha
  gc_sha="$(remote_gitsha "$(gitcode_url manifest.json)")"
  log "GitCode gitSha = ${gc_sha:-<不存在>}"

  if [ -n "$gc_sha" ] && [ "$gc_sha" = "$gh_sha" ] && gitcode_has_all_files; then
    log "GitCode 已是最新（gitSha 一致且 6 个文件齐全），无需同步。"
    exit 0
  fi

  log "需要同步 → 下载 GitHub 产物到 $WORKDIR"
  mkdir -p "$WORKDIR"
  local name
  for name in "${FILES[@]}"; do
    log "  下载: $name"
    curl -fL --retry 3 --retry-delay 5 \
      --connect-timeout 30 --max-time 1800 \
      -o "$WORKDIR/$name" "$(gh_url "$name")"
  done

  log "校验下载完整性 (sha256) ..."
  verify_checksums

  log "推送到 GitCode（复用 sync-gitcode-release.sh）..."
  ARTIFACTS_DIR="$WORKDIR" bash "$SCRIPT_DIR/sync-gitcode-release.sh"

  log "上传成功 → 清理本地临时文件 ..."
  cleanup_local
  log "完成：GitCode 已更新到 gitSha=$gh_sha"
}

main "$@"
