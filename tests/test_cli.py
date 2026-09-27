# -*- coding: utf-8 -*-
from pathlib import Path

from server.cli import main
from tests import fake_cx


def test_init_creates_config(tmp_path, capsys):
    target = tmp_path / "config.yaml"
    assert main(["--config", str(target), "init"]) == 0
    assert target.is_file()
    assert "accounts:" in target.read_text(encoding="utf-8")

    # 已存在时默认不覆盖
    assert main(["--config", str(target), "init"]) == 1
    assert main(["--config", str(target), "init", "--force"]) == 0


def test_check_command(config_path, capsys):
    assert main(["--config", str(config_path), "check"]) == 0
    out = capsys.readouterr().out
    assert "配置校验通过" in out
    assert "tester" in out


def test_check_reports_config_error(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("accounts: []", encoding="utf-8")
    assert main(["--config", str(bad), "check"]) == 2
    assert "配置错误" in capsys.readouterr().err


def test_run_command(server_config, config_path, monkeypatch, fake_openai, capsys):
    fake_cx.install(monkeypatch)
    exit_code = main(["--config", str(config_path), "run", "-a", "tester", "--json"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert '"status": "success"' in out


def test_run_all_command(server_config, config_path, monkeypatch, fake_openai, capsys):
    fake_cx.install(monkeypatch)
    assert main(["--config", str(config_path), "run", "--all"]) == 0
    assert "tester" in capsys.readouterr().out


def test_status_command(config_path, capsys):
    assert main(["--config", str(config_path), "status"]) == 0


def test_courses_command(config_path, monkeypatch, capsys):
    fake_cx.install(monkeypatch)
    assert main(["--config", str(config_path), "courses", "-a", "tester"]) == 0
    out = capsys.readouterr().out
    assert "2151141" in out
    assert "测试课程" in out


def test_login_command_writes_cookies(config_path, monkeypatch, capsys):
    fake_cx.install(monkeypatch)
    assert main(["--config", str(config_path), "login", "-a", "tester"]) == 0
    from server.config import load_config

    config = load_config(config_path)
    cookies = config.data_paths().account("tester").cookies
    assert cookies.is_file()
    assert "_uid" in cookies.read_text(encoding="utf-8")
