"""ETL 流水线编排。

一次运行的完整链路::

    采集源 -> ODS(落地+幂等去重) -> DWD(清洗+维表关联+延误计算)
                               -> 质量规则 -> dq_flight_anomaly
                               -> DWS(按日期重算汇总) -> 质量报告

三条硬性要求贯穿全流程：

1. **幂等** —— 同一批数据跑两遍，ODS / DWD / DWS 的行数与指标完全一致
2. **增量** —— 记录已处理到的日期，下次只取更新的数据
3. **可追溯** —— 每条异常、每个批次都有记录，指标能回溯到明细
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Sequence

from ..config import PipelineConfig
from ..db.engine import Database
from ..layers.dws import rebuild_daily, rebuild_route_daily
from ..layers.ods import load_ods
from ..quality.rules import Anomaly, check_batch, check_row
from ..sources.base import FlightSource
from .cleaning import DWD_COLUMNS, DimensionIndex, FlightTransformer, dwd_values
from .standards import build_batch_id

#: 质量异常表写入列
ANOMALY_COLUMNS: tuple[str, ...] = (
    "batch_id",
    "rule_code",
    "rule_name",
    "severity",
    "stat_date",
    "flight_id",
    "flight_no",
    "field_name",
    "raw_value",
    "normalized_value",
    "message",
    "created_at",
)


@dataclass
class PipelineResult:
    """一次 ETL 运行的统计。"""

    batch_id: str
    source_name: str
    started_at: datetime
    finished_at: datetime | None = None
    source_row_cnt: int = 0
    ods_new_cnt: int = 0
    ods_skip_cnt: int = 0
    dwd_insert_cnt: int = 0
    dwd_update_cnt: int = 0
    dwd_skip_cnt: int = 0
    anomaly_cnt: int = 0
    anomaly_persisted_cnt: int = 0
    dws_daily_cnt: int = 0
    dws_route_cnt: int = 0
    stat_dates: list[str] = field(default_factory=list)
    status: str = "RUNNING"
    error: str | None = None

    @property
    def duration_sec(self) -> float:
        end = self.finished_at or datetime.now()
        return round((end - self.started_at).total_seconds(), 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "source_name": self.source_name,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": self.finished_at.isoformat(timespec="seconds") if self.finished_at else None,
            "duration_sec": self.duration_sec,
            "status": self.status,
            "source_row_cnt": self.source_row_cnt,
            "ods_new_cnt": self.ods_new_cnt,
            "ods_skip_cnt": self.ods_skip_cnt,
            "dwd_insert_cnt": self.dwd_insert_cnt,
            "dwd_update_cnt": self.dwd_update_cnt,
            "dwd_skip_cnt": self.dwd_skip_cnt,
            "anomaly_cnt": self.anomaly_cnt,
            "anomaly_persisted_cnt": self.anomaly_persisted_cnt,
            "dws_daily_cnt": self.dws_daily_cnt,
            "dws_route_cnt": self.dws_route_cnt,
            "stat_dates": self.stat_dates,
            "error": self.error,
        }


def _chunks(items: Sequence[str], size: int = 500) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class EtlPipeline:
    """编排 ODS / DWD / 质量 / DWS 各层。"""

    def __init__(
        self,
        db: Database,
        config: PipelineConfig,
        source: FlightSource,
        transformer: FlightTransformer | None = None,
    ):
        self.db = db
        self.config = config
        self.source = source
        self._transformer = transformer

    # -- 水位 -------------------------------------------------------------
    def read_watermark(self, source_name: str) -> dict[str, Any]:
        row = self.db.fetch_one(
            "SELECT * FROM etl_watermark WHERE source_name = ?", (source_name,)
        )
        return row or {}

    def write_watermark(
        self, source_name: str, batch_id: str, last_flight_date: date | None, rows: int
    ) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.db.upsert(
            "etl_watermark",
            ("source_name", "last_batch_id", "last_flight_date", "last_run_at", "total_rows"),
            [[source_name, batch_id, last_flight_date.isoformat() if last_flight_date else "", now, rows]],
            key_columns=["source_name"],
        )

    # -- 主流程 -----------------------------------------------------------
    def run(self, incremental: bool = False, since: date | None = None) -> PipelineResult:
        started_at = datetime.now()
        batch_id = build_batch_id(self.source.name, started_at)
        result = PipelineResult(
            batch_id=batch_id,
            source_name=self.source.name,
            started_at=started_at,
        )
        self._log_batch(result, status="RUNNING")

        try:
            effective_since = since
            if incremental and effective_since is None:
                watermark = self.read_watermark(self.source.name)
                last = watermark.get("last_flight_date")
                if last:
                    try:
                        effective_since = date.fromisoformat(str(last)[:10]) + timedelta(days=1)
                    except ValueError:
                        effective_since = None

            batch = self.source.fetch(effective_since)
            result.source_row_cnt = batch.row_count

            # 1) ODS
            ods_stats = load_ods(self.db, batch, batch_id, started_at)
            result.ods_new_cnt = ods_stats["new"]
            result.ods_skip_cnt = ods_stats["skipped"]

            # 2) DWD
            transformer = self._transformer or FlightTransformer(
                dims=DimensionIndex.load(self.db),
                delay_threshold_min=self.config.delay_threshold_min,
                early_threshold_min=self.config.early_threshold_min,
                max_duration_min=self.config.max_flight_duration_min,
                default_date=started_at.date(),
            )
            rows: list[dict[str, Any]] = []
            anomalies: list[Anomaly] = []
            for record in batch.records:
                cleaned = transformer.transform(record, batch_id)
                rows.append(cleaned.row)
                anomalies.extend(cleaned.anomalies)

            # 3) 质量规则
            for row in rows:
                anomalies.extend(check_row(row))

            # 4) 批次内去重：同一 flight_id 以最后一次观测为准
            deduped: dict[str, dict[str, Any]] = {}
            for row in rows:
                deduped[str(row.get("flight_id"))] = row
            result.dwd_skip_cnt = len(rows) - len(deduped)
            unique_rows = list(deduped.values())

            # 批级重复检测要在去重之前做，才能反映源端真实重复情况
            anomalies.extend(check_batch(rows))

            # 5) 落 DWD
            stamp = started_at.strftime("%Y-%m-%d %H:%M:%S")
            flagged = {a.flight_id for a in anomalies if a.flight_id}
            for row in unique_rows:
                row["has_anomaly"] = 1 if str(row.get("flight_id")) in flagged else 0
                row["created_at"] = stamp
                row["updated_at"] = stamp

            existing_ids = self._existing_flight_ids([str(r["flight_id"]) for r in unique_rows])
            result.dwd_insert_cnt = len(unique_rows) - len(existing_ids)
            result.dwd_update_cnt = len(existing_ids)

            if unique_rows:
                self.db.upsert(
                    "dwd_flight",
                    DWD_COLUMNS,
                    [dwd_values(row) for row in unique_rows],
                    key_columns=["flight_id"],
                )

            # 6) 质量异常落库
            result.anomaly_cnt = len(anomalies)
            if anomalies:
                values = [a.as_row(batch_id, stamp) for a in anomalies]
                self.db.insert_ignore(
                    "dq_flight_anomaly",
                    ANOMALY_COLUMNS,
                    values,
                    key_columns=["batch_id", "rule_code", "flight_id", "field_name", "raw_value"],
                )
                result.anomaly_persisted_cnt = self.db.count(
                    "dq_flight_anomaly", "batch_id = ?", (batch_id,)
                )

            # 7) DWS 汇总：按本批次涉及的日期，从 DWD 全量重算
            stat_dates = sorted({str(r.get("flight_date"))[:10] for r in unique_rows if r.get("flight_date")})
            dates = [date.fromisoformat(d) for d in stat_dates]
            result.stat_dates = stat_dates
            result.dws_daily_cnt = rebuild_daily(
                self.db,
                dates,
                started_at,
                delay_threshold_min=self.config.delay_threshold_min,
            )
            result.dws_route_cnt = rebuild_route_daily(self.db, dates, started_at)

            # 8) 水位
            if dates:
                self.write_watermark(self.source.name, batch_id, max(dates), result.source_row_cnt)

            result.status = "SUCCESS"
            result.finished_at = datetime.now()
            self._log_batch(result, status="SUCCESS")
        except Exception as exc:  # noqa: BLE001 - 批次级兜底，错误写进批次日志
            result.status = "FAILED"
            result.error = f"{type(exc).__name__}: {exc}"[:1024]
            result.finished_at = datetime.now()
            self._log_batch(result, status="FAILED")
            raise

        return result

    # -- 辅助 -------------------------------------------------------------
    def _existing_flight_ids(self, flight_ids: Sequence[str]) -> set[str]:
        found: set[str] = set()
        if not flight_ids:
            return found
        for chunk in _chunks(flight_ids):
            placeholders = ", ".join("?" for _ in chunk)
            rows = self.db.fetch_all(
                f"SELECT flight_id FROM dwd_flight WHERE flight_id IN ({placeholders})", list(chunk)
            )
            found.update(str(r["flight_id"]) for r in rows)
        return found

    def _log_batch(self, result: PipelineResult, status: str) -> None:
        self.db.upsert(
            "etl_batch_log",
            (
                "batch_id",
                "source_name",
                "status",
                "started_at",
                "finished_at",
                "duration_sec",
                "source_row_cnt",
                "ods_new_cnt",
                "ods_skip_cnt",
                "dwd_insert_cnt",
                "dwd_update_cnt",
                "dwd_skip_cnt",
                "anomaly_cnt",
                "error_msg",
            ),
            [
                [
                    result.batch_id,
                    result.source_name,
                    status,
                    result.started_at.strftime("%Y-%m-%d %H:%M:%S"),
                    result.finished_at.strftime("%Y-%m-%d %H:%M:%S") if result.finished_at else "",
                    int(result.duration_sec),
                    result.source_row_cnt,
                    result.ods_new_cnt,
                    result.ods_skip_cnt,
                    result.dwd_insert_cnt,
                    result.dwd_update_cnt,
                    result.dwd_skip_cnt,
                    result.anomaly_cnt,
                    result.error or "",
                ]
            ],
            key_columns=["batch_id"],
        )
