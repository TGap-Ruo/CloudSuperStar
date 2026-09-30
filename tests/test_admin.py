# -*- coding: utf-8 -*-
"""鉴权 + 管理后台 + 授权码/额度 的端到端测试（Web 层）。

核心规则（与产品约定一致）：
* 进页面不需要登录；**开始刷课**才要求"填了授权码"或"已登录且有额度"；
* 授权码与用户额度同时存在时**优先扣授权码**；
* 授权码是独立实体，不能充值到用户账号上；
* 任务失败（如学习通账号密码错误）**不扣次数**；
* 后台只挂在隐藏路径下，前台不暴露入口。
"""

import os
import sys
import time

import pytest
import yaml

from server.config import load_config
from server.paths import project_root
from server.tasks import TaskManager
from server.web import create_app

ADMIN_USER = "admin"
ADMIN_PASS = "admin123456"


def _script_builder(script: str):
    def builder(record):
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        return [sys.executable, "-c", script], project_root(), env

    return builder


@pytest.fixture
def auth_config(tmp_path):
    config = {
        "server": {
            "data_dir": str(tmp_path / "data"),
            "web_enabled": True,
            "auth_enabled": True,
            "admin_path": "/admin",
            "admin_user": ADMIN_USER,
            "admin_password": ADMIN_PASS,
            "log_level": "WARNING",
            "web_max_parallel_tasks": 4,
        },
        "study": {"speed": 1.0, "jobs": 2, "notopen_action": "continue"},
        "answer": {
            "submit": True,
            "cover_rate": 0.6,
            "providers": [
                {"type": "AI", "base_url": "https://api.deepseek.com/v1",
                 "key": "sk-test1234567890", "model": "deepseek-flash"}
            ],
        },
        "notify": {"provider": "none"},
        "accounts": [],
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return path


def build_app(auth_config, *, script="print('done')", courses=None):
    config = load_config(auth_config)
    manager = TaskManager(
        auth_config,
        config,
        command_builder=_script_builder(script),
        courses_provider=lambda record: (True, courses or [], ""),
    )
    app = create_app(auth_config, task_manager=manager)
    app.config.update(TESTING=True)
    return app


@pytest.fixture
def auth_app(auth_config):
    return build_app(auth_config)


def login(client, username, password):
    return client.post("/login", json={"username": username, "password": password})


def new_code(client, uses=2, count=1):
    """以管理员身份生成授权码，返回第一张。"""
    login(client, ADMIN_USER, ADMIN_PASS)
    codes = client.post("/admin/api/codes",
                        json={"count": count, "uses": uses}).get_json()["codes"]
    client.get("/logout")
    return codes[0]


# ─────────────────────────── 页面与登录 ───────────────────────────

def test_console_is_open_without_login(auth_app):
    """默认不需要登录，直接进界面。"""
    client = auth_app.test_client()
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "授权码" in body
    assert "管理后台" not in body          # 前台不暴露后台入口
    assert "兑换卡密" not in body

    me = client.get("/api/me").get_json()
    assert me["logged_in"] is False


def test_login_and_logout(auth_app):
    client = auth_app.test_client()
    assert login(client, ADMIN_USER, "wrong-password").status_code == 401
    assert login(client, ADMIN_USER, ADMIN_PASS).status_code == 200
    assert client.get("/api/me").get_json()["logged_in"] is True
    assert client.get("/logout").status_code == 302
    assert client.get("/api/me").get_json()["logged_in"] is False
    assert client.get("/").status_code == 200      # 退出后依然能用页面（只是要填授权码）


def test_admin_page_and_apis(auth_app):
    client = auth_app.test_client()
    login(client, ADMIN_USER, ADMIN_PASS)
    page = client.get("/admin/")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    for keyword in ("仪表盘", "用户管理", "授权码管理", "任务管理", "用量与费用", "审计日志", "系统设置"):
        assert keyword in body
    for path in ("/overview", "/users", "/codes", "/tasks", "/usage", "/audit", "/settings"):
        assert client.get("/admin/api" + path).status_code == 200, path


def test_normal_user_cannot_access_admin(auth_app):
    client = auth_app.test_client()
    login(client, ADMIN_USER, ADMIN_PASS)
    client.post("/admin/api/users", json={"username": "stu", "password": "pass123456", "credits": 3})
    client.get("/logout")
    login(client, "stu", "pass123456")

    page = client.get("/admin/")
    assert page.status_code == 302 and page.headers["Location"].endswith("/")
    assert client.get("/admin/api/overview").status_code == 403
    assert client.get("/admin/api/codes").status_code == 403


def test_admin_path_is_configurable(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "server": {"data_dir": str(tmp_path / "data"), "web_enabled": True, "auth_enabled": True,
                   "admin_path": "/my-secret-console", "admin_user": "root",
                   "admin_password": "rootpass123"},
        "answer": {"providers": [{"type": "AI", "base_url": "https://api.deepseek.com/v1",
                                  "key": "sk-test1234567890", "model": "deepseek-flash"}]},
        "accounts": [],
    }, allow_unicode=True), encoding="utf-8")
    app = build_app(path)
    client = app.test_client()
    login(client, "root", "rootpass123")
    assert client.get("/my-secret-console/").status_code == 200
    assert client.get("/admin/api/overview").status_code == 404


# ─────────────────────────── 授权码校验与放行 ───────────────────────────

def test_no_code_and_not_logged_in_is_rejected(auth_app):
    client = auth_app.test_client()
    resp = client.post("/api/tasks", json={"username": "13800000000", "password": "x"})
    assert resp.status_code == 402
    assert "授权码" in resp.get_json()["error"]

    check = client.post("/api/quota/check", json={})
    assert check.status_code == 402
    assert check.get_json()["ok"] is False


def test_invalid_code_is_rejected(auth_app):
    client = auth_app.test_client()
    resp = client.post("/api/tasks",
                       json={"username": "13800000000", "password": "x", "code": "NOTEXIST99"})
    assert resp.status_code == 402
    assert "不存在" in resp.get_json()["error"]


def test_valid_code_allows_running_without_login(auth_config):
    """不登录，只要有有效授权码就能跑。"""
    app = build_app(auth_config)
    client = app.test_client()
    code = new_code(client, uses=2)

    check = client.post("/api/quota/check", json={"code": code}).get_json()
    assert check["ok"] is True
    assert check["quota"]["source"] == "code"
    assert check["quota"]["remaining"] == 2

    resp = client.post("/api/tasks",
                       json={"username": "13800000000", "password": "x", "code": code})
    assert resp.status_code == 201, resp.get_json()
    task_id = resp.get_json()["started"][0]["id"]

    # 扣掉 1 次
    after = client.post("/api/quota/check", json={"code": code}).get_json()
    assert after["quota"]["remaining"] == 1

    for _ in range(150):
        detail = client.get(f"/api/tasks/{task_id}?code={code}").get_json()
        if detail["status"] != "running":
            break
        time.sleep(0.1)
    assert detail["status"] == "finished"


def test_exhausted_code_is_rejected(auth_config):
    app = build_app(auth_config)
    client = app.test_client()
    code = new_code(client, uses=1)
    assert client.post("/api/tasks",
                       json={"username": "13800000000", "password": "x", "code": code}).status_code == 201
    second = client.post("/api/tasks",
                         json={"username": "13800000001", "password": "x", "code": code})
    assert second.status_code == 402
    assert "用尽" in second.get_json()["error"]


def test_code_priority_over_logged_in_user(auth_config):
    """同时有授权码与登录额度时，优先扣授权码。"""
    app = build_app(auth_config)
    admin = app.test_client()
    admin.post("/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
    admin.post("/admin/api/users", json={"username": "stu", "password": "pass123456", "credits": 5})
    codes = admin.post("/admin/api/codes", json={"count": 1, "uses": 2}).get_json()["codes"]
    admin.get("/logout")

    client = app.test_client()
    login(client, "stu", "pass123456")
    resp = client.post("/api/tasks",
                       json={"username": "13800000000", "password": "x", "code": codes[0]})
    assert resp.status_code == 201
    check = client.post("/api/quota/check", json={"code": codes[0]}).get_json()
    assert check["quota"]["source"] == "code"
    assert check["quota"]["remaining"] == 1
    assert client.get("/api/me").get_json()["user"]["credits"] == 5      # 用户额度没动


def test_user_credits_used_when_no_code(auth_config):
    app = build_app(auth_config)
    admin = app.test_client()
    admin.post("/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
    admin.post("/admin/api/users", json={"username": "stu", "password": "pass123456", "credits": 2})
    admin.get("/logout")

    client = app.test_client()
    login(client, "stu", "pass123456")
    resp = client.post("/api/tasks", json={"username": "13800000000", "password": "x"})
    assert resp.status_code == 201
    assert client.get("/api/me").get_json()["user"]["credits"] == 1


# ─────────────────────────── 失败不扣次数 ───────────────────────────

def test_failed_task_refunds_code(auth_config):
    """任务失败（进程退出码非 0）→ 授权码次数自动退回。"""
    app = build_app(auth_config, script="import sys; print('boom'); sys.exit(1)")
    client = app.test_client()
    code = new_code(client, uses=2)

    resp = client.post("/api/tasks",
                       json={"username": "13800000000", "password": "x", "code": code})
    task_id = resp.get_json()["started"][0]["id"]
    assert client.post("/api/quota/check", json={"code": code}).get_json()["quota"]["remaining"] == 1

    for _ in range(150):
        detail = client.get(f"/api/tasks/{task_id}?code={code}").get_json()
        if detail["status"] != "running":
            break
        time.sleep(0.1)
    assert detail["status"] == "failed"
    assert client.post("/api/quota/check", json={"code": code}).get_json()["quota"]["remaining"] == 2


def test_login_failure_during_prepare_does_not_consume(auth_config):
    """单个账号模式：读课程阶段就发现密码错，此时不应扣次数。"""
    config = load_config(auth_config)
    manager = TaskManager(
        auth_config,
        config,
        command_builder=_script_builder("print('ok')"),
        courses_provider=lambda record: (False, [], "RuntimeError: 用户名或密码错误"),
    )
    app = create_app(auth_config, task_manager=manager)
    client = app.test_client()
    code = new_code(client, uses=3)

    resp = client.post("/api/courses",
                       json={"username": "138", "password": "wrong", "code": code})
    assert resp.status_code == 400
    assert "密码错误" in resp.get_json()["error"]
    # 次数没动
    assert client.post("/api/quota/check", json={"code": code}).get_json()["quota"]["remaining"] == 3


# ─────────────────────────── 任务可见性 ───────────────────────────

def test_tasks_are_isolated_by_code(auth_config):
    """授权码相当于身份：只能看到自己授权码启动的任务。"""
    app = build_app(auth_config, script="import time; time.sleep(5)")
    client = app.test_client()
    code_a = new_code(client, uses=2, count=2)
    codes = client.get("/admin/api/codes").get_json()["codes"] if False else None
    # 再拿一张不同的授权码
    login(client, ADMIN_USER, ADMIN_PASS)
    code_b = client.post("/admin/api/codes", json={"count": 1, "uses": 2}).get_json()["codes"][0]
    client.get("/logout")

    task = client.post("/api/tasks",
                       json={"username": "13800000000", "password": "x", "code": code_a}).get_json()
    task_id = task["started"][0]["id"]

    listed_a = client.get(f"/api/tasks?code={code_a}").get_json()["tasks"]
    assert [item["id"] for item in listed_a] == [task_id]
    assert client.get(f"/api/tasks?code={code_b}").get_json()["tasks"] == []
    assert client.get("/api/tasks").get_json()["tasks"] == []
    assert client.get(f"/api/tasks/{task_id}?code={code_b}").status_code == 403

    client.post(f"/api/tasks/{task_id}/stop?code={code_a}")


# ─────────────────────────── 用量与设置 ───────────────────────────

def test_admin_sees_code_usage_and_audit(auth_app):
    client = auth_app.test_client()
    login(client, ADMIN_USER, ADMIN_PASS)
    usage = client.get("/admin/api/usage").get_json()
    assert "groups" in usage and "code" in usage["groups"]
    assert "pricing" in usage
    audit = client.get("/admin/api/audit").get_json()["logs"]
    assert any(item["action"] == "login" for item in audit)


def test_settings_update_writes_config(auth_config):
    app = build_app(auth_config)
    client = app.test_client()
    login(client, ADMIN_USER, ADMIN_PASS)
    resp = client.post(
        "/admin/api/settings",
        json={
            "deepseek_key": "sk-newkey1234567890",
            "deepseek_model": "deepseek-v4-pro",
            "pricing": {"deepseek-v4-pro": {"cache_hit": 0.05, "cache_miss": 1.5, "output": 4.0}},
            "security": {"credits_per_task": 2, "usd_to_cny": 7.1},
            "study": {"speed": 1.5, "jobs": 3, "submit": False, "cover_rate": 0.8},
        },
    )
    assert resp.status_code == 200
    raw = yaml.safe_load(auth_config.read_text(encoding="utf-8"))
    assert raw["answer"]["providers"][0]["model"] == "deepseek-v4-pro"
    assert raw["pricing"]["deepseek-v4-pro"]["cache_miss"] == 1.5
    assert raw["server"]["credits_per_task"] == 2


def test_code_management_apis(auth_app):
    client = auth_app.test_client()
    login(client, ADMIN_USER, ADMIN_PASS)
    codes = client.post("/admin/api/codes", json={"count": 2, "uses": 5}).get_json()["codes"]
    listed = client.get("/admin/api/codes").get_json()["codes"]
    assert len(listed) == 2
    assert listed[0]["uses"] == 5 and listed[0]["used"] == 0

    assert client.post(f"/admin/api/codes/{codes[0]}/uses",
                       json={"uses": 9}).status_code == 200
    assert client.get("/admin/api/codes").get_json()["codes"][0]["uses"] == 9

    assert client.post(f"/admin/api/codes/{codes[0]}/toggle",
                       json={"enabled": False}).status_code == 200
    assert client.post(f"/admin/api/codes/{codes[0]}/reset").status_code == 200
    detail = client.get(f"/admin/api/codes/{codes[0]}/usages").get_json()
    assert "usages" in detail and "usage" in detail
    assert client.delete(f"/admin/api/codes/{codes[0]}").status_code == 200
