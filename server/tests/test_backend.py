import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from datetime import datetime
from unittest.mock import patch

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SERVER_DIR)

from db import Database, now
from config_server import Handler, ThreadingHTTPServer
from migrate_data import migrate
from services.auth import AuthService, verify_password
from services.monitor import MonitorService
from services.programs import ProgramService
from vendor_sync import match_records, sync_due, upsert_records


class BackendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.tmp, "data", "app.db"))
        self.db.initialize()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_migration_is_idempotent_and_applies_overlay(self):
        repo = os.path.join(self.tmp, "repo")
        data_dir = os.path.join(self.tmp, "data")
        os.makedirs(repo)
        with open(os.path.join(repo, "data.js"), "w", encoding="utf-8") as fh:
            fh.write('const RECORDS = [{"id":"rec1","companyName":"甲","miniProgramName":"旧名","status":"待审核"}];')
        with open(os.path.join(data_dir, "data_records.json"), "w", encoding="utf-8") as fh:
            json.dump([{"_localId": "rec1", "_type": "edit", "miniProgramName": "新名"}], fh)
        first = migrate(self.db, repo, data_dir)
        second = migrate(self.db, repo, data_dir)
        self.assertTrue(first["migrated"])
        self.assertFalse(second["migrated"])
        self.assertEqual("新名", ProgramService(self.db).get("rec1")["miniProgramName"])
        self.assertTrue(os.path.isdir(first["details"]["backup"]))
        with self.db.connect() as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) n FROM programs").fetchone()["n"])

    def test_program_crud_and_bulk(self):
        service = ProgramService(self.db)
        created = service.create({
            "id": "p1", "companyName": "甲", "miniProgramName": "程序A",
            "description": "简介", "category": "工具", "email": "a@example.com",
        })
        self.assertEqual("程序A", created["miniProgramName"])
        self.assertEqual("简介", created["description"])
        self.assertEqual("待注册", created["status"])
        self.assertEqual("备案中", service.update("p1", {"status": "审核中"})["status"])
        service.update("p1", {"email": "b@example.com"})
        with self.db.connect() as conn:
            self.assertEqual(2, conn.execute("SELECT COUNT(*) n FROM emails").fetchone()["n"])
        result = service.bulk([
            {"id": "p1", "status": "备案完成"},
            {"id": "p2", "companyName": "乙", "miniProgramName": "程序B"},
        ])
        self.assertEqual(2, len(result))
        self.assertEqual("待注册", result[1]["status"])
        self.assertEqual(2, service.list()["total"])
        self.assertTrue(service.delete("p2"))
        self.assertIsNone(service.get("p2"))

    def test_legacy_program_status_is_remapped(self):
        stamp = now()
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO programs(id,company_name,mini_program_name,status,created_at,updated_at)
                   VALUES('old1','甲','程序A','审核中',?,?),
                         ('old2','甲','程序B','已验收',?,?),
                         ('old3','甲','程序C','',?,?)""",
                (stamp, stamp, stamp, stamp, stamp, stamp),
            )
        self.db.initialize()
        service = ProgramService(self.db)
        self.assertEqual("备案中", service.get("old1")["status"])
        self.assertEqual("已结算", service.get("old2")["status"])
        self.assertEqual("待注册", service.get("old3")["status"])
        self.assertEqual("", service.get("old1")["completionTime"])
        self.assertEqual("", service.get("old2")["settlementTime"])

    def test_existing_database_adds_milestone_columns_and_lock(self):
        path = os.path.join(self.tmp, "legacy.db")
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE programs(
               id TEXT PRIMARY KEY,company_id INTEGER,company_name TEXT NOT NULL DEFAULT '',
               mini_program_name TEXT NOT NULL DEFAULT '',avatar_url TEXT NOT NULL DEFAULT '',
               description TEXT NOT NULL DEFAULT '',category TEXT NOT NULL DEFAULT '',
               appid TEXT NOT NULL DEFAULT '',original_id TEXT NOT NULL DEFAULT '',
               secret TEXT NOT NULL DEFAULT '',admin TEXT NOT NULL DEFAULT '',
               status TEXT NOT NULL DEFAULT '待注册',email TEXT NOT NULL DEFAULT '',
               mini_program_password TEXT NOT NULL DEFAULT '',submit_date TEXT NOT NULL DEFAULT '',
               task_reason TEXT NOT NULL DEFAULT '',external_id TEXT NOT NULL DEFAULT '',
               source TEXT NOT NULL DEFAULT 'api',created_at TEXT NOT NULL,updated_at TEXT NOT NULL
            )"""
        )
        conn.close()
        legacy = Database(path)
        legacy.initialize()
        with legacy.connect() as upgraded:
            columns = {row["name"] for row in upgraded.execute("PRAGMA table_info(programs)")}
            self.assertTrue({"completed_at", "settled_at"} <= columns)
            self.assertEqual(1, upgraded.execute(
                """SELECT COUNT(*) n FROM sqlite_master
                   WHERE type='trigger' AND name='prevent_settled_program_update'"""
            ).fetchone()["n"])

    def test_milestone_times_use_latest_status_transition(self):
        service = ProgramService(self.db)
        with patch("services.programs.now", return_value="2026-07-01 09:00:00"):
            created = service.create({
                "id": "p1", "companyName": "甲", "miniProgramName": "程序A", "status": "备案完成",
            })
        self.assertEqual("2026-07-01 09:00:00", created["completionTime"])
        self.assertEqual("", created["settlementTime"])

        with patch("services.programs.now", return_value="2026-07-02 09:00:00"):
            unchanged = service.update("p1", {"status": "备案完成", "description": "补充"})
        self.assertEqual("2026-07-01 09:00:00", unchanged["completionTime"])

        service.update("p1", {"status": "待审核"})
        with patch("services.programs.now", return_value="2026-07-03 09:00:00"):
            recompleted = service.update("p1", {"status": "备案完成"})
        self.assertEqual("2026-07-03 09:00:00", recompleted["completionTime"])

        with patch("services.programs.now", return_value="2026-08-01 09:00:00"):
            settled = service.update("p1", {"status": "已结算"})
        self.assertEqual("2026-08-01 09:00:00", settled["settlementTime"])
        with patch("services.programs.now", return_value="2026-08-02 09:00:00"):
            with self.assertRaises(ValueError):
                service.update("p1", {"status": "已结算", "category": "工具"})
        with self.db.connect() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE programs SET category='其他' WHERE id='p1'")
        self.assertEqual("", service.get("p1")["category"])

    def test_admin_accounts_company_scope_and_session_version(self):
        auth = AuthService(self.db)
        superuser = auth.ensure_superadmin("wudi2026")
        self.assertEqual("super", superuser["role"])
        sub = auth.create_subadmin("partner-a", "initial123")
        self.assertEqual("subadmin", auth.authenticate("PARTNER-A", "initial123")["role"])
        with self.db.connect() as conn:
            stored = conn.execute(
                "SELECT password_hash FROM admin_users WHERE id=?", (sub["id"],)
            ).fetchone()["password_hash"]
        self.assertNotIn("initial123", stored)
        self.assertTrue(verify_password("initial123", stored))

        root_programs = ProgramService(self.db)
        root_programs.create({"id": "p1", "companyName": "甲", "miniProgramName": "程序A", "email": "a@x.com"})
        root_programs.create({"id": "p2", "companyName": "乙", "miniProgramName": "程序B", "email": "b@x.com"})
        with self.db.connect() as conn:
            company_id = conn.execute("SELECT id FROM companies WHERE name='甲'").fetchone()["id"]
        auth.assign_company(company_id, sub["id"])

        scoped = ProgramService(self.db, sub)
        self.assertEqual(["p1"], [x["id"] for x in scoped.list()["items"]])
        self.assertEqual("程序A", scoped.get("p1")["miniProgramName"])
        with self.assertRaises(PermissionError):
            scoped.get("p2")
        with self.assertRaises(PermissionError):
            scoped.create({"companyName": "乙", "miniProgramName": "越权"})
        with self.assertRaises(PermissionError):
            scoped.update("p1", {"companyName": "乙"})
        self.assertEqual("已结算", scoped.update("p1", {"status": "已结算"})["status"])

        old_version = sub["sessionVersion"]
        updated = auth.update_subadmin(sub["id"], password="changed123")
        self.assertGreater(updated["sessionVersion"], old_version)
        self.assertIsNone(auth.authenticate("partner-a", "initial123"))
        self.assertIsNotNone(auth.authenticate("partner-a", "changed123"))
        disabled = auth.update_subadmin(sub["id"], active=False)
        self.assertFalse(disabled["active"])
        self.assertIsNone(auth.authenticate("partner-a", "changed123"))

    def test_http_role_navigation_and_scope_enforcement(self):
        old_password = os.environ.get("APP_PASSWORD")
        old_secret = os.environ.get("SESSION_SECRET")
        os.environ["APP_PASSWORD"] = "wudi2026"
        os.environ["SESSION_SECRET"] = "test-session-secret"
        auth = AuthService(self.db)
        auth.ensure_superadmin("wudi2026")
        sub = auth.create_subadmin("partner", "partner123")
        programs = ProgramService(self.db)
        programs.create({"id": "p1", "companyName": "甲", "miniProgramName": "程序A", "email": "a@x.com"})
        programs.create({"id": "p2", "companyName": "乙", "miniProgramName": "程序B", "email": "b@x.com"})
        with self.db.connect() as conn:
            company_id = conn.execute("SELECT id FROM companies WHERE name='甲'").fetchone()["id"]
        auth.assign_company(company_id, sub["id"])

        Handler.db = self.db
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]

        def client():
            return urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(CookieJar())
            )

        def call(opener, method, path, body=None):
            raw = json.dumps(body).encode("utf-8") if body is not None else None
            request = urllib.request.Request(
                base + path, data=raw,
                headers={"Content-Type": "application/json"} if raw is not None else {},
            )
            request.get_method = lambda: method
            try:
                response = opener.open(request)
                return response.getcode(), json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

        try:
            admin_client = client()
            code, login = call(admin_client, "POST", "/api/login", {
                "username": "admin", "password": "wudi2026",
            })
            self.assertEqual(200, code)
            self.assertEqual("super", login["user"]["role"])

            sub_client = client()
            code, login = call(sub_client, "POST", "/api/login", {
                "username": "partner", "password": "partner123",
            })
            self.assertEqual(200, code)
            self.assertEqual("subadmin", login["user"]["role"])
            code, listing = call(sub_client, "GET", "/api/programs?pageSize=200")
            self.assertEqual(200, code)
            self.assertEqual(["p1"], [row["id"] for row in listing["data"]["items"]])
            self.assertEqual(403, call(sub_client, "GET", "/api/programs/p2")[0])
            self.assertEqual(403, call(sub_client, "GET", "/api/monitor")[0])
            code, emails = call(sub_client, "GET", "/api/emails")
            self.assertEqual(200, code)
            self.assertEqual(["a@x.com"], [row["email"] for row in emails["data"]])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
            if old_password is None:
                os.environ.pop("APP_PASSWORD", None)
            else:
                os.environ["APP_PASSWORD"] = old_password
            if old_secret is None:
                os.environ.pop("SESSION_SECRET", None)
            else:
                os.environ["SESSION_SECRET"] = old_secret

    def test_vendor_sync_updates_unique_match_and_reports_conflict(self):
        service = ProgramService(self.db)
        service.create({"id": "p1", "companyName": "测试公司", "miniProgramName": "程序A", "appid": "wx1"})
        summary = upsert_records(self.db, [
            {"companyName": "测试公司", "miniProgramName": "程序A", "appid": "wx1", "admin": "新管理员"},
            {"companyName": "测试公司", "miniProgramName": "程序B", "appid": "wx2"},
        ], "test", new_companies_only=False)
        self.assertEqual(1, summary["updated"])
        self.assertEqual(1, summary["created"])
        self.assertEqual("新管理员", service.get("p1")["admin"])

    def test_vendor_sync_only_imports_new_companies_and_allowed_fields(self):
        service = ProgramService(self.db)
        service.create({"id": "p1", "companyName": "已有公司", "miniProgramName": "已有程序"})
        summary = upsert_records(self.db, [
            {"companyName": "已有公司", "miniProgramName": "不应导入", "appid": "old"},
            {
                "companyName": "新增公司", "miniProgramName": "新程序", "appid": "new",
                "originalId": "gh_x", "secret": "s", "admin": "管理员", "email": "a@example.com",
                "avatarUrl": "https://example.com/avatar.png",
                "description": "同步简介", "category": "工具",
            },
        ], "test")
        self.assertEqual(["新增公司"], summary["newCompanies"])
        rows = service.list(page_size=200)["items"]
        imported = [row for row in rows if row["appid"] == "new"][0]
        self.assertEqual("https://example.com/avatar.png", imported["avatarUrl"])
        self.assertEqual("同步简介", imported["description"])
        self.assertEqual("工具", imported["category"])
        self.assertFalse(any(row["appid"] == "old" for row in rows))

    def test_vendor_new_program_randomly_assigns_only_unused_email(self):
        stamp = now()
        with self.db.connect() as conn:
            for index, address in enumerate(("pool1@example.com", "pool2@example.com")):
                conn.execute(
                    """INSERT INTO emails(address,payload,sort_order,created_at,updated_at)
                       VALUES(?,?,?,?,?)""",
                    (address, json.dumps({"email": address}), index, stamp, stamp),
                )
        summary = upsert_records(self.db, [
            {
                "companyName": "新增公司", "miniProgramName": "程序A",
                "email": "vendor@example.com", "appid": "wx-a",
            },
            {"companyName": "新增公司", "miniProgramName": "程序B", "appid": "wx-b"},
            {"companyName": "新增公司", "miniProgramName": "程序C", "appid": "wx-c"},
        ], "test")
        self.assertEqual(2, summary["emailsAssigned"])
        self.assertEqual(0, summary["emailsUnavailable"])
        rows = ProgramService(self.db).list(page_size=200)["items"]
        by_name = {row["miniProgramName"]: row for row in rows}
        self.assertEqual("vendor@example.com", by_name["程序A"]["email"])
        self.assertEqual("待注册", by_name["程序A"]["status"])
        assigned = {by_name["程序B"]["email"], by_name["程序C"]["email"]}
        self.assertEqual({"pool1@example.com", "pool2@example.com"}, assigned)

        exhausted = upsert_records(self.db, [
            {"companyName": "另一新增公司", "miniProgramName": "程序D", "appid": "wx-d"},
        ], "test")
        self.assertEqual(0, exhausted["emailsAssigned"])
        self.assertEqual(1, exhausted["emailsUnavailable"])
        program = ProgramService(self.db).list(search="程序D")["items"][0]
        self.assertEqual("", program["email"])
        self.assertEqual("待乙方补全资料", program["taskReason"])

    def test_vendor_match_only_fills_empty_fields_and_due_days(self):
        service = ProgramService(self.db)
        service.create({
            "id": "p1", "companyName": "甲公司", "miniProgramName": "程序A",
            "appid": "keep-appid", "avatarUrl": "https://manual/avatar.png",
            "taskReason": "待乙方补全资料",
        })
        summary = match_records(self.db, [{
            "externalId": "88", "companyName": "甲公司", "miniProgramName": "程序A",
            "appid": "do-not-overwrite", "originalId": "gh_x", "secret": "secret",
            "admin": "管理员", "email": "a@example.com",
            "avatarUrl": "https://vendor/avatar.png", "description": "乙方简介", "category": "工具",
        }], "test")
        updated = service.get("p1")
        self.assertEqual(1, summary["updated"])
        self.assertEqual(7, summary["fieldsUpdated"])
        self.assertEqual("keep-appid", updated["appid"])
        self.assertEqual("https://manual/avatar.png", updated["avatarUrl"])
        self.assertEqual("乙方简介", updated["description"])
        self.assertEqual("工具", updated["category"])
        self.assertEqual("gh_x", updated["originalId"])
        self.assertEqual("", updated["taskReason"])

        self.db.set_setting("vendor_sync_days", 7)
        self.db.set_setting("vendor_last_sync_at", now())
        self.assertFalse(sync_due(self.db)[0])
        self.db.set_setting("vendor_last_sync_at", "2000-01-01 00:00:00")
        self.assertTrue(sync_due(self.db)[0])

    def test_vendor_sync_skips_settled_programs(self):
        service = ProgramService(self.db)
        service.create({
            "id": "locked", "companyName": "甲公司", "miniProgramName": "已结算程序",
            "appid": "wx-locked", "status": "已结算",
        })
        synced = upsert_records(self.db, [{
            "companyName": "甲公司", "miniProgramName": "已结算程序",
            "appid": "wx-locked", "admin": "不应写入",
        }], "test", new_companies_only=False)
        self.assertEqual(0, synced["updated"])
        self.assertEqual(1, synced["skipped"])
        matched = match_records(self.db, [{
            "miniProgramName": "已结算程序", "appid": "wx-locked", "admin": "仍不应写入",
        }], "test")
        self.assertEqual(0, matched["updated"])
        self.assertEqual(1, matched["skipped"])
        self.assertEqual("", service.get("locked")["admin"])

    def test_monitor_baseline_event_idempotency_and_matching(self):
        stamp = now()
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO companies(name,full_name,enabled,created_at,updated_at) VALUES('甲','甲公司',1,?,?)",
                (stamp, stamp),
            )
        self.db.set_setting("monitor_apihz", {"id": "from-db", "key": "from-db"})
        calls = {"run": 0, "sent": []}

        def query(_credentials, _name):
            calls["run"] += 1
            base = [{"servicename": "程序A", "icpw": "ICP-A"}]
            return base if calls["run"] == 1 else base + [{"servicename": "程序B", "icpw": "ICP-B"}]

        def sender(_ding, content):
            calls["sent"].append(content)
            return True, "ok"

        service = MonitorService(self.db, query=query, sender=sender, sleeper=lambda _: None)
        self.assertEqual("success", service.run("manual", "test-1", datetime(2026, 1, 1, 10, 0))["status"])
        self.assertEqual([], calls["sent"], "新公司首次检测只能建立 baseline")
        service.run("manual", "test-2", datetime(2026, 1, 1, 11, 0))
        service.run("manual", "test-3", datetime(2026, 1, 1, 12, 0))
        self.assertEqual(1, len(calls["sent"]))
        with self.db.connect() as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) n FROM notifications").fetchone()["n"])
            matched = conn.execute(
                "SELECT status,completed_at FROM programs WHERE mini_program_name='程序B'"
            ).fetchone()
            self.assertEqual("备案完成", matched["status"])
            self.assertEqual("2026-01-01 11:00:00", matched["completed_at"])
            self.assertEqual(2, conn.execute("SELECT COUNT(*) n FROM approved_programs").fetchone()["n"])


if __name__ == "__main__":
    unittest.main()
