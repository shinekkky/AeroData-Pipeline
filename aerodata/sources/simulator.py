"""确定性仿真采集源。

用**真实的航线清单**（采集器同款 269 条东南亚—中国航线）合成航班明细，并按
真实分布注入延误、取消、跨零点到达等业务现象，同时按可配置比例注入脏数据，
让下游的清洗与质量规则有真实的东西可打。

同一 ``seed`` 永远产出同一份数据，因此测试与演示可复现。

注入的缺陷覆盖计划里提到的全部场景：机场代码不统一、航班号重复、时间格式
错误、字段缺失。
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..dimensions import load_airlines, load_routes
from .base import FlightSource, SourceBatch

#: 常见航班计划起飞时段（小时），覆盖红眼航班与夜航
DEPARTURE_HOURS = (
    0, 1, 2, 5, 6, 6, 7, 7, 7, 8, 8, 8, 9, 9, 9, 10, 10, 11, 12,
    13, 13, 14, 14, 15, 15, 16, 16, 17, 17, 18, 18, 19, 20, 21, 22, 23,
)

#: 脏数据注入类型
DEFECT_KINDS = (
    "missing_flight_no",
    "missing_airline",
    "bad_plan_time",
    "bad_actual_time",
    "dirty_airport_name",
    "unknown_airport",
    "unknown_airline",
    "arrival_before_departure",
    "missing_country",
    "exact_duplicate",
    "re_observation",
)


def _unit(key: str, salt: str = "") -> float:
    """由字符串键派生 [0,1) 的确定性随机数。"""
    digest = hashlib.sha256(f"{key}|{salt}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def _pick(items: list[Any], key: str, salt: str) -> Any:
    if not items:
        return None
    return items[int(_unit(key, salt) * len(items)) % len(items)]


def _fmt_time(value: datetime) -> str:
    return value.strftime("%H:%M")


def _shift_time(text: str, minutes: int) -> str:
    """把 ``HH:MM`` 平移若干分钟（允许跨零点，跨零点时仍返回 HH:MM）。"""
    try:
        hour, minute = (int(part) for part in text.split(":"))
    except (ValueError, AttributeError):
        return text
    total = (hour * 60 + minute + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _fmt_time_cross_midnight(value: datetime, base_day: date) -> str:
    """跨零点到达时用 ``HH:MM+1`` 表示次日。"""
    delta = (value.date() - base_day).days
    if delta <= 0:
        return value.strftime("%H:%M")
    return f"{value.strftime('%H:%M')}+{delta}"


class SimulatorSource(FlightSource):
    """基于真实航线清单的仿真源。"""

    def __init__(
        self,
        routes_file: str | Path,
        days: int = 14,
        seed: int = 20260101,
        defect_rate: float = 0.06,
        end_date: date | None = None,
        name: str = "simulator",
    ):
        self.name = name
        self.routes_file = Path(routes_file)
        self.days = max(1, int(days))
        self.seed = int(seed)
        self.defect_rate = float(defect_rate)
        self.end_date = end_date or date.today()
        self.routes = load_routes(self.routes_file)
        self.airlines = load_airlines()
        self._airlines_by_country: dict[str, list[dict[str, Any]]] = {}
        for airline in self.airlines:
            self._airlines_by_country.setdefault(airline["country"], []).append(airline)

    # -- 对外接口 ---------------------------------------------------------
    def fetch(self, since: date | None = None) -> SourceBatch:
        start = self.end_date - timedelta(days=self.days - 1)
        if since is not None and since > start:
            start = since

        records: list[dict[str, Any]] = []
        day = start
        while day <= self.end_date:
            for route in self.routes:
                records.extend(self._flights_for_route(route, day))
            day += timedelta(days=1)

        records = self._inject_exact_duplicates(records)
        return SourceBatch(
            source_name=self.name,
            records=records,
            origin=f"simulator:{self.routes_file.name}",
        )

    # -- 单航线生成 -------------------------------------------------------
    def _route_key(self, route: dict[str, Any]) -> str:
        return f"{route.get('from_iata', '')}-{route.get('to_iata', '')}"

    def _flights_for_route(self, route: dict[str, Any], day: date) -> list[dict[str, Any]]:
        key = self._route_key(route)
        route_key = f"{self.seed}|{key}|{day.isoformat()}"
        count = 1 + int(_unit(route_key, "count") * 3)  # 每天 1~3 班
        out: list[dict[str, Any]] = []
        for index in range(count):
            out.append(self._one_flight(route, day, index, route_key))
        return out

    def _one_flight(
        self, route: dict[str, Any], day: date, index: int, route_key: str
    ) -> dict[str, Any]:
        flight_key = f"{route_key}|{index}"

        # 承运人：优先本国航司，其次区域内航司
        dep_country = route.get("from_country", "")
        candidates = self._airlines_by_country.get(dep_country) or self.airlines
        airline = _pick(candidates, flight_key, "airline")
        airline_code = airline["airline_code"] if airline else "XX"
        airline_name = airline["airline_name"] if airline else "未知航空公司"

        # 计划起飞时间
        hour = DEPARTURE_HOURS[int(_unit(flight_key, "hour") * len(DEPARTURE_HOURS))]
        minute = int(_unit(flight_key, "minute") * 60)
        dep_plan_dt = datetime.combine(day, datetime.min.time()) + timedelta(hours=hour, minutes=minute)

        # 飞行时长：国际航线更长
        is_intl = route.get("from_country") != route.get("to_country")
        duration = 60 + int(_unit(flight_key, "duration") * (240 if is_intl else 150))
        arr_plan_dt = dep_plan_dt + timedelta(minutes=duration)

        # 运行结果
        roll = _unit(flight_key, "outcome")
        if roll < 0.55:
            delay = -3 + int(_unit(flight_key, "delay") * 14)      # 准点 ±
            status = "已到达"
        elif roll < 0.76:
            delay = 15 + int(_unit(flight_key, "delay") * 166)     # 延误
            status = "延误"
        elif roll < 0.83:
            delay = 5 + int(_unit(flight_key, "delay") * 10)       # 轻微延误
            status = "已起飞"
        elif roll < 0.89:
            delay = -(5 + int(_unit(flight_key, "delay") * 26))     # 提前
            status = "提前"
        elif roll < 0.94:
            delay = None                                           # 取消
            status = "取消"
        elif roll < 0.985:
            delay = 180 + int(_unit(flight_key, "delay") * 420)    # 大面积延误
            status = "延误"
        else:
            delay = 600 + int(_unit(flight_key, "delay") * 900)    # 极端异常
            status = "延误"

        # 当天的航班可能还没飞
        is_future = day == self.end_date and _unit(flight_key, "future") < 0.25
        if is_future:
            status = "计划"
            delay = None

        dep_actual_dt = None if delay is None else dep_plan_dt + timedelta(minutes=delay)
        arr_actual_dt = (
            None
            if dep_actual_dt is None
            else dep_actual_dt + timedelta(minutes=duration + int(_unit(flight_key, "arr_lag") * 25))
        )

        flight_no = f"{airline_code}{_pick(list(range(1000, 9999)), flight_key, 'fno')}"
        record: dict[str, Any] = {
            "flight_no": flight_no,
            "airline": airline_name,
            "dep_country": route.get("from_country", ""),
            "dep_city": route.get("from_city", ""),
            "dep_airport": route.get("from", ""),
            "dep_plan": _fmt_time(dep_plan_dt),
            "dep_actual": "--" if dep_actual_dt is None else _fmt_time(dep_actual_dt),
            "arr_country": route.get("to_country", ""),
            "arr_city": route.get("to_city", ""),
            "arr_province": route.get("to_province", ""),
            "arr_airport": route.get("to", ""),
            "arr_plan": _fmt_time_cross_midnight(arr_plan_dt, day),
            "arr_actual": "--" if arr_actual_dt is None else _fmt_time_cross_midnight(arr_actual_dt, day),
            "status": status,
            "flight_date": day.isoformat(),
        }

        self._apply_defect(record, flight_key)
        return record

    # -- 缺陷注入 ---------------------------------------------------------
    def _apply_defect(self, record: dict[str, Any], flight_key: str) -> None:
        if self.defect_rate <= 0:
            return
        if _unit(flight_key, "defect") >= self.defect_rate:
            return

        kind = DEFECT_KINDS[int(_unit(flight_key, "defect_kind") * len(DEFECT_KINDS))]
        if kind == "missing_flight_no":
            record["flight_no"] = ""
        elif kind == "missing_airline":
            record["airline"] = ""
        elif kind == "bad_plan_time":
            record["dep_plan"] = _pick(["8:5", "25:00", "08:60", "abc", "8:05"], flight_key, "bad_time")
        elif kind == "bad_actual_time":
            record["dep_actual"] = _pick(["??:??", "8:5", "--", "24:30"], flight_key, "bad_actual")
        elif kind == "dirty_airport_name":
            base = record["dep_airport"]
            record["dep_airport"] = f"  {base}国际机场 " if base else "  "
        elif kind == "unknown_airport":
            record["dep_airport"] = "某某国际机场"
        elif kind == "unknown_airline":
            record["airline"] = "无名航空"
        elif kind == "arrival_before_departure":
            # 到达时间早于起飞时间：计划时刻倒挂 + 实际到达早于实际起飞
            record["arr_plan"] = _shift_time(record["dep_plan"], -90)
            actual = record.get("dep_actual", "")
            if actual not in ("", "--"):
                record["arr_actual"] = _shift_time(actual, -30)
        elif kind == "missing_country":
            record["arr_country"] = ""
        # exact_duplicate / re_observation 在批次级处理

    def _inject_exact_duplicates(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """批次级注入：完全重复行（ODS 幂等）与"二次观测"（DWD 更新语义）。"""
        out: list[dict[str, Any]] = []
        for index, record in enumerate(records):
            out.append(record)
            key = f"{self.seed}|dup|{index}"
            roll = _unit(key, "kind")
            if roll < 0.012:
                # 完全重复：内容一字不差，ODS 应识别为同一条
                out.append(dict(record))
            elif roll < 0.022:
                # 二次观测：同一班航班状态推进（如"已起飞" -> "已到达"）。
                # 业务主键不变但内容变化，用于验证 DWD 的更新语义。
                dup = dict(record)
                if record.get("status") not in ("已到达", "取消", "计划"):
                    dup["status"] = "已到达"
                    out.append(dup)
        return out
