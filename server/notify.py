# -*- coding: utf-8 -*-
"""运行结果消息推送。

统一入口 :func:`send`, 支持 Server 酱 / Bark / Telegram / 钉钉 / Qmsg / 自定义 Webhook。
推送失败只记录日志，绝不打断刷课流程。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

from server.config import NotifyConfig

logger = logging.getLogger("chaoxing.notify")

DEFAULT_TIMEOUT = 10


@dataclass
class NotifyResult:
    ok: bool
    provider: str
    detail: str = ""


def send(config: NotifyConfig | None, title: str, content: str) -> NotifyResult:
    """按配置推送一条消息；provider 为 none 时直接跳过。"""
    if config is None or config.provider in {"", "none"}:
        return NotifyResult(True, "none", "未启用通知")

    try:
        return _dispatch(config, title, content)
    except Exception as exc:  # noqa: BLE001 - 推送不允许影响主流程
        logger.warning("通知推送失败 (%s): %s", config.provider, exc)
        return NotifyResult(False, config.provider, str(exc))


def _dispatch(config: NotifyConfig, title: str, content: str) -> NotifyResult:
    provider = config.provider
    if provider == "serverchan":
        return _serverchan(config, title, content)
    if provider == "bark":
        return _bark(config, title, content)
    if provider == "telegram":
        return _telegram(config, title, content)
    if provider == "dingtalk":
        return _dingtalk(config, title, content)
    if provider == "qmsg":
        return _qmsg(config, title, content)
    if provider == "webhook":
        return _webhook(config, title, content)
    return NotifyResult(False, provider, f"未知的通知 provider: {provider}")


def _post(url: str, **kwargs: Any) -> requests.Response:
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    response = requests.post(url, **kwargs)
    return response


def _serverchan(config: NotifyConfig, title: str, content: str) -> NotifyResult:
    key = config.key or _key_from_url(config.url, prefix="https://sctapi.ftqq.com/")
    if not key:
        return NotifyResult(False, "serverchan", "缺少 key（SendKey）")
    url = config.url or f"https://sctapi.ftqq.com/{key}.send"
    response = _post(url, data={"title": title, "desp": content})
    return NotifyResult(response.ok, "serverchan", response.text[:200])


def _bark(config: NotifyConfig, title: str, content: str) -> NotifyResult:
    url = config.url or config.webhook_url
    if not url:
        return NotifyResult(False, "bark", "缺少 url")
    response = _post(
        url.rstrip("/"),
        json={"title": title, "body": content, "group": "超星学习通"},
    )
    return NotifyResult(response.ok, "bark", response.text[:200])


def _telegram(config: NotifyConfig, title: str, content: str) -> NotifyResult:
    token = config.telegram_bot_token or config.key
    chat_id = config.telegram_chat_id
    if not token or not chat_id:
        return NotifyResult(False, "telegram", "缺少 bot token 或 chat_id")
    url = config.url or f"https://api.telegram.org/bot{token}/sendMessage"
    response = _post(url, json={"chat_id": chat_id, "text": f"{title}\n{content}"})
    return NotifyResult(response.ok, "telegram", response.text[:200])


def _dingtalk(config: NotifyConfig, title: str, content: str) -> NotifyResult:
    token = config.dingtalk_token or config.key
    if not token:
        return NotifyResult(False, "dingtalk", "缺少 access_token")
    url = config.url or f"https://oapi.dingtalk.com/robot/send?access_token={token}"
    response = _post(
        url,
        json={"msgtype": "text", "text": {"content": f"{title}\n{content}"}},
    )
    return NotifyResult(response.ok, "dingtalk", response.text[:200])


def _qmsg(config: NotifyConfig, title: str, content: str) -> NotifyResult:
    url = config.url or config.webhook_url
    if not url:
        return NotifyResult(False, "qmsg", "缺少 url")
    response = _post(url, data={"msg": f"{title}\n{content}"})
    return NotifyResult(response.ok, "qmsg", response.text[:200])


def _webhook(config: NotifyConfig, title: str, content: str) -> NotifyResult:
    url = config.webhook_url or config.url
    if not url:
        return NotifyResult(False, "webhook", "缺少 webhook_url")
    response = _post(url, json={"title": title, "content": content})
    return NotifyResult(response.ok, "webhook", response.text[:200])


def _key_from_url(url: str, prefix: str) -> str:
    if url.startswith(prefix) and url.endswith(".send"):
        return url[len(prefix) : -len(".send")]
    return ""
