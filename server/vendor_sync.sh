#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/www/laingzizhiwang}"
DATA_DIR="${LAINGZIZHIWANG_DATA_DIR:-${DATA_DIR:-/www/laingzizhiwang-data}}"
ENV_FILE="${ENV_FILE:-/etc/lzz-config.env}"
[[ -r "${ENV_FILE}" ]] && set -a && source "${ENV_FILE}" && set +a
[[ "${VENDOR_SYNC_ENABLED:-false}" == "true" ]] || exit 0
NODE_HOME="${NODE_HOME:-/opt/lzz-node}"
PYTHON_BIN="${PYTHON_BIN:-/opt/lzz-venv/bin/python}"
[[ -x "${PYTHON_BIN}" ]] || PYTHON_BIN=/usr/bin/python3
MODE="${1:-scheduled}"

mkdir -p "${DATA_DIR}/vendor-imports"
tmp="$(mktemp "${DATA_DIR}/vendor-imports/.sync.XXXXXX.json")"
selector="$(mktemp "${DATA_DIR}/vendor-imports/.selector.XXXXXX.json")"
trap 'rm -f "${tmp}" "${selector}"' EXIT
exec 9>"${DATA_DIR}/vendor-imports/vendor-sync.lock"
flock -n 9 || { echo "乙方同步正在运行"; exit 75; }

if [[ "${MODE}" == "scheduled" ]]; then
  set +e
  "${PYTHON_BIN}" "${APP_DIR}/server/vendor_sync.py" --check-due
  due_code=$?
  set -e
  [[ ${due_code} -eq 3 ]] && exit 0
  [[ ${due_code} -eq 0 ]] || exit "${due_code}"
fi

if [[ "${MODE}" == "match" ]]; then
  "${PYTHON_BIN}" "${APP_DIR}/server/vendor_sync.py" --export-match-candidates "${selector}"
  VENDOR_MODE=match VENDOR_MATCH_CANDIDATES_FILE="${selector}" \
    "${NODE_HOME}/bin/node" "${APP_DIR}/server/vendor_scrape.js" "${tmp}"
  "${PYTHON_BIN}" "${APP_DIR}/server/vendor_sync.py" --force --match-json "${tmp}"
else
  "${PYTHON_BIN}" "${APP_DIR}/server/vendor_sync.py" --export-known-companies "${selector}"
  VENDOR_MODE=sync VENDOR_KNOWN_COMPANIES_FILE="${selector}" \
    "${NODE_HOME}/bin/node" "${APP_DIR}/server/vendor_scrape.js" "${tmp}"
  "${PYTHON_BIN}" "${APP_DIR}/server/vendor_sync.py" --force --from-json "${tmp}"
fi
