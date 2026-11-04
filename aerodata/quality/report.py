"""数据质量报告。

报告有两个层次：

* **质量视角** —— 规则命中分布、严重度、异常明细
* **业务视角** —— 航班量、准点率/延误率、各航司/机场/航线的指标

输出 JSON（给程序/看板用）和 Markdown（给人看、贴进周报或作品集用）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..db.engine import Database
from ..db.schema import ALL_TABLES

#: 异常严重度排序
_SEVERITY_ORDER = {"ERROR": 0, "WARN": 1, "INFO": 2}


@dataclass
class QualityReport:
    generated_at: str
    scope: dict[str, Any] = field(default_factory=dict)
    tables: dict[str, int] = field(default_factory=dict)
    batch: dict[str, Any] | None = None
    rule_stats: list[dict[str, Any]] = field(default_factory=list)
    severity_stats: list[dict[str, Any]] = field(default_factory=list)
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "scope": self.scope,
            "tables": self.tables,
            "batch": self.batch,
            "quality": {
                "rule_stats": self.rule_stats,
                "severity_stats": self.severity_stats,
                "anomaly_samples": self.anomalies,
            },
            "metrics": self.metrics,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, default=str)

    def to_markdown(self) -> str:
        return _render_markdown(self)


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------
def _table_counts(db: Database) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ALL_TABLES:
        try:
            counts[table.name] = db.count(table.name)
        except Exception:  # noqa: BLE001 - 表不存在时跳过
            counts[table.name] = -1
    return counts


def _fetch_dicts(db: Database, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    return db.fetch_all(sql, params)


def _overall_metrics(db: Database) -> dict[str, Any]:
    row = db.fetch_one(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN delay_status = 'DELAYED' THEN 1 ELSE 0 END) AS delayed,
               SUM(CASE WHEN delay_status = 'ON_TIME'  THEN 1 ELSE 0 END) AS on_time,
               SUM(CASE WHEN delay_status = 'EARLY'   THEN 1 ELSE 0 END) AS early,
               SUM(CASE WHEN delay_status = 'CANCELLED' THEN 1 ELSE 0 END) AS cancelled,
               SUM(CASE WHEN delay_status = 'UNKNOWN' THEN 1 ELSE 0 END) AS unknown,
               SUM(CASE WHEN dep_delay_min IS NOT NULL THEN 1 ELSE 0 END) AS with_delay,
               AVG(dep_delay_min) AS avg_dep_delay,
               MAX(dep_delay_min) AS max_dep_delay,
               AVG(arr_delay_min) AS avg_arr_delay,
               MAX(arr_delay_min) AS max_arr_delay,
               AVG(flight_duration_min) AS avg_duration,
               COUNT(DISTINCT airline_id) AS airline_cnt,
               COUNT(DISTINCT route_id) AS route_cnt,
               COUNT(DISTINCT dep_airport_code) AS airport_cnt,
               COUNT(DISTINCT flight_date) AS day_cnt
        FROM dwd_flight
        """
    ) or {}

    total = int(row.get("total") or 0)
    delayed = int(row.get("delayed") or 0)
    on_time = int(row.get("on_time") or 0)
    with_delay = int(row.get("with_delay") or 0)

    def pct(value: int) -> float | None:
        return round(100.0 * value / total, 2) if total else None

    def rnd(value: Any) -> float | None:
        return round(float(value), 2) if value is not None else None

    return {
        "flight_total": total,
        "delay_rate": pct(delayed),
        "on_time_rate": pct(on_time),
        "early_cnt": int(row.get("early") or 0),
        "cancelled_cnt": int(row.get("cancelled") or 0),
        "unknown_status_cnt": int(row.get("unknown") or 0),
        "avg_dep_delay_min": rnd(row.get("avg_dep_delay")),
        "max_dep_delay_min": int(row["max_dep_delay"]) if row.get("max_dep_delay") is not None else None,
        "avg_arr_delay_min": rnd(row.get("avg_arr_delay")),
        "max_arr_delay_min": int(row["max_arr_delay"]) if row.get("max_arr_delay") is not None else None,
        "avg_flight_duration_min": rnd(row.get("avg_duration")),
        "flights_with_delay_value": with_delay,
        "airline_cnt": int(row.get("airline_cnt") or 0),
        "route_cnt": int(row.get("route_cnt") or 0),
        "airport_cnt": int(row.get("airport_cnt") or 0),
        "day_cnt": int(row.get("day_cnt") or 0),
    }


def _top_lists(db: Database, top_n: int) -> dict[str, Any]:
    airlines = _fetch_dicts(
        db,
        """
        SELECT airline_id AS code, airline_name AS name, COUNT(*) AS flight_cnt,
               ROUND(AVG(dep_delay_min), 2) AS avg_dep_delay_min
        FROM dwd_flight
        GROUP BY airline_id, airline_name
        ORDER BY flight_cnt DESC
        LIMIT ?
        """,
        [top_n],
    )
    dep_airports = _fetch_dicts(
        db,
        """
        SELECT dep_airport_code AS code, dep_airport_name AS name, COUNT(*) AS flight_cnt
        FROM dwd_flight
        GROUP BY dep_airport_code, dep_airport_name
        ORDER BY flight_cnt DESC
        LIMIT ?
        """,
        [top_n],
    )
    arr_airports = _fetch_dicts(
        db,
        """
        SELECT arr_airport_code AS code, arr_airport_name AS name, COUNT(*) AS flight_cnt
        FROM dwd_flight
        GROUP BY arr_airport_code, arr_airport_name
        ORDER BY flight_cnt DESC
        LIMIT ?
        """,
        [top_n],
    )
    routes = _fetch_dicts(
        db,
        """
        SELECT f.route_id, r.route_name AS route_name,
               f.dep_airport_code AS dep, f.arr_airport_code AS arr,
               COUNT(*) AS flight_cnt,
               ROUND(AVG(f.dep_delay_min), 2) AS avg_dep_delay_min,
               MAX(f.dep_delay_min) AS max_dep_delay_min
        FROM dwd_flight f
        LEFT JOIN dim_route r ON f.route_id = r.route_id
        WHERE f.route_id <> '' AND f.dep_delay_min IS NOT NULL
        GROUP BY f.route_id, r.route_name, f.dep_airport_code, f.arr_airport_code
        ORDER BY avg_dep_delay_min DESC
        LIMIT ?
        """,
        [top_n],
    )
    return {
        "top_airlines": airlines,
        "top_departure_airports": dep_airports,
        "top_arrival_airports": arr_airports,
        "worst_routes_by_delay": routes,
    }


def _sample_anomalies(
    db: Database, where: str, params: list[Any], per_rule: int = 3, limit: int = 60
) -> list[dict[str, Any]]:
    """按规则配额抽样异常。

    直接取前 N 条会被命中量最大的规则刷屏，看不出其他规则长什么样，
    所以每条规则最多取 ``per_rule`` 条，再按严重度排序。
    """
    rows = _fetch_dicts(
        db,
        f"""
        SELECT rule_code, rule_name, severity, field_name, raw_value, normalized_value,
               message, flight_no, stat_date
        FROM dq_flight_anomaly{where}
        ORDER BY CASE severity WHEN 'ERROR' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
                 rule_code, anomaly_id
        """,
        params,
    )
    quota: dict[str, int] = {}
    sampled: list[dict[str, Any]] = []
    for row in rows:
        code = str(row["rule_code"])
        if quota.get(code, 0) >= per_rule:
            continue
        quota[code] = quota.get(code, 0) + 1
        sampled.append(row)
        if len(sampled) >= limit:
            break
    return sampled


def build_report(db: Database, batch_id: str | None = None, top_n: int = 10) -> QualityReport:
    """生成质量报告。``batch_id`` 为空时统计全库。"""
    report = QualityReport(generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    report.tables = _table_counts(db)

    where = ""
    params: list[Any] = []
    if batch_id:
        where = " WHERE batch_id = ?"
        params.append(batch_id)
        batch = db.fetch_one("SELECT * FROM etl_batch_log WHERE batch_id = ?", (batch_id,))
        report.batch = batch
        report.scope = {"batch_id": batch_id, "scope": "batch"}
    else:
        batch = db.fetch_one("SELECT * FROM etl_batch_log ORDER BY started_at DESC LIMIT 1")
        report.batch = batch
        report.scope = {"batch_id": (batch or {}).get("batch_id", ""), "scope": "full"}

    report.rule_stats = _fetch_dicts(
        db,
        f"""
        SELECT rule_code, rule_name, severity, COUNT(*) AS hit_cnt
        FROM dq_flight_anomaly{where}
        GROUP BY rule_code, rule_name, severity
        ORDER BY hit_cnt DESC
        """,
        params,
    )
    report.severity_stats = _fetch_dicts(
        db,
        f"""
        SELECT severity, COUNT(*) AS hit_cnt
        FROM dq_flight_anomaly{where}
        GROUP BY severity
        """,
        params,
    )
    report.severity_stats.sort(key=lambda r: _SEVERITY_ORDER.get(str(r["severity"]), 9))
    report.anomalies = _sample_anomalies(db, where, params)

    metrics = _overall_metrics(db)
    metrics.update(_top_lists(db, top_n))
    report.metrics = metrics
    return report


# ---------------------------------------------------------------------------
# Markdown 渲染
# ---------------------------------------------------------------------------
def _table(header: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "_（无数据）_\n"
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join("" if c is None else str(c) for c in row) + " |")
    return "\n".join(lines) + "\n"


def _render_markdown(report: QualityReport) -> str:
    m = report.metrics
    batch = report.batch or {}
    total_anomaly = sum(int(r["hit_cnt"]) for r in report.severity_stats)
    total_flight = m.get("flight_total") or 0

    parts: list[str] = []
    parts.append("# 航空数据质量报告\n")
    parts.append(f"生成时间：{report.generated_at}\n")
    parts.append(
        f"数据范围：{report.scope.get('scope', 'full')}"
        f"（批次 `{report.scope.get('batch_id') or 'N/A'}`）\n"
    )

    parts.append("\n## 一、批次概览\n")
    parts.append(
        _table(
            ["指标", "值"],
            [
                ["采集源", batch.get("source_name", "")],
                ["批次号", batch.get("batch_id", "")],
                ["状态", batch.get("status", "")],
                ["源端记录数", batch.get("source_row_cnt", 0)],
                ["ODS 新增 / 幂等跳过", f"{batch.get('ods_new_cnt', 0)} / {batch.get('ods_skip_cnt', 0)}"],
                ["DWD 新增 / 更新 / 去重丢弃", f"{batch.get('dwd_insert_cnt', 0)} / {batch.get('dwd_update_cnt', 0)} / {batch.get('dwd_skip_cnt', 0)}"],
                ["质量异常命中数", batch.get("anomaly_cnt", 0)],
                ["耗时(秒)", batch.get("duration_sec", 0)],
            ],
        )
    )

    parts.append("\n## 二、数据质量总览\n")
    parts.append(
        f"本次共命中 **{total_anomaly}** 条质量异常"
        f"（航班总数 {total_flight} 条，异常密度 "
        f"{round(100.0 * total_anomaly / total_flight, 2) if total_flight else 0}%）。\n"
    )
    parts.append("\n**按严重度**\n")
    parts.append(
        _table(
            ["严重度", "命中数"],
            [[r["severity"], r["hit_cnt"]] for r in report.severity_stats],
        )
    )
    parts.append("\n**按规则**\n")
    parts.append(
        _table(
            ["规则编码", "规则名称", "严重度", "命中数"],
            [[r["rule_code"], r["rule_name"], r["severity"], r["hit_cnt"]] for r in report.rule_stats],
        )
    )

    parts.append("\n## 三、异常明细采样（每条规则最多 3 条）\n")
    parts.append(
        _table(
            ["严重度", "规则", "字段", "原始值", "标准化值", "说明"],
            [
                [
                    a["severity"],
                    a["rule_code"],
                    a["field_name"],
                    a["raw_value"],
                    a["normalized_value"],
                    a["message"],
                ]
                for a in report.anomalies
            ],
        )
    )

    parts.append("\n## 四、航班统计指标\n")
    parts.append(
        _table(
            ["指标", "值"],
            [
                ["航班总量", m.get("flight_total")],
                ["统计天数", m.get("day_cnt")],
                ["涉及航空公司数", m.get("airline_cnt")],
                ["涉及航线条数", m.get("route_cnt")],
                ["涉及机场数", m.get("airport_cnt")],
                ["航班延误率(%)", m.get("delay_rate")],
                ["航班准点率(%)", m.get("on_time_rate")],
                ["取消班次", m.get("cancelled_cnt")],
                ["平均起飞延误(分钟)", m.get("avg_dep_delay_min")],
                ["最大起飞延误(分钟)", m.get("max_dep_delay_min")],
                ["平均到达延误(分钟)", m.get("avg_arr_delay_min")],
                ["最大到达延误(分钟)", m.get("max_arr_delay_min")],
                ["平均飞行时长(分钟)", m.get("avg_flight_duration_min")],
            ],
        )
    )

    parts.append("\n### 各航空公司航班量 TOP\n")
    parts.append(
        _table(
            ["航空公司编码", "航空公司", "航班量", "平均起飞延误(分钟)"],
            [[r["code"], r["name"], r["flight_cnt"], r["avg_dep_delay_min"]] for r in m.get("top_airlines", [])],
        )
    )

    parts.append("\n### 各机场起降量 TOP\n")
    parts.append(
        _table(
            ["角色", "机场编码", "机场", "航班量"],
            [["出发", r["code"], r["name"], r["flight_cnt"]] for r in m.get("top_departure_airports", [])]
            + [["到达", r["code"], r["name"], r["flight_cnt"]] for r in m.get("top_arrival_airports", [])],
        )
    )

    parts.append("\n### 航线平均延误 TOP（最延误）\n")
    parts.append(
        _table(
            ["航线", "出发", "到达", "航班量", "平均起飞延误(分钟)", "最大起飞延误(分钟)"],
            [
                [
                    r.get("route_name") or r.get("route_id", ""),
                    r["dep"],
                    r["arr"],
                    r["flight_cnt"],
                    r["avg_dep_delay_min"],
                    r["max_dep_delay_min"],
                ]
                for r in m.get("worst_routes_by_delay", [])
            ],
        )
    )

    parts.append("\n## 五、各层数据规模\n")
    parts.append(
        _table(
            ["数据表", "行数"],
            [[name, count] for name, count in report.tables.items()],
        )
    )
    return "".join(parts)


def write_report(report: QualityReport, reports_dir: str | Path, prefix: str = "") -> tuple[Path, Path]:
    """写出 JSON + Markdown 报告，返回两个文件路径。"""
    directory = Path(reports_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = prefix or f"quality_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    json_path = directory / f"{stem}.json"
    md_path = directory / f"{stem}.md"
    json_path.write_text(report.to_json(), encoding="utf-8")
    md_path.write_text(report.to_markdown(), encoding="utf-8")
    return json_path, md_path
