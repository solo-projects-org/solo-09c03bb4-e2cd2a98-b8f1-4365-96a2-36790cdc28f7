"""鲜菜价格指数：Flask 应用工厂。"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from flask import Flask

from . import calc, db
from .routes import bp

log = logging.getLogger(__name__)


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=False)

    project_root = Path(app.root_path).parent
    default_db = Path(
        os.environ.get("DATABASE_PATH", str(project_root / "data" / "prices.db"))
    )

    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "local-dev-secret-change-me"),
        DATABASE=str(default_db),
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,  # 4 MiB
        DEMO_ENABLED=os.environ.get("DEMO_ENABLED", "1") != "0",
    )
    if test_config:
        app.config.update(test_config)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db.init_db(app.config["DATABASE"])
    _upgrade_cache(app)
    app.register_blueprint(bp)
    return app


def _upgrade_cache(app: Flask) -> None:
    """检测到旧结构缓存时启动即重建；失败则保留旧缓存并提示，可手动重试。"""
    if app.config.get("TESTING"):
        return
    conn = db.connect(app.config["DATABASE"])
    try:
        if not calc.cache_is_stale(conn):
            return
        log.warning("指数缓存为旧结构，开始从现行报价重建……")
        try:
            result = calc.rebuild_cache(conn)
        except calc.RebuildError as exc:
            log.warning("启动重建跳过：%s", exc)
        except Exception:
            log.exception("启动重建失败，保留原缓存；可用网页按钮或 rebuild_cache.py 重试")
        else:
            log.info(
                "缓存重建完成：基月 %s，%s 个月（代际 %s）",
                result["base_month"],
                len(result["months"]),
                result["generation"],
            )
    finally:
        conn.close()
