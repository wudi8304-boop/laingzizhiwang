import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta

from db import now


def normalize_item(value):
    if not isinstance(value, dict):
        value = {"name": value[0], "icp": value[1] if len(value) > 1 else ""}
    name = value.get("servicename") or value.get("name") or value.get("xcxname") or value.get("miniProgramName") or ""
    icp = value.get("icpw") or value.get("icp") or value.get("beian") or value.get("record") or ""
    return {"name": str(name).strip(), "icp": str(icp).strip()}


def item_key(item):
    return ("name:" + item["name"]) if item.get("name") else (("icp:" + item["icp"]) if item.get("icp") else "")


class MonitorBusyError(RuntimeError):
    pass


class MonitorService:
    def __init__(self, db, query=None, sender=None, sleeper=time.sleep):
        self.db = db
        self.query = query or self._query_all_pages
        self.sender = sender or self._send_dingtalk
        self.sleep = sleeper

    def run_if_due(self, at=None):
        at = at or datetime.now()
        times = self.db.get_setting("monitor_times", []) or []
        normalized = {str(t).strip()[:5] for t in times}
        hm = at.strftime("%H:%M")
        if hm == "00:02":
            return {"skipped": True, "reason": "reserved for account checkin"}
        if hm not in normalized:
            return {"skipped": True, "reason": "not due"}
        return self.run("scheduled", at.strftime("%Y-%m-%d %H:%M"), at)

    def run(self, trigger_type="manual", trigger_key=None, at=None):
        at = at or datetime.now()
        trigger_key = trigger_key or ("%s:%s" % (trigger_type, uuid.uuid4().hex))
        token = uuid.uuid4().hex
        run_id = self._acquire(token, trigger_type, trigger_key)
        if run_id is None:
            return {"skipped": True, "reason": "duplicate", "triggerKey": trigger_key}
        success = failures = 0
        try:
            config = self._credentials()
            if not config["apihz"].get("id") or not config["apihz"].get("key"):
                raise ValueError("未配置 APIHZ_ID/APIHZ_KEY 或数据库监控凭据")
            with self.db.connect() as conn:
                companies = conn.execute("SELECT * FROM companies WHERE enabled=1 ORDER BY id").fetchall()
            with self.db.connect() as conn:
                conn.execute("UPDATE monitor_runs SET total_companies=? WHERE id=?", (len(companies), run_id))
            for company in companies:
                try:
                    raw_items = self._query_with_retry(
                        config["apihz"], company["full_name"] or company["name"]
                    )
                    self._record_company(run_id, company, raw_items, config["dingtalk"], at)
                    success += 1
                except Exception as exc:
                    failures += 1
                    self.db.audit("monitor_company_failed", "company", company["id"], {"error": str(exc)})
                    self._open_exception(
                        "备案检测连续失败", company["name"], "",
                        {"error": str(exc), "runId": run_id},
                    )
            self.retry_notifications(config["dingtalk"])
            status = "success" if not failures else ("partial" if success else "failed")
            with self.db.connect() as conn:
                conn.execute(
                    """UPDATE monitor_runs SET status=?,finished_at=?,success_count=?,failure_count=? WHERE id=?""",
                    (status, now(), success, failures, run_id),
                )
            return {"runId": run_id, "status": status, "success": success, "failed": failures}
        except Exception as exc:
            with self.db.connect() as conn:
                conn.execute("UPDATE monitor_runs SET status='failed',finished_at=?,error=? WHERE id=?",
                             (now(), str(exc), run_id))
            raise
        finally:
            self._release(token)

    def _query_with_retry(self, credentials, company_name):
        last_error = None
        for attempt in range(3):
            try:
                return self.query(credentials, company_name)
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    self.sleep(2 ** attempt)
        raise last_error

    def _open_exception(self, reason, company_name, mini_program_name, payload):
        with self.db.transaction() as conn:
            existing = conn.execute(
                """SELECT id FROM exceptions WHERE status='open' AND reason=?
                   AND company_name=? AND mini_program_name=? LIMIT 1""",
                (reason, company_name, mini_program_name),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE exceptions SET payload=?,updated_at=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False), now(), existing["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO exceptions(company_name,mini_program_name,reason,status,payload,created_at,updated_at)
                       VALUES(?,?,?,'open',?,?,?)""",
                    (company_name, mini_program_name, reason, json.dumps(payload, ensure_ascii=False), now(), now()),
                )

    def _acquire(self, token, trigger_type, trigger_key):
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT id FROM monitor_runs WHERE trigger_key=?", (trigger_key,)).fetchone()
            if existing:
                return None
            lock = self.db.get_setting("monitor_lock", None, conn)
            if lock:
                try:
                    locked_at = datetime.strptime(lock["at"], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    locked_at = datetime.now()
                if datetime.now() - locked_at < timedelta(minutes=30):
                    raise MonitorBusyError("已有检测任务运行中")
            self.db.set_setting("monitor_lock", {"token": token, "at": now()}, conn)
            cur = conn.execute(
                "INSERT INTO monitor_runs(trigger_type,trigger_key,status,started_at) VALUES(?,?,?,?)",
                (trigger_type, trigger_key, "running", now()),
            )
            return cur.lastrowid

    def _release(self, token):
        with self.db.transaction() as conn:
            lock = self.db.get_setting("monitor_lock", None, conn)
            if lock and lock.get("token") == token:
                conn.execute("DELETE FROM settings WHERE key='monitor_lock'")

    def _record_company(self, run_id, company, raw_items, ding, at):
        items = [normalize_item(x) for x in raw_items]
        items = [x for x in items if item_key(x)]
        stamp = at.strftime("%Y-%m-%d %H:%M:%S")
        with self.db.transaction() as conn:
            had_baseline = bool(company["last_check"]) or conn.execute(
                """SELECT 1 FROM monitor_items mi JOIN monitor_runs mr ON mr.id=mi.run_id
                   WHERE mi.company_id=? AND mr.id<>? AND mr.status IN ('success','partial') LIMIT 1""",
                (company["id"], run_id),
            ).fetchone() is not None
            previous = {
                row["item_key"] for row in conn.execute(
                    """SELECT mi.item_key FROM monitor_items mi JOIN monitor_runs mr ON mr.id=mi.run_id
                       WHERE mi.company_id=? AND mr.id<>? AND mr.status IN ('success','partial')
                       ORDER BY mr.id DESC""", (company["id"], run_id)
                ).fetchall()
            }
            new_items = []
            for item in items:
                key = item_key(item)
                is_new = int(had_baseline and key not in previous)
                conn.execute(
                    """INSERT OR IGNORE INTO monitor_items(run_id,company_id,item_key,name,icp,is_new,raw_json,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (run_id, company["id"], key, item["name"], item["icp"], is_new,
                     json.dumps(item, ensure_ascii=False), stamp),
                )
                if item["name"]:
                    self._approve_and_match(conn, run_id, company, item, stamp)
                if is_new:
                    new_items.append(item)
                    event_key = "filing:%s:%s" % (company["id"], key)
                    payload = {
                        "companyName": company["name"], "companyFullName": company["full_name"],
                        "name": item["name"], "icp": item["icp"], "detectedAt": stamp,
                    }
                    conn.execute(
                        """INSERT OR IGNORE INTO notifications(event_key,payload,status,created_at,updated_at)
                           VALUES(?,?,'pending',?,?)""",
                        (event_key, json.dumps(payload, ensure_ascii=False), stamp, stamp),
                    )
            conn.execute(
                "UPDATE companies SET last_check=?,last_count=?,has_new=?,updated_at=? WHERE id=?",
                (stamp, len(items), int(bool(new_items)), now(), company["id"]),
            )

    def _approve_and_match(self, conn, run_id, company, item, stamp):
        normalized = item["name"].casefold()
        conn.execute(
            """INSERT INTO approved_programs(company_id,company_name,company_full_name,mini_program_name,
               normalized_name,icp,approved_at,source_run_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(normalized_name) DO UPDATE SET
               company_id=excluded.company_id,company_name=excluded.company_name,company_full_name=excluded.company_full_name,
               icp=excluded.icp,approved_at=excluded.approved_at,source_run_id=excluded.source_run_id,updated_at=excluded.updated_at""",
            (company["id"], company["name"], company["full_name"], item["name"], normalized, item["icp"],
             stamp, run_id, stamp, stamp),
        )
        rows = conn.execute("SELECT id FROM programs WHERE lower(trim(mini_program_name))=?", (normalized,)).fetchall()
        if rows:
            for row in rows:
                conn.execute(
                    """UPDATE programs SET
                       completed_at=CASE WHEN status NOT IN ('备案完成','已结算') THEN ? ELSE completed_at END,
                       status='备案完成',updated_at=? WHERE id=? AND status<>'已结算'""",
                    (stamp, stamp, row["id"]),
                )
        else:
            pid = "auto_" + hashlib.sha256((str(company["id"]) + ":" + normalized).encode("utf-8")).hexdigest()[:16]
            email_row = conn.execute(
                """SELECT e.address FROM emails e
                   WHERE e.usable=1 AND NOT EXISTS (
                     SELECT 1 FROM programs p
                     WHERE lower(trim(p.email))=lower(trim(e.address))
                   )
                   ORDER BY e.sort_order,e.id LIMIT 1"""
            ).fetchone()
            assigned_email = email_row["address"] if email_row else ""
            cur = conn.execute(
                """INSERT OR IGNORE INTO programs(
                   id,company_id,company_name,mini_program_name,status,email,task_reason,completed_at,
                   source,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,'monitor',?,?)""",
                (pid, company["id"], company["name"], item["name"], "备案完成", assigned_email,
                 "待补资料", stamp, stamp, stamp),
            )
            if cur.rowcount:
                conn.execute(
                    """INSERT INTO exceptions(program_id,company_name,mini_program_name,reason,status,payload,
                       created_at,updated_at) VALUES(?,?,?,?,?,'{}',?,?)""",
                    (pid, company["name"], item["name"], "监控自动建档，需补充账号资料", "open", stamp, stamp),
                )

    def retry_notifications(self, ding):
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM notifications
                   WHERE status IN ('pending','retry','failed') AND attempts<9
                   AND (next_retry_at IS NULL OR next_retry_at<=?) ORDER BY id""",
                (now(),),
            ).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            content = str(payload.get("content") or "")
            if not content:
                content = "【备案监控】%s 新增小程序：%s%s" % (
                    payload["companyName"], payload["name"],
                    ("（%s）" % payload["icp"]) if payload.get("icp") else "",
                )
            attempts = row["attempts"]
            attempts_this_run = 0
            while attempts_this_run < 3 and attempts < 9:
                ok, message = self.sender(ding, content)
                attempts += 1
                attempts_this_run += 1
                with self.db.connect() as conn:
                    if ok:
                        conn.execute(
                            """UPDATE notifications SET status='sent',attempts=?,sent_at=?,last_error=NULL,
                               next_retry_at=NULL,updated_at=? WHERE id=?""",
                            (attempts, now(), now(), row["id"]),
                        )
                    else:
                        status = "failed" if attempts >= 9 else "retry"
                        conn.execute(
                            """UPDATE notifications SET status=?,attempts=?,last_error=?,next_retry_at=?,updated_at=?
                               WHERE id=?""",
                            (status, attempts, message, (datetime.now() + timedelta(seconds=2 ** attempts)).strftime(
                                "%Y-%m-%d %H:%M:%S"), now(), row["id"]),
                        )
                if ok:
                    break
                if attempts_this_run < 3 and attempts < 9:
                    self.sleep(2 ** attempts_this_run)
            if not ok and attempts >= 9:
                self._open_exception(
                    "钉钉通知多次发送失败", payload.get("companyName", ""), payload.get("name", ""),
                    {"notificationId": row["id"], "error": message},
                )

    def _credentials(self):
        apihz = dict(self.db.get_setting("monitor_apihz", {}) or {})
        ding = dict(self.db.get_setting("monitor_dingtalk", {}) or {})
        apihz["id"] = os.environ.get("APIHZ_ID", apihz.get("id", ""))
        apihz["key"] = os.environ.get("APIHZ_KEY", apihz.get("key", ""))
        ding["webhook"] = os.environ.get("DINGTALK_WEBHOOK", ding.get("webhook", ""))
        ding["secret"] = os.environ.get("DINGTALK_SECRET", ding.get("secret", ""))
        return {"apihz": apihz, "dingtalk": ding}

    def _query_all_pages(self, credentials, main_name):
        endpoint = os.environ.get(
            "APIHZ_URL",
            self.db.get_setting("apihz_url", "https://cn.apihz.cn/api/wangzhan/syicpxcx.php"),
        )
        result, seen, page = [], set(), 1
        while True:
            params = {"id": credentials["id"], "key": credentials["key"], "main": main_name,
                      "hctype": "1", "page": str(page)}
            req = urllib.request.Request(endpoint + "?" + urllib.parse.urlencode(params),
                                         headers={"User-Agent": "laingzizhiwang-monitor/2.0"})
            with urllib.request.urlopen(req, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8"))
            if data.get("code") != 200:
                raise RuntimeError(data.get("msg") or "备案接口查询失败")
            batch = data.get("datas") if isinstance(data.get("datas"), list) else []
            fresh = set()
            for value in batch:
                key = item_key(normalize_item(value))
                if key and key not in seen and key not in fresh:
                    result.append(value)
                    fresh.add(key)
            seen.update(fresh)
            total = int(data.get("total") or len(result))
            if not batch or len(result) >= total or not fresh:
                break
            page += 1
            if page > 1000:
                raise RuntimeError("备案接口分页超过安全上限")
        return result

    def _send_dingtalk(self, ding, content):
        webhook = ding.get("webhook") or ""
        if not webhook:
            return False, "未配置 DINGTALK_WEBHOOK"
        keyword = ding.get("keyword") or "备案监控"
        text = content if keyword in content else keyword + " " + content
        target = webhook
        proxy = os.environ.get("DINGTALK_PROXY_URL") or self.db.get_setting("dingtalk_proxy_url", "")
        if proxy:
            match = re.search(r"access_token=([^&]+)", webhook)
            if not match:
                return False, "Webhook 中缺少 access_token"
            target = proxy + "?access_token=" + match.group(1)
        secret = ding.get("secret")
        if secret:
            timestamp = str(int(time.time() * 1000))
            digest = hmac.new(secret.encode(), ("%s\n%s" % (timestamp, secret)).encode(), hashlib.sha256).digest()
            target += ("&" if "?" in target else "?") + urllib.parse.urlencode(
                {"timestamp": timestamp, "sign": base64.b64encode(digest).decode()}
            )
        body = json.dumps({"msgtype": "text", "text": {"content": text}}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(target, data=body, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8"))
            return data.get("errcode") == 0, data.get("errmsg") or "发送失败"
        except Exception as exc:
            return False, str(exc)
