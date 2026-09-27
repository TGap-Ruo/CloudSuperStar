#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 / 更新部署用的 config.yaml（供 deploy/install.sh 调用）。

特点：

* 首次部署以仓库里的 ``config.example.yaml`` 为模板，并写入适合服务器的默认值；
* 重复部署只更新指定字段（DeepSeek Key、Web 配置），保留用户已改的账号、通知等；
* 保证 ``answer.providers`` 里存在一个 AI provider，且带上本次传入的 Key，
  实现"部署时附上 DeepSeek API Key 即可一键做题"。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml


def ensure_ai_provider(
    answer: dict[str, Any], *, api_key: str, model: str, base_url: str
) -> None:
    providers = answer.get("providers")
    if not isinstance(providers, list):
        providers = []

    target = None
    for item in providers:
        if not isinstance(item, dict):
            continue
        name = str(item.get("type", "")).strip().lower()
        if name in {"ai", "deepseek", "openai"}:
            target = item
            break

    if target is None:
        target = {"type": "AI"}
        providers.insert(0, target)

    target["type"] = "AI"
    target["base_url"] = base_url
    target["key"] = api_key
    target["model"] = model
    target.setdefault("min_interval_seconds", 3)
    answer["providers"] = providers


def build_config(
    template: dict[str, Any] | None,
    *,
    data_dir: str,
    api_key: str,
    model: str,
    base_url: str,
    web_host: str,
    web_port: int,
    web_token: str,
    web_max_parallel: int,
    timezone: str,
    run_timeout_minutes: int,
    default_submit: bool,
    reset_accounts: bool = False,
) -> dict[str, Any]:
    config: dict[str, Any] = dict(template or {})

    server = dict(config.get("server") or {})
    server.update(
        {
            "data_dir": data_dir,
            "timezone": timezone,
            "web_enabled": True,
            "web_host": web_host,
            "web_port": web_port,
            "web_token": web_token,
            "web_max_parallel_tasks": web_max_parallel,
            "run_timeout_minutes": run_timeout_minutes,
        }
    )
    server.setdefault("log_level", "INFO")
    server.setdefault("max_concurrent_accounts", 1)
    server.setdefault("retry_on_failure", 1)
    server.setdefault("retry_delay_minutes", 30)
    config["server"] = server

    answer = dict(config.get("answer") or {})
    answer.setdefault("submit", default_submit)
    answer.setdefault("cover_rate", 0.6)
    answer.setdefault("delay", 1.0)
    answer.setdefault("check_llm_connection", True)
    ensure_ai_provider(answer, api_key=api_key, model=model, base_url=base_url)
    config["answer"] = answer

    config.setdefault("study", {})
    config.setdefault("notify", {"provider": "none"})
    # 首次部署时清空模板里的示例账号：账号在网页上录入，
    # 需要定时任务时再往这里加（格式见 config.example.yaml）。
    if reset_accounts or not isinstance(config.get("accounts"), list):
        config["accounts"] = []
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成/更新超星服务端 config.yaml")
    parser.add_argument("--config", required=True, help="目标配置文件路径")
    parser.add_argument("--template", default=None, help="模板路径（默认仓库 config.example.yaml）")
    parser.add_argument("--deepseek-key", required=True)
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--base-url", default="https://api.deepseek.com/v1")
    parser.add_argument("--data-dir", default="/var/lib/chaoxing")
    parser.add_argument("--web-host", default="0.0.0.0")
    parser.add_argument("--web-port", type=int, default=8765)
    parser.add_argument("--web-token", default="")
    parser.add_argument("--web-max-parallel", type=int, default=8)
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--run-timeout-minutes", type=int, default=360)
    parser.add_argument(
        "--default-submit",
        default="true",
        choices=["true", "false"],
        help="网页新建任务默认是否自动提交答案",
    )
    args = parser.parse_args(argv)

    config_path = Path(args.config).expanduser()
    if config_path.is_file():
        try:
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            print(f"现有配置文件解析失败: {exc}", file=sys.stderr)
            return 1
        template = loaded if isinstance(loaded, dict) else {}
        source = str(config_path)
        reset_accounts = False
    else:
        template_path = (
            Path(args.template).expanduser()
            if args.template
            else Path(__file__).resolve().parent.parent / "config.example.yaml"
        )
        if template_path.is_file():
            template = yaml.safe_load(template_path.read_text(encoding="utf-8")) or {}
            source = str(template_path)
        else:
            template = {}
            source = "内置默认值"
        reset_accounts = True

    config = build_config(
        template,
        data_dir=args.data_dir,
        api_key=args.deepseek_key.strip(),
        model=args.model.strip(),
        base_url=args.base_url.strip(),
        web_host=args.web_host,
        web_port=args.web_port,
        web_token=args.web_token,
        web_max_parallel=args.web_max_parallel,
        timezone=args.timezone,
        run_timeout_minutes=args.run_timeout_minutes,
        default_submit=args.default_submit == "true",
        reset_accounts=reset_accounts,
    )

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    print(f"配置已写入 {config_path}（模板来源: {source}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
