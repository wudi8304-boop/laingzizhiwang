#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将 data.js 与旧 JSON 数据一次性迁移到 SQLite。"""
import json
import os
import re
import shutil
from datetime import datetime

from db import Database, now
from services.programs import FIELDS


LEGACY_NAMES = (
    "emails.json", "monitor.json", "records.json", "approved_programs.json",
    "data_records.json", "last_run.json",
)


def read_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def parse_data_js(path):
    if not path or not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig") as fh:
        text = fh.read()
    match = re.search(r"(?:const|let|var)\s+RECORDS\s*=\s*(\[.*\])\s*;", text, re.S)
    if not match:
        raise ValueError("data.js 中未找到 RECORDS 数组")
    data = json.loads(match.group(1))
    if not isinstance(data, list):
        raise ValueError("RECORDS 必须是数组")
    return data


def apply_overlay(base, overlay):
    merged = {str(row.get("id")): dict(row) for row in base if isinstance(row, dict) and row.get("id")}
    for change in overlay if isinstance(overlay, list) else []:
        if not isinstance(change, dict):
            continue
        key = str(change.get("_localId") or change.get("id") or "")
        kind = change.get("_type")
        if kind == "delete" or change.get("_deleted"):
            merged.pop(key, None)
        elif kind == "edit" and key in merged:
            merged[key].update({k: v for k, v in change.items() if not k.startswith("_")})
        elif kind == "add":
            row = {k: v for k, v in change.items() if not k.startswith("_")}
            row["id"] = key or row.get("id")
            if row["id"]:
                merged[str(row["id"])] = row
    return list(merged.values())


def backup_sources(data_dir, data_js):
    paths = [os.path.join(data_dir, name) for name in LEGACY_NAMES]
    if data_js:
        paths.append(data_js)
    existing = [p for p in paths if os.path.isfile(p)]
    if not existing:
        return None
    backup_dir = os.path.join(data_dir, "backups", datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    os.makedirs(backup_dir, exist_ok=False)
    for path in existing:
        shutil.copy2(path, os.path.join(backup_dir, os.path.basename(path)))
    return backup_dir


def _company(conn, item):
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    stamp = now()
    existing = conn.execute("SELECT * FROM companies WHERE name=?", (name,)).fetchone()
    full_name = str(item.get("fullName") if "fullName" in item else (existing["full_name"] if existing else "") or "")
    enabled = int(bool(item.get("enabled") if "enabled" in item else (existing["enabled"] if existing else True)))
    last_check = item.get("lastCheck") if "lastCheck" in item else (existing["last_check"] if existing else None)
    last_count = int(item.get("lastCount") if item.get("lastCount") is not None else
                     (existing["last_count"] if existing else 0))
    has_new = int(bool(item.get("hasNew") if "hasNew" in item else (existing["has_new"] if existing else False)))
    conn.execute(
        """INSERT INTO companies(name,full_name,enabled,last_check,last_count,has_new,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET
           full_name=excluded.full_name,enabled=excluded.enabled,last_check=excluded.last_check,
           last_count=excluded.last_count,has_new=excluded.has_new,updated_at=excluded.updated_at""",
        (name, full_name, enabled, last_check, last_count, has_new, stamp, stamp),
    )
    return conn.execute("SELECT id FROM companies WHERE name=?", (name,)).fetchone()["id"]


def reconcile_program_emails(db):
    added = 0
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT DISTINCT trim(email) address FROM programs WHERE trim(email)<>'' ORDER BY address"
        ).fetchall()
        next_order = conn.execute("SELECT COALESCE(MAX(sort_order),-1)+1 n FROM emails").fetchone()["n"]
        for row in rows:
            address = row["address"]
            if conn.execute(
                "SELECT 1 FROM emails WHERE lower(address)=lower(?)", (address,)
            ).fetchone():
                continue
            stamp = now()
            conn.execute(
                """INSERT INTO emails(address,payload,sort_order,created_at,updated_at)
                   VALUES(?,?,?,?,?)""",
                (address, json.dumps({"email": address}, ensure_ascii=False), next_order, stamp, stamp),
            )
            next_order += 1
            added += 1
        db.set_setting("email_reconcile_v1", {"at": now(), "added": added}, conn)
    return added


def migrate(db=None, repo_root=None, data_dir=None, force=False):
    db = db or Database()
    db.initialize()
    repo_root = repo_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = data_dir or os.path.dirname(os.path.abspath(db.path))
    marker = db.get_setting("migration_v1", None)
    if marker and not force:
        return {
            "migrated": False, "reason": "already migrated", "details": marker,
            "emailsAdded": reconcile_program_emails(db),
        }

    data_js = os.path.join(repo_root, "data.js")
    backup_dir = backup_sources(data_dir, data_js)
    base_records = parse_data_js(data_js)
    overlay = read_json(os.path.join(data_dir, "data_records.json"), None)
    records = apply_overlay(base_records, overlay)
    emails = read_json(os.path.join(data_dir, "emails.json"), [])
    monitor = read_json(os.path.join(data_dir, "monitor.json"), {})
    legacy_runs = read_json(os.path.join(data_dir, "records.json"), {})
    approved = read_json(os.path.join(data_dir, "approved_programs.json"), [])

    with db.transaction() as conn:
        for record in records:
            if not isinstance(record, dict):
                continue
            record = dict(record)
            program_id = str(record.get("id") or "")
            exists = conn.execute("SELECT 1 FROM programs WHERE id=?", (program_id,)).fetchone() if program_id else None
            # 在同一事务内直接写，避免嵌套连接锁；字段映射与 ProgramService 一致。
            if exists:
                cols = []
                args = []
                for api, col in FIELDS.items():
                    cols.append(col + "=?"); args.append(str(record.get(api) or ""))
                args.extend([now(), program_id])
                conn.execute("UPDATE programs SET %s,updated_at=? WHERE id=?" % ",".join(cols), args)
            else:
                pid = program_id or ("migrated_%d" % (conn.execute("SELECT COUNT(*) n FROM programs").fetchone()["n"] + 1))
                vals = [str(record.get(k) or "") for k in FIELDS]
                company_name = str(record.get("companyName") or "")
                company_id = None
                if company_name:
                    company_id = _company(conn, {"name": company_name})
                stamp = now()
                conn.execute(
                    """INSERT INTO programs(id,company_id,company_name,mini_program_name,avatar_url,description,category,
                    appid,original_id,secret,admin,status,email,mini_program_password,submit_date,task_reason,
                    external_id,source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [pid, company_id] + vals + ["migration", stamp, stamp],
                )

        for index, entry in enumerate(emails if isinstance(emails, list) else []):
            payload = entry if isinstance(entry, dict) else {"email": entry}
            address = str(payload.get("email") or payload.get("address") or "").strip()
            if address:
                conn.execute(
                    """INSERT INTO emails(address,payload,sort_order,created_at,updated_at) VALUES(?,?,?,?,?)
                       ON CONFLICT(address) DO UPDATE SET payload=excluded.payload,sort_order=excluded.sort_order,
                       updated_at=excluded.updated_at""",
                    (address, json.dumps(payload, ensure_ascii=False), index, now(), now()),
                )

        if isinstance(monitor, dict):
            for company in monitor.get("companies") or []:
                if isinstance(company, dict):
                    _company(conn, company)
            for key in ("times", "apihz", "dingtalk"):
                if key in monitor:
                    db.set_setting("monitor_" + key, monitor[key], conn)
        db.set_setting("legacy_data_records", overlay if isinstance(overlay, list) else [], conn)
        db.set_setting("legacy_records", legacy_runs if isinstance(legacy_runs, dict) else {}, conn)

        history_run_id = None
        if isinstance(legacy_runs, dict) and legacy_runs:
            existing_run = conn.execute(
                "SELECT id FROM monitor_runs WHERE trigger_key='migration-history'"
            ).fetchone()
            if existing_run:
                history_run_id = existing_run["id"]
            else:
                cur = conn.execute(
                    """INSERT INTO monitor_runs(trigger_type,trigger_key,status,started_at,finished_at,total_companies,
                       success_count) VALUES('migration','migration-history','success',?,?,?,?)""",
                    (now(), now(), len(legacy_runs), len(legacy_runs)),
                )
                history_run_id = cur.lastrowid
            for company_name, rec in legacy_runs.items():
                cid = _company(conn, {"name": company_name})
                for item in (rec.get("items") or []) if isinstance(rec, dict) else []:
                    norm = item if isinstance(item, dict) else {"name": item[0], "icp": item[1] if len(item) > 1 else ""}
                    name, icp = str(norm.get("name") or ""), str(norm.get("icp") or "")
                    key = "name:" + name if name else "icp:" + icp
                    if key:
                        conn.execute(
                            """INSERT OR IGNORE INTO monitor_items(run_id,company_id,item_key,name,icp,raw_json,created_at)
                               VALUES(?,?,?,?,?,?,?)""",
                            (history_run_id, cid, key, name, icp, json.dumps(norm, ensure_ascii=False), now()),
                        )

        for entry in approved if isinstance(approved, list) else []:
            name = str(entry.get("miniProgramName") or "").strip()
            if not name:
                continue
            cname = str(entry.get("companyName") or "")
            cid = _company(conn, {"name": cname, "fullName": entry.get("companyFullName")}) if cname else None
            conn.execute(
                """INSERT INTO approved_programs(company_id,company_name,company_full_name,mini_program_name,
                   normalized_name,approved_at,source_run_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(normalized_name) DO UPDATE SET company_name=excluded.company_name,
                   company_full_name=excluded.company_full_name,approved_at=excluded.approved_at,updated_at=excluded.updated_at""",
                (cid, cname, str(entry.get("companyFullName") or ""), name, name.casefold(),
                 str(entry.get("approvedAt") or ""), history_run_id, now(), now()),
            )
        details = {"at": now(), "backup": backup_dir, "programs": len(records)}
        db.set_setting("migration_v1", details, conn)
        db.audit("migrate", "database", "", details, "startup", conn)
    details["emailsAdded"] = reconcile_program_emails(db)
    return {"migrated": True, "details": details}


if __name__ == "__main__":
    print(json.dumps(migrate(), ensure_ascii=False, indent=2))
