# -*- coding: utf-8 -*-
"""运行时目录布局。

所有可变数据（Cookie、题库缓存、日志、SQLite 状态库）都集中在一个数据目录下，
默认是项目根目录的 ``data/``，可通过 ``CHAOXING_DATA_DIR`` 或配置文件覆盖::

    data/
    ├── state.db                    # SQLite：运行记录 / 账号状态
    └── accounts/
        └── <account>/
            ├── cookies.txt         # 登录态（chaoxing_core 读写的相对路径）
            ├── cache.json          # 题目答案缓存
            ├── chaoxing.log        # 日志
            ├── config.ini          # 由 YAML 渲染出的内部配置
            └── runs/<时间戳>.json  # 每次运行的报告
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

DATA_DIR_ENV = "CHAOXING_DATA_DIR"
_ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


class ConfigError(ValueError):
    """配置文件无法解析或缺少必填项。"""


def sanitize_account_name(name: str) -> str:
    """校验账号名，防止出现路径穿越（``../`` 之类）。"""
    name = (name or "").strip()
    if not name:
        raise ConfigError("账号名不能为空")
    if not _ACCOUNT_NAME_RE.match(name):
        raise ConfigError(
            f"账号名 {name!r} 非法：只允许字母、数字、下划线、点和短横线"
        )
    return name


@dataclass(frozen=True)
class AccountPaths:
    """某个账号在磁盘上的全部路径。"""

    root: Path

    @property
    def cookies(self) -> Path:
        return self.root / "cookies.txt"

    @property
    def cache(self) -> Path:
        return self.root / "cache.json"

    @property
    def log(self) -> Path:
        return self.root / "chaoxing.log"

    @property
    def config_ini(self) -> Path:
        return self.root / "config.ini"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    def ensure(self) -> AccountPaths:
        self.root.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        return self

    def run_report(self, run_id: str) -> Path:
        return self.runs_dir / f"{run_id}.json"


@dataclass(frozen=True)
class DataPaths:
    """整个服务的数据目录。"""

    root: Path

    @property
    def db(self) -> Path:
        return self.root / "state.db"

    @property
    def accounts_dir(self) -> Path:
        return self.root / "accounts"

    def account(self, name: str) -> AccountPaths:
        return AccountPaths(self.accounts_dir / sanitize_account_name(name))

    def ensure(self) -> DataPaths:
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        return self


def resolve_data_dir(explicit: str | os.PathLike | None = None, base: Path | None = None) -> Path:
    """按 显式参数 > 环境变量 > 项目根目录的 data/ 的顺序定位数据目录。"""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env_value = os.environ.get(DATA_DIR_ENV)
    if env_value:
        return Path(env_value).expanduser().resolve()
    root = base or project_root()
    return (root / "data").resolve()


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_data_paths(explicit: str | os.PathLike | None = None) -> DataPaths:
    return DataPaths(resolve_data_dir(explicit)).ensure()
