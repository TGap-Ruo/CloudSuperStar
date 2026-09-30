# -*- coding: utf-8 -*-
import json

import pytest

from server.runner import run_account, select_courses
from tests import fake_cx


def test_full_run_success(server_config, monkeypatch, fake_openai):
    fake = fake_cx.install(monkeypatch)
    account = server_config.accounts[0]

    report = run_account(server_config, account)

    assert report.status == "success", report.message
    assert report.chapters_total == 3
    assert report.chapters_finished == 3
    assert report.chapters_failed == 0
    # 第二章的章节检测有 3 道题，全部由（假）AI 命中
    assert report.answer_total == 3
    assert report.answer_covered == 3
    assert report.courses[0].status == "success"

    # 提交的答案应当匹配到正确选项字母
    submitted = fake.recorded["work_submit"]
    assert submitted["answerq1"] == ["B"]        # 单选 -> 北京
    assert submitted["answerq2"] == ["true"]     # 判断 -> 正确
    assert submitted["answerq3"] == ["AC"]       # 多选 -> Python / Java

    # 视频任务上报过一次进度
    assert "video_log" in fake.recorded
    assert fake.recorded["video_log"]["playingTime"] == ["10"]


def test_run_creates_runtime_files(server_config, monkeypatch, fake_openai):
    fake_cx.install(monkeypatch)
    account = server_config.accounts[0]
    report = run_account(server_config, account)

    paths = server_config.data_paths().account(account.name)
    assert paths.cookies.is_file(), "登录后应保存 cookies.txt"
    assert paths.config_ini.is_file(), "应渲染出内部 config.ini"
    assert paths.log.is_file(), "应写入账号日志"
    assert paths.run_report(report.run_id).is_file(), "应写出 JSON 运行报告"

    payload = json.loads(paths.run_report(report.run_id).read_text(encoding="utf-8"))
    assert payload["status"] == "success"
    assert payload["courses"][0]["course_id"] == "2151141"


def test_login_failure_returns_failed_report(server_config, monkeypatch):
    fake_cx.install(monkeypatch, fake_cx.FakeChaoxing(login_ok=False))
    report = run_account(server_config, server_config.accounts[0])
    assert report.status == "failed"
    assert "账号或密码错误" in report.message


def test_work_submit_failure_marks_chapter_failed(server_config, monkeypatch, fake_openai):
    fake_cx.install(monkeypatch, fake_cx.FakeChaoxing(work_submit_ok=False))
    report = run_account(server_config, server_config.accounts[0])
    # 章节检测提交失败 -> 该章节进入重试，最终失败
    assert report.status == "partial"
    assert report.chapters_failed >= 1


def test_graded_work_chapter_is_not_marked_failed(server_config, monkeypatch):
    """章节检测已提交（页面变为已批阅、成绩 100）时不应被判失败，也不应再提交。"""
    fake = fake_cx.install(monkeypatch, fake_cx.FakeChaoxing(work_graded=True))
    report = run_account(server_config, server_config.accounts[0])

    assert report.status == "success", report.message
    assert report.chapters_failed == 0
    assert report.chapters_finished == 3
    # 已批阅的检测卷不应触发任何提交
    assert fake.recorded.get("work_submit") is None


def test_transient_failure_is_retried_and_recovers(server_config, monkeypatch, fake_openai):
    """第一次提交失败、重试成功：验证重试中的章节不会被队列提前丢弃。"""
    fake = fake_cx.install(
        monkeypatch, fake_cx.FakeChaoxing(work_submit_fail_times=1)
    )
    report = run_account(server_config, server_config.accounts[0])

    assert report.status == "success", report.message
    assert report.chapters_failed == 0
    assert report.chapters_finished == 3
    assert fake.recorded["work_submit_count"] == 2


def test_dry_run_does_not_execute_tasks(server_config, monkeypatch):
    fake = fake_cx.install(monkeypatch)
    report = run_account(server_config, server_config.accounts[0], dry_run=True)

    assert report.status == "success"
    assert "dry-run" in report.message
    assert report.chapters_total == 0
    assert not any("multimedia/log" in call for call in fake.calls)
    assert not any("addStudentWorkNew" in call for call in fake.calls)


def test_auto_sign_flow(server_config, monkeypatch, fake_openai):
    fake = fake_cx.install(monkeypatch)
    account = server_config.accounts[0]
    account.study.auto_sign = True

    report = run_account(server_config, account)

    assert report.sign_in["checked"] == 1
    assert report.sign_in["signed"] == 1
    assert any("stuSignajax" in call for call in fake.calls)


def test_course_filter(server_config):
    courses = [
        {"courseId": "1", "clazzId": "10", "title": "A"},
        {"courseId": "2", "clazzId": "20", "title": "B"},
        {"courseId": "2", "clazzId": "21", "title": "B2"},
        {"courseId": "3", "clazzId": "30", "title": "C"},
    ]
    study = server_config.study

    assert [c["title"] for c in select_courses(courses, study)] == ["A", "B", "B2", "C"]
    assert [c["title"] for c in select_courses(courses, study, ["2"])] == ["B", "B2"]

    study.exclude_courses = ["3"]
    assert [c["title"] for c in select_courses(courses, study)] == ["A", "B", "B2"]


def test_notify_on_failure(server_config, monkeypatch):
    fake_cx.install(monkeypatch, fake_cx.FakeChaoxing(login_ok=False))
    sent: list[tuple] = []
    monkeypatch.setattr(
        "server.runner.notify_send", lambda cfg, title, content: sent.append((title, content))
    )

    from server.runner import run_and_notify

    report = run_and_notify(server_config, server_config.accounts[0])
    assert report.status == "failed"
    assert len(sent) == 1
    assert "运行失败" in sent[0][0]


def test_instrumented_tiku_counts_queries():
    from server.runner import InstrumentedTiku

    class Inner:
        DISABLE = False

        def query(self, q_info):
            return "答案" if q_info["title"] == "命中" else None

    proxy = InstrumentedTiku(Inner())
    assert proxy.query({"title": "命中"}) == "答案"
    assert proxy.query({"title": "未命中"}) is None
    assert (proxy.queries, proxy.hits) == (2, 1)
    assert proxy.DISABLE is False
