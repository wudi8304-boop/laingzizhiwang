#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置同步后端服务
提供邮箱列表 / 监控配置的读写 API，使用 JSON 文件持久化，实现跨设备同步。

接口：
  GET  /api/emails   读取邮箱列表
  POST /api/emails   保存邮箱列表  body: {"data": [...]}
  GET  /api/monitor  读取监控配置
  POST /api/monitor  保存监控配置  body: {"data": {...}}
  GET  /api/approved-programs   读取备案完成名单
  POST /api/approved-programs   合并备案完成名单  body: {"data": [...]}

鉴权：请求需携带 X-Auth 头，值为 ACCESS_TOKEN（默认 wudi2026，与前端登录密码一致）。

部署：
  监听 127.0.0.1:8091，由 Nginx 反向代理 /api/ 转发到本服务。
  数据目录：/www/laingzizhiwang-data/
  systemd 常驻运行。
"""
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn


# 兼容 Python 3.6 及更早版本（无 ThreadingHTTPServer）
class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    pass

# ============ 配置 ============
HOST = '127.0.0.1'
PORT = 8091
ACCESS_TOKEN = 'wudi2026'
DATA_DIR = '/www/laingzizhiwang-data'
EMAILS_FILE = os.path.join(DATA_DIR, 'emails.json')
MONITOR_FILE = os.path.join(DATA_DIR, 'monitor.json')
RECORDS_FILE = os.path.join(DATA_DIR, 'records.json')   # 检测历史记录
APPROVED_PROGRAMS_FILE = os.path.join(DATA_DIR, 'approved_programs.json')

# ============ 工具函数 ============
def ensure_data_dir():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)

def read_json(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        log('读取失败 %s: %s' % (path, e))
        return default

def write_json(path, data):
    ensure_data_dir()
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def read_approved_programs():
    """读取完成名单；首次上线时从历史检测记录回填"""
    if os.path.exists(APPROVED_PROGRAMS_FILE):
        data = read_json(APPROVED_PROGRAMS_FILE, [])
        return data if isinstance(data, list) else []
    monitor = read_json(MONITOR_FILE, {})
    company_full_names = {}
    if isinstance(monitor, dict):
        for company in (monitor.get('companies') or []):
            if not isinstance(company, dict):
                continue
            short_name = str(company.get('name') or '').strip()
            full_name = str(company.get('fullName') or '').strip()
            if short_name:
                company_full_names[short_name] = full_name
    records = read_json(RECORDS_FILE, {})
    result = []
    if not isinstance(records, dict):
        return result
    for company, record in records.items():
        items = record.get('items', []) if isinstance(record, dict) else []
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                name = str(item.get('name') or '').strip()
            elif isinstance(item, (list, tuple)) and item:
                name = str(item[0] or '').strip()
            else:
                name = ''
            if name:
                result.append({
                    'companyName': company,
                    'companyFullName': company_full_names.get(company, ''),
                    'miniProgramName': name,
                    'approvedAt': record.get('lastCheck') or ''
                })
    return result

def log(msg):
    import sys
    print('[%s] %s' % (log_time(), msg), flush=True)

def log_time():
    from datetime import datetime
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

# ============ HTTP 处理 ============
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # 静默默认日志，用自定义 log
        pass

    def _token(self):
        return self.headers.get('X-Auth', '')

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            return json.loads(raw.decode('utf-8'))
        except Exception as e:
            log('解析body失败: %s' % e)
            return None

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith('/api/'):
            self._send(404, {'error': 'not found'}); return
        if self._token() != ACCESS_TOKEN:
            self._send(403, {'error': 'forbidden'}); return
        if path == '/api/emails':
            self._send(200, {'data': read_json(EMAILS_FILE, [])})
        elif path == '/api/monitor':
            self._send(200, {'data': read_json(MONITOR_FILE, None)})
        elif path == '/api/records':
            self._send(200, {'data': read_json(RECORDS_FILE, {})})
        elif path == '/api/approved-programs':
            self._send(200, {'data': read_approved_programs()})
        else:
            self._send(404, {'error': 'unknown endpoint'})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith('/api/'):
            self._send(404, {'error': 'not found'}); return
        if self._token() != ACCESS_TOKEN:
            self._send(403, {'error': 'forbidden'}); return
        body = self._read_body()
        if body is None:
            self._send(400, {'error': 'invalid json'}); return
        data = body.get('data', body)
        if path == '/api/emails':
            write_json(EMAILS_FILE, data)
            log('保存邮箱列表 %d 条' % (len(data) if isinstance(data, list) else 0))
            self._send(200, {'ok': True})
        elif path == '/api/monitor':
            write_json(MONITOR_FILE, data)
            comps = data.get('companies', []) if isinstance(data, dict) else []
            times = data.get('times', []) if isinstance(data, dict) else []
            log('保存监控配置 公司%d 时间%s' % (len(comps), times))
            self._send(200, {'ok': True})
        elif path == '/api/records':
            write_json(RECORDS_FILE, data)
            log('保存检测记录 %d 家公司' % (len(data) if isinstance(data, dict) else 0))
            self._send(200, {'ok': True})
        elif path == '/api/approved-programs':
            # 前端立即检测可能同时提交，仅做追加合并，不覆盖 cron 已写入的数据
            existing = read_approved_programs()
            incoming = data if isinstance(data, list) else []
            monitor = read_json(MONITOR_FILE, {})
            company_full_names = {}
            if isinstance(monitor, dict):
                for company in (monitor.get('companies') or []):
                    if not isinstance(company, dict):
                        continue
                    short_name = str(company.get('name') or '').strip()
                    full_name = str(company.get('fullName') or '').strip()
                    if short_name:
                        company_full_names[short_name] = full_name
            merged = {}
            for entry in existing + incoming:
                if not isinstance(entry, dict):
                    continue
                company = str(entry.get('companyName') or '').strip()
                full_name = str(entry.get('companyFullName') or company_full_names.get(company, '') or '').strip()
                name = str(entry.get('miniProgramName') or '').strip()
                if not name:
                    continue
                # 小程序名称全局唯一，按名称去重；公司信息仅用于追溯来源
                merged[name] = {
                    'companyName': company,
                    'companyFullName': full_name,
                    'miniProgramName': name,
                    'approvedAt': entry.get('approvedAt') or ''
                }
            result = list(merged.values())
            write_json(APPROVED_PROGRAMS_FILE, result)
            log('保存备案完成名单 %d 条' % len(result))
            self._send(200, {'ok': True, 'count': len(result)})
        else:
            self._send(404, {'error': 'unknown endpoint'})

def main():
    ensure_data_dir()
    log('配置服务启动 %s:%d 数据目录=%s' % (HOST, PORT, DATA_DIR))
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log('服务停止')
        srv.shutdown()

if __name__ == '__main__':
    main()
