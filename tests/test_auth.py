# -*- coding: utf-8 -*-
"""鉴权模块测试：用户、额度、卡密、API Key、审计。"""

import pytest

from server.auth import (
    AuthError,
    AuthManager,
    CODE_LIMITED,
    CODE_SINGLE,
    CODE_UNLIMITED,
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

    auth.consume("u1", "task-1")
    auth.consume("u1", "task-2")
    assert auth.get_user("u1")["credits"] == 0
    assert auth.used_today("u1") == 2

    with pytest.raises(AuthError, match="次数已用完"):
        auth.consume("u1", "task-3")

    assert auth.refund("task-2") is True
    assert auth.get_user("u1")["credits"] == 1
    assert auth.used_today("u1") == 1        # 已退款的不计入今日任务数
    assert auth.refund("task-2") is False     # 不能重复退款


def test_daily_limit(auth):
    auth.create_user("u1", "pass123456", credits=10, daily_task_limit=1)
    auth.consume("u1", "t1")
    status = auth.quota_status("u1")
    assert status["ok"] is False
    assert "今日任务数已达上限" in status["reason"]


def test_unlimited_credits_admin(auth):
    user = auth.create_user("boss", "pass123456", role="admin", credits=UNLIMITED_CREDITS)
    assert user["unlimited"] is True
    auth.consume("boss", "t1")
    auth.consume("boss", "t2")
    assert auth.get_user("boss")["credits"] == UNLIMITED_CREDITS


def test_codes_lifecycle(auth):
    auth.create_user("u1", "pass123456", credits=0)
    codes = auth.create_codes(code_type=CODE_LIMITED, count=2, credits=3,
                              name="测试", max_uses=1)
    assert len(codes) == 2

    ok, message, info = auth.verify_code(codes[0])
    assert ok and info["credits"] == 3

    result = auth.redeem_code(codes[0], "u1")
    assert result["credits"] == 3
    assert auth.get_user("u1")["credits"] == 3

    ok, message, _ = auth.verify_code(codes[0])
    assert not ok and "用尽" in message

    auth.reset_code(codes[0])
    assert auth.verify_code(codes[0])[0] is True

    auth.toggle_code(codes[0], False)
    ok, message, _ = auth.verify_code(codes[0])
    assert not ok and "禁用" in message

    auth.delete_code(codes[0])
    assert auth.verify_code(codes[0])[0] is False


def test_single_and_unlimited_codes(auth):
    single = auth.create_codes(code_type=CODE_SINGLE, count=1, credits=1)[0]
    unlimited = auth.create_codes(code_type=CODE_UNLIMITED, count=1, credits=1)[0]
    auth.create_user("u1", "pass123456")

    info = auth.verify_code(single)[2]
    assert info["max_uses"] == 1 and info["type"] == "single"
    auth.redeem_code(single, "u1")
    assert auth.verify_code(single)[0] is False

    assert auth.verify_code(unlimited)[2]["remaining"] is None
    auth.redeem_code(unlimited, "u1")
    assert auth.verify_code(unlimited)[0] is True    # 无限卡可以继续用


def test_code_binding_and_expiry(auth):
    auth.create_user("u1", "pass123456")
    auth.create_user("u2", "pass123456")
    code = auth.create_codes(code_type=CODE_SINGLE, count=1, credits=1, bound_user="u1")[0]

    with pytest.raises(AuthError, match="绑定"):
        auth.redeem_code(code, "u2")
    assert auth.redeem_code(code, "u1")["credits"] == 1

    expired = auth.create_codes(code_type=CODE_SINGLE, count=1, credits=1,
                                expires_at="2020-01-01")[0]
    ok, message, info = auth.verify_code(expired)
    assert not ok and info["expired"] is True


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
    auth.create_codes(code_type=CODE_SINGLE, count=2, credits=1)
    auth.consume("u1", "t1")
    stats = auth.stats()
    assert stats["users"]["total"] == 2
    assert stats["codes"]["total"] == 2
    assert stats["credits"]["used"] == 1
