# -*- coding: utf-8 -*-
"""服务端 YAML 配置的解析、校验与内部 INI 渲染。

对外只暴露一份 ``config.yaml``（支持多账号、定时、通知、题库），
内部再渲染成 ``chaoxing_core`` 认识的传统 ``config.ini``，这样核心代码
保持与上游一致，升级时冲突面最小。
"""

from __future__ import annotations

import configparser
import io
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from server.paths import ConfigError, DataPaths, load_data_paths, sanitize_account_name

# chaoxing_core 支持的题库 provider（与 chaoxing_core.answer.PROVIDER_REGISTRY 对齐）
SUPPORTED_PROVIDERS = {
    "TikuYanxi",
    "TikuGo",
    "TikuLike",
    "TikuAdapter",
    "AI",
    "SiliconFlow",
}

LLM_PROVIDERS = {"AI", "SiliconFlow"}

# YAML 里更易读的 provider 别名
PROVIDER_ALIASES = {
    "ai": "AI",
    "deepseek": "AI",
    "openai": "AI",
    "siliconflow": "SiliconFlow",
    "go": "TikuGo",
    "tikugo": "TikuGo",
    "yanxi": "TikuYanxi",
    "tikuyanxi": "TikuYanxi",
    "like": "TikuLike",
    "tikulike": "TikuLike",
    "adapter": "TikuAdapter",
    "tikuadapter": "TikuAdapter",
}

# provider 内部选项名 -> config.ini 中的键名
PROVIDER_OPTION_MAP: dict[str, dict[str, str]] = {
    "AI": {
        "api_key": "key",
        "key": "key",
        "base_url": "endpoint",
        "endpoint": "endpoint",
        "model": "model",
        "min_interval_seconds": "min_interval_seconds",
        "http_proxy": "http_proxy",
    },
    "SiliconFlow": {
        "api_key": "siliconflow_key",
        "key": "siliconflow_key",
        "base_url": "siliconflow_endpoint",
        "endpoint": "siliconflow_endpoint",
        "model": "siliconflow_model",
        "min_interval_seconds": "min_interval_seconds",
    },
    "TikuGo": {
        "authorization": "go_authorization",
        "min_interval": "go_min_interval",
        "retry_times": "go_retry_times",
        "retry_backoff": "go_retry_backoff",
    },
    "TikuAdapter": {"url": "url"},
    "TikuYanxi": {"tokens": "tokens"},
    "TikuLike": {
        "tokens": "tokens",
        "search": "likeapi_search",
        "vision": "likeapi_vision",
        "model": "likeapi_model",
        "retry": "likeapi_retry",
        "retry_times": "likeapi_retry_times",
    },
}

ALLOWED_NOTIFY_PROVIDERS = {
    "none",
    "serverchan",
    "bark",
    "telegram",
    "webhook",
    "dingtalk",
    "qmsg",
}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _normalize_path(path: str) -> str:
    """后台路径规范化：以 / 开头、不以 / 结尾。"""
    path = (path or "/admin").strip() or "/admin"
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1:
        path = path.rstrip("/")
    return path


def _parse_pricing(raw: Any) -> dict[str, dict[str, float]]:
    """解析配置里的定价表（美元 / 1M tokens）：{模型: {cache_hit, cache_miss, output}}。"""
    if not isinstance(raw, dict):
        return {}
    pricing: dict[str, dict[str, float]] = {}
    for model, price in raw.items():
        if not isinstance(price, dict):
            continue
        entry: dict[str, float] = {}
        for key in ("cache_hit", "cache_miss", "output", "input"):
            if key in price:
                entry[key] = _as_float(price.get(key), 0.0)
        if "input" in entry and "cache_miss" not in entry:
            entry["cache_miss"] = entry["input"]
        if entry:
            pricing[str(model).strip().lower()] = entry
    return pricing


@dataclass
class ProviderConfig:
    """单个题库/AI provider 的配置。"""

    type: str
    options: dict[str, Any] = field(default_factory=dict)

    def normalized_type(self) -> str:
        raw = (self.type or "").strip()
        return PROVIDER_ALIASES.get(raw.lower(), raw)

    def ini_items(self) -> dict[str, str]:
        """把 YAML 选项翻译成 config.ini 的键值对。"""
        type_name = self.normalized_type()
        mapping = PROVIDER_OPTION_MAP.get(type_name, {})
        items: dict[str, str] = {}
        for key, value in (self.options or {}).items():
            if value is None:
                continue
            target = mapping.get(key, key)
            if isinstance(value, bool):
                items[target] = "true" if value else "false"
            elif isinstance(value, (list, tuple)):
                items[target] = ",".join(str(v) for v in value)
            else:
                items[target] = str(value)
        return items


@dataclass
class AnswerConfig:
    """答题相关配置。"""

    submit: bool = False
    cover_rate: float = 0.9
    delay: float = 1.0
    providers: list[ProviderConfig] = field(default_factory=list)
    true_list: list[str] = field(default_factory=lambda: ["正确", "对", "√", "是"])
    false_list: list[str] = field(default_factory=lambda: ["错误", "错", "×", "否", "不对", "不正确"])
    check_llm_connection: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def provider_names(self) -> list[str]:
        return [p.normalized_type() for p in self.providers]

    def merged(self, override: dict[str, Any] | None) -> AnswerConfig:
        """账号级覆盖：未提到的字段沿用全局默认值，providers 整段替换。"""
        if not override:
            return self
        override = dict(override)
        if "providers" in override:
            providers = _providers_from_raw(override.pop("providers"))
        else:
            providers = list(self.providers)

        known = {
            "submit",
            "cover_rate",
            "delay",
            "true_list",
            "false_list",
            "check_llm_connection",
        }
        data: dict[str, Any] = {
            "submit": self.submit,
            "cover_rate": self.cover_rate,
            "delay": self.delay,
            "true_list": list(self.true_list),
            "false_list": list(self.false_list),
            "check_llm_connection": self.check_llm_connection,
            "providers": providers,
            "extra": dict(self.extra),
        }
        for key, value in override.items():
            if key in known:
                data[key] = value
            elif key == "extra":
                data["extra"].update(value or {})
            else:
                data["extra"][key] = value
        return answer_from_dict(data)


@dataclass
class StudyConfig:
    """刷课行为配置。"""

    speed: float = 1.0
    jobs: int = 4
    notopen_action: str = "retry"
    retry_interval: float = 1.0
    work_max_retries: int = 3
    add_learning_count: bool = False
    target_count: int = 100
    auto_sign: bool = False
    skip_finished: bool = True
    include_courses: list[str] = field(default_factory=list)
    exclude_courses: list[str] = field(default_factory=list)
    ignore_courses: list[str] = field(default_factory=list)

    def merged(self, override: dict[str, Any] | None) -> StudyConfig:
        if not override:
            return self
        data = asdict(self)
        data.update({k: v for k, v in override.items() if v is not None})
        return study_from_dict(data)

    def normalized(self) -> StudyConfig:
        self.speed = min(2.0, max(1.0, self.speed))
        self.jobs = max(1, min(16, self.jobs))
        if self.notopen_action not in {"retry", "continue", "ask"}:
            self.notopen_action = "retry"
        return self


@dataclass
class NotifyConfig:
    """消息推送配置。"""

    provider: str = "none"
    url: str = ""
    key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    webhook_url: str = ""
    dingtalk_token: str = ""
    on_success: bool = True
    on_failure: bool = True

    def merged(self, override: dict[str, Any] | None) -> NotifyConfig:
        if not override:
            return self
        data = asdict(self)
        data.update({k: v for k, v in override.items() if v is not None})
        return notify_from_dict(data)


@dataclass
class AccountConfig:
    name: str
    username: str = ""
    password: str = ""
    enabled: bool = True
    use_cookies: bool = False
    schedule: str = ""
    tags: list[str] = field(default_factory=list)
    study: StudyConfig = field(default_factory=StudyConfig)
    answer: AnswerConfig = field(default_factory=AnswerConfig)
    notify: NotifyConfig | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ServerSettings:
    data_dir: str = ""
    log_level: str = "INFO"
    timezone: str = "Asia/Shanghai"
    max_concurrent_accounts: int = 1
    run_timeout_minutes: int = 360
    retry_on_failure: int = 0
    retry_delay_minutes: int = 30
    web_enabled: bool = False
    web_host: str = "127.0.0.1"
    web_port: int = 8765
    web_token: str = ""
    web_max_parallel_tasks: int = 8
    # ── 鉴权与计费 ──
    auth_enabled: bool = True
    admin_path: str = "/admin"
    admin_user: str = "admin"
    admin_password: str = ""
    session_timeout_minutes: int = 720
    credits_per_task: int = 1
    refund_on_failure: bool = True
    usd_to_cny: float = 7.2
    currency: str = "CNY"


@dataclass
class ServerConfig:
    server: ServerSettings
    study: StudyConfig
    answer: AnswerConfig
    notify: NotifyConfig
    accounts: list[AccountConfig]
    source_path: Path | None = None
    pricing: dict[str, dict[str, float]] = field(default_factory=dict)

    def enabled_accounts(self) -> list[AccountConfig]:
        return [acc for acc in self.accounts if acc.enabled]

    def account(self, name: str) -> AccountConfig:
        wanted = sanitize_account_name(name)
        for acc in self.accounts:
            if acc.name == wanted:
                return acc
        raise ConfigError(f"配置中不存在账号 {wanted!r}")

    def data_paths(self) -> DataPaths:
        return load_data_paths(self.server.data_dir or None)


def study_from_dict(data: dict[str, Any]) -> StudyConfig:
    cfg = StudyConfig(
        speed=_as_float(data.get("speed"), 1.0),
        jobs=_as_int(data.get("jobs"), 4),
        notopen_action=str(data.get("notopen_action", "retry") or "retry").strip().lower(),
        retry_interval=_as_float(data.get("retry_interval"), 1.0),
        work_max_retries=_as_int(data.get("work_max_retries"), 3),
        add_learning_count=_as_bool(data.get("add_learning_count")),
        target_count=_as_int(data.get("target_count"), 100),
        auto_sign=_as_bool(data.get("auto_sign")),
        skip_finished=_as_bool(data.get("skip_finished", True)),
        include_courses=_as_list(data.get("include_courses") or data.get("courses")),
        exclude_courses=_as_list(data.get("exclude_courses")),
        ignore_courses=_as_list(data.get("ignore_courses")),
    )
    return cfg.normalized()


def answer_from_dict(data: dict[str, Any], base: AnswerConfig | None = None) -> AnswerConfig:
    raw_providers = data.get("providers")
    if raw_providers is None and base is not None:
        providers = list(base.providers)
    else:
        providers = _providers_from_raw(raw_providers or [])

    true_list = _as_list(data.get("true_list")) or (base.true_list if base else ["正确", "对", "√", "是"])
    false_list = _as_list(data.get("false_list")) or (
        base.false_list if base else ["错误", "错", "×", "否", "不对", "不正确"]
    )

    known = {
        "submit",
        "cover_rate",
        "delay",
        "providers",
        "true_list",
        "false_list",
        "check_llm_connection",
        "extra",
    }
    extra = dict(base.extra) if base else {}
    extra.update({k: v for k, v in (data.get("extra") or {}).items()})
    for key, value in data.items():
        if key not in known:
            extra[key] = value

    return AnswerConfig(
        submit=_as_bool(data.get("submit", base.submit if base else False)),
        cover_rate=_as_float(data.get("cover_rate"), base.cover_rate if base else 0.9),
        delay=_as_float(data.get("delay"), base.delay if base else 1.0),
        providers=providers,
        true_list=true_list,
        false_list=false_list,
        check_llm_connection=_as_bool(
            data.get("check_llm_connection", base.check_llm_connection if base else True)
        ),
        extra=extra,
    )


def notify_from_dict(data: dict[str, Any]) -> NotifyConfig:
    provider = str(data.get("provider", "none") or "none").strip().lower()
    return NotifyConfig(
        provider=provider,
        url=str(data.get("url", "") or ""),
        key=str(data.get("key", "") or ""),
        telegram_bot_token=str(data.get("telegram_bot_token", "") or ""),
        telegram_chat_id=str(data.get("telegram_chat_id", "") or ""),
        webhook_url=str(data.get("webhook_url", "") or ""),
        dingtalk_token=str(data.get("dingtalk_token", "") or ""),
        on_success=_as_bool(data.get("on_success", True)),
        on_failure=_as_bool(data.get("on_failure", True)),
    )


def _resolve_secret(raw: dict[str, Any], *, name: str) -> str:
    """支持 password / password_env / password_file 三种写法。"""
    password = raw.get("password")
    if password:
        return str(password)

    env_name = raw.get("password_env")
    if env_name:
        value = os.environ.get(str(env_name))
        if not value:
            raise ConfigError(
                f"账号 {name!r} 配置了 password_env={env_name!r}，但该环境变量为空"
            )
        return value

    file_path = raw.get("password_file")
    if file_path:
        path = Path(str(file_path)).expanduser()
        if not path.is_file():
            raise ConfigError(f"账号 {name!r} 的 password_file 不存在: {path}")
        return path.read_text(encoding="utf-8").strip()

    return ""


def _providers_from_raw(values: Iterable[Any]) -> list[ProviderConfig]:
    """把 YAML 里两种写法统一成 ProviderConfig 列表。

    * 简写：``- AI``
    * 完整：``- type: AI\\n    key: sk-xxx``
    * 已解析对象：``{"type": "AI", "options": {...}}``
    """
    providers: list[ProviderConfig] = []
    for item in values:
        if isinstance(item, ProviderConfig):
            providers.append(item)
        elif isinstance(item, str):
            providers.append(ProviderConfig(type=item))
        elif isinstance(item, dict):
            item = dict(item)
            ptype = item.pop("type", None) or item.pop("name", None)
            if not ptype:
                raise ConfigError(f"题库 provider 缺少 type 字段: {item!r}")
            options = item.pop("options", None)
            if options is not None:
                if item:
                    raise ConfigError(
                        f"题库 provider 配置里 options 与其它字段混用: {item!r}"
                    )
                if not isinstance(options, dict):
                    raise ConfigError(f"题库 provider 的 options 必须是键值映射: {options!r}")
                item = dict(options)
            providers.append(ProviderConfig(type=str(ptype), options=item))
        else:
            raise ConfigError(f"无法解析的题库 provider 配置: {item!r}")
    return providers


def load_config(path: str | os.PathLike, *, strict: bool = True) -> ServerConfig:
    """读取并校验 ``config.yaml``。"""
    config_path = Path(path).expanduser()
    if not config_path.is_file():
        raise ConfigError(f"找不到配置文件: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - 取决于用户输入
        raise ConfigError(f"配置文件 YAML 解析失败: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是键值映射")

    server_raw = raw.get("server") or {}
    study_raw = raw.get("study") or raw.get("common") or {}
    answer_raw = raw.get("answer") or raw.get("tiku") or {}
    notify_raw = raw.get("notify") or raw.get("notification") or {}
    accounts_raw = raw.get("accounts") or []

    server = ServerSettings(
        data_dir=str(server_raw.get("data_dir", "") or ""),
        log_level=str(server_raw.get("log_level", "INFO") or "INFO").upper(),
        timezone=str(server_raw.get("timezone", "Asia/Shanghai") or "Asia/Shanghai"),
        max_concurrent_accounts=max(1, _as_int(server_raw.get("max_concurrent_accounts"), 1)),
        run_timeout_minutes=max(1, _as_int(server_raw.get("run_timeout_minutes"), 360)),
        retry_on_failure=max(0, _as_int(server_raw.get("retry_on_failure"), 0)),
        retry_delay_minutes=max(0, _as_int(server_raw.get("retry_delay_minutes"), 30)),
        web_enabled=_as_bool(server_raw.get("web_enabled")),
        web_host=str(server_raw.get("web_host", "127.0.0.1") or "127.0.0.1"),
        web_port=_as_int(server_raw.get("web_port"), 8765),
        web_token=str(server_raw.get("web_token", "") or ""),
        web_max_parallel_tasks=max(1, _as_int(server_raw.get("web_max_parallel_tasks"), 8)),
        auth_enabled=_as_bool(server_raw.get("auth_enabled", True)),
        admin_path=_normalize_path(str(server_raw.get("admin_path", "/admin") or "/admin")),
        admin_user=str(server_raw.get("admin_user", "admin") or "admin").strip() or "admin",
        admin_password=str(server_raw.get("admin_password", "") or ""),
        session_timeout_minutes=max(10, _as_int(server_raw.get("session_timeout_minutes"), 720)),
        credits_per_task=max(1, _as_int(server_raw.get("credits_per_task"), 1)),
        refund_on_failure=_as_bool(server_raw.get("refund_on_failure", True)),
        usd_to_cny=_as_float(server_raw.get("usd_to_cny"), 7.2),
        currency=str(server_raw.get("currency", "CNY") or "CNY").strip().upper(),
    )
    if server.data_dir:
        data_dir_path = Path(server.data_dir).expanduser()
        if not data_dir_path.is_absolute():
            # 相对路径以配置文件所在目录为基准，避免受启动时工作目录影响
            data_dir_path = (config_path.parent / data_dir_path).resolve()
        server.data_dir = str(data_dir_path)

    default_study = study_from_dict(study_raw)
    default_answer = answer_from_dict(answer_raw)
    default_notify = notify_from_dict(notify_raw)

    accounts: list[AccountConfig] = []
    seen_names: set[str] = set()
    for index, item in enumerate(accounts_raw):
        if not isinstance(item, dict):
            raise ConfigError(f"accounts[{index}] 必须是键值映射")
        raw_name = item.get("name") or item.get("username") or f"account{index + 1}"
        name = sanitize_account_name(str(raw_name))
        if name in seen_names:
            raise ConfigError(f"账号名重复: {name}")
        seen_names.add(name)

        study = default_study.merged(item.get("study") or item.get("common"))
        answer = default_answer.merged(item.get("answer") or item.get("tiku"))
        notify_override = item.get("notify") or item.get("notification")
        notify = default_notify.merged(notify_override) if notify_override else None

        account = AccountConfig(
            name=name,
            username=str(item.get("username", "") or "").strip(),
            password=_resolve_secret(item, name=name),
            enabled=_as_bool(item.get("enabled", True)),
            use_cookies=_as_bool(item.get("use_cookies")),
            schedule=str(item.get("schedule", "") or "").strip(),
            tags=_as_list(item.get("tags")),
            study=study,
            answer=answer,
            notify=notify,
            raw=item,
        )
        accounts.append(account)

    config = ServerConfig(
        server=server,
        study=default_study,
        answer=default_answer,
        notify=default_notify,
        accounts=accounts,
        source_path=config_path,
        pricing=_parse_pricing(raw.get("pricing")),
    )

    if strict:
        validate_config(config)
    return config


def validate_config(config: ServerConfig) -> None:
    """校验配置，尽量在启动前就把错误暴露出来。"""
    problems: list[str] = []

    if not config.accounts and not config.server.web_enabled:
        problems.append(
            "accounts 为空：要么在配置里加账号，要么打开 server.web_enabled 用网页录入账号"
        )

    # 全局题库配置（网页录入的账号会继承它，因此必须单独校验）
    for provider in config.answer.providers:
        name = provider.normalized_type()
        if name not in SUPPORTED_PROVIDERS:
            problems.append(
                f"全局 answer 配置: 不支持的题库 provider {provider.type!r}，"
                f"可选值: {', '.join(sorted(SUPPORTED_PROVIDERS))}"
            )
            continue
        problems.extend(_validate_provider("全局 answer 配置", provider))

    for account in config.accounts:
        if not account.username and not account.use_cookies:
            problems.append(f"账号 {account.name}: 缺少 username")
        if not account.password and not account.use_cookies:
            problems.append(
                f"账号 {account.name}: 缺少 password（可用 password_env / password_file 提供）"
            )
        if account.schedule:
            problems.extend(_validate_cron(account.name, account.schedule))

        for provider in account.answer.providers:
            name = provider.normalized_type()
            if name not in SUPPORTED_PROVIDERS:
                problems.append(
                    f"账号 {account.name}: 不支持的题库 provider {provider.type!r}，"
                    f"可选值: {', '.join(sorted(SUPPORTED_PROVIDERS))}"
                )
                continue
            problems.extend(_validate_provider(account.name, provider))

        notify = account.notify or config.notify
        if notify.provider not in ALLOWED_NOTIFY_PROVIDERS:
            problems.append(
                f"账号 {account.name}: 不支持的通知 provider {notify.provider!r}，"
                f"可选值: {', '.join(sorted(ALLOWED_NOTIFY_PROVIDERS))}"
            )

    if config.notify.provider not in ALLOWED_NOTIFY_PROVIDERS:
        problems.append(
            f"notify.provider 非法: {config.notify.provider!r}，"
            f"可选值: {', '.join(sorted(ALLOWED_NOTIFY_PROVIDERS))}"
        )

    if problems:
        raise ConfigError("配置校验失败:\n  - " + "\n  - ".join(problems))


def _validate_cron(account: str, expression: str) -> list[str]:
    parts = expression.split()
    if len(parts) != 5:
        return [
            f"账号 {account}: schedule {expression!r} 不是合法的 5 段 cron 表达式"
            f"（分 时 日 月 周），例如 0 8 * * *"
        ]
    return []


def _validate_provider(account: str, provider: ProviderConfig) -> list[str]:
    name = provider.normalized_type()
    options = provider.options or {}
    problems: list[str] = []

    if name == "AI":
        key = str(options.get("key") or options.get("api_key") or "")
        if not key:
            problems.append(f"账号 {account}: AI provider 缺少 key（API Key）")
        elif not key.isascii() or "替换" in key or "你的" in key or key.endswith("xxx"):
            problems.append(
                f"账号 {account}: AI provider 的 key 还是模板占位符，"
                f"请填写真实 API Key（重新运行部署脚本并按提示输入，"
                f"或把 answer.providers 留空关闭答题）"
            )
        if not (options.get("base_url") or options.get("endpoint")):
            problems.append(
                f"账号 {account}: AI provider 缺少 base_url，"
                f"例如 https://api.deepseek.com/v1"
            )
        if not options.get("model"):
            problems.append(f"账号 {account}: AI provider 缺少 model")
    elif name == "SiliconFlow":
        if not (options.get("key") or options.get("api_key")):
            problems.append(f"账号 {account}: SiliconFlow provider 缺少 key")
    elif name in {"TikuYanxi", "TikuLike"}:
        if not options.get("tokens"):
            problems.append(f"账号 {account}: {name} provider 缺少 tokens")
    elif name == "TikuAdapter":
        if not options.get("url"):
            problems.append(f"账号 {account}: TikuAdapter provider 缺少 url")

    return problems


def render_ini(config: ServerConfig, account: AccountConfig) -> str:
    """把服务端配置渲染为 chaoxing_core 使用的 INI 文本。"""
    study = account.study
    answer = account.answer
    notify = account.notify or config.notify

    parser = configparser.ConfigParser()
    parser.optionxform = str  # 保持键名大小写

    common = {
        "use_cookies": "true" if account.use_cookies else "false",
        "username": account.username,
        "password": account.password,
        "course_list": ",".join(study.include_courses),
        "speed": str(study.speed),
        "jobs": str(study.jobs),
        "notopen_action": study.notopen_action,
        "retry_interval": str(study.retry_interval),
        "work_max_retries": str(study.work_max_retries),
        "add_learning_count": "true" if study.add_learning_count else "false",
        "target_count": str(study.target_count),
    }
    parser["common"] = common

    tiku = _render_tiku_section(answer)
    parser["tiku"] = tiku

    parser["notification"] = {
        "provider": _core_notify_provider(notify.provider),
        "url": notify.url or notify.webhook_url,
        "tg_chat_id": notify.telegram_chat_id or "XXXXXX",
    }

    buffer = io.StringIO()
    parser.write(buffer)
    return buffer.getvalue()


def _render_tiku_section(answer: AnswerConfig) -> dict[str, str]:
    providers = answer.provider_names
    # 服务器的定时任务不支持交互式手动答题，TikuManual 会被核心直接忽略
    providers = [p for p in providers if p != "TikuManual"]

    tiku: dict[str, str] = {
        "provider": ",".join(providers),
        "submit": "true" if answer.submit else "false",
        "cover_rate": str(answer.cover_rate),
        "delay": str(answer.delay),
        "check_llm_connection": "true" if answer.check_llm_connection else "false",
        "true_list": ",".join(answer.true_list),
        "false_list": ",".join(answer.false_list),
        # 服务端无人值守，手动答题模式永远关闭
        "manual_mode_default": "batch",
        "manual_mode_separator": ";",
    }

    for provider in answer.providers:
        if provider.normalized_type() == "TikuManual":
            continue
        for key, value in provider.ini_items().items():
            if value == "":
                continue
            tiku.setdefault(key, value)

    # 核心代码里 `min_interval_seconds` 只读取一次，多个 provider 共用同一键，
    # 这里保证键存在，避免 AI provider 在 init 阶段 KeyError。
    tiku.setdefault("min_interval_seconds", "3")
    for key in ("tokens", "url", "go_authorization", "http_proxy", "siliconflow_key"):
        tiku.setdefault(key, "")

    for key, value in (answer.extra or {}).items():
        if isinstance(value, bool):
            tiku[key] = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            tiku[key] = ",".join(str(v) for v in value)
        else:
            tiku[key] = str(value)

    return {k: v for k, v in tiku.items() if v is not None}


def _core_notify_provider(name: str) -> str:
    return {
        "serverchan": "ServerChan",
        "qmsg": "Qmsg",
        "bark": "Bark",
        "telegram": "Telegram",
    }.get(name, "ServerChan")
