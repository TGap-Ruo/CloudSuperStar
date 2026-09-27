# -*- coding: utf-8 -*-
"""定时调度：按账号的 cron 表达式拉起独立子进程执行刷课。

设计要点
--------
* **进程隔离**：每个账号一次运行都是独立子进程，超时可直接 kill，
  账号之间不会互相污染 Cookie / 缓存 / 全局状态。
* **硬超时**：``server.run_timeout_minutes`` 到点杀进程，并把该次运行标记为
  ``timeout``，避免卡死的任务永久占用调度槽位。
* **失败重试**：``server.retry_on_failure`` 次重试，间隔 ``retry_delay_minutes``。
* **并发上限**：``server.max_concurrent_accounts`` 限制同时运行的账号数量，
  避免同一 IP 短时间大量请求触发风控。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from server.config import ServerConfig, load_config
from server.paths import project_root
from server.runner import new_run_id
from server.store import Store

logger = logging.getLogger("chaoxing.scheduler")

TERMINAL_STATUSES = {"success", "partial"}


class AccountRunner:
    """以子进程方式执行单个账号。"""

    def __init__(self, config: ServerConfig, config_path: Path):
        self.config = config
        self.config_path = config_path
        self._semaphore = threading.Semaphore(config.server.max_concurrent_accounts)

    def _command(self, account_name: str, run_id: str, extra: list[str] | None = None) -> list[str]:
        cmd = [
            sys.executable,
            "-m",
            "server.job",
            "--config",
            str(self.config_path),
            "--account",
            account_name,
            "--run-id",
            run_id,
        ]
        if extra:
            cmd.extend(extra)
        return cmd

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        # 子进程统一按 UTF-8 输出，避免中文日志在英文 locale 下乱码
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUNBUFFERED", "1")
        if self.config.server.data_dir:
            env["CHAOXING_DATA_DIR"] = self.config.server.data_dir
        return env

    def run_once(self, account_name: str, *, extra: list[str] | None = None) -> tuple[str, str]:
        """执行一次并返回 (状态, 说明)。超过硬超时会标记为 timeout。"""
        run_id = new_run_id()
        timeout_seconds = self.config.server.run_timeout_minutes * 60
        logger.info("启动账号 %s 的运行任务 (run_id=%s)", account_name, run_id)

        started = time.monotonic()
        try:
            completed = subprocess.run(
                self._command(account_name, run_id, extra),
                cwd=str(project_root()),
                env=self._environment(),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - started
            logger.error(
                "账号 %s 运行超时（%.1f 分钟），已终止子进程", account_name, elapsed / 60
            )
            self._mark_timeout(account_name, run_id, timeout_seconds)
            return "timeout", f"超过 {self.config.server.run_timeout_minutes} 分钟未完成"

        elapsed = time.monotonic() - started
        lines = (completed.stdout or completed.stderr or "").strip().splitlines()
        message = lines[-1] if lines else ""
        if completed.returncode == 0:
            logger.info("账号 %s 运行完成（%.1f 分钟）", account_name, elapsed / 60)
            return "success", message

        logger.warning(
            "账号 %s 运行失败，返回码 %s（%.1f 分钟）",
            account_name,
            completed.returncode,
            elapsed / 60,
        )
        return "failed", message or f"退出码 {completed.returncode}"

    def _mark_timeout(self, account_name: str, run_id: str, timeout_seconds: int) -> None:
        store = Store(self.config.data_paths().db)
        try:
            row_id = store.run_row_id(run_id)
            if row_id is not None:
                store.finish_run(
                    row_id,
                    status="timeout",
                    message=f"运行超过 {timeout_seconds // 60} 分钟被强制终止",
                    duration_seconds=float(timeout_seconds),
                )
            store.record_account_result(
                account_name, status="timeout", message="运行超时被强制终止"
            )
        finally:
            store.close()

    def run_with_retry(self, account_name: str, *, extra: list[str] | None = None) -> tuple[str, str]:
        """带重试的执行入口（调度器直接调用）。"""
        attempts = self.config.server.retry_on_failure + 1
        last_status, last_message = "failed", ""
        for attempt in range(1, attempts + 1):
            with self._semaphore:
                status, message = self.run_once(account_name, extra=extra)
            last_status, last_message = status, message
            if status in TERMINAL_STATUSES:
                return status, message
            if attempt < attempts:
                delay = max(1, self.config.server.retry_delay_minutes)
                logger.warning(
                    "账号 %s 第 %s/%s 次运行未成功（%s），%s 分钟后重试",
                    account_name,
                    attempt,
                    attempts,
                    status,
                    delay,
                )
                time.sleep(delay * 60)
        return last_status, last_message


def build_scheduler(config: ServerConfig, config_path: Path):
    """构建 APScheduler 调度器（所有账号共享一个调度器实例）。"""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    timezone = ZoneInfo(config.server.timezone)
    runner = AccountRunner(config, config_path)
    scheduler = BlockingScheduler(
        timezone=timezone,
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 3600,
        },
    )

    scheduled = 0
    for account in config.enabled_accounts():
        if not account.schedule:
            logger.info("账号 %s 未配置 schedule，只能手动运行", account.name)
            continue
        trigger = CronTrigger.from_crontab(account.schedule, timezone=timezone)
        scheduler.add_job(
            runner.run_with_retry,
            trigger=trigger,
            args=[account.name],
            id=f"account:{account.name}",
            name=f"超星刷课 {account.name}",
            replace_existing=True,
        )
        scheduled += 1
        logger.info(
            "账号 %s 已排程: %s (%s)", account.name, account.schedule, config.server.timezone
        )

    if not scheduled:
        logger.warning("没有任何账号配置 schedule，调度器将空转；可用 `run` 命令手动执行")
    return scheduler, runner


def serve(config_path: str | os.PathLike, *, run_on_start: bool = False) -> int:
    """阻塞式启动调度服务。"""
    path = Path(config_path).expanduser().resolve()
    config = load_config(path)
    logging.basicConfig(
        level=getattr(logging, config.server.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    store = Store(config.data_paths().db)
    stale = store.mark_stale_running_as_failed("调度服务重启，上一次运行被标记为中断")
    if stale:
        logger.warning("清理了 %s 条中断的运行记录", stale)
    store.close()

    scheduler, runner = build_scheduler(config, path)

    if run_on_start:
        def _startup_runs() -> None:
            for account in config.enabled_accounts():
                runner.run_with_retry(account.name)

        threading.Thread(target=_startup_runs, daemon=True, name="startup-run").start()

    logger.info(
        "调度服务已启动，时区 %s，并发上限 %s",
        config.server.timezone,
        config.server.max_concurrent_accounts,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("调度服务已停止")
    return 0


def describe_schedule(config: ServerConfig) -> list[dict[str, Optional[str]]]:
    """返回各账号的排程摘要（供 CLI 展示）。"""
    from apscheduler.triggers.cron import CronTrigger

    rows: list[dict[str, Optional[str]]] = []
    for account in config.accounts:
        next_run = None
        if account.schedule:
            try:
                timezone = ZoneInfo(config.server.timezone)
                trigger = CronTrigger.from_crontab(account.schedule, timezone=timezone)
                next_run = str(
                    trigger.get_next_fire_time(None, datetime.now(timezone))
                )
            except Exception:  # noqa: BLE001
                next_run = "cron 解析失败"
        rows.append(
            {
                "account": account.name,
                "enabled": "是" if account.enabled else "否",
                "schedule": account.schedule or "-",
                "next_run": next_run or "-",
            }
        )
    return rows
