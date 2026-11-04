"""字段标准化规则集。

采集器给出的原始值脏得五花八门：航班号带空格、机场名带全角括号、时间写成
``8:5`` 或 ``25:00``、状态是"晚点/正常/已起飞"……这一层把它们统一成下游可用的
标准值，同时把"改不了"的情况作为错误码返回，交给质量规则记录，而不是悄悄
吞掉。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------------------
# 文本归一
# ---------------------------------------------------------------------------
_FULLWIDTH_START = 0xFF01
_FULLWIDTH_END = 0xFF5E
_FULLWIDTH_OFFSET = 0xFEE0
_SPACE_LIKE = "　 \t\r\n"

#: 采集器用这些值表示"没有值"
MISSING_TOKENS = {
    "",
    "-",
    "--",
    "---",
    "/",
    "n/a",
    "na",
    "null",
    "none",
    "暂无",
    "无",
    "未知",
    "待定",
    "—",
}

#: 时间字段里需要剔除的说明性文字
_TIME_NOISE_RE = re.compile(r"当地时间|当地|时间|约|预计|实际|\(.*?\)|（.*?）")


def to_halfwidth(text: str) -> str:
    """全角字符转半角。"""
    out = []
    for ch in text:
        code = ord(ch)
        if _FULLWIDTH_START <= code <= _FULLWIDTH_END:
            out.append(chr(code - _FULLWIDTH_OFFSET))
        else:
            out.append(ch)
    return "".join(out)


def normalize_text(value: object) -> str:
    """通用文本归一：全角转半角、去首尾空白、压缩内部空白。"""
    if value is None:
        return ""
    text = to_halfwidth(str(value)).strip()
    text = re.sub(r"\s+", " ", text)
    return text


def is_missing(value: object) -> bool:
    """判断原始值是否代表"缺失"。"""
    if value is None:
        return True
    text = to_halfwidth(str(value)).strip().lower()
    return text in MISSING_TOKENS


# ---------------------------------------------------------------------------
# 航班号
# ---------------------------------------------------------------------------
#: 航空公司前缀：2~3 位字母（IATA/ICAO），允许 "3U""9C""K6""D7" 这类含数字的特例
_AIRLINE_PREFIX_RE = re.compile(r"^(?:[A-Z]{2}|[A-Z]{3}|[A-Z]\d|\d[A-Z])$")


def normalize_flight_no(raw: object) -> str:
    """``"SQ 830"`` → ``"SQ830"``；去空格/连字符，统一大写。"""
    text = to_halfwidth(str(raw or "")).upper()
    text = re.sub(r"[\s\-_.·/、,，]", "", text)
    return text


def is_valid_flight_no(flight_no: str) -> bool:
    """航班号是否形如 ``<航空公司前缀><1-4 位数字>``。

    前缀为 2~3 位字母，或 "3U" 这类数字+字母的特例；另允许 1~5 位纯数字编号。
    """
    if not flight_no:
        return False
    if flight_no.isdigit():
        return len(flight_no) <= 5
    for prefix_len in (2, 3):
        match = re.fullmatch(rf"([A-Z0-9]{{{prefix_len}}})(\d{{1,4}})", flight_no)
        if match and _AIRLINE_PREFIX_RE.match(match.group(1)):
            return True
    return False


# ---------------------------------------------------------------------------
# 机场 / 航空公司名称
# ---------------------------------------------------------------------------
_AIRPORT_STRIP_RE = re.compile(r"[\s\-_·．\.、,，/\\()（）\[\]【】\"'“”‘’]")
_AIRLINE_STRIP_RE = re.compile(r"[\s\-_·．\.、,，/\\()（）\[\]【】\"'“”‘’]")
_AIRLINE_SUFFIX_RE = re.compile(r"(股份有限公司|有限责任公司|有限公司|Co\.?,? ?Ltd\.?|Ltd\.?|Inc\.?)$", re.I)


def normalize_airport_key(raw: object) -> str:
    """机场名匹配键：去分隔符、去"国际机场/机场"后缀。

    维表和原始值走同一个函数，因此"上海浦东国际机场"与"上海浦东"能落到同一个键。
    """
    text = to_halfwidth(str(raw or ""))
    text = _AIRPORT_STRIP_RE.sub("", text)
    text = text.replace("国际机场", "").replace("国际", "")
    text = text.replace("航站楼", "").replace("机场", "")
    return text.strip()


def normalize_airline_key(raw: object) -> str:
    """航司名匹配键：去分隔符、去公司后缀。"""
    text = to_halfwidth(str(raw or ""))
    text = _AIRLINE_STRIP_RE.sub("", text)
    text = _AIRLINE_SUFFIX_RE.sub("", text)
    return text.strip()


# ---------------------------------------------------------------------------
# 时间解析
# ---------------------------------------------------------------------------
_HHMM_RE = re.compile(r"^(?P<h>\d{1,2}):(?P<m>\d{1,2})(?:\+(?P<day>\d))?$")
_HHMM_CN_RE = re.compile(r"^(?:次日|翌日)?\s*(?P<h>\d{1,2}):(?P<m>\d{1,2})(?:\+(?P<day>\d))?\s*(?:次日|翌日)?$")
_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y年%m月%d日 %H:%M",
)

#: 不带年份的 "MM-DD HH:MM"（跨年时自动归到下一年）
_MONTH_DAY_RE = re.compile(r"^(?P<m>\d{1,2})-(?P<d>\d{1,2})[ T](?P<H>\d{1,2}):(?P<M>\d{2})$")


@dataclass(frozen=True)
class TimeParseResult:
    """时间解析结果。

    ``value`` 为解析出的时间；``error`` 为 None 时表示解析成功且字段有值。
    """

    value: datetime | None = None
    error: str | None = None  # missing / invalid_format / invalid_range / invalid_date

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def present(self) -> bool:
        """字段是否有值（与是否合法无关）。"""
        return self.error != "missing"


def parse_flight_time(raw: object, base_date: date) -> TimeParseResult:
    """解析航班时间字段。

    支持 ``HH:MM``、``HH:MM+1``（次日）、``次日HH:MM``、带日期的完整时间戳，
    以及 ``--``/``暂无`` 这类缺失值。``8:5`` 会尽力解析成 08:05，同时返回
    ``invalid_format`` 交给质量规则留痕。
    """
    if is_missing(raw):
        return TimeParseResult(None, "missing")

    text = to_halfwidth(str(raw)).strip()
    text = _TIME_NOISE_RE.sub("", text).strip()
    text = text.replace("次日", " ").replace("翌日", " ").strip()
    if not text:
        return TimeParseResult(None, "missing")

    # 1) 完整日期时间
    for fmt in _DATETIME_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return TimeParseResult(parsed, None)

    # 1b) 不带年份的 MM-DD HH:MM
    md_match = _MONTH_DAY_RE.match(text)
    if md_match:
        try:
            parsed = datetime(
                base_date.year,
                int(md_match.group("m")),
                int(md_match.group("d")),
                int(md_match.group("H")),
                int(md_match.group("M")),
            )
        except ValueError:
            return TimeParseResult(None, "invalid_date")
        if parsed.date() < base_date:
            parsed += timedelta(days=365)
        return TimeParseResult(parsed, None)

    # 2) HH:MM(+N)
    match = _HHMM_CN_RE.match(text) or _HHMM_RE.match(text)
    if not match:
        return TimeParseResult(None, "invalid_format")

    hour = int(match.group("h"))
    minute = int(match.group("m"))
    day_offset = int(match.group("day") or 0)

    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        return TimeParseResult(None, "invalid_range")

    value = datetime.combine(base_date, datetime.min.time()) + timedelta(
        hours=hour, minutes=minute, days=day_offset
    )
    error = "invalid_format" if (len(match.group("h")) == 1 or len(match.group("m")) == 1) else None
    return TimeParseResult(value, error)


# ---------------------------------------------------------------------------
# 航班状态
# ---------------------------------------------------------------------------
STATUS_PLANNED = "PLANNED"
STATUS_BOARDING = "BOARDING"
STATUS_DEPARTED = "DEPARTED"
STATUS_ARRIVED = "ARRIVED"
STATUS_DELAYED = "DELAYED"
STATUS_EARLY = "EARLY"
STATUS_CANCELLED = "CANCELLED"
STATUS_DIVERTED = "DIVERTED"
STATUS_RETURNED = "RETURNED"
STATUS_UNKNOWN = "UNKNOWN"

STATUS_LABELS: dict[str, str] = {
    STATUS_PLANNED: "计划",
    STATUS_BOARDING: "登机",
    STATUS_DEPARTED: "已起飞",
    STATUS_ARRIVED: "已到达",
    STATUS_DELAYED: "延误",
    STATUS_EARLY: "提前",
    STATUS_CANCELLED: "取消",
    STATUS_DIVERTED: "备降",
    STATUS_RETURNED: "返航",
    STATUS_UNKNOWN: "未知",
}

_STATUS_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (STATUS_CANCELLED, ("取消", "cancel", "取消航班")),
    (STATUS_RETURNED, ("返航", "return")),
    (STATUS_DIVERTED, ("备降", "diversion", "备降航班")),
    (STATUS_ARRIVED, ("已到达", "到达", "arrived", "landed")),
    (STATUS_DEPARTED, ("已起飞", "起飞", "departed", "takeoff", "已离港")),
    (STATUS_BOARDING, ("登机", "boarding", "开始登机")),
    (STATUS_DELAYED, ("延误", "晚点", "delay", "delayed")),
    (STATUS_EARLY, ("提前", "early")),
    (STATUS_PLANNED, ("计划", "预计", "准点", "scheduled", "on time", "正常", "按时")),
)


@dataclass(frozen=True)
class StatusResult:
    code: str
    label: str
    error: str | None = None  # missing / unknown


def normalize_status(raw: object) -> StatusResult:
    """把采集到的状态文本映射到标准状态码。"""
    if is_missing(raw):
        return StatusResult(STATUS_UNKNOWN, STATUS_LABELS[STATUS_UNKNOWN], "missing")
    text = to_halfwidth(str(raw)).strip().lower()
    for code, keys in _STATUS_PATTERNS:
        for key in keys:
            if key in text:
                return StatusResult(code, STATUS_LABELS[code], None)
    return StatusResult(STATUS_UNKNOWN, STATUS_LABELS[STATUS_UNKNOWN], "unknown")


# ---------------------------------------------------------------------------
# 延误与时长
# ---------------------------------------------------------------------------
def compute_delay_min(scheduled: datetime | None, actual: datetime | None) -> int | None:
    """延误分钟：正数为延误，负数为提前；缺实际时间时返回 None。"""
    if scheduled is None or actual is None:
        return None
    return int(round((actual - scheduled).total_seconds() / 60))


@dataclass(frozen=True)
class DurationResult:
    minutes: int | None = None
    crossed_midnight: bool = False
    error: str | None = None  # missing / invalid_range


def compute_flight_duration_min(
    scheduled_dep: datetime | None, scheduled_arr: datetime | None
) -> DurationResult:
    """按计划时刻计算飞行时长。

    先用完整时间差；出现负数（到达时刻早于起飞时刻）说明是同一天的倒挂，
    按跨零点处理并加 24 小时。``crossed_midnight`` 标记航班是否跨零点。
    """
    if scheduled_dep is None or scheduled_arr is None:
        return DurationResult(None, False, "missing")
    minutes = (scheduled_arr - scheduled_dep).total_seconds() / 60
    crossed = scheduled_arr.date() != scheduled_dep.date()
    if minutes < 0:
        minutes += 24 * 60
        crossed = True
    return DurationResult(int(round(minutes)), crossed, None)


def classify_delay(
    delay_min: int | None,
    status_code: str,
    delay_threshold_min: int = 15,
    early_threshold_min: int = 5,
) -> str:
    """把延误分钟数归入 ON_TIME / DELAYED / EARLY / CANCELLED / UNKNOWN。"""
    if status_code == STATUS_CANCELLED:
        return "CANCELLED"
    if delay_min is None:
        return "UNKNOWN"
    if delay_min >= delay_threshold_min:
        return "DELAYED"
    if delay_min <= -early_threshold_min:
        return "EARLY"
    return "ON_TIME"


# ---------------------------------------------------------------------------
# 主键
# ---------------------------------------------------------------------------
def build_flight_id(
    flight_no: str,
    flight_date: date,
    dep_airport_code: str,
    arr_airport_code: str,
    scheduled_dep: datetime | None,
) -> str:
    """航班业务主键：航班号 + 日期 + 起降机场 + 计划起飞时间。"""
    raw = "|".join(
        [
            flight_no.upper(),
            flight_date.isoformat(),
            dep_airport_code.upper(),
            arr_airport_code.upper(),
            scheduled_dep.strftime("%Y-%m-%d %H:%M") if scheduled_dep else "",
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def build_row_hash(record: dict[str, object]) -> str:
    """ODS 行哈希：对全部业务字段做稳定摘要，用于幂等去重。"""
    fields = (
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
    )
    parts = [normalize_text(record.get(f)) for f in fields]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def build_route_id(dep_airport_code: str, arr_airport_code: str) -> str:
    raw = f"{dep_airport_code.upper()}-{arr_airport_code.upper()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def build_batch_id(source_name: str, moment: datetime) -> str:
    return f"{source_name}_{moment.strftime('%Y%m%d%H%M%S')}"
