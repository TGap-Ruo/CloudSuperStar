# -*- coding: utf-8 -*-
"""只登录并列出课程（供网页端"选择课程"弹窗使用）。

刻意做成独立子进程：登录 + 拉课程列表本身要几秒钟，跑在 Web 进程里会阻塞其它请求；
而且它复用与刷课任务完全相同的配置/账号目录，登录成功后 cookies.txt 就落在该任务
的账号目录里，后续正式刷课时可以直接复用，不必再登录一次。

用法::

    python -m server.login_helper --config <config.yaml> --account <账号名>

stdout 只输出一行 JSON（日志走 stderr / 账号日志文件）::

    {"ok": true, "courses": [{"course_id": "...", "clazz_id": "...", "title": "..."}]}
    {"ok": false, "error": "LoginError: 用户名或密码错误"}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

from server.config import load_config
from server.runner import setup_runtime_environment


def list_courses(config_path: str | Path, account_name: str) -> dict[str, Any]:
    config = load_config(Path(config_path).expanduser().resolve())
    account = config.account(account_name)
    paths = config.data_paths().account(account.name)
    setup_runtime_environment(paths, log_level=config.server.log_level)

    from chaoxing_core.base import Account as CoreAccount
    from chaoxing_core.base import Chaoxing
    from chaoxing_core.logger import logger

    try:
        chaoxing = Chaoxing(account=CoreAccount(account.username, account.password))
        state = chaoxing.login(login_with_cookies=account.use_cookies)
        if not state.get("status"):
            raise RuntimeError(state.get("msg") or "登录失败")

        courses = chaoxing.get_course_list() or []
        logger.info("共读取到 {} 门课程", len(courses))
        return {
            "ok": True,
            "courses": [
                {
                    "course_id": str(course.get("courseId", "")),
                    "clazz_id": str(course.get("clazzId", "")),
                    "title": str(course.get("title", "")),
                    "teacher": str(course.get("teacher", "")),
                }
                for course in courses
            ],
        }
    except Exception as exc:  # noqa: BLE001 - 统一转成 JSON 错误返回
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m server.login_helper",
        description="登录超星账号并列出课程（只读取，不刷课）",
    )
    parser.add_argument("--config", required=True, help="配置文件路径")
    parser.add_argument("--account", required=True, help="账号名")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    result = list_courses(args.config, args.account)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
