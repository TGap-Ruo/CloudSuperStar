# -*- coding: utf-8 -*-
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def restore_cwd():
    """runner 会 chdir 到账号目录，测试结束后恢复，避免相互影响。"""
    original = os.getcwd()
    try:
        yield
    finally:
        os.chdir(original)


@pytest.fixture
def config_dict(tmp_path: Path) -> dict:
    return {
        "server": {
            "data_dir": str(tmp_path / "data"),
            "log_level": "WARNING",
            "timezone": "Asia/Shanghai",
            "max_concurrent_accounts": 1,
            "run_timeout_minutes": 30,
            "retry_on_failure": 0,
        },
        "study": {
            "speed": 1.0,
            "jobs": 2,
            "notopen_action": "continue",
            "retry_interval": 0.0,
            "work_max_retries": 2,
        },
        "answer": {
            "submit": True,
            "cover_rate": 0.5,
            "delay": 0.0,
            "check_llm_connection": False,
            "providers": [
                {
                    "type": "AI",
                    "base_url": "https://api.deepseek.com/v1",
                    "key": "sk-test",
                    "model": "deepseek-chat",
                    "min_interval_seconds": 0,
                }
            ],
        },
        "notify": {"provider": "none"},
        "accounts": [
            {
                "name": "tester",
                "username": "13800000000",
                "password": "secret",
                "enabled": True,
                "schedule": "0 8 * * *",
            }
        ],
    }


@pytest.fixture
def config_path(tmp_path: Path, config_dict: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config_dict, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture
def server_config(config_path: Path):
    from server.config import load_config

    return load_config(config_path)


def fake_openai_factory(resolver):
    """构造可注入 chaoxing_core.answer.OpenAI 的假客户端。"""

    class FakeCompletions:
        def create(self, **kwargs):
            system = kwargs["messages"][0]["content"]
            user = kwargs["messages"][-1]["content"]
            answers = resolver(system, user)
            content = json.dumps({"Answer": answers}, ensure_ascii=False)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, *args, **kwargs):
            self.chat = FakeChat()

    return FakeOpenAI


@pytest.fixture
def fake_openai(monkeypatch):
    """默认假 AI：单选答“北京”、判断答“正确”、多选答“Python/Java”。"""

    def resolver(system: str, user: str):
        if "判断题" in system:
            return ["正确"]
        if "多选题" in system:
            return ["Python", "Java"]
        if "单选题" in system:
            return ["北京"]
        return ["答案"]

    import chaoxing_core.answer as answer_module

    monkeypatch.setattr(answer_module, "OpenAI", fake_openai_factory(resolver))
    return resolver
