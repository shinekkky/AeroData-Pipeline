"""ODS → DWD 清洗加工。

一条原始记录进来，出去的是：

* 一行字段标准化、维表关联、延误计算完成的 DWD 明细
* 一组质量异常（清洗过程中发现的、改不好的问题）

清洗只做"能确定地修正"的事：航班号去空格统一大写、机场名挂到维表编码、
时间补成完整 datetime、延误算成分钟。改不了的（机场查不到、时间 25:00）
原样保留并记为异常，绝不静默丢弃。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ..db.engine import Database
from ..quality.rules import Anomaly, RULES_BY_CODE
from .standards import (
    build_flight_id,
    classify_delay,
    compute_delay_min,
    compute_flight_duration_min,
    is_valid_flight_no,
    normalize_airline_key,
    normalize_airport_key,
    normalize_flight_no,
    normalize_status,
    normalize_text,
    parse_flight_time,
)


@dataclass
class DimensionIndex:
    """维表内存索引。"""

    airports: dict[str, dict[str, Any]] = field(default_factory=dict)
    airport_by_code: dict[str, dict[str, Any]] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    airlines: dict[str, dict[str, Any]] = field(default_factory=dict)
    airline_by_code: dict[str, dict[str, Any]] = field(default_factory=dict)
    routes: dict[str, dict[str, Any]] = field(default_factory=dict)
    country_codes: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, db: Database) -> "DimensionIndex":
        index = cls()
        for row in db.fetch_all("SELECT * FROM dim_airport"):
            index.airports[str(row["airport_name_norm"])] = row
            index.airport_by_code[str(row["airport_code"])] = row
            country = str(row.get("country") or "")
            code = str(row.get("country_code") or "")
            if country and code:
                index.country_codes.setdefault(country, code)
        for row in db.fetch_all("SELECT * FROM dim_airport_alias"):
            index.aliases[str(row["alias_name_norm"])] = str(row["airport_code"])
        for row in db.fetch_all("SELECT * FROM dim_airline"):
            index.airlines[str(row["airline_name_norm"])] = row
            index.airline_by_code[str(row["airline_code"])] = row
        for row in db.fetch_all("SELECT * FROM dim_route"):
            key = f"{row['dep_airport_code']}|{row['arr_airport_code']}"
            index.routes[key] = row
        return index

    def match_airport(self, raw_name: str) -> dict[str, Any] | None:
        """按名称匹配机场：先查别名表，再查机场主表。"""
        key = normalize_airport_key(raw_name)
        if not key:
            return None
        code = self.aliases.get(key)
        if code:
            return self.airport_by_code.get(code)
        return self.airports.get(key)

    def match_airline(self, raw_name: str) -> dict[str, Any] | None:
        key = normalize_airline_key(raw_name)
        if not key:
            return None
        return self.airlines.get(key)

    def match_route(self, dep_code: str, arr_code: str) -> dict[str, Any] | None:
        return self.routes.get(f"{dep_code}|{arr_code}")


@dataclass
class CleanResult:
    """一条记录的清洗结果。"""

    row: dict[str, Any]
    anomalies: list[Anomaly] = field(default_factory=list)


class FlightTransformer:
    """把 ODS 原始记录加工成 DWD 明细。"""

    def __init__(
        self,
        dims: DimensionIndex,
        delay_threshold_min: int = 15,
        early_threshold_min: int = 5,
        max_duration_min: int = 20 * 60,
        default_date: date | None = None,
    ):
        self.dims = dims
        self.delay_threshold_min = delay_threshold_min
        self.early_threshold_min = early_threshold_min
        self.max_duration_min = max_duration_min
        self.default_date = default_date or date.today()

    # -- 主流程 -----------------------------------------------------------
    def transform(self, raw: dict[str, Any], batch_id: str = "") -> CleanResult:
        anomalies: list[Anomaly] = []
        time_notes: dict[str, dict[str, str]] = {}

        # 1) 航班日期
        raw_date = normalize_text(raw.get("flight_date"))
        flight_date = self._parse_date(raw_date)
        if flight_date is None:
            flight_date = self.default_date
            anomalies.append(
                self._anomaly(
                    "MISSING_REQUIRED_FIELD",
                    "航班日期缺失或不可解析，已回退到采集日期",
                    field_name="flight_date",
                    raw_value=raw_date,
                    flight_no=normalize_flight_no(raw.get("flight_no")),
                    stat_date=flight_date,
                )
            )

        # 2) 航班号
        flight_no = normalize_flight_no(raw.get("flight_no"))
        flight_no_raw = normalize_text(raw.get("flight_no"))

        # 3) 航空公司
        airline_raw = normalize_text(raw.get("airline"))
        airline_row = self.dims.match_airline(airline_raw)
        airline_id = str(airline_row["airline_code"]) if airline_row else ""
        airline_name = str(airline_row["airline_name"]) if airline_row else airline_raw

        # 4) 出发 / 到达机场
        dep_raw = normalize_text(raw.get("dep_airport"))
        arr_raw = normalize_text(raw.get("arr_airport"))
        dep_airport = self.dims.match_airport(dep_raw)
        arr_airport = self.dims.match_airport(arr_raw)

        dep_code = str(dep_airport["airport_code"]) if dep_airport else ""
        arr_code = str(arr_airport["airport_code"]) if arr_airport else ""
        dep_name = str(dep_airport["airport_name"]) if dep_airport else dep_raw
        arr_name = str(arr_airport["airport_name"]) if arr_airport else arr_raw

        # 5) 航线
        route_row = self.dims.match_route(dep_code, arr_code)
        route_id = str(route_row["route_id"]) if route_row else ""

        # 6) 时间
        sched_dep = self._parse_time(raw.get("dep_plan"), flight_date, "scheduled_dep_time", time_notes)
        actual_dep = self._parse_time(raw.get("dep_actual"), flight_date, "actual_dep_time", time_notes)
        sched_arr = self._parse_time(raw.get("arr_plan"), flight_date, "scheduled_arr_time", time_notes)
        actual_arr = self._parse_time(raw.get("arr_actual"), flight_date, "actual_arr_time", time_notes)

        # 7) 延误与时长
        dep_delay = compute_delay_min(sched_dep, actual_dep)
        arr_delay = compute_delay_min(sched_arr, actual_arr)
        duration = compute_flight_duration_min(sched_dep, sched_arr)

        # 8) 状态
        status = normalize_status(raw.get("status"))
        delay_status = classify_delay(
            dep_delay, status.code, self.delay_threshold_min, self.early_threshold_min
        )

        row: dict[str, Any] = {
            "flight_id": build_flight_id(flight_no, flight_date, dep_code, arr_code, sched_dep),
            "batch_id": batch_id,
            "flight_date": flight_date.isoformat(),
            "flight_no": flight_no,
            "flight_no_raw": flight_no_raw,
            "airline_id": airline_id,
            "airline_name": airline_name,
            "route_id": route_id,
            "dep_airport_code": dep_code,
            "dep_airport_name": dep_name,
            "dep_city": str(dep_airport.get("city", "")) if dep_airport else normalize_text(raw.get("dep_city")),
            "dep_country": str(dep_airport.get("country", "")) if dep_airport else normalize_text(raw.get("dep_country")),
            "dep_country_code": str(dep_airport.get("country_code", "")) if dep_airport else "",
            "arr_airport_code": arr_code,
            "arr_airport_name": arr_name,
            "arr_city": str(arr_airport.get("city", "")) if arr_airport else normalize_text(raw.get("arr_city")),
            "arr_country": str(arr_airport.get("country", "")) if arr_airport else normalize_text(raw.get("arr_country")),
            "arr_country_code": str(arr_airport.get("country_code", "")) if arr_airport else "",
            "scheduled_dep_time": sched_dep.strftime("%Y-%m-%d %H:%M:%S") if sched_dep else None,
            "actual_dep_time": actual_dep.strftime("%Y-%m-%d %H:%M:%S") if actual_dep else None,
            "scheduled_arr_time": sched_arr.strftime("%Y-%m-%d %H:%M:%S") if sched_arr else None,
            "actual_arr_time": actual_arr.strftime("%Y-%m-%d %H:%M:%S") if actual_arr else None,
            "dep_delay_min": dep_delay,
            "arr_delay_min": arr_delay,
            "delay_status": delay_status,
            "flight_status": status.code,
            "flight_status_raw": normalize_text(raw.get("status")),
            "flight_duration_min": duration.minutes,
            "has_anomaly": 0,
            "created_at": "",
            "updated_at": "",
        }

        # 诊断信息（下划线开头，入库前会被剔除）
        row["_time_notes"] = time_notes
        row["_dep_airport_raw"] = dep_raw
        row["_arr_airport_raw"] = arr_raw
        row["_airline_raw"] = airline_raw
        row["_dep_country_raw"] = normalize_text(raw.get("dep_country"))
        row["_arr_country_raw"] = normalize_text(raw.get("arr_country"))
        row["_country_lookup"] = dict(self.dims.country_codes)
        row["_duration_limit_min"] = self.max_duration_min
        row["_status_error"] = status.error

        # 航班号前缀是否指向已知航司
        prefix, prefix_known = self._resolve_airline_prefix(flight_no)
        row["_airline_prefix"] = prefix
        row["_known_airline_prefix"] = prefix_known

        # 状态本身异常（缺失/无法识别）
        if status.error == "missing":
            anomalies.append(
                self._anomaly(
                    "MISSING_TIME",
                    "航班状态缺失",
                    field_name="status",
                    raw_value=normalize_text(raw.get("status")),
                    flight_no=flight_no,
                    stat_date=flight_date,
                )
            )
        elif status.error == "unknown":
            anomalies.append(
                self._anomaly(
                    "MISSING_REQUIRED_FIELD",
                    f"航班状态 {row['flight_status_raw']!r} 无法识别",
                    field_name="status",
                    raw_value=row["flight_status_raw"],
                    flight_no=flight_no,
                    stat_date=flight_date,
                )
            )

        if flight_no and not is_valid_flight_no(flight_no):
            anomalies.append(
                self._anomaly(
                    "INVALID_FLIGHT_NO",
                    f"航班号 {flight_no!r} 不符合编码规则",
                    field_name="flight_no",
                    raw_value=flight_no_raw,
                    normalized_value=flight_no,
                    flight_no=flight_no,
                    stat_date=flight_date,
                )
            )

        return CleanResult(row=row, anomalies=anomalies)

    # -- 辅助 -------------------------------------------------------------
    def _resolve_airline_prefix(self, flight_no: str) -> tuple[str, bool]:
        """拆出航班号里的承运人前缀，并判断它是否为已知航司。

        前缀长度可能是 2 或 3（如 ``SQ830`` / ``K69236``），取能命中维表的那个。
        """
        if not flight_no:
            return "", False
        for length in (2, 3):
            prefix = flight_no[:length]
            if not prefix.isalnum():
                break
            if prefix in self.dims.airline_by_code:
                return prefix, True
        return flight_no[:2], False

    @staticmethod
    def _parse_date(raw: str) -> date | None:
        if not raw:
            return None
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
            try:
                return datetime.strptime(raw[:10], fmt).date()
            except ValueError:
                continue
        return None

    @staticmethod
    def _parse_time(
        raw: Any, base_date: date, field_name: str, notes: dict[str, dict[str, str]]
    ) -> datetime | None:
        result = parse_flight_time(raw, base_date)
        notes[field_name] = {"error": result.error or "", "raw": normalize_text(raw)}
        return result.value

    @staticmethod
    def _anomaly(
        rule_code: str,
        message: str,
        field_name: str = "",
        raw_value: str = "",
        normalized_value: str = "",
        flight_no: str = "",
        stat_date: date | None = None,
    ) -> Anomaly:
        rule = RULES_BY_CODE[rule_code]
        return Anomaly(
            rule_code=rule.code,
            rule_name=rule.name,
            severity=rule.severity,
            field_name=field_name,
            raw_value=raw_value,
            normalized_value=normalized_value,
            message=message,
            flight_no=flight_no,
            stat_date=stat_date,
        )


#: DWD 表的写入列顺序
DWD_COLUMNS: tuple[str, ...] = (
    "flight_id",
    "batch_id",
    "flight_date",
    "flight_no",
    "flight_no_raw",
    "airline_id",
    "airline_name",
    "route_id",
    "dep_airport_code",
    "dep_airport_name",
    "dep_city",
    "dep_country",
    "dep_country_code",
    "arr_airport_code",
    "arr_airport_name",
    "arr_city",
    "arr_country",
    "arr_country_code",
    "scheduled_dep_time",
    "actual_dep_time",
    "scheduled_arr_time",
    "actual_arr_time",
    "dep_delay_min",
    "arr_delay_min",
    "delay_status",
    "flight_status",
    "flight_status_raw",
    "flight_duration_min",
    "has_anomaly",
    "created_at",
    "updated_at",
)


def dwd_values(row: dict[str, Any]) -> list[Any]:
    """按 :data:`DWD_COLUMNS` 取出待写入的值（诊断字段自动剔除）。"""
    return [row.get(column) for column in DWD_COLUMNS]
