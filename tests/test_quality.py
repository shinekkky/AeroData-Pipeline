"""数据质量规则的单元测试。"""

from __future__ import annotations

import pytest

from aerodata.etl.cleaning import FlightTransformer
from aerodata.quality.rules import ALL_RULES, RULES_BY_CODE, check_batch, check_row

from .conftest import make_record


def codes_of(anomalies) -> set[str]:
    return {a.rule_code for a in anomalies}


class Test规则注册:
    def test_规则数量不少于五类(self) -> None:
        assert len(ALL_RULES) >= 5

    def test_规则编码唯一(self) -> None:
        codes = [r.code for r in ALL_RULES]
        assert len(codes) == len(set(codes))

    def test_每条规则都有严重度与说明(self) -> None:
        for rule in ALL_RULES:
            assert rule.severity in {"ERROR", "WARN", "INFO"}
            assert rule.description
            assert rule.name


class Test行级规则:
    def test_必填缺失(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record(flight_no="")).row
        assert "MISSING_REQUIRED_FIELD" in codes_of(check_row(row))

    def test_航班号非法(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record(flight_no="X1")).row
        assert "INVALID_FLIGHT_NO" in codes_of(check_row(row))

    def test_机场未匹配(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record(dep_airport="某某国际机场")).row
        assert "UNKNOWN_AIRPORT" in codes_of(check_row(row))

    def test_航司未匹配(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record(airline="无名航空")).row
        assert "UNKNOWN_AIRLINE" in codes_of(check_row(row))

    def test_航班号前缀非已知航司(self, transformer: FlightTransformer) -> None:
        """格式合法但承运人不存在：靠参照完整性规则发现，而不是格式规则。"""
        row = transformer.transform(make_record(flight_no="S830")).row
        assert "UNKNOWN_AIRLINE_PREFIX" in codes_of(check_row(row))

    def test_已知航司前缀不报错(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record(flight_no="K61234")).row
        assert "UNKNOWN_AIRLINE_PREFIX" not in codes_of(check_row(row))

    def test_时间值非法(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record(dep_plan="25:00")).row
        assert "INVALID_TIME_VALUE" in codes_of(check_row(row))

    def test_时间格式不规范(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record(dep_plan="8:5")).row
        assert "INVALID_TIME_FORMAT" in codes_of(check_row(row))

    def test_取消却有实际时间(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record(status="取消")).row
        assert "STATUS_CONTRADICTION" in codes_of(check_row(row))

    def test_已到达却缺实际到达时间(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record(arr_actual="--")).row
        assert "MISSING_ACTUAL_TIME" in codes_of(check_row(row))

    def test_到达早于起飞(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(
            make_record(dep_actual="13:00", arr_actual="12:00")
        ).row
        assert "ARRIVAL_BEFORE_DEPARTURE" in codes_of(check_row(row))

    def test_飞行时长异常(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record(dep_plan="08:00", arr_plan="08:00+1")).row
        assert row["flight_duration_min"] == 1440
        assert "INVALID_FLIGHT_DURATION" in codes_of(check_row(row))

    def test_延误超出24小时(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record(dep_plan="08:00", dep_actual="09:00")).row
        row["dep_delay_min"] = 2000
        assert "DELAY_OUT_OF_RANGE" in codes_of(check_row(row))

    def test_国家与维表不一致(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record(dep_country="泰国")).row
        assert "COUNTRY_MISMATCH" in codes_of(check_row(row))

    def test_干净记录不报错(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record()).row
        assert check_row(row) == []


class Test批级规则:
    def test_完全重复被识别(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record()).row
        anomalies = check_batch([row, dict(row)])
        assert "DUPLICATE_FLIGHT" in codes_of(anomalies)

    def test_单条不报重复(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record()).row
        assert check_batch([row]) == []


class Test异常结构:
    def test_异常可序列化为数据库行(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record(dep_airport="某某国际机场")).row
        anomalies = check_row(row)
        assert anomalies
        values = anomalies[0].as_row("batch-1", "2026-01-15 00:00:00")
        assert len(values) == 12
        assert values[0] == "batch-1"
        assert values[1] in RULES_BY_CODE
        assert values[3] in {"ERROR", "WARN", "INFO"}
        assert values[10]  # raw_value
        assert values[11]  # message 非空

    def test_异常携带航班号与日期(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record(dep_airport="某某国际机场")).row
        anomaly = check_row(row)[0]
        assert anomaly.flight_no == "SQ830"
        assert str(anomaly.stat_date) == "2026-01-15"
