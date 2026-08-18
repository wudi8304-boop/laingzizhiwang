#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""乙方平台同步：登录网页、读取列表并通过统一规则写入 SQLite。

真实凭据和页面选择器仅从环境变量读取。站点页面变化时，失败会写入异常工作台。
"""
import argparse
import json
import os
import re
import sys
import traceback
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import Database, now
from migrate_data import migrate
from services.programs import ProgramService


HEADER_MAP = {
    "小程序名称": "miniProgramName", "名称": "miniProgramName", "小程序名字": "miniProgramName",
    "appid": "appid", "app id": "appid", "小程序appid": "appid",
    "原始id": "originalId", "原始 ID": "originalId", "公司": "companyName", "主体": "companyName",
    "管理员": "admin", "邮箱": "email", "状态": "status", "简介": "description", "类目": "category",
}


def normalize_header(value):
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def mapped_record(headers, values):
    result = {}
    normalized_map = {normalize_header(k): v for k, v in HEADER_MAP.items()}
    for index, header in enumerate(headers):
        field = normalized_map.get(normalize_header(header))
        if field and index < len(values):
            result[field] = str(values[index] or "").strip()
    return result


def add_exception(db, reason, payload, program_id=None):
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO exceptions(program_id,company_name,mini_program_name,reason,status,payload,
               created_at,updated_at) VALUES(?,?,?,?,? ,?,?,?)""",
            (
                program_id, str(payload.get("companyName") or ""), str(payload.get("miniProgramName") or ""),
                reason, "open", json.dumps(payload, ensure_ascii=False), now(), now(),
            ),
        )


VENDOR_FIELDS = {
    "companyName", "miniProgramName", "avatarUrl", "description", "category",
    "appid", "originalId", "secret", "admin", "email", "externalId"
}


def random_unused_email(db):
    with db.connect() as conn:
        row = conn.execute(
            """SELECT e.address FROM emails e
               WHERE e.usable=1 AND trim(e.address)<>''
                 AND NOT EXISTS (
                   SELECT 1 FROM programs p
                   WHERE lower(trim(p.email))=lower(trim(e.address))
                 )
               ORDER BY RANDOM() LIMIT 1"""
        ).fetchone()
    return str(row["address"]).strip() if row else ""


def upsert_records(db, records, source_ref="", new_companies_only=True):
    service = ProgramService(db)
    with db.connect() as conn:
        known_company_names = {
            str(row["name"]).strip().casefold()
            for row in conn.execute("SELECT name FROM companies WHERE trim(name)<>''")
        }
        known_company_names.update(
            str(row["full_name"]).strip().casefold()
            for row in conn.execute("SELECT full_name FROM companies WHERE trim(full_name)<>''")
        )
    vendor_companies = {
        str(row.get("companyName") or "").strip()
        for row in records if isinstance(row, dict) and str(row.get("companyName") or "").strip()
    }
    new_companies = sorted(
        name for name in vendor_companies if name.casefold() not in known_company_names
    )
    selected = [
        row for row in records
        if not new_companies_only or str(row.get("companyName") or "").strip() in new_companies
    ]
    summary = {
        "total": len(records), "newCompanies": new_companies, "selected": len(selected),
        "created": 0, "updated": 0, "conflicts": 0, "skipped": 0,
        "emailsAssigned": 0, "emailsUnavailable": 0,
    }
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO import_jobs(source,status,source_ref,started_at) VALUES('vendor','running',?,?)",
            (source_ref, now()),
        )
        job_id = cur.lastrowid
    try:
        for raw in selected:
            item = {
                key: value for key, value in raw.items()
                if key in VENDOR_FIELDS and value not in (None, "")
            }
            name = str(item.get("miniProgramName") or "").strip()
            appid = str(item.get("appid") or "").strip()
            external_id = str(item.get("externalId") or "").strip()
            company_name = str(item.get("companyName") or "").strip()
            if not company_name or (not name and not appid):
                summary["skipped"] += 1
                continue
            with db.connect() as conn:
                matches = []
                if external_id:
                    matches = conn.execute("SELECT id FROM programs WHERE external_id=?", (external_id,)).fetchall()
                if not matches and appid:
                    matches = conn.execute("SELECT id FROM programs WHERE appid=?", (appid,)).fetchall()
                if not matches and name:
                    matches = conn.execute(
                        "SELECT id FROM programs WHERE trim(mini_program_name)=?", (name,)
                    ).fetchall()
            if len(matches) > 1:
                summary["conflicts"] += 1
                add_exception(db, "乙方平台记录匹配到多个小程序", item)
                continue
            if len(matches) == 1 and service.get(matches[0]["id"])["status"] == "已结算":
                summary["skipped"] += 1
                continue
            item["externalId"] = external_id
            item.setdefault("status", "待注册")
            if not matches and not str(item.get("email") or "").strip():
                assigned_email = random_unused_email(db)
                if assigned_email:
                    item["email"] = assigned_email
                    summary["emailsAssigned"] += 1
                else:
                    summary["emailsUnavailable"] += 1
            required = ("appid", "originalId", "secret", "admin", "email")
            if any(not str(item.get(field) or "").strip() for field in required):
                item["taskReason"] = "待乙方补全资料"
            if len(matches) == 1:
                service.update(matches[0]["id"], item, "vendor-sync")
                summary["updated"] += 1
            else:
                service.create(item, "vendor-sync")
                summary["created"] += 1
        with db.connect() as conn:
            conn.execute(
                "UPDATE import_jobs SET status='success',summary=?,finished_at=? WHERE id=?",
                (json.dumps(summary, ensure_ascii=False), now(), job_id),
            )
        return summary
    except Exception as exc:
        with db.connect() as conn:
            conn.execute(
                "UPDATE import_jobs SET status='failed',error=?,summary=?,finished_at=? WHERE id=?",
                (str(exc), json.dumps(summary, ensure_ascii=False), now(), job_id),
            )
        raise


def export_match_candidates(db, output_path):
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT id,external_id,appid,mini_program_name FROM programs
               WHERE status<>'已结算' AND (
                  trim(avatar_url)='' OR trim(description)='' OR trim(category)=''
                  OR trim(appid)='' OR trim(original_id)='' OR trim(secret)=''
                  OR trim(admin)='' OR trim(email)=''
               )"""
        ).fetchall()
    payload = [
        {
            "id": row["id"], "externalId": row["external_id"],
            "appid": row["appid"], "miniProgramName": row["mini_program_name"],
        }
        for row in rows
    ]
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)


def match_records(db, records, source_ref=""):
    service = ProgramService(db)
    summary = {
        "total": len(records), "matched": 0, "updated": 0,
        "fieldsUpdated": 0, "conflicts": 0, "skipped": 0,
    }
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO import_jobs(source,status,source_ref,started_at) VALUES('vendor-match','running',?,?)",
            (source_ref, now()),
        )
        job_id = cur.lastrowid
    try:
        for raw in records:
            item = {key: raw.get(key) for key in VENDOR_FIELDS if raw.get(key) not in (None, "")}
            external_id = str(item.get("externalId") or "").strip()
            appid = str(item.get("appid") or "").strip()
            name = str(item.get("miniProgramName") or "").strip()
            with db.connect() as conn:
                matches = []
                if external_id:
                    matches = conn.execute(
                        "SELECT id FROM programs WHERE external_id=?", (external_id,)
                    ).fetchall()
                if not matches and appid:
                    matches = conn.execute("SELECT id FROM programs WHERE appid=?", (appid,)).fetchall()
                if not matches and name:
                    matches = conn.execute(
                        "SELECT id FROM programs WHERE trim(mini_program_name)=?", (name,)
                    ).fetchall()
            if len(matches) != 1:
                if len(matches) > 1:
                    summary["conflicts"] += 1
                    add_exception(db, "乙方资料匹配到多个现有小程序", item)
                else:
                    summary["skipped"] += 1
                continue
            program_id = matches[0]["id"]
            existing = service.get(program_id)
            summary["matched"] += 1
            if existing["status"] == "已结算":
                summary["skipped"] += 1
                continue
            updates = {}
            for field in (
                "externalId", "avatarUrl", "description", "category",
                "appid", "originalId", "secret", "admin", "email",
            ):
                if not str(existing.get(field) or "").strip() and str(item.get(field) or "").strip():
                    updates[field] = str(item[field]).strip()
            merged = dict(existing)
            merged.update(updates)
            required = ("appid", "originalId", "secret", "admin", "email")
            complete = all(str(merged.get(field) or "").strip() for field in required)
            if complete and existing.get("taskReason") == "待乙方补全资料":
                updates["taskReason"] = ""
            elif not complete and not str(existing.get("taskReason") or "").strip():
                updates["taskReason"] = "待乙方补全资料"
            field_count = sum(1 for key in updates if key != "taskReason")
            if updates:
                service.update(program_id, updates, "vendor-match")
                summary["updated"] += 1
                summary["fieldsUpdated"] += field_count
        with db.connect() as conn:
            conn.execute(
                "UPDATE import_jobs SET status='success',summary=?,finished_at=? WHERE id=?",
                (json.dumps(summary, ensure_ascii=False), now(), job_id),
            )
        return summary
    except Exception as exc:
        with db.connect() as conn:
            conn.execute(
                "UPDATE import_jobs SET status='failed',error=?,summary=?,finished_at=? WHERE id=?",
                (str(exc), json.dumps(summary, ensure_ascii=False), now(), job_id),
            )
        raise


def sync_due(db):
    days = max(1, min(365, int(db.get_setting("vendor_sync_days", 1) or 1)))
    last = str(db.get_setting("vendor_last_sync_at", "") or "")
    if not last:
        return True, days, last
    try:
        due = datetime.now() >= datetime.strptime(last, "%Y-%m-%d %H:%M:%S") + timedelta(days=days)
    except ValueError:
        due = True
    return due, days, last


def _selector(name, default):
    return os.environ.get(name, "").strip() or default


def scrape_vendor():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("未安装 Playwright，请执行 pip install -r server/requirements.txt")

    base_url = os.environ.get("VENDOR_BASE_URL", "https://xcx.qn76.cn/").strip()
    username = os.environ.get("VENDOR_USERNAME", "").strip()
    password = os.environ.get("VENDOR_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("未配置 VENDOR_USERNAME/VENDOR_PASSWORD")
    list_url = os.environ.get("VENDOR_LIST_URL", "").strip()
    headless = os.environ.get("VENDOR_HEADLESS", "true").lower() != "false"
    debug_dir = os.path.join(os.environ.get("LAINGZIZHIWANG_DATA_DIR", "/www/laingzizhiwang-data"), "vendor-debug")
    os.makedirs(debug_dir, exist_ok=True)

    with sync_playwright() as p:
        executable = os.environ.get("VENDOR_BROWSER_EXECUTABLE") or None
        browser = p.chromium.launch(headless=headless, executable_path=executable)
        page = browser.new_page(locale="zh-CN")
        try:
            page.goto(base_url, wait_until="domcontentloaded", timeout=60000)
            page.locator(_selector(
                "VENDOR_USERNAME_SELECTOR",
                "input[placeholder*='账号'],input[placeholder*='用户名'],input[type='text']",
            )).first.fill(username)
            page.locator(_selector(
                "VENDOR_PASSWORD_SELECTOR", "input[placeholder*='密码'],input[type='password']"
            )).first.fill(password)
            page.locator(_selector(
                "VENDOR_LOGIN_SELECTOR", "button:has-text('登录'),button[type='submit'],input[type='submit']"
            )).first.click()
            page.wait_for_load_state("networkidle", timeout=60000)
            if list_url:
                page.goto(list_url, wait_until="networkidle", timeout=60000)
            table = page.locator(_selector("VENDOR_TABLE_SELECTOR", "table")).first
            table.wait_for(state="visible", timeout=30000)
            headers = [x.strip() for x in table.locator("thead th").all_inner_texts()]
            if not headers:
                raise RuntimeError("列表表格缺少表头，请配置 VENDOR_TABLE_SELECTOR")
            records = []
            max_pages = int(os.environ.get("VENDOR_MAX_PAGES", "200"))
            for _ in range(max_pages):
                for row in table.locator("tbody tr").all():
                    values = [x.strip() for x in row.locator("td").all_inner_texts()]
                    record = mapped_record(headers, values)
                    if record:
                        records.append(record)
                next_button = page.locator(_selector(
                    "VENDOR_NEXT_SELECTOR",
                    "button:has-text('下一页'),li[title='下一页'],.el-pagination .btn-next",
                )).first
                if not next_button.count() or next_button.is_disabled():
                    break
                marker = table.locator("tbody").inner_text()
                next_button.click()
                page.wait_for_function(
                    "(old) => document.querySelector('tbody') && document.querySelector('tbody').innerText !== old",
                    marker, timeout=30000,
                )
            return records, page.url
        except Exception:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            page.screenshot(path=os.path.join(debug_dir, "failure-%s.png" % stamp), full_page=True)
            with open(os.path.join(debug_dir, "failure-%s.txt" % stamp), "w", encoding="utf-8") as fh:
                fh.write("URL: %s\n\n%s" % (page.url, traceback.format_exc()))
            raise
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="忽略 VENDOR_SYNC_ENABLED 开关")
    parser.add_argument("--from-json", help="从 JSON 数组验证导入规则，不访问网站")
    parser.add_argument("--match-json", help="仅用乙方资料补齐现有小程序空字段")
    parser.add_argument("--export-known-companies", help="导出公司名称和全称供增量抓取使用")
    parser.add_argument("--export-match-candidates", help="导出资料不完整的现有小程序")
    parser.add_argument("--check-due", action="store_true", help="检查自动同步是否到期")
    args = parser.parse_args()
    db = Database()
    db.initialize()
    migrate(db)
    if args.export_known_companies:
        names = db.get_setting("vendor_synced_companies")
        if not isinstance(names, list):
            with db.connect() as conn:
                names = sorted({
                    str(value).strip()
                    for row in conn.execute("SELECT name,full_name FROM companies")
                    for value in (row["name"], row["full_name"])
                    if str(value or "").strip()
                })
            db.set_setting("vendor_synced_companies", names)
        with open(args.export_known_companies, "w", encoding="utf-8") as fh:
            json.dump(sorted(names), fh, ensure_ascii=False)
        return 0
    if args.export_match_candidates:
        export_match_candidates(db, args.export_match_candidates)
        return 0
    if args.check_due:
        due, days, last = sync_due(db)
        print(json.dumps({"due": due, "days": days, "lastSyncAt": last}, ensure_ascii=False))
        return 0 if due else 3
    if not args.force and os.environ.get("VENDOR_SYNC_ENABLED", "false").lower() != "true":
        print("乙方平台同步未启用")
        return 0
    try:
        json_path = args.match_json or args.from_json
        if json_path:
            with open(json_path, "r", encoding="utf-8") as fh:
                records, source_ref = json.load(fh), json_path
        else:
            records, source_ref = scrape_vendor()
        if args.match_json:
            summary = match_records(db, records, source_ref)
        else:
            summary = upsert_records(
                db, records, source_ref, new_companies_only=not bool(args.from_json)
            )
        if args.from_json and not summary["conflicts"] and not summary["skipped"]:
            synced = set(db.get_setting("vendor_synced_companies") or [])
            synced.update(
                str(row.get("companyName") or "").strip()
                for row in records if isinstance(row, dict) and str(row.get("companyName") or "").strip()
            )
            db.set_setting("vendor_synced_companies", sorted(synced))
        if args.from_json:
            db.set_setting("vendor_last_sync_at", now())
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except Exception as exc:
        add_exception(db, "乙方平台自动同步失败", {"error": str(exc)})
        print("乙方平台同步失败：%s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
