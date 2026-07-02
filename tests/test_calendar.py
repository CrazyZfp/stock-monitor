"""交易日历判断测试

覆盖 is_trading_day / is_trading_time 在工作日、法定节假日、周末、午休、盘前盘后等场景。
"""
import datetime
from datetime import datetime as dt
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from stock_monitor import StockMonitor

BEIJING = ZoneInfo("Asia/Shanghai")


def at(year, month, day, hour=10, minute=0):
    return dt(year, month, day, hour, minute, tzinfo=BEIJING)


# ----- is_trading_day -----

class TestIsTradingDay:
    def test_normal_weekday(self):
        # 2026-06-26 周五，普通工作日
        assert StockMonitor.is_trading_day(at(2026, 6, 26).date()) is True

    def test_weekend_saturday(self):
        assert StockMonitor.is_trading_day(at(2026, 6, 27).date()) is False

    def test_weekend_sunday(self):
        assert StockMonitor.is_trading_day(at(2026, 6, 28).date()) is False

    def test_national_day_holiday(self):
        # 2026-10-01 国庆
        assert StockMonitor.is_trading_day(at(2026, 10, 1).date()) is False

    def test_friday_but_holiday(self):
        # 2026-06-19 周五但端午休市 —— 旧实现的盲区
        assert StockMonitor.is_trading_day(at(2026, 6, 19).date()) is False

    def test_spring_festival(self):
        # 2026-02-16~23 春节连休
        for day in range(16, 24):
            assert StockMonitor.is_trading_day(at(2026, 2, day).date()) is False, \
                f"2026-02-{day} 应为休市"

    def test_graceful_degradation_on_lib_error(self):
        # 库挂掉时降级为"仅看周末"
        with patch("stock_monitor.core.shsz.get_cached",
                   side_effect=RuntimeError("模拟库异常")):
            # 周五仍应返回 True
            assert StockMonitor.is_trading_day(at(2026, 6, 26).date()) is True
            # 周末仍应返回 False
            assert StockMonitor.is_trading_day(at(2026, 6, 27).date()) is False


# ----- is_trading_time -----

class TestIsTradingTime:
    @pytest.mark.parametrize("hour, minute, expected", [
        (9,  0,  False),  # 盘前
        (9,  19, False),  # 集合竞价尚未开始
        (9,  20, True),   # 9:20 开始轮询
        (9,  30, True),   # 开盘边界
        (10, 0,  True),   # 早盘
        (11, 30, True),   # 午盘收盘边界
        (11, 31, False),  # 午休
        (12, 0,  False),  # 午休
        (12, 59, False),  # 午后开盘前 1 分钟
        (13, 0,  True),   # 午后开盘边界
        (14, 30, True),   # 午后
        (15, 0,  True),   # 收盘边界
        (15, 1,  False),  # 盘后
        (20, 0,  False),  # 夜晚
    ])
    def test_time_boundaries(self, hour, minute, expected):
        # 2026-06-26 周五，盘中时段
        assert StockMonitor.is_trading_time(at(2026, 6, 26, hour, minute)) is expected

    def test_weekend_returns_false_during_session(self):
        # 周六 10:00 不应处于交易时段
        assert StockMonitor.is_trading_time(at(2026, 6, 27, 10, 0)) is False

    def test_holiday_returns_false_during_session(self):
        # 国庆节 10:00 不应处于交易时段
        assert StockMonitor.is_trading_time(at(2026, 10, 1, 10, 0)) is False

    def test_holiday_friday_returns_false(self):
        # 2026-06-19 周五 10:00 是端午休市
        assert StockMonitor.is_trading_time(at(2026, 6, 19, 10, 0)) is False

    def test_default_uses_beijing_time(self):
        # 不传参：脚本应能正常运行不抛异常
        # 不校验具体值（依赖运行环境当前时间）
        result = StockMonitor.is_trading_time()
        assert isinstance(result, bool)

    def test_naive_datetime_assumed_beijing(self):
        # 传无时区的 datetime 时，应被当作北京时间
        naive = dt(2026, 6, 26, 10, 0)  # 无 tzinfo
        assert StockMonitor.is_trading_time(naive) is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
