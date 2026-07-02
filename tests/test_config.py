"""ConfigStore + Config 数据类的单元测试"""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from stock_monitor.config import (
    Config,
    ConfigStore,
    StockConfig,
    default_config_path,
    default_log_dir,
)


@pytest.fixture
def tmp_store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(path=tmp_path / "config.json")


class TestStockConfig:
    def test_round_trip(self):
        """测试 StockConfig 序列化与反序列化后的相等性"""
        s = StockConfig(
            code="sz300115",
            name="CY",
            nickname="我的茅台",
            price_high=45.0,
            price_low=42.5,
            speed_threshold=2.5,
            speed_window=5,
            cooldown_minutes=5,
        )
        d = s.to_dict()
        s2 = StockConfig.from_dict(d)
        assert s2 == s

    def test_minimal(self):
        """测试最少字段构造 StockConfig 时各字段默认值正确"""
        s = StockConfig.from_dict({"code": "sh600000", "name": "X"})
        assert s.nickname == ""
        assert s.price_high is None
        assert s.speed_window == 5
        assert s.cooldown_minutes == 5
        assert s.enabled is True

    def test_nickname_default_empty(self):
        """测试 nickname 字段默认值为空字符串"""
        s = StockConfig(code="x", name="X")
        assert s.nickname == ""

    def test_tier_getters(self):
        """测试 tier getter 方法：get_high_tiers / get_low_tiers / get_change_high_tiers / get_change_low_tiers"""
        s = StockConfig.from_dict({"code": "sz1", "name": "A"})
        assert s.daily_change_up == []
        assert s.daily_change_down == []
        assert s.retracement_threshold is None
        assert s.bounce_threshold is None

    def test_backward_compat_old_field_names(self):
        """旧字段名（surge_threshold/surge_period/price_tiers_*）应被新字段名兼容"""
        s = StockConfig.from_dict({
            "code": "sz1", "name": "A",
            "surge_threshold": 2.5, "surge_period": 5,
            "price_tiers_high": [2.0, 5.0], "price_tiers_low": [3.0],
        })
        assert s.speed_threshold == 2.5
        assert s.speed_window == 5
        assert s.daily_change_up == [2.0, 5.0]
        assert s.daily_change_down == [3.0]


class TestConfig:
    def test_round_trip(self):
        """测试 Config 序列化与反序列化，验证字段和模板默认值"""
        cfg = Config(
            dingding_webhook="https://x",
            disguise_templates={"price_high": ["🟢 {name}"]},
            stocks=[StockConfig(code="sz1", name="A")],
        )
        d = cfg.to_dict()
        cfg2 = Config.from_dict(d)
        assert cfg2.dingding_webhook == "https://x"
        # 加载时自动合并 DEFAULT_TEMPLATES，确保 daily_up/daily_down 等新类型有默认值
        assert cfg2.disguise_templates["price_high"] == ["🟢 {name}"]
        assert "daily_up" in cfg2.disguise_templates
        assert "daily_down" in cfg2.disguise_templates
        assert len(cfg2.stocks) == 1
        assert cfg2.stocks[0].code == "sz1"

    def test_find_stock(self):
        """测试根据 code 查找 stock，不存在的返回 None"""
        cfg = Config(stocks=[StockConfig(code="a", name="A"), StockConfig(code="b", name="B")])
        assert cfg.find_stock("a").name == "A"
        assert cfg.find_stock("missing") is None


class TestConfigStore:
    def test_load_creates_default(self, tmp_store: ConfigStore):
        """测试加载不存在的配置文件时自动创建默认配置"""
        cfg = tmp_store.load()
        assert cfg.dingding_webhook == ""
        assert "price_high" in cfg.disguise_templates
        assert "daily_up" in cfg.disguise_templates

    def test_save_and_load(self, tmp_store: ConfigStore):
        """测试配置保存后再读取的一致性"""
        cfg = Config(
            dingding_webhook="https://test",
            stocks=[StockConfig(code="sz1", name="A", price_high=10.0)],
        )
        tmp_store.save(cfg)
        # 新实例应能读到
        store2 = ConfigStore(path=tmp_store.path)
        loaded = store2.load()
        assert loaded.dingding_webhook == "https://test"
        assert loaded.stocks[0].code == "sz1"
        assert loaded.stocks[0].price_high == 10.0

    def test_atomic_write_no_leftover(self, tmp_store: ConfigStore):
        """原子写不应留下 .tmp 文件"""
        tmp_store.save(Config(dingding_webhook="x"))
        assert (tmp_store.path.with_suffix(".json.tmp")).exists() is False
        assert tmp_store.path.exists()

    def test_env_var_fallback(self, tmp_path: Path):
        """测试环境变量 DINGDING_WEBHOOK 作为 webhook 备用值"""
        store = ConfigStore(path=tmp_path / "config.json")
        with patch.dict(os.environ, {"DINGDING_WEBHOOK": "https://from-env"}):
            cfg = store.load()
        assert cfg.dingding_webhook == "https://from-env"

    def test_file_webhook_takes_precedence(self, tmp_store: ConfigStore):
        """测试文件中的 webhook 优先级高于环境变量"""
        tmp_store.save(Config(dingding_webhook="https://from-file"))
        with patch.dict(os.environ, {"DINGDING_WEBHOOK": "https://from-env"}):
            cfg = tmp_store.load()
        assert cfg.dingding_webhook == "https://from-file"

    def test_corrupt_json_raises(self, tmp_store: ConfigStore):
        """测试损坏的 JSON 文件加载时抛出 ValueError"""
        tmp_store.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_store.path.write_text("{ this is not valid json", encoding="utf-8")
        with pytest.raises(ValueError, match="配置文件 JSON 损坏"):
            tmp_store.load()

    def test_update_mutator(self, tmp_store: ConfigStore):
        """测试 update 方法通过 mutator 修改配置并持久化"""
        tmp_store.save(Config(dingding_webhook="x", stocks=[StockConfig(code="a", name="A")]))
        def add_stock(cfg: Config):
            cfg.stocks.append(StockConfig(code="b", name="B"))
        tmp_store.update(add_stock)
        assert len(tmp_store.get().stocks) == 2

    def test_on_change_callback(self, tmp_store: ConfigStore):
        """测试 on_change 回调在每次 save 后被触发"""
        calls = []
        tmp_store.on_change(lambda cfg: calls.append(cfg.dingding_webhook))
        tmp_store.save(Config(dingding_webhook="first"))
        tmp_store.save(Config(dingding_webhook="second"))
        assert calls == ["first", "second"]

    def test_callback_error_does_not_break_save(self, tmp_store: ConfigStore):
        def bad(cfg):
            raise RuntimeError("boom")
        tmp_store.on_change(bad)
        # 不应抛
        tmp_store.save(Config(dingding_webhook="ok"))
        assert tmp_store.get().dingding_webhook == "ok"


class TestPlatformHelpers:
    def test_default_config_path_darwin(self):
        """测试 macOS 下默认配置路径正确"""
        with patch("stock_monitor.config.sys.platform", "darwin"):
            p = default_config_path()
        assert "Library/Application Support/stock-monitor" in str(p)
        assert p.name == "config.json"

    def test_default_config_path_linux(self):
        """测试 Linux 下默认配置路径使用 XDG_CONFIG_HOME"""
        with patch("stock_monitor.config.sys.platform", "linux"), \
             patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/xdg"}):
            p = default_config_path()
        assert "/tmp/xdg/stock-monitor" in str(p)
