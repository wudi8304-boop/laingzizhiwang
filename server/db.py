#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite 数据访问底座。所有运行时业务数据只读写本数据库。"""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime


DEFAULT_DATA_DIR = os.environ.get(
    "LAINGZIZHIWANG_DATA_DIR",
    "/www/laingzizhiwang-data" if os.name != "nt" else os.path.join(os.path.dirname(__file__), "data"),
)
DEFAULT_DB_PATH = os.environ.get("LAINGZIZHIWANG_DB", os.path.join(DEFAULT_DATA_DIR, "app.db"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_users (
 id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
 password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'subadmin',
 active INTEGER NOT NULL DEFAULT 1, session_version INTEGER NOT NULL DEFAULT 1,
 last_login TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS companies (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, full_name TEXT NOT NULL DEFAULT '',
 enabled INTEGER NOT NULL DEFAULT 1, monitor_listed INTEGER NOT NULL DEFAULT 1,
 last_check TEXT, last_count INTEGER NOT NULL DEFAULT 0,
 has_new INTEGER NOT NULL DEFAULT 0, assigned_admin_id INTEGER REFERENCES admin_users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS programs (
 id TEXT PRIMARY KEY, company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
 company_name TEXT NOT NULL DEFAULT '', mini_program_name TEXT NOT NULL DEFAULT '',
 avatar_url TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '',
 appid TEXT NOT NULL DEFAULT '', original_id TEXT NOT NULL DEFAULT '', secret TEXT NOT NULL DEFAULT '',
 admin TEXT NOT NULL DEFAULT '', legal_person_phone TEXT NOT NULL DEFAULT '',
 mini_program_phone TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '待注册', email TEXT NOT NULL DEFAULT '',
 completed_at TEXT NOT NULL DEFAULT '', settled_at TEXT NOT NULL DEFAULT '',
 mini_program_password TEXT NOT NULL DEFAULT '', submit_date TEXT NOT NULL DEFAULT '',
 task_reason TEXT NOT NULL DEFAULT '', external_id TEXT NOT NULL DEFAULT '',
 source TEXT NOT NULL DEFAULT 'api', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_programs_company ON programs(company_name);
CREATE INDEX IF NOT EXISTS idx_programs_name ON programs(mini_program_name);
CREATE TRIGGER IF NOT EXISTS prevent_settled_program_update
BEFORE UPDATE ON programs WHEN OLD.status='已结算'
BEGIN
 SELECT RAISE(ABORT, '已结算的小程序不允许修改');
END;
CREATE TABLE IF NOT EXISTS emails (
 id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL UNIQUE, payload TEXT NOT NULL DEFAULT '{}',
 sort_order INTEGER NOT NULL DEFAULT 0, usable INTEGER NOT NULL DEFAULT 1,
 invalid_at TEXT, invalid_by TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
 key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS monitor_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, trigger_type TEXT NOT NULL, trigger_key TEXT UNIQUE,
 status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, total_companies INTEGER NOT NULL DEFAULT 0,
 success_count INTEGER NOT NULL DEFAULT 0, failure_count INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE TABLE IF NOT EXISTS monitor_items (
 id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES monitor_runs(id) ON DELETE CASCADE,
 company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE, item_key TEXT NOT NULL,
 name TEXT NOT NULL DEFAULT '', icp TEXT NOT NULL DEFAULT '', is_new INTEGER NOT NULL DEFAULT 0,
 raw_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
 UNIQUE(run_id, company_id, item_key)
);
CREATE INDEX IF NOT EXISTS idx_monitor_items_company ON monitor_items(company_id, item_key);
CREATE TABLE IF NOT EXISTS approved_programs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
 company_name TEXT NOT NULL DEFAULT '', company_full_name TEXT NOT NULL DEFAULT '',
 mini_program_name TEXT NOT NULL, normalized_name TEXT NOT NULL UNIQUE, icp TEXT NOT NULL DEFAULT '',
 approved_at TEXT NOT NULL DEFAULT '', source_run_id INTEGER REFERENCES monitor_runs(id),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
 id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE, channel TEXT NOT NULL DEFAULT 'dingtalk',
 payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 last_error TEXT, next_retry_at TEXT, sent_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exceptions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, program_id TEXT REFERENCES programs(id) ON DELETE SET NULL,
 company_name TEXT NOT NULL DEFAULT '', mini_program_name TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'open', payload TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_logs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, entity_type TEXT NOT NULL,
 entity_id TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}', actor TEXT NOT NULL DEFAULT 'system',
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS import_jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 source_ref TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '{}', error TEXT,
 started_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS apihz_account_runs (
 run_date TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'running',
 checkin_succeeded INTEGER NOT NULL DEFAULT 0, checkin_message TEXT NOT NULL DEFAULT '',
 balance TEXT NOT NULL DEFAULT '', account_payload TEXT NOT NULL DEFAULT '{}',
 started_at TEXT NOT NULL, finished_at TEXT, error TEXT
);
"""


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class ClosingConnection(sqlite3.Connection):
    """让 ``with db.connect()`` 在提交/回滚后可靠关闭连接。"""
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class Database:
    def __init__(self, path=None):
        self.path = path or DEFAULT_DB_PATH

    def connect(self):
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def initialize(self):
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(programs)")}
            for name in (
                "avatar_url", "description", "category", "completed_at", "settled_at",
                "task_reason", "external_id", "legal_person_phone", "mini_program_phone",
            ):
                if name not in existing:
                    conn.execute("ALTER TABLE programs ADD COLUMN %s TEXT NOT NULL DEFAULT ''" % name)
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_programs_external_id "
                "ON programs(external_id) WHERE external_id<>''"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_programs_completed_at ON programs(completed_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_programs_settled_at ON programs(settled_at)")
            email_columns = {row["name"] for row in conn.execute("PRAGMA table_info(emails)")}
            if "usable" not in email_columns:
                conn.execute("ALTER TABLE emails ADD COLUMN usable INTEGER NOT NULL DEFAULT 1")
            if "invalid_at" not in email_columns:
                conn.execute("ALTER TABLE emails ADD COLUMN invalid_at TEXT")
            if "invalid_by" not in email_columns:
                conn.execute("ALTER TABLE emails ADD COLUMN invalid_by TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_usable ON emails(usable,sort_order,id)")
            company_columns = {row["name"] for row in conn.execute("PRAGMA table_info(companies)")}
            if "assigned_admin_id" not in company_columns:
                conn.execute("ALTER TABLE companies ADD COLUMN assigned_admin_id INTEGER")
            if "monitor_listed" not in company_columns:
                conn.execute("ALTER TABLE companies ADD COLUMN monitor_listed INTEGER NOT NULL DEFAULT 1")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_companies_assigned_admin "
                "ON companies(assigned_admin_id)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_companies_monitor_listed ON companies(monitor_listed)")
            conn.executemany(
                "UPDATE programs SET status=? WHERE status=?",
                [
                    ("备案中", "审核中"),
                    ("备案完成", "审核通过"),
                    ("备案完成", "待验收"),
                    ("已结算", "已验收"),
                    ("待注册", ""),
                ],
            )

    @contextmanager
    def transaction(self, immediate=True):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_setting(self, key, default=None, conn=None):
        own = conn is None
        conn = conn or self.connect()
        try:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row["value"]) if row else default
        finally:
            if own:
                conn.close()

    def set_setting(self, key, value, conn=None):
        own = conn is None
        conn = conn or self.connect()
        try:
            conn.execute(
                """INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                (key, json.dumps(value, ensure_ascii=False), now()),
            )
        finally:
            if own:
                conn.close()

    def audit(self, action, entity_type, entity_id="", detail=None, actor="system", conn=None):
        own = conn is None
        conn = conn or self.connect()
        try:
            conn.execute(
                "INSERT INTO audit_logs(action,entity_type,entity_id,detail,actor,created_at) VALUES(?,?,?,?,?,?)",
                (action, entity_type, str(entity_id), json.dumps(detail or {}, ensure_ascii=False), actor, now()),
            )
        finally:
            if own:
                conn.close()


def row_dict(row):
    return dict(row) if row is not None else None
