# -*- coding: utf-8 -*-
import os
import sys
import time
from pathlib import Path

import pytest
import yaml

from server.config import load_config
from server.paths import project_root
from server.tasks import TaskError, TaskManager, deepseek_status


def _script_builder(script: str):
    """构造一个只跑本地 Python 脚本的命令，避免测试访问真实学习通。"""

    def builder(record):
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        return [sys.executable, "-c", script], project_root(), env

    return builder


def _wait_status(manager: TaskManager, task_id: str, timeout: float = 20.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = manager.get_task(task_id)
        if record is not None and record.status != "running":
            return record.status
        time.sleep(0.1)
    return "timeout"


def test_start_task_captures_output(server_config, config_path, tmp_path):
    manager = TaskManager(
        config_path,
        server_config,
        command_builder=_script_builder("print('hello 学习通')\nprint('done')"),
    )
    record = manager.start_task("13800000000", "secret")
    assert record.status == "running"

    assert _wait_status(manager, record.id) == "finished"
    output, total = manager.get_output(record.id)
    assert "hello 学习通" in output
    assert total >= 2
    assert manager.log_path(record.id).is_file()
    assert manager.get_task(record.id).exit_code == 0
    assert manager.stats()["finished"] == 1


def test_failed_task_marks_failed(server_config, config_path):
    manager = TaskManager(
        config_path,
        server_config,
        command_builder=_script_builder("import sys; print('boom'); sys.exit(1)"),
    )
    record = manager.start_task("13800000000", "secret")
    assert _wait_status(manager, record.id) == "failed"
    assert manager.get_task(record.id).exit_code == 1


def test_task_generates_isolated_config(server_config, config_path):
    manager = TaskManager(
        config_path, server_config, command_builder=_script_builder("print('ok')")
    )
    record = manager.start_task(
        "13800000000",
        "secret",
        course_ids=["2151141", "189191"],
        speed=1.5,
        jobs=2,
        submit=False,
        cover_rate=0.8,
        work_max_retries=1,
    )
    _wait_status(manager, record.id)

    task_config_path = Path(record.config_file)
    assert task_config_path.is_file()
    assert oct(task_config_path.stat().st_mode)[-3:] == "600"

    task_config = load_config(task_config_path)
    assert task_config.server.web_enabled is False
    assert str(task_config.data_paths().root) == str(server_config.data_paths().root)

    account = task_config.accounts[0]
    assert account.username == "13800000000"
    assert account.password == "secret"
    assert account.study.include_courses == ["2151141", "189191"]
    assert account.study.speed == 1.5
    assert account.study.jobs == 2
    assert account.answer.submit is False
    assert account.answer.cover_rate == 0.8
    # DeepSeek Key 继承自主配置，网页任务无需再次配置
    assert account.answer.providers[0].options["key"] == "sk-test"
    # 任务账号独立，避免多个账号互相覆盖 Cookie
    assert account.name == record.account
    assert account.name.startswith("web-")


def test_parallel_limit(server_config, config_path):
    manager = TaskManager(
        config_path,
        server_config,
        max_parallel=1,
        command_builder=_script_builder("import time; print('start', flush=True); time.sleep(30)"),
    )
    first = manager.start_task("13800000001", "secret")
    with pytest.raises(TaskError, match="上限"):
        manager.start_task("13800000002", "secret")
    manager.stop_task(first.id)


def test_stop_and_delete(server_config, config_path):
    manager = TaskManager(
        config_path,
        server_config,
        max_parallel=2,
        command_builder=_script_builder("import time; print('start', flush=True); time.sleep(30)"),
    )
    record = manager.start_task("13800000003", "secret")
    time.sleep(0.6)

    ok, message = manager.stop_task(record.id)
    assert ok, message
    assert manager.get_task(record.id).status == "stopped"

    task_dir = manager.task_root / record.id
    account_dir = server_config.data_paths().account(record.account).root
    assert task_dir.is_dir() and account_dir.is_dir()

    ok, message = manager.delete_task(record.id)
    assert ok, message
    assert manager.get_task(record.id) is None
    assert not task_dir.exists()
    assert not account_dir.exists()


def test_delete_running_task_is_rejected(server_config, config_path):
    manager = TaskManager(
        config_path,
        server_config,
        command_builder=_script_builder("import time; time.sleep(30)"),
    )
    record = manager.start_task("13800000004", "secret")
    ok, message = manager.delete_task(record.id)
    assert not ok
    assert "先停止" in message
    manager.stop_task(record.id)


def test_password_required(server_config, config_path):
    manager = TaskManager(config_path, server_config, command_builder=_script_builder("pass"))
    with pytest.raises(TaskError, match="密码"):
        manager.start_task("13800000005", "")


def test_index_persists_and_running_becomes_stopped(server_config, config_path):
    builder = _script_builder("import time; print('go', flush=True); time.sleep(30)")
    manager = TaskManager(config_path, server_config, command_builder=builder)
    record = manager.start_task("13800000006", "secret")
    time.sleep(0.5)

    reloaded = TaskManager(config_path, server_config, command_builder=builder)
    restored = reloaded.get_task(record.id)
    assert restored is not None
    assert restored.status == "stopped"
    assert "服务重启" in restored.stop_reason

    manager.stop_task(record.id)


def test_deepseek_status_detects_key(server_config):
    status = deepseek_status(server_config)
    assert status["configured"] is True
    assert status["model"] == "deepseek-chat"


def test_deepseek_status_without_key(tmp_path, config_path):
    raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    raw["answer"]["providers"] = [{"type": "TikuGo", "authorization": "x"}]
    Path(config_path).write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    config = load_config(config_path)
    assert deepseek_status(config)["configured"] is False
