# -*- coding: utf-8 -*-
"""命令行入口：``python -m server.cli <命令>``。

常用命令::

    python -m server.cli init                 # 生成 config.yaml
    python -m server.cli check                # 校验配置（不发网络请求）
    python -m server.cli login -a 主账号       # 登录并保存 Cookie
    python -m server.cli courses -a 主账号     # 查看课程列表与课程ID
    python -m server.cli run -a 主账号         # 立即执行一次刷课
    python -m server.cli run --all            # 立即执行所有账号
    python -m server.cli serve                # 启动定时调度（常驻）
    python -m server.cli status               # 查看运行状态
    python -m server.cli web                  # 启动状态面板
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from server.config import ConfigError, ServerConfig, load_config
from server.paths import project_root
from server.runner import setup_runtime_environment
from server.store import Store

DEFAULT_CONFIG_NAME = "config.yaml"
TEMPLATE_NAME = "config.example.yaml"


def _default_prog() -> str:
    invoked = Path(sys.argv[0]).name
    return "chaoxing" if invoked == "chaoxing" else "python -m server.cli"


def _load(args: argparse.Namespace) -> tuple[ServerConfig, Path]:
    path = Path(args.config).expanduser().resolve()
    return load_config(path), path


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    if not rows:
        print("（无数据）")
        return
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("-" * len(line))
    for row in rows:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


# --------------------------------------------------------------------- commands
def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.config).expanduser().resolve()
    if target.exists() and not args.force:
        print(f"配置文件已存在: {target}（加 --force 覆盖）")
        return 1
    template = project_root() / TEMPLATE_NAME
    if not template.is_file():
        print(f"找不到模板文件: {template}", file=sys.stderr)
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template, target)
    print(f"已生成配置文件: {target}")
    print("请编辑其中的账号、题库（推荐先填 DeepSeek API Key）后再执行 check。")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    config, path = _load(args)
    print(f"配置文件: {path}")
    print(f"数据目录: {config.data_paths().root}")
    print(f"账号数量: {len(config.accounts)}（启用 {len(config.enabled_accounts())}）")

    rows = []
    for account in config.accounts:
        providers = ",".join(account.answer.provider_names) or "未配置（将随机作答）"
        rows.append(
            [
                account.name,
                "是" if account.enabled else "否",
                providers,
                account.schedule or "-",
                ",".join(account.study.include_courses) or "全部课程",
            ]
        )
    _print_table(["账号", "启用", "题库", "cron", "学习范围"], rows)
    print("\n配置校验通过 ✅")

    if args.login:
        for account in config.enabled_accounts():
            chaoxing, _ = _connect(config, account)
            if chaoxing is None:
                return 1
            print(f"账号 {account.name} 登录成功")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    config, _ = _load(args)
    account = config.account(args.account)
    paths = config.data_paths().account(account.name).ensure()

    if args.cookie:
        paths.cookies.write_text(args.cookie.strip().rstrip(";"), encoding="utf-8")
        print(f"已写入 Cookie: {paths.cookies}")

    setup_runtime_environment(paths, log_level=config.server.log_level)

    from chaoxing_core.base import Account as CoreAccount
    from chaoxing_core.base import Chaoxing

    chaoxing = Chaoxing(account=CoreAccount(account.username, account.password))
    use_cookies = bool(args.cookie) or account.use_cookies or not account.password
    state = chaoxing.login(login_with_cookies=use_cookies)

    if not state.get("status"):
        print(f"登录失败: {state.get('msg')}", file=sys.stderr)
        return 1

    print(f"登录成功: {account.name}")
    print(f"Cookie 已保存到 {paths.cookies}")
    return 0


def cmd_courses(args: argparse.Namespace) -> int:
    config, _ = _load(args)
    account = config.account(args.account)
    chaoxing, _ = _connect(config, account)
    if chaoxing is None:
        return 1

    courses = chaoxing.get_course_list()
    if args.json:
        print(json.dumps(courses, ensure_ascii=False, indent=2))
        return 0

    rows = [
        [
            str(course.get("courseId", "")),
            str(course.get("clazzId", "")),
            str(course.get("cpi", "")),
            str(course.get("title", "")),
        ]
        for course in courses
    ]
    _print_table(["课程ID", "班级ID", "cpi", "课程名"], rows)
    print(f"\n共 {len(courses)} 门课程；把需要的课程ID填入 config.yaml 的 study.include_courses 即可只刷指定课程。")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config, path = _load(args)
    accounts = (
        config.enabled_accounts()
        if args.all
        else [config.account(args.account)]
    )
    if not accounts:
        print("没有可运行的账号", file=sys.stderr)
        return 1

    from server.job import execute

    exit_code = 0
    for account in accounts:
        report = execute(
            config,
            account,
            course_ids=args.course,
            dry_run=args.dry_run,
            record=not args.no_record,
        )
        if args.json:
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(
                f"[{account.name}] {report.status}: {report.message} "
                f"（章节 {report.chapters_finished}/{report.chapters_total}，"
                f"答题命中 {report.answer_covered}/{report.answer_total}，"
                f"{report.duration_seconds / 60:.1f} 分钟）"
            )
        if report.status not in {"success", "partial"}:
            exit_code = 1
    return exit_code


def cmd_serve(args: argparse.Namespace) -> int:
    config, path = _load(args)
    from server.scheduler import serve

    if config.server.web_enabled:
        import threading

        from server.web import run_web

        threading.Thread(
            target=run_web,
            args=(path,),
            daemon=True,
            name="web-panel",
        ).start()
        print(
            f"状态面板: http://{config.server.web_host}:{config.server.web_port}/",
            file=sys.stderr,
        )

    return serve(path, run_on_start=args.run_on_start)


def cmd_web(args: argparse.Namespace) -> int:
    config, path = _load(args)
    from server.web import run_web

    host = args.host or config.server.web_host
    port = args.port or config.server.web_port
    token = args.token if args.token is not None else config.server.web_token
    suffix = f"?token={token}" if token else ""
    print(f"控制台启动: http://{host}:{port}/{suffix}")
    run_web(path, host=host, port=port, token=token)
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    """查看网页端提交的任务（数据来自 data/web_tasks/index.json）。"""
    config, path = _load(args)
    from server.tasks import TaskManager

    manager = TaskManager(path, config)
    rows = []
    for task in manager.list_tasks(limit=args.limit):
        summary = task.get("summary") or {}
        chapters = (
            f"{summary.get('chapters_finished', 0)}/{summary.get('chapters_total', 0)}"
            if summary.get("chapters_total")
            else "-"
        )
        answers = (
            f"{summary.get('answer_covered', 0)}/{summary.get('answer_total', 0)}"
            if summary.get("answer_total")
            else "-"
        )
        rows.append(
            [
                task["id"],
                task.get("display_name") or task.get("username", ""),
                task.get("status", ""),
                task.get("created_at", ""),
                chapters,
                answers,
                (task.get("summary") or {}).get("message", "")[:30],
            ]
        )
    _print_table(["任务ID", "账号", "状态", "创建时间", "章节", "答题", "信息"], rows)
    stats = manager.stats()
    print(
        f"\n运行中 {stats['running']} / 上限 {stats['max_parallel']}，"
        f"已完成 {stats['finished']}，失败 {stats['failed']}"
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config, _ = _load(args)
    from server.scheduler import describe_schedule

    store = Store(config.data_paths().db)
    try:
        states = {row["account"]: row for row in store.account_states()}
        rows = []
        for row in describe_schedule(config):
            state = states.get(str(row["account"]), {})
            rows.append(
                [
                    str(row["account"]),
                    str(row["enabled"]),
                    str(row["schedule"]),
                    str(state.get("last_status") or "-"),
                    str(state.get("last_run_at") or "-"),
                    str(state.get("consecutive_failures", 0)),
                ]
            )
        _print_table(["账号", "启用", "cron", "上次状态", "上次运行", "连续失败"], rows)

        print()
        recent = store.recent_runs(limit=args.limit, account=args.account)
        run_rows = [
            [
                str(run.id),
                run.account,
                run.started_at[:19].replace("T", " "),
                run.status,
                f"{run.chapters_finished}/{run.chapters_total}",
                f"{run.answer_covered}/{run.answer_total}",
                f"{run.duration_seconds / 60:.1f}m",
                run.message[:40],
            ]
            for run in recent
        ]
        _print_table(
            ["#", "账号", "开始时间(UTC)", "状态", "章节", "答题", "耗时", "信息"],
            run_rows,
        )
    finally:
        store.close()
    return 0


def _connect(config: ServerConfig, account):
    """按账号配置登录，返回 (Chaoxing 实例, 账号目录)。"""
    paths = config.data_paths().account(account.name).ensure()
    setup_runtime_environment(paths, log_level=config.server.log_level)

    from chaoxing_core.base import Account as CoreAccount
    from chaoxing_core.base import Chaoxing

    chaoxing = Chaoxing(account=CoreAccount(account.username, account.password))
    use_cookies = account.use_cookies or not account.password
    state = chaoxing.login(login_with_cookies=use_cookies)
    if not state.get("status"):
        print(f"账号 {account.name} 登录失败: {state.get('msg')}", file=sys.stderr)
        return None, paths
    return chaoxing, paths


# ----------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_default_prog(),
        description="超星学习通服务端自动化（刷课 / 答题 / 签到）",
    )
    parser.add_argument(
        "-c",
        "--config",
        default=str(project_root() / DEFAULT_CONFIG_NAME),
        help="配置文件路径（默认 ./config.yaml）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="生成 config.yaml")
    p_init.add_argument("--force", action="store_true", help="覆盖已存在的配置")
    p_init.set_defaults(func=cmd_init)

    p_check = sub.add_parser("check", help="校验配置")
    p_check.add_argument("--login", action="store_true", help="同时尝试登录所有启用账号")
    p_check.set_defaults(func=cmd_check)

    p_login = sub.add_parser("login", help="登录并保存 Cookie")
    p_login.add_argument("-a", "--account", required=True, help="账号名")
    p_login.add_argument("--cookie", default="", help="直接导入浏览器 Cookie 字符串")
    p_login.set_defaults(func=cmd_login)

    p_courses = sub.add_parser("courses", help="查看课程列表")
    p_courses.add_argument("-a", "--account", required=True, help="账号名")
    p_courses.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_courses.set_defaults(func=cmd_courses)

    p_run = sub.add_parser("run", help="立即执行一次刷课")
    group = p_run.add_mutually_exclusive_group(required=True)
    group.add_argument("-a", "--account", help="账号名")
    group.add_argument("--all", action="store_true", help="执行所有启用账号")
    p_run.add_argument("--course", action="append", default=None, help="只处理指定课程ID，可重复")
    p_run.add_argument("--dry-run", action="store_true", help="只登录并读取课程，不执行任务")
    p_run.add_argument("--json", action="store_true", help="以 JSON 输出运行报告")
    p_run.add_argument("--no-record", action="store_true", help="不写入状态库")
    p_run.set_defaults(func=cmd_run)

    p_serve = sub.add_parser("serve", help="启动定时调度（常驻）")
    p_serve.add_argument("--run-on-start", action="store_true", help="启动时先跑一轮")
    p_serve.set_defaults(func=cmd_serve)

    p_web = sub.add_parser("web", help="启动状态面板")
    p_web.add_argument("--host", default=None)
    p_web.add_argument("--port", type=int, default=None)
    p_web.add_argument("--token", default=None, help="访问令牌（默认取配置里的 web_token）")
    p_web.set_defaults(func=cmd_web)

    p_tasks = sub.add_parser("tasks", help="查看网页端提交的任务")
    p_tasks.add_argument("--limit", type=int, default=30)
    p_tasks.set_defaults(func=cmd_tasks)

    p_status = sub.add_parser("status", help="查看运行状态")
    p_status.add_argument("-a", "--account", default=None, help="只看某个账号")
    p_status.add_argument("--limit", type=int, default=10, help="展示最近 N 条运行记录")
    p_status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
