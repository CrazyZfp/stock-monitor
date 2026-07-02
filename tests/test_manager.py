"""MonitorManager 测试"""
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from stock_monitor.config import ConfigStore, StockConfig
from stock_monitor.manager import MonitorManager


@pytest.fixture
def mgr(tmp_path: Path) -> MonitorManager:
    store = ConfigStore(path=tmp_path / "config.json")
    return MonitorManager(store, interval_seconds=1)


class TestMonitorManagerCRUD:
    def test_upsert_new_stock(self, mgr: MonitorManager):
        """新增股票后应出现在配置列表中"""
        mgr.upsert_stock(StockConfig(code="sz1", name="A", price_high=10.0))
        cfg = mgr.get_config()
        assert len(cfg.stocks) == 1
        assert cfg.stocks[0].code == "sz1"

    def test_upsert_updates_existing(self, mgr: MonitorManager):
        """重复 upsert 同一股票应更新而非新增"""
        mgr.upsert_stock(StockConfig(code="sz1", name="A", price_high=10.0))
        mgr.upsert_stock(StockConfig(code="sz1", name="A2", price_high=20.0))
        cfg = mgr.get_config()
        assert len(cfg.stocks) == 1
        assert cfg.stocks[0].name == "A2"
        assert cfg.stocks[0].price_high == 20.0

    def test_delete_existing(self, mgr: MonitorManager):
        """删除股票后应从配置中移除"""
        mgr.upsert_stock(StockConfig(code="sz1", name="A"))
        mgr.upsert_stock(StockConfig(code="sz2", name="B"))
        assert mgr.delete_stock("sz1") is True
        assert mgr.get_config().find_stock("sz1") is None
        assert mgr.get_config().find_stock("sz2") is not None

    def test_patch_enabled(self, mgr: MonitorManager):
        """禁用股票后配置中的 enabled 应为 False"""
        mgr.upsert_stock(StockConfig(code="sz1", name="A", enabled=True))
        assert mgr.patch_stock_enabled("sz1", False) is True
        assert mgr.get_config().find_stock("sz1").enabled is False

    def test_update_webhook(self, mgr: MonitorManager):
        """更新 webhook 后配置持久化"""
        mgr.update_webhook("https://new")
        assert mgr.get_config().dingding_webhook == "https://new"

    def test_update_templates(self, mgr: MonitorManager):
        """更新通知模板后配置持久化"""
        mgr.update_templates({"price_high": ["TEST {name}"]})
        assert mgr.get_config().disguise_templates["price_high"] == ["TEST {name}"]

    def test_replace_config(self, mgr: MonitorManager):
        """整体替换配置后应完全生效"""
        from stock_monitor.config import Config
        new_cfg = Config(
            dingding_webhook="https://x",
            stocks=[StockConfig(code="x1", name="X")],
        )
        mgr.replace_config(new_cfg)
        assert mgr.get_config().dingding_webhook == "https://x"
        assert len(mgr.get_config().stocks) == 1


class TestMonitorManagerRuntime:
    def test_start_stop(self, mgr: MonitorManager):
        """启动/停止监控器应正确切换运行状态"""
        mgr.update_webhook("https://dummy")
        mgr.start()
        assert mgr._running is True
        time.sleep(0.3)
        mgr.stop()
        assert mgr._running is False

    def test_hot_reload_new_stock_visible_in_monitor(self, mgr: MonitorManager):
        """运行时新增股票应同步到 monitor 实例"""
        mgr.update_webhook("https://dummy")
        mgr.start()
        try:
            mgr.upsert_stock(StockConfig(code="sz_new", name="NEW", price_high=10))
            # monitor 实例应已加载该股票
            assert "sz_new" in mgr.monitor.stocks
        finally:
            mgr.stop()

    def test_hot_reload_delete_removes_from_monitor(self, mgr: MonitorManager):
        """运行时删除股票应从 monitor 实例移除"""
        mgr.update_webhook("https://dummy")
        mgr.upsert_stock(StockConfig(code="sz_d", name="D"))
        mgr.start()
        try:
            assert "sz_d" in mgr.monitor.stocks
            mgr.delete_stock("sz_d")
            assert "sz_d" not in mgr.monitor.stocks
        finally:
            mgr.stop()

    def test_disabled_stock_not_in_monitor(self, mgr: MonitorManager):
        """禁用的股票不应出现在 monitor 运行时中"""
        mgr.update_webhook("https://dummy")
        mgr.upsert_stock(StockConfig(code="sz_x", name="X", enabled=False))
        mgr.start()
        try:
            assert "sz_x" not in mgr.monitor.stocks
        finally:
            mgr.stop()

    def test_status_shape(self, mgr: MonitorManager):
        """status() 返回的字典应包含所有必要字段"""
        s = mgr.status()
        for key in ("running", "stocks", "config_path", "check_count", "alert_count"):
            assert key in s

    def test_empty_when_no_history(self, mgr: MonitorManager):
        """无价格历史时 quotes 返回空值"""
        mgr.upsert_stock(StockConfig(code="sz1", name="A"))
        mgr.update_webhook("https://dummy")
        mgr.start()
        try:
            quotes = mgr.get_quotes()
            assert quotes == {"sz1": {"price": None, "change_percent": None, "as_of": None}}
        finally:
            mgr.stop()

    def test_uses_price_history(self, mgr: MonitorManager):
        """有价格历史时 quotes 返回最新价格和涨跌幅"""
        from datetime import datetime
        mgr.upsert_stock(StockConfig(code="sz1", name="A"))
        mgr.update_webhook("https://dummy")
        mgr.start()
        try:
            now = datetime.now()
            mgr.monitor.price_history["sz1"] = [
                {"time": now, "price": 100.0},
                {"time": now, "price": 103.0},
            ]
            mgr.monitor.yesterday_close["sz1"] = 100.0
            quotes = mgr.get_quotes()
            assert quotes["sz1"]["price"] == 103.0
            assert quotes["sz1"]["change_percent"] == 3.0
        finally:
            mgr.stop()
