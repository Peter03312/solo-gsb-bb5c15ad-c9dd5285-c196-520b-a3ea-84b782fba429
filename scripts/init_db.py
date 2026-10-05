"""数据初始化入口：创建 SQLite 数据库、表结构并开启 WAL（幂等）。

容器启动时由 CMD 先执行；应用启动（lifespan）也会再执行一次，
因此本地直接 ``uvicorn app.main:app`` 运行同样可用。
"""

from __future__ import annotations

from app import config, database


def main() -> None:
    print(f"[init] 初始化数据库: {config.database_url()}")
    database.init_db()
    print("[init] 完成：drills / logs / results / locks / snapshots 表已就绪")
    print("[init] 既有演练已从当前版本起留存快照（旧版本不补造）")


if __name__ == "__main__":
    main()
