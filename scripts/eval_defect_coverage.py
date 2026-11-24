"""缺陷注入 → 检测效果 评估。

对仿真源生成的每一条原始记录，标注它被注入的缺陷类型，再跑一遍
「标准化 + 维表关联 + 质量规则」，统计每类缺陷的最终去向：

* ``detected``  —— 命中至少一条质量规则：问题被记录下来，可追溯
* ``absorbed``  —— 没命中规则，但脏值在标准化/维表关联中被正确消解
                    （例如带空格与「国际机场」后缀的机场名）
* ``unhandled`` —— 既没记录也没消解：脏值原样进入 DWD，属于漏检

用法::

    python scripts/eval_defect_coverage.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from aerodata.config import DbConfig  # noqa: E402
from aerodata.db.engine import Database  # noqa: E402
from aerodata.etl.cleaning import DimensionIndex, FlightTransformer  # noqa: E402
from aerodata.etl.standards import normalize_text  # noqa: E402
from aerodata.quality.rules import check_batch, check_row  # noqa: E402
from aerodata.sources.simulator import SimulatorSource  # noqa: E402

#: 仿真源没有把"注入了哪种缺陷"记在记录里，这里用同构子类把它截下来
class TaggedSimulatorSource(SimulatorSource):
    """在注入缺陷时顺手记下缺陷类型。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.defect_of: dict[int, str] = {}

    def _apply_defect(self, record: dict[str, Any], flight_key: str) -> None:
        from aerodata.sources.simulator import DEFECT_KINDS, _unit

        if self.defect_rate > 0 and _unit(flight_key, "defect") < self.defect_rate:
            kind = DEFECT_KINDS[int(_unit(flight_key, "defect_kind") * len(DEFECT_KINDS))]
            if kind in ("exact_duplicate", "re_observation"):
                kind = "none"
            self.defect_of[id(record)] = kind
        super()._apply_defect(record, flight_key)

    def _inject_exact_duplicates(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from aerodata.sources.simulator import _unit

        out: list[dict[str, Any]] = []
        for index, record in enumerate(records):
            out.append(record)
            key = f"{self.seed}|dup|{index}"
            roll = _unit(key, "kind")
            if roll < 0.012:
                dup = dict(record)
                self.defect_of[id(dup)] = "exact_duplicate"
                out.append(dup)
            elif roll < 0.022:
                dup = dict(record)
                if record.get("status") not in ("已到达", "取消", "计划"):
                    dup["status"] = "已到达"
                    self.defect_of[id(dup)] = "re_observation"
                    out.append(dup)
        return out


def _resolved(kind: str, raw: dict[str, Any], row: dict[str, Any]) -> bool:
    """该缺陷在 DWD 中是否已被正确消解。"""
    if kind == "dirty_airport_name":
        return bool(row.get("dep_airport_code"))
    if kind == "missing_country":
        return bool(row.get("arr_country_code") or row.get("arr_country"))
    if kind == "bad_plan_time":
        return row.get("scheduled_dep_time") is not None
    if kind == "bad_actual_time":
        return row.get("actual_dep_time") is not None
    if kind == "unknown_airport":
        return bool(row.get("dep_airport_code"))
    if kind == "unknown_airline":
        return bool(row.get("airline_id"))
    if kind == "missing_flight_no":
        return bool(row.get("flight_no"))
    if kind == "missing_airline":
        return bool(row.get("airline_name"))
    if kind == "arrival_before_departure":
        return bool(row.get("actual_arr_time")) and row.get("actual_arr_time") >= row.get("actual_dep_time")
    return False


def main() -> int:
    config_path = PROJECT_ROOT / "config" / "local.json"
    raw_config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    db_path = PROJECT_ROOT / raw_config["db"]["path"]
    if not db_path.exists():
        print("找不到本地数仓，请先运行：python -m aerodata.cli init-db")
        return 1

    db = Database(DbConfig(dialect="sqlite", path=str(db_path))).connect()
    dims = DimensionIndex.load(db)
    transformer = FlightTransformer(dims=dims)

    source = TaggedSimulatorSource(
        routes_file=PROJECT_ROOT / raw_config["source"]["routes_file"],
        days=int(raw_config["source"].get("days", 14)),
        seed=int(raw_config["source"].get("seed", 20260101)),
        defect_rate=float(raw_config["source"].get("defect_rate", 0.06)),
    )
    batch = source.fetch(None)

    stats: dict[str, Counter] = defaultdict(Counter)
    rules_by_kind: dict[str, Counter] = defaultdict(Counter)

    # 1) 逐条标准化 + 行级规则
    rows: list[dict[str, Any]] = []
    per_record: list[tuple[str, dict[str, Any], list[Any]]] = []
    for record in batch.records:
        kind = source.defect_of.get(id(record), "none")
        cleaned = transformer.transform(record, "eval")
        anomalies = list(cleaned.anomalies) + check_row(cleaned.row)
        for anomaly in anomalies:
            rules_by_kind[kind][anomaly.rule_code] += 1
        rows.append(cleaned.row)
        per_record.append((kind, cleaned.row, anomalies))

    # 2) 批次级重复：去重之前检测，命中 DUPLICATE_FLIGHT 的 flight_id 视为已记录
    duplicated_ids = {anomaly.flight_id for anomaly in check_batch(rows)}

    # 3) 逐条判定最终去向
    for kind, row, anomalies in per_record:
        if kind in ("exact_duplicate", "re_observation"):
            detected = str(row.get("flight_id")) in duplicated_ids
            if detected:
                rules_by_kind[kind]["DUPLICATE_FLIGHT"] += 1
            outcome = "detected" if detected else "unhandled"
        elif anomalies:
            outcome = "detected"
        elif _resolved(kind, {}, row):
            outcome = "absorbed"
        else:
            outcome = "unhandled"
        stats[kind][outcome] += 1
        stats[kind]["total"] += 1

    print(f"批次：{batch.source_name}  源端 {batch.row_count} 条")
    print()
    header = f"{'缺陷类型':<26}{'条数':>7}{'检出':>8}{'消解':>8}{'漏检':>8}  检出率"
    print(header)
    print("-" * len(header))
    total_detected = total_absorbed = total_unhandled = 0
    for kind in sorted(stats, key=lambda k: -stats[k]["total"]):
        counter = stats[kind]
        total = counter["total"]
        detected, absorbed, unhandled = counter["detected"], counter["absorbed"], counter["unhandled"]
        total_detected += detected
        total_absorbed += absorbed
        total_unhandled += unhandled
        rate = f"{100.0 * detected / total:6.1f}%" if total else "   -  "
        print(
            f"{kind:<26}{total:>7}{detected:>8}{absorbed:>8}{unhandled:>8}  {rate}"
            + ("  <- 批次级" if kind in ("exact_duplicate", "re_observation") else "")
        )
    grand = total_detected + total_absorbed + total_unhandled
    print("-" * len(header))
    print(
        f"{'合计':<24}{grand:>7}{total_detected:>8}{total_absorbed:>8}{total_unhandled:>8}"
        f"  {100.0 * total_detected / grand:6.1f}%"
    )
    print()
    print("各缺陷类型命中的规则：")
    for kind in sorted(rules_by_kind):
        if not rules_by_kind[kind]:
            continue
        detail = "，".join(f"{code}×{n}" for code, n in rules_by_kind[kind].most_common())
        print(f"  {kind:<26}{detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
