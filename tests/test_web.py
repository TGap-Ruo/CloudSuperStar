# -*- coding: utf-8 -*-
import json
import os
import sys
import time
from pathlib import Path

import pytest

from server.config import load_config
from server.paths import project_root
from server.tasks import TaskManager
from server.web import create_app


def _script_builder(script: str):
    def builder(record):
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        return [sys.executable, "-c", script], project_root(), env

    return builder


@pytest.fixture
def web_app(config_path, server_config):
    manager = TaskManager(
        config_path,
        server_config,
        command_builder=_script_builder("print('log-line-1')\nprint('log-line-2')"),
    )
    app = create_app(config_path, token="test-token", task_manager=manager)
    app.config.update(TESTING=True)
    return app


def _client(web_app, token="test-token"):
    client = web_app.test_client()
    if token:
        client.environ_base["HTTP_X_TOKEN"] = token
    return client


def test_healthz_needs_no_token(web_app):
    response = web_app.test_client().get("/healthz")
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "ok"


def test_index_renders(web_app):
    response = web_app.test_client().get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "自动刷课控制台" in body
    assert "批量并行" in body


def test_api_requires_token(web_app):
    response = web_app.test_client().get("/api/tasks")
    assert response.status_code == 401
    assert response.get_json()["need_token"] is True


def test_start_task_and_stream(web_app):
    client = _client(web_app)
    response = client.post(
        "/api/tasks",
        json={"username": "13800000000", "password": "secret"},
    )
    assert response.status_code == 201
    payload = response.get_json()
    assert len(payload["started"]) == 1
    task_id = payload["started"][0]["id"]

    # 等待任务结束
    for _ in range(100):
        detail = client.get(f"/api/tasks/{task_id}").get_json()
        if detail["status"] != "running":
            break
        time.sleep(0.1)
    assert detail["status"] == "finished"

    output = client.get(f"/api/tasks/{task_id}/output").get_json()
    assert "log-line-1" in output["output"]
    assert output["total_lines"] >= 2

    log = client.get(f"/api/tasks/{task_id}/log")
    assert log.status_code == 200
    assert b"log-line-1" in log.data

    tasks = client.get("/api/tasks").get_json()
    assert tasks["stats"]["finished"] == 1
    assert tasks["tasks"][0]["username"] == "13800000000"


def test_start_task_requires_account(web_app):
    client = _client(web_app)
    assert client.post("/api/tasks", json={}).status_code == 400
    assert client.post("/api/tasks", json={"username": "1", "password": ""}).status_code == 400


def test_batch_accounts_parallel(web_app):
    client = _client(web_app)
    response = client.post(
        "/api/tasks",
        json={"accounts_text": "13800000001,pass1\n13800000002,pass2\n# 注释行\n"},
    )
    assert response.status_code == 201
    started = response.get_json()["started"]
    assert len(started) == 2
    usernames = {item["username"] for item in started}
    assert usernames == {"13800000001", "13800000002"}

    # 每个任务都应有独立账号目录，互不干扰
    for item in started:
        assert item["account"].startswith("web-")
        assert len({entry["account"] for entry in started}) == 2


def test_delete_and_stop_flow(config_path, server_config):
    manager = TaskManager(
        config_path,
        server_config,
        command_builder=_script_builder("import time; print('run', flush=True); time.sleep(20)"),
    )
    app = create_app(config_path, token="", task_manager=manager)
    client = app.test_client()

    task_id = client.post(
        "/api/tasks", json={"username": "13800000009", "password": "x"}
    ).get_json()["started"][0]["id"]

    assert client.delete(f"/api/tasks/{task_id}").status_code == 400  # 运行中不允许删除
    assert client.post(f"/api/tasks/{task_id}/stop").status_code == 200
    assert client.delete(f"/api/tasks/{task_id}").status_code == 200
    assert client.get(f"/api/tasks/{task_id}").status_code == 404


def test_meta_and_status(web_app):
    client = _client(web_app)
    meta = client.get("/api/meta").get_json()
    assert meta["deepseek"]["configured"] is True
    assert meta["max_parallel"] >= 1

    status = client.get("/api/status").get_json()
    assert "accounts" in status and "runs" in status


def test_options_are_passed_through(web_app):
    client = _client(web_app)
    response = client.post(
        "/api/tasks",
        json={
            "username": "13800000010",
            "password": "x",
            "course_ids": "2151141, 189191",
            "speed": 1.5,
            "jobs": 2,
            "submit": False,
            "cover_rate": 0.75,
            "work_max_retries": 1,
        },
    )
    assert response.status_code == 201
    options = response.get_json()["started"][0]["options"]
    assert options["course_ids"] == ["2151141", "189191"]
    assert options["speed"] == 1.5
    assert options["jobs"] == 2
    assert options["submit"] is False
    assert options["cover_rate"] == 0.75


def test_no_token_mode_allows_access(config_path, server_config):
    manager = TaskManager(config_path, server_config, command_builder=_script_builder("pass"))
    app = create_app(config_path, token="", task_manager=manager)
    assert app.test_client().get("/api/tasks").status_code == 200
