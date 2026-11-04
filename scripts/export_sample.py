"""导出样例明细文件，用于演示 CSV / JSON 采集源。

    python scripts/export_sample.py --days 3 --out data/sample

导出的是**采集器原始格式**（未清洗），并且刻意保留了几种脏数据写法，
用来验证 CSV 源的列名映射与清洗链路。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from aerodata.sources.simulator import SimulatorSource  # noqa: E402

#: 导出列（中文表头，演示别名映射能力）
CSV_HEADER = [
    "航班号",
    "航空公司",
    "出发国家",
    "出发城市",
    "出发机场",
    "计划起飞",
    "实际起飞",
    "到达国家",
    "到达城市",
    "到达省份",
    "到达机场",
    "计划到达",
    "实际到达",
    "状态",
    "航班日期",
]

KEY_MAP = {
    "flight_no": "航班号",
    "airline": "航空公司",
    "dep_country": "出发国家",
    "dep_city": "出发城市",
    "dep_airport": "出发机场",
    "dep_plan": "计划起飞",
    "dep_actual": "实际起飞",
    "arr_country": "到达国家",
    "arr_city": "到达城市",
    "arr_province": "到达省份",
    "arr_airport": "到达机场",
    "arr_plan": "计划到达",
    "arr_actual": "实际到达",
    "status": "状态",
    "flight_date": "航班日期",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="导出样例航班明细")
    parser.add_argument("--days", type=int, default=3, help="导出天数")
    parser.add_argument("--seed", type=int, default=20260101, help="随机种子")
    parser.add_argument("--routes", default=str(PROJECT_ROOT / "data" / "reference" / "routes_daily.json"))
    parser.add_argument("--out", default=str(PROJECT_ROOT / "data" / "sample"), help="输出目录")
    args = parser.parse_args()

    source = SimulatorSource(routes_file=args.routes, days=args.days, seed=args.seed, defect_rate=0.08)
    batch = source.fetch()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_path = out_dir / "flights_sample.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        for record in batch.records:
            writer.writerow([record.get(KEY_MAP_COL, "") for KEY_MAP_COL in KEY_MAP])

    json_path = out_dir / "flights_sample.json"
    json_path.write_text(
        json.dumps(batch.records, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    print(f"[export] {len(batch.records)} 条记录 -> {csv_path}")
    print(f"[export] {len(batch.records)} 条记录 -> {json_path}")
    print(f"[export] 航班日期范围: {min(r['flight_date'] for r in batch.records)} ~ "
          f"{max(r['flight_date'] for r in batch.records)}")
    return 0


KEY_MAP_COL = {v: k for k, v in KEY_MAP.items()}

if __name__ == "__main__":
    raise SystemExit(main())
