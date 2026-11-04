"""数据库访问层。

对外暴露一套与方言无关的接口，内部同时支持：

* ``sqlite`` —— 本地开发 / 单元测试默认后端，零依赖零配置
* ``mysql``  —— 生产后端，与采集器 ``asean_flights.py`` 写入的实例保持一致

SQL 一律用 ``?`` 占位符书写，MySQL 方言在发送前统一翻译成 ``%s``。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from ..config import DbConfig

try:  # PyMySQL 为可选依赖，sqlite 后端不需要
    import pymysql
except ImportError:  # pragma: no cover - 取决于运行环境
    pymysql = None  # type: ignore[assignment]


class DatabaseError(RuntimeError):
    """数据库访问异常。"""


class Database:
    """轻量数据库封装，屏蔽 SQLite / MySQL 的语法差异。"""

    def __init__(self, config: DbConfig):
        self.config = config
        self.dialect = (config.dialect or "sqlite").lower()
        if self.dialect not in {"sqlite", "mysql"}:
            raise DatabaseError(f"不支持的数据库方言: {self.dialect}")
        self._conn: Any = None

    # -- 连接管理 ---------------------------------------------------------
    def connect(self) -> "Database":
        if self._conn is not None:
            return self
        if self.dialect == "sqlite":
            path = Path(self.config.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path))
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
        else:
            if pymysql is None:
                raise DatabaseError("未安装 PyMySQL，无法使用 MySQL 后端")
            self._conn = pymysql.connect(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                database=self.config.database,
                charset=self.config.charset,
                connect_timeout=10,
                read_timeout=60,
                write_timeout=60,
                autocommit=False,
            )
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Database":
        return self.connect()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def conn(self) -> Any:
        if self._conn is None:
            raise DatabaseError("数据库尚未连接，请先调用 connect()")
        return self._conn

    # -- SQL 翻译 ---------------------------------------------------------
    def _translate(self, sql: str, has_params: bool) -> str:
        """把 ``?`` 占位符翻译成目标方言。"""
        if self.dialect != "mysql":
            return sql
        if has_params:
            # 先转义字面量 %，再替换占位符，避免参数化 SQL 中的 % 被误解析
            sql = sql.replace("%", "%%")
        return sql.replace("?", "%s")

    # -- 执行 -------------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        cur = self.conn.cursor()
        try:
            cur.execute(self._translate(sql, params is not None), tuple(params or ()))
            self.conn.commit()
            return cur.rowcount
        except Exception as exc:  # noqa: BLE001 - 统一包装成领域异常
            self.conn.rollback()
            raise DatabaseError(f"SQL 执行失败: {exc}\nSQL: {sql[:500]}") from exc
        finally:
            cur.close()

    def execute_many(self, sql: str, rows: Sequence[Sequence[Any]]) -> int:
        if not rows:
            return 0
        cur = self.conn.cursor()
        try:
            cur.executemany(self._translate(sql, True), [tuple(r) for r in rows])
            self.conn.commit()
            return cur.rowcount
        except Exception as exc:  # noqa: BLE001
            self.conn.rollback()
            raise DatabaseError(f"批量执行失败: {exc}\nSQL: {sql[:500]}") from exc
        finally:
            cur.close()

    def fetch_all(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        try:
            cur.execute(self._translate(sql, params is not None), tuple(params or ()))
            return [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            raise DatabaseError(f"查询失败: {exc}\nSQL: {sql[:500]}") from exc
        finally:
            cur.close()

    def fetch_one(self, sql: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
        rows = self.fetch_all(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        row = self.fetch_one(sql, params)
        if not row:
            return None
        return next(iter(row.values()))

    def count(self, table: str, where: str = "", params: Sequence[Any] | None = None) -> int:
        clause = f" WHERE {where}" if where else ""
        return int(self.scalar(f"SELECT COUNT(*) AS c FROM {table}{clause}", params) or 0)

    def table_exists(self, table: str) -> bool:
        if self.dialect == "sqlite":
            row = self.fetch_one(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            )
        else:
            row = self.fetch_one(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = ?",
                (table,),
            )
        return row is not None

    # -- 写入辅助 ---------------------------------------------------------
    def upsert(
        self,
        table: str,
        columns: Sequence[str],
        rows: Sequence[Sequence[Any]],
        key_columns: Sequence[str],
        update_columns: Sequence[str] | None = None,
    ) -> int:
        """按主键 upsert，重复键更新非主键列。"""
        if not rows:
            return 0
        cols = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        updates = [c for c in (update_columns or columns) if c not in key_columns]
        if self.dialect == "sqlite":
            conflict = ", ".join(key_columns)
            set_clause = ", ".join(f"{c}=excluded.{c}" for c in updates) or f"{key_columns[0]}=excluded.{key_columns[0]}"
            sql = (
                f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
                f"ON CONFLICT({conflict}) DO UPDATE SET {set_clause}"
            )
        else:
            set_clause = ", ".join(f"{c}=VALUES({c})" for c in updates) or f"{key_columns[0]}=VALUES({key_columns[0]})"
            sql = (
                f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
                f"ON DUPLICATE KEY UPDATE {set_clause}"
            )
        return self.execute_many(sql, rows)

    def insert_ignore(
        self,
        table: str,
        columns: Sequence[str],
        rows: Sequence[Sequence[Any]],
        key_columns: Sequence[str],
    ) -> int:
        """仅在主键不存在时插入（用于 ODS 去重落地）。"""
        if not rows:
            return 0
        cols = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        if self.dialect == "sqlite":
            conflict = ", ".join(key_columns)
            sql = (
                f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
                f"ON CONFLICT({conflict}) DO NOTHING"
            )
        else:
            sql = f"INSERT IGNORE INTO {table} ({cols}) VALUES ({placeholders})"
        return self.execute_many(sql, rows)

    # -- 事务 -------------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator["Database"]:
        try:
            yield self
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise


def connect(config: DbConfig) -> Database:
    """便捷入口：连接并返回数据库句柄。"""
    return Database(config).connect()


def rows_to_dicts(rows: Iterable[Sequence[Any]], columns: Sequence[str]) -> list[dict[str, Any]]:
    return [dict(zip(columns, row)) for row in rows]
