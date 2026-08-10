#!/bin/bash
# 一键部署脚本：修复Python兼容 + 启动后端 + 修复nginx + 配cron
# 用法：bash /www/laingzizhiwang/server/deploy.sh
set -e

echo "===== 0. 拉取最新代码 ====="
cd /www/laingzizhiwang
git pull || echo "git pull失败，继续"

echo "===== 1. 创建数据目录 ====="
mkdir -p /www/laingzizhiwang-data

echo "===== 2. 创建 systemd 服务 ====="
cat > /etc/systemd/system/lzz-config.service <<'UNIT'
[Unit]
Description=Laingzizhiwang Config Server
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /www/laingzizhiwang/server/config_server.py
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable lzz-config
systemctl restart lzz-config
sleep 2

echo "===== 3. 检查后端服务状态 ====="
if systemctl is-active --quiet lzz-config; then
    echo "✓ 后端服务运行中"
else
    echo "✗ 后端服务启动失败，错误日志："
    journalctl -u lzz-config -n 10 --no-pager
    echo ""
    echo "尝试诊断 Python 版本："
    python3 --version
    echo "尝试手动运行："
    timeout 3 python3 /www/laingzizhiwang/server/config_server.py 2>&1 || true
fi

echo "===== 4. 测试后端 API ====="
RESP=$(curl -s -H "X-Auth: wudi2026" http://127.0.0.1:8091/api/monitor)
if [ -n "$RESP" ]; then
    echo "✓ 后端API响应: $RESP"
else
    echo "✗ 后端API无响应"
fi

echo "===== 5. 写入 nginx 配置 ====="
cat > /etc/nginx/conf.d/laingzizhiwang.conf <<'NGINX'
server {
    listen 8080;
    server_name _;
    root /www/laingzizhiwang;
    index index.html;
    location / {
        try_files $uri $uri/ =404;
    }
    location /api/ {
        proxy_pass http://127.0.0.1:8091;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Auth $http_x_auth;
    }
    location /ding-proxy/ {
        proxy_pass https://oapi.dingtalk.com/;
        proxy_ssl_server_name on;
        proxy_set_header Host oapi.dingtalk.com;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Content-Type "application/json";
    }
}
NGINX

echo "===== 6. 测试并重载 nginx ====="
if nginx -t; then
    systemctl reload nginx
    echo "✓ nginx 已重载"
else
    echo "✗ nginx 配置错误"
fi

echo "===== 7. 配置 cron 定时检测 ====="
( crontab -l 2>/dev/null | grep -v check_monitor; echo "* * * * * /usr/bin/python3 /www/laingzizhiwang/server/check_monitor.py >> /www/monitor-check.log 2>&1" ) | crontab -
echo "✓ cron 配置完成"

echo "===== 8. 最终验证 ====="
echo "--- nginx 配置 ---"
cat /etc/nginx/conf.d/laingzizhiwang.conf
echo ""
echo "--- 后端服务 ---"
systemctl status lzz-config --no-pager | head -3
echo ""
echo "--- 后端 API (直连) ---"
curl -s -H "X-Auth: wudi2026" http://127.0.0.1:8091/api/monitor
echo ""
echo "--- 后端 API (经nginx) ---"
curl -s -H "X-Auth: wudi2026" http://127.0.0.1:8080/api/monitor
echo ""
echo "--- cron ---"
crontab -l | grep check_monitor
echo ""
echo "===== 部署完成 ====="
