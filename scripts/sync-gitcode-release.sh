#!/usr/bin/env bash
# Sync smartsub-py-engine build artifacts to GitCode Release (tag=latest).
# 用 attach_files 接口取附件 id 后 DELETE 再上传，确保覆盖同名旧附件。
#
# 本地手动运行（CI 已不再自动同步 GitCode）：
#   1. 准备产物目录（默认 ./artifacts），放入与 GitHub latest release 同名的
#      smartsub-*-*.tar.gz + manifest.json + checksums.sha256。可直接下载：
#        mkdir -p artifacts && cd artifacts
#        gh release download latest --repo buxuku/smartsub-py-engine
#        cd ..
#   2. 导出 GitCode 令牌后执行：
#        GITCODE_TOKEN=xxxx bash scripts/sync-gitcode-release.sh
#   可选：GITCODE_DRY_RUN=1 预演不实际上传；ARTIFACTS_DIR 自定义产物目录。
set -euo pipefail

GITCODE_OWNER="${GITCODE_OWNER:-buxuku1}"
GITCODE_REPO="${GITCODE_REPO:-smartsub-py-engine}"
GITCODE_TAG="${GITCODE_TAG:-latest}"
GITCODE_API_URL="${GITCODE_API_URL:-https://api.gitcode.com/api/v5}"
ARTIFACTS_DIR="${ARTIFACTS_DIR:-artifacts}"
MAX_RETRIES="${MAX_RETRIES:-3}"
GITCODE_DRY_RUN="${GITCODE_DRY_RUN:-0}"
UPLOAD_PUT_TIMEOUT="${UPLOAD_PUT_TIMEOUT:-1800}"

SYNC_FILES=(
  "smartsub-faster-whisper-runtime-windows-x64.tar.gz"
  "smartsub-faster-whisper-runtime-macos-arm64.tar.gz"
  "smartsub-faster-whisper-runtime-macos-x64.tar.gz"
  "smartsub-faster-whisper-runtime-linux-x64.tar.gz"
  "manifest.json"
  "checksums.sha256"
)

FAILED_FILES=()
UPLOADED_COUNT=0

log() { echo "$*"; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}
require_env() {
  [ -n "${GITCODE_TOKEN:-}" ] || {
    echo "GITCODE_TOKEN is not set" >&2
    exit 1
  }
}

api_request() {
  local method="$1" url="$2"
  shift 2
  if [ "$GITCODE_DRY_RUN" = "1" ]; then
    echo "[dry-run] $method $url"
    return 0
  fi
  curl -sS -w "\n%{http_code}" -X "$method" \
    -H "Authorization: Bearer ${GITCODE_TOKEN}" "$@" "$url"
}

asset_already_exists() {
  local http_code="$1" body="$2"
  if [ "$http_code" = "409" ] || [ "$http_code" = "422" ]; then
    return 0
  fi
  echo "$body" | grep -qiE 'already exist|已存在|duplicate' && return 0
  return 1
}

fetch_release_json() {
  local response http_code body
  response=$(api_request GET \
    "${GITCODE_API_URL}/repos/${GITCODE_OWNER}/${GITCODE_REPO}/releases/tags/${GITCODE_TAG}" \
    2>/dev/null || true)
  http_code=$(echo "$response" | tail -n1)
  body=$(echo "$response" | sed '$d')
  [ "$http_code" = "200" ] && {
    echo "$body"
    return 0
  }
  return 1
}

# get-by-tag 详情有时不带 .id；兜底用 get-all 列表按 tag 匹配。
get_release_id() {
  local rid
  rid=$(fetch_release_json 2>/dev/null | jq -r '.id // empty')
  if [ -n "$rid" ]; then
    echo "$rid"
    return 0
  fi
  local response http_code body
  response=$(api_request GET \
    "${GITCODE_API_URL}/repos/${GITCODE_OWNER}/${GITCODE_REPO}/releases" \
    2>/dev/null || true)
  http_code=$(echo "$response" | tail -n1)
  body=$(echo "$response" | sed '$d')
  [ "$http_code" = "200" ] && echo "$body" | jq -r --arg tag "$GITCODE_TAG" \
    '(if type=="array" then . else (.data // .list // []) end)[]
      | select(.tag_name == $tag) | (.id // empty)' | head -n1
}

# GitCode release 详情里的 assets 不含附件 id，必须用专门的 attach_files 列表接口取 id。
fetch_attach_files() {
  local release_id="$1"
  [ -z "$release_id" ] && {
    echo '[]'
    return 0
  }
  local response http_code body
  response=$(api_request GET \
    "${GITCODE_API_URL}/repos/${GITCODE_OWNER}/${GITCODE_REPO}/releases/${release_id}/attach_files" \
    2>/dev/null || true)
  http_code=$(echo "$response" | tail -n1)
  body=$(echo "$response" | sed '$d')
  if [ "$http_code" = "200" ]; then
    echo "$body" | jq -c 'if type=="array" then . elif .data then .data elif .list then .list else (.attach_files // []) end' 2>/dev/null || echo '[]'
  else
    echo '[]'
  fi
}

attach_id_from_list() {
  echo "$1" | jq -r --arg name "$2" \
    '.[] | select(.name == $name) | (.id // .attach_id // .attach_file_id // empty) | tostring' | head -n1
}

delete_attachment() {
  local release_id="$1" attach_id="$2" filename="$3"
  { [ -z "$attach_id" ] || [ "$attach_id" = "null" ]; } && return 0
  log "  Deleting existing: ${filename} (id=${attach_id})"
  [ "$GITCODE_DRY_RUN" = "1" ] && return 0
  local response http_code
  response=$(curl -sS -w "\n%{http_code}" -X DELETE \
    -H "Authorization: Bearer ${GITCODE_TOKEN}" \
    "${GITCODE_API_URL}/repos/${GITCODE_OWNER}/${GITCODE_REPO}/releases/${release_id}/attach_files/${attach_id}")
  http_code=$(echo "$response" | tail -n1)
  case "$http_code" in
    200 | 204 | 404) return 0 ;;
    *)
      log "  Warning: delete ${filename} HTTP ${http_code}"
      return 1
      ;;
  esac
}

create_release() {
  log "Creating GitCode tag and release '${GITCODE_TAG}'..."
  [ "$GITCODE_DRY_RUN" = "1" ] && return 0
  curl -sS -X POST -H "Authorization: Bearer ${GITCODE_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"tag_name\":\"${GITCODE_TAG}\",\"refs\":\"main\",\"tag_message\":\"Latest smartsub-engine builds\"}" \
    "${GITCODE_API_URL}/repos/${GITCODE_OWNER}/${GITCODE_REPO}/tags" >/dev/null || true
  local create_response create_code
  create_response=$(curl -sS -w "\n%{http_code}" -X POST \
    -H "Authorization: Bearer ${GITCODE_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"tag_name\":\"${GITCODE_TAG}\",\"name\":\"latest\",\"body\":\"Auto-synced from smartsub-py-engine CI\",\"target_commitish\":\"main\"}" \
    "${GITCODE_API_URL}/repos/${GITCODE_OWNER}/${GITCODE_REPO}/releases")
  create_code=$(echo "$create_response" | tail -n1)
  if [ "$create_code" != "201" ] && [ "$create_code" != "200" ]; then
    echo "Failed to create GitCode release (HTTP ${create_code})" >&2
    echo "$create_response" | sed '$d' >&2
    exit 1
  fi
}

# 关键修复：GitCode 对「同名附件」的 PUT 上传不会覆盖旧内容，且该 release 与附件
# 均不暴露数字 id（实测 release JSON 无 id 字段、attach_files 需要 release id 而拿不到），
# 因此无法定点删除单个旧附件。实测唯一可靠的刷新办法：删除 tag（GitCode 会级联删除
# 该 release 及其全部附件）再重建空 release，随后所有文件都按「全新名字」上传即可生效。
reset_release() {
  log "Resetting GitCode release '${GITCODE_TAG}' (delete tag -> recreate, the only way to overwrite assets) ..."
  [ "$GITCODE_DRY_RUN" = "1" ] && return 0
  curl -sS -o /dev/null -w "  delete tag ${GITCODE_TAG} -> %{http_code}\n" \
    -X DELETE -H "Authorization: Bearer ${GITCODE_TOKEN}" \
    "${GITCODE_API_URL}/repos/${GITCODE_OWNER}/${GITCODE_REPO}/tags/${GITCODE_TAG}" || true
  # 等删除在 GitCode 后端传播，避免重建/上传命中残留索引（GET 不再 200 即视为已删）
  local i
  for i in 1 2 3 4 5 6; do
    sleep 2
    fetch_release_json >/dev/null 2>&1 || break
  done
  create_release
}

upload_file() {
  local file_path="$1" filename release_id attach_list attach_id
  filename=$(basename "$file_path")
  [ -f "$file_path" ] || {
    log "  Skip missing: ${filename}"
    return 0
  }

  release_id=$(get_release_id)
  attach_list=$(fetch_attach_files "$release_id")
  attach_id=$(attach_id_from_list "$attach_list" "$filename")
  if [ -n "$attach_id" ] && [ -n "$release_id" ]; then
    delete_attachment "$release_id" "$attach_id" "$filename" || true
  fi

  local encoded retry curl_status http_code upload_response upload_info upload_url put_response response_body headers_file
  encoded=$(printf '%s' "$filename" | jq -sRr @uri)

  for ((retry = 0; retry < MAX_RETRIES; retry++)); do
    log "  Uploading: ${filename} (attempt $((retry + 1))/${MAX_RETRIES})"
    if [ "$GITCODE_DRY_RUN" = "1" ]; then
      UPLOADED_COUNT=$((UPLOADED_COUNT + 1))
      return 0
    fi

    curl_status=0
    upload_response=$(curl -sS -w "\n%{http_code}" --connect-timeout 30 --max-time 120 \
      -H "Authorization: Bearer ${GITCODE_TOKEN}" \
      "${GITCODE_API_URL}/repos/${GITCODE_OWNER}/${GITCODE_REPO}/releases/${GITCODE_TAG}/upload_url?file_name=${encoded}") || curl_status=$?
    [ "$curl_status" -ne 0 ] && {
      log "  upload_url failed (curl ${curl_status})"
      sleep $((10 * (retry + 1)))
      continue
    }

    http_code=$(echo "$upload_response" | tail -n1)
    upload_info=$(echo "$upload_response" | sed '$d')
    upload_url=$(echo "$upload_info" | jq -r '.url // empty')
    if [ -z "$upload_url" ]; then
      # 关键修复：取 upload_url 时报“已存在”说明预删未生效（同名旧附件仍在）。
      # 绝不能当成功返回——否则文件没真正上传，GitCode 仍是旧内容（“假成功”）。
      # 这里重取 id 强制删除后重试；若重试耗尽仍删不掉，落到 FAILED_FILES 真报错。
      if asset_already_exists "$http_code" "$upload_info"; then
        log "  Asset still exists (pre-delete missed); re-deleting & retrying: ${filename}"
        release_id=$(get_release_id)
        attach_list=$(fetch_attach_files "$release_id")
        attach_id=$(attach_id_from_list "$attach_list" "$filename")
        delete_attachment "$release_id" "$attach_id" "$filename" || true
        sleep $((5 * (retry + 1)))
        continue
      fi
      log "  upload_url HTTP ${http_code}: ${upload_info}"
      sleep $((10 * (retry + 1)))
      continue
    fi

    headers_file=$(mktemp)
    echo "$upload_info" | jq -r '.headers | to_entries[] | "header = \"" + .key + ": " + .value + "\""' >"$headers_file"
    curl_status=0
    put_response=$(curl -sS -w "\n%{http_code}" --connect-timeout 30 --max-time "$UPLOAD_PUT_TIMEOUT" \
      --speed-time 120 --speed-limit 10240 -K "$headers_file" -T "${file_path}" "$upload_url") || curl_status=$?
    rm -f "$headers_file"
    [ "$curl_status" -ne 0 ] && {
      log "  PUT failed (curl ${curl_status})"
      sleep $((15 * (retry + 1)))
      continue
    }

    http_code=$(echo "$put_response" | tail -n1)
    response_body=$(echo "$put_response" | sed '$d')
    if [ "$http_code" -ge 200 ] && [ "$http_code" -lt 300 ]; then
      log "  Uploaded: ${filename}"
      UPLOADED_COUNT=$((UPLOADED_COUNT + 1))
      return 0
    fi
    if asset_already_exists "$http_code" "$response_body"; then
      release_id=$(get_release_id)
      attach_list=$(fetch_attach_files "$release_id")
      attach_id=$(attach_id_from_list "$attach_list" "$filename")
      delete_attachment "$release_id" "$attach_id" "$filename" || true
    else
      log "  Failed (HTTP ${http_code}): ${response_body}"
    fi
    sleep $((15 * (retry + 1)))
  done

  log "  ERROR: gave up uploading ${filename}"
  FAILED_FILES+=("$filename")
  return 1
}

main() {
  require_cmd curl
  require_cmd jq
  require_env
  reset_release
  for filename in "${SYNC_FILES[@]}"; do
    upload_file "${ARTIFACTS_DIR}/${filename}" || true
    sleep 1
  done
  log "Uploaded: ${UPLOADED_COUNT}"
  if [ "${#FAILED_FILES[@]}" -gt 0 ]; then
    echo "GitCode sync completed with failures: ${FAILED_FILES[*]}" >&2
    exit 1
  fi
  log "GitCode sync completed successfully"
}

main "$@"
