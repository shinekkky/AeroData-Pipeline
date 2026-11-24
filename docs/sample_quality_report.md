# 航空数据质量报告
生成时间：2026-11-04 19:43:24
数据范围：batch（批次 `simulator_20261104194321`）

## 一、批次概览
| 指标 | 值 |
|---|---|
| 采集源 | simulator |
| 批次号 | simulator_20261104194321 |
| 状态 | SUCCESS |
| 源端记录数 | 7626 |
| ODS 新增 / 幂等跳过 | 0 / 7626 |
| DWD 新增 / 更新 / 去重丢弃 | 0 / 7511 / 115 |
| 质量异常命中数 | 474 |
| 耗时(秒) | 3 |

## 二、数据质量总览
本次共命中 **471** 条质量异常（航班总数 7511 条，异常密度 6.27%）。

**按严重度**
| 严重度 | 命中数 |
|---|---|
| ERROR | 230 |
| WARN | 241 |

**按规则**
| 规则编码 | 规则名称 | 严重度 | 命中数 |
|---|---|---|---|
| DUPLICATE_FLIGHT | 同一批次航班重复 | WARN | 115 |
| MISSING_REQUIRED_FIELD | 必填字段缺失 | ERROR | 74 |
| UNKNOWN_AIRLINE | 航空公司未匹配到维表 | WARN | 68 |
| INVALID_FLIGHT_DURATION | 飞行时长异常 | ERROR | 46 |
| UNKNOWN_AIRPORT | 机场未匹配到维表 | ERROR | 46 |
| ARRIVAL_BEFORE_DEPARTURE | 到达早于起飞 | ERROR | 43 |
| INVALID_TIME_FORMAT | 时间格式不规范 | WARN | 36 |
| INVALID_TIME_VALUE | 时间值非法 | ERROR | 21 |
| MISSING_ACTUAL_TIME | 缺少实际时间 | WARN | 14 |
| DELAY_OUT_OF_RANGE | 延误超出合理范围 | WARN | 8 |

## 三、异常明细采样（每条规则最多 3 条）
| 严重度 | 规则 | 字段 | 原始值 | 标准化值 | 说明 |
|---|---|---|---|---|---|
| ERROR | ARRIVAL_BEFORE_DEPARTURE | actual_arr_time | 2026-10-22 07:14:00 |  | 实际到达 2026-10-22 07:14:00 早于实际起飞 2026-10-22 07:44:00 |
| ERROR | ARRIVAL_BEFORE_DEPARTURE | actual_arr_time | 2026-10-22 17:43:00 |  | 实际到达 2026-10-22 17:43:00 早于实际起飞 2026-10-22 18:13:00 |
| ERROR | ARRIVAL_BEFORE_DEPARTURE | actual_arr_time | 2026-10-22 15:16:00 |  | 实际到达 2026-10-22 15:16:00 早于实际起飞 2026-10-22 15:46:00 |
| ERROR | INVALID_FLIGHT_DURATION | flight_duration_min | 1350 | 1350 | 飞行时长 1350 分钟超出合理区间 |
| ERROR | INVALID_FLIGHT_DURATION | flight_duration_min | 1350 | 1350 | 飞行时长 1350 分钟超出合理区间 |
| ERROR | INVALID_FLIGHT_DURATION | flight_duration_min | 1350 | 1350 | 飞行时长 1350 分钟超出合理区间 |
| ERROR | INVALID_TIME_VALUE | scheduled_dep_time | 25:00 |  | 计划起飞时间取值非法：'25:00' |
| ERROR | INVALID_TIME_VALUE | scheduled_dep_time | 25:00 |  | 计划起飞时间取值非法：'25:00' |
| ERROR | INVALID_TIME_VALUE | actual_dep_time | 24:30 |  | 实际起飞时间取值非法：'24:30' |
| ERROR | MISSING_REQUIRED_FIELD | airline_name |  |  | 航空公司为空 |
| ERROR | MISSING_REQUIRED_FIELD | airline_name |  |  | 航空公司为空 |
| ERROR | MISSING_REQUIRED_FIELD | flight_no |  |  | 航班号为空 |
| ERROR | UNKNOWN_AIRPORT | dep_airport_code | 某某国际机场 |  | 出发机场 '某某国际机场' 未匹配到 dim_airport |
| ERROR | UNKNOWN_AIRPORT | dep_airport_code | 某某国际机场 |  | 出发机场 '某某国际机场' 未匹配到 dim_airport |
| ERROR | UNKNOWN_AIRPORT | dep_airport_code | 某某国际机场 |  | 出发机场 '某某国际机场' 未匹配到 dim_airport |
| WARN | DELAY_OUT_OF_RANGE | arr_delay_min | 1446 |  | 到达延误 1446 分钟超出 24 小时范围 |
| WARN | DELAY_OUT_OF_RANGE | arr_delay_min | 1442 |  | 到达延误 1442 分钟超出 24 小时范围 |
| WARN | DELAY_OUT_OF_RANGE | arr_delay_min | 1481 |  | 到达延误 1481 分钟超出 24 小时范围 |
| WARN | DUPLICATE_FLIGHT | flight_id | 6d830f713dec7058b87a66a0ea0623dc | 2 | 航班 PR1320 在本批次出现 2 次 |
| WARN | DUPLICATE_FLIGHT | flight_id | f4cdedf27a7ea82316f9b1a47cc209c6 | 2 | 航班 FD9601 在本批次出现 2 次 |
| WARN | DUPLICATE_FLIGHT | flight_id | 4f54c56dee45e9bc202e0ab8121546c3 | 2 | 航班 TR9869 在本批次出现 2 次 |
| WARN | INVALID_TIME_FORMAT | scheduled_dep_time | abc |  | 计划起飞时间写法不规范：'abc' |
| WARN | INVALID_TIME_FORMAT | actual_dep_time | 8:5 | 2026-10-22 08:05:00 | 实际起飞时间写法不规范：'8:5' |
| WARN | INVALID_TIME_FORMAT | actual_dep_time | 8:5 | 2026-10-23 08:05:00 | 实际起飞时间写法不规范：'8:5' |
| WARN | MISSING_ACTUAL_TIME | actual_dep_time |  |  | 状态为已到达但缺少实际起飞时间 |
| WARN | MISSING_ACTUAL_TIME | actual_dep_time |  |  | 状态为已到达但缺少实际起飞时间 |
| WARN | MISSING_ACTUAL_TIME | actual_dep_time |  |  | 状态为已到达但缺少实际起飞时间 |
| WARN | UNKNOWN_AIRLINE | airline_id |  |  | 航空公司 '' 未匹配到 dim_airline |
| WARN | UNKNOWN_AIRLINE | airline_id |  |  | 航空公司 '' 未匹配到 dim_airline |
| WARN | UNKNOWN_AIRLINE | airline_id | 无名航空 |  | 航空公司 '无名航空' 未匹配到 dim_airline |

## 四、航班统计指标
| 指标 | 值 |
|---|---|
| 航班总量 | 7511 |
| 统计天数 | 14 |
| 涉及航空公司数 | 33 |
| 涉及航线条数 | 269 |
| 涉及机场数 | 42 |
| 航班延误率(%) | 22.86 |
| 航班准点率(%) | 60.76 |
| 取消班次 | 381 |
| 平均起飞延误(分钟) | 5.22 |
| 最大起飞延误(分钟) | 1424 |
| 平均到达延误(分钟) | 66.81 |
| 最大到达延误(分钟) | 1517 |
| 平均飞行时长(分钟) | 186.37 |

### 各航空公司航班量 TOP
| 航空公司编码 | 航空公司 | 航班量 | 平均起飞延误(分钟) |
|---|---|---|---|
| K6 | 柬埔寨吴哥航空 | 756 | 4.21 |
| VJ | 越捷航空 | 684 | 7.79 |
| VN | 越南航空 | 633 | 2.58 |
| WE | 泰国微笑航空 | 442 | -20.1 |
| SL | 泰狮航 | 416 | 11.64 |
| FD | 泰国亚洲航空 | 414 | 2.6 |
| PG | 曼谷航空 | 409 | 1.21 |
| TG | 泰国国际航空 | 389 | 0.01 |
| D7 | 亚洲航空X | 254 | 9.94 |
| MH | 马来西亚航空 | 243 | 53.5 |

### 各机场起降量 TOP
| 角色 | 机场编码 | 机场 | 航班量 |
|---|---|---|---|
| 出发 | BKK | 素万那普 | 884 |
| 出发 | SIN | 新加坡樟宜 | 720 |
| 出发 | CXR | 芽庄卡姆拉恩 | 639 |
| 出发 | DMK | 廊曼 | 617 |
| 出发 | KUL | 吉隆坡 | 584 |
| 出发 | PNH | 金边 | 480 |
| 出发 | HKT | 普吉 | 349 |
| 出发 | SGN | 胡志明市新山 | 323 |
| 出发 | CGK | 雅加达苏加诺-哈达 | 285 |
| 出发 | HAN | 河内内拜 | 267 |
| 到达 | PVG | 上海浦东 | 708 |
| 到达 | CAN | 广州白云 | 584 |
| 到达 | CTU | 成都双流 | 539 |
| 到达 | XMN | 厦门高崎 | 389 |
| 到达 | SZX | 深圳宝安 | 387 |
| 到达 | NNG | 南宁吴圩 | 350 |
| 到达 | PEK | 北京首都 | 330 |
| 到达 | HGH | 杭州萧山 | 250 |
| 到达 | WUH | 武汉天河 | 244 |
| 到达 | HAK | 海口美兰 | 239 |

### 航线平均延误 TOP（最延误）
| 航线 | 出发 | 到达 | 航班量 | 平均起飞延误(分钟) | 最大起飞延误(分钟) |
|---|---|---|---|---|---|
| 吉隆坡-青岛流亭 | KUL | TAO | 22 | 112.91 | 907 |
| 素万那普-南宁吴圩 | BKK | NNG | 23 | 88.17 | 1052 |
| 芽庄卡姆拉恩-泉州晋江 | CXR | JJN | 24 | 85.17 | 558 |
| 美娜多-广州白云 | MDC | CAN | 23 | 80.61 | 822 |
| 槟城-广州白云 | PEN | CAN | 25 | 79.48 | 443 |
| 万象瓦岱-广州白云 | VTE | CAN | 21 | 72.57 | 585 |
| 廊曼-成都双流 | DMK | CTU | 25 | 71.68 | 554 |
| 金边-三亚凤凰 | PNH | SYX | 22 | 68.95 | 648 |
| 芽庄卡姆拉恩-南宁吴圩 | CXR | NNG | 24 | 67.29 | 1031 |
| 东京成田-厦门高崎 | NRT | XMN | 30 | 67.23 | 1009 |

## 五、各层数据规模
| 数据表 | 行数 |
|---|---|
| dim_airport | 86 |
| dim_airport_alias | 89 |
| dim_airline | 54 |
| dim_route | 268 |
| ods_flight | 7536 |
| dwd_flight | 7511 |
| dws_flight_daily | 1307 |
| dws_route_daily | 3745 |
| dq_flight_anomaly | 942 |
| etl_batch_log | 2 |
| etl_watermark | 1 |
