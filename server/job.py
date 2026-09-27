# -*- coding: utf-8 -*-
"""单账号一次性运行的入口（供调度器子进程与 CLI 共用）。

调度器以子进程方式调用本模块，好处是：

* 单个账号卡死/占满内存不会影响其它账号，超时可以直接 kill；
* 核心代码里的全局状态（Cookie 路径、题库缓存、会话单例）天然按进程隔离；
* 账号目录可以安全地作为工作目录，``cookies.txt`` / ``cache.json`` 不会串号。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from server.config import AccountConfig, ServerConfig, load_config
from server.paths import DataPaths, load_data_paths
from server.runner import RunReport, new_run_id, run_and_notify
from server.store import Store


def execute(
    config: ServerConfig,
    account: AccountConfig,
    *,
    course_ids: Optional[list[str]] = None,
    dry_run: bool = False,
    data_paths: DataPaths | None = None,
    record: bool = True,
    run_id: Optional[str] = None,
) -> RunReport:
    """运行一个账号并把结果写入 SQLite。"""
    paths = data_paths or config.data_paths()
    store = Store(paths.db) if record else None
    run_id = run_id or new_run_id()
    run_row_id = store.start_run(account.name, run_id) if store else 0

    try:
        report = run_and_notify(
            config,
            account,
            course_ids=course_ids,
            dry_run=dry_run,
            data_paths=paths,
            run_id=run_id,
        )
    except Exception as exc:  # noqa: BLE001 - 兜底，保证状态一定被写回
        report = RunReport(
            account=account.name,
            run_id="unknown",
            started_at="",
            status="failed",
            message=f"{type(exc).__name__}: {exc}",
        )

    if store:
        store.finish_run(
            run_row_id,
            status=report.status,
            message=report.message,
            summary=report.summary(),
            report_path=str(paths.account(account.name).run_report(report.run_id)),
            duration_seconds=report.duration_seconds,
            finished_at=report.finished_at or None,
        )
        store.record_account_result(
            account.name,
            status=report.status,
            message=report.message,
        )
        store.close()

    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m server.job",
        description="执行单个超星账号的一次刷课/答题",
    )
    parser.add_argument("--config", required=True, help="config.yaml 路径")
    parser.add_argument("--account", required=True, help="账号名")
    parser.add_argument("--course", action="append", default=None, help="只处理指定课程ID，可重复")
    parser.add_argument("--dry-run", action="store_true", help="只登录并读取课程，不执行任务")
    parser.add_argument("--json", action="store_true", help="把运行报告打印到 stdout（JSON）")
    parser.add_argument("--no-record", action="store_true", help="不写入 SQLite 状态库")
    parser.add_argument("--run-id", default=None, help="指定 run_id（调度器超时兜底用）")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    account = config.account(args.account)
    data_paths = load_data_paths(config.server.data_dir or None)

    report = execute(
        config,
        account,
        course_ids=args.course,
        dry_run=args.dry_run,
        data_paths=data_paths,
        record=not args.no_record,
        run_id=args.run_id,
    )

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            f"[{account.name}] {report.status}: {report.message} "
            f"({report.duration_seconds:.0f}s)",
            file=sys.stderr,
        )

    return 0 if report.status in {"success", "partial"} else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
