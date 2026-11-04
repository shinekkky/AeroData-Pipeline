"""MySQL 采集源：直连采集器写入的航班表。

采集器 ``asean_flights.py`` 默认写入 ``asean_flights_table``，列与本项目的
:data:`aerodata.sources.base.RAW_FIELDS` 一一对应（额外带 md5 / insert_time /
jky_clear_flag 等采集侧字段，读取时忽略）。

连接参数来自采集器的 ``settings/db_setting.json``，也支持直接传扁平配置。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from .base import RAW_FIELDS, FlightSource, SourceBatch

try:  # pragma: no cover - 取决于运行环境
    import pymysql
except ImportError:  # pragma: no cover
    pymysql = None  # type: ignore[assignment]

#: 采集表中的原始列
_SELECT_COLUMNS = ", ".join(RAW_FIELDS)


def load_db_settings(settings_file: str | Path, settings_key: str = "db_config") -> dict[str, Any]:
    """读取采集器的数据库配置。

    ``settings_file`` 既可以是 ``db_setting.json``（含 ``db_config`` 等子对象），
    也可以是直接给出 host/user/password/database 的扁平配置。
    """
    path = Path(settings_file)
    if not path.exists():
        raise FileNotFoundError(f"数据库配置文件不存在: {path}")
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(raw.get(settings_key), dict):
        raw = raw[settings_key]
    if "host" not in raw:
        raise ValueError(f"数据库配置缺少 host 字段: {path}")
    return raw


class MySQLCollectorSource(FlightSource):
    """从采集器 MySQL 表读取航班明细。"""

    def __init__(
        self,
        settings_file: str | Path,
        settings_key: str = "db_config",
        table: str = "asean_flights_table",
        name: str | None = None,
    ):
        if pymysql is None:
            raise RuntimeError("未安装 PyMySQL，无法使用 MySQL 采集源")
        self.name = name or f"mysql:{table}"
        self.table = table
        self.settings = load_db_settings(settings_file, settings_key)
        self._conn: Any = None

    def _connect(self) -> Any:
        if self._conn is None:
            self._conn = pymysql.connect(
                host=self.settings["host"],
                port=int(self.settings.get("port", 3306)),
                user=self.settings["user"],
                password=self.settings.get("password", ""),
                database=self.settings.get("database"),
                charset=self.settings.get("charset", "utf8mb4"),
                connect_timeout=10,
                read_timeout=120,
                cursorclass=pymysql.cursors.DictCursor,
            )
        return self._conn

    def fetch(self, since: date | None = None) -> SourceBatch:
        conn = self._connect()
        where = ""
        params: list[Any] = []
        if since is not None:
            where = " WHERE flight_date >= %s"
            params.append(since.isoformat())
        sql = f"SELECT {_SELECT_COLUMNS} FROM `{self.table}`{where} ORDER BY flight_date, flight_no"

        records: list[dict[str, Any]] = []
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for row in cur.fetchall():
                record = {field: ("" if row.get(field) is None else str(row.get(field))) for field in RAW_FIELDS}
                records.append(record)
        return SourceBatch(
            source_name=self.name,
            records=records,
            origin=f"mysql:{self.settings.get('host')}/{self.table}",
        )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
