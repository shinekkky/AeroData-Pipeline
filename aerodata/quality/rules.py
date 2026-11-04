"""数据质量规则库。

清洗层负责"把数据改对"，质量层负责"把改不好的记录下来"。两条职责分开，
才能既保证下游可用，又保证问题可追溯。

规则分两个作用域：

* ``row``   —— 单条航班内部的一致性（必填缺失、时间非法、状态矛盾……）
* ``batch`` —— 批次级别的重复与冲突（同一班航班重复入库等）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable

SEVERITY_ERROR = "ERROR"
SEVERITY_WARN = "WARN"
SEVERITY_INFO = "INFO"


@dataclass
class Anomaly:
    """一条质量异常。"""

    rule_code: str
    rule_name: str
    severity: str
    field_name: str = ""
    raw_value: str = ""
    normalized_value: str = ""
    message: str = ""
    flight_id: str = ""
    flight_no: str = ""
    stat_date: date | None = None

    def as_row(self, batch_id: str, created_at: str) -> list[Any]:
        return [
            batch_id,
            self.rule_code,
            self.rule_name,
            self.severity,
            self.stat_date.isoformat() if self.stat_date else "",
            self.flight_id or "",
            self.flight_no or "",
            self.field_name or "",
            str(self.raw_value)[:255],
            str(self.normalized_value)[:255],
            self.message[:512],
            created_at,
        ]


@dataclass(frozen=True)
class Rule:
    code: str
    name: str
    severity: str
    scope: str  # row / batch
    description: str


ALL_RULES: tuple[Rule, ...] = (
    Rule("MISSING_REQUIRED_FIELD", "必填字段缺失", SEVERITY_ERROR, "row",
         "航班号 / 航空公司 / 航班日期为空"),
    Rule("INVALID_FLIGHT_NO", "航班号格式非法", SEVERITY_ERROR, "row",
         "航班号不符合 <2-3位字母><1-4位数字> 的编码规则"),
    Rule("INVALID_TIME_FORMAT", "时间格式不规范", SEVERITY_WARN, "row",
         "时间可解析但写法不规范，如 8:5（已尽力修正）"),
    Rule("INVALID_TIME_VALUE", "时间值非法", SEVERITY_ERROR, "row",
         "时间无法解析为合法时刻，如 25:00"),
    Rule("MISSING_TIME", "时间字段缺失", SEVERITY_WARN, "row",
         "计划/实际时间缺失，影响延误指标计算"),
    Rule("UNKNOWN_AIRPORT", "机场未匹配到维表", SEVERITY_ERROR, "row",
         "机场名称无法在 dim_airport 中找到对应编码"),
    Rule("UNKNOWN_AIRLINE", "航空公司未匹配到维表", SEVERITY_WARN, "row",
         "航空公司名称无法在 dim_airline 中找到对应编码"),
    Rule("UNKNOWN_AIRLINE_PREFIX", "航班号前缀非已知航司", SEVERITY_WARN, "row",
         "航班号前缀不在 dim_airline 中，疑似不存在的承运人"),
    Rule("MISSING_ACTUAL_TIME", "缺少实际时间", SEVERITY_WARN, "row",
         "航班状态为已起飞/已到达，但缺少对应实际时间"),
    Rule("STATUS_CONTRADICTION", "状态与时间矛盾", SEVERITY_WARN, "row",
         "取消的航班却有实际时间，或已到达却没有实际到达时间"),
    Rule("INVALID_FLIGHT_DURATION", "飞行时长异常", SEVERITY_ERROR, "row",
         "按计划时间计算的飞行时长超出合理区间"),
    Rule("ARRIVAL_BEFORE_DEPARTURE", "到达早于起飞", SEVERITY_ERROR, "row",
         "实际到达时间早于实际起飞时间"),
    Rule("DELAY_OUT_OF_RANGE", "延误超出合理范围", SEVERITY_WARN, "row",
         "延误分钟数绝对值超过 24 小时"),
    Rule("COUNTRY_MISMATCH", "国家与维表不一致", SEVERITY_WARN, "row",
         "记录中的国家与维表推导的国家不一致"),
    Rule("DUPLICATE_FLIGHT", "同一批次航班重复", SEVERITY_WARN, "batch",
         "同一 flight_id 在批次内出现多次"),
)

RULES_BY_CODE: dict[str, Rule] = {rule.code: rule for rule in ALL_RULES}
ROW_RULES = tuple(r for r in ALL_RULES if r.scope == "row")
BATCH_RULES = tuple(r for r in ALL_RULES if r.scope == "batch")

# 一天的分钟数
_MINUTES_PER_DAY = 24 * 60


def _anomaly(rule: Rule, row: dict[str, Any], message: str, **kwargs: Any) -> Anomaly:
    return Anomaly(
        rule_code=rule.code,
        rule_name=rule.name,
        severity=rule.severity,
        message=message,
        flight_id=str(row.get("flight_id") or ""),
        flight_no=str(row.get("flight_no") or ""),
        stat_date=_as_date(row.get("flight_date")),
        **kwargs,
    )


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------------------
# 行级规则
# ---------------------------------------------------------------------------
def _check_missing_required(row: dict[str, Any]) -> list[Anomaly]:
    rule = RULES_BY_CODE["MISSING_REQUIRED_FIELD"]
    out = []
    for field_name, label in (
        ("flight_no", "航班号"),
        ("airline_name", "航空公司"),
        ("flight_date", "航班日期"),
    ):
        if not str(row.get(field_name) or "").strip():
            out.append(
                _anomaly(rule, row, f"{label}为空", field_name=field_name, raw_value="")
            )
    return out


def _check_invalid_flight_no(row: dict[str, Any]) -> list[Anomaly]:
    from ..etl.standards import is_valid_flight_no

    rule = RULES_BY_CODE["INVALID_FLIGHT_NO"]
    flight_no = str(row.get("flight_no") or "")
    if not flight_no or is_valid_flight_no(flight_no):
        return []
    return [
        _anomaly(
            rule,
            row,
            f"航班号 {flight_no!r} 不符合编码规则",
            field_name="flight_no",
            raw_value=flight_no,
            normalized_value=flight_no,
        )
    ]


def _check_time_fields(row: dict[str, Any], rule_code: str) -> list[Anomaly]:
    """时间类规则的统一入口。

    清洗阶段已经把"格式不规范但能救""完全非法""缺失"三种情况分开标记，这里按
    规则编码只取自己关心的那一类，避免一条问题被多个规则重复计数。
    """
    out: list[Anomaly] = []
    rule = RULES_BY_CODE[rule_code]
    notes = row.get("_time_notes") or {}

    for field_name, label in (
        ("scheduled_dep_time", "计划起飞时间"),
        ("actual_dep_time", "实际起飞时间"),
        ("scheduled_arr_time", "计划到达时间"),
        ("actual_arr_time", "实际到达时间"),
    ):
        note = notes.get(field_name) or {}
        error = note.get("error")
        raw = note.get("raw", "")
        if error == "invalid_format" and rule_code == "INVALID_TIME_FORMAT":
            out.append(
                _anomaly(rule, row, f"{label}写法不规范：{raw!r}", field_name=field_name,
                          raw_value=raw, normalized_value=str(row.get(field_name) or ""))
            )
        elif error == "invalid_range" and rule_code == "INVALID_TIME_VALUE":
            out.append(
                _anomaly(rule, row, f"{label}取值非法：{raw!r}", field_name=field_name,
                          raw_value=raw, normalized_value="")
            )
        elif (
            error == "missing"
            and rule_code == "MISSING_TIME"
            # 只管计划时间：取消 / 未起飞的航班本来就没有实际时间，不是缺陷
            and field_name.startswith("scheduled_")
        ):
            out.append(
                _anomaly(rule, row, f"{label}缺失", field_name=field_name, raw_value=raw)
            )
    return out


def _check_unknown_airport(row: dict[str, Any]) -> list[Anomaly]:
    rule = RULES_BY_CODE["UNKNOWN_AIRPORT"]
    out = []
    for code_field, name_field, raw_field, label in (
        ("dep_airport_code", "dep_airport_name", "_dep_airport_raw", "出发"),
        ("arr_airport_code", "arr_airport_name", "_arr_airport_raw", "到达"),
    ):
        code = str(row.get(code_field) or "")
        if code and code != "UNK":
            continue
        raw = str(row.get(raw_field) or row.get(name_field) or "")
        out.append(
            _anomaly(rule, row, f"{label}机场 {raw!r} 未匹配到 dim_airport",
                      field_name=code_field, raw_value=raw)
        )
    return out


def _check_unknown_airline(row: dict[str, Any]) -> list[Anomaly]:
    rule = RULES_BY_CODE["UNKNOWN_AIRLINE"]
    code = str(row.get("airline_id") or "")
    if code:
        return []
    raw = str(row.get("_airline_raw") or row.get("airline_name") or "")
    return [
        _anomaly(rule, row, f"航空公司 {raw!r} 未匹配到 dim_airline",
                  field_name="airline_id", raw_value=raw)
    ]


def _check_unknown_airline_prefix(row: dict[str, Any]) -> list[Anomaly]:
    """航班号前缀是否是已知航司 —— 格式对但承运人不存在的情况。"""
    rule = RULES_BY_CODE["UNKNOWN_AIRLINE_PREFIX"]
    if row.get("_known_airline_prefix") is True:
        return []
    prefix = str(row.get("_airline_prefix") or "")
    if not prefix:
        return []
    return [
        _anomaly(rule, row, f"航班号前缀 {prefix!r} 不在 dim_airline 中",
                  field_name="flight_no", raw_value=str(row.get("flight_no") or ""),
                  normalized_value=prefix)
    ]


def _check_missing_actual_time(row: dict[str, Any]) -> list[Anomaly]:
    from ..etl.standards import STATUS_LABELS

    rule = RULES_BY_CODE["MISSING_ACTUAL_TIME"]
    status = str(row.get("flight_status") or "")
    label = STATUS_LABELS.get(status, status)
    out = []
    if status in ("DEPARTED", "ARRIVED") and not row.get("actual_dep_time"):
        out.append(
            _anomaly(rule, row, f"状态为{label}但缺少实际起飞时间",
                      field_name="actual_dep_time")
        )
    if status == "ARRIVED" and not row.get("actual_arr_time"):
        out.append(
            _anomaly(rule, row, f"状态为{label}但缺少实际到达时间",
                      field_name="actual_arr_time")
        )
    return out


def _check_status_contradiction(row: dict[str, Any]) -> list[Anomaly]:
    rule = RULES_BY_CODE["STATUS_CONTRADICTION"]
    status = str(row.get("flight_status") or "")
    has_actual = bool(row.get("actual_dep_time") or row.get("actual_arr_time"))
    if status == "CANCELLED" and has_actual:
        return [
            _anomaly(rule, row, "航班状态为取消，但存在实际起降时间",
                      field_name="flight_status", raw_value=str(row.get("flight_status_raw") or ""))
        ]
    return []


def _check_duration(row: dict[str, Any]) -> list[Anomaly]:
    rule = RULES_BY_CODE["INVALID_FLIGHT_DURATION"]
    duration = row.get("flight_duration_min")
    if duration is None:
        return []
    limit = int(row.get("_duration_limit_min") or 20 * 60)
    if duration <= 0 or duration > limit:
        return [
            _anomaly(rule, row, f"飞行时长 {duration} 分钟超出合理区间",
                      field_name="flight_duration_min", raw_value=str(duration),
                      normalized_value=str(duration))
        ]
    return []


def _check_arrival_before_departure(row: dict[str, Any]) -> list[Anomaly]:
    rule = RULES_BY_CODE["ARRIVAL_BEFORE_DEPARTURE"]
    dep = _as_datetime(row.get("actual_dep_time"))
    arr = _as_datetime(row.get("actual_arr_time"))
    if dep is None or arr is None:
        return []
    if arr < dep:
        return [
            _anomaly(rule, row, f"实际到达 {arr} 早于实际起飞 {dep}",
                      field_name="actual_arr_time", raw_value=str(arr))
        ]
    return []


def _check_delay_range(row: dict[str, Any]) -> list[Anomaly]:
    rule = RULES_BY_CODE["DELAY_OUT_OF_RANGE"]
    out = []
    for field_name, label in (("dep_delay_min", "起飞"), ("arr_delay_min", "到达")):
        value = row.get(field_name)
        if value is None:
            continue
        if abs(int(value)) > _MINUTES_PER_DAY:
            out.append(
                _anomaly(rule, row, f"{label}延误 {value} 分钟超出 24 小时范围",
                          field_name=field_name, raw_value=str(value))
            )
    return out


def _check_country_mismatch(row: dict[str, Any]) -> list[Anomaly]:
    rule = RULES_BY_CODE["COUNTRY_MISMATCH"]
    out = []
    for code_field, raw_field, label in (
        ("dep_country_code", "_dep_country_raw", "出发"),
        ("arr_country_code", "_arr_country_raw", "到达"),
    ):
        expected = str(row.get(code_field) or "")
        raw = str(row.get(raw_field) or "")
        if not expected or not raw:
            continue
        if row.get("_country_lookup", {}).get(raw) != expected:
            out.append(
                _anomaly(rule, row, f"{label}国家 {raw!r} 与维表推导的编码 {expected} 不一致",
                          field_name=raw_field, raw_value=raw, normalized_value=expected)
            )
    return out


_ROW_CHECKS: dict[str, Callable[[dict[str, Any]], list[Anomaly]]] = {
    "MISSING_REQUIRED_FIELD": _check_missing_required,
    "INVALID_FLIGHT_NO": _check_invalid_flight_no,
    "INVALID_TIME_FORMAT": lambda row: _check_time_fields(row, "INVALID_TIME_FORMAT"),
    "INVALID_TIME_VALUE": lambda row: _check_time_fields(row, "INVALID_TIME_VALUE"),
    "MISSING_TIME": lambda row: _check_time_fields(row, "MISSING_TIME"),
    "UNKNOWN_AIRPORT": _check_unknown_airport,
    "UNKNOWN_AIRLINE": _check_unknown_airline,
    "UNKNOWN_AIRLINE_PREFIX": _check_unknown_airline_prefix,
    "MISSING_ACTUAL_TIME": _check_missing_actual_time,
    "STATUS_CONTRADICTION": _check_status_contradiction,
    "INVALID_FLIGHT_DURATION": _check_duration,
    "ARRIVAL_BEFORE_DEPARTURE": _check_arrival_before_departure,
    "DELAY_OUT_OF_RANGE": _check_delay_range,
    "COUNTRY_MISMATCH": _check_country_mismatch,
}


def check_row(row: dict[str, Any]) -> list[Anomaly]:
    """对单条 DWD 记录执行全部行级规则。"""
    out: list[Anomaly] = []
    for rule in ROW_RULES:
        handler = _ROW_CHECKS.get(rule.code)
        if handler is None:
            continue
        out.extend(handler(row))
    return out


# ---------------------------------------------------------------------------
# 批次级规则
# ---------------------------------------------------------------------------
def check_batch(rows: list[dict[str, Any]]) -> list[Anomaly]:
    """对整个批次执行批级规则（重复航班）。"""
    rule = RULES_BY_CODE["DUPLICATE_FLIGHT"]
    seen: dict[str, int] = {}
    out: list[Anomaly] = []
    for row in rows:
        flight_id = str(row.get("flight_id") or "")
        if not flight_id:
            continue
        seen[flight_id] = seen.get(flight_id, 0) + 1
    for flight_id, count in seen.items():
        if count <= 1:
            continue
        sample = next(r for r in rows if r.get("flight_id") == flight_id)
        out.append(
            _anomaly(rule, sample, f"航班 {sample.get('flight_no')} 在本批次出现 {count} 次",
                      field_name="flight_id", raw_value=flight_id, normalized_value=str(count))
        )
    return out
