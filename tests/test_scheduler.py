# -*- coding: utf-8 -*-
import subprocess
import sys

from server.scheduler import AccountRunner, build_scheduler, describe_schedule
from server.store import Store


def test_describe_schedule(server_config):
    rows = describe_schedule(server_config)
    assert rows[0]["account"] == "tester"
    assert rows[0]["schedule"] == "0 8 * * *"
    assert rows[0]["next_run"] not in {"-", "cron 解析失败"}


def test_scheduler_registers_jobs(server_config, config_path):
    scheduler, runner = build_scheduler(server_config, config_path)
    job_ids = {job.id for job in scheduler.get_jobs()}
    assert job_ids == {"account:tester"}
    assert isinstance(runner, AccountRunner)


def test_scheduler_skips_accounts_without_schedule(server_config, config_path):
    server_config.accounts[0].schedule = ""
    scheduler, _ = build_scheduler(server_config, config_path)
    assert scheduler.get_jobs() == []


def test_run_once_success(server_config, config_path, monkeypatch):
    runner = AccountRunner(server_config, config_path)

    def fake_run(cmd, **kwargs):
        assert cmd[:3] == [sys.executable, "-m", "server.job"]
        assert kwargs["timeout"] == server_config.server.run_timeout_minutes * 60
        return subprocess.CompletedProcess(cmd, 0, stdout="跑完了\n", stderr="")

    monkeypatch.setattr("server.scheduler.subprocess.run", fake_run)
    status, message = runner.run_once("tester")
    assert status == "success"
    assert message == "跑完了"


def test_run_once_failure(server_config, config_path, monkeypatch):
    runner = AccountRunner(server_config, config_path)

    monkeypatch.setattr(
        "server.scheduler.subprocess.run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="失败了\n"),
    )
    status, message = runner.run_once("tester")
    assert status == "failed"
    assert message == "失败了"


def test_run_once_timeout_marks_run(server_config, config_path, monkeypatch):
    runner = AccountRunner(server_config, config_path)
    store = Store(server_config.data_paths().db)

    def fake_run(cmd, **kwargs):
        # 模拟子进程已经插入了 running 记录后被 kill
        run_id = cmd[cmd.index("--run-id") + 1]
        store.start_run("tester", run_id)
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    monkeypatch.setattr("server.scheduler.subprocess.run", fake_run)
    status, message = runner.run_once("tester")

    assert status == "timeout"
    assert "分钟" in message
    runs = store.recent_runs()
    assert len(runs) == 1
    assert runs[0].status == "timeout"
    assert store.account_state("tester")["last_status"] == "timeout"
    store.close()


def test_run_with_retry(server_config, config_path, monkeypatch):
    server_config.server.retry_on_failure = 1
    server_config.server.retry_delay_minutes = 0
    runner = AccountRunner(server_config, config_path)

    results = iter([("failed", "第一次失败"), ("success", "第二次成功")])
    calls: list[str] = []

    def fake_run_once(account_name, extra=None):
        calls.append(account_name)
        return next(results)

    monkeypatch.setattr(runner, "run_once", fake_run_once)
    monkeypatch.setattr("server.scheduler.time.sleep", lambda _seconds: None)

    status, message = runner.run_with_retry("tester")
    assert (status, message) == ("success", "第二次成功")
    assert calls == ["tester", "tester"]


def test_concurrency_semaphore_limits(server_config, config_path, monkeypatch):
    server_config.server.max_concurrent_accounts = 2
    runner = AccountRunner(server_config, config_path)
    assert runner._semaphore._value == 2  # noqa: SLF001 - 断言并发上限已生效
