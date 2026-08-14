#!/usr/bin/env bash
# 首次部署。日常发布请使用 server/update.sh。
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/www/laingzizhiwang}"
DATA_DIR="${DATA_DIR:-/www/laingzizhiwang-data}"
ENV_FILE="${ENV_FILE:-/etc/lzz-config.env}"
SERVICE_USER="${SERVICE_USER:-lzz}"

if [[ "${EUID}" -ne 0 ]]; then
    echo "错误：首次部署必须以 root 运行。" >&2
    exit 1
fi
if ! command -v sqlite3 >/dev/null && command -v dnf >/dev/null; then
    dnf install -y sqlite
fi
for command in git python3 node npm sqlite3 systemctl nginx curl tar awk sha256sum; do
    command -v "${command}" >/dev/null || {
        echo "错误：缺少命令 ${command}" >&2
        exit 1
    }
done
[[ -d "${APP_DIR}/.git" ]] || {
    echo "错误：${APP_DIR} 不是已克隆的 Git 仓库。" >&2
    exit 1
}

echo "===== 创建低权限服务账户和目录 ====="
id "${SERVICE_USER}" >/dev/null 2>&1 ||
    useradd --system --home-dir "${DATA_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${DATA_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 /var/log/lzz-config
install -d -o root -g root -m 0750 /var/backups/lzz-config

if [[ ! -f "${ENV_FILE}" ]]; then
    install -o root -g "${SERVICE_USER}" -m 0640 "${APP_DIR}/.env.example" "${ENV_FILE}"
    echo "已创建 ${ENV_FILE}。请填写必填项后重新运行本脚本。" >&2
    exit 2
fi
chown root:"${SERVICE_USER}" "${ENV_FILE}"
chmod 0640 "${ENV_FILE}"
set -a
source "${ENV_FILE}"
set +a
export LAINGZIZHIWANG_DATA_DIR="${LAINGZIZHIWANG_DATA_DIR:-${DATA_DIR}}"

echo "===== 检查 Node.js 运行环境 ====="
NODE_HOME="${NODE_HOME:-/opt/lzz-node}"
node_major="$(node --version | tr -d v | cut -d. -f1)"
if (( node_major < 20 )); then
    tmp_node="$(mktemp -d)"
    trap 'rm -rf "${tmp_node}"' EXIT
    checksum_line="$(curl -fsSL https://nodejs.org/dist/latest-v20.x/SHASUMS256.txt | awk '$2 ~ /linux-x64.tar.xz$/ {print; exit}')"
    node_archive="${checksum_line##* }"
    [[ -n "${node_archive}" ]] || { echo "无法解析 Node.js 20 下载信息" >&2; exit 1; }
    curl -fsSL "https://nodejs.org/dist/latest-v20.x/${node_archive}" -o "${tmp_node}/${node_archive}"
    (cd "${tmp_node}" && echo "${checksum_line}" | sha256sum -c -)
    rm -rf "${NODE_HOME}"
    mkdir -p "${NODE_HOME}"
    tar -xJf "${tmp_node}/${node_archive}" --strip-components=1 -C "${NODE_HOME}"
else
    NODE_HOME="$(dirname "$(dirname "$(command -v node)")")"
fi
export PATH="${NODE_HOME}/bin:${PATH}"
node --version

echo "===== 安装应用依赖 ====="
"${NODE_HOME}/bin/npm" --prefix "${APP_DIR}" ci
if [[ "${VENDOR_SYNC_ENABLED:-false}" == "true" ]]; then
    export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/opt/lzz-playwright}"
    if command -v dnf >/dev/null; then
        dnf install -y atk at-spi2-atk libX11 libxcb libXcomposite libXdamage libXext libXfixes \
            libXrandr mesa-libgbm alsa-lib cairo pango cups-libs nss nspr libdrm
    fi
    PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=180000 \
        "${APP_DIR}/node_modules/.bin/playwright" install chromium --no-shell
    chmod -R a+rX "${PLAYWRIGHT_BROWSERS_PATH}"
fi

echo "===== 安装 systemd 和日志轮转配置 ====="
install -o root -g root -m 0644 "${APP_DIR}/server/lzz-config.service" /etc/systemd/system/lzz-config.service
install -o root -g root -m 0644 "${APP_DIR}/server/logrotate.conf" /etc/logrotate.d/lzz-config
chmod 0755 "${APP_DIR}/server/update.sh" "${APP_DIR}/server/backup.sh"
chmod 0755 "${APP_DIR}/server/health_check.py" "${APP_DIR}/server/verify_backup.py"
chmod 0755 "${APP_DIR}/server/vendor_sync.sh" "${APP_DIR}/server/vendor_scrape.js"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${DATA_DIR}" /var/log/lzz-config
systemctl daemon-reload
systemctl enable lzz-config.service

echo "===== 安装定时任务 ====="
cat >/etc/cron.d/lzz-config <<EOF
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
* * * * * ${SERVICE_USER} /bin/bash -c 'set -a; source ${ENV_FILE}; /usr/bin/python3 ${APP_DIR}/server/check_monitor.py' >>/var/log/lzz-config/monitor.log 2>&1
*/5 * * * * ${SERVICE_USER} /bin/bash -c 'set -a; source ${ENV_FILE}; /usr/bin/python3 ${APP_DIR}/server/health_check.py --notify' >>/var/log/lzz-config/health.log 2>&1
10 8 * * * ${SERVICE_USER} ${APP_DIR}/server/vendor_sync.sh scheduled >>/var/log/lzz-config/vendor.log 2>&1
EOF
chmod 0644 /etc/cron.d/lzz-config

echo "===== 启动并进行冒烟检查 ====="
systemctl restart lzz-config.service
sleep 2
if ! runuser -u "${SERVICE_USER}" -- /usr/bin/python3 "${APP_DIR}/server/health_check.py"; then
    journalctl -u lzz-config.service -n 50 --no-pager >&2
    echo "部署检查失败；修正 ${ENV_FILE} 后执行：systemctl restart lzz-config && python3 ${APP_DIR}/server/health_check.py" >&2
    exit 1
fi

nginx -t
systemctl reload nginx
echo "部署完成。日常更新请执行：sudo ${APP_DIR}/server/update.sh"
