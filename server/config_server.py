#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite 后端及向后兼容 HTTP API。"""
import hmac
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import Database, now
from migrate_data import migrate
from services.account import AccountService
from services.auth import AuthService
from services.monitor import MonitorBusyError, MonitorService
from services.programs import ProgramService


HOST = os.environ.get("LAINGZIZHIWANG_HOST", "127.0.0.1")
PORT = int(os.environ.get("LAINGZIZHIWANG_PORT", "8091"))
DB = Database()
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_vendor_job(mode):
    script = os.path.join(APP_DIR, "server", "vendor_sync.sh")
    try:
        result = subprocess.run(
            ["/bin/bash", script, mode], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=240,
        )
    except subprocess.TimeoutExpired:
        raise ValueError("乙方平台任务超时")
    if result.returncode == 75:
        raise ValueError("乙方平台任务正在运行，请稍后再试")
    if result.returncode:
        message = (result.stderr or result.stdout or "执行失败").strip().splitlines()[-1]
        raise ValueError(message)
    for line in reversed(result.stdout.splitlines()):
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
    return {"ok": True}


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def log(message):
    print("[%s] %s" % (now(), message), flush=True)


def monitor_config(db):
    with db.connect() as conn:
        companies = [
            {
                "name": r["name"], "fullName": r["full_name"], "enabled": bool(r["enabled"]),
                "lastCheck": r["last_check"] or "", "lastCount": r["last_count"], "hasNew": bool(r["has_new"]),
            }
            for r in conn.execute("SELECT * FROM companies ORDER BY id")
        ]
    return {
        "companies": companies,
        "times": db.get_setting("monitor_times", []),
        "apihz": db.get_setting("monitor_apihz", {}),
        "dingtalk": db.get_setting("monitor_dingtalk", {}),
    }


def save_monitor(db, data):
    if not isinstance(data, dict):
        raise ValueError("data must be an object")
    with db.transaction() as conn:
        for key in ("times", "apihz", "dingtalk"):
            if key in data:
                db.set_setting("monitor_" + key, data[key], conn)
        company_names = []
        for company in data.get("companies") or []:
            if not isinstance(company, dict) or not str(company.get("name") or "").strip():
                continue
            stamp = now()
            company_names.append(str(company["name"]).strip())
            conn.execute(
                """INSERT INTO companies(name,full_name,enabled,last_check,last_count,has_new,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET full_name=excluded.full_name,
                   enabled=excluded.enabled,updated_at=excluded.updated_at""",
                (str(company["name"]).strip(), str(company.get("fullName") or ""), int(bool(company.get("enabled", True))),
                 company.get("lastCheck"), int(company.get("lastCount") or 0), int(bool(company.get("hasNew"))),
                 stamp, stamp),
            )
        if company_names:
            placeholders = ",".join("?" for _ in company_names)
            conn.execute(
                "UPDATE companies SET enabled=0,updated_at=? WHERE name NOT IN (%s)" % placeholders,
                [now()] + company_names,
            )
        else:
            conn.execute("UPDATE companies SET enabled=0,updated_at=?", (now(),))
        db.audit("update", "monitor_config", "", {"companyCount": len(data.get("companies") or [])}, "api", conn)


def legacy_emails(db, principal=None):
    with db.connect() as conn:
        if principal and principal.get("role") != "super":
            rows = conn.execute(
                """SELECT DISTINCT e.address,e.payload,e.sort_order,e.id,e.usable,e.invalid_at,e.invalid_by
                   FROM emails e JOIN programs p ON lower(trim(p.email))=lower(trim(e.address))
                   JOIN companies c ON c.id=p.company_id
                   WHERE c.assigned_admin_id=? ORDER BY e.sort_order,e.id""",
                (principal["id"],),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT address,payload,sort_order,id,usable,invalid_at,invalid_by
                   FROM emails ORDER BY usable,sort_order,id"""
            ).fetchall()
    values = []
    for row in rows:
        payload = json.loads(row["payload"])
        if not isinstance(payload, dict):
            payload = {"email": row["address"]}
        payload["email"] = row["address"]
        payload["usable"] = bool(row["usable"])
        payload["emailStatus"] = "available" if row["usable"] else "invalid"
        payload["invalidAt"] = row["invalid_at"] or ""
        payload["invalidBy"] = row["invalid_by"] or ""
        values.append(payload)
    return values


def save_emails(db, data):
    if not isinstance(data, list):
        raise ValueError("data must be an array")
    with db.transaction() as conn:
        seen = set()
        for index, entry in enumerate(data):
            payload = entry if isinstance(entry, dict) else {"email": entry}
            address = str(payload.get("email") or payload.get("address") or "").strip()
            normalized = address.casefold()
            if not address or normalized in seen:
                continue
            seen.add(normalized)
            existing = conn.execute(
                "SELECT id FROM emails WHERE lower(trim(address))=lower(trim(?))",
                (address,),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE emails SET payload=?,sort_order=?,updated_at=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False), index, now(), existing["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO emails(address,payload,sort_order,created_at,updated_at)
                       VALUES(?,?,?,?,?)""",
                    (address, json.dumps(payload, ensure_ascii=False), index, now(), now()),
                )
        db.audit("replace", "emails", "", {"count": len(data)}, "api", conn)


def company_summaries(db, principal=None):
    where, args = "", []
    if principal and principal.get("role") != "super":
        where, args = " WHERE c.assigned_admin_id=?", [principal["id"]]
    with db.connect() as conn:
        companies = conn.execute(
            """SELECT c.*,u.username assigned_admin_username
               FROM companies c LEFT JOIN admin_users u ON u.id=c.assigned_admin_id%s
               ORDER BY c.name""" % where,
            args,
        ).fetchall()
        result = []
        for company in companies:
            statuses = conn.execute(
                """SELECT COALESCE(NULLIF(status,''),'未设置') status,COUNT(*) count
                   FROM programs WHERE company_id=? GROUP BY status""",
                (company["id"],),
            ).fetchall()
            status_counts = {row["status"]: row["count"] for row in statuses}
            total = sum(status_counts.values())
            completed = sum(status_counts.get(key, 0) for key in ("备案完成", "已结算"))
            result.append({
                "id": company["id"], "name": company["name"], "fullName": company["full_name"],
                "assignedAdminId": company["assigned_admin_id"],
                "assignedAdminUsername": company["assigned_admin_username"] or "",
                "programCount": total, "completedCount": completed,
                "progress": round(completed * 100.0 / total, 1) if total else 0,
                "statusCounts": status_counts,
            })
    return result


def approved_programs(db):
    with db.connect() as conn:
        return [
            {"companyName": r["company_name"], "companyFullName": r["company_full_name"],
             "miniProgramName": r["mini_program_name"], "approvedAt": r["approved_at"]}
            for r in conn.execute("SELECT * FROM approved_programs ORDER BY id")
        ]


def save_approved(db, data):
    if not isinstance(data, list):
        raise ValueError("data must be an array")
    with db.transaction() as conn:
        for entry in data:
            name = str(entry.get("miniProgramName") or "").strip() if isinstance(entry, dict) else ""
            if not name:
                continue
            conn.execute(
                """INSERT INTO approved_programs(company_name,company_full_name,mini_program_name,normalized_name,
                   approved_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(normalized_name) DO UPDATE SET company_name=excluded.company_name,
                   company_full_name=excluded.company_full_name,approved_at=excluded.approved_at,updated_at=excluded.updated_at""",
                (str(entry.get("companyName") or ""), str(entry.get("companyFullName") or ""), name,
                 name.casefold(), str(entry.get("approvedAt") or ""), now(), now()),
            )
            stamp = now()
            conn.execute(
                """UPDATE programs SET
                   completed_at=CASE WHEN status NOT IN ('备案完成','已结算') THEN ? ELSE completed_at END,
                   status='备案完成',updated_at=?
                   WHERE lower(trim(mini_program_name))=? AND status<>'已结算'""",
                (stamp, stamp, name.casefold()),
            )


AVATAR_TYPES = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/webp": ".webp", "image/gif": ".gif",
}


def fetch_program_avatar(program):
    url = str(program.get("avatarUrl") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("没有可下载的头像")
    request = urllib.request.Request(
        url, headers={"User-Agent": "laingzizhiwang-avatar/1.0"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        chunks, total = [], 0
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > 5 * 1024 * 1024:
                raise ValueError("头像文件过大，无法下载")
            chunks.append(chunk)
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    body = b"".join(chunks)
    if not body:
        raise ValueError("头像内容为空")
    ext = AVATAR_TYPES.get(content_type)
    if not ext:
        path = urllib.parse.urlparse(url).path
        ext = os.path.splitext(path)[1].lower() if path else ""
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            ext = ".png"
        content_type = content_type or "application/octet-stream"
    name = str(program.get("miniProgramName") or "avatar").strip() or "avatar"
    return {
        "_file": True,
        "body": body,
        "contentType": content_type or "image/png",
        "filename": name + ext,
    }


class Handler(BaseHTTPRequestHandler):
    db = DB
    current_user = None

    def log_message(self, fmt, *args):
        log(fmt % args)

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in getattr(self, "_extra_headers", []):
            self.send_header(key, value)
        self._extra_headers = []
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, payload):
        body = payload["body"]
        filename = payload.get("filename") or "avatar"
        self.send_response(200)
        self.send_header("Content-Type", payload.get("contentType") or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition",
            "attachment; filename*=UTF-8''%s" % urllib.parse.quote(filename),
        )
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            raise ValueError("invalid json")

    def _authorized(self, path):
        self.current_user = None
        if path in ("/api/health", "/api/login"):
            return True
        expected = (
            os.environ.get("LAINGZIZHIWANG_ACCESS_TOKEN")
            or os.environ.get("ACCESS_TOKEN")
            or self.db.get_setting("access_token", "")
        )
        supplied = self.headers.get("X-Auth", "")
        if expected and supplied and hmac.compare_digest(str(expected), supplied):
            self.current_user = {
                "id": 0, "username": "system", "role": "super",
                "active": True, "sessionVersion": 0, "system": True,
            }
            return True
        self.current_user = self._valid_session()
        return self.current_user is not None

    def _session_secret(self):
        return (
            os.environ.get("SESSION_SECRET")
            or os.environ.get("LAINGZIZHIWANG_ACCESS_TOKEN")
            or os.environ.get("ACCESS_TOKEN")
            or ""
        )

    def _valid_session(self):
        secret = self._session_secret()
        if not secret:
            return False
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            value = cookie["lzz_session"].value
            encoded, signature = value.split(".", 1)
            expected = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                return False
            padding = "=" * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode(encoded + padding).decode("utf-8"))
            if int(payload.get("expires") or 0) < int(time.time()):
                return None
            user = AuthService(self.db).get(int(payload.get("userId") or 0))
            if not user or not user["active"]:
                return None
            if int(user["sessionVersion"]) != int(payload.get("sessionVersion") or 0):
                return None
            return user
        except Exception:
            return None

    def _set_session(self, user):
        secret = self._session_secret()
        expires = int(time.time()) + 12 * 60 * 60
        payload = json.dumps({
            "userId": user["id"], "sessionVersion": user["sessionVersion"], "expires": expires,
        }, separators=(",", ":")).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
        secure = "; Secure" if os.environ.get("COOKIE_SECURE", "false").lower() == "true" else ""
        self._extra_headers = [(
            "Set-Cookie",
            "lzz_session=%s.%s; Path=/; Max-Age=43200; HttpOnly; SameSite=Strict%s"
            % (encoded, signature, secure),
        )]

    def _route(self, method):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if not path.startswith("/api/"):
            self._send(404, {"error": "not found"}); return
        if not self._authorized(path):
            self._send(401, {"error": "unauthorized", "message": "请重新登录"}); return
        query = urllib.parse.parse_qs(parsed.query)
        try:
            body = self._body() if method in ("POST", "PUT", "PATCH") else {}
            data = body.get("data", body) if isinstance(body, dict) else body
            result, code = self._dispatch(method, path, query, data), 200
            if method == "POST" and path == "/api/programs":
                code = 201
            if isinstance(result, dict) and result.get("_file"):
                self._send_file(result)
                return
            if isinstance(result, dict) and "data" in result:
                response = result
            elif method == "GET":
                response = {"data": result}
            else:
                response = result
            self._send(code, response)
        except MonitorBusyError as exc:
            self._send(409, {"error": "monitor busy", "message": str(exc)})
        except PermissionError as exc:
            self._send(403, {"error": "forbidden", "message": str(exc) or "无权执行该操作"})
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
        except KeyError as exc:
            self._send(400, {"error": "missing field: %s" % exc})
        except Exception as exc:
            log("请求失败 %s %s: %s" % (method, path, exc))
            self._send(500, {"error": "internal error", "message": str(exc)})

    def _dispatch(self, method, path, query, data):
        auth = AuthService(self.db)
        programs = ProgramService(self.db, self.current_user)
        if path == "/api/login" and method == "POST":
            username = str(data.get("username") or "admin") if isinstance(data, dict) else "admin"
            supplied = str(data.get("password") or "") if isinstance(data, dict) else ""
            user = auth.authenticate(username, supplied)
            if not user:
                raise ValueError("账号或密码错误")
            if not self._session_secret():
                raise ValueError("服务器未配置 SESSION_SECRET 或 ACCESS_TOKEN")
            self._set_session(user)
            return {"ok": True, "user": user}
        if path == "/api/logout" and method == "POST":
            self._extra_headers = [(
                "Set-Cookie", "lzz_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"
            )]
            return {"ok": True}
        if path == "/api/session" and method == "GET":
            return {"data": {"authenticated": True, "user": self.current_user}}
        if method == "GET" and path == "/api/health":
            with self.db.connect() as conn:
                conn.execute("SELECT 1").fetchone()
            return {"data": {"ok": True, "database": "ok", "time": now()}}

        is_super = bool(self.current_user and self.current_user.get("role") == "super")
        subadmin_allowed = (
            (path == "/api/companies" and method == "GET")
            or path == "/api/programs" or path.startswith("/api/programs/")
            or (path == "/api/emails" and method == "GET")
            or (path == "/api/exceptions" and method == "POST")
        )
        if not is_super and not subadmin_allowed:
            raise PermissionError("当前账号无权访问该功能")

        if path == "/api/admin-users":
            if method == "GET":
                return auth.list()
            if method == "POST":
                return auth.create_subadmin(data.get("username"), data.get("password"))
        if path.startswith("/api/admin-users/") and method == "PATCH":
            user_id = int(path.rsplit("/", 1)[1])
            return auth.update_subadmin(
                user_id, data.get("active") if "active" in data else None,
                data.get("password") if "password" in data else None,
            )
        if path == "/api/companies" and method == "GET":
            return company_summaries(self.db, self.current_user)
        if path.startswith("/api/companies/") and path.endswith("/assign") and method == "PATCH":
            company_id = int(path.split("/")[3])
            admin_id = data.get("adminId")
            auth.assign_company(company_id, int(admin_id) if admin_id not in (None, "") else None)
            return {"ok": True}
        if path == "/api/account-automation":
            account = AccountService(self.db)
            if method == "GET":
                return account.status()
            if method == "PATCH":
                if not isinstance(data, dict) or not isinstance(data.get("enabled"), bool):
                    raise ValueError("签到开关必须是布尔值")
                return account.set_enabled(data["enabled"], self.current_user.get("username", "api"))
        if path == "/api/vendor/settings" and method == "GET":
            return {
                "syncDays": max(1, min(365, int(self.db.get_setting("vendor_sync_days", 1) or 1))),
                "lastSyncAt": self.db.get_setting("vendor_last_sync_at", ""),
            }
        if path == "/api/vendor/settings" and method == "PATCH":
            try:
                days = int(data.get("syncDays"))
            except (TypeError, ValueError):
                raise ValueError("同步天数必须是整数")
            if days < 1 or days > 365:
                raise ValueError("同步天数必须在 1 到 365 之间")
            self.db.set_setting("vendor_sync_days", days)
            return {"syncDays": days, "lastSyncAt": self.db.get_setting("vendor_last_sync_at", "")}
        if path == "/api/vendor/sync" and method == "POST":
            return run_vendor_job("force")
        if path == "/api/vendor/match" and method == "POST":
            return run_vendor_job("match")
        if method == "GET" and path == "/api/status":
            with self.db.connect() as conn:
                last = conn.execute("SELECT * FROM monitor_runs ORDER BY id DESC LIMIT 1").fetchone()
                last_import = conn.execute("SELECT * FROM import_jobs ORDER BY id DESC LIMIT 1").fetchone()
                counts = {t: conn.execute("SELECT COUNT(*) n FROM " + t).fetchone()["n"]
                          for t in ("programs", "companies", "exceptions", "notifications")}
                counts["openExceptions"] = conn.execute(
                    "SELECT COUNT(*) n FROM exceptions WHERE status='open'"
                ).fetchone()["n"]
                counts["pendingNotifications"] = conn.execute(
                    "SELECT COUNT(*) n FROM notifications WHERE status<>'sent'"
                ).fetchone()["n"]
            return {"data": {
                "ok": True, "counts": counts, "lastRun": dict(last) if last else None,
                "lastImport": dict(last_import) if last_import else None,
            }}
        if path == "/api/audit" and method == "GET":
            limit = min(500, max(1, int(query.get("limit", ["100"])[0])))
            with self.db.connect() as conn:
                rows = conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
                undone = {
                    r["entity_id"] for r in conn.execute(
                        "SELECT entity_id FROM audit_logs WHERE action='undo' AND entity_type='audit'"
                    )
                }
            result = []
            for row in rows:
                item = dict(row)
                item["detail"] = json.loads(item["detail"])
                item["undone"] = str(item["id"]) in undone
                result.append(item)
            return result
        if path.startswith("/api/audit/") and path.endswith("/undo") and method == "POST":
            audit_id = int(path.split("/")[3])
            with self.db.connect() as conn:
                row = conn.execute("SELECT * FROM audit_logs WHERE id=?", (audit_id,)).fetchone()
                already = conn.execute(
                    "SELECT 1 FROM audit_logs WHERE action='undo' AND entity_type='audit' AND entity_id=?",
                    (str(audit_id),),
                ).fetchone()
            if not row or row["entity_type"] != "program":
                raise ValueError("该审计记录不可撤销")
            if already:
                raise ValueError("该操作已经撤销")
            detail = json.loads(row["detail"])
            if row["action"] == "create":
                programs.delete(row["entity_id"], "undo")
            elif row["action"] == "update":
                programs.update(row["entity_id"], detail.get("before") or {}, "undo")
            elif row["action"] == "delete":
                before = detail.get("before") or {}
                before["id"] = row["entity_id"]
                programs.create(before, "undo")
            else:
                raise ValueError("该操作类型不可撤销")
            self.db.audit("undo", "audit", str(audit_id), {"programId": row["entity_id"]}, "api")
            return {"ok": True}
        if path == "/api/emails":
            if method == "GET": return legacy_emails(self.db, self.current_user)
            if method == "POST": save_emails(self.db, data); return {"ok": True}
        if path.startswith("/api/emails/") and method == "DELETE":
            address = urllib.parse.unquote(path.split("/", 3)[3])
            with self.db.transaction() as conn:
                used = conn.execute(
                    """SELECT COUNT(*) n FROM programs
                       WHERE lower(trim(email))=lower(trim(?))""",
                    (address,),
                ).fetchone()["n"]
                if used:
                    raise ValueError("该邮箱仍被 %d 个小程序使用" % used)
                conn.execute(
                    "DELETE FROM emails WHERE lower(trim(address))=lower(trim(?))",
                    (address,),
                )
                self.db.audit("delete", "email", address, {}, "api", conn)
            return {"ok": True}
        if path == "/api/monitor":
            if method == "GET": return monitor_config(self.db)
            if method == "POST": save_monitor(self.db, data); return {"ok": True}
        if path == "/api/monitor/run" and method == "POST":
            return MonitorService(self.db).run("manual")
        if path == "/api/monitor/runs" and method == "GET":
            limit = min(200, int(query.get("limit", ["50"])[0]))
            with self.db.connect() as conn:
                return [dict(r) for r in conn.execute("SELECT * FROM monitor_runs ORDER BY id DESC LIMIT ?", (limit,))]
        if path == "/api/approved-programs":
            if method == "GET": return approved_programs(self.db)
            if method == "POST": save_approved(self.db, data); return {"ok": True, "count": len(data)}
        if path == "/api/programs" and method == "GET":
            return programs.list(query.get("page", ["1"])[0], query.get("pageSize", ["50"])[0],
                                 query.get("search", [""])[0], query.get("company", [""])[0],
                                 query.get("status", [""])[0])
        if path == "/api/programs" and method == "POST": return programs.create(data)
        if path == "/api/programs/bulk" and method == "POST":
            items = data.get("items", []) if isinstance(data, dict) else data
            if not isinstance(items, list): raise ValueError("items must be an array")
            return {"items": programs.bulk(items), "count": len(items)}
        if path.startswith("/api/programs/") and path.endswith("/refresh-email") and method == "POST":
            pid = urllib.parse.unquote(path.split("/")[3])
            value = programs.refresh_email(pid)
            if value is None:
                raise KeyError("program")
            return value
        if path.startswith("/api/programs/") and path.endswith("/avatar") and method == "GET":
            pid = urllib.parse.unquote(path.split("/")[3])
            value = programs.get(pid)
            if value is None:
                raise KeyError("program")
            return fetch_program_avatar(value)
        if path.startswith("/api/programs/"):
            pid = urllib.parse.unquote(path.split("/", 3)[3])
            if method == "GET":
                value = programs.get(pid)
                if value is None: raise KeyError("program")
                return value
            if method in ("PUT", "PATCH"):
                value = programs.update(pid, data)
                if value is None: raise KeyError("program")
                return value
            if method == "DELETE": return {"ok": programs.delete(pid)}
        if path == "/api/exceptions":
            if method == "GET":
                with self.db.connect() as conn:
                    return [dict(r) for r in conn.execute("SELECT * FROM exceptions ORDER BY id DESC")]
            if method == "POST":
                if not is_super:
                    with self.db.connect() as conn:
                        allowed = conn.execute(
                            "SELECT 1 FROM companies WHERE name=? AND assigned_admin_id=?",
                            (str(data.get("companyName") or ""), self.current_user["id"]),
                        ).fetchone()
                    if not allowed:
                        raise PermissionError("无权为该公司创建异常记录")
                with self.db.connect() as conn:
                    cur = conn.execute(
                        """INSERT INTO exceptions(program_id,company_name,mini_program_name,reason,status,payload,
                           created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
                        (data.get("programId"), str(data.get("companyName") or ""), str(data.get("miniProgramName") or ""),
                         str(data["reason"]), str(data.get("status") or "open"),
                         json.dumps(data.get("payload") or {}, ensure_ascii=False), now(), now()),
                    )
                    return {"id": cur.lastrowid}
        if path.startswith("/api/exceptions/"):
            eid = int(path.rsplit("/", 1)[1])
            if method in ("PUT", "PATCH"):
                status = str(data.get("status") or "open")
                with self.db.connect() as conn:
                    conn.execute(
                        """UPDATE exceptions SET reason=COALESCE(?,reason),status=?,payload=COALESCE(?,payload),
                           resolved_at=?,updated_at=? WHERE id=?""",
                        (data.get("reason"), status,
                         json.dumps(data["payload"], ensure_ascii=False) if "payload" in data else None,
                         now() if status == "resolved" else None, now(), eid),
                    )
                return {"ok": True}
            if method == "DELETE":
                with self.db.connect() as conn: conn.execute("DELETE FROM exceptions WHERE id=?", (eid,))
                return {"ok": True}
        raise KeyError("endpoint")

    def do_GET(self): self._route("GET")
    def do_POST(self): self._route("POST")
    def do_PUT(self): self._route("PUT")
    def do_PATCH(self): self._route("PATCH")
    def do_DELETE(self): self._route("DELETE")


def main():
    DB.initialize()
    result = migrate(DB)
    AuthService(DB).ensure_superadmin(
        os.environ.get("APP_PASSWORD") or os.environ.get("ACCESS_TOKEN") or ""
    )
    log("迁移状态: %s" % result)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log("服务启动 %s:%d SQLite=%s" % (HOST, PORT, DB.path))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
