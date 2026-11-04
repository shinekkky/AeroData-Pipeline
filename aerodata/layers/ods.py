"""ODS 原始层落地。

原则：**只落地，不加工**。原始值原样入库，加工全部留给 DWD。

幂等靠 ``row_hash`` 唯一索引实现：同一条记录重复采集时，ODS 层直接跳过，
不会产生重复行。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..db.engine import Database
from ..etl.standards import build_row_hash
from ..sources.base import SourceBatch

ODS_COLUMNS: tuple[str, ...] = (
    "batch_id",
    "source_name",
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
    "src_md5",
    "row_hash",
    "source_file",
    "ingest_time",
)

#: 采集源可能带上的额外字段
_EXTRA_FIELDS = ("md5", "src_md5", "insert_time", "flight_id")


def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    return {column: ("" if record.get(column) is None else record.get(column)) for column in ODS_COLUMNS if column not in {"batch_id", "source_name", "row_hash", "source_file", "ingest_time"}}


def load_ods(
    db: Database,
    batch: SourceBatch,
    batch_id: str,
    now: datetime | None = None,
) -> dict[str, int]:
    """把采集批次写入 ODS，返回新增/跳过行数。"""
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    before = db.count("ods_flight")

    values: list[list[Any]] = []
    for record in batch.records:
        row = _normalize_record(record)
        src_md5 = row.get("src_md5") or record.get("md5") or ""
        values.append(
            [
                batch_id,
                batch.source_name,
                row["flight_no"],
                row["airline"],
                row["dep_country"],
                row["dep_city"],
                row["dep_airport"],
                row["dep_plan"],
                row["dep_actual"],
                row["arr_country"],
                row["arr_city"],
                row["arr_province"],
                row["arr_airport"],
                row["arr_plan"],
                row["arr_actual"],
                row["status"],
                row["flight_date"],
                src_md5,
                build_row_hash(record),
                batch.origin,
                stamp,
            ]
        )

    db.insert_ignore("ods_flight", ODS_COLUMNS, values, key_columns=["row_hash"])
    after = db.count("ods_flight")
    new_rows = max(0, after - before)
    return {"new": new_rows, "skipped": max(0, len(values) - new_rows)}
