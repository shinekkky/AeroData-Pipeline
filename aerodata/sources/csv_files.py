"""CSV / JSON 文件采集源。

对接采集器离线交付的明细文件（导出成 CSV 或 JSON 都行），列名支持中英文别名，
未识别的列忽略，缺失的列留空。
"""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path
from typing import Any

from ..etl.standards import normalize_text
from .base import RAW_FIELDS, FlightSource, SourceBatch

#: 原始字段的列名别名（小写比较）
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "flight_no": ("flight_no", "flight_number", "flightnumber", "航班号", "航班"),
    "airline": ("airline", "airline_name", "carrier", "航空公司", "承运人"),
    "dep_country": ("dep_country", "departure_country", "origin_country", "出发国家", "起飞机场所在国家"),
    "dep_city": ("dep_city", "departure_city", "origin_city", "出发城市"),
    "dep_airport": ("dep_airport", "departure_airport", "dep_airport_name", "origin", "出发机场"),
    "dep_plan": ("dep_plan", "scheduled_departure", "dep_scheduled", "计划起飞", "计划起飞时间"),
    "dep_actual": ("dep_actual", "actual_departure", "实际起飞", "实际起飞时间"),
    "arr_country": ("arr_country", "arrival_country", "destination_country", "到达国家"),
    "arr_city": ("arr_city", "arrival_city", "destination_city", "到达城市"),
    "arr_province": ("arr_province", "arrival_province", "到达省份"),
    "arr_airport": ("arr_airport", "arrival_airport", "arr_airport_name", "destination", "到达机场"),
    "arr_plan": ("arr_plan", "scheduled_arrival", "arr_scheduled", "计划到达", "计划到达时间"),
    "arr_actual": ("arr_actual", "actual_arrival", "实际到达", "实际到达时间"),
    "status": ("status", "flight_status", "状态", "航班状态"),
    "flight_date": ("flight_date", "date", "flightday", "日期", "航班日期"),
}

_LOOKUP: dict[str, str] = {}
for _canonical, _aliases in COLUMN_ALIASES.items():
    for _alias in _aliases:
        _LOOKUP[_alias.lower().replace(" ", "").replace("_", "")] = _canonical


def _canonical_key(name: str) -> str | None:
    return _LOOKUP.get(str(name).strip().lower().replace(" ", "").replace("_", ""))


def _normalize_record(raw: dict[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = {field: "" for field in RAW_FIELDS}
    for key, value in raw.items():
        canonical = _canonical_key(key)
        if canonical:
            record[canonical] = normalize_text(value)
    return record


def _read_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        for key in ("data", "rows", "records", "flights", "result"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    return [row for row in data if isinstance(row, dict)]


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


class CsvFileSource(FlightSource):
    """从目录或单个文件读取航班明细。"""

    def __init__(self, path: str | Path, name: str = "csv"):
        self.name = name
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"数据文件不存在: {self.path}")

    def _files(self) -> list[Path]:
        if self.path.is_dir():
            return sorted(
                p for p in self.path.iterdir() if p.suffix.lower() in {".csv", ".json"}
            )
        return [self.path]

    def fetch(self, since: date | None = None) -> SourceBatch:
        records: list[dict[str, Any]] = []
        used: list[str] = []
        for file_path in self._files():
            if file_path.suffix.lower() == ".csv":
                rows = _read_csv(file_path)
            else:
                rows = _read_json(file_path)
            for row in rows:
                record = _normalize_record(row)
                if since is not None:
                    flight_date = record.get("flight_date", "")[:10]
                    try:
                        if date.fromisoformat(flight_date) < since:
                            continue
                    except ValueError:
                        pass
                records.append(record)
            used.append(file_path.name)
        return SourceBatch(
            source_name=self.name,
            records=records,
            origin=f"files:{','.join(used)}" if used else "files:none",
        )
