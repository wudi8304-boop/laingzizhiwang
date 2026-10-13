#!/usr/bin/env python3
"""本地 API 健康检查；失败时可选发送钉钉机器人告警。"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def load_env_file(path):
    if not path or not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if value[:1] == value[-1:] and value[:1] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key, value)


def request_json(url, headers=None, data=None, timeout=5):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(url, data=body, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError("HTTP %s" % response.status)
        return json.loads(response.read().decode("utf-8"))


def dingtalk_config():
    webhook = os.environ.get("HEALTH_DINGTALK_WEBHOOK", "").strip()
    secret = os.environ.get("HEALTH_DINGTALK_SECRET", "").strip()
    if webhook:
        return webhook, secret
    data_dir = os.environ.get("LAINGZIZHIWANG_DATA_DIR", "/www/laingzizhiwang-data")
    db_path = os.environ.get("LAINGZIZHIWANG_DB", os.path.join(data_dir, "app.db"))
    try:
        import sqlite3
        with sqlite3.connect("file:%s?mode=ro" % db_path, uri=True) as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='monitor_dingtalk'").fetchone()
        value = json.loads(row[0]) if row else {}
        return str(value.get("webhook") or "").strip(), str(value.get("secret") or "").strip()
    except Exception:
        return "", ""


def notify_dingtalk(message):
    webhook, secret = dingtalk_config()
    if not webhook:
        return
    if secret:
        timestamp = str(int(time.time() * 1000))
        digest = hmac.new(
            secret.encode("utf-8"),
            ("%s\n%s" % (timestamp, secret)).encode("utf-8"),
            hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("ascii"))
        separator = "&" if "?" in webhook else "?"
        webhook += "%stimestamp=%s&sign=%s" % (separator, timestamp, sign)
    keyword = os.environ.get("HEALTH_DINGTALK_KEYWORD", "服务健康告警")
    content = "%s\n主机：%s\n%s" % (keyword, socket.gethostname(), message)
    request_json(
        webhook,
        headers={"Content-Type": "application/json"},
        data={"msgtype": "text", "text": {"content": content}},
        timeout=10,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--notify", action="store_true", help="失败时发送钉钉告警")
    args = parser.parse_args()
    load_env_file(os.environ.get("ENV_FILE", "/etc/lzz-config.env"))

    token = os.environ.get("ACCESS_TOKEN", "")
    url = os.environ.get("HEALTH_URL", "http://127.0.0.1:8091/api/health")
    headers = {"X-Auth": token} if token and not token.startswith("CHANGE_ME") else {}
    try:
        payload = request_json(url, headers=headers)
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        if not isinstance(data, dict) or not data.get("ok"):
            raise RuntimeError("响应未报告健康状态")
        print("健康检查通过：%s" % url)
        return 0
    except Exception as exc:
        error = "健康检查失败：%s (%s)" % (url, exc)

    print(error, file=sys.stderr)
    if args.notify:
        try:
            notify_dingtalk(error)
        except Exception as exc:
            print("钉钉告警发送失败：%s" % exc, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
