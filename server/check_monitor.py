#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定时监控检测脚本（由 cron 每分钟调用）

工作流程：
1. 读取监控配置 monitor.json
2. 判断当前 HH:MM 是否命中 cfg.times
3. 防重复：同一 (日期, 时间点) 只执行一次（last_run.json）
4. 读取 records.json（每家公司的上次检测明细）
5. 对每家启用的公司检测一次，失败跳过，全部完成后对失败的再查一次
6. 比对本次结果与 records.json 中的上次明细，找出新增小程序
7. 有新增则推送钉钉通知（含名称+备案号）
8. 更新 records.json（本次明细作为下次的比对基准）

新公司默认 items 为空，首次检测出的全部小程序都算新增。

cron 配置（每分钟执行）：
  * * * * * /usr/bin/python3 /www/laingzizhiwang/server/check_monitor.py >> /www/monitor-check.log 2>&1
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
RECORDS_FILE = os.path.join(DATA_DIR, 'records.json')   # {公司名: {items: [...], lastCheck: '', lastCount: 0}}
APPROVED_PROGRAMS_FILE = os.path.join(DATA_DIR, 'approved_programs.json')
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
    """从 apihz 返回项提取统一结构 {name, icp}
    - servicename: 小程序名称
    - icpw: 应用备案号（优先）；icp 是主体备案号，仅作兜底
    """
    name = d.get('servicename') or d.get('name') or d.get('xcxname') or d.get('title') or d.get('appname') or d.get('app_name') or d.get('nickName') or d.get('nickname') or d.get('miniProgramName') or ''
    icp = d.get('icpw') or d.get('icp') or d.get('beian') or d.get('record') or d.get('beianhao') or d.get('icpNo') or ''
    return {'name': str(name or '').strip(), 'icp': str(icp or '').strip()}

def normalize_item(it):
    """兼容历史两种格式：{"name","icp"} 与 [name, icp]"""
    if isinstance(it, dict):
        return {'name': str(it.get('name') or '').strip(), 'icp': str(it.get('icp') or '').strip()}
    if isinstance(it, (list, tuple)) and len(it) >= 2:
        return {'name': str(it[0] or '').strip(), 'icp': str(it[1] or '').strip()}
    return {'name': '', 'icp': ''}

def item_identity(it):
    """比对身份：优先小程序名称，避免主体/应用备案号字段切换导致重复误报"""
    item = normalize_item(it)
    if item['name']:
        return 'name:' + item['name']
    if item['icp']:
        return 'icp:' + item['icp']
    return ''

def update_approved_programs(entries):
    """合并备案通过名单，供数据管理页面自动更新状态"""
    if not entries:
        return 0
    cfg = read_json(MONITOR_FILE, {})
    company_full_names = {}
    if isinstance(cfg, dict):
        for company in (cfg.get('companies') or []):
            if not isinstance(company, dict):
                continue
            short_name = str(company.get('name') or '').strip()
            full_name = str(company.get('fullName') or '').strip()
            if short_name:
                company_full_names[short_name] = full_name
    current = read_json(APPROVED_PROGRAMS_FILE, [])
    if not isinstance(current, list):
        current = []
    # 首次启用该功能时，用历史成功检测记录回填，避免必须等下一轮全部查询成功
    if not os.path.exists(APPROVED_PROGRAMS_FILE):
        legacy_records = read_json(RECORDS_FILE, {})
        if isinstance(legacy_records, dict):
            for company, record in legacy_records.items():
                items = record.get('items', []) if isinstance(record, dict) else []
                if not isinstance(items, list):
                    continue
                for it in items:
                    item = normalize_item(it)
                    if item['name']:
                        current.append({
                            'companyName': company,
                            'companyFullName': company_full_names.get(company, ''),
                            'miniProgramName': item['name'],
                            'approvedAt': record.get('lastCheck') or ''
                        })
    merged = {}
    for entry in current + entries:
        if not isinstance(entry, dict):
            continue
        company = str(entry.get('companyName') or '').strip()
        full_name = str(entry.get('companyFullName') or company_full_names.get(company, '') or '').strip()
        name = str(entry.get('miniProgramName') or '').strip()
        if not name:
            continue
        # 小程序名称全局唯一，按名称去重；公司信息仅用于追溯来源
        key = name
        merged[key] = {
            'companyName': company,
            'companyFullName': full_name,
            'miniProgramName': name,
            'approvedAt': entry.get('approvedAt') or ''
        }
    write_json(APPROVED_PROGRAMS_FILE, list(merged.values()))
    return len(merged)

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
    # 兼容 HH:MM:SS（部分浏览器 time 输入会带秒），统一成 HH:MM 再比对
    times = []
    for t in (cfg.get('times', []) or []):
        s = str(t).strip()
        if len(s) >= 5 and s[2] == ':':
            s = s[:5]
        if s and s not in times:
            times.append(s)
    if current_hm not in times:
        return  # 当前分钟不在检测时间，静默退出

    # 防重复：同一 (日期, 时间点) 只执行一次
    last = read_json(LAST_RUN_FILE, {})
    if last.get('date') == today and last.get('time') == current_hm:
        return
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

    # 读取历史检测记录（每家公司的上次 items）
    records = read_json(RECORDS_FILE, {})
    if not isinstance(records, dict):
        records = {}

    log('开始检测 %d 家公司' % len(targets))
    new_lines = []
    state = {'has_new': False, 'approved': []}  # 用 dict 包装，避免 nonlocal 作用域问题

    def handle_result(c, ok, total, lst, now_str):
        """处理单次检测结果，返回 True=成功 False=失败"""
        main_name = c.get('fullName') or c.get('name', '')
        cname = c.get('name') or main_name
        if ok:
            # 提取本次所有小程序（统一为 {name, icp}）
            cur_items = [extract_item(d) for d in lst]
            # 查询结果均为已备案小程序，加入备案完成名单
            for it in cur_items:
                if it.get('name'):
                    state['approved'].append({
                        'companyName': cname,
                        'companyFullName': main_name,
                        'miniProgramName': it['name'],
                        'approvedAt': now_str
                    })
            # 读取上次记录（兼容旧版 [name, icp] 与前端 {name, icp}）
            prev_rec = records.get(cname, {})
            prev_items = prev_rec.get('items', []) if isinstance(prev_rec, dict) else []
            if not isinstance(prev_items, list):
                prev_items = []
            prev_keys = set()
            for it in prev_items:
                k = item_identity(it)
                if k:
                    prev_keys.add(k)
            # 找出新增（本次有但上次没有）
            added = []
            for it in cur_items:
                k = item_identity(it)
                if k and k not in prev_keys:
                    added.append(it)
            if added:
                state['has_new'] = True
                new_lines.append('【%s】新增 %d 个小程序：' % (cname, len(added)))
                for idx, it in enumerate(added, 1):
                    label = it.get('name') or it.get('icp') or '未命名'
                    icp = it.get('icp') or ''
                    new_lines.append('  %d. %s%s' % (idx, label, ('（%s）' % icp) if icp else ''))
            # 更新记录（本次作为下次的基准，始终写对象格式）
            records[cname] = {'items': cur_items, 'lastCheck': now_str, 'lastCount': total}
            # 同步更新 monitor.json 里的显示状态
            c['lastCount'] = total
            c['lastCheck'] = now_str
            c['hasNew'] = len(added) > 0
            log('  %s: 查询成功，当前%d个，新增%d个' % (cname, total, len(added)))
            return True
        else:
            return False  # 失败：不更新记录，等待重试

    # 第一轮：逐个检测，失败的加入重试列表
    failed_list = []
    for c in targets:
        main_name = c.get('fullName') or c.get('name', '')
        ok, total, lst, msg = query_apihz(apihz, main_name)
        now_str = now.strftime('%Y-%m-%d %H:%M:%S')
        success = handle_result(c, ok, total, lst, now_str)
        if not success:
            failed_list.append((c, msg))
        if c is not targets[-1]:
            time.sleep(2)

    # 第二轮：对失败的公司重新查一次
    if failed_list:
        log('第一轮 %d 家失败，2秒后重试...' % len(failed_list))
        time.sleep(2)
        still_failed = []
        for c, _ in failed_list:
            main_name = c.get('fullName') or c.get('name', '')
            ok, total, lst, msg = query_apihz(apihz, main_name)
            now_str = now.strftime('%Y-%m-%d %H:%M:%S')
            success = handle_result(c, ok, total, lst, now_str)
            if not success:
                still_failed.append(c.get('name') or main_name)
                c['lastCheck'] = now_str + ' 失败'
            if c is not failed_list[-1][0]:
                time.sleep(2)
        if still_failed:
            log('  仍失败：%s' % '、'.join(still_failed))

    # 回写 records.json（本次结果作为下次比对基准）
    write_json(RECORDS_FILE, records)
    log('检测记录已回写 records.json')

    # 回写 monitor.json（更新显示状态）
    write_json(MONITOR_FILE, cfg)
    log('监控配置已回写 monitor.json')

    # 将查询到的备案通过名称同步给数据管理页面
    approved_count = update_approved_programs(state['approved'])
    if state['approved']:
        log('备案完成名单已同步，共%d条' % approved_count)

    # 有新增则推送钉钉（含名称和备案号）
    if state['has_new']:
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

    # 记录本次执行
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
