# -*- coding: utf-8 -*-
"""管理后台：用户 / 卡密 / 任务 / 用量 / 审计 / 系统设置。

以 Flask Blueprint 形式挂在可配置的 ``server.admin_path``（默认 /admin）下。
所有接口都要求管理员身份（见 server/web.py 的 current_user / require_admin）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import yaml
from flask import Blueprint, Response, jsonify, redirect, render_template, request, send_file

from server.auth import AuthError, AuthManager
from server.config import ServerConfig
from server.usage import DEFAULT_PRICING, UsageRecorder, format_cost
from server.tasks import TaskError, TaskManager

logger = logging.getLogger("chaoxing.admin")


def _json_ok(**payload: Any) -> Response:
    payload.setdefault("ok", True)
    return jsonify(payload)


def _json_error(message: str, status: int = 400) -> Response:
    return jsonify({"ok": False, "error": message}), status


def _pricing_view(config: ServerConfig) -> dict[str, dict[str, float]]:
    merged = {key: dict(value) for key, value in DEFAULT_PRICING.items()}
    for model, price in (config.pricing or {}).items():
        merged.setdefault(model, {}).update(price)
    return merged


def update_config_file(path: str | Path, updates: dict[str, Any]) -> None:
    """把少量配置改动写回 config.yaml（先备份，避免写坏）。"""
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raw = {}
    for section, values in updates.items():
        if values is None:
            continue
        if not isinstance(values, dict):
            raw[section] = values
            continue
        target = raw.get(section)
        if not isinstance(target, dict):
            target = {}
        target.update(values)
        raw[section] = target
    backup = config_path.with_suffix(".yaml.bak")
    try:
        backup.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        pass
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def create_admin_blueprint(
    config: ServerConfig,
    config_path: Path,
    *,
    auth: AuthManager,
    manager: TaskManager,
    usage: UsageRecorder,
    current_user: Callable[[], dict[str, Any] | None],
    client_ip: Callable[[], str],
) -> Blueprint:
    base = config.server.admin_path or "/admin"
    bp = Blueprint("admin", __name__, template_folder=str(Path(__file__).parent / "templates"))

    def _guard() -> dict[str, Any] | None:
        user = current_user()
        if not user:
            return None
        return user if user.get("role") == "admin" else None

    def _need_admin():
        user = current_user()
        if user is None:
            return _json_error("需要登录", 401)
        if user.get("role") != "admin":
            return _json_error("需要管理员权限", 403)
        return None

    # ────────────────────────────── 页面
    @bp.get("/")
    def page():
        user = current_user()
        if user is None:
            return redirect("/login")
        if user.get("role") != "admin":
            # 普通用户没有后台权限，送回刷课控制台
            return redirect("/")
        return render_template(
            "admin.html",
            base=base,
            admin_path=base,
            username=user.get("username", ""),
            currency=config.server.currency,
            usd_to_cny=config.server.usd_to_cny,
        )

    # ────────────────────────────── 概览
    @bp.get("/api/overview")
    def api_overview() -> Response:
        denied = _need_admin()
        if denied:
            return denied
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        runs = manager.list_tasks(limit=500)
        running = sum(1 for item in runs if item.get("status") == "running")
        finished = sum(1 for item in runs if item.get("status") == "finished")
        failed = sum(1 for item in runs if item.get("status") == "failed")
        stopped = sum(1 for item in runs if item.get("status") == "stopped")
        return _json_ok(
            auth=auth.stats(),
            tasks={
                "total": len(runs),
                "running": running,
                "finished": finished,
                "failed": failed,
                "stopped": stopped,
                "max_parallel": config.server.web_max_parallel_tasks,
            },
            usage={
                "all": usage.totals(),
                "today": usage.totals(since=today),
                "week": usage.totals(since=week_ago),
            },
            deepseek=_deepseek_info(config),
            server={
                "data_dir": str(config.data_paths().root),
                "config_path": str(config_path),
                "auth_enabled": config.server.auth_enabled,
                "admin_path": base,
                "usd_to_cny": config.server.usd_to_cny,
            },
        )

    # ────────────────────────────── 用户
    @bp.get("/api/users")
    def api_users() -> Response:
        denied = _need_admin()
        if denied:
            return denied
        keyword = request.args.get("q", "")
        users = auth.list_users(keyword)
        for user in users:
            user["usage"] = usage.totals(user=user["username"])
            user["used_today"] = auth.used_today(user["username"])
        return _json_ok(users=users)

    @bp.post("/api/users")
    def api_create_user() -> Response:
        denied = _need_admin()
        if denied:
            return denied
        payload = request.get_json(silent=True) or {}
        try:
            user = auth.create_user(
                str(payload.get("username", "")),
                str(payload.get("password", "")),
                role=str(payload.get("role", "user")),
                credits=int(payload.get("credits", 0) or 0),
                daily_task_limit=int(payload.get("daily_task_limit", 0) or 0),
                max_parallel=int(payload.get("max_parallel", 0) or 0),
                note=str(payload.get("note", "")),
            )
        except AuthError as exc:
            return _json_error(str(exc))
        auth.log("create_user", actor=(current_user() or {}).get("username", ""),
                 target=user.get("username", ""), ip=client_ip())
        return _json_ok(user=user)

    @bp.patch("/api/users/<username>")
    def api_update_user(username: str) -> Response:
        denied = _need_admin()
        if denied:
            return denied
        payload = request.get_json(silent=True) or {}
        try:
            user = auth.update_user(username, **payload)
        except AuthError as exc:
            return _json_error(str(exc))
        auth.log("update_user", actor=(current_user() or {}).get("username", ""),
                 target=username, detail=str(payload), ip=client_ip())
        return _json_ok(user=user)

    @bp.post("/api/users/<username>/password")
    def api_user_password(username: str) -> Response:
        denied = _need_admin()
        if denied:
            return denied
        payload = request.get_json(silent=True) or {}
        try:
            auth.set_password(username, str(payload.get("password", "")))
        except AuthError as exc:
            return _json_error(str(exc))
        auth.log("reset_password", actor=(current_user() or {}).get("username", ""),
                 target=username, ip=client_ip())
        return _json_ok()

    @bp.delete("/api/users/<username>")
    def api_delete_user(username: str) -> Response:
        denied = _need_admin()
        if denied:
            return denied
        try:
            auth.delete_user(username)
        except AuthError as exc:
            return _json_error(str(exc))
        auth.log("delete_user", actor=(current_user() or {}).get("username", ""),
                 target=username, ip=client_ip())
        return _json_ok()

    @bp.post("/api/users/<username>/api-key")
    def api_user_api_key(username: str) -> Response:
        denied = _need_admin()
        if denied:
            return denied
        payload = request.get_json(silent=True) or {}
        key = auth.create_api_key(username, str(payload.get("name", "")))
        auth.log("create_api_key", actor=(current_user() or {}).get("username", ""),
                 target=username, ip=client_ip())
        return _json_ok(key=key)

    @bp.get("/api/api-keys")
    def api_api_keys() -> Response:
        denied = _need_admin()
        if denied:
            return denied
        return _json_ok(keys=auth.list_api_keys())

    @bp.delete("/api/api-keys/<key>")
    def api_delete_api_key(key: str) -> Response:
        denied = _need_admin()
        if denied:
            return denied
        auth.delete_api_key(key)
        auth.log("delete_api_key", actor=(current_user() or {}).get("username", ""),
                 target=key[:12], ip=client_ip())
        return _json_ok()

    # ────────────────────────────── 授权码
    @bp.get("/api/codes")
    def api_codes() -> Response:
        denied = _need_admin()
        if denied:
            return denied
        keyword = request.args.get("q", "")
        codes = auth.list_codes(keyword)
        for code in codes:
            code["usage"] = usage.for_code(code["code"])
        return _json_ok(codes=codes, stats=auth.stats().get("codes", {}))

    @bp.post("/api/codes")
    def api_create_codes() -> Response:
        denied = _need_admin()
        if denied:
            return denied
        payload = request.get_json(silent=True) or {}
        try:
            codes = auth.create_codes(
                count=int(payload.get("count", 1) or 1),
                uses=int(payload.get("uses", 1) or 1),
                name=str(payload.get("name", "")),
                note=str(payload.get("note", "")),
                expires_at=str(payload.get("expires_at", "")),
            )
        except AuthError as exc:
            return _json_error(str(exc))
        auth.log("create_codes", actor=(current_user() or {}).get("username", ""),
                 detail=f"生成 {len(codes)} 张授权码", ip=client_ip())
        return _json_ok(codes=codes)

    @bp.post("/api/codes/<code>/toggle")
    def api_toggle_code(code: str) -> Response:
        denied = _need_admin()
        if denied:
            return denied
        payload = request.get_json(silent=True) or {}
        try:
            auth.toggle_code(code, bool(payload.get("enabled", True)))
        except AuthError as exc:
            return _json_error(str(exc))
        auth.log("toggle_code", actor=(current_user() or {}).get("username", ""),
                 target=code, ip=client_ip())
        return _json_ok()

    @bp.post("/api/codes/<code>/reset")
    def api_reset_code(code: str) -> Response:
        denied = _need_admin()
        if denied:
            return denied
        try:
            auth.reset_code(code)
        except AuthError as exc:
            return _json_error(str(exc))
        auth.log("reset_code", actor=(current_user() or {}).get("username", ""),
                 target=code, ip=client_ip())
        return _json_ok()

    @bp.post("/api/codes/<code>/uses")
    def api_set_code_uses(code: str) -> Response:
        denied = _need_admin()
        if denied:
            return denied
        payload = request.get_json(silent=True) or {}
        try:
            auth.set_code_uses(code, int(payload.get("uses", 1)))
        except (AuthError, TypeError, ValueError) as exc:
            return _json_error(str(exc) or "次数不合法")
        auth.log("set_code_uses", actor=(current_user() or {}).get("username", ""),
                 target=code, detail=f"次数={payload.get('uses')}", ip=client_ip())
        return _json_ok()

    @bp.get("/api/codes/<code>/usages")
    def api_code_usages(code: str) -> Response:
        denied = _need_admin()
        if denied:
            return denied
        return _json_ok(
            usages=auth.code_usages(code, limit=200),
            code=auth.get_code(code),
            usage=usage.for_code(code),
        )

    @bp.delete("/api/codes/<code>")
    def api_delete_code(code: str) -> Response:
        denied = _need_admin()
        if denied:
            return denied
        auth.delete_code(code)
        auth.log("delete_code", actor=(current_user() or {}).get("username", ""),
                 target=code, ip=client_ip())
        return _json_ok()

    # ────────────────────────────── 任务（跨用户）
    @bp.get("/api/tasks")
    def api_tasks() -> Response:
        denied = _need_admin()
        if denied:
            return denied
        limit = min(500, max(1, request.args.get("limit", 200, type=int)))
        tasks = manager.list_tasks(limit=limit)
        for task in tasks:
            task["usage"] = usage.for_task(task["id"])
        return _json_ok(tasks=tasks, stats=manager.stats())

    @bp.post("/api/tasks/<task_id>/stop")
    def api_stop_task(task_id: str) -> Response:
        denied = _need_admin()
        if denied:
            return denied
        ok, message = manager.stop_task(task_id)
        if not ok:
            return _json_error(message)
        auth.log("admin_stop_task", actor=(current_user() or {}).get("username", ""),
                 target=task_id, ip=client_ip())
        return _json_ok(message=message)

    @bp.delete("/api/tasks/<task_id>")
    def api_delete_task(task_id: str) -> Response:
        denied = _need_admin()
        if denied:
            return denied
        ok, message = manager.delete_task(task_id)
        if not ok:
            return _json_error(message)
        auth.log("admin_delete_task", actor=(current_user() or {}).get("username", ""),
                 target=task_id, ip=client_ip())
        return _json_ok(message=message)

    @bp.get("/api/tasks/<task_id>/log")
    def api_task_log(task_id: str) -> Response:
        denied = _need_admin()
        if denied:
            return denied
        path = manager.log_path(task_id)
        if path is None:
            return _json_error("日志不存在", 404)
        return send_file(path, as_attachment=True, download_name=f"task_{task_id}.log")

    # ────────────────────────────── 用量 / 费用
    @bp.get("/api/usage")
    def api_usage() -> Response:
        denied = _need_admin()
        if denied:
            return denied
        group = request.args.get("group", "account")
        days = max(1, min(365, request.args.get("days", 30, type=int)))
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        groups: dict[str, Any] = {}
        for dimension in ("code", "account", "user", "day", "model"):
            groups[dimension] = usage.group_by(dimension, since=since, limit=200)
        rows = usage.recent(limit=min(500, request.args.get("limit", 100, type=int)))
        for row in rows:
            row["cost_display"] = format_cost(float(row.get("cost") or 0))
        return _json_ok(
            group=group,
            since=since,
            groups=groups,
            recent=rows,
            totals={"all": usage.totals(), "range": usage.totals(since=since)},
            pricing=_pricing_view(config),
            usd_to_cny=config.server.usd_to_cny,
        )

    # ────────────────────────────── 审计日志
    @bp.get("/api/audit")
    def api_audit() -> Response:
        denied = _need_admin()
        if denied:
            return denied
        keyword = request.args.get("q", "")
        limit = min(1000, max(1, request.args.get("limit", 200, type=int)))
        return _json_ok(logs=auth.list_audit(limit=limit, keyword=keyword))

    # ────────────────────────────── 系统设置
    @bp.get("/api/settings")
    def api_settings() -> Response:
        denied = _need_admin()
        if denied:
            return denied
        return _json_ok(
            deepseek=_deepseek_info(config),
            pricing=_pricing_view(config),
            study={
                "speed": config.study.speed,
                "jobs": config.study.jobs,
                "submit": config.answer.submit,
                "cover_rate": config.answer.cover_rate,
                "work_max_retries": config.study.work_max_retries,
            },
            security={
                "auth_enabled": config.server.auth_enabled,
                "admin_path": base,
                "credits_per_task": config.server.credits_per_task,
                "refund_on_failure": config.server.refund_on_failure,
                "currency": config.server.currency,
                "web_max_parallel_tasks": config.server.web_max_parallel_tasks,
            },
        )

    @bp.post("/api/settings")
    def api_save_settings() -> Response:
        denied = _need_admin()
        if denied:
            return denied
        payload = request.get_json(silent=True) or {}
        updates: dict[str, Any] = {}

        answer_update: dict[str, Any] = {}
        if payload.get("deepseek_key"):
            answer_update["providers"] = [
                {
                    "type": "AI",
                    "base_url": str(payload.get("deepseek_base_url") or "https://api.deepseek.com/v1"),
                    "key": str(payload["deepseek_key"]),
                    "model": str(payload.get("deepseek_model") or "deepseek-flash"),
                    "min_interval_seconds": 3,
                }
            ]
        if answer_update:
            updates["answer"] = answer_update

        pricing = payload.get("pricing")
        if isinstance(pricing, dict) and pricing:
            updates["pricing"] = {
                str(model): {
                    key: float(value)
                    for key, value in price.items()
                    if key in {"cache_hit", "cache_miss", "output"} and value not in (None, "")
                }
                for model, price in pricing.items()
                if isinstance(price, dict)
            }

        security = payload.get("security")
        if isinstance(security, dict):
            allowed = {
                "auth_enabled",
                "credits_per_task",
                "refund_on_failure",
                "currency",
                "web_max_parallel_tasks",
            }
            server_update = {key: value for key, value in security.items() if key in allowed}
            if server_update:
                updates["server"] = server_update

        study = payload.get("study")
        if isinstance(study, dict):
            study_update = {
                key: study[key]
                for key in ("speed", "jobs", "work_max_retries")
                if key in study
            }
            if study_update:
                updates.setdefault("study", {}).update(study_update)
            answer_keys = {key: study[key] for key in ("submit", "cover_rate") if key in study}
            if answer_keys:
                updates.setdefault("answer", {}).update(answer_keys)

        if not updates:
            return _json_error("没有需要保存的改动")

        try:
            update_config_file(config_path, updates)
        except Exception as exc:  # noqa: BLE001
            logger.exception("保存配置失败")
            return _json_error(f"保存失败: {exc}", 500)

        auth.log("update_settings", actor=(current_user() or {}).get("username", ""),
                 detail=str(list(updates.keys())), ip=client_ip())
        return _json_ok(restart_required=True, message="已写入配置文件，重启服务后生效")

    return bp


def _deepseek_info(config: ServerConfig) -> dict[str, Any]:
    """当前大模型配置概况（不返回完整 Key，只给前后几位）。"""
    for answer in [config.answer] + [account.answer for account in config.accounts]:
        for provider in answer.providers:
            if provider.normalized_type() not in {"AI", "SiliconFlow"}:
                continue
            options = provider.options or {}
            key = str(options.get("key") or options.get("api_key") or "")
            if not key:
                continue
            return {
                "configured": True,
                "provider": provider.normalized_type(),
                "model": str(options.get("model", "")),
                "base_url": str(options.get("base_url") or options.get("endpoint") or ""),
                "key_masked": f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "已配置",
            }
    return {"configured": False}
