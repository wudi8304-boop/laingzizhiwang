# 来自知网部署与运维

## 首次部署

前提：Linux 服务器已安装 `git`、`python3`、`nginx`、`curl`，仓库位于
`/www/laingzizhiwang`。部署脚本不会在拉取失败后继续运行，首次部署也不会自动写入任何真实密钥。

```bash
sudo bash /www/laingzizhiwang/server/deploy.sh
# 首次会生成配置模板并退出
sudo editor /etc/lzz-config.env
sudo bash /www/laingzizhiwang/server/deploy.sh
```

脚本会创建无登录权限的 `lzz` 用户、安装 systemd 服务、健康检查任务和日志轮转；
不会创建定时备份任务。
`/etc/lzz-config.env` 权限为 `root:lzz 0640`。

至少填写 `ACCESS_TOKEN`、`APP_PASSWORD` 和随机生成的 `SESSION_SECRET`。浏览器使用
HttpOnly 会话登录，健康检查使用 `ACCESS_TOKEN`；所有真实值只保存在 `/etc/lzz-config.env`。

首次启动会创建总管理员账号 `admin`，初始密码取 `APP_PASSWORD`。之后由总管理员在“公司列表”
创建、停用或重置分级管理员，并把多个公司分配给账号。一个公司最多归一个分级管理员；
分级管理员只可访问公司列表、名下小程序明细和名下小程序正在使用的邮箱。

## 日常更新与回滚

```bash
sudo /www/laingzizhiwang/server/update.sh
```

更新过程依次执行：确认工作区干净、创建数据备份、`git pull --ff-only`、重启服务、API 冒烟。
任何 pull 错误都会立即停止。健康检查失败时脚本会输出包含更新前 commit 的回滚命令，但不会擅自执行
`git reset --hard`。先查看：

```bash
sudo journalctl -u lzz-config.service -n 100 --no-pager
```

## 备份

系统不默认执行定时备份。如需手工备份，JSON 等普通数据会归档；发现 `.db`、`.sqlite`、
`.sqlite3` 时使用 SQLite 在线备份并执行 `PRAGMA integrity_check`：

```bash
sudo /www/laingzizhiwang/server/backup.sh --manual
cd /var/backups/lzz-config
sha256sum -c <备份文件名>.sha256
sudo /usr/bin/python3 /www/laingzizhiwang/server/verify_backup.py
```

启用 OSS 前安装 `ossutil`，并在环境文件中设置 `OSS_ENABLED=true`、目标路径、Endpoint 和访问密钥。
建议为该密钥配置仅可写指定备份前缀的最小权限。

## 健康与日志

```bash
sudo /usr/bin/python3 /www/laingzizhiwang/server/health_check.py
sudo /usr/bin/python3 /www/laingzizhiwang/server/health_check.py --notify
sudo logrotate -d /etc/logrotate.d/lzz-config
```

健康检查每 5 分钟运行一次；未单独配置健康告警机器人时，会复用数据库中的监控机器人。
应用日志查看 `journalctl -u lzz-config`；定时任务日志位于 `/var/log/lzz-config/`。

监控配置页可开启 APIHZ 每日签到。系统复用每分钟监控入口，在服务器本地时间 `00:02`
执行签到，成功后查询并保存盟点余额；关闭开关时不会调用签到或余额接口。同日成功后不会
重复签到，失败会进入异常工作台并按现有钉钉规则通知。

小程序编辑页的“刷新换邮箱”会在服务端事务内随机选择可用且未绑定的新邮箱，换绑成功后
将旧邮箱标记为“不能使用”。没有候选邮箱时不会改变原绑定；分级管理员只能操作名下小程序，
总管理员可在邮箱列表集中删除失效邮箱。

## Nginx 与 HTTPS

不要把示例域名直接上线。先让 DNS 指向服务器，再显式提供真实域名。以下命令可执行地生成 HTTP
配置，随后由 Certbot 申请证书并改为 HTTPS：

```bash
export DOMAIN='填写已解析到本机的域名'
test -n "$DOMAIN" && test "$DOMAIN" != '填写已解析到本机的域名'

sudo tee /etc/nginx/conf.d/laingzizhiwang.conf >/dev/null <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};
    root /www/laingzizhiwang;
    index index.html;

    location / {
        try_files \$uri \$uri/ =404;
    }
    location /api/ {
        proxy_pass http://127.0.0.1:8091;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Auth \$http_x_auth;
    }
    location /ding-proxy/ {
        allow 127.0.0.1;
        allow ::1;
        deny all;
        proxy_pass https://oapi.dingtalk.com/;
        proxy_ssl_server_name on;
        proxy_set_header Host oapi.dingtalk.com;
    }
}
EOF
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d "$DOMAIN" --redirect
sudo certbot renew --dry-run
```

请在安全组/防火墙开放 80 和 443，并关闭不再使用的公网 8080 入口。

## GitHub Actions 自动部署

工作流在推送 `main` 或手工触发时，通过 SSH 执行安全更新脚本。先在 GitHub
`production` Environment 中配置 Secrets：

- `DEPLOY_HOST`：服务器地址
- `DEPLOY_USER`：仅允许部署的 SSH 用户
- `DEPLOY_PORT`：SSH 端口
- `DEPLOY_SSH_KEY`：专用私钥
- `DEPLOY_HOST_FINGERPRINT`：服务器 SSH 主机指纹

部署用户只需被允许无密码执行这一条 sudo 命令：

```text
DEPLOY_USER ALL=(root) NOPASSWD: /www/laingzizhiwang/server/update.sh
```

服务器仓库应提前配置只读 Deploy Key。不要把私钥、Token、Webhook 或 OSS 密钥写入仓库。

## 乙方平台自动同步

服务器 Node.js 需为 20 或更高版本；`deploy.sh` 会在 `/opt/lzz-node` 安装并校验 Node.js 20，
不会替换系统 Node.js。先在 `/etc/lzz-config.env` 填写：

```text
VENDOR_BASE_URL=https://xcx.qn76.cn/
VENDOR_USERNAME=填写账号
VENDOR_PASSWORD=填写密码
VENDOR_SYNC_ENABLED=true
PLAYWRIGHT_BROWSERS_PATH=/opt/lzz-playwright
```

重新运行 `deploy.sh` 会安装 Playwright Chromium。服务器每天 08:10 检查一次，只有达到页面
设置的间隔天数才登录乙方平台；立即同步只处理新增企业，立即匹配只补现有记录的空字段。
同步会读取头像 URL、小程序名称、简介和类目；乙方邮箱为空时，从邮箱池中随机分配一个尚未
被任何小程序使用的邮箱。邮箱池耗尽不会中断同步，该小程序会进入待补资料状态。

## 历史数据路径下线

运行时数据只来自 SQLite。`data.js` 已清空且不再由页面加载，`/api/records` 和
`/api/data-records` 旧写入接口已关闭。迁移前的 JSON 只保留在受限备份目录，不再参与运行。
如果登录后不是数据列表页，填写 `VENDOR_LIST_URL`；页面控件不同可通过 `.env.example`
中的选择器变量调整。手工验证：

```bash
sudo -u lzz /www/laingzizhiwang/server/vendor_sync.sh
sudo tail -n 100 /var/log/lzz-config/vendor.log
```

首次启用前建议保持 `VENDOR_SYNC_ENABLED=false`，手工验证页面选择器和同步预览后再开启。
