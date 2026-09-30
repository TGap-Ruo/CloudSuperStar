# -*- coding: utf-8 -*-
"""SQLite 状态存储：运行记录与账号状态。

服务端需要回答「昨天跑完了吗 / 哪个账号连续失败 / 这次有没有超时」这类问题，
所以把每次运行的结果落库，供 ``status`` 命令和 Web 面板读取。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account          TEXT    NOT NULL,
    run_id           TEXT    NOT NULL,
    started_at       TEXT    NOT NULL,
    finished_at      TEXT,
    status           TEXT    NOT NULL,
    message          TEXT    DEFAULT '',
    courses_total    INTEGER DEFAULT 0,
    courses_finished INTEGER DEFAULT 0,
    chapters_total   INTEGER DEFAULT 0,
    chapters_failed  INTEGER DEFAULT 0,
    answer_total     INTEGER DEFAULT 0,
    answer_covered   INTEGER DEFAULT 0,
    duration_seconds REAL    DEFAULT 0,
    report_path      TEXT    DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_runs_account_started
    ON runs (account, started_at DESC);

CREATE TABLE IF NOT EXISTS account_state (
    account              TEXT PRIMARY KEY,
    last_run_at          TEXT,
    last_status          TEXT,
    last_message         TEXT,
    consecutive_failures INTEGER DEFAULT 0,
    cookies_updated_at   TEXT,
    updated_at           TEXT
);
"""

RUN_STATUSES = {"running", "success", "failed", "timeout", "skipped", "partial"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class RunRecord:
    id: int
    account: str
    run_id: str
    started_at: str
    finished_at: Optional[str]
    status: str
    message: str
    courses_total: int
    courses_finished: int
    chapters_total: int
    chapters_failed: int
    answer_total: int
    answer_covered: int
    duration_seconds: float
    report_path: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "RunRecord":
        return cls(
            id=row["id"],
            account=row["account"],
            run_id=row["run_id"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            status=row["status"],
            message=row["message"] or "",
            courses_total=row["courses_total"],
            courses_finished=row["courses_finished"],
            chapters_total=row["chapters_total"],
            chapters_failed=row["chapters_failed"],
            answer_total=row["answer_total"],
            answer_covered=row["answer_covered"],
            duration_seconds=row["duration_seconds"] or 0.0,
            report_path=row["report_path"] or "",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "account": self.account,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "message": self.message,
            "courses_total": self.courses_total,
            "courses_finished": self.courses_finished,
            "chapters_total": self.chapters_total,
            "chapters_failed": self.chapters_failed,
            "answer_total": self.answer_total,
            "answer_covered": self.answer_covered,
            "duration_seconds": round(self.duration_seconds, 1),
            "report_path": self.report_path,
        }


class Store:
    """轻量 SQLite 封装；连接按需创建，可跨线程使用。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # WAL + NORMAL：允许多个子进程同时读写运行记录而不互相阻塞
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ runs
    def start_run(self, account: str, run_id: str, *, started_at: str | None = None) -> int:
        started = started_at or utc_now()
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO runs (account, run_id, started_at, status) VALUES (?, ?, ?, 'running')",
                (account, run_id, started),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def finish_run(
        self,
        run_row_id: int,
        *,
        status: str,
        message: str = "",
        summary: dict[str, Any] | None = None,
        report_path: str = "",
        duration_seconds: float = 0.0,
        finished_at: str | None = None,
    ) -> None:
        if status not in RUN_STATUSES:
            status = "failed"
        summary = summary or {}
        with self._lock:
            self._conn.execute(
                """
                UPDATE runs SET
                    finished_at = ?, status = ?, message = ?,
                    courses_total = ?, courses_finished = ?,
                    chapters_total = ?, chapters_failed = ?,
                    answer_total = ?, answer_covered = ?,
                    duration_seconds = ?, report_path = ?
                WHERE id = ?
                """,
                (
                    finished_at or utc_now(),
                    status,
                    message[:2000],
                    int(summary.get("courses_total", 0) or 0),
                    int(summary.get("courses_finished", 0) or 0),
                    int(summary.get("chapters_total", 0) or 0),
                    int(summary.get("chapters_failed", 0) or 0),
                    int(summary.get("answer_total", 0) or 0),
                    int(summary.get("answer_covered", 0) or 0),
                    float(duration_seconds or 0.0),
                    report_path,
                    run_row_id,
                ),
            )
            self._conn.commit()

    def recent_runs(self, limit: int = 20, account: str | None = None) -> list[RunRecord]:
        with self._lock:
            if account:
                rows = self._conn.execute(
                    "SELECT * FROM runs WHERE account = ? ORDER BY id DESC LIMIT ?",
                    (account, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [RunRecord.from_row(row) for row in rows]

    def running_runs(self) -> list[RunRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE status = 'running' ORDER BY id DESC"
            ).fetchall()
        return [RunRecord.from_row(row) for row in rows]

    def run_row_id(self, run_id: str) -> int | None:
        """按 run_id 查找运行记录主键（调度器超时兜底时使用）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM runs WHERE run_id = ? ORDER BY id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return int(row["id"]) if row else None

    def mark_stale_running_as_failed(self, message: str = "服务重启，任务中断") -> int:
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE runs
                   SET status = 'failed', finished_at = ?, message = ?
                 WHERE status = 'running'
                """,
                (utc_now(), message),
            )
            self._conn.commit()
            return int(cursor.rowcount or 0)

    # ---------------------------------------------------------- account state
    def record_account_result(
        self,
        account: str,
        *,
        status: str,
        message: str = "",
        cookies_updated_at: str | None = None,
    ) -> None:
        now = utc_now()
        with self._lock:
            row = self._conn.execute(
                "SELECT consecutive_failures FROM account_state WHERE account = ?",
                (account,),
            ).fetchone()
            failures = int(row["consecutive_failures"]) if row else 0
            failures = 0 if status == "success" else failures + 1
            self._conn.execute(
                """
                INSERT INTO account_state
                    (account, last_run_at, last_status, last_message,
                     consecutive_failures, cookies_updated_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account) DO UPDATE SET
                    last_run_at = excluded.last_run_at,
                    last_status = excluded.last_status,
                    last_message = excluded.last_message,
                    consecutive_failures = excluded.consecutive_failures,
                    cookies_updated_at = COALESCE(excluded.cookies_updated_at,
                                                  account_state.cookies_updated_at),
                    updated_at = excluded.updated_at
                """,
                (
                    account,
                    now,
                    status,
                    message[:2000],
                    failures,
                    cookies_updated_at,
                    now,
                ),
            )
            self._conn.commit()

    def account_states(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM account_state ORDER BY account"
            ).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    def account_state(self, account: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM account_state WHERE account = ?", (account,)
            ).fetchone()
        return {key: row[key] for key in row.keys()} if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------- 通用 SQL（供其它模块复用）
    def executescript(self, script: str) -> None:
        """执行一段建表/索引脚本（其它模块自带表结构时使用）。"""
        with self._lock:
            self._conn.executescript(script)
            self._conn.commit()

    def execute(self, sql: str, params: tuple | dict = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def read_report(path: str | Path) -> dict[str, Any] | None:
    """读取一次运行的 JSON 报告，损坏时返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def iter_reports(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    reports = []
    for path in paths:
        report = read_report(path)
        if report is not None:
            reports.append(report)
    return reports
