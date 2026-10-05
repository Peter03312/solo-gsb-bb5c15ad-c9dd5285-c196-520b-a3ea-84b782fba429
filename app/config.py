"""运行配置（全部通过环境变量覆盖，便于容器化）。"""

from __future__ import annotations

import os


def database_url() -> str:
    """SQLite 数据库文件路径。"""
    return os.getenv("QSO_DB_PATH", "/data/qso.db")


def http_host() -> str:
    return os.getenv("QSO_HOST", "0.0.0.0")


def http_port() -> int:
    return int(os.getenv("QSO_PORT", "8080"))
