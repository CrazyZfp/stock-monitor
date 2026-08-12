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


@pytest.fixture
def m() -> StockMonitor:
    return make_monitor()


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


# ---------- 市场模板回退 ----------

class TestMarketTemplateFallback:
    def test_market_overrides_global(self):
        """合约市场模板覆盖全局模板"""
        m = make_monitor()
        m.disguise_templates['price_high'] = ["GLOBAL {name}"]
        m.market_templates['crypto'] = {'price_high': ["CRYPTO {name}"]}
        add_crypto(m, "fapi:BTCUSDT", price_high=100000)
        m.yesterday_close["fapi:BTCUSDT"] = 95000
        msgs = capture(m)
        m.check_stock_alerts("fapi:BTCUSDT", override_price=99000)  # init
        m.check_stock_alerts("fapi:BTCUSDT", override_price=101000)  # trigger
        assert len(msgs) == 1
        assert msgs[0].startswith("CRYPTO ")

    def test_stock_falls_back_to_global(self):
        """股票市场无覆盖时回退全局模板"""
        m = make_monitor()
        m.disguise_templates['price_high'] = ["GLOBAL {name}"]
        # market_templates.stock 为空
        m.add_stock("sh600000", {"name": "测试", "nickname": "", "cooldown_minutes": 5})
        m.stocks["sh600000"]["price_high"] = 10
        m.yesterday_close["sh600000"] = 9
        msgs = capture(m)
        m.check_stock_alerts("sh600000", override_price=9)   # init
        m.check_stock_alerts("sh600000", override_price=11)   # trigger
        assert len(msgs) == 1
        assert msgs[0].startswith("GLOBAL ")

    def test_empty_market_list_falls_back(self):
        """市场模板显式为空列表时回退全局（避免发空消息）"""
        m = make_monitor()
        m.disguise_templates['price_high'] = ["GLOBAL {name}"]
        m.market_templates['crypto'] = {'price_high': []}  # 显式空列表
        add_crypto(m, "fapi:BTCUSDT", price_high=100000)
        m.yesterday_close["fapi:BTCUSDT"] = 95000
        msgs = capture(m)
        m.check_stock_alerts("fapi:BTCUSDT", override_price=99000)  # init
        m.check_stock_alerts("fapi:BTCUSDT", override_price=101000)  # trigger
        assert len(msgs) == 1
        assert msgs[0].startswith("GLOBAL ")

    def test_no_template_sends_nothing(self):
        """全局和市场都无该 alert_type 模板时不发送"""
        m = make_monitor()
        m.disguise_templates = {}  # 清空全局
        m.market_templates = {"stock": {}, "fund": {}, "crypto": {}}
        m.add_stock("sh600000", {"name": "测试", "nickname": "", "cooldown_minutes": 5})
        m.stocks["sh600000"]["price_high"] = 10
        m.yesterday_close["sh600000"] = 9
        msgs = capture(m)
        m.check_stock_alerts("sh600000", override_price=9)   # init
        m.check_stock_alerts("sh600000", override_price=11)   # trigger
        assert len(msgs) == 0


# ---------- 通知通道分发 ----------

class TestNotifyChannelDispatch:
    def _make(self, **kw):
        defaults = dict(dingding_webhook="http://x", notify_mode="single",
                        notify_channels={"dingding": True, "email": False},
                        notify_priority=["dingding", "email"])
        defaults.update(kw)
        return StockMonitor(**defaults)

    def test_single_mode_first_ready_channel(self):
        """single 模式：首个开启且完整的通道发送"""
        m = self._make()
        calls = []
        m._do_send = lambda msg: calls.append(("dd", msg)) or True
        m._send_email = lambda msg: calls.append(("email", msg)) or True
        m.send_notification("hello")
        assert calls == [("dd", "hello")]

    def test_single_mode_falls_back_on_failure(self):
        """single 模式：首个失败则回退到下一个，直到成功"""
        m = self._make(notify_channels={"dingding": True, "email": True},
                       notify_priority=["dingding", "email"],
                       email_smtp_host="smtp.x", email_username="a@b", email_to_addrs=["c@d"])
        calls = []
        m._do_send = lambda msg: calls.append(("dd", msg)) or False   # 失败
        m._send_email = lambda msg: calls.append(("email", msg)) or True
        m.send_notification("hello")
        assert calls == [("dd", "hello"), ("email", "hello")]

    def test_single_mode_stops_after_success(self):
        """single 模式：成功后不再尝试后续通道"""
        m = self._make(notify_channels={"dingding": True, "email": True},
                       notify_priority=["dingding", "email"])
        calls = []
        m._do_send = lambda msg: calls.append(("dd", msg)) or True   # 成功
        m._send_email = lambda msg: calls.append(("email", msg)) or True
        m.send_notification("hello")
        assert calls == [("dd", "hello")]  # email 未被调用

    def test_single_mode_skips_disabled_channel(self):
        """single 模式：跳过未开启的通道"""
        m = self._make(notify_channels={"dingding": False, "email": True},
                       notify_priority=["dingding", "email"],
                       email_smtp_host="smtp.x", email_username="a@b", email_to_addrs=["c@d"])
        calls = []
        m._do_send = lambda msg: calls.append(("dd", msg)) or True
        m._send_email = lambda msg: calls.append(("email", msg)) or True
        m.send_notification("hello")
        assert calls == [("email", "hello")]

    def test_single_mode_all_fail(self):
        """single 模式：所有通道失败，全部尝试"""
        m = self._make(notify_channels={"dingding": True, "email": True},
                       notify_priority=["dingding", "email"],
                       email_smtp_host="smtp.x", email_username="a@b", email_to_addrs=["c@d"])
        calls = []
        m._do_send = lambda msg: calls.append(("dd", msg)) or False
        m._send_email = lambda msg: calls.append(("email", msg)) or False
        m.send_notification("hello")
        assert calls == [("dd", "hello"), ("email", "hello")]

    def test_multi_mode_sends_all_ready(self):
        """multi 模式：向所有开启且完整的通道发送，互不影响"""
        m = self._make(notify_mode="multi",
                       notify_channels={"dingding": True, "email": True},
                       notify_priority=["dingding", "email"],
                       email_smtp_host="smtp.x", email_username="a@b", email_to_addrs=["c@d"])
        calls = []
        m._do_send = lambda msg: calls.append(("dd", msg)) or False
        m._send_email = lambda msg: calls.append(("email", msg)) or True
        m.send_notification("hello")
        assert calls == [("dd", "hello"), ("email", "hello")]

    def test_multi_mode_skips_disabled(self):
        """multi 模式：只发开启且完整的通道"""
        m = self._make(notify_mode="multi",
                       notify_channels={"dingding": True, "email": False})
        calls = []
        m._do_send = lambda msg: calls.append(("dd", msg)) or True
        m._send_email = lambda msg: calls.append(("email", msg)) or True
        m.send_notification("hello")
        assert calls == [("dd", "hello")]

    def test_empty_message_dropped(self):
        """空消息直接丢弃"""
        m = self._make()
        calls = []
        m._do_send = lambda msg: calls.append(msg)
        m._send_email = lambda msg: calls.append(msg)
        m.send_notification("")
        m.send_notification("   \n  ")
        assert calls == []

    def test_send_notification_returns_false_when_no_channel_ready(self):
        """无可用通道时返回 False（供测试消息等判断真实送达）"""
        m = self._make(notify_channels={"dingding": False, "email": False})
        assert m.send_notification("hello") is False

    def test_send_notification_returns_false_when_all_fail(self):
        """single 模式全部通道失败时返回 False"""
        m = self._make(notify_channels={"dingding": True, "email": True},
                       notify_priority=["dingding", "email"],
                       email_smtp_host="smtp.x", email_username="a@b", email_to_addrs=["c@d"])
        m._do_send = lambda msg: False
        m._send_email = lambda msg: False
        assert m.send_notification("hello") is False

    def test_send_notification_returns_true_on_delivery(self):
        """有通道送达时返回 True"""
        m = self._make()
        m._do_send = lambda msg: True
        assert m.send_notification("hello") is True

    def test_send_notification_returns_false_in_multi_mode_when_all_fail(self):
        """multi 模式全部通道失败时返回 False"""
        m = self._make(notify_mode="multi",
                       notify_channels={"dingding": True, "email": False})
        m._do_send = lambda msg: False
        assert m.send_notification("hello") is False

    def test_send_notification_batch_mode_buffers_and_returns_true(self):
        """批量模式下入缓冲视为已送达，返回 True"""
        m = self._make()
        m._batch_mode = True
        m._do_send = lambda msg: (_ for _ in ()).throw(AssertionError("不应直接发送"))
        assert m.send_notification("a") is True
        assert m._alert_buffer == ["a"]

    def test_batch_mode_buffers(self):
        """batch 模式下消息进缓冲区而非直接发送"""
        m = self._make()
        m._batch_mode = True
        m._do_send = lambda msg: (_ for _ in ()).throw(AssertionError("不应直接发送"))
        m.send_notification("a")
        m.send_notification("b")
        assert m._alert_buffer == ["a", "b"]

    def test_send_email_via_mock_smtp(self):
        """_send_email 用 mock SMTP 验证登录、发送、退出"""
        with patch("smtplib.SMTP_SSL") as mock_smtp:
            instance = MagicMock()
            mock_smtp.return_value = instance
            m = StockMonitor(dingding_webhook="http://x", notify_mode="single",
                             notify_channels={"email": True}, notify_priority=["email"],
                             email_smtp_host="smtp.qq.com", email_smtp_port=465,
                             email_username="a@b.com", email_password="pwd",
                             email_to_addrs=["c@d.com"], email_use_ssl=True)
            ok = m._send_email("告警内容")
            assert ok is True
            mock_smtp.assert_called_once_with("smtp.qq.com", 465, timeout=10)
            instance.login.assert_called_once_with("a@b.com", "pwd")
            instance.sendmail.assert_called_once()
            args = instance.sendmail.call_args.args
            assert args[0] == "a@b.com"
            assert args[1] == ["c@d.com"]
            assert "From: a@b.com" in args[2]
            assert "To: c@d.com" in args[2]
            assert "Subject:" in args[2]
            instance.quit.assert_called_once()

    def test_send_email_returns_false_on_exception(self):
        """_send_email 异常时返回 False（供 single 模式回退判断）"""
        with patch("smtplib.SMTP_SSL", side_effect=Exception("conn refused")):
            m = StockMonitor(dingding_webhook="http://x", notify_mode="single",
                             notify_channels={"email": True}, notify_priority=["email"],
                             email_smtp_host="smtp.x", email_username="a@b",
                             email_to_addrs=["c@d"])
            assert m._send_email("x") is False


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

    def test_parses_dapi_list_response(self):
        """dapi /v1/ticker/24hr 返回 list 而非 dict，应取 [0]"""
        m = make_monitor()
        add_crypto(m, "dapi:ETHUSD_PERP")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"lastPrice": "1913.94", "prevClosePrice": "1916.10", "symbol": "ETHUSD_PERP"},
        ]
        with patch("requests.get", return_value=mock_resp):
            m._crypto_precision = {"dapi:ETHUSD_PERP": 2}
            prices = m.fetch_crypto_prices(["dapi:ETHUSD_PERP"])
        assert prices["dapi:ETHUSD_PERP"] == 1913.94
        assert m.yesterday_close["dapi:ETHUSD_PERP"] == 1916.10


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


# ---------- _do_send 钉钉响应 errcode 处理 ----------

class TestDoSend:
    """钉钉 HTTP 200 但 body errcode!=0 时（如关键词不匹配）应记为失败，不得静默吞掉"""

    def test_success_when_errcode_zero(self, m: StockMonitor, caplog):
        with patch("stock_monitor.core.requests.post") as mock_post:
            resp = MagicMock(status_code=200)
            resp.json.return_value = {"errcode": 0, "errmsg": "ok"}
            mock_post.return_value = resp
            with caplog.at_level("INFO", logger="stock_monitor.core"):
                m._do_send("test message")
        assert "发送成功" in caplog.text
        assert "被拒" not in caplog.text

    def test_keyword_prepended_to_content(self, m: StockMonitor):
        m.dingding_keyword = "【预警】"
        with patch("stock_monitor.core.requests.post") as mock_post:
            resp = MagicMock(status_code=200)
            resp.json.return_value = {"errcode": 0, "errmsg": "ok"}
            mock_post.return_value = resp
            assert m._do_send("test message")
        payload = mock_post.call_args.kwargs["data"]
        import json as _json
        assert _json.loads(payload)["text"]["content"] == "【预警】test message"

    def test_no_keyword_when_empty(self, m: StockMonitor):
        m.dingding_keyword = ""
        with patch("stock_monitor.core.requests.post") as mock_post:
            resp = MagicMock(status_code=200)
            resp.json.return_value = {"errcode": 0, "errmsg": "ok"}
            mock_post.return_value = resp
            assert m._do_send("test message")
        payload = mock_post.call_args.kwargs["data"]
        import json as _json
        assert _json.loads(payload)["text"]["content"] == "test message"

    def test_rejected_when_errcode_nonzero(self, m: StockMonitor, caplog):
        with patch("stock_monitor.core.requests.post") as mock_post:
            resp = MagicMock(status_code=200)
            resp.json.return_value = {"errcode": 310000, "errmsg": "关键词不匹配"}
            mock_post.return_value = resp
            with caplog.at_level("ERROR", logger="stock_monitor.core"):
                m._do_send("test message")
        assert "发送成功" not in caplog.text
        assert "errcode=310000" in caplog.text
        assert "关键词不匹配" in caplog.text

    def test_failed_on_http_error(self, m: StockMonitor, caplog):
        with patch("stock_monitor.core.requests.post") as mock_post:
            resp = MagicMock(status_code=500)
            mock_post.return_value = resp
            with caplog.at_level("ERROR", logger="stock_monitor.core"):
                m._do_send("test message")
        assert "发送失败: 500" in caplog.text
