# -*- coding: utf-8 -*-
from pathlib import Path

import pytest
import yaml

from server.config import ConfigError, load_config, render_ini


def test_load_config_basic(config_path):
    config = load_config(config_path)
    assert len(config.accounts) == 1
    account = config.accounts[0]
    assert account.name == "tester"
    assert account.schedule == "0 8 * * *"
    assert account.answer.submit is True
    assert account.answer.provider_names == ["AI"]


def test_account_override_study(config_path):
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["accounts"][0]["study"] = {"include_courses": ["2151141"], "speed": 1.8}
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    config = load_config(config_path)
    account = config.accounts[0]
    assert account.study.include_courses == ["2151141"]
    assert account.study.speed == 1.8
    assert account.study.work_max_retries == 2
    assert config.study.speed == 1.0


def test_account_override_providers(config_path):
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["accounts"][0]["answer"] = {"providers": [{"type": "TikuGo", "authorization": "tok"}]}
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    config = load_config(config_path)
    account = config.accounts[0]
    assert account.answer.provider_names == ["TikuGo"]
    assert account.answer.submit is True
    assert account.answer.cover_rate == 0.5


def test_render_ini_maps_provider_options(server_config):
    account = server_config.accounts[0]
    ini_text = render_ini(server_config, account)

    assert "[common]" in ini_text
    assert "provider = AI" in ini_text
    assert "endpoint = https://api.deepseek.com/v1" in ini_text
    assert "key = sk-test" in ini_text
    assert "model = deepseek-chat" in ini_text
    assert "submit = true" in ini_text
    assert "notopen_action = continue" in ini_text


def test_speed_is_clamped(config_path):
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["study"]["speed"] = 5
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    assert load_config(config_path).study.speed == 2.0


def test_password_env(config_path, monkeypatch):
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["accounts"][0].pop("password")
    raw["accounts"][0]["password_env"] = "TEST_CX_PASSWORD"
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    monkeypatch.setenv("TEST_CX_PASSWORD", "from-env")
    assert load_config(config_path).accounts[0].password == "from-env"

    monkeypatch.delenv("TEST_CX_PASSWORD")
    with pytest.raises(ConfigError, match="password_env"):
        load_config(config_path)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda raw: raw["accounts"][0].pop("password"), "缺少 password"),
        (lambda raw: raw["accounts"][0].update({"schedule": "每天八点"}), "cron"),
        (
            lambda raw: raw["answer"].update({"providers": [{"type": "不存在的题库"}]}),
            "不支持的题库",
        ),
        (
            lambda raw: raw["answer"].update({"providers": [{"type": "AI", "key": "sk-x"}]}),
            "base_url",
        ),
        (lambda raw: raw.update({"accounts": []}), "accounts 为空"),
        (lambda raw: raw["notify"].update({"provider": "unknown-provider"}), "provider"),
    ],
)
def test_validation_errors(config_path, mutate, message):
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    mutate(raw)
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(config_path)
    assert message in str(excinfo.value)


def test_duplicate_account_names(config_path):
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["accounts"].append(dict(raw["accounts"][0]))
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError, match="账号名重复"):
        load_config(config_path)


def test_account_name_path_traversal_rejected(config_path):
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["accounts"][0]["name"] = "../../etc/passwd"
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError, match="非法"):
        load_config(config_path)


def test_relative_data_dir_follows_config_location(tmp_path):
    cfg = tmp_path / "sub" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        yaml.safe_dump(
            {
                "server": {"data_dir": "./data"},
                "accounts": [
                    {"name": "a", "username": "1", "password": "2"},
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    config = load_config(cfg)
    assert Path(config.server.data_dir) == (tmp_path / "sub" / "data").resolve()


def test_example_config_requires_real_api_key():
    """模板里的 Key 是占位符，应当被明确拒绝（提示用户填真实 Key）。"""
    example = Path(__file__).resolve().parent.parent / "config.example.yaml"
    with pytest.raises(ConfigError, match="占位符"):
        load_config(example)


def test_example_config_valid_after_key_replaced(tmp_path):
    import yaml

    example = Path(__file__).resolve().parent.parent / "config.example.yaml"
    raw = yaml.safe_load(example.read_text(encoding="utf-8"))
    raw["answer"]["providers"][0]["key"] = "sk-real-key-1234"
    target = tmp_path / "config.yaml"
    target.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    config = load_config(target)
    assert config.accounts[0].name == "main"
    assert config.answer.provider_names == ["AI"]
