"""命令行入口。

    python -m aerodata.cli init-db          # 建表 + 装载维度
    python -m aerodata.cli run               # 跑一次 ETL（含质量检查与报告）
    python -m aerodata.cli run --incremental # 增量跑
    python -m aerodata.cli report            # 只生成质量报告
    python -m aerodata.cli stats             # 看各表行数
    python -m aerodata.cli query "SELECT ..."# 查数
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, PipelineConfig, load_config
from .db.engine import Database
from .db.schema import create_all
from .dimensions import load_dimensions
from .etl.pipeline import EtlPipeline
from .quality.report import build_report, write_report
from .sources.base import FlightSource

DEFAULT_ROUTES = PROJECT_ROOT / "data" / "reference" / "routes_daily.json"


# ---------------------------------------------------------------------------
# 采集源构建
# ---------------------------------------------------------------------------
def build_source(config: PipelineConfig) -> FlightSource:
    src = config.source
    kind = (src.type or "simulator").lower()

    if kind == "simulator":
        from .sources.simulator import SimulatorSource

        return SimulatorSource(
            routes_file=src.routes_file or DEFAULT_ROUTES,
            days=src.days,
            seed=src.seed,
            defect_rate=src.defect_rate,
            name=src.name or "simulator",
        )

    if kind == "csv":
        from .sources.csv_files import CsvFileSource

        if not src.path:
            raise SystemExit("使用 --source csv 时必须提供 source.path")
        return CsvFileSource(src.path, name=src.name or "csv")

    if kind == "mysql":
        from .sources.mysql_collector import MySQLCollectorSource

        if not src.settings_file:
            raise SystemExit("使用 --source mysql 时必须提供 source.settings_file")
        return MySQLCollectorSource(
            settings_file=src.settings_file,
            settings_key=src.settings_key,
            table=src.table,
            name=src.name or None,
        )

    raise SystemExit(f"未知的采集源类型: {kind}（可选 simulator / csv / mysql）")


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
def cmd_init_db(args: argparse.Namespace, config: PipelineConfig) -> int:
    with Database(config.db).connect() as db:
        statements = create_all(db)
        print(f"[init-db] 已创建/确认 {len(statements)} 条建表与建索引语句（{config.db.dialect}）")
        counts = load_dimensions(db, config.source.routes_file or DEFAULT_ROUTES)
        for name, count in counts.items():
            print(f"[init-db] {name}: 写入 {count} 行")
    return 0


def cmd_run(args: argparse.Namespace, config: PipelineConfig) -> int:
    since = date.fromisoformat(args.since) if args.since else None
    with Database(config.db).connect() as db:
        if not db.table_exists("dwd_flight"):
            print("[run] 未检测到数仓表，先执行 init-db")
            create_all(db)
            load_dimensions(db, config.source.routes_file or DEFAULT_ROUTES)

        source = build_source(config)
        pipeline = EtlPipeline(db, config, source)
        result = pipeline.run(incremental=args.incremental, since=since)

        print(f"[run] 批次 {result.batch_id} 状态 {result.status} 耗时 {result.duration_sec}s")
        print(
            f"[run] 源端 {result.source_row_cnt} 条 -> ODS 新增 {result.ods_new_cnt} / "
            f"幂等跳过 {result.ods_skip_cnt} -> DWD 新增 {result.dwd_insert_cnt} / "
            f"更新 {result.dwd_update_cnt} / 去重 {result.dwd_skip_cnt}"
        )
        print(f"[run] 质量异常命中 {result.anomaly_cnt} 条，入库 {result.anomaly_persisted_cnt} 条")
        print(f"[run] DWS 日汇总 {result.dws_daily_cnt} 行，航线汇总 {result.dws_route_cnt} 行")

        if not args.no_report:
            report = build_report(db, batch_id=result.batch_id, top_n=args.top)
            json_path, md_path = write_report(report, Path(config.reports_dir), prefix=result.batch_id)
            print(f"[run] 质量报告: {md_path} / {json_path}")
    return 0


def cmd_report(args: argparse.Namespace, config: PipelineConfig) -> int:
    with Database(config.db).connect() as db:
        report = build_report(db, batch_id=args.batch_id, top_n=args.top)
        prefix = args.batch_id or f"quality_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        json_path, md_path = write_report(report, Path(config.reports_dir), prefix=prefix)
    print(f"[report] 已生成 {md_path} / {json_path}")
    return 0


def cmd_reset_db(args: argparse.Namespace, config: PipelineConfig) -> int:
    """清空数仓（本地演示用）。MySQL 模式下只删表不删库。"""
    from .db.schema import drop_all

    with Database(config.db).connect() as db:
        drop_all(db)
    print("[reset-db] 已清空本地数仓表结构")
    return 0


def cmd_stats(args: argparse.Namespace, config: PipelineConfig) -> int:
    with Database(config.db).connect() as db:
        from .db.schema import ALL_TABLES

        payload: dict[str, Any] = {}
        for table in ALL_TABLES:
            try:
                payload[table.name] = db.count(table.name)
            except Exception:  # noqa: BLE001
                payload[table.name] = None
        batch = db.fetch_one("SELECT * FROM etl_batch_log ORDER BY started_at DESC LIMIT 1")
        print(json.dumps({"tables": payload, "last_batch": batch}, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_query(args: argparse.Namespace, config: PipelineConfig) -> int:
    sql = args.sql.strip()
    if not sql.lower().startswith(("select", "show", "describe", "with")):
        raise SystemExit("query 只允许只读语句（SELECT / SHOW / DESCRIBE / WITH）")
    with Database(config.db).connect() as db:
        rows = db.fetch_all(sql)
    if not rows:
        print("（无结果）")
        return 0
    print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    return 0


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aerodata", description="航空数据治理与数仓分层 ETL 流水线"
    )
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config/config.json）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-db", help="建表并装载维度数据")
    p_init.set_defaults(func=cmd_init_db)

    p_reset = sub.add_parser("reset-db", help="清空本地数仓（演示用）")
    p_reset.set_defaults(func=cmd_reset_db)

    p_run = sub.add_parser("run", help="执行一次 ETL 流水线")
    p_run.add_argument("--source", choices=["simulator", "csv", "mysql"], default=None, help="采集源类型")
    p_run.add_argument("--days", type=int, default=None, help="仿真源生成天数")
    p_run.add_argument("--seed", type=int, default=None, help="仿真源随机种子")
    p_run.add_argument("--defect-rate", type=float, default=None, help="脏数据注入比例")
    p_run.add_argument("--path", default=None, help="CSV 源目录/文件路径")
    p_run.add_argument("--settings-file", default=None, help="MySQL 源配置文件")
    p_run.add_argument("--table", default=None, help="MySQL 源表名")
    p_run.add_argument("--incremental", action="store_true", help="按水位增量处理")
    p_run.add_argument("--since", default=None, help="起始日期 YYYY-MM-DD")
    p_run.add_argument("--top", type=int, default=10, help="报告 TOP N")
    p_run.add_argument("--no-report", action="store_true", help="不生成质量报告")
    p_run.set_defaults(func=cmd_run)

    p_report = sub.add_parser("report", help="生成数据质量报告")
    p_report.add_argument("--batch-id", default=None, help="只统计指定批次")
    p_report.add_argument("--top", type=int, default=10)
    p_report.set_defaults(func=cmd_report)

    p_stats = sub.add_parser("stats", help="查看各表行数与最近批次")
    p_stats.set_defaults(func=cmd_stats)

    p_query = sub.add_parser("query", help="执行只读 SQL")
    p_query.add_argument("sql")
    p_query.set_defaults(func=cmd_query)

    return parser


def apply_overrides(config: PipelineConfig, args: argparse.Namespace) -> PipelineConfig:
    """命令行参数覆盖配置文件的来源相关设置。"""
    from dataclasses import replace

    source = config.source
    changes: dict[str, Any] = {}
    if getattr(args, "source", None):
        changes["type"] = args.source
    if getattr(args, "days", None) is not None:
        changes["days"] = args.days
    if getattr(args, "seed", None) is not None:
        changes["seed"] = args.seed
    if getattr(args, "defect_rate", None) is not None:
        changes["defect_rate"] = args.defect_rate
    if getattr(args, "path", None):
        changes["path"] = args.path
    if getattr(args, "settings_file", None):
        changes["settings_file"] = args.settings_file
    if getattr(args, "table", None):
        changes["table"] = args.table
    if changes:
        source = replace(source, **changes)
    return replace(config, source=source)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = apply_overrides(load_config(args.config), args)
    try:
        return int(args.func(args, config))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
