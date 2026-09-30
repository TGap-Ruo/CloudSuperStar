# -*- coding: utf-8 -*-
"""服务鉴权：授权码（主）、用户（可选）、会话、审计日志。

设计要点
--------
* **授权码是主要方式**：管理员批量生成，每张授权码就是一个"次数包"——
  ``uses`` 是总次数，跑一次刷课程序消耗 1 次（一次里刷几门课、几个章节都算 1 次）。
  授权码是独立实体，**不能充值到用户账号上**。
* **用户可选**：也可以给用户账号配额度（``credits``）。前台允许不登录直接跑，
  只要求"填了授权码"或者"已登录且用户还有额度"。
* **优先级**：授权码与用户额度同时存在时，**优先扣授权码**。
* **失败不扣次数**：只有任务正常跑起来才扣；任务失败（如学习通账号密码错误）自动退还。
* **审计**：登录、建用户、生成授权码、扣次、退次等操作全部记录。

用户表里存的是 PBKDF2-SHA256 加盐哈希，不存明文密码。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import random
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from server.store import Store, utc_now

logger = logging.getLogger("chaoxing.auth")

ROLE_ADMIN = "admin"
ROLE_USER = "user"

UNLIMITED_CREDITS = -1
CODE_LENGTH = 10
# 去掉 0/O、1/I/L 等易混淆字符，方便人工抄写
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
PBKDF2_ROUNDS = 200_000

AUTH_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS users ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " username TEXT UNIQUE NOT NULL,"
    " password_hash TEXT NOT NULL,"
    " salt TEXT NOT NULL,"
    " role TEXT NOT NULL DEFAULT 'user',"
    " enabled INTEGER NOT NULL DEFAULT 1,"
    " credits INTEGER NOT NULL DEFAULT 0,"
    " daily_task_limit INTEGER NOT NULL DEFAULT 0,"
    " max_parallel INTEGER NOT NULL DEFAULT 0,"
    " note TEXT DEFAULT '',"
    " created_at TEXT NOT NULL,"
    " updated_at TEXT DEFAULT '',"
    " last_login_at TEXT DEFAULT ''"
    ");"
    "CREATE TABLE IF NOT EXISTS auth_codes ("
    " code TEXT PRIMARY KEY,"
    " name TEXT DEFAULT '',"
    " note TEXT DEFAULT '',"
    " uses INTEGER NOT NULL DEFAULT 1,"
    " used INTEGER NOT NULL DEFAULT 0,"
    " enabled INTEGER NOT NULL DEFAULT 1,"
    " created_at TEXT NOT NULL,"
    " updated_at TEXT DEFAULT '',"
    " expires_at TEXT DEFAULT '',"
    " last_used_at TEXT DEFAULT ''"
    ");"
    "CREATE TABLE IF NOT EXISTS credit_usages ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " at TEXT NOT NULL,"
    " user TEXT DEFAULT '',"
    " code TEXT DEFAULT '',"
    " task_id TEXT DEFAULT '',"
    " cost INTEGER NOT NULL DEFAULT 1,"
    " kind TEXT DEFAULT 'task',"
    " refunded INTEGER NOT NULL DEFAULT 0,"
    " detail TEXT DEFAULT ''"
    ");"
    "CREATE TABLE IF NOT EXISTS api_keys ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " key TEXT UNIQUE NOT NULL,"
    " user TEXT NOT NULL,"
    " name TEXT DEFAULT '',"
    " enabled INTEGER NOT NULL DEFAULT 1,"
    " created_at TEXT NOT NULL,"
    " last_used_at TEXT DEFAULT ''"
    ");"
    "CREATE TABLE IF NOT EXISTS audit_logs ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " at TEXT NOT NULL,"
    " actor TEXT DEFAULT '',"
    " action TEXT DEFAULT '',"
    " target TEXT DEFAULT '',"
    " detail TEXT DEFAULT '',"
    " ip TEXT DEFAULT ''"
    ");"
)

# 索引单独建：老库的表可能缺少新列，必须等 _migrate() 补完列再建索引
AUTH_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_credit_user ON credit_usages (user, at DESC);"
    "CREATE INDEX IF NOT EXISTS idx_credit_task ON credit_usages (task_id);"
    "CREATE INDEX IF NOT EXISTS idx_credit_code ON credit_usages (code);"
    "CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_logs (at DESC);"
)

AUTH_SCHEMA = AUTH_TABLE_SQL


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return digest.hex(), salt.hex()


def verify_password(password: str, password_hash: str, salt_hex: str) -> bool:
    try:
        expected, _ = hash_password(password, salt_hex)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, password_hash or "")


def generate_code() -> str:
    rng = random.SystemRandom()
    return "".join(rng.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


class AuthError(Exception):
    """鉴权/额度相关错误（Web 层转成 4xx）。"""


class AuthManager:
    def __init__(self, db_path: str | Path):
        self.store = Store(db_path)
        self.store.executescript(AUTH_TABLE_SQL)
        self._migrate()                     # 先补列
        self.store.executescript(AUTH_INDEX_SQL)  # 后建索引
        self._lock = threading.RLock()

    def _columns(self, table: str) -> set[str]:
        rows = self.store.query(f"PRAGMA table_info({table})")
        return {str(row.get("name")) for row in rows}

    def _migrate(self) -> None:
        """兼容旧版本数据库：把老的卡密表（type/max_uses/credits）迁到新结构。"""
        columns = self._columns("auth_codes")
        if "uses" not in columns:
            self.store.execute("ALTER TABLE auth_codes ADD COLUMN uses INTEGER NOT NULL DEFAULT 1")
            if "max_uses" in columns:
                if "type" in columns:
                    self.store.execute(
                        "UPDATE auth_codes SET uses = CASE WHEN type = 'unlimited' THEN -1"
                        " ELSE max_uses END"
                    )
                else:
                    self.store.execute("UPDATE auth_codes SET uses = max_uses")
        if "used" not in columns:
            self.store.execute("ALTER TABLE auth_codes ADD COLUMN used INTEGER NOT NULL DEFAULT 0")
            if "used_count" in columns:
                self.store.execute("UPDATE auth_codes SET used = used_count")
        if "user" not in self._columns("api_keys"):
            pass  # api_keys 结构未变
        if "code" not in self._columns("credit_usages"):
            self.store.execute("ALTER TABLE credit_usages ADD COLUMN code TEXT DEFAULT ''")

    # ────────────────────────────── 用户
    def _user_row(self, username: str) -> Optional[dict[str, Any]]:
        rows = self.store.query("SELECT * FROM users WHERE username = ?", (username,))
        return rows[0] if rows else None

    def get_user(self, username: str) -> Optional[dict[str, Any]]:
        row = self._user_row((username or "").strip())
        return self._public_user(row) if row else None

    @staticmethod
    def _public_user(row: dict[str, Any]) -> dict[str, Any]:
        data = {key: row[key] for key in row if key not in {"password_hash", "salt"}}
        data["enabled"] = bool(row.get("enabled"))
        data["unlimited"] = int(row.get("credits", 0)) < 0
        data["credits_label"] = "不限" if data["unlimited"] else f"{row.get('credits', 0)} 次"
        return data

    def count_users(self) -> int:
        rows = self.store.query("SELECT COUNT(*) AS n FROM users")
        return int(rows[0]["n"]) if rows else 0

    def ensure_bootstrap_admin(self, username: str = "admin", password: str = "") -> tuple[bool, str]:
        """没有管理员时创建初始管理员；密码留空则随机生成并返回（只返回一次）。"""
        if self.count_users() > 0:
            return False, ""
        password = password or secrets.token_urlsafe(12)
        self.create_user(username, password, role=ROLE_ADMIN, credits=UNLIMITED_CREDITS)
        logger.warning("已创建初始管理员 %s，初始密码：%s（请登录后立即修改）", username, password)
        return True, password

    def create_user(
        self,
        username: str,
        password: str,
        *,
        role: str = ROLE_USER,
        credits: int = 0,
        daily_task_limit: int = 0,
        max_parallel: int = 0,
        note: str = "",
    ) -> dict[str, Any]:
        username = (username or "").strip()
        if not username or len(username) > 32:
            raise AuthError("用户名不能为空且不超过 32 个字符")
        if not password or len(password) < 6:
            raise AuthError("密码至少 6 位")
        if role not in {ROLE_ADMIN, ROLE_USER}:
            raise AuthError("角色只能是 admin 或 user")
        if self._user_row(username):
            raise AuthError(f"用户 {username} 已存在")

        password_hash, salt = hash_password(password)
        with self._lock:
            self.store.execute(
                "INSERT INTO users (username, password_hash, salt, role, enabled, credits,"
                " daily_task_limit, max_parallel, note, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
                (
                    username,
                    password_hash,
                    salt,
                    role,
                    int(credits),
                    int(daily_task_limit),
                    int(max_parallel),
                    note,
                    now_str(),
                    now_str(),
                ),
            )
        return self.get_user(username) or {}

    def list_users(self, keyword: str = "") -> list[dict[str, Any]]:
        if keyword:
            rows = self.store.query(
                "SELECT * FROM users WHERE username LIKE ? OR note LIKE ? ORDER BY id",
                (f"%{keyword}%", f"%{keyword}%"),
            )
        else:
            rows = self.store.query("SELECT * FROM users ORDER BY id")
        return [self._public_user(row) for row in rows]

    def update_user(self, username: str, **fields: Any) -> dict[str, Any]:
        allowed = {"role", "enabled", "credits", "daily_task_limit", "max_parallel", "note"}
        updates = {key: value for key, value in fields.items() if key in allowed and value is not None}
        if not updates:
            return self.get_user(username) or {}
        if "role" in updates and updates["role"] not in {ROLE_ADMIN, ROLE_USER}:
            raise AuthError("角色只能是 admin 或 user")
        for key in ("credits", "daily_task_limit", "max_parallel"):
            if key in updates:
                updates[key] = int(updates[key])
        if "enabled" in updates:
            updates["enabled"] = 1 if updates["enabled"] else 0
        assignments = ", ".join(f"{key} = ?" for key in updates)
        params = list(updates.values()) + [now_str(), username]
        with self._lock:
            self.store.execute(
                f"UPDATE users SET {assignments}, updated_at = ? WHERE username = ?", tuple(params)
            )
        return self.get_user(username) or {}

    def set_password(self, username: str, password: str) -> None:
        if not password or len(password) < 6:
            raise AuthError("密码至少 6 位")
        password_hash, salt = hash_password(password)
        with self._lock:
            self.store.execute(
                "UPDATE users SET password_hash = ?, salt = ?, updated_at = ? WHERE username = ?",
                (password_hash, salt, now_str(), username),
            )

    def delete_user(self, username: str) -> None:
        user = self._user_row(username)
        if not user:
            raise AuthError("用户不存在")
        if user.get("role") == ROLE_ADMIN:
            admins = self.store.query("SELECT COUNT(*) AS n FROM users WHERE role = ?", (ROLE_ADMIN,))
            if admins and int(admins[0]["n"]) <= 1:
                raise AuthError("不能删除最后一个管理员")
        with self._lock:
            self.store.execute("DELETE FROM users WHERE username = ?", (username,))

    def verify_login(self, username: str, password: str) -> Optional[dict[str, Any]]:
        row = self._user_row((username or "").strip())
        if not row or not row.get("enabled"):
            return None
        if not verify_password(password, row.get("password_hash", ""), row.get("salt", "")):
            return None
        with self._lock:
            self.store.execute(
                "UPDATE users SET last_login_at = ? WHERE username = ?", (now_str(), row["username"])
            )
        return self._public_user(row)

    # ────────────────────────────── 额度（刷课次数）
    def quota_status(self, username: str) -> dict[str, Any]:
        """查看用户额度是否够用（不扣减）。"""
        user = self._user_row(username)
        if not user:
            return {"ok": False, "reason": "用户不存在"}
        if not user.get("enabled"):
            return {"ok": False, "reason": "账号已被禁用"}
        credits = int(user.get("credits", 0))
        daily_limit = int(user.get("daily_task_limit", 0))
        used_today = self.used_today(username)
        if credits == 0 and user.get("role") != ROLE_ADMIN:
            return {"ok": False, "reason": "刷课次数已用完，请使用卡密兑换或联系管理员", "credits": 0}
        if daily_limit and used_today >= daily_limit:
            return {
                "ok": False,
                "reason": f"今日任务数已达上限（{daily_limit} 次）",
                "credits_label": "不限" if credits < 0 else credits,
            }
        return {
            "ok": True,
            "credits": credits,
            "credits_label": "不限" if credits < 0 else credits,
            "used_today": used_today,
            "daily_task_limit": daily_limit,
        }

    def used_today(self, username: str) -> int:
        # 只统计"启动刷课任务"的扣减；兑换卡密的记录（kind=redeem，cost 为负）不算任务数
        rows = self.store.query(
            "SELECT COALESCE(SUM(cost), 0) AS n FROM credit_usages"
            " WHERE user = ? AND kind = 'task' AND refunded = 0 AND cost > 0"
            " AND substr(at, 1, 10) = ?",
            (username, today_str()),
        )
        return int(rows[0]["n"]) if rows else 0

    def consume_user_credit(self, username: str, task_id: str, cost: int = 1,
                            detail: str = "") -> dict[str, Any]:
        """扣减用户的额度（原子），返回最新用户信息。"""
        with self._lock:
            status = self.quota_status(username)
            if not status.get("ok"):
                raise AuthError(status.get("reason", "额度不足"))
            user = self._user_row(username)
            credits = int(user.get("credits", 0))
            if credits > 0:
                self.store.execute(
                    "UPDATE users SET credits = ?, updated_at = ? WHERE username = ?",
                    (credits - cost, now_str(), username),
                )
            self.store.execute(
                "INSERT INTO credit_usages (at, user, code, task_id, cost, kind, detail)"
                " VALUES (?, ?, '', ?, ?, 'task', ?)",
                (now_str(), username, task_id, cost, detail),
            )
        return self.get_user(username) or {}

    def consume_code(self, code: str, task_id: str, user: str = "") -> dict[str, Any]:
        """核销授权码 1 次（原子）。"""
        code = (code or "").strip().upper()
        with self._lock:
            ok, message, info = self.verify_code(code)
            if not ok or not info:
                raise AuthError(message)
            self.store.execute(
                "UPDATE auth_codes SET used = used + 1, last_used_at = ?, updated_at = ?"
                " WHERE code = ?",
                (now_str(), now_str(), code),
            )
            self.store.execute(
                "INSERT INTO credit_usages (at, user, code, task_id, cost, kind, detail)"
                " VALUES (?, ?, ?, ?, 1, 'code', ?)",
                (now_str(), user, code, task_id, "授权码核销"),
            )
            after = self.get_code(code) or {}
        return {"code": code, "remaining": after.get("remaining"),
                "remaining_label": after.get("remaining_label"), "info": after}

    def consume(self, *, task_id: str, code: str = "", user: str = "",
                detail: str = "") -> dict[str, Any]:
        """统一扣次入口：**优先扣授权码**，没有授权码才扣登录用户的额度。

        返回 {"source": "code"|"user", "label": 剩余次数文案, "remaining": ...}
        """
        if code:
            result = self.consume_code(code, task_id, user=user)
            return {
                "source": "code",
                "code": result["code"],
                "remaining": result["remaining"],
                "remaining_label": result["remaining_label"],
            }
        if user:
            self.consume_user_credit(user, task_id, detail=detail)
            info = self.get_user(user) or {}
            return {
                "source": "user",
                "user": user,
                "remaining": info.get("credits"),
                "remaining_label": info.get("credits_label", "0 次"),
            }
        raise AuthError("请填写授权码，或登录后使用账号额度")

    def refund(self, task_id: str) -> bool:
        """任务失败时退次（授权码退给授权码，用户额度退给用户）。"""
        rows = self.store.query(
            "SELECT id, user, code, cost, kind FROM credit_usages"
            " WHERE task_id = ? AND refunded = 0",
            (task_id,),
        )
        if not rows:
            return False
        with self._lock:
            for row in rows:
                self.store.execute("UPDATE credit_usages SET refunded = 1 WHERE id = ?", (row["id"],))
                if row.get("kind") == "code" and row.get("code"):
                    self.store.execute(
                        "UPDATE auth_codes SET used = CASE WHEN used > 0 THEN used - 1 ELSE 0 END,"
                        " updated_at = ? WHERE code = ?",
                        (now_str(), row["code"]),
                    )
                elif row.get("user"):
                    user = self._user_row(row["user"])
                    if user and int(user.get("credits", 0)) >= 0:
                        self.store.execute(
                            "UPDATE users SET credits = credits + ?, updated_at = ? WHERE username = ?",
                            (int(row["cost"]), now_str(), row["user"]),
                        )
        return True

    def credit_usages(self, *, user: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if user:
            rows = self.store.query(
                "SELECT * FROM credit_usages WHERE user = ? ORDER BY id DESC LIMIT ?",
                (user, max(1, limit)),
            )
        else:
            rows = self.store.query(
                "SELECT * FROM credit_usages ORDER BY id DESC LIMIT ?", (max(1, limit),)
            )
        return rows

    def code_usages(self, code: str, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.query(
            "SELECT * FROM credit_usages WHERE code = ? ORDER BY id DESC LIMIT ?",
            ((code or "").strip().upper(), max(1, limit)),
        )

    # ────────────────────────────── 授权码
    def create_codes(
        self,
        *,
        count: int = 1,
        uses: int = 1,
        name: str = "",
        note: str = "",
        expires_at: str = "",
    ) -> list[str]:
        """批量生成授权码。uses = 这张授权码能跑几次（-1 表示不限）。"""
        count = max(1, min(500, int(count)))
        uses = int(uses)
        if uses == 0 or uses < -1:
            raise AuthError("次数必须是正整数，或填 -1 表示不限次数")
        created: list[str] = []
        with self._lock:
            for _ in range(count):
                code = generate_code()
                while self._code_row(code):
                    code = generate_code()
                self.store.execute(
                    "INSERT INTO auth_codes (code, name, note, uses, used, enabled,"
                    " created_at, updated_at, expires_at)"
                    " VALUES (?, ?, ?, ?, 0, 1, ?, ?, ?)",
                    (code, name, note, uses, now_str(), now_str(), expires_at),
                )
                created.append(code)
        return created

    def _code_row(self, code: str) -> Optional[dict[str, Any]]:
        rows = self.store.query("SELECT * FROM auth_codes WHERE code = ?", ((code or "").strip().upper(),))
        return rows[0] if rows else None

    @staticmethod
    def _is_past(value: Any) -> bool:
        if not value:
            return False
        text = str(value)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                moment = datetime.strptime(text, fmt)
                if fmt == "%Y-%m-%d":
                    moment = moment.replace(hour=23, minute=59, second=59)
                return datetime.now() > moment
            except ValueError:
                continue
        return False

    @classmethod
    def _code_public(cls, row: dict[str, Any]) -> dict[str, Any]:
        uses = int(row.get("uses", 1) or 0)
        used = int(row.get("used", 0) or 0)
        unlimited = uses < 0
        remaining = None if unlimited else max(0, uses - used)
        return {
            "code": row.get("code"),
            "name": row.get("name", ""),
            "note": row.get("note", ""),
            "uses": uses,
            "used": used,
            "remaining": remaining,
            "remaining_label": "不限次数" if unlimited else f"剩余 {remaining} 次",
            "unlimited": unlimited,
            "enabled": bool(row.get("enabled")),
            "expired": cls._is_past(row.get("expires_at")),
            "created_at": row.get("created_at", ""),
            "expires_at": row.get("expires_at", ""),
            "last_used_at": row.get("last_used_at", ""),
        }

    def list_codes(self, keyword: str = "", limit: int = 500) -> list[dict[str, Any]]:
        if keyword:
            kw = f"%{keyword.strip()}%"
            rows = self.store.query(
                "SELECT * FROM auth_codes WHERE code LIKE ? OR name LIKE ? OR note LIKE ?"
                " ORDER BY created_at DESC LIMIT ?",
                (kw, kw, kw, max(1, limit)),
            )
        else:
            rows = self.store.query(
                "SELECT * FROM auth_codes ORDER BY created_at DESC LIMIT ?", (max(1, limit),)
            )
        return [self._code_public(row) for row in rows]

    def get_code(self, code: str) -> Optional[dict[str, Any]]:
        row = self._code_row(code)
        return self._code_public(row) if row else None

    def verify_code(self, code: str) -> tuple[bool, str, dict[str, Any] | None]:
        code = (code or "").strip().upper()
        if not code:
            return False, "请输入授权码", None
        row = self._code_row(code)
        if not row:
            return False, "授权码不存在", None
        info = self._code_public(row)
        if not info["enabled"]:
            return False, "该授权码已被禁用", info
        if info["expired"]:
            return False, "该授权码已过期", info
        if info["remaining"] is not None and info["remaining"] <= 0:
            return False, "该授权码可用次数已用尽", info
        return True, "授权码有效", info

    def toggle_code(self, code: str, enabled: bool) -> None:
        if not self._code_row(code):
            raise AuthError("授权码不存在")
        self.store.execute(
            "UPDATE auth_codes SET enabled = ?, updated_at = ? WHERE code = ?",
            (1 if enabled else 0, now_str(), (code or "").strip().upper()),
        )

    def reset_code(self, code: str) -> None:
        if not self._code_row(code):
            raise AuthError("授权码不存在")
        self.store.execute(
            "UPDATE auth_codes SET used = 0, updated_at = ? WHERE code = ?",
            (now_str(), (code or "").strip().upper()),
        )

    def set_code_uses(self, code: str, uses: int) -> None:
        if not self._code_row(code):
            raise AuthError("授权码不存在")
        self.store.execute(
            "UPDATE auth_codes SET uses = ?, updated_at = ? WHERE code = ?",
            (int(uses), now_str(), (code or "").strip().upper()),
        )

    def delete_code(self, code: str) -> None:
        self.store.execute("DELETE FROM auth_codes WHERE code = ?", ((code or "").strip().upper(),))

    # ────────────────────────────── API Key
    def create_api_key(self, username: str, name: str = "") -> str:
        key = "cx_" + secrets.token_urlsafe(24)
        self.store.execute(
            "INSERT INTO api_keys (key, user, name, enabled, created_at) VALUES (?, ?, ?, 1, ?)",
            (key, username, name, now_str()),
        )
        return key

    def verify_api_key(self, key: str) -> Optional[dict[str, Any]]:
        rows = self.store.query(
            "SELECT * FROM api_keys WHERE key = ? AND enabled = 1", ((key or "").strip(),)
        )
        if not rows:
            return None
        row = rows[0]
        self.store.execute("UPDATE api_keys SET last_used_at = ? WHERE key = ?", (now_str(), row["key"]))
        return self.get_user(row["user"])

    def list_api_keys(self, username: str | None = None) -> list[dict[str, Any]]:
        if username:
            return self.store.query(
                "SELECT * FROM api_keys WHERE user = ? ORDER BY id DESC", (username,)
            )
        return self.store.query("SELECT * FROM api_keys ORDER BY id DESC")

    def delete_api_key(self, key: str) -> None:
        self.store.execute("DELETE FROM api_keys WHERE key = ?", (key,))

    # ────────────────────────────── 审计日志
    def log(self, action: str, *, actor: str = "", target: str = "", detail: str = "", ip: str = "") -> None:
        try:
            self.store.execute(
                "INSERT INTO audit_logs (at, actor, action, target, detail, ip)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (now_str(), actor, action, target, detail[:500], ip),
            )
        except Exception as exc:  # noqa: BLE001 - 审计失败不影响主流程
            logger.warning("写审计日志失败: %s", exc)

    def list_audit(self, limit: int = 200, keyword: str = "") -> list[dict[str, Any]]:
        if keyword:
            kw = f"%{keyword}%"
            return self.store.query(
                "SELECT * FROM audit_logs WHERE actor LIKE ? OR action LIKE ? OR target LIKE ?"
                " ORDER BY id DESC LIMIT ?",
                (kw, kw, kw, max(1, limit)),
            )
        return self.store.query("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (max(1, limit),))

    # ────────────────────────────── 概览
    def stats(self) -> dict[str, Any]:
        users = self.store.query(
            "SELECT COUNT(*) AS total,"
            " COALESCE(SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END), 0) AS enabled,"
            " COALESCE(SUM(CASE WHEN role = 'admin' THEN 1 ELSE 0 END), 0) AS admins"
            " FROM users"
        )
        codes = self.store.query(
            "SELECT COUNT(*) AS total,"
            " COALESCE(SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END), 0) AS enabled,"
            " COALESCE(SUM(used), 0) AS used"
            " FROM auth_codes"
        )
        consumed = self.store.query(
            "SELECT COALESCE(SUM(CASE WHEN refunded = 0 AND cost > 0 THEN cost ELSE 0 END), 0) AS used,"
            " COALESCE(SUM(CASE WHEN refunded = 1 THEN 1 ELSE 0 END), 0) AS refunded"
            " FROM credit_usages"
        )
        return {
            "users": users[0] if users else {},
            "codes": codes[0] if codes else {},
            "credits": consumed[0] if consumed else {},
        }

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "AuthManager":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
