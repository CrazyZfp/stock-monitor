"""合约监控测试"""
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from stock_monitor.config import Config, CryptoConfig, ConfigStore
from stock_monitor.core import StockMonitor
from stock_monitor.webui.app import create_app


def make_monitor(webhook: str = "http://x") -> StockMonitor:
    return StockMonitor(dingding_webhook=webhook)


def add_crypto(m: StockMonitor, code: str, name: str = "BTCUSDT", **cfg):
    config = {
        "name": name,
        "nickname": "",
        "cooldown_minutes": 5,
        "price_high": None,
        "price_low": None,
        "t_threshold": None,
        "t_events": [],
        "t_s_enabled": True,
        "t_b_enabled": True,
        "disabled_alerts": [],
        "price_precision": 2,
        **cfg,
    }
    m.add_crypto(code, config)
    return config


def capture(m: StockMonitor):
    msgs = []
    m._do_send = lambda message: msgs.append(message)
    m._batch_mode = False
    return msgs


# ---------- add_crypto 状态初始化 ----------

class TestAddCrypto:
    def test_registered_in_crypto_codes(self):
        m = make_monitor()
        add_crypto(m, "fapi:BTCUSDT")
        assert "fapi:BTCUSDT" in m._crypto_codes
        assert "fapi:BTCUSDT" in m.stocks

    def test_no_limit_state_initialized(self):
        """合约不应初始化涨跌停状态（check_limit_status 会自动 return）"""
        m = make_monitor()
        add_crypto(m, "fapi:BTCUSDT")
        assert "fapi:BTCUSDT" not in m._limit_state
        assert "fapi:BTCUSDT" not in m._seal_history

    def test_t_events_loaded(self):
        m = make_monitor()
        add_crypto(m, "fapi:BTCUSDT", t_events=[{"id": "abc", "type": "S", "price": 100}])
        assert len(m.t_events["fapi:BTCUSDT"]) == 1


# ---------- 价格阈值告警 ----------

class TestPriceThreshold:
    def test_price_high_triggers(self):
        m = make_monitor()
        add_crypto(m, "fapi:BTCUSDT", price_high=100000)
        m.yesterday_close["fapi:BTCUSDT"] = 95000
        msgs = capture(m)
        # 第一次调用：初始化基线（价格低于阈值，不告警）
        m.check_stock_alerts("fapi:BTCUSDT", override_price=99000)
        assert len(msgs) == 0
        # 第二次调用：价格突破阈值，触发告警
        m.check_stock_alerts("fapi:BTCUSDT", override_price=101000)
        assert len(msgs) == 1
        assert "BTCUSDT" in msgs[0]

    def test_price_low_triggers(self):
        m = make_monitor()
        add_crypto(m, "fapi:BTCUSDT", price_low=90000)
        m.yesterday_close["fapi:BTCUSDT"] = 95000
        msgs = capture(m)
        m.check_stock_alerts("fapi:BTCUSDT", override_price=91000)
        assert len(msgs) == 0
        m.check_stock_alerts("fapi:BTCUSDT", override_price=89000)
        assert len(msgs) == 1

    def test_no_alert_when_price_in_range(self):
        m = make_monitor()
        add_crypto(m, "fapi:BTCUSDT", price_high=100000, price_low=90000)
        m.yesterday_close["fapi:BTCUSDT"] = 95000
        msgs = capture(m)
        m.check_stock_alerts("fapi:BTCUSDT", override_price=95000)
        m.check_stock_alerts("fapi:BTCUSDT", override_price=95000)
        assert len(msgs) == 0


# ---------- 价格精度格式化 ----------

class TestPricePrecision:
    def test_precision_applied_to_message(self):
        m = make_monitor()
        m.disguise_templates['price_high'] = ["🟢 {name} 突破 {threshold} 现{price}"]
        add_crypto(m, "dapi:DOGEUSD_PERP", price_high=0.5, price_precision=6)
        m.yesterday_close["dapi:DOGEUSD_PERP"] = 0.4
        msgs = capture(m)
        m.check_stock_alerts("dapi:DOGEUSD_PERP", override_price=0.4)  # init
        m.check_stock_alerts("dapi:DOGEUSD_PERP", override_price=0.51)  # trigger
        assert len(msgs) == 1
        # 6 位小数
        assert "0.510000" in msgs[0]
        assert "0.500000" in msgs[0]

    def test_default_precision_2_for_stocks(self):
        m = make_monitor()
        m.disguise_templates['price_high'] = ["🟢 {name} 突破 {threshold}"]
        m.add_stock("sh600000", {"name": "测试", "nickname": "", "cooldown_minutes": 5})
        m.stocks["sh600000"]["price_high"] = 10
        m.yesterday_close["sh600000"] = 9
        msgs = capture(m)
        m.check_stock_alerts("sh600000", override_price=9)  # init
        m.check_stock_alerts("sh600000", override_price=11)  # trigger
        assert len(msgs) == 1
        assert "10.00" in msgs[0]


# ---------- 做T事件 ----------

class TestTEvents:
    def test_t_sell_triggers_when_price_drops(self):
        m = make_monitor()
        add_crypto(m, "fapi:BTCUSDT", t_threshold=3.0)
        m.yesterday_close["fapi:BTCUSDT"] = 100000
        m.t_events["fapi:BTCUSDT"] = [{"id": "ev1", "type": "S", "price": 100000, "target_price": None}]
        msgs = capture(m)
        # 跌 3% → 97000
        m.check_stock_alerts("fapi:BTCUSDT", override_price=97000)
        assert len(msgs) == 1
        assert len(m.t_events["fapi:BTCUSDT"]) == 0  # 触发后移除

    def test_t_buy_triggers_when_price_rises(self):
        m = make_monitor()
        add_crypto(m, "fapi:BTCUSDT", t_threshold=3.0)
        m.yesterday_close["fapi:BTCUSDT"] = 100000
        m.t_events["fapi:BTCUSDT"] = [{"id": "ev1", "type": "B", "price": 100000, "target_price": None}]
        msgs = capture(m)
        m.check_stock_alerts("fapi:BTCUSDT", override_price=103000)
        assert len(msgs) == 1

    def test_t_event_with_target_price(self):
        m = make_monitor()
        add_crypto(m, "fapi:BTCUSDT")
        m.yesterday_close["fapi:BTCUSDT"] = 100000
        m.t_events["fapi:BTCUSDT"] = [{"id": "ev1", "type": "S", "price": 100000, "target_price": 95000}]
        msgs = capture(m)
        m.check_stock_alerts("fapi:BTCUSDT", override_price=96000)
        assert len(msgs) == 0  # 未到目标价
        m.check_stock_alerts("fapi:BTCUSDT", override_price=95000)
        assert len(msgs) == 1


# ---------- check_limit_status 跳过合约 ----------

class TestLimitSkipped:
    def test_check_limit_status_returns_early_for_crypto(self):
        m = make_monitor()
        add_crypto(m, "fapi:BTCUSDT")
        # 不应抛异常，也不应有任何封单逻辑
        m.check_limit_status("fapi:BTCUSDT", 100000)


# ---------- fetch_crypto_prices (mock) ----------

class TestFetchCryptoPrices:
    def test_parses_ticker_24hr(self):
        m = make_monitor()
        add_crypto(m, "fapi:BTCUSDT")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "lastPrice": "65000.50",
            "prevClosePrice": "64000.00",
        }
        with patch("requests.get", return_value=mock_resp):
            m._crypto_precision = {"fapi:BTCUSDT": 2}  # 跳过 exchangeInfo 拉取
            prices = m.fetch_crypto_prices(["fapi:BTCUSDT"])
        assert prices["fapi:BTCUSDT"] == 65000.50
        assert m.yesterday_close["fapi:BTCUSDT"] == 64000.00
        assert m.stocks["fapi:BTCUSDT"]["price_precision"] == 2


# ---------- Config 持久化 ----------

class TestCryptoConfig:
    def test_to_dict_and_from_dict(self):
        c = CryptoConfig(
            code="fapi:BTCUSDT",
            name="BTCUSDT",
            nickname="大饼",
            price_high=100000,
            price_low=90000,
            t_threshold=3.0,
            t_events=[{"id": "x", "type": "S", "price": 100}],
        )
        d = c.to_dict()
        assert d["code"] == "fapi:BTCUSDT"
        c2 = CryptoConfig.from_dict(d)
        assert c2.code == "fapi:BTCUSDT"
        assert c2.price_high == 100000
        assert len(c2.t_events) == 1

    def test_config_cryptos_serialization(self):
        cfg = Config(cryptos=[CryptoConfig(code="fapi:BTCUSDT", name="BTC")])
        d = cfg.to_dict()
        assert "cryptos" in d
        assert len(d["cryptos"]) == 1
        cfg2 = Config.from_dict(d)
        assert len(cfg2.cryptos) == 1
        assert cfg2.find_crypto("fapi:BTCUSDT") is not None
        assert cfg2.find_crypto("dapi:BTCUSD_PERP") is None


# ---------- API 端点 ----------

@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_app(config_path=tmp_path / "config.json", interval_seconds=3600)
    with TestClient(app) as c:
        yield c


class TestCryptoAPI:
    def test_create_and_list(self, client: TestClient):
        r = client.post("/api/cryptos", json={"code": "fapi:BTCUSDT", "name": "BTCUSDT"})
        assert r.status_code == 201
        r = client.get("/api/cryptos")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["code"] == "fapi:BTCUSDT"
        assert "quote" in data[0]

    def test_create_duplicate_409(self, client: TestClient):
        client.post("/api/cryptos", json={"code": "fapi:BTCUSDT", "name": "BTC"})
        r = client.post("/api/cryptos", json={"code": "fapi:BTCUSDT", "name": "BTC"})
        assert r.status_code == 409

    def test_update(self, client: TestClient):
        client.post("/api/cryptos", json={"code": "fapi:BTCUSDT", "name": "BTC"})
        r = client.put("/api/cryptos/fapi:BTCUSDT", json={
            "code": "fapi:BTCUSDT", "name": "BTC2", "price_high": 100000
        })
        assert r.status_code == 200
        assert r.json()["name"] == "BTC2"

    def test_delete(self, client: TestClient):
        client.post("/api/cryptos", json={"code": "fapi:BTCUSDT", "name": "BTC"})
        r = client.delete("/api/cryptos/fapi:BTCUSDT")
        assert r.status_code == 200
        assert len(client.get("/api/cryptos").json()) == 0

    def test_patch_enabled(self, client: TestClient):
        client.post("/api/cryptos", json={"code": "fapi:BTCUSDT", "name": "BTC"})
        r = client.patch("/api/cryptos/fapi:BTCUSDT/enabled", json={"enabled": False})
        assert r.status_code == 200

    def test_t_event_crud(self, client: TestClient):
        client.post("/api/cryptos", json={"code": "fapi:BTCUSDT", "name": "BTC"})
        r = client.post("/api/cryptos/fapi:BTCUSDT/t-events", json={"type": "S", "price": 100000})
        assert r.status_code == 201
        event_id = r.json()["id"]
        # 更新
        r = client.put(f"/api/cryptos/fapi:BTCUSDT/t-events/{event_id}", json={"type": "S", "price": 101000})
        assert r.status_code == 200
        # 删除
        r = client.delete(f"/api/cryptos/fapi:BTCUSDT/t-events/{event_id}")
        assert r.status_code == 200

    def test_export_import_cryptos(self, client: TestClient):
        client.post("/api/cryptos", json={"code": "fapi:BTCUSDT", "name": "BTC"})
        export = client.get("/api/export?scope=cryptos").json()
        assert "cryptos" in export
