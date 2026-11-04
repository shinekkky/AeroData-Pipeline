"""字段标准化规则的单元测试。"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from aerodata.etl.standards import (
    build_flight_id,
    build_row_hash,
    classify_delay,
    compute_delay_min,
    compute_flight_duration_min,
    is_valid_flight_no,
    normalize_airline_key,
    normalize_airport_key,
    normalize_flight_no,
    normalize_status,
    parse_flight_time,
    to_halfwidth,
)


class Test航班号:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("SQ830", "SQ830"),
            ("SQ 830", "SQ830"),
            ("sq-830", "SQ830"),
            (" SQ.830 ", "SQ830"),
            ("MU　5100", "MU5100"),  # 全角空格
        ],
    )
    def test_标准化(self, raw: str, expected: str) -> None:
        assert normalize_flight_no(raw) == expected

    @pytest.mark.parametrize(
        "value,valid",
        [("SQ830", True), ("CA1501", True), ("3U8888", True), ("9C1234", True),
         ("K69236", True), ("D77523", True), ("8M1234", True),
         ("1234", True), ("SQ83A", False), ("SQ", False), ("", False), ("SQ-830", False)],
    )
    def test_合法性判定(self, value: str, valid: bool) -> None:
        assert is_valid_flight_no(value) is valid


class Test机场名称:
    def test_去国际机场后缀后与主表一致(self) -> None:
        assert normalize_airport_key("上海浦东国际机场") == normalize_airport_key("上海浦东")

    def test_去空格与全角括号(self) -> None:
        assert normalize_airport_key("  上海（浦东）  ") == normalize_airport_key("上海浦东")

    def test_不同机场不混淆(self) -> None:
        assert normalize_airport_key("曼谷素万那普") != normalize_airport_key("曼谷廊曼")

    def test_全角转半角(self) -> None:
        assert to_halfwidth("ＡＢＣ１２３") == "ABC123"


class Test航空公司名称:
    def test_去公司后缀(self) -> None:
        assert normalize_airline_key("新加坡航空有限公司") == normalize_airline_key("新加坡航空")

    def test_去空格(self) -> None:
        assert normalize_airline_key("马 来 西 亚 航 空") == "马来西亚航空"


class Test时间解析:
    base = date(2026, 1, 15)

    def test_标准格式(self) -> None:
        result = parse_flight_time("08:35", self.base)
        assert result.ok
        assert result.value == datetime(2026, 1, 15, 8, 35)

    def test_未补零仍可解析但标记格式异常(self) -> None:
        result = parse_flight_time("8:5", self.base)
        assert result.value == datetime(2026, 1, 15, 8, 5)
        assert result.error == "invalid_format"

    def test_跨零点次日到达(self) -> None:
        result = parse_flight_time("23:50+1", self.base)
        assert result.value == datetime(2026, 1, 16, 23, 50)

    def test_带完整日期(self) -> None:
        result = parse_flight_time("2026-01-16 01:20", self.base)
        assert result.value == datetime(2026, 1, 16, 1, 20)

    @pytest.mark.parametrize("raw", ["--", "", "  ", "暂无", "N/A", None])
    def test_缺失值(self, raw: object) -> None:
        result = parse_flight_time(raw, self.base)
        assert result.value is None
        assert result.error == "missing"

    @pytest.mark.parametrize("raw", ["25:00", "08:60", "99:99"])
    def test_超出取值范围(self, raw: str) -> None:
        result = parse_flight_time(raw, self.base)
        assert result.value is None
        assert result.error == "invalid_range"

    @pytest.mark.parametrize("raw", ["abc", "08-35", "下午三点"])
    def test_无法解析(self, raw: str) -> None:
        result = parse_flight_time(raw, self.base)
        assert result.value is None
        assert result.error == "invalid_format"


class Test状态标准化:
    @pytest.mark.parametrize(
        "raw,code",
        [
            ("已到达", "ARRIVED"),
            ("已起飞", "DEPARTED"),
            ("计划", "PLANNED"),
            ("延误", "DELAYED"),
            ("取消", "CANCELLED"),
            ("提前", "EARLY"),
            ("备降", "DIVERTED"),
            ("登机", "BOARDING"),
        ],
    )
    def test_映射(self, raw: str, code: str) -> None:
        assert normalize_status(raw).code == code

    def test_缺失(self) -> None:
        result = normalize_status("--")
        assert result.error == "missing"

    def test_无法识别(self) -> None:
        result = normalize_status("外星人降落")
        assert result.error == "unknown"


class Test延误与时长:
    def test_延误分钟(self) -> None:
        sched = datetime(2026, 1, 15, 8, 0)
        actual = datetime(2026, 1, 15, 8, 45)
        assert compute_delay_min(sched, actual) == 45

    def test_提前为负(self) -> None:
        sched = datetime(2026, 1, 15, 8, 0)
        actual = datetime(2026, 1, 15, 7, 30)
        assert compute_delay_min(sched, actual) == -30

    def test_缺实际时间时为None(self) -> None:
        assert compute_delay_min(datetime(2026, 1, 15, 8, 0), None) is None

    def test_跨零点时长(self) -> None:
        dep = datetime(2026, 1, 15, 23, 0)
        arr = datetime(2026, 1, 16, 1, 0)
        result = compute_flight_duration_min(dep, arr)
        assert result.minutes == 120
        assert result.crossed_midnight is True

    @pytest.mark.parametrize(
        "delay,status,expected",
        [
            (5, "ARRIVED", "ON_TIME"),
            (15, "ARRIVED", "DELAYED"),
            (-6, "ARRIVED", "EARLY"),
            (None, "PLANNED", "UNKNOWN"),
            (300, "CANCELLED", "CANCELLED"),
        ],
    )
    def test_延误归类(self, delay: int | None, status: str, expected: str) -> None:
        assert classify_delay(delay, status) == expected


class Test主键:
    def test_同内容主键一致(self) -> None:
        args = ("SQ830", date(2026, 1, 15), "SIN", "PVG", datetime(2026, 1, 15, 8, 35))
        assert build_flight_id(*args) == build_flight_id(*args)

    def test_计划时间不同则主键不同(self) -> None:
        a = build_flight_id("SQ830", date(2026, 1, 15), "SIN", "PVG", datetime(2026, 1, 15, 8, 35))
        b = build_flight_id("SQ830", date(2026, 1, 15), "SIN", "PVG", datetime(2026, 1, 15, 9, 35))
        assert a != b

    def test_行哈希对同内容稳定(self) -> None:
        row = {"flight_no": "SQ830", "airline": "新加坡航空", "dep_plan": "08:35"}
        assert build_row_hash(row) == build_row_hash(dict(row))

    def test_行哈希对不同内容不同(self) -> None:
        a = build_row_hash({"flight_no": "SQ830"})
        b = build_row_hash({"flight_no": "SQ831"})
        assert a != b
