"""DWS 主题汇总层。

两个汇总主题：

* ``dws_flight_daily`` —— 日期 × 航空公司 × 出发机场：航班量、准点率、延误均值
* ``dws_route_daily``   —— 日期 × 航线：航线级平均延误

汇总口径的关键设计：**汇总不是从"本批次"算的，而是从 DWD 全量按日期重算**。
这样同一批数据跑第二遍，结果完全一样（幂等），也不会因为增量批次只覆盖部分
数据而把历史指标算错。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Sequence

import pandas as pd

from ..db.engine import Database

DAILY_COLUMNS: tuple[str, ...] = (
    "stat_date",
    "airline_id",
    "airline_name",
    "dep_airport_code",
    "dep_airport_name",
    "flight_cnt",
    "dep_delay_cnt",
    "arr_delay_cnt",
    "early_cnt",
    "cancel_cnt",
    "on_time_rate",
    "avg_dep_delay_min",
    "max_dep_delay_min",
    "avg_arr_delay_min",
    "max_arr_delay_min",
    "avg_flight_duration_min",
    "updated_at",
)

ROUTE_COLUMNS: tuple[str, ...] = (
    "stat_date",
    "route_id",
    "route_name",
    "dep_airport_code",
    "arr_airport_code",
    "flight_cnt",
    "delay_cnt",
    "on_time_rate",
    "avg_dep_delay_min",
    "max_dep_delay_min",
    "avg_arr_delay_min",
    "avg_flight_duration_min",
    "updated_at",
)

_FACT_COLUMNS = (
    "flight_date",
    "airline_id",
    "airline_name",
    "dep_airport_code",
    "dep_airport_name",
    "arr_airport_code",
    "route_id",
    "route_name",
    "dep_delay_min",
    "arr_delay_min",
    "delay_status",
    "flight_duration_min",
)


def _date_clause(dates: Sequence[date]) -> tuple[str, list[Any]]:
    if not dates:
        return "", []
    placeholders = ", ".join("?" for _ in dates)
    return f" WHERE flight_date IN ({placeholders})", [d.isoformat() for d in dates]


def _load_facts(db: Database, dates: Sequence[date]) -> pd.DataFrame:
    where, params = _date_clause(dates)
    fact_cols = ", ".join(f"f.{c}" for c in _FACT_COLUMNS if c != "route_name")
    sql = (
        f"SELECT {fact_cols}, r.route_name AS route_name "
        "FROM dwd_flight f LEFT JOIN dim_route r ON f.route_id = r.route_id"
        f"{where}"
    )
    rows = db.fetch_all(sql, params)
    if not rows:
        return pd.DataFrame(columns=list(_FACT_COLUMNS))
    return pd.DataFrame(rows)


def _round(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    return round(float(value), 2)


def _as_int(value: Any) -> int | None:
    if value is None or pd.isna(value):
        return None
    return int(value)


def rebuild_daily(
    db: Database,
    dates: Sequence[date],
    now: datetime | None = None,
    delay_threshold_min: int = 15,
) -> int:
    """重算指定日期的日汇总，返回写入行数。"""
    if not dates:
        return 0
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    df = _load_facts(db, dates)
    if df.empty:
        return 0

    df["is_dep_delay"] = (df["delay_status"] == "DELAYED").astype(int)
    df["is_arr_delay"] = (df["arr_delay_min"].notna() & (df["arr_delay_min"] >= delay_threshold_min)).astype(int)
    df["is_early"] = (df["delay_status"] == "EARLY").astype(int)
    df["is_cancel"] = (df["delay_status"] == "CANCELLED").astype(int)
    df["is_on_time"] = (df["delay_status"] == "ON_TIME").astype(int)

    agg = (
        df.groupby(["flight_date", "airline_id", "dep_airport_code"], dropna=False, sort=True)
        .agg(
            airline_name=("airline_name", "first"),
            dep_airport_name=("dep_airport_name", "first"),
            flight_cnt=("flight_date", "size"),
            dep_delay_cnt=("is_dep_delay", "sum"),
            arr_delay_cnt=("is_arr_delay", "sum"),
            early_cnt=("is_early", "sum"),
            cancel_cnt=("is_cancel", "sum"),
            on_time_cnt=("is_on_time", "sum"),
            avg_dep_delay_min=("dep_delay_min", "mean"),
            max_dep_delay_min=("dep_delay_min", "max"),
            avg_arr_delay_min=("arr_delay_min", "mean"),
            max_arr_delay_min=("arr_delay_min", "max"),
            avg_flight_duration_min=("flight_duration_min", "mean"),
        )
        .reset_index()
    )

    values: list[list[Any]] = []
    for record in agg.to_dict("records"):
        flight_cnt = int(record["flight_cnt"])
        on_time_rate = round(100.0 * record["on_time_cnt"] / flight_cnt, 2) if flight_cnt else None
        values.append(
            [
                str(record["flight_date"])[:10],
                record["airline_id"] or "",
                record.get("airline_name") or "",
                record["dep_airport_code"] or "",
                record.get("dep_airport_name") or "",
                flight_cnt,
                int(record["dep_delay_cnt"]),
                int(record["arr_delay_cnt"]),
                int(record["early_cnt"]),
                int(record["cancel_cnt"]),
                on_time_rate,
                _round(record["avg_dep_delay_min"]),
                _as_int(record["max_dep_delay_min"]),
                _round(record["avg_arr_delay_min"]),
                _as_int(record["max_arr_delay_min"]),
                _round(record["avg_flight_duration_min"]),
                stamp,
            ]
        )

    db.upsert(
        "dws_flight_daily",
        DAILY_COLUMNS,
        values,
        key_columns=["stat_date", "airline_id", "dep_airport_code"],
    )
    return len(values)


def rebuild_route_daily(db: Database, dates: Sequence[date], now: datetime | None = None) -> int:
    """重算指定日期的航线汇总，返回写入行数。"""
    if not dates:
        return 0
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    df = _load_facts(db, dates)
    if df.empty:
        return 0

    df = df[df["route_id"].astype(str) != ""]
    if df.empty:
        return 0

    df["is_delay"] = (df["delay_status"] == "DELAYED").astype(int)
    df["is_on_time"] = (df["delay_status"] == "ON_TIME").astype(int)

    agg = (
        df.groupby(["flight_date", "route_id"], dropna=False, sort=True)
        .agg(
            route_name=("route_name", "first"),
            dep_airport_code=("dep_airport_code", "first"),
            arr_airport_code=("arr_airport_code", "first"),
            flight_cnt=("flight_date", "size"),
            delay_cnt=("is_delay", "sum"),
            on_time_cnt=("is_on_time", "sum"),
            avg_dep_delay_min=("dep_delay_min", "mean"),
            max_dep_delay_min=("dep_delay_min", "max"),
            avg_arr_delay_min=("arr_delay_min", "mean"),
            avg_flight_duration_min=("flight_duration_min", "mean"),
        )
        .reset_index()
    )

    values: list[list[Any]] = []
    for record in agg.to_dict("records"):
        flight_cnt = int(record["flight_cnt"])
        on_time_rate = round(100.0 * record["on_time_cnt"] / flight_cnt, 2) if flight_cnt else None
        values.append(
            [
                str(record["flight_date"])[:10],
                record["route_id"],
                record.get("route_name") or "",
                record.get("dep_airport_code") or "",
                record.get("arr_airport_code") or "",
                flight_cnt,
                int(record["delay_cnt"]),
                on_time_rate,
                _round(record["avg_dep_delay_min"]),
                _as_int(record["max_dep_delay_min"]),
                _round(record["avg_arr_delay_min"]),
                _round(record["avg_flight_duration_min"]),
                stamp,
            ]
        )

    db.upsert("dws_route_daily", ROUTE_COLUMNS, values, key_columns=["stat_date", "route_id"])
    return len(values)
