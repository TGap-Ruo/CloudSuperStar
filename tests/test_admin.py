# -*- coding: utf-8 -*-
"""鉴权 + 管理后台 + 额度/退款的端到端测试（Web 层）。"""

import os
import sys
import time
from pathlib import Path

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
    """开启鉴权的配置。"""
    data_dir = tmp_path / "data"
    config = {
        "server": {
            "data_dir": str(data_dir),
            "web_enabled": True,
            "auth_enabled": True,
            "admin_path": "/admin",
            "admin_user": ADMIN_USER,
            "admin_password": ADMIN_PASS,
            "web_token": "",
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


@pytest.fixture
def auth_app(auth_config):
    config = load_config(auth_config)
    manager = TaskManager(
        auth_config,
        config,
        command_builder=_script_builder("print('done')"),
        courses_provider=lambda record: (True, [{"course_id": "1", "clazz_id": "2",
                                                 "title": "测试课", "teacher": ""}], ""),
    )
    app = create_app(auth_config, token="", task_manager=manager)
    app.config.update(TESTING=True)
    return app


def login(client, username, password):
    return client.post("/login", json={"username": username, "password": password})


def test_console_requires_login(auth_app):
    client = auth_app.test_client()
    resp = client.get("/")
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("Location", "")
    assert client.get("/api/tasks").status_code == 401
    assert client.get("/api/tasks").get_json()["need_login"] is True


def test_login_and_logout(auth_app):
    client = auth_app.test_client()
    assert login(client, ADMIN_USER, "wrong-password").status_code == 401
    assert login(client, ADMIN_USER, ADMIN_PASS).status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/logout").status_code == 302
    assert client.get("/").status_code == 302      # 登出后又被拦


def test_admin_page_and_apis(auth_app):
    client = auth_app.test_client()
    login(client, ADMIN_USER, ADMIN_PASS)
    page = client.get("/admin/")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    for keyword in ("仪表盘", "用户管理", "卡密管理", "任务管理", "用量与费用", "审计日志", "系统设置"):
        assert keyword in body
    for path in ("/overview", "/users", "/codes", "/tasks", "/usage", "/audit", "/settings"):
        assert client.get("/admin/api" + path).status_code == 200, path


def test_normal_user_cannot_access_admin(auth_app):
    client = auth_app.test_client()
    login(client, ADMIN_USER, ADMIN_PASS)
    client.post("/admin/api/users", json={"username": "stu", "password": "pass123456"})
    client.get("/logout")
    login(client, "stu", "pass123456")

    page = client.get("/admin/")
    assert page.status_code == 302 and page.headers["Location"].endswith("/")
    assert client.get("/admin/api/overview").status_code == 403
    assert client.get("/admin/api/users").status_code == 403


def test_admin_creates_codes_and_user_redeems(auth_app):
    client = auth_app.test_client()
    login(client, ADMIN_USER, ADMIN_PASS)
    client.post("/admin/api/users", json={"username": "stu", "password": "pass123456", "credits": 0})
    codes = client.post("/admin/api/codes",
                        json={"type": "single", "count": 1, "credits": 3}).get_json()["codes"]
    assert len(codes) == 1
    client.get("/logout")

    login(client, "stu", "pass123456")
    me = client.get("/api/me").get_json()
    assert me["user"]["credits"] == 0
    assert me["is_admin"] is False

    redeemed = client.post("/api/redeem", json={"code": codes[0]}).get_json()
    assert redeemed["credits"] == 3
    assert redeemed["quota"]["credits_label"] == 3

    # 用过的卡密不能再用
    assert client.post("/api/redeem", json={"code": codes[0]}).status_code == 400


def test_starting_task_consumes_and_failing_task_refunds(auth_config):
    """任务启动扣额度；任务失败自动退还。"""
    config = load_config(auth_config)
    manager = TaskManager(
        auth_config,
        config,
        command_builder=_script_builder("import sys; print('boom'); sys.exit(1)"),
        courses_provider=lambda record: (True, [], ""),
    )
    app = create_app(auth_config, token="", task_manager=manager)
    client = app.test_client()

    login(client, ADMIN_USER, ADMIN_PASS)
    client.post("/admin/api/users", json={"username": "stu", "password": "pass123456", "credits": 1})
    client.get("/logout")
    login(client, "stu", "pass123456")

    resp = client.post("/api/tasks", json={"username": "13800000000", "password": "x"})
    assert resp.status_code == 201, resp.get_json()
    task_id = resp.get_json()["started"][0]["id"]

    # 启动即扣 1 次
    assert client.get("/api/me").get_json()["user"]["credits"] == 0
    # 额度用完后不能再启动
    blocked = client.post("/api/tasks", json={"username": "13800000001", "password": "x"})
    assert blocked.status_code == 402
    assert "次数已用完" in blocked.get_json()["error"]

    # 任务失败 → 自动退款
    for _ in range(150):
        detail = client.get(f"/api/tasks/{task_id}").get_json()
        if detail["status"] != "running":
            break
        time.sleep(0.1)
    assert detail["status"] == "failed"
    assert client.get("/api/me").get_json()["user"]["credits"] == 1


def test_task_ownership_isolation(auth_config):
    """普通用户只能看到自己的任务。"""
    config = load_config(auth_config)
    manager = TaskManager(
        auth_config,
        config,
        command_builder=_script_builder("import time; time.sleep(5)"),
        courses_provider=lambda record: (True, [], ""),
    )
    app = create_app(auth_config, token="", task_manager=manager)
    admin = app.test_client()
    login(admin, ADMIN_USER, ADMIN_PASS)
    admin.post("/admin/api/users", json={"username": "a1", "password": "pass123456", "credits": 5})
    admin.post("/admin/api/users", json={"username": "a2", "password": "pass123456", "credits": 5})

    a1 = app.test_client()
    login(a1, "a1", "pass123456")
    task_id = a1.post("/api/tasks", json={"username": "13800000000", "password": "x"}).get_json()["started"][0]["id"]

    a2 = app.test_client()
    login(a2, "a2", "pass123456")
    assert a2.get("/api/tasks").get_json()["tasks"] == []
    assert a2.get(f"/api/tasks/{task_id}").status_code == 403
    assert a2.post(f"/api/tasks/{task_id}/stop").status_code == 403

    # 管理员能看到全部任务
    assert len(admin.get("/admin/api/tasks").get_json()["tasks"]) == 1
    admin.post(f"/admin/api/tasks/{task_id}/stop")


def test_admin_sees_task_usage_and_audit(auth_config):
    config = load_config(auth_config)
    manager = TaskManager(auth_config, config, command_builder=_script_builder("print('ok')"))
    app = create_app(auth_config, token="", task_manager=manager)
    client = app.test_client()
    login(client, ADMIN_USER, ADMIN_PASS)

    # 用量接口即使没有数据也应正常返回结构
    usage = client.get("/admin/api/usage").get_json()
    assert "totals" in usage and "groups" in usage
    assert "pricing" in usage

    audit = client.get("/admin/api/audit").get_json()["logs"]
    assert any(item["action"] == "login" for item in audit)


def test_settings_update_writes_config(auth_config):
    config = load_config(auth_config)
    manager = TaskManager(auth_config, config, command_builder=_script_builder("print('ok')"))
    app = create_app(auth_config, token="", task_manager=manager)
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
    assert resp.get_json()["restart_required"] is True

    raw = yaml.safe_load(Path(auth_config).read_text(encoding="utf-8"))
    provider = raw["answer"]["providers"][0]
    assert provider["key"] == "sk-newkey1234567890"
    assert provider["model"] == "deepseek-v4-pro"
    assert raw["pricing"]["deepseek-v4-pro"]["cache_miss"] == 1.5
    assert raw["server"]["credits_per_task"] == 2
    assert raw["study"]["speed"] == 1.5
    assert raw["answer"]["submit"] is False

    # 备份文件存在，便于回滚
    assert auth_config.with_suffix(".yaml.bak").exists()


def test_quota_guard_blocks_when_credits_zero(auth_config):
    config = load_config(auth_config)
    manager = TaskManager(auth_config, config, command_builder=_script_builder("print('ok')"))
    app = create_app(auth_config, token="", task_manager=manager)
    client = app.test_client()
    login(client, ADMIN_USER, ADMIN_PASS)
    client.post("/admin/api/users", json={"username": "poor", "password": "pass123456", "credits": 0})
    client.get("/logout")
    login(client, "poor", "pass123456")

    resp = client.post("/api/tasks", json={"username": "13800000000", "password": "x"})
    assert resp.status_code == 402
    assert "次数已用完" in resp.get_json()["error"]


def test_admin_path_is_configurable(tmp_path):
    data_dir = tmp_path / "data"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "server": {"data_dir": str(data_dir), "web_enabled": True, "auth_enabled": True,
                   "admin_path": "/my-secret-console", "admin_user": "root",
                   "admin_password": "rootpass123", "web_token": ""},
        "answer": {"providers": [{"type": "AI", "base_url": "https://api.deepseek.com/v1",
                                  "key": "sk-test1234567890", "model": "deepseek-flash"}]},
        "accounts": [],
    }, allow_unicode=True), encoding="utf-8")
    config = load_config(path)
    manager = TaskManager(path, config, command_builder=_script_builder("print('ok')"))
    app = create_app(path, token="", task_manager=manager)
    client = app.test_client()
    login(client, "root", "rootpass123")
    assert client.get("/my-secret-console/").status_code == 200
    assert client.get("/admin/api/overview").status_code == 404
