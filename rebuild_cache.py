"""命令行入口：沿用 SQLite 旧库，从现行报价全量重建环比与指数缓存。

用法：
    DATABASE_PATH=/data/prices.db python3 rebuild_cache.py

重建在单个事务内完成；失败时整体回滚，旧缓存原样保留，退出码非零，可直接重试。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import calc, db  # noqa: E402
from app.services import base_month  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    # create_app 会完成建表/迁移与启动重建；这里只需要它把库结构准备好
    from app import create_app

    app = create_app()
    database = app.config["DATABASE"]
    conn = db.connect(database)
    try:
        if base_month(conn) is None:
            print("库里还没有任何报价，无需重建", file=sys.stderr)
            return 2
        if not calc.cache_is_stale(conn):
            print("缓存已是最新结构，仍按要求全量重建……")
        result = calc.rebuild_cache(conn)
    except calc.RebuildError as exc:
        print(f"重建跳过：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"重建失败，已回滚，原缓存保留，可重试：{exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(
        f"重建完成：基月 {result['base_month']}，{len(result['months'])} 个月，"
        f"缓存 {result['row_count']} 行，代际 {result['generation']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
