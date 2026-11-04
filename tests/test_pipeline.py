"""端到端流水线测试：ODS 幂等、DWD 去重与更新、质量异常、增量水位。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from aerodata.db.engine import Database
from aerodata.etl.pipeline import EtlPipeline
from aerodata.layers.dws import rebuild_daily
from aerodata.sources.base import SourceBatch

from .conftest import StubSource, make_record


@pytest.fixture()
def sample_records() -> list[dict]:
    return [
        make_record(flight_no="SQ830", flight_date="2026-01-15"),
        make_record(flight_no="SQ831", flight_date="2026-01-15", dep_plan="10:00", arr_plan="14:00"),
        make_record(flight_no="CA1501", flight_date="2026-01-16", airline="中国国际航空",
                    dep_airport="北京首都", dep_city="北京", dep_country="中国",
                    arr_airport="广州白云", arr_city="广州", arr_country="中国",
                    dep_plan="07:00", arr_plan="10:00", dep_actual="07:20", arr_actual="10:30"),
    ]


def run(db: Database, config, records: list[dict], **kwargs):
    source = StubSource(records)
    return EtlPipeline(db, config, source).run(**kwargs)


class TestODS:
    def test_原始行数与字段(self, db: Database, pipeline_config, sample_records) -> None:
        run(db, pipeline_config, sample_records)
        assert db.count("ods_flight") == len(sample_records)

    def test_重复采集不新增行(self, db: Database, pipeline_config, sample_records) -> None:
        run(db, pipeline_config, sample_records)
        result = run(db, pipeline_config, sample_records)
        assert db.count("ods_flight") == len(sample_records)
        assert result.ods_new_cnt == 0
        assert result.ods_skip_cnt == len(sample_records)

    def test_原始值不被加工(self, db: Database, pipeline_config) -> None:
        run(db, pipeline_config, [make_record(flight_no="sq 830", dep_plan="8:5")])
        row = db.fetch_one("SELECT * FROM ods_flight")
        assert row["flight_no"] == "sq 830"
        assert row["dep_plan"] == "8:5"


class TestDWD:
    def test_清洗后字段标准化(self, db: Database, pipeline_config, sample_records) -> None:
        run(db, pipeline_config, sample_records)
        row = db.fetch_one("SELECT * FROM dwd_flight WHERE flight_no = 'SQ830'")
        assert row["dep_airport_code"] == "SIN"
        assert row["airline_id"] == "SQ"
        assert row["dep_delay_min"] == 6

    def test_批次内重复航班只保留一条(
        self, db: Database, pipeline_config, sample_records
    ) -> None:
        duplicated = sample_records + [dict(sample_records[0])]
        result = run(db, pipeline_config, duplicated)
        assert db.count("dwd_flight") == len(sample_records)
        assert result.dwd_skip_cnt == 1

    def test_二次观测走更新语义(
        self, db: Database, pipeline_config, sample_records
    ) -> None:
        run(db, pipeline_config, sample_records)
        updated = dict(sample_records[0], status="已起飞", arr_actual="--")
        result = run(db, pipeline_config, [updated])
        assert db.count("dwd_flight") == len(sample_records)
        assert result.dwd_insert_cnt == 0
        assert result.dwd_update_cnt == 1
        row = db.fetch_one("SELECT * FROM dwd_flight WHERE flight_no = 'SQ830'")
        assert row["flight_status"] == "DEPARTED"

    def test_缺关键字段的记录仍然入库并标记异常(
        self, db: Database, pipeline_config
    ) -> None:
        run(db, pipeline_config, [make_record(flight_no="", airline="")])
        row = db.fetch_one("SELECT * FROM dwd_flight")
        assert row is not None
        assert row["has_anomaly"] == 1

    def test_主键唯一(self, db: Database, pipeline_config, sample_records) -> None:
        run(db, pipeline_config, sample_records)
        total = db.count("dwd_flight")
        distinct = db.scalar("SELECT COUNT(DISTINCT flight_id) FROM dwd_flight")
        assert total == distinct


class Test质量异常:
    def test_异常入库(self, db: Database, pipeline_config) -> None:
        run(db, pipeline_config, [make_record(dep_airport="某某国际机场", dep_plan="25:00")])
        codes = {
            r["rule_code"]
            for r in db.fetch_all("SELECT DISTINCT rule_code FROM dq_flight_anomaly")
        }
        assert "UNKNOWN_AIRPORT" in codes
        assert "INVALID_TIME_VALUE" in codes

    def test_同一批次重跑异常不重复(
        self, db: Database, pipeline_config, sample_records
    ) -> None:
        first = run(db, pipeline_config, sample_records)
        count_after_first = db.count("dq_flight_anomaly")
        run(db, pipeline_config, sample_records)
        assert db.count("dq_flight_anomaly") == count_after_first
        assert first.anomaly_persisted_cnt == count_after_first

    def test_异常记录带批次号(self, db: Database, pipeline_config, sample_records) -> None:
        result = run(db, pipeline_config, sample_records)
        row = db.fetch_one("SELECT * FROM dq_flight_anomaly LIMIT 1")
        if row is not None:
            assert row["batch_id"] == result.batch_id


class TestDWS汇总:
    def test_汇总按日期重算且行数正确(
        self, db: Database, pipeline_config, sample_records
    ) -> None:
        run(db, pipeline_config, sample_records)
        rows = db.fetch_all("SELECT * FROM dws_flight_daily")
        assert rows
        assert all(r["flight_cnt"] >= 1 for r in rows)

    def test_汇总幂等(self, db: Database, pipeline_config, sample_records) -> None:
        run(db, pipeline_config, sample_records)
        snapshot = db.fetch_all("SELECT * FROM dws_flight_daily ORDER BY stat_date, airline_id")
        run(db, pipeline_config, sample_records)
        again = db.fetch_all("SELECT * FROM dws_flight_daily ORDER BY stat_date, airline_id")
        assert len(snapshot) == len(again)
        assert [r["flight_cnt"] for r in snapshot] == [r["flight_cnt"] for r in again]

    def test_航线汇总存在(self, db: Database, pipeline_config, sample_records) -> None:
        run(db, pipeline_config, sample_records)
        assert db.count("dws_route_daily") >= 1

    def test_空日期不报错(self, db: Database) -> None:
        assert rebuild_daily(db, []) == 0


class Test增量与水位:
    def test_水位记录已处理日期(self, db: Database, pipeline_config, sample_records) -> None:
        run(db, pipeline_config, sample_records)
        watermark = db.fetch_one("SELECT * FROM etl_watermark WHERE source_name = 'stub'")
        assert watermark is not None
        assert str(watermark["last_flight_date"])[:10] == "2026-01-16"

    def test_增量只处理新日期(self, db: Database, pipeline_config, sample_records) -> None:
        run(db, pipeline_config, sample_records)
        newer = make_record(flight_no="SQ999", flight_date="2026-01-17")
        result = run(db, pipeline_config, [newer], incremental=True)
        assert result.source_row_cnt == 1
        assert db.count("dwd_flight") == len(sample_records) + 1

    def test_增量跳过旧日期(self, db: Database, pipeline_config, sample_records) -> None:
        run(db, pipeline_config, sample_records)
        result = run(db, pipeline_config, [sample_records[0]], incremental=True)
        assert result.source_row_cnt == 0

    def test_批次日志留痕(self, db: Database, pipeline_config, sample_records) -> None:
        result = run(db, pipeline_config, sample_records)
        row = db.fetch_one("SELECT * FROM etl_batch_log WHERE batch_id = ?", (result.batch_id,))
        assert row["status"] == "SUCCESS"
        assert row["source_row_cnt"] == len(sample_records)


class Test失败处理:
    def test_源异常时批次状态为失败(self, db: Database, pipeline_config) -> None:
        class BrokenSource(StubSource):
            def fetch(self, since: date | None = None) -> SourceBatch:
                raise RuntimeError("采集端炸了")

        from aerodata.etl.pipeline import EtlPipeline

        pipeline = EtlPipeline(db, pipeline_config, BrokenSource([]))
        with pytest.raises(RuntimeError):
            pipeline.run()
        row = db.fetch_one("SELECT * FROM etl_batch_log ORDER BY started_at DESC LIMIT 1")
        assert row["status"] == "FAILED"
        assert "采集端炸了" in row["error_msg"]
