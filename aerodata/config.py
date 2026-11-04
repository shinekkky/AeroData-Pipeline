"""配置加载。

配置来源优先级：显式传入的文件 > 环境变量 > ``config/config.json`` 默认值。
数据库口令只从环境变量或配置文件读取，绝不写进日志。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"


@dataclass(frozen=True)
class DbConfig:
    """数据库连接配置。

    ``dialect`` 为 ``sqlite`` 时使用 ``path``；为 ``mysql`` 时使用
    host/port/user/password/database 五个字段。
    """

    dialect: str = "sqlite"
    path: str = "var/aerodata.db"
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "aerodata"
    charset: str = "utf8mb4"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DbConfig":
        raw = raw or {}
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def public_summary(self) -> dict[str, Any]:
        """用于日志展示的脱敏摘要。"""
        if self.dialect == "sqlite":
            return {"dialect": "sqlite", "path": self.path}
        return {
            "dialect": "mysql",
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "user": self.user,
        }


@dataclass(frozen=True)
class SourceConfig:
    """采集源配置。

    ``type`` 可取 ``simulator`` / ``csv`` / ``mysql``：

    * ``simulator``  —— 基于真实航线清单的确定性仿真源（离线开发与演示用）
    * ``csv``       —— 读取采集器导出的 CSV / JSON 文件
    * ``mysql``     —— 直连采集器的 ``asean_flights_table``
    """

    type: str = "simulator"
    name: str = ""
    routes_file: str = ""
    path: str = ""
    table: str = "asean_flights_table"
    settings_file: str = ""
    settings_key: str = "db_config"
    days: int = 14
    seed: int = 20260101
    defect_rate: float = 0.06

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SourceConfig":
        raw = raw or {}
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass(frozen=True)
class PipelineConfig:
    """流水线整体配置。"""

    source: SourceConfig = field(default_factory=SourceConfig)
    db: DbConfig = field(default_factory=DbConfig)
    reports_dir: str = "reports"
    data_dir: str = "data"
    delay_threshold_min: int = 15
    early_threshold_min: int = 5
    max_flight_duration_min: int = 20 * 60
    min_flight_duration_min: int = 30

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PipelineConfig":
        raw = raw or {}
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in raw.items() if k in known}
        if "source" in kwargs:
            kwargs["source"] = SourceConfig.from_dict(kwargs["source"])
        if "db" in kwargs:
            kwargs["db"] = DbConfig.from_dict(kwargs["db"])
        return cls(**kwargs)


def load_config(path: str | Path | None = None) -> PipelineConfig:
    """读取配置文件并套用环境变量覆盖。"""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))

    raw.setdefault("db", {})
    raw["db"].setdefault("path", "var/aerodata.db")
    raw.setdefault("source", {})
    raw["source"].setdefault("routes_file", "data/reference/routes_daily.json")

    _apply_env(raw)
    config = PipelineConfig.from_dict(raw)
    return _resolve_paths(config)


def _resolve_paths(config: PipelineConfig) -> PipelineConfig:
    """把配置里的相对路径解析为相对项目根目录的绝对路径。"""
    from dataclasses import replace

    db = config.db
    if db.dialect == "sqlite" and not Path(db.path).is_absolute():
        db = replace(db, path=str(PROJECT_ROOT / db.path))

    source = config.source
    changes: dict[str, Any] = {}
    if source.routes_file and not Path(source.routes_file).is_absolute():
        changes["routes_file"] = str(PROJECT_ROOT / source.routes_file)
    if source.path and not Path(source.path).is_absolute():
        changes["path"] = str(PROJECT_ROOT / source.path)
    if source.settings_file and not Path(source.settings_file).is_absolute():
        changes["settings_file"] = str(PROJECT_ROOT / source.settings_file)
    if changes:
        source = replace(source, **changes)

    reports_dir = config.reports_dir
    if reports_dir and not Path(reports_dir).is_absolute():
        reports_dir = str(PROJECT_ROOT / reports_dir)

    return replace(config, db=db, source=source, reports_dir=reports_dir)


def _apply_env(raw: dict[str, Any]) -> None:
    """环境变量覆盖（便于在服务器上切换到 MySQL 而不改动配置文件）。"""
    db = raw["db"]
    if os.getenv("AERODATA_DB_DIALECT"):
        db["dialect"] = os.environ["AERODATA_DB_DIALECT"]
    for env_key, field_name in (
        ("AERODATA_DB_HOST", "host"),
        ("AERODATA_DB_USER", "user"),
        ("AERODATA_DB_PASSWORD", "password"),
        ("AERODATA_DB_NAME", "database"),
    ):
        if os.getenv(env_key):
            db[field_name] = os.environ[env_key]
    if os.getenv("AERODATA_DB_PORT"):
        db["port"] = int(os.environ["AERODATA_DB_PORT"])
    if os.getenv("AERODATA_DB_PATH"):
        db["path"] = os.environ["AERODATA_DB_PATH"]

    src = raw["source"]
    if os.getenv("AERODATA_SOURCE"):
        src["type"] = os.environ["AERODATA_SOURCE"]
