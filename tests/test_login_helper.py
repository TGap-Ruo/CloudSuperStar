# -*- coding: utf-8 -*-
"""login_helper 的离线测试（复用假超星服务）。

login_helper 负责「只登录、不刷课，只列课程」，它的输出决定前端弹窗里能勾哪些课。
"""

import json

import pytest
import yaml

from server.config import load_config
from server.login_helper import main as login_helper_main
from tests import fake_cx


@pytest.fixture
def helper_config(tmp_path, config_dict):
    config_dict["server"]["data_dir"] = str(tmp_path / "data")
    config_dict["server"]["log_level"] = "WARNING"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config_dict, allow_unicode=True), encoding="utf-8")
    return path


def test_list_courses_success(helper_config, monkeypatch, capsys):
    fake_cx.install(monkeypatch)
    exit_code = login_helper_main(["--config", str(helper_config), "--account", "tester"])
    assert exit_code == 0

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert len(payload["courses"]) == 1
    course = payload["courses"][0]
    assert course["course_id"] == "2151141"
    assert course["clazz_id"] == "107515845"
    assert course["title"] == "测试课程"
    assert "teacher" in course


def test_list_courses_login_failure(helper_config, monkeypatch, capsys):
    fake_cx.install(monkeypatch, fake_cx.FakeChaoxing(login_ok=False))
    exit_code = login_helper_main(["--config", str(helper_config), "--account", "tester"])
    assert exit_code == 1

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert "密码错误" in payload["error"]


def test_login_helper_saves_cookies(helper_config, monkeypatch, capsys):
    fake_cx.install(monkeypatch)
    login_helper_main(["--config", str(helper_config), "--account", "tester"])
    capsys.readouterr()

    config = load_config(helper_config)
    cookies = config.data_paths().account("tester").cookies
    assert cookies.is_file()
    assert "_uid" in cookies.read_text(encoding="utf-8")
