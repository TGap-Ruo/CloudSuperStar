# -*- coding: utf-8 -*-
"""网页端任务管理。

每个任务 = 一个独立子进程（``python -m server.job``），因此天然支持多账号并行：
账号之间不共享 Cookie / 缓存 / 工作目录，互不影响；卡死可以单独终止。

与 ``server/scheduler.py`` 的关系：

* 调度器负责 ``config.yaml`` 里配置的定时账号；
* 本模块负责网页上临时录入的账号（立即执行）；
* 两者都调用同一个 ``server.job``，也共用同一个 SQLite 状态库，
  所以 ``status`` 命令 / 状态列表里能看到全部运行记录。

网页任务的账号在数据目录中使用 ``web-<任务ID>`` 作为内部账号名，
展示给用户的是他输入的账号（手机号），密码只写入任务私有的 config.yaml。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from server.config import ServerConfig
from server.paths import DataPaths, project_root, sanitize_account_name
from server.store import read_report

MAX_BUFFER_LINES = 5000
TERMINAL_STATUSES = {"finished", "failed", "stopped"}
SELECTING_STATUS = "selecting"
MAX_SELECTING_TASKS = 10
LOGIN_TIMEOUT_SECONDS = 120

# 日志里的 ANSI 颜色码：网页终端与前缀下载的日志都不需要它们
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    """去掉 ANSI 颜色码与 \r，保证网页终端与下载的日志都干净可读。"""
    return ANSI_RE.sub("", text).replace("\r", "")

_TASK_FIELDS = {
    "id",
    "account",
    "username",
    "display_name",
    "status",
    "created_at",
    "finished_at",
    "exit_code",
    "stop_reason",
    "log_file",
    "config_file",
    "options",
    "summary",
    "report_file",
    "courses",
}

CommandBuilder = Callable[["TaskRecord"], tuple[list[str], Path, dict[str, str]]]
CoursesProvider = Callable[["TaskRecord"], tuple[bool, list[dict[str, Any]], str]]


class TaskError(RuntimeError):
    """任务相关错误（参数非法、超出并发上限等）。"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class TaskRecord:
    id: str
    account: str
    username: str
    display_name: str
    status: str = "running"
    created_at: str = field(default_factory=_now)
    finished_at: Optional[str] = None
    exit_code: Optional[int] = None
    stop_reason: str = ""
    log_file: str = ""
    config_file: str = ""
    options: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] | None = None
    report_file: str = ""
    courses: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """对外输出（不含任何密码信息）。"""
        return {
            "id": self.id,
            "account": self.account,
            "username": self.username,
            "display_name": self.display_name,
            "status": self.status,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "stop_reason": self.stop_reason,
            "options": dict(self.options),
            "summary": self.summary,
            "has_log": bool(self.log_file and Path(self.log_file).is_file()),
            "courses": list(self.courses),
        }


class TaskManager:
    """任务的创建、输出捕获、状态查询与生命周期管理。"""

    def __init__(
        self,
        config_path: str | os.PathLike,
        config: ServerConfig,
        *,
        data_paths: DataPaths | None = None,
        max_parallel: int | None = None,
        command_builder: CommandBuilder | None = None,
        courses_provider: CoursesProvider | None = None,
    ):
        self.config_path = Path(config_path).expanduser().resolve()
        self.config = config
        self.data_paths = data_paths or config.data_paths()
        self.task_root = self.data_paths.root / "web_tasks"
        self.index_file = self.task_root / "index.json"
        self.max_parallel = max_parallel or config.server.web_max_parallel_tasks
        self.command_builder = command_builder
        self.courses_provider = courses_provider or self._default_courses_provider

        self._tasks: dict[str, TaskRecord] = {}
        self._buffers: dict[str, deque[str]] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._handles: dict[str, Any] = {}
        self._lock = threading.RLock()

        self.task_root.mkdir(parents=True, exist_ok=True)
        self._load_index()

    # ------------------------------------------------------------------ 索引
    def _load_index(self) -> None:
        if not self.index_file.is_file():
            return
        try:
            records = json.loads(self.index_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for item in records if isinstance(records, list) else []:
            try:
                record = TaskRecord(
                    **{key: value for key, value in item.items() if key in _TASK_FIELDS}
                )
            except (TypeError, AttributeError):
                continue
            if record.status not in TERMINAL_STATUSES:
                # 服务重启后进程已不存在
                record.status = "stopped"
                record.stop_reason = record.stop_reason or "服务重启，任务中断"
                record.finished_at = record.finished_at or _now()
            self._tasks[record.id] = record

    def _save_index(self) -> None:
        payload = [record.to_dict() | {"log_file": record.log_file,
                                       "config_file": record.config_file,
                                       "report_file": record.report_file}
                   for record in self._tasks.values()]
        tmp = self.index_file.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.index_file)
        except OSError:
            pass

    # ------------------------------------------------------------------ 查询
    def list_tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            records = sorted(
                self._tasks.values(), key=lambda r: r.created_at, reverse=True
            )
        return [record.to_dict() for record in records[: max(1, limit)]]

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            return self._tasks.get(task_id)

    def running_count(self) -> int:
        with self._lock:
            return sum(1 for r in self._tasks.values() if r.status == "running")

    def selecting_count(self) -> int:
        with self._lock:
            return sum(1 for r in self._tasks.values() if r.status == SELECTING_STATUS)

    def stats(self) -> dict[str, int]:
        with self._lock:
            records = list(self._tasks.values())
        return {
            "total": len(records),
            "running": sum(1 for r in records if r.status == "running"),
            "finished": sum(1 for r in records if r.status == "finished"),
            "failed": sum(1 for r in records if r.status == "failed"),
            "stopped": sum(1 for r in records if r.status == "stopped"),
            "max_parallel": self.max_parallel,
        }

    def get_output(self, task_id: str, start_line: int = 0) -> tuple[str, int]:
        """返回 (从 start_line 开始的输出, 总行数)。"""
        with self._lock:
            buffer = self._buffers.get(task_id)
            record = self._tasks.get(task_id)
        if buffer is not None:
            lines = list(buffer)
            return "".join(lines[start_line:]), len(lines)
        if record and record.log_file and Path(record.log_file).is_file():
            try:
                with open(record.log_file, "r", encoding="utf-8", errors="replace") as handle:
                    lines = handle.readlines()
                return "".join(lines[start_line:]), len(lines)
            except OSError:
                pass
        return "", 0

    def log_path(self, task_id: str) -> Path | None:
        record = self.get_task(task_id)
        if record and record.log_file and Path(record.log_file).is_file():
            return Path(record.log_file)
        return None

    # ------------------------------------------------------------------ 启动
    def start_task(
        self,
        username: str,
        password: str,
        *,
        display_name: str = "",
        course_ids: Optional[list[str]] = None,
        speed: Optional[float] = None,
        jobs: Optional[int] = None,
        submit: Optional[bool] = None,
        cover_rate: Optional[float] = None,
        work_max_retries: Optional[int] = None,
        use_cookies: bool = False,
        cookie: str = "",
    ) -> TaskRecord:
        """直接启动（不做课程选择）：批量模式与命令行使用。"""
        record = self._create_record(
            username,
            password,
            display_name=display_name,
            course_ids=course_ids,
            speed=speed,
            jobs=jobs,
            submit=submit,
            cover_rate=cover_rate,
            work_max_retries=work_max_retries,
            use_cookies=use_cookies,
            cookie=cookie,
        )
        try:
            command, cwd, env = self._build_command(record)
            self._spawn(record, command, cwd, env)
        except Exception:
            self._discard(record)
            raise
        self._save_index()
        return record

    def prepare_task(
        self,
        username: str,
        password: str,
        *,
        display_name: str = "",
        speed: Optional[float] = None,
        jobs: Optional[int] = None,
        submit: Optional[bool] = None,
        cover_rate: Optional[float] = None,
        work_max_retries: Optional[int] = None,
        use_cookies: bool = False,
        cookie: str = "",
    ) -> tuple[TaskRecord, list[dict[str, Any]]]:
        """登录并读取课程列表，等待用户勾选后再真正开始刷课。"""
        record = self._create_record(
            username,
            password,
            display_name=display_name,
            course_ids=None,
            speed=speed,
            jobs=jobs,
            submit=submit,
            cover_rate=cover_rate,
            work_max_retries=work_max_retries,
            use_cookies=use_cookies,
            cookie=cookie,
        )
        try:
            ok, courses, error = self.courses_provider(record)
            if not ok:
                raise TaskError(error or "读取课程列表失败")
            record.courses = courses
            self._save_index()
            return record, courses
        except Exception:
            self._discard(record)
            raise

    def start_prepared_task(
        self,
        task_id: str,
        course_ids: Optional[list[str]] = None,
    ) -> TaskRecord:
        """课程选择完成后真正启动该任务（复用准备阶段保存的 Cookie）。"""
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise TaskError("任务不存在或已过期，请重新登录")
            if record.status != SELECTING_STATUS:
                raise TaskError(f"任务当前状态为 {record.status}，无法启动")
            if self.running_count() >= self.max_parallel:
                raise TaskError(
                    f"同时运行的任务已达上限（{self.max_parallel} 个），请先等待或停止部分任务"
                )

        selected = [str(item).strip() for item in (course_ids or []) if str(item).strip()]
        record.options["course_ids"] = selected
        try:
            self._update_task_config(record, selected)
            command, cwd, env = self._build_command(record)
            self._spawn(record, command, cwd, env)
        except Exception as exc:
            raise TaskError(f"启动失败: {exc}") from exc
        self._save_index()
        return record

    def _create_record(
        self,
        username: str,
        password: str,
        *,
        display_name: str = "",
        course_ids: Optional[list[str]] = None,
        speed: Optional[float] = None,
        jobs: Optional[int] = None,
        submit: Optional[bool] = None,
        cover_rate: Optional[float] = None,
        work_max_retries: Optional[int] = None,
        use_cookies: bool = False,
        cookie: str = "",
    ) -> TaskRecord:
        username = (username or "").strip()
        if not username:
            raise TaskError("请输入账号")
        if not password and not use_cookies and not cookie:
            raise TaskError("请输入密码")

        with self._lock:
            if self.running_count() >= self.max_parallel:
                raise TaskError(
                    f"同时运行的任务已达上限（{self.max_parallel} 个），请先等待或停止部分任务"
                )
            if self.selecting_count() >= MAX_SELECTING_TASKS:
                raise TaskError(
                    f"待选择课程的任务过多（{MAX_SELECTING_TASKS} 个），请先处理或等待其过期"
                )
            task_id = uuid.uuid4().hex[:12]
            account = sanitize_account_name(f"web-{task_id}")
            record = TaskRecord(
                id=task_id,
                account=account,
                username=username,
                display_name=(display_name or username).strip() or username,
                status=SELECTING_STATUS,
                options={
                    "course_ids": course_ids or [],
                    "speed": speed,
                    "jobs": jobs,
                    "submit": submit,
                    "cover_rate": cover_rate,
                    "work_max_retries": work_max_retries,
                    "use_cookies": use_cookies,
                },
            )
            self._tasks[task_id] = record

        try:
            config_file = self._write_task_config(
                record,
                password=password,
                course_ids=course_ids,
                speed=speed,
                jobs=jobs,
                submit=submit,
                cover_rate=cover_rate,
                work_max_retries=work_max_retries,
                use_cookies=use_cookies or bool(cookie),
                cookie=cookie,
            )
            record.config_file = str(config_file)
        except Exception:
            self._discard(record)
            raise

        self._save_index()
        return record

    def _discard(self, record: TaskRecord) -> None:
        """创建过程中失败时清理记录与目录。"""
        with self._lock:
            self._tasks.pop(record.id, None)
        shutil.rmtree(self.task_root / record.id, ignore_errors=True)
        shutil.rmtree(self.data_paths.account(record.account).root, ignore_errors=True)
        self._save_index()

    def _write_task_config(
        self,
        record: TaskRecord,
        *,
        password: str,
        course_ids: Optional[list[str]],
        speed: Optional[float],
        jobs: Optional[int],
        submit: Optional[bool],
        cover_rate: Optional[float],
        work_max_retries: Optional[int],
        use_cookies: bool,
        cookie: str,
    ) -> Path:
        """基于主配置生成该任务专属的 config.yaml（只含一个账号）。"""
        try:
            raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}

        task_dir = self.task_root / record.id
        task_dir.mkdir(parents=True, exist_ok=True)

        raw["server"] = dict(raw.get("server") or {})
        raw["server"]["data_dir"] = str(self.data_paths.root)
        raw["server"]["web_enabled"] = False

        account: dict[str, Any] = {
            "name": record.account,
            "username": record.username,
            "enabled": True,
            "use_cookies": bool(use_cookies),
            "schedule": "",
        }
        if not use_cookies:
            account["password"] = password

        study = dict(raw.get("study") or {})
        if course_ids:
            study["include_courses"] = [str(c) for c in course_ids]
        if speed is not None:
            study["speed"] = speed
        if jobs is not None:
            study["jobs"] = jobs
        if work_max_retries is not None:
            study["work_max_retries"] = work_max_retries
        if study:
            account["study"] = study

        answer = dict(raw.get("answer") or {})
        if submit is not None:
            answer["submit"] = bool(submit)
        if cover_rate is not None:
            answer["cover_rate"] = cover_rate
        if answer:
            account["answer"] = answer

        raw["accounts"] = [account]

        config_file = task_dir / "config.yaml"
        config_file.write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        try:
            os.chmod(config_file, 0o600)
        except OSError:
            pass

        # 账号运行目录（cookies.txt / cache.json / 日志）
        account_paths = self.data_paths.account(record.account).ensure()
        if cookie:
            account_paths.cookies.write_text(
                cookie.strip().rstrip(";"), encoding="utf-8"
            )

        record.log_file = str(task_dir / "run.log")
        return config_file

    def _build_command(self, record: TaskRecord) -> tuple[list[str], Path, dict[str, str]]:
        if self.command_builder is not None:
            return self.command_builder(record)
        command = [
            sys.executable,
            "-m",
            "server.job",
            "--config",
            record.config_file,
            "--account",
            record.account,
        ]
        return command, project_root(), self._child_env()

    def _child_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["TQDM_DISABLE"] = "1"
        return env

    def _default_courses_provider(
        self, record: TaskRecord
    ) -> tuple[bool, list[dict[str, Any]], str]:
        """调用 server.login_helper 子进程：登录 + 读取课程列表（不刷课）。"""
        command = [
            sys.executable,
            "-m",
            "server.login_helper",
            "--config",
            record.config_file,
            "--account",
            record.account,
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=str(project_root()),
                env=self._child_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=LOGIN_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return False, [], f"登录或读取课程超时（>{LOGIN_TIMEOUT_SECONDS}s），请稍后重试"
        except Exception as exc:  # noqa: BLE001
            return False, [], f"登录子进程启动失败: {exc}"

        payload = None
        for line in reversed((completed.stdout or "").splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    payload = None
                if payload is not None:
                    break

        if payload is None:
            tail = (completed.stderr or completed.stdout or "").strip().splitlines()
            detail = tail[-1][:200] if tail else f"退出码 {completed.returncode}"
            return False, [], f"读取课程失败：{detail}"
        if not payload.get("ok"):
            return False, [], str(payload.get("error") or "登录失败")

        courses: list[dict[str, Any]] = []
        for item in payload.get("courses") or []:
            if not isinstance(item, dict):
                continue
            courses.append(
                {
                    "course_id": str(item.get("course_id", "")),
                    "clazz_id": str(item.get("clazz_id", "")),
                    "title": str(item.get("title", "")),
                    "teacher": str(item.get("teacher", "")),
                }
            )
        return True, courses, ""

    def _update_task_config(self, record: TaskRecord, course_ids: list[str]) -> None:
        """把用户勾选的课程写回任务配置；准备阶段已拿到 Cookie 就复用它。"""
        path = Path(record.config_file)
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        accounts = raw.get("accounts") or []
        if accounts:
            account = dict(accounts[0])
            study = dict(account.get("study") or {})
            study["include_courses"] = list(course_ids)
            account["study"] = study
            if self.data_paths.account(record.account).cookies.is_file():
                account["use_cookies"] = True
            raw["accounts"] = [account]
        path.write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _spawn(self, record: TaskRecord, command: list[str], cwd: Path, env: dict[str, str]) -> None:
        Path(record.log_file).parent.mkdir(parents=True, exist_ok=True)
        handle = open(record.log_file, "w", encoding="utf-8", buffering=1)
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
            )
        except Exception:
            handle.close()
            raise

        with self._lock:
            self._processes[record.id] = process
            self._handles[record.id] = handle
            self._buffers[record.id] = deque(maxlen=MAX_BUFFER_LINES)
            record.status = "running"

        threading.Thread(
            target=self._pump_output,
            args=(record.id, process, handle),
            daemon=True,
            name=f"task-log-{record.id}",
        ).start()

    def _pump_output(self, task_id: str, process: subprocess.Popen, handle) -> None:
        buffer = self._buffers.get(task_id)
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    line = strip_ansi(line)
                    if buffer is not None:
                        buffer.append(line)
                    handle.write(line)
                    handle.flush()
        except (OSError, ValueError):
            pass
        finally:
            try:
                handle.close()
            except OSError:
                pass
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

            record = self.get_task(task_id)
            if record is not None:
                if record.status == "running":
                    record.status = "finished" if process.returncode == 0 else "failed"
                record.exit_code = process.returncode
                record.finished_at = record.finished_at or _now()
                record.summary = self._load_summary(record)
            with self._lock:
                self._processes.pop(task_id, None)
                self._handles.pop(task_id, None)
            self._save_index()

    def _load_summary(self, record: TaskRecord) -> dict[str, Any] | None:
        """读取任务最近一次运行的 JSON 报告，供界面展示。"""
        runs_dir = self.data_paths.account(record.account).runs_dir
        if not runs_dir.is_dir():
            return None
        reports = sorted(runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for path in reversed(reports):
            report = read_report(path)
            if not report:
                continue
            record.report_file = str(path)
            return {
                "status": report.get("status"),
                "message": report.get("message"),
                "chapters_total": report.get("chapters_total"),
                "chapters_finished": report.get("chapters_finished"),
                "chapters_failed": report.get("chapters_failed"),
                "answer_total": report.get("answer_total"),
                "answer_covered": report.get("answer_covered"),
                "duration_seconds": report.get("duration_seconds"),
                "courses": [
                    {
                        "title": course.get("title"),
                        "status": course.get("status"),
                        "chapters_total": course.get("chapters_total"),
                        "chapters_finished": course.get("chapters_finished"),
                    }
                    for course in (report.get("courses") or [])
                ],
            }
        return None

    # -------------------------------------------------------------- 生命周期
    def stop_task(self, task_id: str) -> tuple[bool, str]:
        with self._lock:
            record = self._tasks.get(task_id)
            process = self._processes.get(task_id)
        if record is None:
            return False, "任务不存在"
        if record.status != "running":
            return False, "任务未在运行"
        if process is None or process.poll() is not None:
            record.status = "stopped"
            record.finished_at = record.finished_at or _now()
            self._save_index()
            return True, "任务已结束"

        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

        record.status = "stopped"
        record.stop_reason = "用户手动停止"
        record.exit_code = process.returncode
        record.finished_at = _now()
        self._save_index()
        return True, "已停止"

    def delete_task(self, task_id: str) -> tuple[bool, str]:
        with self._lock:
            record = self._tasks.get(task_id)
        if record is None:
            return False, "任务不存在"
        if record.status == "running":
            return False, "任务正在运行，请先停止"

        task_dir = self.task_root / record.id
        if task_dir.is_dir():
            shutil.rmtree(task_dir, ignore_errors=True)
        account_dir = self.data_paths.account(record.account).root
        if account_dir.is_dir():
            shutil.rmtree(account_dir, ignore_errors=True)

        with self._lock:
            self._tasks.pop(task_id, None)
            self._buffers.pop(task_id, None)
        self._save_index()
        return True, "已删除"

    def shutdown(self) -> None:
        """Web 服务退出时终止仍在运行的任务。"""
        for task_id in list(self._processes):
            try:
                self.stop_task(task_id)
            except Exception:  # noqa: BLE001
                pass


def deepseek_status(config: ServerConfig) -> dict[str, Any]:
    """检查是否已配置可用的 AI（DeepSeek）答题 Key，用于界面提示。"""
    candidates = [config.answer] + [account.answer for account in config.accounts]
    for answer in candidates:
        for provider in answer.providers:
            if provider.normalized_type() not in {"AI", "SiliconFlow"}:
                continue
            options = provider.options or {}
            if options.get("key") or options.get("api_key"):
                return {
                    "configured": True,
                    "provider": provider.normalized_type(),
                    "model": str(options.get("model", "")),
                }
    return {"configured": False}
