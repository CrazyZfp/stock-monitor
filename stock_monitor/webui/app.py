"""FastAPI app + lifespan"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..config import ConfigStore
from ..manager import MonitorManager

logger = logging.getLogger(__name__)


def create_app(
    config_path: Optional[Path] = None,
    interval_seconds: int = 30,
) -> FastAPI:
    """工厂函数：可注入配置路径（测试用）"""
    store = ConfigStore(path=config_path)
    manager = MonitorManager(store, interval_seconds=interval_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 启动
        try:
            store.load()  # 触发初始 load
        except Exception as e:
            logger.error(f"加载配置失败: {e}")
        manager.start()
        app.state.manager = manager
        app.state.store = store
        try:
            yield
        finally:
            manager.stop()

    app = FastAPI(
        title="Stock Monitor Admin",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
    )

    # 路由
    from . import api
    api.register_routes(app, manager, store)

    # 静态文件
    static_dir = Path(__file__).parent / "static"
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app
