# -*- coding: utf-8 -*-
"""测试 deploy/write_config.py（部署脚本里生成配置的那一步）。"""

from pathlib import Path

import yaml

from deploy.write_config import main as write_config_main


def _read(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_generates_config_from_template(tmp_path):
    target = tmp_path / "config.yaml"
    code = write_config_main(
        [
            "--config",
            str(target),
            "--deepseek-key",
            "sk-deploy-test",
            "--model",
            "deepseek-chat",
            "--data-dir",
            "/var/lib/chaoxing",
            "--web-port",
            "8765",
            "--web-token",
            "tok123",
        ]
    )
    assert code == 0
    config = _read(target)

    assert config["server"]["data_dir"] == "/var/lib/chaoxing"
    assert config["server"]["web_enabled"] is True
    assert config["server"]["web_host"] == "0.0.0.0"
    assert config["server"]["web_port"] == 8765
    assert config["server"]["web_token"] == "tok123"

    provider = config["answer"]["providers"][0]
    assert provider["type"] == "AI"
    assert provider["key"] == "sk-deploy-test"
    assert provider["model"] == "deepseek-chat"
    assert provider["base_url"] == "https://api.deepseek.com/v1"

    # 网页录入账号，所以定时账号可以为空
    assert config["accounts"] == []


def test_config_is_loadable_by_server(tmp_path):
    from server.config import load_config

    target = tmp_path / "config.yaml"
    write_config_main(
        ["--config", str(target), "--deepseek-key", "sk-abc", "--data-dir", str(tmp_path / "data")]
    )
    config = load_config(target)
    assert config.server.web_enabled is True
    assert config.answer.provider_names == ["AI"]


def test_updates_existing_config_and_keeps_accounts(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text(
        yaml.safe_dump(
            {
                "server": {"data_dir": str(tmp_path / "data"), "web_port": 9000},
                "accounts": [
                    {"name": "main", "username": "138", "password": "p", "schedule": "0 8 * * *"}
                ],
                "notify": {"provider": "bark", "url": "https://api.day.app/xxx"},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    write_config_main(
        ["--config", str(target), "--deepseek-key", "sk-new", "--web-port", "8765"]
    )
    config = _read(target)
    assert config["accounts"][0]["name"] == "main"          # 保留已有定时账号
    assert config["notify"]["provider"] == "bark"           # 保留通知配置
    assert config["server"]["web_port"] == 8765             # 更新为新端口
    assert config["answer"]["providers"][0]["key"] == "sk-new"


def test_replaces_placeholder_key(tmp_path):
    target = tmp_path / "config.yaml"
    template = tmp_path / "template.yaml"
    template.write_text(
        yaml.safe_dump(
            {
                "answer": {
                    "providers": [
                        {
                            "type": "AI",
                            "base_url": "https://api.deepseek.com/v1",
                            "key": "sk-替换成你自己的DeepSeek密钥",
                            "model": "deepseek-chat",
                        }
                    ]
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    write_config_main(
        [
            "--config",
            str(target),
            "--template",
            str(template),
            "--deepseek-key",
            "sk-real-key",
        ]
    )
    providers = _read(target)["answer"]["providers"]
    assert len(providers) == 1
    assert providers[0]["key"] == "sk-real-key"


def test_existing_model_is_preserved(tmp_path):
    """重复部署不应把正在跑通的模型改掉（除非显式 --force-model）。"""
    target = tmp_path / "config.yaml"
    target.write_text(
        yaml.safe_dump(
            {
                "answer": {
                    "providers": [
                        {"type": "AI", "base_url": "https://api.deepseek.com/v1",
                         "key": "sk-old", "model": "deepseek-chat"}
                    ]
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    write_config_main(["--config", str(target), "--deepseek-key", "sk-new"])
    provider = _read(target)["answer"]["providers"][0]
    assert provider["key"] == "sk-new"           # key 会更新
    assert provider["model"] == "deepseek-chat"  # 模型保留

    write_config_main(["--config", str(target), "--deepseek-key", "sk-3", "--force-model",
                       "--model", "deepseek-flash"])
    assert _read(target)["answer"]["providers"][0]["model"] == "deepseek-flash"


def test_new_deployment_uses_current_default_model(tmp_path):
    target = tmp_path / "config.yaml"
    write_config_main(["--config", str(target), "--deepseek-key", "sk-abc"])
    table = _read(target)
    assert table["answer"]["providers"][0]["model"] == "deepseek-flash"
    assert table["server"]["auth_enabled"] is True
    assert table["server"]["admin_path"] == "/admin"
    assert table["pricing"]["deepseek-flash"]["cache_miss"] == 2.0
    assert table["server"]["currency"] == "CNY"
