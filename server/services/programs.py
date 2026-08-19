import json
import uuid

from db import now


DEFAULT_STATUS = "待注册"
STATUS_ALIASES = {
    "": DEFAULT_STATUS,
    "审核中": "备案中",
    "审核通过": "备案完成",
    "待验收": "备案完成",
    "已验收": "已结算",
}
FIELDS = {
    "companyName": "company_name", "miniProgramName": "mini_program_name",
    "avatarUrl": "avatar_url", "description": "description", "category": "category", "appid": "appid",
    "originalId": "original_id", "secret": "secret", "admin": "admin",
    "legalPersonPhone": "legal_person_phone", "miniProgramPhone": "mini_program_phone",
    "status": "status",
    "email": "email", "miniProgramPassword": "mini_program_password", "submitDate": "submit_date",
    "taskReason": "task_reason", "externalId": "external_id",
}


def normalize_status(value):
    text = str(value or "").strip()
    return STATUS_ALIASES.get(text, text)


def external(row):
    if not row:
        return None
    d = dict(row)
    result = {"id": d["id"]}
    for api, col in FIELDS.items():
        result[api] = d.get(col, "")
    result["completionTime"] = d.get("completed_at", "")
    result["settlementTime"] = d.get("settled_at", "")
    result["createdAt"] = d.get("created_at")
    result["updatedAt"] = d.get("updated_at")
    return result


class ProgramService:
    def __init__(self, db, principal=None):
        self.db = db
        self.principal = principal

    def _scoped(self):
        return bool(self.principal and self.principal.get("role") != "super")

    def _scope(self):
        if not self._scoped():
            return "", []
        return (
            "company_id IN (SELECT id FROM companies WHERE assigned_admin_id=?)",
            [int(self.principal["id"])],
        )

    def _actor(self, actor):
        if actor != "api" or not self.principal:
            return actor
        return str(self.principal.get("username") or "api")

    def _assert_company(self, conn, company_name):
        if not self._scoped():
            return
        row = conn.execute(
            "SELECT id FROM companies WHERE name=? AND assigned_admin_id=?",
            (str(company_name or ""), int(self.principal["id"])),
        ).fetchone()
        if not row:
            raise PermissionError("无权操作该公司")

    def _assert_program(self, conn, program_id):
        scope, scope_args = self._scope()
        sql = "SELECT * FROM programs WHERE id=?"
        args = [program_id]
        if scope:
            sql += " AND " + scope
            args.extend(scope_args)
        row = conn.execute(sql, args).fetchone()
        if not row and self._scoped():
            raise PermissionError("无权操作该小程序")
        return row

    def list(self, page=1, page_size=50, search="", company="", status=""):
        page, page_size = max(1, int(page)), min(200, max(1, int(page_size)))
        where, args = [], []
        scope, scope_args = self._scope()
        if scope:
            where.append(scope)
            args.extend(scope_args)
        if search:
            where.append(
                "(mini_program_name LIKE ? OR company_name LIKE ? OR appid LIKE ? OR email LIKE ?"
                " OR legal_person_phone LIKE ? OR mini_program_phone LIKE ?)"
            )
            args.extend(["%%%s%%" % search] * 6)
        if company:
            where.append("company_name=?"); args.append(company)
        if status:
            where.append("status=?"); args.append(status)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self.db.connect() as conn:
            total = conn.execute("SELECT COUNT(*) n FROM programs" + clause, args).fetchone()["n"]
            rows = conn.execute(
                "SELECT * FROM programs%s ORDER BY id LIMIT ? OFFSET ?" % clause,
                args + [page_size, (page - 1) * page_size],
            ).fetchall()
        return {"items": [external(r) for r in rows], "total": total, "page": page, "pageSize": page_size}

    def get(self, program_id):
        with self.db.connect() as conn:
            row = self._assert_program(conn, program_id)
        return external(row)

    def create(self, data, actor="api"):
        data = dict(data)
        data["status"] = normalize_status(data.get("status"))
        self._apply_business_rules(data)
        actor = self._actor(actor)
        program_id = str(data.get("id") or ("rec_" + uuid.uuid4().hex[:12]))
        stamp = now()
        completed_at = stamp if data["status"] == "备案完成" else ""
        settled_at = stamp if data["status"] == "已结算" else ""
        values = [str(data.get(k) or "") for k in FIELDS]
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM programs WHERE id=?", (program_id,)).fetchone():
                raise ValueError("program id already exists")
            company_name = str(data.get("companyName") or "")
            self._assert_company(conn, company_name)
            company_id = self._company_id(conn, company_name)
            self._assert_email_available(conn, str(data.get("email") or ""), program_id)
            conn.execute(
                """INSERT INTO programs(id,company_id,company_name,mini_program_name,avatar_url,description,category,
                   appid,original_id,secret,admin,legal_person_phone,mini_program_phone,status,email,
                   mini_program_password,submit_date,task_reason,external_id,completed_at,settled_at,source,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [program_id, company_id] + values + [completed_at, settled_at, actor, stamp, stamp],
            )
            self._ensure_email(conn, str(data.get("email") or ""))
            created = dict(data)
            created["id"] = program_id
            self.db.audit("create", "program", program_id, {"after": created}, actor, conn)
        return self.get(program_id)

    def update(self, program_id, data, actor="api"):
        data = dict(data)
        if "status" in data:
            data["status"] = normalize_status(data.get("status"))
        with self.db.connect() as conn:
            current_row = self._assert_program(conn, program_id)
        if not current_row:
            return None
        if current_row["status"] == "已结算":
            raise ValueError("已结算的小程序已锁定，不允许修改")
        actor = self._actor(actor)
        current = external(current_row)
        self._apply_business_rules(data, current)
        updates, args = [], []
        stamp = now()
        for api, col in FIELDS.items():
            if api in data:
                updates.append(col + "=?"); args.append(str(data[api] or ""))
        if not updates:
            return self.get(program_id)
        with self.db.transaction() as conn:
            if "email" in data:
                self._assert_email_available(conn, str(data.get("email") or ""), program_id)
            target_status = str(data.get("status") or "")
            if target_status == "备案完成" and current.get("status") != "备案完成":
                updates.append("completed_at=?")
                args.append(stamp)
            elif "status" in data and target_status not in ("备案完成", "已结算"):
                updates.append("completed_at=?")
                args.append("")
            if target_status == "已结算" and current.get("status") != "已结算":
                updates.append("settled_at=?")
                args.append(stamp)
            if "companyName" in data:
                self._assert_company(conn, str(data.get("companyName") or ""))
                updates.append("company_id=?")
                args.append(self._company_id(conn, str(data.get("companyName") or "")))
            updates.append("updated_at=?"); args.append(stamp); args.append(program_id)
            conn.execute("UPDATE programs SET %s WHERE id=?" % ",".join(updates), args)
            if "email" in data:
                self._ensure_email(conn, str(data.get("email") or ""))
            if data.get("taskReason") == "":
                conn.execute(
                    """UPDATE exceptions SET status='resolved',resolved_at=?,updated_at=?
                       WHERE program_id=? AND status='open'""",
                    (now(), now(), program_id),
                )
            self.db.audit(
                "update", "program", program_id, {"before": current, "changes": data}, actor, conn
            )
        return self.get(program_id)

    def refresh_email(self, program_id, actor="api"):
        actor = self._actor(actor)
        with self.db.transaction() as conn:
            row = self._assert_program(conn, program_id)
            if not row:
                return None
            if row["status"] == "已结算":
                raise ValueError("已结算的小程序已锁定，不允许更换邮箱")
            replacement = conn.execute(
                """SELECT e.address FROM emails e
                   WHERE e.usable=1
                     AND lower(trim(e.address))<>lower(trim(?))
                     AND NOT EXISTS (
                       SELECT 1 FROM programs p
                       WHERE lower(trim(p.email))=lower(trim(e.address))
                     )
                   ORDER BY RANDOM() LIMIT 1""",
                (row["email"] or "",),
            ).fetchone()
            if not replacement:
                raise ValueError("暂无可用且未绑定的新邮箱，当前邮箱未作修改")
            old_email = str(row["email"] or "").strip()
            new_email = replacement["address"]
            stamp = now()
            if old_email:
                existing_email = conn.execute(
                    "SELECT id FROM emails WHERE lower(trim(address))=lower(trim(?))",
                    (old_email,),
                ).fetchone()
                if existing_email:
                    conn.execute(
                        """UPDATE emails SET usable=0,invalid_at=?,invalid_by=?,updated_at=?
                           WHERE id=?""",
                        (stamp, actor, stamp, existing_email["id"]),
                    )
                else:
                    next_order = conn.execute(
                        "SELECT COALESCE(MAX(sort_order),-1)+1 n FROM emails"
                    ).fetchone()["n"]
                    conn.execute(
                        """INSERT INTO emails(
                           address,payload,sort_order,usable,invalid_at,invalid_by,created_at,updated_at
                           ) VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            old_email, json.dumps({"email": old_email}, ensure_ascii=False),
                            next_order, 0, stamp, actor, stamp, stamp,
                        ),
                    )
            conn.execute(
                "UPDATE programs SET email=?,updated_at=? WHERE id=?",
                (new_email, stamp, program_id),
            )
            self.db.audit(
                "refresh_email", "program", program_id,
                {"oldEmail": old_email, "newEmail": new_email}, actor, conn,
            )
        return {
            "program": self.get(program_id),
            "oldEmail": old_email,
            "newEmail": new_email,
        }

    def delete(self, program_id, actor="api"):
        actor = self._actor(actor)
        with self.db.transaction() as conn:
            row = self._assert_program(conn, program_id)
            if not row:
                return False
            conn.execute("DELETE FROM programs WHERE id=?", (program_id,))
            self.db.audit("delete", "program", program_id, {"before": external(row)}, actor, conn)
        return True

    def bulk(self, items, actor="api"):
        results = []
        for item in items:
            existing = self.get(str(item.get("id") or "")) if item.get("id") else None
            results.append(self.update(item["id"], item, actor) if existing else self.create(item, actor))
        return results

    @staticmethod
    def _apply_business_rules(data, current=None):
        current = current or {}
        status = data.get("status", current.get("status", ""))
        if status == "备案中" and "submitDate" not in data and not current.get("submitDate"):
            data["submitDate"] = now()[:10]
        elif status in ("备案完成", "已结算"):
            data["submitDate"] = ""
        effective = dict(current)
        effective.update(data)
        required = ("miniProgramName", "appid", "admin", "email")
        if effective.get("taskReason") and all(str(effective.get(key) or "").strip() for key in required):
            data["taskReason"] = ""

    @staticmethod
    def _company_id(conn, name):
        if not name:
            return None
        stamp = now()
        conn.execute(
            """INSERT INTO companies(name,full_name,created_at,updated_at) VALUES(?,?,?,?)
               ON CONFLICT(name) DO NOTHING""", (name, "", stamp, stamp)
        )
        return conn.execute("SELECT id FROM companies WHERE name=?", (name,)).fetchone()["id"]

    @staticmethod
    def _ensure_email(conn, address):
        address = str(address or "").strip()
        if not address:
            return
        if conn.execute(
            "SELECT 1 FROM emails WHERE lower(trim(address))=lower(trim(?))", (address,)
        ).fetchone():
            return
        sort_order = conn.execute("SELECT COALESCE(MAX(sort_order),-1)+1 n FROM emails").fetchone()["n"]
        stamp = now()
        conn.execute(
            """INSERT INTO emails(address,payload,sort_order,created_at,updated_at)
               VALUES(?,?,?,?,?)""",
            (address, json.dumps({"email": address}, ensure_ascii=False), sort_order, stamp, stamp),
        )

    @staticmethod
    def _assert_email_available(conn, address, program_id):
        address = str(address or "").strip()
        if not address:
            return
        email = conn.execute(
            "SELECT usable FROM emails WHERE lower(trim(address))=lower(trim(?))",
            (address,),
        ).fetchone()
        if email and not email["usable"]:
            raise ValueError("该邮箱已标记为不能使用")
        used = conn.execute(
            """SELECT id FROM programs
               WHERE lower(trim(email))=lower(trim(?)) AND id<>? LIMIT 1""",
            (address, str(program_id)),
        ).fetchone()
        if used:
            raise ValueError("该邮箱已绑定其他小程序")
