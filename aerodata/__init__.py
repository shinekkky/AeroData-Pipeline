"""AeroData Pipeline — 航空数据治理与数仓分层 ETL 流水线。

分层约定::

    采集源(Collector) -> ODS(原始层) -> DWD(清洗明细层) -> DWS(主题汇总层)
                                              |
                                              +-> 数据质量规则 -> dq_flight_anomaly / 质量报告
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
