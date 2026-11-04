"""采集器数据库连通性检查。

    python scripts/_probe_db.py <settings/db_setting.json> [db_config|db_config_test]

用于确认「本机能不能连上采集器写入的那个 MySQL」，是接真实数据前的第一步。
只做只读探测：不写库、不打印口令。
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

try:
    import pymysql
except ImportError:  # pragma: no cover
    pymysql = None  # type: ignore[assignment]


def load_settings(settings_file: Path, key: str) -> dict:
    raw = json.loads(settings_file.read_text(encoding="utf-8-sig"))
    if isinstance(raw.get(key), dict):
        raw = raw[key]
    if "host" not in raw:
        raise ValueError(f"配置里没有 host 字段: {settings_file}")
    return raw


def probe(config: dict) -> int:
    host = config["host"]
    port = int(config.get("port", 3306))
    print(f"目标: {host}:{port}  库: {config.get('database')}  用户: {config.get('user')}")

    try:
        socket.create_connection((host, port), timeout=8).close()
        print("[1/3] TCP 连接: OK")
    except OSError as exc:
        print(f"[1/3] TCP 连接: 失败 ({type(exc).__name__}: {exc})")
        return 1

    if pymysql is None:
        print("[2/3] MySQL 认证: 跳过（未安装 PyMySQL）")
        return 2

    try:
        conn = pymysql.connect(
            host=host,
            port=port,
            user=config["user"],
            password=config.get("password", ""),
            database=config.get("database"),
            charset=config.get("charset", "utf8mb4"),
            connect_timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[2/3] MySQL 认证: 失败 ({type(exc).__name__}: {exc})")
        return 1

    print("[2/3] MySQL 认证: OK")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            print(f"[3/3] 版本: {cur.fetchone()[0]}")
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name LIKE %s",
                ("%flight%",),
            )
            tables = [row[0] for row in cur.fetchall()]
            if not tables:
                print("[3/3] 航班表: 未找到")
            for table in tables:
                cur.execute(f"SELECT COUNT(*) FROM `{table}`")
                print(f"[3/3] 表 {table}: {cur.fetchone()[0]} 行")
    finally:
        conn.close()
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 64
    settings_file = Path(argv[1])
    key = argv[2] if len(argv) > 2 else "db_config"
    if not settings_file.exists():
        print(f"配置文件不存在: {settings_file}")
        return 2
    return probe(load_settings(settings_file, key))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
