"""数仓表结构定义（ODS / DWD / DWS / 维表 / 质量表 / 元数据表）。

同一份表结构同时渲染成 SQLite 与 MySQL 两套 DDL：

* 类型统一选用两边都接受的名字（``VARCHAR`` / ``DATETIME`` / ``DECIMAL`` / ``INT``）
* 只有自增主键、时间戳默认值、引擎选项三处按方言分化
* 时间戳一律由 Python 侧写入，避免两种数据库的时区/默认值差异
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .engine import Database

MYSQL_TABLE_OPTIONS = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool = True
    primary_key: bool = False
    autoincrement: bool = False
    default: str | None = None
    comment: str = ""


@dataclass(frozen=True)
class Index:
    name: str
    columns: Sequence[str]
    unique: bool = False


@dataclass(frozen=True)
class Table:
    name: str
    columns: Sequence[Column]
    indexes: Sequence[Index] = field(default_factory=tuple)
    comment: str = ""

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


# ---------------------------------------------------------------------------
# 维度表
# ---------------------------------------------------------------------------
DIM_AIRPORT = Table(
    name="dim_airport",
    comment="机场维度表：机场标准编码与多语言名称，ODS/DWD 的名称标准化都依赖它",
    columns=(
        Column("airport_code", "VARCHAR(8)", nullable=False, primary_key=True, comment="IATA 三字码"),
        Column("airport_name", "VARCHAR(128)", nullable=False, comment="机场中文名（采集源原值）"),
        Column("airport_name_norm", "VARCHAR(128)", nullable=False, comment="标准化名称，用于匹配"),
        Column("airport_name_en", "VARCHAR(128)", comment="英文名"),
        Column("city", "VARCHAR(64)", comment="城市"),
        Column("country", "VARCHAR(64)", comment="国家/地区"),
        Column("country_code", "CHAR(2)", comment="ISO-3166 alpha-2"),
        Column("is_hub", "INT", default="0", comment="是否枢纽机场"),
        Column("source", "VARCHAR(32)", comment="维度来源"),
        Column("created_at", "DATETIME", nullable=False),
        Column("updated_at", "DATETIME", nullable=False),
    ),
    indexes=(
        Index("uk_airport_name_norm", ("airport_name_norm",), unique=True),
        Index("idx_airport_country", ("country",)),
    ),
)

DIM_AIRPORT_ALIAS = Table(
    name="dim_airport_alias",
    comment="机场别名表：把采集到的各种写法映射到标准机场编码，解决机场代码不统一",
    columns=(
        Column("alias_name", "VARCHAR(128)", nullable=False, primary_key=True, comment="别名原文"),
        Column("alias_name_norm", "VARCHAR(128)", nullable=False, comment="标准化别名"),
        Column("airport_code", "VARCHAR(8)", nullable=False, comment="标准机场编码"),
        Column("source", "VARCHAR(32)", comment="别名来源"),
        Column("created_at", "DATETIME", nullable=False),
        Column("updated_at", "DATETIME", nullable=False),
    ),
    indexes=(
        Index("uk_alias_norm", ("alias_name_norm",), unique=True),
        Index("idx_alias_code", ("airport_code",)),
    ),
)

DIM_AIRLINE = Table(
    name="dim_airline",
    comment="航空公司维度表：航空公司编码与名称，用于把采集到的中文名映射到标准编码",
    columns=(
        Column("airline_code", "VARCHAR(8)", nullable=False, primary_key=True, comment="IATA/ICAO 航空公司编码"),
        Column("airline_name", "VARCHAR(128)", nullable=False, comment="中文名称"),
        Column("airline_name_norm", "VARCHAR(128)", nullable=False, comment="标准化名称，用于匹配"),
        Column("airline_name_en", "VARCHAR(128)", comment="英文名称"),
        Column("country", "VARCHAR(64)", comment="注册国家"),
        Column("country_code", "CHAR(2)", comment="ISO-3166 alpha-2"),
        Column("created_at", "DATETIME", nullable=False),
        Column("updated_at", "DATETIME", nullable=False),
    ),
    indexes=(
        Index("uk_airline_name_norm", ("airline_name_norm",), unique=True),
    ),
)

DIM_ROUTE = Table(
    name="dim_route",
    comment="航线维度表：出发/到达机场组合，航线级指标的分母",
    columns=(
        Column("route_id", "VARCHAR(32)", nullable=False, primary_key=True, comment="航线主键"),
        Column("dep_airport_code", "VARCHAR(8)", nullable=False, comment="出发机场编码"),
        Column("arr_airport_code", "VARCHAR(8)", nullable=False, comment="到达机场编码"),
        Column("route_name", "VARCHAR(160)", comment="航线展示名"),
        Column("dep_city", "VARCHAR(64)"),
        Column("arr_city", "VARCHAR(64)"),
        Column("dep_country", "VARCHAR(64)"),
        Column("arr_country", "VARCHAR(64)"),
        Column("is_international", "INT", default="0", comment="是否国际航线"),
        Column("source", "VARCHAR(32)"),
        Column("created_at", "DATETIME", nullable=False),
        Column("updated_at", "DATETIME", nullable=False),
    ),
    indexes=(
        Index("uk_route_airports", ("dep_airport_code", "arr_airport_code"), unique=True),
    ),
)

# ---------------------------------------------------------------------------
# ODS 原始层：字段与采集器 asean_flights_table 保持一致，只做落地不做加工
# ---------------------------------------------------------------------------
ODS_FLIGHT = Table(
    name="ods_flight",
    comment="ODS 原始层：采集源原样落地，附批次号与行哈希用于幂等",
    columns=(
        Column("ods_id", "INT", nullable=False, primary_key=True, autoincrement=True),
        Column("batch_id", "VARCHAR(32)", nullable=False, comment="ETL 批次号"),
        Column("source_name", "VARCHAR(32)", nullable=False, comment="采集源标识"),
        Column("flight_no", "VARCHAR(32)", comment="航班号（原始）"),
        Column("airline", "VARCHAR(128)", comment="航空公司（原始）"),
        Column("dep_country", "VARCHAR(64)"),
        Column("dep_city", "VARCHAR(64)"),
        Column("dep_airport", "VARCHAR(128)"),
        Column("dep_plan", "VARCHAR(32)", comment="计划起飞时间（原始）"),
        Column("dep_actual", "VARCHAR(32)", comment="实际起飞时间（原始）"),
        Column("arr_country", "VARCHAR(64)"),
        Column("arr_city", "VARCHAR(64)"),
        Column("arr_province", "VARCHAR(64)"),
        Column("arr_airport", "VARCHAR(128)"),
        Column("arr_plan", "VARCHAR(32)", comment="计划到达时间（原始）"),
        Column("arr_actual", "VARCHAR(32)", comment="实际到达时间（原始）"),
        Column("status", "VARCHAR(32)", comment="航班状态（原始）"),
        Column("flight_date", "VARCHAR(32)", comment="航班日期（原始）"),
        Column("src_md5", "VARCHAR(64)", comment="采集源自带 md5"),
        Column("row_hash", "VARCHAR(64)", nullable=False, comment="行内容哈希，幂等去重键"),
        Column("source_file", "VARCHAR(255)", comment="来源文件/表"),
        Column("ingest_time", "DATETIME", nullable=False, comment="入库时间"),
    ),
    indexes=(
        Index("uk_ods_row_hash", ("row_hash",), unique=True),
        Index("idx_ods_batch", ("batch_id",)),
        Index("idx_ods_flight_date", ("flight_date",)),
    ),
)

# ---------------------------------------------------------------------------
# DWD 清洗明细层
# ---------------------------------------------------------------------------
DWD_FLIGHT = Table(
    name="dwd_flight",
    comment="DWD 清洗明细层：字段标准化、维表关联、延误计算后的航班明细",
    columns=(
        Column("flight_id", "VARCHAR(40)", nullable=False, primary_key=True, comment="航班业务主键"),
        Column("batch_id", "VARCHAR(32)", nullable=False),
        Column("flight_date", "DATE", nullable=False, comment="航班日期"),
        Column("flight_no", "VARCHAR(32)", nullable=False, comment="标准化航班号"),
        Column("flight_no_raw", "VARCHAR(32)", comment="原始航班号"),
        Column("airline_id", "VARCHAR(8)", comment="航空公司编码"),
        Column("airline_name", "VARCHAR(128)", comment="航空公司名称"),
        Column("route_id", "VARCHAR(32)", comment="航线主键"),
        Column("dep_airport_code", "VARCHAR(8)", comment="出发机场编码"),
        Column("dep_airport_name", "VARCHAR(128)", comment="出发机场名称"),
        Column("dep_city", "VARCHAR(64)"),
        Column("dep_country", "VARCHAR(64)"),
        Column("dep_country_code", "CHAR(2)"),
        Column("arr_airport_code", "VARCHAR(8)", comment="到达机场编码"),
        Column("arr_airport_name", "VARCHAR(128)"),
        Column("arr_city", "VARCHAR(64)"),
        Column("arr_country", "VARCHAR(64)"),
        Column("arr_country_code", "CHAR(2)"),
        Column("scheduled_dep_time", "DATETIME", comment="计划起飞时间"),
        Column("actual_dep_time", "DATETIME", comment="实际起飞时间"),
        Column("scheduled_arr_time", "DATETIME", comment="计划到达时间"),
        Column("actual_arr_time", "DATETIME", comment="实际到达时间"),
        Column("dep_delay_min", "INT", comment="起飞延误分钟（负数表示提前）"),
        Column("arr_delay_min", "INT", comment="到达延误分钟（负数表示提前）"),
        Column("delay_status", "VARCHAR(16)", comment="ON_TIME/DELAYED/EARLY/CANCELLED/UNKNOWN"),
        Column("flight_status", "VARCHAR(16)", comment="标准化航班状态"),
        Column("flight_status_raw", "VARCHAR(32)", comment="原始状态"),
        Column("flight_duration_min", "INT", comment="飞行时长（计划）"),
        Column("has_anomaly", "INT", default="0", comment="是否命中质量规则"),
        Column("created_at", "DATETIME", nullable=False),
        Column("updated_at", "DATETIME", nullable=False),
    ),
    indexes=(
        Index("idx_dwd_date", ("flight_date",)),
        Index("idx_dwd_airline", ("airline_id",)),
        Index("idx_dwd_dep_ap", ("dep_airport_code",)),
        Index("idx_dwd_route", ("route_id",)),
    ),
)

# ---------------------------------------------------------------------------
# DWS 主题汇总层
# ---------------------------------------------------------------------------
DWS_FLIGHT_DAILY = Table(
    name="dws_flight_daily",
    comment="DWS 日汇总：日期 × 航空公司 × 出发机场 的航班量与延误指标",
    columns=(
        Column("id", "INT", nullable=False, primary_key=True, autoincrement=True),
        Column("stat_date", "DATE", nullable=False),
        Column("airline_id", "VARCHAR(8)", nullable=False),
        Column("airline_name", "VARCHAR(128)"),
        Column("dep_airport_code", "VARCHAR(8)", nullable=False),
        Column("dep_airport_name", "VARCHAR(128)"),
        Column("flight_cnt", "INT", default="0", comment="航班量"),
        Column("dep_delay_cnt", "INT", default="0", comment="起飞延误班次"),
        Column("arr_delay_cnt", "INT", default="0", comment="到达延误班次"),
        Column("early_cnt", "INT", default="0", comment="提前起飞班次"),
        Column("cancel_cnt", "INT", default="0", comment="取消班次"),
        Column("on_time_rate", "DECIMAL(6,2)", comment="准点率(%)"),
        Column("avg_dep_delay_min", "DECIMAL(10,2)", comment="平均起飞延误(分钟)"),
        Column("max_dep_delay_min", "INT", comment="最大起飞延误(分钟)"),
        Column("avg_arr_delay_min", "DECIMAL(10,2)", comment="平均到达延误(分钟)"),
        Column("max_arr_delay_min", "INT", comment="最大到达延误(分钟)"),
        Column("avg_flight_duration_min", "DECIMAL(10,2)", comment="平均飞行时长(分钟)"),
        Column("updated_at", "DATETIME", nullable=False),
    ),
    indexes=(
        Index("uk_dws_daily_grain", ("stat_date", "airline_id", "dep_airport_code"), unique=True),
        Index("idx_dws_daily_date", ("stat_date",)),
    ),
)

DWS_ROUTE_DAILY = Table(
    name="dws_route_daily",
    comment="DWS 航线日汇总：日期 × 航线 的航班量与平均延误",
    columns=(
        Column("id", "INT", nullable=False, primary_key=True, autoincrement=True),
        Column("stat_date", "DATE", nullable=False),
        Column("route_id", "VARCHAR(32)", nullable=False),
        Column("route_name", "VARCHAR(160)"),
        Column("dep_airport_code", "VARCHAR(8)"),
        Column("arr_airport_code", "VARCHAR(8)"),
        Column("flight_cnt", "INT", default="0"),
        Column("delay_cnt", "INT", default="0", comment="存在延误的班次"),
        Column("on_time_rate", "DECIMAL(6,2)", comment="准点率(%)"),
        Column("avg_dep_delay_min", "DECIMAL(10,2)"),
        Column("max_dep_delay_min", "INT"),
        Column("avg_arr_delay_min", "DECIMAL(10,2)"),
        Column("avg_flight_duration_min", "DECIMAL(10,2)"),
        Column("updated_at", "DATETIME", nullable=False),
    ),
    indexes=(
        Index("uk_dws_route_grain", ("stat_date", "route_id"), unique=True),
    ),
)

# ---------------------------------------------------------------------------
# 质量与元数据
# ---------------------------------------------------------------------------
DQ_FLIGHT_ANOMALY = Table(
    name="dq_flight_anomaly",
    comment="数据质量异常记录表：每条命中规则的脏数据都留痕",
    columns=(
        Column("anomaly_id", "INT", nullable=False, primary_key=True, autoincrement=True),
        Column("batch_id", "VARCHAR(32)", nullable=False),
        Column("rule_code", "VARCHAR(32)", nullable=False, comment="规则编码"),
        Column("rule_name", "VARCHAR(64)", nullable=False, comment="规则名称"),
        Column("severity", "VARCHAR(8)", nullable=False, comment="WARN/ERROR"),
        Column("stat_date", "DATE"),
        Column("flight_id", "VARCHAR(40)", comment="关联的清洗后航班（可能为空）"),
        Column("flight_no", "VARCHAR(32)", comment="航班号"),
        Column("field_name", "VARCHAR(64)", comment="问题字段"),
        Column("raw_value", "VARCHAR(255)", comment="原始值"),
        Column("normalized_value", "VARCHAR(255)", comment="标准化后的值"),
        Column("message", "VARCHAR(512)", comment="问题描述"),
        Column("created_at", "DATETIME", nullable=False),
    ),
    indexes=(
        Index(
            "uk_anomaly",
            ("batch_id", "rule_code", "flight_id", "field_name", "raw_value"),
            unique=True,
        ),
        Index("idx_anomaly_rule", ("rule_code",)),
    ),
)

ETL_BATCH_LOG = Table(
    name="etl_batch_log",
    comment="批次日志：记录每次 ETL 的来源、行数、耗时与异常统计",
    columns=(
        Column("batch_id", "VARCHAR(32)", nullable=False, primary_key=True),
        Column("source_name", "VARCHAR(32)", nullable=False),
        Column("status", "VARCHAR(16)", nullable=False, comment="RUNNING/SUCCESS/FAILED"),
        Column("started_at", "DATETIME", nullable=False),
        Column("finished_at", "DATETIME"),
        Column("duration_sec", "INT"),
        Column("source_row_cnt", "INT", default="0", comment="源端记录数"),
        Column("ods_new_cnt", "INT", default="0", comment="ODS 新增行数"),
        Column("ods_skip_cnt", "INT", default="0", comment="ODS 幂等跳过行数"),
        Column("dwd_insert_cnt", "INT", default="0", comment="DWD 新增行数"),
        Column("dwd_update_cnt", "INT", default="0", comment="DWD 更新行数"),
        Column("dwd_skip_cnt", "INT", default="0", comment="DWD 内部去重丢弃行数"),
        Column("anomaly_cnt", "INT", default="0", comment="质量异常条数"),
        Column("error_msg", "VARCHAR(1024)", comment="失败原因"),
        Column("source_fingerprint", "VARCHAR(64)", comment="源指纹"),
    ),
    indexes=(Index("idx_batch_source", ("source_name",)),),
)

ETL_WATERMARK = Table(
    name="etl_watermark",
    comment="增量水位：记录每个采集源已处理到的日期，用于增量同步",
    columns=(
        Column("source_name", "VARCHAR(64)", nullable=False, primary_key=True),
        Column("last_batch_id", "VARCHAR(32)"),
        Column("last_flight_date", "DATE", comment="已处理的最大航班日期"),
        Column("last_run_at", "DATETIME"),
        Column("total_rows", "INT", default="0", comment="累计处理行数"),
    ),
)


ALL_TABLES: tuple[Table, ...] = (
    DIM_AIRPORT,
    DIM_AIRPORT_ALIAS,
    DIM_AIRLINE,
    DIM_ROUTE,
    ODS_FLIGHT,
    DWD_FLIGHT,
    DWS_FLIGHT_DAILY,
    DWS_ROUTE_DAILY,
    DQ_FLIGHT_ANOMALY,
    ETL_BATCH_LOG,
    ETL_WATERMARK,
)


# ---------------------------------------------------------------------------
# DDL 渲染
# ---------------------------------------------------------------------------
def render_column(column: Column, dialect: str) -> str:
    if column.autoincrement:
        if dialect == "mysql":
            return f"{column.name} INT NOT NULL AUTO_INCREMENT PRIMARY KEY"
        return f"{column.name} INTEGER PRIMARY KEY AUTOINCREMENT"

    parts = [column.name, column.type]
    if column.primary_key:
        parts.append("PRIMARY KEY")
    if not column.nullable:
        parts.append("NOT NULL")
    if column.default is not None:
        parts.append(f"DEFAULT {column.default}")
    return " ".join(parts)


def render_create_table(table: Table, dialect: str) -> str:
    body = ",\n  ".join(render_column(c, dialect) for c in table.columns)
    sql = f"CREATE TABLE IF NOT EXISTS {table.name} (\n  {body}\n)"
    if dialect == "mysql":
        sql += MYSQL_TABLE_OPTIONS
    return sql


def render_create_index(table: Table, index: Index, dialect: str) -> str:
    unique = "UNIQUE " if index.unique else ""
    cols = ", ".join(index.columns)
    return f"CREATE {unique}INDEX {index.name} ON {table.name} ({cols})"


def existing_indexes(db: Database, table: str) -> set[str]:
    if db.dialect == "sqlite":
        rows = db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
        )
    else:
        rows = db.fetch_all(
            "SELECT DISTINCT index_name AS name FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = ?",
            (table,),
        )
    return {r["name"] for r in rows}


def create_all(db: Database, tables: Sequence[Table] = ALL_TABLES) -> list[str]:
    """建表 + 建索引，返回实际执行的语句列表。"""
    executed: list[str] = []
    for table in tables:
        ddl = render_create_table(table, db.dialect)
        db.execute(ddl)
        executed.append(ddl)
        present = existing_indexes(db, table.name)
        for index in table.indexes:
            if index.name in present:
                continue
            sql = render_create_index(table, index, db.dialect)
            try:
                db.execute(sql)
                executed.append(sql)
            except Exception as exc:  # noqa: BLE001 - 索引已存在等场景可忽略
                if "exist" not in str(exc).lower():
                    raise
    return executed


def drop_all(db: Database, tables: Sequence[Table] = ALL_TABLES) -> None:
    for table in reversed(tables):
        db.execute(f"DROP TABLE IF EXISTS {table.name}")
