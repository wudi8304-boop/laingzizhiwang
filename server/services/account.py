import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime

from db import now


CHECKIN_URL = "https://cn.apihz.cn/api/xitong/function.php"
ACCOUNT_URL = "https://cn.apihz.cn/api/xitong/info.php"


class AccountService:
    def __init__(self, db, requester=None, sleeper=time.sleep):
        self.db = db
        self.requester = requester or self._request
        self.sleep = sleeper

    def status(self):
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM apihz_account_runs ORDER BY run_date DESC LIMIT 1"
            ).fetchone()
        return {
            "enabled": bool(self.db.get_setting("apihz_checkin_enabled", False)),
            "time": "00:02",
            "lastRun": dict(row) if row else None,
        }

    def set_enabled(self, enabled, actor="api"):
        value = bool(enabled)
        with self.db.transaction() as conn:
            self.db.set_setting("apihz_checkin_enabled", value, conn)
            self.db.audit(
                "update", "apihz_checkin", "", {"enabled": value}, actor, conn
            )
        return self.status()

    def run_if_due(self, at=None):
        at = at or datetime.now()
        if not self.db.get_setting("apihz_checkin_enabled", False):
            return {"skipped": True, "reason": "disabled"}
        if at.strftime("%H:%M") != "00:02":
            return {"skipped": True, "reason": "not due"}
        return self.run(at)

    def run(self, at=None):
        at = at or datetime.now()
        run_date = at.strftime("%Y-%m-%d")
        with self.db.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM apihz_account_runs WHERE run_date=?", (run_date,)
            ).fetchone()
        if existing and existing["status"] == "success":
            return {"skipped": True, "reason": "already completed", "date": run_date}

        credentials = self._credentials()
        if not credentials["id"] or not credentials["key"]:
            error = "未配置 APIHZ_ID/APIHZ_KEY 或数据库监控凭据"
            self._record_failure(run_date, "failed", error, False)
            self._notify_failure(run_date, "APIHZ 每日签到失败", error)
            raise ValueError(error)

        signed = bool(existing and existing["checkin_succeeded"])
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO apihz_account_runs(
                   run_date,status,checkin_succeeded,checkin_message,balance,
                   account_payload,started_at,finished_at,error
                   ) VALUES(?,'running',0,'','','{}',?,NULL,NULL)
                   ON CONFLICT(run_date) DO UPDATE SET
                   status='running',started_at=excluded.started_at,finished_at=NULL,error=NULL""",
                (run_date, now()),
            )
        try:
            if not signed:
                checkin = self._with_retry(
                    os.environ.get("APIHZ_CHECKIN_URL", CHECKIN_URL),
                    {"id": credentials["id"], "key": credentials["key"], "type": "1"},
                )
                if int(checkin.get("code") or 0) != 200:
                    raise RuntimeError(checkin.get("msg") or "APIHZ 每日签到失败")
                message = str(checkin.get("msg") or "签到成功")
                with self.db.connect() as conn:
                    conn.execute(
                        """UPDATE apihz_account_runs SET status='checkin_success',
                           checkin_succeeded=1,checkin_message=?,error=NULL
                           WHERE run_date=?""",
                        (message, run_date),
                    )
                signed = True

            account = self._with_retry(
                os.environ.get("APIHZ_ACCOUNT_URL", ACCOUNT_URL),
                {"id": credentials["id"], "key": credentials["key"]},
            )
            if int(account.get("code") or 0) != 200:
                raise RuntimeError(account.get("msg") or "APIHZ 盟点余额查询失败")
            balance = str(account.get("md") or "")
            with self.db.transaction() as conn:
                conn.execute(
                    """UPDATE apihz_account_runs SET status='success',balance=?,
                       account_payload=?,finished_at=?,error=NULL WHERE run_date=?""",
                    (balance, json.dumps(account, ensure_ascii=False), now(), run_date),
                )
                self.db.audit(
                    "run", "apihz_checkin", run_date,
                    {"status": "success", "balance": balance}, "cron", conn,
                )
                conn.execute(
                    """UPDATE exceptions SET status='resolved',resolved_at=?,updated_at=?
                       WHERE status='open' AND reason IN ('APIHZ每日签到失败','APIHZ盟点余额查询失败')""",
                    (now(), now()),
                )
            return {
                "date": run_date, "status": "success", "balance": balance,
                "message": self.status()["lastRun"]["checkin_message"],
            }
        except Exception as exc:
            status = "balance_failed" if signed else "failed"
            self._record_failure(run_date, status, str(exc), signed)
            title = "APIHZ 盟点余额查询失败" if signed else "APIHZ 每日签到失败"
            self._notify_failure(run_date, title, str(exc))
            raise

    def _credentials(self):
        config = dict(self.db.get_setting("monitor_apihz", {}) or {})
        return {
            "id": str(os.environ.get("APIHZ_ID", config.get("id", "")) or ""),
            "key": str(os.environ.get("APIHZ_KEY", config.get("key", "")) or ""),
        }

    def _with_retry(self, endpoint, params):
        last_error = None
        for attempt in range(3):
            try:
                return self.requester(endpoint, params)
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    self.sleep(2 ** attempt)
        raise last_error

    @staticmethod
    def _request(endpoint, params):
        request = urllib.request.Request(
            endpoint + "?" + urllib.parse.urlencode(params),
            headers={"User-Agent": "laingzizhiwang-account/1.0"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    def _record_failure(self, run_date, status, error, signed):
        stamp = now()
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO apihz_account_runs(
                   run_date,status,checkin_succeeded,checkin_message,balance,
                   account_payload,started_at,finished_at,error
                   ) VALUES(?,?,?,'','','{}',?,?,?)
                   ON CONFLICT(run_date) DO UPDATE SET status=excluded.status,
                   checkin_succeeded=MAX(checkin_succeeded,excluded.checkin_succeeded),
                   finished_at=excluded.finished_at,error=excluded.error""",
                (run_date, status, int(signed), stamp, stamp, error),
            )
            reason = "APIHZ盟点余额查询失败" if signed else "APIHZ每日签到失败"
            existing = conn.execute(
                """SELECT id FROM exceptions WHERE status='open' AND reason=?
                   AND company_name='' AND mini_program_name='' LIMIT 1""",
                (reason,),
            ).fetchone()
            payload = json.dumps({"date": run_date, "error": error}, ensure_ascii=False)
            if existing:
                conn.execute(
                    "UPDATE exceptions SET payload=?,updated_at=? WHERE id=?",
                    (payload, stamp, existing["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO exceptions(
                       company_name,mini_program_name,reason,status,payload,created_at,updated_at
                       ) VALUES('','',?,'open',?,?,?)""",
                    (reason, payload, stamp, stamp),
                )
            self.db.audit(
                "run", "apihz_checkin", run_date,
                {"status": status, "error": error}, "cron", conn,
            )

    def _notify_failure(self, run_date, title, error):
        event_key = "apihz-checkin:%s:%s" % (run_date, title)
        stamp = now()
        with self.db.connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO notifications(
                   event_key,channel,payload,status,created_at,updated_at
                   ) VALUES(?,'dingtalk',?,'pending',?,?)""",
                (
                    event_key,
                    json.dumps(
                        {"content": "【自动化异常】%s：%s" % (title, error)},
                        ensure_ascii=False,
                    ),
                    stamp, stamp,
                ),
            )
        try:
            from services.monitor import MonitorService
            config = dict(self.db.get_setting("monitor_dingtalk", {}) or {})
            config["webhook"] = os.environ.get(
                "DINGTALK_WEBHOOK", config.get("webhook", "")
            )
            config["secret"] = os.environ.get(
                "DINGTALK_SECRET", config.get("secret", "")
            )
            MonitorService(self.db, sleeper=self.sleep).retry_notifications(config)
        except Exception:
            pass
