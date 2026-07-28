"""涨跌停封单告警测试"""
from datetime import datetime, timedelta
from unittest.mock import patch

from stock_monitor.config import StockConfig
from stock_monitor.core import StockMonitor


def make_monitor(webhook: str = "http://x") -> StockMonitor:
    m = StockMonitor(dingding_webhook=webhook)
    m._limit_exhaust_seconds = 30
    m._limit_exhaust_samples = 3
    return m


def add_stock(m: StockMonitor, code: str, name: str = "测试", **cfg):
    config = {"name": name, "nickname": "", "cooldown_minutes": 5, **cfg}
    m.add_stock(code, config)
    return config


def set_quote(m, code, price, prev_close, bid1_vol=0.0, bid1_price=0.0,
              ask1_vol=0.0, ask1_price=0.0, name=None):
    m._latest_quote[code] = {
        "name": name or m.stocks[code].get("name", ""),
        "prev_close": prev_close,
        "price": price,
        "bid1_vol": bid1_vol,
        "bid1_price": bid1_price,
        "ask1_vol": ask1_vol,
        "ask1_price": ask1_price,
    }
    m.yesterday_close[code] = prev_close


def capture(m: StockMonitor):
    """劫持发送，收集消息列表"""
    msgs = []
    m._do_send = lambda message: msgs.append(message)
    m._alert_buffer.clear()
    # 批量模式下 send_dingding_notification 会入 buffer；这里强制走 _do_send
    m._batch_mode = False
    return msgs


# ---------- 涨跌停价计算 ----------

class TestLimitPriceCalc:
    def test_main_board_10(self):
        m = make_monitor()
        up, down, r = m._compute_limit_prices("sh600000", "浦发银行", 10.00)
        assert up == 11.00 and down == 9.00 and r == 0.10

    def test_chinext_20(self):
        m = make_monitor()
        up, down, r = m._compute_limit_prices("sz300001", "特锐德", 10.00)
        assert up == 12.00 and down == 8.00 and r == 0.20

    def test_star_20(self):
        m = make_monitor()
        up, down, r = m._compute_limit_prices("sh688001", "华兴源创", 100.00)
        assert up == 120.00 and down == 80.00 and r == 0.20

    def test_bj_30(self):
        m = make_monitor()
        up, down, r = m._compute_limit_prices("bj920001", "贝特瑞", 10.00)
        assert up == 13.00 and down == 7.00 and r == 0.30

    def test_st_5(self):
        m = make_monitor()
        up, down, r = m._compute_limit_prices("sh600001", "*ST清越", 10.00)
        assert up == 10.50 and down == 9.50 and r == 0.05

    def test_st_prefix(self):
        m = make_monitor()
        up, down, r = m._compute_limit_prices("sz000001", "ST平能", 10.00)
        assert r == 0.05 and up == 10.50

    def test_round_half_up(self):
        """12.42 × 1.10 = 13.662 → 13.66（四舍五入，非 banker 13.66）"""
        m = make_monitor()
        up, down, _ = m._compute_limit_prices("sh600619", "海立股份", 12.42)
        assert up == 13.66
        # 12.42 × 0.9 = 11.178 → 11.18
        assert down == 11.18

    def test_no_prev_close(self):
        m = make_monitor()
        up, down, r = m._compute_limit_prices("sh600000", "A", 0.0)
        assert up is None and r == 0


# ---------- 封板 / 开板边沿 ----------

class TestSealEdge:
    def test_seal_up_fires_limit_up(self):
        m = make_monitor()
        add_stock(m, "sh600619", "海立股份")
        msgs = capture(m)
        # 第一轮：未封板（初始化不告警）
        set_quote(m, "sh600619", 12.00, prev_close=12.42,
                  bid1_price=12.00, ask1_price=12.01, bid1_vol=100, ask1_vol=100)
        m.check_limit_status("sh600619", 12.00)
        assert msgs == []
        # 第二轮：封板（现价=涨停价13.66，无卖盘）
        set_quote(m, "sh600619", 13.66, prev_close=12.42,
                  bid1_price=13.66, ask1_price=0.0, bid1_vol=34968427, ask1_vol=0)
        m.check_limit_status("sh600619", 13.66)
        assert len(msgs) == 1
        assert "涨停" in msgs[0]
        # 封单 34968427 股 = 349684 手
        assert "349,684" in msgs[0]
        # 金额 = 34968427 × 13.66 / 10000 ≈ 47766.81 万元
        assert "47,766" in msgs[0]

    def test_break_up_fires_broken(self):
        m = make_monitor()
        add_stock(m, "sh600619", "海立股份")
        msgs = capture(m)
        # 初始化为封板状态
        set_quote(m, "sh600619", 13.66, prev_close=12.42,
                  bid1_price=13.66, ask1_price=0.0, bid1_vol=34968427, ask1_vol=0)
        m.check_limit_status("sh600619", 13.66)  # init，不告警
        assert msgs == []
        # 脱离涨停 → 开板
        set_quote(m, "sh600619", 13.50, prev_close=12.42,
                  bid1_price=13.50, ask1_price=13.51, bid1_vol=100, ask1_vol=100)
        m.check_limit_status("sh600619", 13.50)
        assert len(msgs) == 1
        assert "开板" in msgs[0]

    def test_seal_down_fires_limit_down(self):
        m = make_monitor()
        add_stock(m, "sh600000", "浦发银行")
        msgs = capture(m)
        set_quote(m, "sh600000", 10.00, prev_close=10.00,
                  bid1_price=10.00, ask1_price=10.01, bid1_vol=100, ask1_vol=100)
        m.check_limit_status("sh600000", 10.00)  # init
        assert msgs == []
        # 跌停：现价=9.00，无买盘
        set_quote(m, "sh600000", 9.00, prev_close=10.00,
                  bid1_price=0.0, ask1_price=9.00, bid1_vol=0, ask1_vol=5000000)
        m.check_limit_status("sh600000", 9.00)
        assert len(msgs) == 1
        assert "跌停" in msgs[0]
        assert "50,000" in msgs[0]  # 5000000股 = 50000手

    def test_disabled_alerts_suppressed(self):
        m = make_monitor()
        add_stock(m, "sh600619", "海立股份", disabled_alerts=["limit_up"])
        msgs = capture(m)
        set_quote(m, "sh600619", 12.00, prev_close=12.42,
                  bid1_price=12.00, ask1_price=12.01, bid1_vol=100, ask1_vol=100)
        m.check_limit_status("sh600619", 12.00)
        set_quote(m, "sh600619", 13.66, prev_close=12.42,
                  bid1_price=13.66, ask1_price=0.0, bid1_vol=34968427, ask1_vol=0)
        m.check_limit_status("sh600619", 13.66)
        assert msgs == []


# ---------- 封单不足 ----------

class TestLowSeal:
    def test_low_seal_fires_when_below_threshold(self):
        m = make_monitor()
        add_stock(m, "sh600619", "海立股份", limit_seal_min_lots=100000)
        msgs = capture(m)
        # 初始化为封板
        set_quote(m, "sh600619", 13.66, prev_close=12.42,
                  bid1_price=13.66, ask1_price=0.0, bid1_vol=20000000, ask1_vol=0)
        m.check_limit_status("sh600619", 13.66)  # init
        # 封单 20000000股 = 200000手 >= 100000，不告警
        m.check_limit_status("sh600619", 13.66)  # 封板持续，200000手
        assert all("封单不足" not in x for x in msgs)
        # 封单降到 5000000股 = 50000手 < 100000 → 告警
        set_quote(m, "sh600619", 13.66, prev_close=12.42,
                  bid1_price=13.66, ask1_price=0.0, bid1_vol=5000000, ask1_vol=0)
        m.check_limit_status("sh600619", 13.66)
        assert any("封单不足" in x for x in msgs)

    def test_low_seal_no_threshold_no_alert(self):
        m = make_monitor()
        add_stock(m, "sh600619", "海立股份")  # limit_seal_min_lots=None
        msgs = capture(m)
        set_quote(m, "sh600619", 13.66, prev_close=12.42,
                  bid1_price=13.66, ask1_price=0.0, bid1_vol=100, ask1_vol=0)
        m.check_limit_status("sh600619", 13.66)  # init
        m.check_limit_status("sh600619", 13.66)  # 封单 1手，但无阈值
        assert all("封单不足" not in x for x in msgs)


# ---------- 封单将尽 ----------

class TestExhaust:
    def test_exhaust_fires_when_eta_below_threshold(self):
        m = make_monitor()
        add_stock(m, "sh600619", "海立股份")
        msgs = capture(m)
        # 初始化 + 封板，封单 10000000 股
        set_quote(m, "sh600619", 13.66, prev_close=12.42,
                  bid1_price=13.66, ask1_price=0.0, bid1_vol=10000000, ask1_vol=0)
        m.check_limit_status("sh600619", 13.66)  # init
        # 推进封单历史：构造 3 个样本，过去消耗速度使 ETA < 30s
        # 假设每次间隔 10s，封单从 10000000 → 6000000 → 2000000
        now = datetime.now()
        m._seal_history["sh600619"] = [
            (now - timedelta(seconds=20), 10000000),
            (now - timedelta(seconds=10), 6000000),
        ]
        # 当前轮封单 2000000，rate = (10000000-2000000)/20 = 400000 股/s, eta = 2000000/400000 = 5s < 30
        set_quote(m, "sh600619", 13.66, prev_close=12.42,
                  bid1_price=13.66, ask1_price=0.0, bid1_vol=2000000, ask1_vol=0)
        m.check_limit_status("sh600619", 13.66)
        assert any("封单将尽" in x for x in msgs)
        # 消息中应含预测秒数
        assert any("5" in x for x in msgs)

    def test_exhaust_no_fire_when_seal_stable(self):
        m = make_monitor()
        add_stock(m, "sh600619", "海立股份")
        msgs = capture(m)
        set_quote(m, "sh600619", 13.66, prev_close=12.42,
                  bid1_price=13.66, ask1_price=0.0, bid1_vol=10000000, ask1_vol=0)
        m.check_limit_status("sh600619", 13.66)  # init
        # 封单不变（消耗=0），不应告警
        now = datetime.now()
        m._seal_history["sh600619"] = [
            (now - timedelta(seconds=20), 10000000),
            (now - timedelta(seconds=10), 10000000),
        ]
        set_quote(m, "sh600619", 13.66, prev_close=12.42,
                  bid1_price=13.66, ask1_price=0.0, bid1_vol=10000000, ask1_vol=0)
        m.check_limit_status("sh600619", 13.66)
        assert all("封单将尽" not in x for x in msgs)


# ---------- daily_reset ----------

class TestDailyReset:
    def test_daily_reset_clears_limit_state(self):
        m = make_monitor()
        add_stock(m, "sh600619", "海立股份")
        # 置为封板状态
        m._limit_state["sh600619"]["is_limit_up"] = True
        m._limit_state["sh600619"]["_init"] = True
        m._seal_history["sh600619"] = [(datetime.now(), 1000)]
        m._low_seal_fired["sh600619"] = True
        m._exhaust_fired["sh600619"] = True
        m.daily_reset({})
        assert m._limit_state["sh600619"]["is_limit_up"] is False
        assert m._limit_state["sh600619"]["_init"] is False
        assert m._seal_history["sh600619"] == []
        assert m._low_seal_fired["sh600619"] is False
        assert m._exhaust_fired["sh600619"] is False


# ---------- 基金不参与 ----------

class TestFundExcluded:
    def test_fund_not_in_limit_state(self):
        m = make_monitor()
        add_stock(m, "sz000001", "A")
        # 模拟基金（add_fund 不应初始化 limit 状态）
        m.add_fund("sz159915", {"name": "ETF", "nickname": "", "cooldown_minutes": 5})
        assert "sh600619" not in m._limit_state  # 未添加
        assert "sz159915" not in m._limit_state   # 基金不初始化
        # check_limit_status 对基金应直接返回
        msgs = capture(m)
        m.check_limit_status("sz159915", 1.0)
        assert msgs == []