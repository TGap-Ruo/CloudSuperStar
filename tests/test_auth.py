# -*- coding: utf-8 -*-
"""鉴权模块测试：用户、额度、卡密、API Key、审计。"""

import pytest

from server.auth import (
    CODE_LIMITED,
    CODE_SINGLE,
    CODE_UNLIMITED,
    AuthError,
    AuthManager,
    UNLIMITED_CREDITS,
    hash_password,
    verify_password,
)


@pytest.fixture
def auth(tmp_path):
    manager = AuthManager(tmp_path / "state.db")
    yield manager
    manager.close()


def test_password_hash_is_salted_and_verifiable():
    digest1, salt1 = hash_password("pass123456")
    digest2, salt2 = hash_password("pass123456")
    assert digest1 != digest2          # 不同盐 → 不同哈希
    assert salt1 != salt2
    assert verify_password("pass123456", digest1, salt1)
    assert not verify_password("wrong", digest1, salt1)


def test_bootstrap_admin_only_once(auth):
    created, password = auth.ensure_bootstrap_admin("admin", "")
    assert created is True
    assert len(password) >= 10
    assert auth.verify_login("admin", password)

    created_again, _ = auth.ensure_bootstrap_admin("admin", "another")
    assert created_again is False
    assert auth.verify_login("admin", password)  # 密码没被覆盖


def test_user_crud(auth):
    auth.create_user("u1", "pass123456", credits=5, note="测试")
    assert auth.get_user("u1")["credits"] == 5

    with pytest.raises(AuthError, match="已存在"):
        auth.create_user("u1", "pass123456")
    with pytest.raises(AuthError, match="至少 6 位"):
        auth.create_user("u2", "123")

    auth.update_user("u1", credits=9, enabled=False)
    assert auth.get_user("u1")["credits"] == 9
    assert auth.get_user("u1")["enabled"] is False
    assert auth.verify_login("u1", "pass123456") is None  # 禁用后无法登录

    auth.delete_user("u1")
    assert auth.get_user("u1") is None


def test_cannot_delete_last_admin(auth):
    auth.ensure_bootstrap_admin("admin", "pass123456")
    with pytest.raises(AuthError, match="最后一个管理员"):
        auth.delete_user("admin")


def test_set_password(auth):
    auth.create_user("u1", "pass123456")
    auth.set_password("u1", "newpass123")
    assert auth.verify_login("u1", "pass123456") is None
    assert auth.verify_login("u1", "newpass123")


def test_quota_consume_and_refund(auth):
    auth.create_user("u1", "pass123456", credits=2)
    assert auth.quota_status("u1")["ok"] is True

    auth.consume(task_id="task-1", user="u1")
    auth.consume(task_id="task-2", user="u1")
    assert auth.get_user("u1")["credits"] == 0
    assert auth.used_today("u1") == 2

    with pytest.raises(AuthError, match="次数已用完"):
        auth.consume(task_id="task-3", user="u1")

    assert auth.refund("task-2") is True
    assert auth.get_user("u1")["credits"] == 1
    assert auth.used_today("u1") == 1        # 已退款的不计入今日任务数
    assert auth.refund("task-2") is False     # 不能重复退款


def test_daily_limit(auth):
    auth.create_user("u1", "pass123456", credits=10, daily_task_limit=1)
    auth.consume(task_id="t1", user="u1")
    status = auth.quota_status("u1")
    assert status["ok"] is False
    assert "今日任务数已达上限" in status["reason"]


def test_unlimited_credits_admin(auth):
    user = auth.create_user("boss", "pass123456", role="admin", credits=UNLIMITED_CREDITS)
    assert user["unlimited"] is True
    auth.consume(task_id="t1", user="boss")
    auth.consume(task_id="t2", user="boss")
    assert auth.get_user("boss")["credits"] == UNLIMITED_CREDITS


def test_codes_lifecycle(auth):
    """一张授权码 = 几次刷课；跑一次扣 1 次。"""
    codes = auth.create_codes(count=2, uses=3, name="测试班")
    assert len(codes) == 2

    ok, message, info = auth.verify_code(codes[0])
    assert ok and info["uses"] == 3 and info["remaining"] == 3
    assert info["remaining_label"] == "剩余 3 次"

    result = auth.consume(task_id="t1", code=codes[0])
    assert result["source"] == "code"
    assert result["remaining"] == 2
    assert auth.get_code(codes[0])["used"] == 1

    # 用尽后拒绝
    auth.consume(task_id="t2", code=codes[0])
    auth.consume(task_id="t3", code=codes[0])
    ok, message, _ = auth.verify_code(codes[0])
    assert not ok and "用尽" in message

    # 重置已用次数 / 改总次数 / 禁用 / 删除
    auth.reset_code(codes[0])
    assert auth.verify_code(codes[0])[0] is True
    auth.set_code_uses(codes[0], 10)
    assert auth.get_code(codes[0])["remaining"] == 10
    auth.toggle_code(codes[0], False)
    ok, message, _ = auth.verify_code(codes[0])
    assert not ok and "禁用" in message
    auth.delete_code(codes[0])
    assert auth.verify_code(codes[0])[0] is False


def test_code_priority_over_user_credits(auth):
    """授权码与用户额度同时存在时优先扣授权码。"""
    auth.create_user("u1", "pass123456", credits=5)
    code = auth.create_codes(count=1, uses=2)[0]

    result = auth.consume(task_id="t1", code=code, user="u1")
    assert result["source"] == "code"
    assert result["remaining"] == 1
    assert auth.get_user("u1")["credits"] == 5      # 用户额度没动
    assert auth.get_code(code)["used"] == 1


def test_code_without_login(auth):
    """不登录也能用授权码跑。"""
    code = auth.create_codes(count=1, uses=1)[0]
    result = auth.consume(task_id="t1", code=code)
    assert result["source"] == "code"
    assert result["remaining"] == 0


def test_need_code_or_login(auth):
    with pytest.raises(AuthError, match="授权码"):
        auth.consume(task_id="t1")


def test_invalid_code_is_rejected(auth):
    auth.create_user("u1", "pass123456", credits=5)
    with pytest.raises(AuthError, match="不存在"):
        auth.consume(task_id="t1", code="NOTEXIST99", user="u1")


def test_unlimited_code(auth):
    code = auth.create_codes(count=1, uses=-1)[0]
    assert auth.get_code(code)["unlimited"] is True
    for index in range(3):
        auth.consume(task_id=f"t{index}", code=code)
    assert auth.get_code(code)["remaining"] is None
    assert auth.verify_code(code)[0] is True


def test_failure_refunds_code_and_user(auth):
    """运行失败不扣次数：授权码退回授权码，用户额度退回用户。"""
    auth.create_user("u1", "pass123456", credits=1)
    code = auth.create_codes(count=1, uses=1)[0]

    auth.consume(task_id="t-code", code=code)
    assert auth.get_code(code)["remaining"] == 0
    assert auth.refund("t-code") is True
    assert auth.get_code(code)["remaining"] == 1

    auth.consume(task_id="t-user", user="u1")
    assert auth.get_user("u1")["credits"] == 0
    assert auth.refund("t-user") is True
    assert auth.get_user("u1")["credits"] == 1

    assert auth.refund("t-user") is False     # 不能重复退


def test_code_expiry(auth):
    expired = auth.create_codes(count=1, uses=1, expires_at="2020-01-01")[0]
    ok, message, info = auth.verify_code(expired)
    assert not ok and info["expired"] is True

    future = auth.create_codes(count=1, uses=1, expires_at="2099-01-01")[0]
    assert auth.verify_code(future)[0] is True


def test_code_usage_log(auth):
    code = auth.create_codes(count=1, uses=5)[0]
    auth.consume(task_id="t1", code=code, user="u1")
    usages = auth.code_usages(code)
    assert len(usages) == 1
    assert usages[0]["task_id"] == "t1"
    assert usages[0]["kind"] == "code"


def test_three_code_types(auth):
    """三种授权码：单次 / 有限次数 / 无限次数。"""
    single = auth.create_codes(count=1, code_type=CODE_SINGLE)[0]
    limited = auth.create_codes(count=1, code_type=CODE_LIMITED, uses=3)[0]
    unlimited = auth.create_codes(count=1, code_type=CODE_UNLIMITED)[0]

    info = auth.get_code(single)
    assert info["type"] == "single"
    assert info["type_label"] == "单次授权"
    assert info["uses"] == 1 and info["remaining"] == 1

    info = auth.get_code(limited)
    assert info["type"] == "limited"
    assert info["type_label"] == "有限次数"
    assert info["uses"] == 3 and info["remaining"] == 3

    info = auth.get_code(unlimited)
    assert info["type"] == "unlimited"
    assert info["type_label"] == "无限次数"
    assert info["unlimited"] is True and info["remaining"] is None
    assert info["remaining_label"] == "不限次数"


def test_single_code_only_once(auth):
    code = auth.create_codes(count=1, code_type=CODE_SINGLE, uses=99)[0]
    assert auth.get_code(code)["uses"] == 1          # 单次强制 1 次
    auth.consume(task_id="t1", code=code)
    ok, message, _ = auth.verify_code(code)
    assert not ok and "用尽" in message


def test_limited_code_requires_positive_uses(auth):
    with pytest.raises(AuthError, match="大于 0"):
        auth.create_codes(count=1, code_type=CODE_LIMITED, uses=0)
    with pytest.raises(AuthError, match="大于 0"):
        auth.create_codes(count=1, code_type=CODE_LIMITED, uses=-1)


def test_unlimited_code_ignores_uses(auth):
    code = auth.create_codes(count=1, code_type=CODE_UNLIMITED, uses=7)[0]
    assert auth.get_code(code)["uses"] == -1
    for index in range(15):
        auth.consume(task_id=f"t{index}", code=code)
    assert auth.verify_code(code)[0] is True


def test_code_type_inferred_from_uses(auth):
    """不显式传类型时按次数推断（兼容旧调用）。"""
    assert auth.get_code(auth.create_codes(count=1, uses=1)[0])["type"] == "single"
    assert auth.get_code(auth.create_codes(count=1, uses=5)[0])["type"] == "limited"
    assert auth.get_code(auth.create_codes(count=1, uses=-1)[0])["type"] == "unlimited"


def test_api_keys(auth):
    auth.create_user("u1", "pass123456")
    key = auth.create_api_key("u1", "机器人")
    assert key.startswith("cx_")
    assert auth.verify_api_key(key)["username"] == "u1"
    assert auth.verify_api_key("cx_invalid") is None
    auth.delete_api_key(key)
    assert auth.verify_api_key(key) is None


def test_audit_log(auth):
    auth.log("login", actor="admin", ip="1.2.3.4")
    auth.log("create_user", actor="admin", target="u1")
    logs = auth.list_audit(limit=10)
    assert len(logs) == 2
    assert logs[0]["action"] == "create_user"
    assert len(auth.list_audit(keyword="login")) == 1


def test_stats(auth):
    auth.ensure_bootstrap_admin("admin", "pass123456")
    auth.create_user("u1", "pass123456", credits=3)
    auth.create_codes(count=2, uses=1)
    auth.consume(task_id="t1", user="u1")
    stats = auth.stats()
    assert stats["users"]["total"] == 2
    assert stats["codes"]["total"] == 2
    assert stats["credits"]["used"] == 1


def test_migrates_legacy_code_table(tmp_path):
    """老版本数据库（type/max_uses/credits/used_count）要能平滑迁移到新结构。"""
    import sqlite3

    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE auth_codes ("
        " code TEXT PRIMARY KEY, type TEXT, name TEXT, note TEXT,"
        " max_uses INTEGER, used_count INTEGER, credits INTEGER, bound_user TEXT,"
        " enabled INTEGER, created_at TEXT, updated_at TEXT, expires_at TEXT,"
        " last_used_at TEXT);"
        "CREATE TABLE credit_usages ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, user TEXT, task_id TEXT,"
        " cost INTEGER, kind TEXT, refunded INTEGER, detail TEXT);"
    )
    conn.execute(
        "INSERT INTO auth_codes (code, type, name, max_uses, used_count, credits, enabled,"
        " created_at) VALUES ('OLDCODE123', 'limited', '老卡密', 5, 2, 3, 1, '2026-01-01 00:00:00')"
    )
    conn.execute(
        "INSERT INTO auth_codes (code, type, name, max_uses, used_count, credits, enabled,"
        " created_at) VALUES ('OLDUNLIMIT', 'unlimited', '老无限卡', 0, 7, 1, 1, '2026-01-01 00:00:00')"
    )
    conn.commit()
    conn.close()

    manager = AuthManager(db)
    try:
        info = manager.get_code("OLDCODE123")
        assert info is not None
        assert info["uses"] == 5          # max_uses → uses
        assert info["used"] == 2          # used_count → used
        assert info["remaining"] == 3
        assert info["name"] == "老卡密"

        unlimited = manager.get_code("OLDUNLIMIT")
        assert unlimited["unlimited"] is True
        assert unlimited["remaining"] is None
        # 迁移后仍可正常扣次
        manager.consume(task_id="t-old", code="OLDCODE123")
        assert manager.get_code("OLDCODE123")["remaining"] == 2
    finally:
        manager.close()
