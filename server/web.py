# -*- coding: utf-8 -*-
"""网页控制台：填写账号密码 → 并行刷课 → 实时日志。

页面（``server/templates/index.html``，单页应用，无需前端构建）提供：

* 单个账号 / 批量账号（每行 ``账号,密码``）并行启动
* 可选参数：课程 ID、倍速、并发章节数、是否自动提交、覆盖率、章节检测重做次数
* 任务列表（运行中 / 已完成 / 失败 / 已停止）与停止、删除、日志下载
* SSE 实时终端输出
* 定时账号与历史运行记录（读 SQLite）

安全：若配置了 ``server.web_token``，除 ``/healthz`` 外的所有接口都需要携带
token（``?token=`` / ``Authorization: Bearer`` / ``X-Token`` 任一）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
)

from server.config import ServerConfig, load_config
from server.scheduler import describe_schedule
from server.store import Store, read_report
from server.tasks import TaskError, TaskManager, deepseek_status

logger = logging.getLogger("chaoxing.web")

STATUS_TEXT = {
    "success": "完成",
    "partial": "部分完成",
    "failed": "失败",
    "timeout": "超时",
    "running": "运行中",
    "skipped": "已跳过",
}


def _token_from_request() -> str:
    token = request.args.get("token") or ""
    if not token:
        header = request.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
    if not token:
        token = request.headers.get("X-Token", "")
    return token


def _parse_accounts(payload: dict[str, Any]) -> list[dict[str, str]]:
    """支持三种入参：单条 username/password、accounts 数组、accounts_text 多行文本。"""
    accounts: list[dict[str, str]] = []

    username = str(payload.get("username", "") or "").strip()
    password = str(payload.get("password", "") or "")
    if username:
        accounts.append({"username": username, "password": password})

    raw_list = payload.get("accounts")
    if isinstance(raw_list, list):
        for item in raw_list:
            if isinstance(item, dict):
                accounts.append(
                    {
                        "username": str(item.get("username", "") or "").strip(),
                        "password": str(item.get("password", "") or ""),
                    }
                )
            elif isinstance(item, str):
                accounts.extend(_split_account_line(item))

    text = payload.get("accounts_text")
    if isinstance(text, str):
        for line in text.splitlines():
            accounts.extend(_split_account_line(line))

    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for account in accounts:
        if not account["username"] or account["username"] in seen:
            continue
        seen.add(account["username"])
        unique.append(account)
    return unique


def _split_account_line(line: str) -> list[dict[str, str]]:
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return []
    for separator in (",", "，", "\t", "|", " "):
        if separator in line:
            parts = [part.strip() for part in line.split(separator, 1)]
            if len(parts) == 2 and parts[0]:
                return [{"username": parts[0], "password": parts[1]}]
    return [{"username": line, "password": ""}]


def _parse_course_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = value
    else:
        text = str(value).replace("，", ",")
        items = text.split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def _optional_float(payload: dict[str, Any], key: str, low: float, high: float) -> Optional[float]:
    value = payload.get(key)
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(low, min(high, number))


def _optional_int(payload: dict[str, Any], key: str, low: int, high: int) -> Optional[int]:
    value = payload.get(key)
    if value in (None, ""):
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return max(low, min(high, number))


def create_app(
    config_path: str | os.PathLike,
    *,
    token: str | None = None,
    task_manager: TaskManager | None = None,
) -> Flask:
    config_file = Path(config_path).expanduser().resolve()
    config: ServerConfig = load_config(config_file)
    if token is not None:
        config.server.web_token = token

    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    app.config["JSON_AS_ASCII"] = False

    manager = task_manager or TaskManager(config_file, config)

    # ------------------------------------------------------------------ 鉴权
    def _require_token() -> None:
        expected = config.server.web_token
        if not expected:
            return
        if _token_from_request() != expected:
            abort(401)

    @app.before_request
    def _check_token() -> None:
        if request.path in {"/healthz"}:
            return
        if request.path.startswith("/static/") or request.path == "/":
            # 页面本身允许加载（前端会引导输入 token）
            return
        _require_token()

    @app.errorhandler(401)
    def _unauthorized(_error):
        return jsonify({"error": "访问令牌无效或缺失", "need_token": True}), 401

    # ------------------------------------------------------------------ 页面
    @app.get("/")
    def index() -> str:
        return render_template(
            "index.html",
            has_token="1" if config.server.web_token else "",
            max_parallel=config.server.web_max_parallel_tasks,
        )

    @app.get("/healthz")
    def healthz() -> Response:
        return Response("ok", mimetype="text/plain")

    # ------------------------------------------------------------------ 元信息
    @app.get("/api/meta")
    def api_meta() -> Response:
        return jsonify(
            {
                "data_dir": str(config.data_paths().root),
                "timezone": config.server.timezone,
                "max_parallel": config.server.web_max_parallel_tasks,
                "deepseek": deepseek_status(config),
                "stats": manager.stats(),
                "scheduled_accounts": len(config.enabled_accounts()),
                "version": _version(),
            }
        )

    # ------------------------------------------------------------------ 任务
    @app.get("/api/tasks")
    def api_list_tasks() -> Response:
        limit = min(500, max(1, request.args.get("limit", 100, type=int)))
        return jsonify({"tasks": manager.list_tasks(limit), "stats": manager.stats()})

    @app.get("/api/tasks/<task_id>")
    def api_get_task(task_id: str) -> Response:
        record = manager.get_task(task_id)
        if record is None:
            return jsonify({"error": "任务不存在"}), 404
        return jsonify(record.to_dict())

    @app.post("/api/tasks")
    def api_start_tasks() -> Response:
        payload = request.get_json(silent=True) or {}
        accounts = _parse_accounts(payload)
        if not accounts:
            return jsonify({"error": "请至少输入一个账号"}), 400

        course_ids = _parse_course_ids(
            payload.get("course_ids") or payload.get("courses")
        )
        speed = _optional_float(payload, "speed", 1.0, 2.0)
        jobs = _optional_int(payload, "jobs", 1, 16)
        work_max_retries = _optional_int(payload, "work_max_retries", 0, 10)
        cover_rate = _optional_float(payload, "cover_rate", 0.0, 1.0)
        submit_raw = payload.get("submit")
        submit = None if submit_raw is None else bool(submit_raw)
        use_cookies = bool(payload.get("use_cookies"))
        cookie = str(payload.get("cookie", "") or "")
        display_name = str(payload.get("display_name", "") or "").strip()

        started: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for account in accounts:
            try:
                record = manager.start_task(
                    account["username"],
                    account["password"],
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
                started.append(record.to_dict())
            except TaskError as exc:
                errors.append({"username": account["username"], "error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                logger.exception("启动任务失败")
                errors.append({"username": account["username"], "error": f"启动失败: {exc}"})

        if not started and errors:
            return jsonify({"error": errors[0]["error"], "errors": errors}), 400
        return jsonify({"started": started, "errors": errors, "stats": manager.stats()}), 201

    @app.post("/api/courses")
    def api_prepare_courses() -> Response:
        """登录并读取课程列表（不刷课），返回 task_id 供勾选后启动。"""
        payload = request.get_json(silent=True) or {}
        accounts = _parse_accounts(payload)
        if len(accounts) != 1:
            return jsonify({"error": "读取课程列表请只填一个账号（批量模式默认刷全部课程）"}), 400

        account = accounts[0]
        try:
            record, courses = manager.prepare_task(
                account["username"],
                account["password"],
                speed=_optional_float(payload, "speed", 1.0, 2.0),
                jobs=_optional_int(payload, "jobs", 1, 16),
                submit=None if payload.get("submit") is None else bool(payload.get("submit")),
                cover_rate=_optional_float(payload, "cover_rate", 0.0, 1.0),
                work_max_retries=_optional_int(payload, "work_max_retries", 0, 10),
                use_cookies=bool(payload.get("use_cookies")),
                cookie=str(payload.get("cookie", "") or ""),
            )
        except TaskError as exc:
            return jsonify({"error": str(exc), "login_failed": True}), 400
        except Exception as exc:  # noqa: BLE001
            logger.exception("读取课程列表失败")
            return jsonify({"error": f"读取课程列表失败: {exc}"}), 500

        return jsonify(
            {
                "task_id": record.id,
                "username": record.username,
                "courses": courses,
                "count": len(courses),
            }
        )

    @app.post("/api/tasks/<task_id>/start")
    def api_start_prepared(task_id: str) -> Response:
        """课程勾选完成后启动任务。course_ids 为空表示刷全部课程。"""
        payload = request.get_json(silent=True) or {}
        course_ids = _parse_course_ids(payload.get("course_ids"))
        try:
            record = manager.start_prepared_task(task_id, course_ids)
        except TaskError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"started": record.to_dict(), "stats": manager.stats()})

    @app.post("/api/tasks/<task_id>/stop")
    def api_stop_task(task_id: str) -> Response:
        ok, message = manager.stop_task(task_id)
        if not ok:
            return jsonify({"error": message}), 400
        return jsonify({"status": "stopped", "message": message})

    @app.delete("/api/tasks/<task_id>")
    def api_delete_task(task_id: str) -> Response:
        ok, message = manager.delete_task(task_id)
        if not ok:
            return jsonify({"error": message}), 400
        return jsonify({"status": "deleted", "message": message})

    @app.get("/api/tasks/<task_id>/output")
    def api_task_output(task_id: str) -> Response:
        start_line = max(0, request.args.get("from", 0, type=int))
        output, total = manager.get_output(task_id, start_line)
        return jsonify({"output": output, "total_lines": total, "from": start_line})

    @app.get("/api/tasks/<task_id>/log")
    def api_task_log(task_id: str) -> Response:
        path = manager.log_path(task_id)
        record = manager.get_task(task_id)
        if record is None or path is None:
            return jsonify({"error": "日志文件不存在"}), 404
        filename = f"chaoxing_{record.username}_{task_id}.log"
        return send_file(path, as_attachment=True, download_name=filename)

    @app.get("/api/stream/<task_id>")
    def api_stream(task_id: str) -> Response:
        """SSE 实时输出：先把已有内容推完，再持续推送增量。"""

        def generate():
            last_line = 0
            output, total = manager.get_output(task_id, 0)
            if output:
                yield _sse(output)
                last_line = total

            idle = 0
            while True:
                record = manager.get_task(task_id)
                if record is None:
                    yield "event: error\ndata: 任务不存在\n\n"
                    return

                output, total = manager.get_output(task_id, last_line)
                if output:
                    yield _sse(output)
                    last_line = total
                    idle = 0
                else:
                    idle += 1
                    if idle % 30 == 0:  # 约 15 秒发一次心跳，避免代理断开
                        yield ": keep-alive\n\n"

                if record.status != "running":
                    output, total = manager.get_output(task_id, last_line)
                    if output:
                        yield _sse(output)
                    payload = json.dumps(
                        {
                            "status": record.status,
                            "exit_code": record.exit_code,
                            "summary": record.summary,
                            "stop_reason": record.stop_reason,
                        },
                        ensure_ascii=False,
                    )
                    yield f"event: end\ndata: {payload}\n\n"
                    return
                time.sleep(0.5)

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ------------------------------------------------------------------ 状态
    @app.get("/api/status")
    def api_status() -> Response:
        store = Store(config.data_paths().db)
        try:
            states = {row["account"]: row for row in store.account_states()}
            runs = [record.to_dict() for record in store.recent_runs(limit=30)]
        finally:
            store.close()

        accounts = []
        for row in describe_schedule(config):
            state = states.get(str(row["account"]), {})
            accounts.append(
                {
                    **row,
                    "last_status": state.get("last_status") or "-",
                    "last_run_at": state.get("last_run_at") or "-",
                    "consecutive_failures": state.get("consecutive_failures", 0),
                }
            )
        return jsonify({"accounts": accounts, "runs": runs, "stats": manager.stats()})

    @app.get("/api/runs/<int:run_id>/report")
    def api_run_report(run_id: int) -> Response:
        store = Store(config.data_paths().db)
        try:
            runs = [r for r in store.recent_runs(limit=500) if r.id == run_id]
        finally:
            store.close()
        if not runs:
            return jsonify({"error": "运行记录不存在"}), 404
        report = read_report(runs[0].report_path) if runs[0].report_path else None
        if report is None:
            return jsonify({"error": "报告文件不存在"}), 404
        return jsonify(report)

    # 暴露给测试与 CLI
    app.extensions["task_manager"] = manager
    app.extensions["chaoxing_config"] = config
    return app


def _sse(text: str) -> str:
    """把多行文本编码成一条 SSE 事件（浏览器会把多个 data 字段拼回换行）。"""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    body = "".join(f"data: {line}\n" for line in lines)
    return body + "\n"


def _version() -> str:
    try:
        from server import __version__

        return __version__
    except Exception:  # noqa: BLE001
        return "unknown"


def run_web(
    config_path: str | os.PathLike,
    *,
    host: str | None = None,
    port: int | None = None,
    token: str | None = None,
) -> None:
    config = load_config(Path(config_path).expanduser().resolve())
    app = create_app(config_path, token=token)
    app.run(
        host=host or config.server.web_host,
        port=port or config.server.web_port,
        debug=False,
        threaded=True,
    )
