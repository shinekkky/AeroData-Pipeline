"""pytest 公共夹具。

每个测试用独立的临时 SQLite 文件，互不干扰；维表用真实的航线清单装载，
保证测试跑的是真实的维度数据而不是编造的假数据。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from aerodata.config import DbConfig, PipelineConfig, SourceConfig
from aerodata.db.engine import Database
from aerodata.db.schema import create_all
from aerodata.dimensions import load_dimensions
from aerodata.etl.cleaning import DimensionIndex, FlightTransformer
from aerodata.sources.base import FlightSource, SourceBatch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ROUTES_FILE = PROJECT_ROOT / "data" / "reference" / "routes_daily.json"


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    """建好表与维度的临时数据库。"""
    database = Database(DbConfig(dialect="sqlite", path=str(tmp_path / "test.db"))).connect()
    create_all(database)
    load_dimensions(database, ROUTES_FILE)
    yield database
    database.close()


@pytest.fixture()
def dims(db: Database) -> DimensionIndex:
    return DimensionIndex.load(db)


@pytest.fixture()
def transformer(dims: DimensionIndex) -> FlightTransformer:
    return FlightTransformer(dims=dims, default_date=date(2026, 1, 15))


@pytest.fixture()
def pipeline_config(tmp_path: Path) -> PipelineConfig:
    return PipelineConfig(
        source=SourceConfig(type="simulator"),
        db=DbConfig(dialect="sqlite", path=str(tmp_path / "test.db")),
        reports_dir=str(tmp_path / "reports"),
    )


def _default_record() -> dict[str, Any]:
    """一条符合采集器字段的原始记录。"""
    return {
        "flight_no": "SQ830",
        "airline": "新加坡航空",
        "dep_country": "新加坡",
        "dep_city": "新加坡",
        "dep_airport": "新加坡樟宜",
        "dep_plan": "08:35",
        "dep_actual": "08:41",
        "arr_country": "中国",
        "arr_city": "上海",
        "arr_province": "上海",
        "arr_airport": "上海浦东",
        "arr_plan": "13:20",
        "arr_actual": "13:35",
        "status": "已到达",
        "flight_date": "2026-01-15",
    }


def make_record(**overrides: Any) -> dict[str, Any]:
    """记录工厂：``make_record(flight_no="SQ999")`` 生成一条带覆盖字段的记录。"""
    record = _default_record()
    record.update(overrides)
    return record


@pytest.fixture()
def record() -> dict[str, Any]:
    return make_record()


class StubSource(FlightSource):
    """测试用的固定数据源。"""

    name = "stub"

    def __init__(self, records: list[dict[str, Any]], name: str = "stub"):
        self.records = records
        self.name = name

    def fetch(self, since: date | None = None) -> SourceBatch:
        if since is None:
            return SourceBatch(self.name, list(self.records), origin="stub")
        kept = [
            r for r in self.records if str(r.get("flight_date", ""))[:10] >= since.isoformat()
        ]
        return SourceBatch(self.name, kept, origin="stub")


@pytest.fixture()
def stub_source():
    """数据源工厂：``stub_source(records)``。"""
    return StubSource
