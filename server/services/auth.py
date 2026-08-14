#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管理员账号、密码哈希与公司授权。兼容服务器 Python 3.6。"""
import base64
import hashlib
import hmac
import os

from db import now


HASH_NAME = "sha256"
ITERATIONS = 200000


def _b64(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password, iterations=ITERATIONS):
    password = str(password or "")
    if len(password) < 6:
        raise ValueError("密码至少需要 6 位")
    salt = os.urandom(18)
    digest = hashlib.pbkdf2_hmac(HASH_NAME, password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256$%d$%s$%s" % (iterations, _b64(salt), _b64(digest))


def verify_password(password, encoded):
    try:
        algorithm, iterations, salt, expected = str(encoded).split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            HASH_NAME, str(password or "").encode("utf-8"), _decode(salt), int(iterations)
        )
        return hmac.compare_digest(_b64(actual), expected)
    except (TypeError, ValueError):
        return False


def public_user(row):
    if not row:
        return None
    value = dict(row)
    return {
        "id": value["id"],
        "username": value["username"],
        "role": value["role"],
        "active": bool(value["active"]),
        "sessionVersion": value["session_version"],
        "lastLogin": value.get("last_login") or "",
        "createdAt": value.get("created_at") or "",
    }


class AuthService:
    def __init__(self, db):
        self.db = db

    def ensure_superadmin(self, password):
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM admin_users WHERE role='super' ORDER BY id LIMIT 1"
            ).fetchone()
            if row:
                return public_user(row)
            if not password:
                raise ValueError("首次启动需要配置 APP_PASSWORD")
            stamp = now()
            cur = conn.execute(
                """INSERT INTO admin_users(username,password_hash,role,active,session_version,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                ("admin", hash_password(password), "super", 1, 1, stamp, stamp),
            )
            self.db.audit("create", "admin_user", str(cur.lastrowid), {"username": "admin"}, "startup", conn)
            return public_user(conn.execute("SELECT * FROM admin_users WHERE id=?", (cur.lastrowid,)).fetchone())

    def authenticate(self, username, password):
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM admin_users WHERE username=? COLLATE NOCASE", (str(username or "").strip(),)
            ).fetchone()
            if not row or not row["active"] or not verify_password(password, row["password_hash"]):
                return None
            conn.execute("UPDATE admin_users SET last_login=?,updated_at=? WHERE id=?", (now(), now(), row["id"]))
            return public_user(conn.execute("SELECT * FROM admin_users WHERE id=?", (row["id"],)).fetchone())

    def get(self, user_id):
        with self.db.connect() as conn:
            return public_user(conn.execute("SELECT * FROM admin_users WHERE id=?", (user_id,)).fetchone())

    def list(self):
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT u.*,
                   (SELECT COUNT(*) FROM companies c WHERE c.assigned_admin_id=u.id) company_count
                   FROM admin_users u ORDER BY CASE role WHEN 'super' THEN 0 ELSE 1 END,username"""
            ).fetchall()
        result = []
        for row in rows:
            item = public_user(row)
            item["companyCount"] = row["company_count"]
            result.append(item)
        return result

    def create_subadmin(self, username, password):
        username = str(username or "").strip()
        if len(username) < 2 or len(username) > 40:
            raise ValueError("用户名长度必须为 2 到 40 位")
        stamp = now()
        try:
            with self.db.transaction() as conn:
                cur = conn.execute(
                    """INSERT INTO admin_users(username,password_hash,role,active,session_version,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (username, hash_password(password), "subadmin", 1, 1, stamp, stamp),
                )
                self.db.audit(
                    "create", "admin_user", str(cur.lastrowid), {"username": username}, "admin", conn
                )
                return public_user(
                    conn.execute("SELECT * FROM admin_users WHERE id=?", (cur.lastrowid,)).fetchone()
                )
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                raise ValueError("用户名已存在")
            raise

    def update_subadmin(self, user_id, active=None, password=None):
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM admin_users WHERE id=?", (user_id,)).fetchone()
            if not row or row["role"] != "subadmin":
                raise ValueError("分级管理员不存在")
            updates, args = [], []
            if active is not None:
                updates.append("active=?")
                args.append(int(bool(active)))
            if password is not None:
                updates.append("password_hash=?")
                args.append(hash_password(password))
            if updates:
                updates.extend(["session_version=session_version+1", "updated_at=?"])
                args.extend([now(), user_id])
                conn.execute("UPDATE admin_users SET %s WHERE id=?" % ",".join(updates), args)
            self.db.audit(
                "update", "admin_user", str(user_id),
                {"active": active, "passwordReset": password is not None}, "admin", conn
            )
            return public_user(conn.execute("SELECT * FROM admin_users WHERE id=?", (user_id,)).fetchone())

    def assign_company(self, company_id, user_id):
        with self.db.transaction() as conn:
            company = conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
            if not company:
                raise ValueError("公司不存在")
            if user_id is not None:
                user = conn.execute("SELECT * FROM admin_users WHERE id=?", (user_id,)).fetchone()
                if not user or user["role"] != "subadmin" or not user["active"]:
                    raise ValueError("只能分配给已启用的分级管理员")
            conn.execute(
                "UPDATE companies SET assigned_admin_id=?,updated_at=? WHERE id=?",
                (user_id, now(), company_id),
            )
            self.db.audit(
                "assign", "company", str(company_id), {"adminId": user_id}, "admin", conn
            )
        return True
