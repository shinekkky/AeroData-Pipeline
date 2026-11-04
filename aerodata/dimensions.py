"""维度表装载。

维度不靠"在 SQL 里手写常量"维护，而是从可版本化的参考文件装载：

* ``data/reference/routes_daily.json`` —— 采集器使用的真实航线清单（机场/航线维度的来源）
* ``data/reference/airlines.csv``         —— 航空公司标准编码表
* ``data/reference/airport_overrides.csv`` —— 航线清单里缺失 IATA 码的机场补齐表
* ``data/reference/countries.csv``        —— 国家/地区与 ISO-3166 alpha-2 映射
"""

from __future__ import annotations

import csv
import json
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

from .db.engine import Database
from .etl.standards import (
    build_route_id,
    normalize_airline_key,
    normalize_airport_key,
    normalize_text,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REFERENCE_DIR = PROJECT_ROOT / "data" / "reference"


# ---------------------------------------------------------------------------
# 参考数据读取
# ---------------------------------------------------------------------------
def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [
            {k: (v or "").strip() for k, v in row.items() if k}
            for row in csv.DictReader(handle)
        ]


def load_country_map(reference_dir: Path = REFERENCE_DIR) -> dict[str, str]:
    """国家中文名 → ISO-3166 alpha-2。"""
    return {row["country"]: row["country_code"] for row in _read_csv(reference_dir / "countries.csv")}


def load_airlines(reference_dir: Path = REFERENCE_DIR) -> list[dict[str, Any]]:
    rows = []
    for row in _read_csv(reference_dir / "airlines.csv"):
        rows.append(
            {
                "airline_code": row["airline_code"].upper(),
                "airline_name": row["airline_name"],
                "airline_name_norm": normalize_airline_key(row["airline_name"]),
                "airline_name_en": row.get("airline_name_en", ""),
                "country": row.get("country", ""),
                "country_code": row.get("country_code", ""),
            }
        )
    return rows


def load_airport_overrides(reference_dir: Path = REFERENCE_DIR) -> dict[str, dict[str, str]]:
    return {row["airport_name"]: row for row in _read_csv(reference_dir / "airport_overrides.csv")}


def load_airport_aliases(reference_dir: Path = REFERENCE_DIR) -> dict[str, dict[str, str]]:
    """人工维护的机场别名表：别名 -> 标准机场编码。"""
    return {
        row["alias_name"]: row
        for row in _read_csv(reference_dir / "airport_aliases.csv")
        if row.get("alias_name") and row.get("airport_code")
    }


def load_routes(routes_file: str | Path) -> list[dict[str, Any]]:
    """读取航线清单（兼容 BOM）。"""
    path = Path(routes_file)
    if not path.exists():
        raise FileNotFoundError(f"航线清单不存在: {path}")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        data = data.get("routes", [])
    return [r for r in data if isinstance(r, dict)]


# ---------------------------------------------------------------------------
# 维度构建
# ---------------------------------------------------------------------------
def build_airport_rows(
    routes: list[dict[str, Any]],
    country_map: dict[str, str],
    overrides: dict[str, dict[str, str]],
    aliases: dict[str, dict[str, str]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    """由航线清单推导机场维度与别名维度。

    返回 ``(机场列表, 别名列表, 冲突列表)``。

    核心思路：**一个机场一个编码**。源数据里同一机场可能以多个中文名出现
    （如"沙巴"与"斗湖"其实是哥打京那巴鲁国际机场），归并到同一个 airport_code，
    其余名称进别名表，这样"机场代码不统一"的问题在维度层就解决了。
    """
    aliases = aliases or {}
    # 1) 名称 -> 编码
    entries: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

    def _collect(name: str, iata: str, city: str, country: str) -> None:
        name = normalize_text(name)
        key = normalize_airport_key(name)
        if not key:
            return
        entry = entries.get(key)
        if entry is None:
            entries[key] = {
                "name": name,
                "code": (iata or "").upper(),
                "city": normalize_text(city),
                "country": normalize_text(country),
            }
            return
        if not entry["code"] and iata:
            entry["code"] = iata.upper()
        if not entry["city"] and city:
            entry["city"] = normalize_text(city)
        if not entry["country"] and country:
            entry["country"] = normalize_text(country)

    for route in routes:
        _collect(route.get("from", ""), route.get("from_iata", ""), route.get("from_city", ""), route.get("from_country", ""))
        _collect(route.get("to", ""), route.get("to_iata", ""), route.get("to_city", ""), route.get("to_country", ""))

    # 2) 人工校正表优先：override 里的编码视为已核对过的标准值
    for name, override in overrides.items():
        key = normalize_airport_key(name)
        if key in entries:
            entries[key]["code"] = override["iata_code"].upper()
        else:
            entries[key] = {
                "name": name,
                "code": override["iata_code"].upper(),
                "city": normalize_text(name),
                "country": "",
            }

    # 3) 人工别名表：别名 -> 编码
    for name, alias in aliases.items():
        key = normalize_airport_key(name)
        code = alias["airport_code"].upper()
        if key in entries:
            entries[key]["code"] = code
        else:
            entries[key] = {
                "name": name,
                "code": code,
                "city": normalize_text(name),
                "country": "",
            }

    # 4) 按编码归并
    conflicts: list[dict[str, str]] = []
    merged: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for key, entry in entries.items():
        code = entry["code"]
        if not code:
            conflicts.append(
                {
                    "type": "missing_code",
                    "airport": entry["name"],
                    "detail": "航线清单与补码表均未提供 IATA 码，落占位编码 UNK",
                }
            )
            code = "UNK"
        item = merged.get(code)
        if item is None:
            item = merged[code] = {
                "airport_code": code,
                "airport_name": entry["name"],
                "airport_name_norm": key,
                "airport_name_en": "",
                "city": entry["city"],
                "country": entry["country"],
                "country_code": country_map.get(entry["country"], ""),
                "is_hub": 0,
                "source": "routes_daily",
                "alias_names": [],
            }
        else:
            # 同一编码的第二个名字 —— 作为别名挂上去
            item["alias_names"].append(entry["name"])
            if not item["city"] and entry["city"]:
                item["city"] = entry["city"]
            if not item["country"] and entry["country"]:
                item["country"] = entry["country"]
                item["country_code"] = country_map.get(entry["country"], "")
        if not item["city"]:
            item["city"] = normalize_text(entry["name"])

    # 5) 套用人工属性（枢纽标记、国家）
    by_norm = {normalize_airport_key(name): override for name, override in overrides.items()}
    for item in merged.values():
        override = by_norm.get(item["airport_name_norm"])
        if override:
            item["is_hub"] = int(override.get("is_hub") or 0)
            if override.get("country_code"):
                item["country_code"] = override["country_code"]
            item["source"] = f"{item['source']}+override"

    # 6) 生成别名行
    alias_rows: list[dict[str, Any]] = []
    seen_alias: set[str] = set()
    for item in merged.values():
        names = [item["airport_name"], *item["alias_names"]]
        for name in names:
            norm = normalize_airport_key(name)
            if not norm or norm in seen_alias:
                continue
            seen_alias.add(norm)
            alias_rows.append(
                {
                    "alias_name": name,
                    "alias_name_norm": norm,
                    "airport_code": item["airport_code"],
                    "source": "auto" if norm == item["airport_name_norm"] else "merged_name",
                }
            )

    # 7) 人工别名表里指向不存在编码的别名要显式报出来
    known_codes = set(merged)
    for name, alias in aliases.items():
        if alias["airport_code"].upper() not in known_codes:
            conflicts.append(
                {
                    "type": "dangling_alias",
                    "airport": name,
                    "detail": f"别名指向的编码 {alias['airport_code']} 不在 dim_airport 中",
                }
            )

    return list(merged.values()), alias_rows, conflicts


def build_route_rows(
    routes: list[dict[str, Any]],
    name_to_code: dict[str, str],
    airport_by_code: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """由航线清单推导航线维度。"""
    rows: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for route in routes:
        dep_code = name_to_code.get(normalize_airport_key(route.get("from", "")), "")
        arr_code = name_to_code.get(normalize_airport_key(route.get("to", "")), "")
        if not dep_code or not arr_code:
            continue
        dep = airport_by_code.get(dep_code, {})
        arr = airport_by_code.get(arr_code, {})
        route_id = build_route_id(dep_code, arr_code)
        rows.setdefault(
            route_id,
            {
                "route_id": route_id,
                "dep_airport_code": dep_code,
                "arr_airport_code": arr_code,
                "route_name": f"{dep.get('airport_name', '')}-{arr.get('airport_name', '')}",
                "dep_city": dep.get("city", ""),
                "arr_city": arr.get("city", ""),
                "dep_country": dep.get("country", ""),
                "arr_country": arr.get("country", ""),
                "is_international": 0 if dep.get("country_code") == arr.get("country_code") else 1,
                "source": "routes_daily",
            },
        )
    return list(rows.values())


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------
def load_dimensions(
    db: Database,
    routes_file: str | Path,
    reference_dir: Path = REFERENCE_DIR,
    now: datetime | None = None,
) -> dict[str, int]:
    """装载机场 / 航空公司 / 航线三个维度表，返回各表写入行数。"""
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    routes = load_routes(routes_file)
    country_map = load_country_map(reference_dir)
    overrides = load_airport_overrides(reference_dir)
    aliases = load_airport_aliases(reference_dir)

    airport_rows, alias_rows, conflicts = build_airport_rows(
        routes, country_map, overrides, aliases
    )
    for conflict in conflicts:
        print(
            f"[dimensions][warn] 机场维度冲突 {conflict['type']}: "
            f"{conflict['airport']} —— {conflict['detail']}"
        )
    airport_by_code = {row["airport_code"]: row for row in airport_rows}
    name_to_code = {row["airport_name_norm"]: row["airport_code"] for row in airport_rows}
    for row in alias_rows:
        name_to_code[row["alias_name_norm"]] = row["airport_code"]
    route_rows = build_route_rows(routes, name_to_code, airport_by_code)
    airline_rows = load_airlines(reference_dir)

    for row in airport_rows:
        row["created_at"] = stamp
        row["updated_at"] = stamp
    for row in alias_rows:
        row["created_at"] = stamp
        row["updated_at"] = stamp
    for row in airline_rows:
        row["created_at"] = stamp
        row["updated_at"] = stamp
    for row in route_rows:
        row["created_at"] = stamp
        row["updated_at"] = stamp

    written: dict[str, int] = {}

    written["dim_airport"] = db.upsert(
        "dim_airport",
        [
            "airport_code",
            "airport_name",
            "airport_name_norm",
            "airport_name_en",
            "city",
            "country",
            "country_code",
            "is_hub",
            "source",
            "created_at",
            "updated_at",
        ],
        [
            [
                r["airport_code"],
                r["airport_name"],
                r["airport_name_norm"],
                r["airport_name_en"],
                r["city"],
                r["country"],
                r["country_code"],
                r["is_hub"],
                r["source"],
                r["created_at"],
                r["updated_at"],
            ]
            for r in airport_rows
        ],
        key_columns=["airport_code"],
    )

    written["dim_airport_alias"] = db.upsert(
        "dim_airport_alias",
        ["alias_name", "alias_name_norm", "airport_code", "source", "created_at", "updated_at"],
        [
            [
                r["alias_name"],
                r["alias_name_norm"],
                r["airport_code"],
                r["source"],
                r["created_at"],
                r["updated_at"],
            ]
            for r in alias_rows
        ],
        key_columns=["alias_name_norm"],
    )

    written["dim_airline"] = db.upsert(
        "dim_airline",
        [
            "airline_code",
            "airline_name",
            "airline_name_norm",
            "airline_name_en",
            "country",
            "country_code",
            "created_at",
            "updated_at",
        ],
        [
            [
                r["airline_code"],
                r["airline_name"],
                r["airline_name_norm"],
                r["airline_name_en"],
                r["country"],
                r["country_code"],
                r["created_at"],
                r["updated_at"],
            ]
            for r in airline_rows
        ],
        key_columns=["airline_code"],
    )

    written["dim_route"] = db.upsert(
        "dim_route",
        [
            "route_id",
            "dep_airport_code",
            "arr_airport_code",
            "route_name",
            "dep_city",
            "arr_city",
            "dep_country",
            "arr_country",
            "is_international",
            "source",
            "created_at",
            "updated_at",
        ],
        [
            [
                r["route_id"],
                r["dep_airport_code"],
                r["arr_airport_code"],
                r["route_name"],
                r["dep_city"],
                r["arr_city"],
                r["dep_country"],
                r["arr_country"],
                r["is_international"],
                r["source"],
                r["created_at"],
                r["updated_at"],
            ]
            for r in route_rows
        ],
        key_columns=["route_id"],
    )
    return written
