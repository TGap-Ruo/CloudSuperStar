# -*- coding: utf-8 -*-
"""AI 答题的 token 用量与费用统计。

刷课本身不花钱，花钱的地方只有一处：章节检测/作业交给大模型搜题。
把每次模型调用返回的 usage 记下来，就能算出每个账号 / 每个任务 / 每天花了多少钱。

默认计价（美元 / 1M tokens，取自 DeepSeek 官方定价页的**高峰价**）：

    deepseek-flash   缓存命中 0.006   缓存未命中 0.3   输出 1.2
    deepseek-v4-pro  缓存命中 0.044   缓存未命中 1.32  输出 3.96

* 非高峰时段单价是高峰价的一半；高峰为 UTC 周一至周五 01:00-04:00 与 06:00-10:00。
* DeepSeek 的 usage 带 prompt_cache_hit_tokens / prompt_cache_miss_tokens，
  可精确区分缓存命中；缺失时退化为「输入全部按未命中计」。
* 价格会变、也可能用中转服务，所以定价表放在配置里，后台可直接修改。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from server.store import Store

logger = logging.getLogger("chaoxing.usage")

UNIT = 1_000_000

DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "deepseek-flash": {"cache_hit": 0.006, "cache_miss": 0.3, "output": 1.2},
    "deepseek-v4-pro": {"cache_hit": 0.044, "cache_miss": 1.32, "output": 3.96},
    "deepseek-chat": {"cache_hit": 0.006, "cache_miss": 0.3, "output": 1.2},
    "deepseek-reasoner": {"cache_hit": 0.044, "cache_miss": 1.32, "output": 3.96},
    "__default__": {"cache_hit": 0.006, "cache_miss": 0.3, "output": 1.2},
}

OFF_PEAK_FACTOR = 0.5
PEAK_HOUR_RANGES = ((1, 4), (6, 10))

MODEL_ALIASES = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
    "deepseek-v3": "deepseek-flash",
    "deepseek-v3.2": "deepseek-flash",
}

USAGE_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS ai_usage ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " at TEXT NOT NULL,"
    " user TEXT DEFAULT '',"
    " account TEXT DEFAULT '',"
    " task_id TEXT DEFAULT '',"
    " run_id TEXT DEFAULT '',"
    " provider TEXT DEFAULT '',"
    " model TEXT DEFAULT '',"
    " prompt_tokens INTEGER DEFAULT 0,"
    " cache_hit_tokens INTEGER DEFAULT 0,"
    " cache_miss_tokens INTEGER DEFAULT 0,"
    " output_tokens INTEGER DEFAULT 0,"
    " total_tokens INTEGER DEFAULT 0,"
    " cost REAL DEFAULT 0,"
    " currency TEXT DEFAULT 'USD',"
    " detail TEXT DEFAULT ''"
    ");"
    "CREATE INDEX IF NOT EXISTS idx_usage_account ON ai_usage (account, at DESC);"
    "CREATE INDEX IF NOT EXISTS idx_usage_user ON ai_usage (user, at DESC);"
    "CREATE INDEX IF NOT EXISTS idx_usage_task ON ai_usage (task_id);"
    "CREATE INDEX IF NOT EXISTS idx_usage_at ON ai_usage (at DESC);"
)


def utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def is_peak(at: Optional[datetime] = None) -> bool:
    """是否处于高峰计费时段（UTC 工作日 01-04 / 06-10 点）。"""
    at = at or utc_now_dt()
    if at.weekday() >= 5:
        return False
    for start, end in PEAK_HOUR_RANGES:
        if start <= at.hour < end:
            return True
    return False


def normalize_model(model: str) -> str:
    name = (model or "").strip().lower()
    return MODEL_ALIASES.get(name, name)


def price_for(model: str, pricing: dict[str, dict[str, float]] | None = None) -> dict[str, float]:
    table = pricing or DEFAULT_PRICING
    return table.get(normalize_model(model)) or table.get("__default__") or DEFAULT_PRICING["__default__"]


def compute_cost(
    model: str,
    *,
    cache_hit_tokens: int = 0,
    cache_miss_tokens: int = 0,
    output_tokens: int = 0,
    at: Optional[datetime] = None,
    pricing: dict[str, dict[str, float]] | None = None,
    peak: Optional[bool] = None,
) -> float:
    """按缓存命中 / 未命中 / 输出分别计价，返回美元金额。"""
    unit_price = dict(price_for(model, pricing))
    if peak is None:
        peak = is_peak(at)
    if not peak:
        unit_price = {key: value * OFF_PEAK_FACTOR for key, value in unit_price.items()}
    cost = (
        cache_hit_tokens * unit_price.get("cache_hit", 0.0)
        + cache_miss_tokens * unit_price.get("cache_miss", 0.0)
        + output_tokens * unit_price.get("output", 0.0)
    ) / UNIT
    return round(cost, 8)


def parse_usage(raw: Any) -> dict[str, int]:
    """把 OpenAI 兼容接口的 usage 归一化成 token 统计。"""

    def _num(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    empty = {"prompt": 0, "cache_hit": 0, "cache_miss": 0, "output": 0, "total": 0}
    if raw is None:
        return empty

    data: dict[str, Any] = {}
    if isinstance(raw, dict):
        data = raw
    elif hasattr(raw, "model_dump"):
        try:
            data = raw.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    if not data:
        data = {
            key: getattr(raw, key, 0)
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "prompt_cache_hit_tokens",
                "prompt_cache_miss_tokens",
            )
        }

    prompt = _num(data.get("prompt_tokens"))
    output = _num(data.get("completion_tokens"))
    total = _num(data.get("total_tokens")) or (prompt + output)
    cache_hit = _num(data.get("prompt_cache_hit_tokens"))
    cache_miss = _num(data.get("prompt_cache_miss_tokens"))
    if cache_hit or cache_miss:
        cache_miss = cache_miss or max(0, prompt - cache_hit)
        prompt = prompt or (cache_hit + cache_miss)
    else:
        cache_hit = 0
        cache_miss = prompt

    return {
        "prompt": prompt,
        "cache_hit": cache_hit,
        "cache_miss": cache_miss,
        "output": output,
        "total": total,
    }


@dataclass
class UsageContext:
    """一次运行的归属信息（写进每条用量记录，便于聚合）。"""

    user: str = ""
    account: str = ""
    task_id: str = ""
    run_id: str = ""


class UsageRecorder:
    """写入并聚合 AI 用量（与运行记录共用同一个 SQLite 文件）。"""

    def __init__(self, db_path: str | Path):
        self.store = Store(db_path)
        self.store.executescript(USAGE_SCHEMA)

    def record(
        self,
        *,
        model: str,
        usage: Any,
        context: UsageContext | None = None,
        provider: str = "",
        pricing: dict[str, dict[str, float]] | None = None,
        at: Optional[datetime] = None,
    ) -> dict[str, Any]:
        context = context or UsageContext()
        at = at or utc_now_dt()
        tokens = parse_usage(usage)
        cost = compute_cost(
            model,
            cache_hit_tokens=tokens["cache_hit"],
            cache_miss_tokens=tokens["cache_miss"],
            output_tokens=tokens["output"],
            at=at,
            pricing=pricing,
        )
        row = {
            "at": at.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
            "user": context.user,
            "account": context.account,
            "task_id": context.task_id,
            "run_id": context.run_id,
            "provider": provider,
            "model": normalize_model(model),
            "prompt_tokens": tokens["prompt"],
            "cache_hit_tokens": tokens["cache_hit"],
            "cache_miss_tokens": tokens["cache_miss"],
            "output_tokens": tokens["output"],
            "total_tokens": tokens["total"],
            "cost": cost,
            "currency": "USD",
            "detail": json.dumps(
                {"peak": is_peak(at), "raw_model": model}, ensure_ascii=False
            ),
        }
        self.store.execute(
            """
            INSERT INTO ai_usage
                (at, user, account, task_id, run_id, provider, model,
                 prompt_tokens, cache_hit_tokens, cache_miss_tokens,
                 output_tokens, total_tokens, cost, currency, detail)
            VALUES (:at, :user, :account, :task_id, :run_id, :provider, :model,
                    :prompt_tokens, :cache_hit_tokens, :cache_miss_tokens,
                    :output_tokens, :total_tokens, :cost, :currency, :detail)
            """,
            row,
        )
        return row

    # ------------------------------------------------------------ 聚合查询
    def totals(self, *, since: str | None = None, user: str | None = None) -> dict[str, Any]:
        where, params = _filter_sql(since=since, user=user)
        rows = self.store.query(
            "SELECT COUNT(*) AS calls,"
            " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
            " COALESCE(SUM(cache_hit_tokens), 0) AS cache_hit_tokens,"
            " COALESCE(SUM(cache_miss_tokens), 0) AS cache_miss_tokens,"
            " COALESCE(SUM(output_tokens), 0) AS output_tokens,"
            " COALESCE(SUM(total_tokens), 0) AS total_tokens,"
            " COALESCE(SUM(cost), 0) AS cost"
            f" FROM ai_usage {where}",
            params,
        )
        return rows[0] if rows else {}

    def group_by(
        self, field: str, *, since: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        if field not in {"account", "user", "task_id", "model", "day"}:
            raise ValueError(f"不支持的聚合维度: {field}")
        expr = "substr(at, 1, 10)" if field == "day" else field
        where, params = _filter_sql(since=since)
        return self.store.query(
            f"SELECT {expr} AS name, COUNT(*) AS calls,"
            " COALESCE(SUM(total_tokens), 0) AS total_tokens,"
            " COALESCE(SUM(output_tokens), 0) AS output_tokens,"
            " COALESCE(SUM(cost), 0) AS cost"
            f" FROM ai_usage {where} GROUP BY {expr}"
            " ORDER BY cost DESC, total_tokens DESC LIMIT ?",
            params + (max(1, limit),),
        )

    def recent(self, limit: int = 100, *, user: str | None = None) -> list[dict[str, Any]]:
        where, params = _filter_sql(user=user)
        return self.store.query(
            f"SELECT * FROM ai_usage {where} ORDER BY id DESC LIMIT ?",
            params + (max(1, limit),),
        )

    def for_task(self, task_id: str) -> dict[str, Any]:
        rows = self.store.query(
            "SELECT COUNT(*) AS calls,"
            " COALESCE(SUM(total_tokens), 0) AS total_tokens,"
            " COALESCE(SUM(cost), 0) AS cost"
            " FROM ai_usage WHERE task_id = ?",
            (task_id,),
        )
        return rows[0] if rows else {"calls": 0, "total_tokens": 0, "cost": 0.0}

    def for_account(self, account: str) -> dict[str, Any]:
        rows = self.store.query(
            "SELECT COUNT(*) AS calls,"
            " COALESCE(SUM(total_tokens), 0) AS total_tokens,"
            " COALESCE(SUM(cost), 0) AS cost"
            " FROM ai_usage WHERE account = ?",
            (account,),
        )
        return rows[0] if rows else {"calls": 0, "total_tokens": 0, "cost": 0.0}

    def by_task(self) -> dict[str, dict[str, Any]]:
        """一次查出所有任务的用量，供任务列表直接带出「本次花费」。"""
        rows = self.store.query(
            "SELECT task_id, COUNT(*) AS calls,"
            " COALESCE(SUM(total_tokens), 0) AS total_tokens,"
            " COALESCE(SUM(cost), 0) AS cost"
            " FROM ai_usage WHERE task_id != '' GROUP BY task_id"
        )
        return {str(row["task_id"]): row for row in rows}

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "UsageRecorder":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _filter_sql(*, since: str | None = None, user: str | None = None) -> tuple[str, tuple]:
    clauses, params = [], []
    if since:
        clauses.append("at >= ?")
        params.append(since)
    if user:
        clauses.append("user = ?")
        params.append(user)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, tuple(params)


# ─────────────────────────── 与核心对接 ───────────────────────────
_recorders: dict[str, UsageRecorder] = {}


def install_usage_hook(
    db_path: str | Path,
    context: UsageContext,
    *,
    pricing: dict[str, dict[str, float]] | None = None,
) -> None:
    """在刷课子进程里挂上用量回调（每次调用大模型后回调一次）。"""
    key = str(db_path)
    recorder = _recorders.get(key)
    if recorder is None:
        recorder = UsageRecorder(db_path)
        _recorders[key] = recorder

    def _hook(payload: dict[str, Any]) -> None:
        try:
            recorder.record(
                model=str(payload.get("model", "")),
                usage=payload.get("usage"),
                context=context,
                provider=str(payload.get("provider", "")),
                pricing=pricing,
            )
        except Exception as exc:  # noqa: BLE001 - 统计失败不能影响答题
            logger.warning("记录 AI 用量失败: %s", exc)

    from chaoxing_core import answer as core_answer

    core_answer.set_usage_hook(_hook)


def current_recorder(db_path: str | Path) -> Optional[UsageRecorder]:
    """取当前进程里已安装的 recorder（供运行结束后读取本次用量）。"""
    return _recorders.get(str(db_path))


def format_cost(cost: float, currency: str = "USD") -> str:
    symbol = "¥" if currency == "CNY" else "$"
    if not cost:
        return f"{symbol}0"
    if cost < 0.01:
        return f"{symbol}{cost:.6f}"
    return f"{symbol}{cost:.4f}"
