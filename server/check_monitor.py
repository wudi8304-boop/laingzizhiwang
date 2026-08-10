#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定时监控检测脚本（由 cron 每分钟调用）

工作流程：
1. 读取监控配置 monitor.json
2. 判断当前 HH:MM 是否命中 cfg.times
3. 防重复：同一 (日期, 时间点) 只执行一次（last_run.json）
4. 对每家启用的公司检测一次（成功失败都只检测1次，失败不重试不扣费）
5. 发现新增小程序时推送钉钉通知（含名称+备案号）
6. 回写 monitor.json（lastCount/lastCheck/hasNew/lastKeys）

cron 配置（每分钟执行）：
  * * * * * /usr/bin/python3 /www/laingzizhiwang-site/server/check_monitor.py >> /www/monitor-check.log 2>&1
"""
import json
import os
import sys
import time
import hmac
import hashlib
import base64
import urllib.parse
import urllib.request
from datetime import datetime

# ============ 配置 ============
DATA_DIR = '/www/laingzizhiwang-data'
MONITOR_FILE = os.path.join(DATA_DIR, 'monitor.json')
LAST_RUN_FILE = os.path.join(DATA_DIR, 'last_run.json')
APIHZ_URL = 'https://cn.apihz.cn/api/wangzhan/syicpxcx.php'
DINGTALK_PROXY = 'http://127.0.0.1:8080/ding-proxy/robot/send'

# ============ 工具函数 ============
def log(msg):
    print('[%s] %s' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg), flush=True)

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
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def http_get(url, timeout=20):
    req = urllib.request.Request(url, method='GET')
    req.add_header('User-Agent', 'laingzizhiwang-monitor/1.0')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))

def http_post_json(url, payload, timeout=20):
    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    req.add_header('User-Agent', 'laingzizhiwang-monitor/1.0')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))

# ============ 钉钉加签 ============
def ding_sign(secret, timestamp):
    string_to_sign = '%s\n%s' % (timestamp, secret)
    hmac_code = hmac.new(secret.encode('utf-8'), string_to_sign.encode('utf-8'), digestmod=hashlib.sha256).digest()
    return urllib.parse.quote_plus(base64.b64encode(hmac_code).decode('utf-8'))

def send_dingtalk(ding, content):
    """通过 Nginx 反代 /ding-proxy/ 发送钉钉消息"""
    if not ding or not ding.get('webhook'):
        return False, '未配置 Webhook'
    keyword = ding.get('keyword') or '备案监控'
    text = content if keyword in content else ('%s %s' % (keyword, content))
    # 提取 access_token
    m = None
    try:
        import re
        m = re.search(r'access_token=([^&]+)', ding['webhook'])
    except Exception:
        pass
    if not m:
        return False, 'Webhook中未找到access_token'
    access_token = m.group(1)
    url = '%s?access_token=%s' % (DINGTALK_PROXY, access_token)
    try:
        if ding.get('secret'):
            timestamp = str(int(time.time() * 1000))
            sign = ding_sign(ding['secret'], timestamp)
            url += '&timestamp=%s&sign=%s' % (timestamp, sign)
        data = http_post_json(url, {'msgtype': 'text', 'text': {'content': text}})
        if data.get('errcode') == 0:
            return True, '发送成功'
        return False, data.get('errmsg') or ('errcode:' + str(data.get('errcode')))
    except Exception as e:
        return False, '网络错误: %s' % e

# ============ apihz 查询 ============
def query_apihz(apihz, main_name):
    """返回 (success, total, list, msg)"""
    if not main_name:
        return False, 0, [], '未填写主体全称'
    params = {
        'id': apihz.get('id', ''),
        'key': apihz.get('key', ''),
        'main': main_name,
        'hctype': '1',
        'page': '1'
    }
    url = APIHZ_URL + '?' + urllib.parse.urlencode(params)
    try:
        data = http_get(url, timeout=20)
        if data.get('code') == 200:
            lst = data.get('datas') if isinstance(data.get('datas'), list) else []
            total = data.get('total') if data.get('total') is not None else len(lst)
            return True, total, lst, 'ok'
        return False, 0, [], data.get('msg') or '查询失败'
    except Exception as e:
        return False, 0, [], '网络错误: %s' % e

def extract_item(d):
    """从 apihz 返回项提取 (name, icp)"""
    name = d.get('name') or d.get('xcxname') or d.get('title') or '未命名'
    icp = d.get('icp') or d.get('beian') or d.get('record') or ''
    return name, icp

# ============ 主流程 ============
def main():
    cfg = read_json(MONITOR_FILE, None)
    if not cfg:
        log('无监控配置，退出')
        return

    # 判断当前时间是否命中配置的检测时间
    now = datetime.now()
    current_hm = now.strftime('%H:%M')
    today = now.strftime('%Y-%m-%d')
    times = cfg.get('times', []) or []
    if current_hm not in times:
        return  # 当前分钟不在检测时间，静默退出

    # 防重复：同一 (日期, 时间点) 只执行一次
    last = read_json(LAST_RUN_FILE, {})
    if last.get('date') == today and last.get('time') == current_hm:
        return  # 今天该时间点已执行过
    log('命中检测时间 %s，开始执行' % current_hm)

    apihz = cfg.get('apihz', {}) or {}
    if not apihz.get('id') or not apihz.get('key'):
        log('未配置 apihz id/key，退出')
        return

    companies = cfg.get('companies', []) or []
    targets = [c for c in companies if c.get('enabled') and (c.get('fullName') or c.get('name'))]
    if not targets:
        log('无启用的公司，退出')
        return

    log('开始检测 %d 家公司' % len(targets))
    new_lines = []
    has_any_new = False

    for c in targets:
        main_name = c.get('fullName') or c.get('name', '')
        ok, total, lst, msg = query_apihz(apihz, main_name)
        now_str = now.strftime('%Y-%m-%d %H:%M:%S')

        if ok:
            prev = c.get('lastCount')
            prev_keys = c.get('lastKeys') if isinstance(c.get('lastKeys'), list) else []
            cur_items = [extract_item(d) for d in lst]
            cur_keys = ['%s|%s' % (n, i) for (n, i) in cur_items]
            c['lastCount'] = total
            c['lastKeys'] = cur_keys
            c['lastCheck'] = now_str
            if prev is not None and total > prev:
                # 找出新增明细
                added = [it for it in cur_items if ('%s|%s' % (it[0], it[1])) not in prev_keys]
                c['hasNew'] = True
                has_any_new = True
                new_lines.append('【%s】新增 %d 个小程序：' % (c.get('name') or main_name, len(added)))
                for idx, (n, i) in enumerate(added, 1):
                    new_lines.append('  %d. %s%s' % (idx, n, ('（%s）' % i) if i else ''))
            elif prev is None:
                c['hasNew'] = False
            log('  %s: 查询成功，当前%d个' % (c.get('name') or main_name, total))
        else:
            # 失败也记录时间，不更新数量，不算新增，不重试
            c['lastCheck'] = now_str + ' 失败'
            log('  %s: 查询失败 - %s' % (c.get('name') or main_name, msg))
        time.sleep(2)  # 请求间隔，避免触发频控

    # 回写配置
    write_json(MONITOR_FILE, cfg)
    log('监控配置已回写')

    # 有新增则推送钉钉（含名称和备案号）
    if has_any_new:
        keyword = cfg.get('dingtalk', {}).get('keyword') or '备案监控'
        content = '【%s】发现新增小程序备案！\n检测时间：%s\n\n%s\n\n请及时登录系统查看详情。' % (
            keyword,
            now.strftime('%Y-%m-%d %H:%M:%S'),
            '\n'.join(new_lines)
        )
        ding = cfg.get('dingtalk', {}) or {}
        ok, m = send_dingtalk(ding, content)
        if ok:
            log('钉钉通知已推送')
        else:
            log('钉钉通知失败: %s' % m)
    else:
        log('检测完成，无新增')

    # 记录本次执行（无论是否有新增）
    write_json(LAST_RUN_FILE, {'date': today, 'time': current_hm})
    log('执行完成，标记 %s %s 已执行' % (today, current_hm))

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        log('脚本异常: %s' % e)
        import traceback
        traceback.print_exc()
        sys.exit(1)
