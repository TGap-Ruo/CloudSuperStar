# -*- coding: utf-8 -*-
"""图片资料题的视觉答题支持。

超星有些章节检测的"题目"只有一张图片（题型代码 10），纯文本模型只能看到
``<img src="...">`` 标签文字，会回答"无法作答"→ 提交 → 0 分。
现在会把图片抓下来转成 data URI，交给支持视觉的模型（deepseek-flash）直接看图作答。
"""

import json
from types import SimpleNamespace

import pytest

import chaoxing_core.answer as answer_module
from chaoxing_core.answer import (
    AI,
    extract_image_urls,
    fetch_image_data_uri,
    model_supports_vision,
)

IMAGE_HTML = '【资料题】<img src="https://p.ananas.chaoxing.com/star3/origin/abc.png" alt="题图">'


def _conf(model="deepseek-flash", vision="true"):
    return {
        "provider": "AI",
        "submit": "true",
        "cover_rate": "0.6",
        "true_list": "对",
        "false_list": "错",
        "endpoint": "https://api.deepseek.com/v1",
        "key": "sk-test",
        "model": model,
        "min_interval_seconds": "0",
        "http_proxy": "",
        "check_llm_connection": "false",
        "vision": vision,
    }


@pytest.fixture
def fake_vision(monkeypatch):
    """假 OpenAI + 假图片下载，并记录发给模型的消息。"""
    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"Answer": ["A"]})))]
            )

    class FakeOpenAI:
        def __init__(self, *args, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    def fake_get(url, **kwargs):
        return SimpleNamespace(
            status_code=200,
            content=b"\x89PNG\r\n\x1a\n-fake-image-bytes",
            headers={"Content-Type": "image/png"},
        )

    monkeypatch.setattr(answer_module, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(answer_module.requests, "get", fake_get)
    # 清掉图片缓存，避免用例之间互相影响
    answer_module._IMAGE_CACHE.clear()
    return captured


def test_model_supports_vision():
    assert model_supports_vision("deepseek-flash") is True
    assert model_supports_vision("deepseek-v4-flash") is True
    assert model_supports_vision("qwen-vl-max") is True
    assert model_supports_vision("gpt-4o-vision") is True
    # 明确不支持视觉的模型
    assert model_supports_vision("deepseek-v4-pro") is False
    assert model_supports_vision("deepseek-reasoner") is False
    assert model_supports_vision("") is False


def test_extract_image_urls():
    assert extract_image_urls(IMAGE_HTML) == ["https://p.ananas.chaoxing.com/star3/origin/abc.png"]
    assert extract_image_urls("普通文字题目") == []
    assert extract_image_urls("") == []
    # 去重
    assert extract_image_urls(IMAGE_HTML + IMAGE_HTML) == [
        "https://p.ananas.chaoxing.com/star3/origin/abc.png"
    ]


def test_vision_model_gets_image(fake_vision):
    ai = AI()
    ai.config_set(_conf("deepseek-flash"))
    ai.init_tiku()

    ai._query({"title": IMAGE_HTML, "options": "", "type": "single"})

    content = fake_vision["messages"][-1]["content"]
    assert isinstance(content, list), "图片题应该发多段内容"
    assert content[0]["type"] == "text"
    assert "题目见附图" in content[0]["text"]      # <img> 标签被替换成提示
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_text_only_model_stays_text(fake_vision):
    """deepseek-v4-pro 不支持视觉 → 不应附带图片（否则接口会 400）。"""
    ai = AI()
    ai.config_set(_conf("deepseek-v4-pro"))
    ai.init_tiku()

    ai._query({"title": IMAGE_HTML, "options": "", "type": "single"})

    content = fake_vision["messages"][-1]["content"]
    assert isinstance(content, str)
    assert "img" in content          # 原样保留 <img> 标签文本


def test_vision_disabled_by_config(fake_vision):
    ai = AI()
    ai.config_set(_conf("deepseek-flash", vision="false"))
    ai.init_tiku()

    ai._query({"title": IMAGE_HTML, "options": "", "type": "single"})

    assert isinstance(fake_vision["messages"][-1]["content"], str)


def test_image_cached_and_download_failure_is_safe(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, **kwargs):
        calls["n"] += 1
        return SimpleNamespace(status_code=200, content=b"x" * 10, headers={"Content-Type": "image/png"})

    monkeypatch.setattr(answer_module.requests, "get", fake_get)
    answer_module._IMAGE_CACHE.clear()
    first = fetch_image_data_uri("https://example.com/a.png")
    second = fetch_image_data_uri("https://example.com/a.png")
    assert first == second
    assert calls["n"] == 1, "同一张图应该复用缓存"

    def boom(url, **kwargs):
        raise RuntimeError("网络挂了")

    monkeypatch.setattr(answer_module.requests, "get", boom)
    assert fetch_image_data_uri("https://example.com/b.png") is None
