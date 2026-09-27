# -*- coding: utf-8 -*-
from server.store import Store


def test_run_lifecycle(tmp_path):
    store = Store(tmp_path / "state.db")
    row_id = store.start_run("tester", "20260101-000000")
    assert store.run_row_id("20260101-000000") == row_id

    store.finish_run(
        row_id,
        status="success",
        message="完成",
        summary={
            "courses_total": 2,
            "courses_finished": 2,
            "chapters_total": 10,
            "chapters_failed": 1,
            "answer_total": 8,
            "answer_covered": 7,
        },
        duration_seconds=123.4,
    )

    runs = store.recent_runs()
    assert len(runs) == 1
    run = runs[0]
    assert run.status == "success"
    assert run.chapters_total == 10
    assert run.answer_covered == 7
    assert run.duration_seconds == 123.4
    store.close()


def test_account_state_counts_failures(tmp_path):
    store = Store(tmp_path / "state.db")
    store.record_account_result("tester", status="failed", message="登录失败")
    store.record_account_result("tester", status="timeout", message="超时")
    state = store.account_state("tester")
    assert state is not None
    assert state["consecutive_failures"] == 2

    store.record_account_result("tester", status="success", message="完成")
    assert store.account_state("tester")["consecutive_failures"] == 0
    store.close()


def test_stale_running_runs_are_failed(tmp_path):
    store = Store(tmp_path / "state.db")
    store.start_run("tester", "run-a")
    store.start_run("tester", "run-b")
    assert len(store.running_runs()) == 2

    assert store.mark_stale_running_as_failed() == 2
    assert store.running_runs() == []
    assert all(run.status == "failed" for run in store.recent_runs())
    store.close()


def test_recent_runs_filter_by_account(tmp_path):
    store = Store(tmp_path / "state.db")
    store.start_run("a", "1")
    store.start_run("b", "2")
    assert [run.account for run in store.recent_runs(account="b")] == ["b"]
    assert len(store.recent_runs()) == 2
    store.close()
