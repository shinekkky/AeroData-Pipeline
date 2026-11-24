# AeroData Pipeline — 航空数据治理与数仓分层 ETL

> 把多源航班数据接进来，清洗成规范明细，沉淀成能直接分析的数据资产，
> 并且对数据质量问题**可发现、可解释、可追溯**。

数据来源是中科闻歌实习期间的采集器 `asean_flights.py`（东南亚—中国 269 条航线的
Playwright 采集脚本，写入 `asean_flights_table`）。
本项目**不重复造采集轮子**，而是站在采集器下游，把原始数据加工成可用的数据资产。

> 英文版**项目陈述（申请用文书）**：[`docs/PROJECT_STATEMENT.md`](docs/PROJECT_STATEMENT.md)
> —— 含研究问题、方法、评估结果、局限与边界，可直接用于 DS/CS 方向的项目陈述或简历附件。

---

## 一、这个项目解决什么问题

采集器给出的原始数据长这样：

| 问题 | 例子 |
|---|---|
| 机场代码不统一 | 同一个机场叫「沙巴」「斗湖」；「上海浦东」和「上海浦东国际机场」 |
| 航班号格式乱 | `SQ 830` / `sq-830` / `SQ.830` |
| 时间格式错 | `8:5`、`25:00`、`--`、`23:50+1` |
| 字段缺失 | 航班号为空、航空公司为空、实际到达时间为空 |
| 数据重复 | 同一班航班被重复采集；同一班航班状态从「已起飞」推进到「已到达」 |
| 源数据本身有错 | 航线清单把「斗湖」的 IATA 码写成了 `TWU`（那其实是塔瓦乌的码） |

这些数据直接拿去算延误率，指标就是错的。本项目把它们变成：

- 字段标准化、维表关联、延误分钟可直接计算的 **DWD 明细**
- 按日期 × 航司 × 机场 / 日期 × 航线聚合的 **DWS 汇总**
- 每条脏数据都有规则编码、原始值、标准化值和处理建议的 **质量异常记录**

---

## 二、整体架构

```
   多源接入                ODS 原始层            DWD 清洗明细层        DWS 主题汇总层
  ┌──────────┐          ┌─────────────┐      ┌──────────────┐      ┌────────────────┐
  │ MySQL    │          │ ods_flight  │      │ dwd_flight   │      │ dws_flight_daily│
  │ asean_   │  落地    │  原样落库   │ 清洗 │ 标准化+维表   │ 聚合 │ dws_route_daily │
  │ flights_ │ ───────► │  +行哈希幂等 │ ───► │  +延误计算    │ ───► │  航班量/准点率  │
  │ table    │          └─────────────┘      └──────────────┘      └────────────────┘
  ├──────────┤                 │                     │                      │
  │ CSV/JSON │                 ▼                     ▼                      ▼
  ├──────────┤          ┌─────────────┐      ┌──────────────┐      ┌────────────────┐
  │ 仿真源   │          │ dim_airport │      │ 数据质量规则 │      │   质量报告      │
  └──────────┘          │ dim_airline │      │ 14 条规则    │      │ JSON + Markdown │
                        │ dim_route   │      └──────────────┘      └────────────────┘
                        │ dim_airport_alias        │
                        └───────────────────────────┴──► dq_flight_anomaly
```

**关键设计：流水线不关心数据从哪来。** 采集源实现同一个接口
（`aerodata/sources/base.py`），所以：

| 采集源 | 场景 | 状态 |
|---|---|---|
| `mysql` | 直连采集器写入的 `asean_flights_table` | 代码就绪，需在有权限的机器上跑 |
| `csv` | 采集器导出的 CSV / JSON 明细 | 代码就绪，附样例文件 |
| `simulator` | 基于**真实 269 条航线**的确定性仿真 | 默认，离线可跑可复现 |

---

## 三、数据模型

### 维度表

| 表 | 作用 | 关键点 |
|---|---|---|
| `dim_airport` | 机场主表，一个机场一个 IATA 编码 | 同名不同码的机场按编码归并 |
| `dim_airport_alias` | 机场别名表，别名 → 标准编码 | 「斗湖」「曼谷素万那普」等写法在这里收口 |
| `dim_airline` | 航空公司维度，54 家承运人 | 采集源给的是中文名，这里映射到 IATA 编码 |
| `dim_route` | 航线维度，268 条 | 归并后重复航线自动折叠 |

维度不靠 SQL 常量维护，而是从可版本化的参考文件装载：

- `data/reference/routes_daily.json` —— 采集器同款航线清单
- `data/reference/airlines.csv` —— 航空公司标准编码表
- `data/reference/airport_overrides.csv` —— **人工校��表**，修正源数据里的错误编码
- `data/reference/airport_aliases.csv` —— 人工维护的别���
- `data/reference/countries.csv` —— 国家/地区 ISO-3166 映射

### 事实表

| 表 | 粒度 | 说明 |
|---|---|---|
| `ods_flight` | 一条采集原始记录 | 只落地不加工，`row_hash` 唯一索引保证幂等 |
| `dwd_flight` | 一班航班 | 标准化 + 维表关联 + 延误计算，`flight_id` 为业务主键 |
| `dws_flight_daily` | 日期 × 航司 × 出发机场 | 航班量、准点率、延误均值/极值 |
| `dws_route_daily` | 日期 × 航线 | 航线级平均延误 |

### 元数据与质量表

| 表 | 作用 |
|---|---|
| `dq_flight_anomaly` | 数据质量异常记录，逐条留痕 |
| `etl_batch_log` | 批次日志：来源、行数、耗时、异常统计 |
| `etl_watermark` | 增量水位：每个源处理到哪个日期 |

---

## 四、数据质量规则

15 条规则，分 `row`（单条内部一致性）和 `batch`（批次级重复）两个作用域。
每条异常记录**规则编码、严重度、字段名、原始值、标准化值、处理说明**。

| 规则 | 严重度 | 触发条件 |
|---|---|---|
| `MISSING_REQUIRED_FIELD` | ERROR | 航班号 / 航空公司 / 航班日期为空 |
| `INVALID_FLIGHT_NO` | ERROR | 航班号不符合 `<航司前缀><1-4位数字>` |
| `INVALID_TIME_FORMAT` | WARN | 时间能解析但写法不规范（`8:5`），已尽力修正 |
| `INVALID_TIME_VALUE` | ERROR | 时间无法解析为合法时刻（`25:00`） |
| `MISSING_TIME` | WARN | **计划**时间缺失，影响延误指标（实际时间缺失不算，取消航班本就没有） |
| `UNKNOWN_AIRPORT` | ERROR | 机场未匹配到维表 |
| `UNKNOWN_AIRLINE` | WARN | 航司名未匹配到维表 |
| `UNKNOWN_AIRLINE_PREFIX` | WARN | 航班号前缀不在维表中，疑似不存在的承运人 |
| `MISSING_ACTUAL_TIME` | WARN | 状态为已起飞/已到达但缺实际时间 |
| `STATUS_CONTRADICTION` | WARN | 取消的航班却有实际起降时间 |
| `INVALID_FLIGHT_DURATION` | ERROR | 飞行时长超出合理区间 |
| `ARRIVAL_BEFORE_DEPARTURE` | ERROR | 实际到达早于实际起飞 |
| `DELAY_OUT_OF_RANGE` | WARN | 延误绝对值超过 24 小时 |
| `COUNTRY_MISMATCH` | WARN | 记录国家与维表推导国家不一致 |
| `DUPLICATE_FLIGHT` | WARN | 同一批次航班重复 |

> 设计原则：**清洗负责「把数据改对」，质量层负责「把改不好的记下来」。**
> 两条职责分开，才能既保证下游可用，又保证问题不丢。
>
> 另外，**格式规则保持通用，参照完整性交给维表**。航班号格式只管
> 「前缀 + 数字」的结构，`K6`（柬埔寨吴哥）、`3U`（四川航空）这类
> 含数字的真实航司代码都放行；至于这个前缀是不是真的存在承运人，
> 由 `UNKNOWN_AIRLINE_PREFIX` 拿维表来判。两层规则各司其职。

---

## 五、快速开始

```bash
pip install -r requirements.txt

# 1. 建表 + 装载维度（本地 SQLite，零配置）
python -m aerodata.cli --config config/local.json init-db

# 2. 跑一次完整 ETL（含质量检查 + 生成报告）
python -m aerodata.cli --config config/local.json run --source simulator

# 3. 看各表行数与最近批次
python -m aerodata.cli --config config/local.json stats

# 4. 查数
python -m aerodata.cli --config config/local.json query \
  "SELECT airline_name, COUNT(*) FROM dwd_flight GROUP BY airline_name ORDER BY 2 DESC LIMIT 10"

# 5. 跑测试
python -m pytest -q
```

生成的质量报告在 `reports/`（JSON 给程序用，Markdown 给人看）。

### 换成真实采集源

```bash
# 第 0 步：先确认本机能连上采集器的库（只读探测，不写库、不打印口令）
python scripts/_probe_db.py D:/Desktop/zkwg/jky-collection-program/settings/db_setting.json db_config

# 第 1 步：直连采集器的 MySQL
python -m aerodata.cli run --source mysql \
  --settings-file D:/Desktop/zkwg/jky-collection-program/settings/db_setting.json \
  --table asean_flights_table

# 或者：读取采集器导出的 CSV / JSON
python scripts/export_sample.py --days 3          # 造一份样例
python -m aerodata.cli run --source csv --path data/sample
```

MySQL 后端与 SQLite 后端共用同一份表结构定义（`aerodata/db/schema.py`），
切换只改配置：

```bash
export AERODATA_DB_DIALECT=mysql
export AERODATA_DB_HOST=...  AERODATA_DB_PORT=...  AERODATA_DB_USER=...
export AERODATA_DB_PASSWORD=...  AERODATA_DB_NAME=...
python -m aerodata.cli init-db
python -m aerodata.cli run --source mysql --settings-file <db_setting.json>
```

---

## 六、幂等与增量

**幂等**：同一批数据跑两遍，ODS / DWD / DWS 的行数与指标完全一致。

- `ods_flight.row_hash` 唯一索引 → 完全相同的记录重复采集直接跳过
- `dwd_flight.flight_id` 业务主键 → 同一班航班状态推进走 UPDATE，不产生重复行
- `dws_*` **不按批次算，而是按日期从 DWD 全量重算** → 重复跑结果不变，
  增量批次也不会把历史指标算错

实测（本地 SQLite，14 天 × 269 条航线）：

| | 第一次 | 第二次 |
|---|---|---|
| ODS 新增 / 跳过 | 7536 / 90 | 0 / 7626 |
| DWD 新增 / 更新 | 7511 / 0 | 0 / 7511 |
| DWS 日汇总 / 航线汇总 | 1307 / 3745 | 1307 / 3745 |

**增量**：`etl_watermark` 记录每个源处理到的航班日期，
`run --incremental` 只取比水位更新的数据。

---

## 七、目录结构

```
AeroData Pipeline/
├── aerodata/
│   ├── config.py            配置加载（文件 + 环境变量覆盖）
│   ├── cli.py               命令行入口
│   ├── dimensions.py        维度装载（航线清单 + 参考表 → 维表）
│   ├── db/
│   │   ├── engine.py        数据库封装（SQLite / MySQL 一套接口）
│   │   └── schema.py        全部表结构定义与 DDL 渲染
│   ├── sources/             采集源：base / simulator / csv_files / mysql_collector
│   ├── etl/
│   │   ├── standards.py     字段标准化（航班号/机场/航司/时间/状态）
│   │   ├── cleaning.py      ODS → DWD 清洗加工
│   │   └── pipeline.py      流水线编排（批次日志、水位、幂等）
│   ├── layers/
│   │   ├── ods.py           ODS 落地
│   │   └── dws.py           DWS 汇总（按日期重算）
│   └── quality/
│       ├── rules.py         14 条质量规则
│       └── report.py        质量报告（JSON + Markdown）
├── data/reference/          可版本化的参考数据
├── scripts/export_sample.py 导出 CSV / JSON 样例
├── tests/                   pytest 用例
├── config/                  配置（local.json = 本地 SQLite 离线）
└── reports/                 质量报告输出
```

---

## 八、测试

```bash
python -m pytest -q
```

覆盖：航班号/时间/状态标准化、机场别名归并、缺失值、非法字段、重复数据、
二次观测更新语义、ODS 幂等、DWD 去重、DWS 汇总幂等、增量水位、失败批次留痕。

---

## 九、边界与后续

**当前边界（诚实说明）**

- 默认使用仿真源。仿真源基于**真实 269 条航线清单**，但航班明细是按分布规则合成的，
  不是真实航班；接真实数据只需切换 `--source mysql` / `--source csv`，
  清洗、质量、汇总逻辑一行都不用改。
- MySQL 后端代码完整，但本机到采集器数据库的网络不通（`Access denied`），
  尚未在真实实例上跑通；SQLite 后端已全量验证。
- 采集器侧网络出口在本机被拦截，`asean_flights.py` 实测报
  `net::ERR_CONNECTION_CLOSED`，因此本次没有拿到线上真实航班明细。

**后续可做**

- 增量改造成基于 `update_time` 的 CDC 订阅，替代按日期的水位
- 质量规则接入告警（异常超阈值时推送）
- DWD 加 `PARTITION BY flight_date` 的分区表，支撑更大量级
- 汇总层接 BI（Superset / Metabase），把质量报告做成看板
