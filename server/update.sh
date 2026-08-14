#!/usr/bin/env bash
# 安全日常更新：备份 -> fast-forward 拉取 -> 重启 -> 冒烟检查。
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/www/laingzizhiwang}"
ENV_FILE="${ENV_FILE:-/etc/lzz-config.env}"
SERVICE_NAME="${SERVICE_NAME:-lzz-config.service}"

if [[ "${EUID}" -ne 0 ]]; then
    echo "错误：更新需要 root 权限以执行备份和重启服务。" >&2
    exit 1
fi
[[ -r "${ENV_FILE}" ]] && set -a && source "${ENV_FILE}" && set +a
NODE_HOME="${NODE_HOME:-/opt/lzz-node}"
export PATH="${NODE_HOME}/bin:${PATH}"
cd "${APP_DIR}"

if [[ -n "$(git status --porcelain)" ]]; then
    echo "错误：工作区存在未提交改动，停止更新以避免覆盖。" >&2
    exit 1
fi

previous_commit="$(git rev-parse HEAD)"
echo "更新前版本：${previous_commit}"
echo "===== 更新前安全备份 ====="
"${APP_DIR}/server/backup.sh" --pre-update

echo "===== 拉取代码（仅允许 fast-forward） ====="
# pull 失败必须立即退出，不能继续重启服务。
git pull --ff-only
new_commit="$(git rev-parse HEAD)"
"${NODE_HOME}/bin/npm" --prefix "${APP_DIR}" ci
if [[ "${VENDOR_SYNC_ENABLED:-false}" == "true" ]]; then
    export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/opt/lzz-playwright}"
    PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=180000 \
        "${APP_DIR}/node_modules/.bin/playwright" install chromium --no-shell
    chmod -R a+rX "${PLAYWRIGHT_BROWSERS_PATH}"
fi
/usr/bin/python3 -m unittest server.tests.test_backend
/usr/bin/python3 -m py_compile server/config_server.py server/check_monitor.py server/vendor_sync.py
"${NODE_HOME}/bin/node" --check server/vendor_scrape.js

echo "===== 重启服务并执行健康冒烟 ====="
systemctl restart "${SERVICE_NAME}"
sleep 2
if /usr/bin/python3 "${APP_DIR}/server/health_check.py"; then
    echo "更新成功：${previous_commit} -> ${new_commit}"
    exit 0
fi

echo "错误：新版本健康检查失败，当前未自动改写 Git 工作区。" >&2
echo "查看日志：journalctl -u ${SERVICE_NAME} -n 100 --no-pager" >&2
cat >&2 <<EOF
确认需要回滚后执行：
  cd ${APP_DIR}
  git reset --hard ${previous_commit}
  systemctl restart ${SERVICE_NAME}
  /usr/bin/python3 ${APP_DIR}/server/health_check.py
如数据需要恢复，请先停止服务，再从本次 pre-update 备份恢复。
EOF
exit 1
