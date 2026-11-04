"""采集源抽象。

流水线只认这个接口，因此"数据从哪来"是可替换的：

* :mod:`aerodata.sources.mysql_collector` —— 直连采集器写入的 MySQL 表（生产）
* :mod:`aerodata.sources.csv_files`      —— 采集器导出的 CSV / JSON（离线交付）
* :mod:`aerodata.sources.simulator`      —— 基于真实航线清单的确定性仿真（开发演示）
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any

#: 采集源统一的原始字段（与采集器 asean_flights_table 的列一一对应）
RAW_FIELDS: tuple[str, ...] = (
    "flight_no",
    "airline",
    "dep_country",
    "dep_city",
    "dep_airport",
    "dep_plan",
    "dep_actual",
    "arr_country",
    "arr_city",
    "arr_province",
    "arr_airport",
    "arr_plan",
    "arr_actual",
    "status",
    "flight_date",
)


@dataclass
class SourceBatch:
    """一次采集的结果。"""

    source_name: str
    records: list[dict[str, Any]] = field(default_factory=list)
    origin: str = ""

    @property
    def row_count(self) -> int:
        return len(self.records)

    def fingerprint(self) -> str:
        """源指纹：记录数 + 首末记录的哈希，用于批次日志与幂等判断。"""
        if not self.records:
            return "empty"
        head = hashlib.sha256(repr(sorted(self.records[0].items())).encode()).hexdigest()[:16]
        tail = hashlib.sha256(repr(sorted(self.records[-1].items())).encode()).hexdigest()[:16]
        return f"{len(self.records)}:{head}:{tail}"


class FlightSource(ABC):
    """航班数据采集源。"""

    #: 写入 ODS 的 source_name 标识
    name: str = "base"

    @abstractmethod
    def fetch(self, since: date | None = None) -> SourceBatch:
        """拉取原始航班记录。``since`` 为增量起始日期，None 表示全量。"""

    def close(self) -> None:  # pragma: no cover - 默认无需释放资源
        return None
