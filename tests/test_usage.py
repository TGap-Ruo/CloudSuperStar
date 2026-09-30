# -*- coding: utf-8 -*-
"""AI 用量与费用统计测试。"""

from datetime import datetime, timedelta, timezone

import pytest

from server.usage import (
    DEFAULT_PRICING,
    UsageContext,
    UsageRecorder,
    compute_cost,
    format_cost,
    is_peak,
    parse_usage,
)

# UTC 周三 02:00 = 高峰；UTC 周三 12:00 = 非高峰；UTC 周六 02:00 = 非高峰
PEAK = datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc)
OFF_PEAK = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
WEEKEND = datetime(2026, 10, 3, 2, 0, tzinfo=timezone.utc)


def test_is_peak_windows():
    assert is_peak(PEAK) is True
    assert is_peak(OFF_PEAK) is False
    assert is_peak(WEEKEND) is False           # 周末全天非高峰
    assert is_peak(datetime(2026, 9, 30, 5, 0, tzinfo=timezone.utc)) is False
    assert is_peak(datetime(2026, 9, 30, 7, 0, tzinfo=timezone.utc)) is True


def test_parse_usage_openai_and_deepseek():
    openai_usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    assert parse_usage(openai_usage) == {
        "prompt": 100, "cache_hit": 0, "cache_miss": 100, "output": 20, "total": 120,
    }

    deepseek_usage = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "prompt_cache_hit_tokens": 60,
        "prompt_cache_miss_tokens": 40,
    }
    assert parse_usage(deepseek_usage) == {
        "prompt": 100, "cache_hit": 60, "cache_miss": 40, "output": 20, "total": 120,
    }

    # 只给命中数时，其余算未命中
    partial = parse_usage({"prompt_tokens": 100, "completion_tokens": 0,
                           "prompt_cache_hit_tokens": 30})
    assert partial["cache_miss"] == 70

    assert parse_usage(None)["total"] == 0


def test_compute_cost_peak_and_off_peak():
    # deepseek-flash 高峰：未命中 0.3 / 输出 1.2（美元 / 1M）
    cost = compute_cost("deepseek-flash", cache_miss_tokens=1000, output_tokens=500, at=PEAK)
    assert cost == pytest.approx(0.0009, rel=1e-6)
    # 非高峰打五折
    cost_off = compute_cost("deepseek-flash", cache_miss_tokens=1000, output_tokens=500, at=OFF_PEAK)
    assert cost_off == pytest.approx(cost / 2, rel=1e-6)


def test_compute_cost_cache_hit_cheaper():
    hit = compute_cost("deepseek-flash", cache_hit_tokens=1000, at=PEAK)
    miss = compute_cost("deepseek-flash", cache_miss_tokens=1000, at=PEAK)
    assert hit < miss
    assert hit == pytest.approx(1000 * DEFAULT_PRICING["deepseek-flash"]["cache_hit"] / 1_000_000)


def test_unknown_model_falls_back():
    cost = compute_cost("some-unknown-model", cache_miss_tokens=1000, at=PEAK)
    assert cost > 0


def test_model_alias():
    assert compute_cost("deepseek-v4-flash", cache_miss_tokens=1000, at=PEAK) == pytest.approx(
        compute_cost("deepseek-flash", cache_miss_tokens=1000, at=PEAK)
    )


def test_recorder_aggregations(tmp_path):
    recorder = UsageRecorder(tmp_path / "state.db")
    ctx = UsageContext(user="u1", account="web-a", task_id="task-1", run_id="r1")
    usage = {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500,
             "prompt_cache_hit_tokens": 200, "prompt_cache_miss_tokens": 800}

    recorder.record(model="deepseek-flash", usage=usage, context=ctx, provider="AI", at=PEAK)
    recorder.record(model="deepseek-flash", usage=usage,
                    context=UsageContext(user="u2", account="web-b", task_id="task-2"),
                    provider="AI", at=OFF_PEAK)

    totals = recorder.totals()
    assert totals["calls"] == 2
    assert totals["total_tokens"] == 3000
    assert totals["cost"] > 0

    by_task = recorder.by_task()
    assert set(by_task) == {"task-1", "task-2"}
    assert recorder.for_task("task-1")["total_tokens"] == 1500
    assert recorder.for_account("web-a")["calls"] == 1
    assert recorder.totals(user="u1")["calls"] == 1

    by_user = {row["name"]: row for row in recorder.group_by("user")}
    assert set(by_user) == {"u1", "u2"}
    assert by_user["u1"]["cost"] > by_user["u2"]["cost"]   # 高峰更贵
    assert len(recorder.group_by("day")) >= 1
    assert len(recorder.recent()) == 2
    recorder.close()


def test_recorder_since_filter(tmp_path):
    recorder = UsageRecorder(tmp_path / "state.db")
    old = datetime.now(timezone.utc) - timedelta(days=10)
    recorder.record(model="deepseek-flash", usage={"prompt_tokens": 100},
                    context=UsageContext(user="u1"), at=old)
    recorder.record(model="deepseek-flash", usage={"prompt_tokens": 100},
                    context=UsageContext(user="u1"))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert recorder.totals()["calls"] == 2
    assert recorder.totals(since=today)["calls"] == 1
    recorder.close()


def test_format_cost():
    assert format_cost(0) == "$0"
    assert format_cost(0.0009).startswith("$0.0009")
    assert format_cost(1.5) == "$1.5000"
    assert format_cost(0.01, "CNY").startswith("¥")
