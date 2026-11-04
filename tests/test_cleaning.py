"""清洗与维表关联的单元测试。"""

from __future__ import annotations

from datetime import date

import pytest

from aerodata.etl.cleaning import DimensionIndex, FlightTransformer

from .conftest import make_record


class Test正常记录:
    def test_字段标准化(self, transformer: FlightTransformer) -> None:
        result = transformer.transform(make_record(flight_no="sq 830"))
        row = result.row
        assert row["flight_no"] == "SQ830"
        assert row["flight_no_raw"] == "sq 830"

    def test_维表关联(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record()).row
        assert row["dep_airport_code"] == "SIN"
        assert row["arr_airport_code"] == "PVG"
        assert row["airline_id"] == "SQ"
        assert row["airline_name"] == "新加坡航空"
        assert row["route_id"] != ""

    def test_延误计算(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record(dep_actual="08:50")).row
        assert row["dep_delay_min"] == 15
        assert row["delay_status"] == "DELAYED"

    def test_准点判定(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record(dep_actual="08:38")).row
        assert row["dep_delay_min"] == 3
        assert row["delay_status"] == "ON_TIME"

    def test_实际时间缺失时延误为空(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record(dep_actual="--", arr_actual="--")).row
        assert row["dep_delay_min"] is None
        assert row["delay_status"] == "UNKNOWN"

    def test_跨零点到达(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record(dep_plan="23:50", arr_plan="01:20+1")).row
        assert row["scheduled_arr_time"] == "2026-01-16 01:20:00"
        assert row["flight_duration_min"] == 90

    def test_主键稳定(self, transformer: FlightTransformer) -> None:
        a = transformer.transform(make_record()).row["flight_id"]
        b = transformer.transform(make_record()).row["flight_id"]
        assert a == b

    def test_实际时间变化不改变主键(self, transformer: FlightTransformer) -> None:
        """实际时间属于可更新字段，主键必须保持稳定，否则会产生重复行。"""
        a = transformer.transform(make_record()).row["flight_id"]
        b = transformer.transform(make_record(dep_actual="23:59")).row["flight_id"]
        assert a == b

    def test_计划时间变化会改变主键(self, transformer: FlightTransformer) -> None:
        a = transformer.transform(make_record()).row["flight_id"]
        b = transformer.transform(make_record(dep_plan="23:50")).row["flight_id"]
        assert a != b


class Test机场别名:
    def test_同一机场的不同中文名映射到同一编码(self, transformer: FlightTransformer) -> None:
        a = transformer.transform(make_record(dep_airport="沙巴")).row
        b = transformer.transform(make_record(dep_airport="斗湖")).row
        assert a["dep_airport_code"] == b["dep_airport_code"] == "BKI"

    def test_国际机场后缀可识别(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record(dep_airport="上海浦东国际机场")).row
        assert row["dep_airport_code"] == "PVG"

    def test_人工别名可识别(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record(dep_airport="曼谷素万那普")).row
        assert row["dep_airport_code"] == "BKK"

    def test_无法识别的机场编码为空(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record(dep_airport="某某国际机场")).row
        assert row["dep_airport_code"] == ""
        assert row["dep_airport_name"] == "某某国际机场"


class Test异常输入:
    def test_航班号缺失(self, transformer: FlightTransformer) -> None:
        result = transformer.transform(make_record(flight_no=""))
        assert result.row["flight_no"] == ""
        # 缺失类问题由质量规则层判定，清洗层只负责不改坏数据
        from aerodata.quality.rules import check_row

        codes = {a.rule_code for a in check_row(result.row)}
        assert "MISSING_REQUIRED_FIELD" in codes

    def test_航班号格式非法(self, transformer: FlightTransformer) -> None:
        result = transformer.transform(make_record(flight_no="不是航班号"))
        codes = {a.rule_code for a in result.anomalies}
        assert "INVALID_FLIGHT_NO" in codes

    def test_航空公司缺失(self, transformer: FlightTransformer) -> None:
        result = transformer.transform(make_record(airline=""))
        assert result.row["airline_id"] == ""

    def test_时间非法保留原值不做瞎猜(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record(dep_plan="25:00")).row
        assert row["scheduled_dep_time"] is None

    def test_时间未补零被修正并留痕(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record(dep_plan="8:5")).row
        assert row["scheduled_dep_time"] == "2026-01-15 08:05:00"
        notes = row["_time_notes"]["scheduled_dep_time"]
        assert notes["error"] == "invalid_format"

    def test_日期缺失回退到采集日期(self, transformer: FlightTransformer) -> None:
        result = transformer.transform(make_record(flight_date=""))
        assert result.row["flight_date"] == "2026-01-15"
        assert any(a.rule_code == "MISSING_REQUIRED_FIELD" for a in result.anomalies)

    def test_未知航空公司不编造编码(self, transformer: FlightTransformer) -> None:
        row = transformer.transform(make_record(airline="无名航空")).row
        assert row["airline_id"] == ""
        assert row["airline_name"] == "无名航空"


class Test维表索引:
    def test_别名索引可加载(self, dims: DimensionIndex) -> None:
        assert dims.aliases.get("斗湖") == "BKI"
        assert dims.aliases.get("曼谷素万那普") == "BKK"

    def test_机场主表不含占位编码(self, dims: DimensionIndex) -> None:
        assert "UNK" not in dims.airport_by_code

    def test_路线可按编码命中(self, dims: DimensionIndex) -> None:
        assert dims.match_route("SIN", "PVG") is not None
