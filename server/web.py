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
import secrets
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from server.admin import create_admin_blueprint
from server.auth import AuthError, AuthManager
from server.config import ServerConfig, load_config
from server.scheduler import describe_schedule
from server.store import Store, read_report
from server.tasks import TaskError, TaskManager, deepseek_status
from server.usage import UsageRecorder, format_cost

logger = logging.getLogger("chaoxing.web")


def _load_or_create_secret(path: Path) -> str:
    """会话签名密钥：首次生成后落盘（0600），保证重启后登录态不失效。"""
    try:
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = secrets.token_urlsafe(48)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return value
    except OSError:
        return secrets.token_urlsafe(48)

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


_LOGIN_FAILURES: dict[str, list[float]] = {}
_MAX_LOGIN_FAILURES = 8
_LOGIN_FAILURE_WINDOW = 300  # 秒


def _client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _is_login_blocked(ip: str) -> bool:
    now = time.time()
    records = [t for t in _LOGIN_FAILURES.get(ip, []) if now - t < _LOGIN_FAILURE_WINDOW]
    _LOGIN_FAILURES[ip] = records
    return len(records) >= _MAX_LOGIN_FAILURES


def _record_login_failure(ip: str) -> None:
    _LOGIN_FAILURES.setdefault(ip, []).append(time.time())


def _clear_login_failures(ip: str) -> None:
    _LOGIN_FAILURES.pop(ip, None)


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
    auth_manager: "AuthManager | None" = None,
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
    data_paths = config.data_paths()
    app.secret_key = _load_or_create_secret(data_paths.root / ".session_secret")
    app.permanent_session_lifetime = timedelta(minutes=config.server.session_timeout_minutes)

    auth = auth_manager or AuthManager(data_paths.db)
    initial_admin_password = ""
    try:
        created, initial_admin_password = auth.ensure_bootstrap_admin(
            config.server.admin_user, config.server.admin_password
        )
        if created:
            (data_paths.root / "initial_admin_password.txt").write_text(
                f"用户名：{config.server.admin_user}\n密码：{initial_admin_password}\n"
                f"（登录后请立即在后台修改；本文件可直接删除）\n",
                encoding="utf-8",
            )
    except Exception:  # noqa: BLE001 - 鉴权初始化失败不阻塞服务
        logger.exception("初始化管理员失败")

    if initial_admin_password:
        logger.warning(
            "已创建初始管理员 %s，初始密码：%s（也写入了 %s）",
            config.server.admin_user,
            initial_admin_password,
            data_paths.root / "initial_admin_password.txt",
        )

    usage = UsageRecorder(data_paths.db)

    def _refund_on_finish(record) -> None:
        """任务最终失败时退还额度（可配置关闭）。"""
        if not config.server.refund_on_failure:
            return
        if record.status in {"failed", "timeout"}:
            try:
                if auth.refund(record.id):
                    auth.log("refund", actor=record.owner or "system", target=record.id,
                             detail=f"任务 {record.status}，自动退还额度")
            except Exception:  # noqa: BLE001
                logger.exception("退还额度失败")

    manager = task_manager or TaskManager(config_file, config, on_finish=_refund_on_finish)
    if task_manager is not None:
        # 注入的 manager（测试/自定义）也要挂上退款回调，保证额度语义一致
        previous_on_finish = manager.on_finish

        def _composed_on_finish(record) -> None:
            if previous_on_finish is not None:
                try:
                    previous_on_finish(record)
                except Exception:  # noqa: BLE001
                    logger.exception("自定义任务结束回调异常")
            _refund_on_finish(record)

        manager.on_finish = _composed_on_finish

    # ------------------------------------------------------------------ 鉴权
    def current_user() -> dict[str, Any] | None:
        """识别当前身份：会话 → API Key → 兼容旧的 web_token（视为管理员）。"""
        username = session.get("username")
        if username:
            user = auth.get_user(str(username))
            if user and user.get("enabled"):
                return user
            session.clear()

        api_key = request.headers.get("X-Api-Key", "")
        if api_key:
            user = auth.verify_api_key(api_key)
            if user and user.get("enabled"):
                return user

        expected = config.server.web_token
        if expected and _token_from_request() == expected:
            admin = auth.get_user(config.server.admin_user)
            if admin and admin.get("enabled"):
                return admin
            return {
                "username": "token-admin",
                "role": "admin",
                "enabled": True,
                "credits": -1,
                "credits_label": "不限",
                "unlimited": True,
            }
        return None

    def require_user(role: str | None = None):
        """接口鉴权：返回 None 表示通过，否则返回可直接返回给前端的错误响应。"""
        user = current_user()
        if user is None:
            # 配了访问令牌就按令牌校验（兼容旧部署），否则按是否开启登录鉴权决定
            if config.server.web_token:
                return jsonify({"error": "访问令牌无效或缺失", "need_token": True}), 401
            if config.server.auth_enabled:
                return jsonify({"error": "未登录或登录已过期", "need_login": True}), 401
            return None
        if role == "admin" and user.get("role") != "admin":
            return jsonify({"error": "需要管理员权限"}), 403
        return None

    def quota_guard(user: dict[str, Any] | None, needed: int = 1):
        if not config.server.auth_enabled or not user:
            return None
        status = auth.quota_status(str(user.get("username", "")))
        if not status.get("ok"):
            return jsonify({"error": status.get("reason", "额度不足"), "quota": status}), 402
        return None

    def consume_credit(user: dict[str, Any] | None, task_id: str) -> str | None:
        """扣减额度；成功返回 None，失败返回错误信息。"""
        if not config.server.auth_enabled or not user:
            return None
        try:
            auth.consume(
                str(user.get("username", "")),
                task_id,
                cost=config.server.credits_per_task,
                detail="启动刷课任务",
            )
            auth.log("consume", actor=str(user.get("username", "")), target=task_id, ip=_client_ip())
            return None
        except AuthError as exc:
            return str(exc)

    def _task_access(task_id: str):
        """任务访问控制：返回 (record, 错误响应)。普通用户只能看自己的任务。"""
        denied = require_user()
        if denied:
            return None, denied
        user = current_user()
        record = manager.get_task(task_id)
        if record is None:
            return None, (jsonify({"error": "任务不存在"}), 404)
        if user and user.get("role") != "admin" and record.owner and record.owner != user.get("username"):
            return None, (jsonify({"error": "无权访问该任务"}), 403)
        return record, None

    @app.errorhandler(401)
    def _unauthorized(_error):
        return jsonify({"error": "未登录或登录已过期", "need_login": True}), 401

    # ------------------------------------------------------------- 登录 / 登出
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            return render_template(
                "login.html",
                error=None,
                admin_path=config.server.admin_path,
                auth_enabled=config.server.auth_enabled,
            )
        payload = request.get_json(silent=True) or request.form
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        ip = _client_ip()
        if _is_login_blocked(ip):
            return jsonify({"error": "登录失败次数过多，请 5 分钟后再试"}), 429
        user = auth.verify_login(username, password)
        if not user:
            _record_login_failure(ip)
            auth.log("login_failed", actor=username, ip=ip)
            if request.is_json:
                return jsonify({"error": "用户名或密码错误"}), 401
            return render_template(
                "login.html",
                error="用户名或密码错误",
                admin_path=config.server.admin_path,
                auth_enabled=config.server.auth_enabled,
            ), 401
        _clear_login_failures(ip)
        session.permanent = True
        session["username"] = user["username"]
        auth.log("login", actor=user["username"], ip=ip)
        if request.is_json:
            return jsonify({"ok": True, "user": user})
        return redirect(url_for("index"))

    @app.get("/logout")
    def logout():
        auth.log("logout", actor=session.get("username", ""), ip=_client_ip())
        session.clear()
        return redirect(url_for("login"))

    # ------------------------------------------------------------------ 页面
    @app.get("/")
    def index() -> str:
        user = current_user()
        if user is None and config.server.auth_enabled:
            return redirect(url_for("login"))
        quota = auth.quota_status(str(user.get("username", ""))) if user else {"ok": True}
        return render_template(
            "index.html",
            has_token="1" if config.server.web_token else "",
            max_parallel=config.server.web_max_parallel_tasks,
            user=user or {},
            quota=quota,
            admin_path=config.server.admin_path,
            auth_enabled=config.server.auth_enabled,
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
        denied = require_user()
        if denied:
            return denied
        user = current_user()
        limit = min(500, max(1, request.args.get("limit", 100, type=int)))
        tasks = manager.list_tasks(limit)
        if user and user.get("role") != "admin":
            tasks = [item for item in tasks if item.get("owner") == user.get("username")]
        costs = usage.by_task()
        for task in tasks:
            stat = costs.get(str(task.get("id")), {})
            task["usage"] = {
                "calls": int(stat.get("calls", 0) or 0),
                "total_tokens": int(stat.get("total_tokens", 0) or 0),
                "cost": float(stat.get("cost", 0) or 0),
                "cost_display": format_cost(float(stat.get("cost", 0) or 0)),
            }
        return jsonify({"tasks": tasks, "stats": manager.stats()})

    @app.get("/api/me")
    def api_me() -> Response:
        user = current_user()
        if user is None:
            return jsonify({"error": "未登录", "need_login": True}), 401
        quota = auth.quota_status(str(user.get("username", "")))
        return jsonify(
            {
                "user": user,
                "quota": quota,
                "auth_enabled": config.server.auth_enabled,
                "is_admin": user.get("role") == "admin",
                "admin_path": config.server.admin_path,
                "usage": usage.totals(user=str(user.get("username", ""))),
            }
        )

    @app.post("/api/redeem")
    def api_redeem() -> Response:
        user = current_user()
        if user is None:
            return jsonify({"error": "未登录", "need_login": True}), 401
        payload = request.get_json(silent=True) or {}
        code = str(payload.get("code", "")).strip()
        if not code:
            return jsonify({"error": "请输入卡密"}), 400
        try:
            result = auth.redeem_code(code, str(user.get("username", "")))
        except AuthError as exc:
            auth.log("redeem_failed", actor=str(user.get("username", "")), target=code, ip=_client_ip())
            return jsonify({"error": str(exc)}), 400
        auth.log("redeem", actor=str(user.get("username", "")), target=result["code"],
                 detail=f"+{result['credits']} 次", ip=_client_ip())
        return jsonify(
            {
                "ok": True,
                "credits": result["credits"],
                "user": result["user"],
                "quota": auth.quota_status(str(user.get("username", ""))),
            }
        )

    @app.get("/api/tasks/<task_id>")
    def api_get_task(task_id: str) -> Response:
        record, error = _task_access(task_id)
        if error:
            return error
        return jsonify(record.to_dict())

    @app.post("/api/tasks")
    def api_start_tasks() -> Response:
        payload = request.get_json(silent=True) or {}
        accounts = _parse_accounts(payload)
        if not accounts:
            return jsonify({"error": "请至少输入一个账号"}), 400

        user = current_user()
        denied = quota_guard(user)
        if denied:
            return denied

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
                    owner=str(user.get("username", "")) if user else "",
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
                credit_error = consume_credit(user, record.id)
                if credit_error:
                    manager.stop_task(record.id)
                    manager.delete_task(record.id)
                    errors.append({"username": account["username"], "error": credit_error})
                    continue
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

        user = current_user()
        denied = require_user()
        if denied:
            return denied
        denied = quota_guard(user)
        if denied:
            return denied

        account = accounts[0]
        try:
            record, courses = manager.prepare_task(
                account["username"],
                account["password"],
                owner=str(user.get("username", "")) if user else "",
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
        user = current_user()
        denied = require_user()
        if denied:
            return denied
        denied = quota_guard(user)
        if denied:
            return denied
        try:
            record = manager.start_prepared_task(task_id, course_ids)
        except TaskError as exc:
            return jsonify({"error": str(exc)}), 400
        credit_error = consume_credit(user, record.id)
        if credit_error:
            manager.stop_task(record.id)
            manager.delete_task(record.id)
            return jsonify({"error": credit_error}), 402
        return jsonify({"started": record.to_dict(), "stats": manager.stats()})

    @app.post("/api/tasks/<task_id>/stop")
    def api_stop_task(task_id: str) -> Response:
        _, error = _task_access(task_id)
        if error:
            return error
        ok, message = manager.stop_task(task_id)
        if not ok:
            return jsonify({"error": message}), 400
        return jsonify({"status": "stopped", "message": message})

    @app.delete("/api/tasks/<task_id>")
    def api_delete_task(task_id: str) -> Response:
        _, error = _task_access(task_id)
        if error:
            return error
        ok, message = manager.delete_task(task_id)
        if not ok:
            return jsonify({"error": message}), 400
        return jsonify({"status": "deleted", "message": message})

    @app.get("/api/tasks/<task_id>/output")
    def api_task_output(task_id: str) -> Response:
        _, error = _task_access(task_id)
        if error:
            return error
        start_line = max(0, request.args.get("from", 0, type=int))
        output, total = manager.get_output(task_id, start_line)
        return jsonify({"output": output, "total_lines": total, "from": start_line})

    @app.get("/api/tasks/<task_id>/log")
    def api_task_log(task_id: str) -> Response:
        path = manager.log_path(task_id)
        record = manager.get_task(task_id)
        if record is None or path is None:
            return jsonify({"error": "日志文件不存在"}), 404
        _, error = _task_access(task_id)
        if error:
            return error
        filename = f"chaoxing_{record.username}_{task_id}.log"
        return send_file(path, as_attachment=True, download_name=filename)

    @app.get("/api/stream/<task_id>")
    def api_stream(task_id: str) -> Response:
        """SSE 实时输出：先把已有内容推完，再持续推送增量。"""
        _, error = _task_access(task_id)
        if error:
            return error

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
    app.extensions["auth"] = auth
    app.extensions["usage"] = usage

    # ------------------------------------------------------------ 管理后台
    admin_blueprint = create_admin_blueprint(
        config,
        config_file,
        auth=auth,
        manager=manager,
        usage=usage,
        current_user=current_user,
        client_ip=_client_ip,
    )
    app.register_blueprint(admin_blueprint, url_prefix=config.server.admin_path)
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
