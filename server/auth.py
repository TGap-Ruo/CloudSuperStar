# -*- coding: utf-8 -*-
"""服务鉴权：用户、角色、额度（卡密）、会话、审计日志。

设计要点
--------
* **用户**：管理员在后台创建，分为 ``admin`` / ``user`` 两种角色。普通用户
  登录后才能使用刷课控制台，并受「剩余次数 + 每日任务上限 + 并发上限」约束。
* **卡密**：管理员批量生成，支持 无限次数 / 有限次数 / 单次 三种类型。
  用户在前台输入卡密兑换成自己的刷课次数（``credits``）；管理员也可以直接给用户充值。
* **额度扣减**：每次真正启动一个刷课任务扣 1 次（可配），任务失败自动退还。
  每一笔扣减都写进 ``credit_usages``，既能算每日任务数，也能做审计。
* **审计**：登录、创建用户、生成卡密、扣费、退款等敏感操作全部记录。

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

CODE_UNLIMITED = "unlimited"
CODE_LIMITED = "limited"
CODE_SINGLE = "single"

CODE_TYPE_LABELS = {
    CODE_UNLIMITED: "无限次数",
    CODE_LIMITED: "有限次数",
    CODE_SINGLE: "单次授权",
}

UNLIMITED_CREDITS = -1
CODE_LENGTH = 10
# 去掉 0/O、1/I/L 等易混淆字符，方便人工抄写
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
PBKDF2_ROUNDS = 200_000

AUTH_SCHEMA = (
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
    " type TEXT NOT NULL DEFAULT 'single',"
    " name TEXT DEFAULT '',"
    " note TEXT DEFAULT '',"
    " max_uses INTEGER NOT NULL DEFAULT 1,"
    " used_count INTEGER NOT NULL DEFAULT 0,"
    " credits INTEGER NOT NULL DEFAULT 1,"
    " bound_user TEXT DEFAULT '',"
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
    " task_id TEXT DEFAULT '',"
    " cost INTEGER NOT NULL DEFAULT 1,"
    " kind TEXT DEFAULT 'task',"
    " refunded INTEGER NOT NULL DEFAULT 0,"
    " detail TEXT DEFAULT ''"
    ");"
    "CREATE INDEX IF NOT EXISTS idx_credit_user ON credit_usages (user, at DESC);"
    "CREATE INDEX IF NOT EXISTS idx_credit_task ON credit_usages (task_id);"
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
    "CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_logs (at DESC);"
)


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
        self.store.executescript(AUTH_SCHEMA)
        self._lock = threading.RLock()

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

    def consume(self, username: str, task_id: str, cost: int = 1, detail: str = "") -> dict[str, Any]:
        """扣减一次额度（原子），返回最新用户信息。"""
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
                "INSERT INTO credit_usages (at, user, task_id, cost, kind, detail)"
                " VALUES (?, ?, ?, ?, 'task', ?)",
                (now_str(), username, task_id, cost, detail),
            )
        return self.get_user(username) or {}

    def refund(self, task_id: str) -> bool:
        """任务失败时退还额度（按 task_id 找回那笔扣减）。"""
        rows = self.store.query(
            "SELECT id, user, cost FROM credit_usages WHERE task_id = ? AND refunded = 0",
            (task_id,),
        )
        if not rows:
            return False
        with self._lock:
            for row in rows:
                self.store.execute("UPDATE credit_usages SET refunded = 1 WHERE id = ?", (row["id"],))
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

    # ────────────────────────────── 卡密
    def create_codes(
        self,
        *,
        code_type: str = CODE_SINGLE,
        count: int = 1,
        credits: int = 1,
        name: str = "",
        note: str = "",
        max_uses: int = 1,
        bound_user: str = "",
        expires_at: str = "",
    ) -> list[str]:
        if code_type not in CODE_TYPE_LABELS:
            raise AuthError("卡密类型只能是 unlimited / limited / single")
        count = max(1, min(200, int(count)))
        if code_type == CODE_SINGLE:
            max_uses = 1
        created: list[str] = []
        with self._lock:
            for _ in range(count):
                code = generate_code()
                while self._code_row(code):
                    code = generate_code()
                self.store.execute(
                    "INSERT INTO auth_codes (code, type, name, note, max_uses, credits,"
                    " bound_user, enabled, created_at, updated_at, expires_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                    (
                        code,
                        code_type,
                        name,
                        note,
                        max(1, int(max_uses)),
                        max(1, int(credits)),
                        bound_user,
                        now_str(),
                        now_str(),
                        expires_at,
                    ),
                )
                created.append(code)
        return created

    def _code_row(self, code: str) -> Optional[dict[str, Any]]:
        rows = self.store.query("SELECT * FROM auth_codes WHERE code = ?", ((code or "").strip().upper(),))
        return rows[0] if rows else None

    @staticmethod
    def _code_public(row: dict[str, Any]) -> dict[str, Any]:
        unlimited = row.get("type") == CODE_UNLIMITED
        remaining = None if unlimited else max(0, int(row.get("max_uses", 1)) - int(row.get("used_count", 0)))
        expired = False
        if row.get("expires_at"):
            try:
                expired = datetime.now() > datetime.strptime(str(row["expires_at"]), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                try:
                    expired = datetime.now() > datetime.strptime(str(row["expires_at"]), "%Y-%m-%d").replace(
                        hour=23, minute=59, second=59
                    )
                except ValueError:
                    expired = False
        return {
            "code": row.get("code"),
            "type": row.get("type"),
            "type_label": CODE_TYPE_LABELS.get(str(row.get("type")), str(row.get("type"))),
            "name": row.get("name", ""),
            "note": row.get("note", ""),
            "credits": int(row.get("credits", 1)),
            "max_uses": int(row.get("max_uses", 1)),
            "used_count": int(row.get("used_count", 0)),
            "remaining": remaining,
            "remaining_label": "无限" if unlimited else f"{remaining} 次",
            "bound_user": row.get("bound_user", ""),
            "enabled": bool(row.get("enabled")),
            "expired": expired,
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

    def verify_code(self, code: str) -> tuple[bool, str, dict[str, Any] | None]:
        row = self._code_row(code)
        if not row:
            return False, "卡密不存在", None
        info = self._code_public(row)
        if not info["enabled"]:
            return False, "该卡密已被禁用", info
        if info["expired"]:
            return False, "该卡密已过期", info
        if info["remaining"] is not None and info["remaining"] <= 0:
            return False, "该卡密使用次数已用尽", info
        return True, "卡密有效", info

    def redeem_code(self, code: str, username: str) -> dict[str, Any]:
        """用户用卡密兑换刷课次数：给用户加 credits，并消耗卡密一次。"""
        with self._lock:
            ok, message, info = self.verify_code(code)
            if not ok or not info:
                raise AuthError(message)
            if info.get("bound_user") and info["bound_user"] != username:
                raise AuthError("该卡密已绑定其它用户")
            user = self._user_row(username)
            if not user:
                raise AuthError("用户不存在")

            credits = int(info.get("credits", 1))
            if int(user.get("credits", 0)) >= 0:
                self.store.execute(
                    "UPDATE users SET credits = credits + ?, updated_at = ? WHERE username = ?",
                    (credits, now_str(), username),
                )
            self.store.execute(
                "UPDATE auth_codes SET used_count = used_count + 1, last_used_at = ?,"
                " updated_at = ? WHERE code = ?",
                (now_str(), now_str(), info["code"]),
            )
            self.store.execute(
                "INSERT INTO credit_usages (at, user, task_id, cost, kind, detail)"
                " VALUES (?, ?, '', ?, 'redeem', ?)",
                (now_str(), username, -credits, f"卡密 {info['code']} 兑换 {credits} 次"),
            )
        return {"code": info["code"], "credits": credits, "user": self.get_user(username) or {}}

    def toggle_code(self, code: str, enabled: bool) -> None:
        if not self._code_row(code):
            raise AuthError("卡密不存在")
        self.store.execute(
            "UPDATE auth_codes SET enabled = ?, updated_at = ? WHERE code = ?",
            (1 if enabled else 0, now_str(), (code or "").strip().upper()),
        )

    def reset_code(self, code: str) -> None:
        if not self._code_row(code):
            raise AuthError("卡密不存在")
        self.store.execute(
            "UPDATE auth_codes SET used_count = 0, updated_at = ? WHERE code = ?",
            (now_str(), (code or "").strip().upper()),
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
            " COALESCE(SUM(used_count), 0) AS used"
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
