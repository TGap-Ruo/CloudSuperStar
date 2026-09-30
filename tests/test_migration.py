# -*- coding: utf-8 -*-
"""升级兼容测试：老版本数据库必须能平滑升级，不能导致服务起不来。

真实事故（2026-09-30）：老库的 ai_usage 表没有 code 列，而建表脚本里带了
``CREATE INDEX ... ON ai_usage (code)``，索引在"补列迁移"之前执行 →
``sqlite3.OperationalError: no such column: code`` → chaoxing-web 崩溃重启 45 次。

这里用旧版本的建表语句原样构造数据库，验证新的初始化流程（建表 → 补列 → 建索引）
能正常升级，且数据不丢。
"""

import sqlite3

from server.auth import AuthManager
from server.usage import UsageContext, UsageRecorder

# ─────────────── 旧版本（v1.0.0 / d5e14b4 时代）的建表语句 ───────────────
OLD_SCHEMA = """
CREATE TABLE ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    user TEXT DEFAULT '',
    account TEXT DEFAULT '',
    task_id TEXT DEFAULT '',
    run_id TEXT DEFAULT '',
    provider TEXT DEFAULT '',
    model TEXT DEFAULT '',
    prompt_tokens INTEGER DEFAULT 0,
    cache_hit_tokens INTEGER DEFAULT 0,
    cache_miss_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    cost REAL DEFAULT 0,
    currency TEXT DEFAULT 'USD',
    detail TEXT DEFAULT ''
);
CREATE INDEX idx_usage_account ON ai_usage (account, at DESC);
CREATE INDEX idx_usage_user ON ai_usage (user, at DESC);

CREATE TABLE auth_codes (
    code TEXT PRIMARY KEY,
    type TEXT NOT NULL DEFAULT 'single',
    name TEXT DEFAULT '',
    note TEXT DEFAULT '',
    max_uses INTEGER NOT NULL DEFAULT 1,
    used_count INTEGER NOT NULL DEFAULT 0,
    credits INTEGER NOT NULL DEFAULT 1,
    bound_user TEXT DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT DEFAULT '',
    expires_at TEXT DEFAULT '',
    last_used_at TEXT DEFAULT ''
);

CREATE TABLE credit_usages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    user TEXT DEFAULT '',
    task_id TEXT DEFAULT '',
    cost INTEGER NOT NULL DEFAULT 1,
    kind TEXT DEFAULT 'task',
    refunded INTEGER NOT NULL DEFAULT 0,
    detail TEXT DEFAULT ''
);

CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    enabled INTEGER NOT NULL DEFAULT 1,
    credits INTEGER NOT NULL DEFAULT 0,
    daily_task_limit INTEGER NOT NULL DEFAULT 0,
    max_parallel INTEGER NOT NULL DEFAULT 0,
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT DEFAULT '',
    last_login_at TEXT DEFAULT ''
);

CREATE TABLE audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT DEFAULT '',
    action TEXT DEFAULT '',
    target TEXT DEFAULT '',
    detail TEXT DEFAULT '',
    ip TEXT DEFAULT ''
);
"""


def build_legacy_db(path):
    """构造一个老版本数据库，并塞入一些历史数据。"""
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO ai_usage (at, user, account, task_id, model, prompt_tokens,"
        " output_tokens, total_tokens, cost, currency) VALUES"
        " ('2026-09-01T00:00:00+00:00', 'u1', 'web-a', 'task-old', 'deepseek-chat',"
        "  1000, 500, 1500, 0.01, 'USD')"
    )
    conn.execute(
        "INSERT INTO auth_codes (code, type, name, max_uses, used_count, credits, enabled,"
        " created_at) VALUES ('LEGACY0001', 'limited', '老卡密', 10, 3, 5, 1, '2026-09-01 00:00:00')"
    )
    conn.execute(
        "INSERT INTO credit_usages (at, user, task_id, cost, kind, refunded)"
        " VALUES ('2026-09-01 00:00:00', 'u1', 'task-old', 1, 'task', 0)"
    )
    conn.commit()
    conn.close()


def test_usage_recorder_upgrades_legacy_db(tmp_path):
    db = tmp_path / "state.db"
    build_legacy_db(db)

    recorder = UsageRecorder(db)          # 旧版本这里会抛 no such column: code
    try:
        columns = {row["name"] for row in recorder.store.query("PRAGMA table_info(ai_usage)")}
        assert "code" in columns
        # 历史数据保留
        assert recorder.totals()["calls"] == 1
        assert recorder.for_task("task-old")["total_tokens"] == 1500
        # 新列可用
        recorder.record(
            model="deepseek-flash",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            context=UsageContext(user="u2", code="NEWCODE001", account="web-b", task_id="task-new"),
        )
        assert recorder.for_code("NEWCODE001")["total_tokens"] == 15
        assert len(recorder.group_by("code")) == 2
    finally:
        recorder.close()


def test_auth_manager_upgrades_legacy_db(tmp_path):
    db = tmp_path / "state.db"
    build_legacy_db(db)

    auth = AuthManager(db)
    try:
        columns = {row["name"] for row in auth.store.query("PRAGMA table_info(auth_codes)")}
        assert {"uses", "used"} <= columns
        assert "code" in {row["name"] for row in auth.store.query("PRAGMA table_info(credit_usages)")}

        info = auth.get_code("LEGACY0001")
        assert info["uses"] == 10 and info["used"] == 3
        assert info["remaining"] == 7

        # 迁移后能正常扣次与退款
        auth.consume(task_id="t-new", code="LEGACY0001")
        assert auth.get_code("LEGACY0001")["remaining"] == 6
        assert auth.refund("t-new") is True
        assert auth.get_code("LEGACY0001")["remaining"] == 7

        # 能继续生成新授权码
        created = auth.create_codes(count=1, uses=2)
        assert auth.get_code(created[0])["remaining"] == 2
    finally:
        auth.close()


def test_upgrade_is_idempotent(tmp_path):
    """重复初始化（每次服务启动都会跑）不能报错、不能重复加列。"""
    db = tmp_path / "state.db"
    build_legacy_db(db)
    for _ in range(3):
        usage = UsageRecorder(db)
        auth = AuthManager(db)
        usage.close()
        auth.close()
    usage = UsageRecorder(db)
    try:
        assert usage.totals()["calls"] == 1
    finally:
        usage.close()


def test_fresh_db_has_indexes(tmp_path):
    """全新数据库要能建出所有索引（含 code 相关）。"""
    db = tmp_path / "state.db"
    usage = UsageRecorder(db)
    auth = AuthManager(db)
    try:
        usage_indexes = {row["name"] for row in usage.store.query(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='ai_usage'")}
        assert "idx_usage_code" in usage_indexes
        auth_indexes = {row["name"] for row in auth.store.query(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='credit_usages'")}
        assert "idx_credit_code" in auth_indexes
    finally:
        usage.close()
        auth.close()
