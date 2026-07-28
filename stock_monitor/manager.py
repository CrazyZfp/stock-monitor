"""MonitorManager - 协调 ConfigStore ↔ StockMonitor 实例，支持热重载

工作模型：
- 单例 StockMonitor 实例由 manager 持有
- 每次 monitor loop 开头（以及 API 写完后）从 ConfigStore 读最新 cfg
- 与旧 cfg diff，仅对变化的部分做最小改动：
    - 新增股票 → add_stock
    - 移除股票 → 从 self.stocks 弹出 + 清掉对应的 price_history / cooldown / alert_status
    - 修改股票 → remove + add（实现简单）
    - webhook/templates 变化 → 重建（不重建 StockMonitor 实例，只更新字段）
- 用 RLock 保护 self.monitor
- 后台线程跑 monitor_loop
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from .config import Config, ConfigStore, FundConfig, StockConfig
from .core import StockMonitor

logger = logging.getLogger(__name__)


class MonitorManager:
    def __init__(self, config_store: ConfigStore, interval_seconds: int | None = None):
        self.store = config_store
        if interval_seconds is not None:
            self.interval_seconds = interval_seconds
        else:
            self.interval_seconds = config_store.get().poll_interval_seconds
        self._lock = threading.RLock()
        self.monitor: Optional[StockMonitor] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        # 运行状态
        self.stats = {
            "started_at": None,
            "last_check_at": None,
            "check_count": 0,
            "last_alert_at": None,
            "alert_count": 0,
            "last_error": None,
        }

    # ===== 生命周期 =====

    def start(self):
        """初始化 StockMonitor 并启动后台监控线程"""
        with self._lock:
            if self._running:
                return
            cfg = self.store.get()
            self.monitor = self._build_monitor(cfg)
            self._running = True
            self.stats["started_at"] = int(time.time())
            self._thread = threading.Thread(
                target=self._loop, name="stock-monitor-loop", daemon=True
            )
            self._thread.start()
            logger.info("MonitorManager 启动，监控 %d 只股票", len(cfg.stocks))

    def stop(self, timeout: float = 5.0):
        """停 monitor loop（不等线程结束就返回）"""
        with self._lock:
            if not self._running:
                return
            self._running = False
            if self.monitor is not None:
                self.monitor.stop()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("MonitorManager 已停止")

    # ===== CRUD 入口（由 api.py 调用） =====

    def upsert_stock(self, stock: StockConfig):
        with self._lock:
            def mut(cfg: Config):
                existing = cfg.find_stock(stock.code)
                if existing is not None:
                    cfg.stocks.remove(existing)
                cfg.stocks.append(stock)
            self.store.update(mut)
        self._apply_stock_change(stock.code)

    def delete_stock(self, code: str) -> bool:
        with self._lock:
            cfg = self.store.get()
            if cfg.find_stock(code) is None:
                return False
            def mut(c: Config):
                c.stocks = [s for s in c.stocks if s.code != code]
            self.store.update(mut)
        self._remove_stock_runtime(code)
        return True

    def patch_stock_enabled(self, code: str, enabled: bool) -> bool:
        with self._lock:
            cfg = self.store.get()
            target = cfg.find_stock(code)
            if target is None:
                return False
            target.enabled = enabled
            self.store.save(cfg)
        self._apply_stock_change(code)
        return True

    # ===== 基金 CRUD =====

    def upsert_fund(self, fund: FundConfig):
        with self._lock:
            def mut(cfg: Config):
                existing = cfg.find_fund(fund.code)
                if existing is not None:
                    cfg.funds.remove(existing)
                cfg.funds.append(fund)
            self.store.update(mut)
        self._apply_fund_change(fund.code)

    def delete_fund(self, code: str) -> bool:
        with self._lock:
            cfg = self.store.get()
            if cfg.find_fund(code) is None:
                return False
            def mut(c: Config):
                c.funds = [f for f in c.funds if f.code != code]
            self.store.update(mut)
        self._remove_fund_runtime(code)
        return True

    def patch_fund_enabled(self, code: str, enabled: bool) -> bool:
        with self._lock:
            cfg = self.store.get()
            target = cfg.find_fund(code)
            if target is None:
                return False
            target.enabled = enabled
            self.store.save(cfg)
        self._apply_fund_change(code)
        return True

    def update_poll_interval(self, seconds: int):
        if seconds < 5:
            seconds = 5
        with self._lock:
            cfg = self.store.get()
            cfg.poll_interval_seconds = seconds
            self.store.save(cfg)
        self._apply_runtime_changes()

    def update_webhook(self, webhook: str, at_mobiles: list[str] | None = None, at_user_ids: list[str] | None = None):
        with self._lock:
            cfg = self.store.get()
            cfg.dingding_webhook = webhook
            if at_mobiles is not None:
                cfg.at_mobiles = list(at_mobiles)
            if at_user_ids is not None:
                cfg.at_user_ids = list(at_user_ids)
            self.store.save(cfg)
        self._apply_runtime_changes()

    def update_templates(self, templates: dict):
        with self._lock:
            cfg = self.store.get()
            cfg.disguise_templates = templates
            self.store.save(cfg)
        self._apply_runtime_changes()

    def replace_config(self, cfg: Config):
        """整体替换（导入用）"""
        with self._lock:
            self.store.save(cfg)
        self._apply_runtime_changes()

    def export_config(self, scope: list[str]) -> dict:
        """按 scope 导出配置子集"""
        cfg = self.store.get()
        result = {}
        if "stocks" in scope:
            result["stocks"] = [s.to_dict() for s in cfg.stocks]
        if "funds" in scope:
            result["funds"] = [f.to_dict() for f in cfg.funds]
        if "templates" in scope:
            result["disguise_templates"] = cfg.disguise_templates
        if "webhook" in scope:
            result["dingding_webhook"] = cfg.dingding_webhook
            result["at_mobiles"] = list(cfg.at_mobiles)
            result["at_user_ids"] = list(cfg.at_user_ids)
        if "other" in scope:
            result["poll_interval_seconds"] = cfg.poll_interval_seconds
        return result

    def import_config(self, data: dict, scope: list[str]):
        """按 scope 导入配置子集"""
        with self._lock:
            cfg = self.store.get()
            if "stocks" in scope and "stocks" in data:
                cfg.stocks = [StockConfig.from_dict(s) for s in data["stocks"]]
            if "funds" in scope and "funds" in data:
                cfg.funds = [FundConfig.from_dict(f) for f in data["funds"]]
            if "templates" in scope and "disguise_templates" in data:
                cfg.disguise_templates = data["disguise_templates"]
            if "webhook" in scope:
                if "dingding_webhook" in data:
                    cfg.dingding_webhook = data["dingding_webhook"]
                if "at_mobiles" in data:
                    cfg.at_mobiles = list(data["at_mobiles"])
                if "at_user_ids" in data:
                    cfg.at_user_ids = list(data["at_user_ids"])
            if "other" in scope and "poll_interval_seconds" in data:
                cfg.poll_interval_seconds = int(data["poll_interval_seconds"])
            self.store.save(cfg)
        self._apply_runtime_changes()

    def get_config(self) -> Config:
        return self.store.get()

    def status(self) -> dict:
        result = {
            **self.stats,
            "running": self._running,
            "poll_interval_seconds": self.store.get().poll_interval_seconds,
            "stocks": [s.to_dict() for s in self.store.get().stocks],
            "funds": [f.to_dict() for f in self.store.get().funds],
            "config_path": str(self.store.path),
            "price_latency": self.monitor._price_latency if self.monitor else None,
        }
        if self.monitor is not None:
            triggered = self.monitor.t_events_triggered
            for sd in result["stocks"]:
                for ev in sd.get("t_events", []):
                    code = sd.get("code")
                    ev["triggered"] = code in triggered and ev["id"] in triggered[code]
        return result

    def test_notify(self, message: str = "") -> bool:
        """发一条测试消息到 webhook（空字符串用默认文案）"""
        msg = (message or "").strip() or "🧪 stock-monitor 测试消息 - 来自 Web UI"
        with self._lock:
            if self.monitor is None:
                return False
            self.monitor.send_dingding_notification(msg)
            return True

    def get_quotes(self) -> dict:
        """返回每只股票最新报价 + 当日涨跌幅 + 当前涨速（用 monitor 内存中的 price_history，不发 HTTP）"""
        empty = {"price": None, "change_percent": None, "as_of": None}
        cfg = self.store.get()
        with self._lock:
            if self.monitor is None:
                return {s.code: dict(empty) for s in cfg.stocks}
            result = {}
            for stock in cfg.stocks:
                history = self.monitor.price_history.get(stock.code, [])
                if not history:
                    result[stock.code] = dict(empty)
                    continue
                latest = history[-1]
                yesterday_close = self.monitor.yesterday_close.get(stock.code, 0.0)
                if yesterday_close > 0:
                    change = (latest["price"] - yesterday_close) / yesterday_close * 100
                    change = round(change, 2)
                else:
                    change = None
                quote = {
                    "price": latest["price"],
                    "change_percent": change,
                    "as_of": int(latest["time"].timestamp()),
                }
                # 涨跌停状态 + 封单（来自 monitor 行情快照）
                qk = self.monitor._latest_quote.get(stock.code)
                stt = self.monitor._limit_state.get(stock.code)
                if qk and stt:
                    up_p, down_p, _ratio = self.monitor._compute_limit_prices(
                        stock.code, qk.get('name', ''), qk.get('prev_close', 0.0))
                    if up_p is not None:
                        bid1_price = qk.get('bid1_price', 0.0)
                        ask1_price = qk.get('ask1_price', 0.0)
                        bid1_vol = qk.get('bid1_vol', 0.0)
                        ask1_vol = qk.get('ask1_vol', 0.0)
                        is_up = abs(latest["price"] - up_p) < 0.005 and (ask1_price == 0 or ask1_vol == 0)
                        is_down = abs(latest["price"] - down_p) < 0.005 and (bid1_price == 0 or bid1_vol == 0)
                        if is_up:
                            seal_vol = bid1_vol
                            quote["limit_status"] = "limit_up"
                            quote["limit_price"] = up_p
                        elif is_down:
                            seal_vol = ask1_vol
                            quote["limit_status"] = "limit_down"
                            quote["limit_price"] = down_p
                        else:
                            seal_vol = 0
                            quote["limit_status"] = "normal"
                        if seal_vol > 0:
                            quote["sealed_lots"] = int(round(seal_vol / 100.0))
                            quote["sealed_amount"] = round(seal_vol * quote.get("limit_price", 0) / 10000.0, 2)
                        else:
                            quote["sealed_lots"] = 0
                            quote["sealed_amount"] = 0.0
                    else:
                        quote["limit_status"] = "unknown"
                        quote["sealed_lots"] = 0
                        quote["sealed_amount"] = 0.0
                else:
                    quote["limit_status"] = "unknown"
                    quote["sealed_lots"] = 0
                    quote["sealed_amount"] = 0.0
                # 当前涨速：speed_window 内最早一笔到当前价的涨跌幅
                speed_window = getattr(stock, 'speed_window', 5)
                period_start = datetime.now() - timedelta(minutes=speed_window)
                prices_in_window = [p for p in history if p['time'] >= period_start]
                if len(prices_in_window) >= 2:
                    base = prices_in_window[0]
                    base_price = base["price"]
                    if base_price > 0:
                        surge = (latest["price"] - base_price) / base_price * 100
                        quote["surge_change"] = round(surge, 2)
                        quote["surge_base_price"] = base_price
                        quote["surge_base_time"] = int(base["time"].timestamp())
                result[stock.code] = quote
            return result

    def get_fund_quotes(self) -> dict:
        """返回每只基金估算净值 + 当日涨跌幅（无缓存时实时获取）"""
        empty = {"nav": None, "estimated_nav": None, "change_percent": None, "as_of": None}
        cfg = self.store.get()
        with self._lock:
            if self.monitor is None:
                return {f.code: dict(empty) for f in cfg.funds}
            stale_codes = [f.code for f in cfg.funds if f.enabled and not self.monitor.price_history.get(f.code)]
            if stale_codes:
                self.monitor.fetch_fund_prices(stale_codes)
            result = {}
            for fund in cfg.funds:
                history = self.monitor.price_history.get(fund.code, [])
                last_nav = self.monitor.yesterday_close.get(fund.code, 0.0)
                if not history:
                    result[fund.code] = dict(empty)
                    continue
                latest = history[-1]
                change = None
                if last_nav > 0:
                    change = (latest["price"] - last_nav) / last_nav * 100
                    change = round(change, 2)
                result[fund.code] = {
                    "nav": last_nav if last_nav > 0 else None,
                    "estimated_nav": latest["price"],
                    "change_percent": change,
                    "as_of": int(latest["time"].timestamp()),
                }
            return result

    # ===== 做T事件 =====

    def add_t_event(self, code: str, event_type: str, price: float, target_price: Optional[float] = None) -> dict:
        """添加做T事件，持久化到 config + 同步到 monitor 运行时"""
        import uuid
        event = {
            "id": uuid.uuid4().hex[:12],
            "type": event_type,
            "price": price,
            "target_price": target_price,
            "created_at": int(time.time()),
        }
        with self._lock:
            # 写 config
            def mut(cfg: Config):
                stock = cfg.find_stock(code)
                if stock is not None:
                    stock.t_events.append(event)
            self.store.update(mut)
            # 同步 monitor 运行时
            if self.monitor is not None and code in self.monitor.t_events:
                self.monitor.t_events[code].append(event)
        return event

    def update_t_event(self, code: str, event_id: str, price: Optional[float] = None, target_price: Optional[float] = None) -> Optional[dict]:
        """更新做T事件，持久化到 config + 同步到 monitor 运行时"""
        with self._lock:
            updated_event: Optional[dict] = None

            def mut(cfg: Config):
                nonlocal updated_event
                stock = cfg.find_stock(code)
                if stock is None:
                    return
                for ev in stock.t_events:
                    if ev.get("id") == event_id:
                        ev["price"] = price
                        ev["target_price"] = target_price
                        updated_event = dict(ev)
                        break

            self.store.update(mut)

            if updated_event is None:
                return None

            # 同步 monitor 运行时
            if self.monitor is not None and code in self.monitor.t_events:
                for ev in self.monitor.t_events[code]:
                    if ev.get("id") == event_id:
                        if price is not None:
                            ev["price"] = price
                        if target_price is not None:
                            ev["target_price"] = target_price
                        if target_price is None and "target_price" not in ev:
                            ev["target_price"] = None
                        break

        return updated_event

    def remove_t_event(self, code: str, event_id: str) -> bool:
        """删除做T事件"""
        with self._lock:
            cfg = self.store.get()
            stock = cfg.find_stock(code)
            if stock is None:
                return False
            old_len = len(stock.t_events)
            stock.t_events = [e for e in stock.t_events if e.get("id") != event_id]
            if len(stock.t_events) == old_len:
                return False
            self.store.save(cfg)
            # 同步 monitor 运行时
            if self.monitor is not None and code in self.monitor.t_events:
                self.monitor.t_events[code] = [
                    e for e in self.monitor.t_events[code] if e.get("id") != event_id
                ]
        return True

    def reset_t_event(self, code: str, event_id: str) -> bool:
        """重置已触发的 T 事件，使其当日可再次触发"""
        with self._lock:
            if self.monitor is None:
                return False
            if code in self.monitor.t_events_triggered:
                self.monitor.t_events_triggered[code].discard(event_id)
            if code in self.monitor.t_events:
                if not any(ev.get("id") == event_id for ev in self.monitor.t_events[code]):
                    cfg = self.store.get()
                    stock = cfg.find_stock(code)
                    if stock:
                        for ev in stock.t_events:
                            if ev.get("id") == event_id:
                                self.monitor.t_events[code].append(dict(ev))
                                return True
        return True

    # ===== 内部 =====

    def _build_monitor(self, cfg: Config) -> Optional[StockMonitor]:
        if not cfg.dingding_webhook:
            logger.warning("dingding_webhook 未配置，监控启动后无法发送通知")
        m = StockMonitor(cfg.dingding_webhook, at_mobiles=cfg.at_mobiles, at_user_ids=cfg.at_user_ids)
        for s in cfg.stocks:
            if not s.enabled:
                continue
            m.add_stock(s.code, self._stock_to_dict(s))
        for f in cfg.funds:
            if not f.enabled:
                continue
            m.add_fund(f.code, self._fund_to_dict(f))
        # 覆盖默认模板
        if cfg.disguise_templates:
            m.disguise_templates = {
                k: list(v) for k, v in cfg.disguise_templates.items()
            }
        return m

    def _stock_to_dict(self, s: StockConfig) -> dict:
        """转成 StockMonitor.add_stock 接受的 dict 格式"""
        return {
            "name": s.name,
            "nickname": s.nickname,
            "price_high": s.price_high,
            "price_low": s.price_low,
            "speed_threshold": s.speed_threshold,
            "speed_window": s.speed_window,
            "cooldown_minutes": s.cooldown_minutes,
            "daily_change_up": list(s.daily_change_up),
            "daily_change_down": list(s.daily_change_down),
            "retracement_threshold": s.retracement_threshold,
            "bounce_threshold": s.bounce_threshold,
            "limit_seal_min_lots": s.limit_seal_min_lots,
            "t_threshold": s.t_threshold,
            "t_events": list(s.t_events),
            "t_s_enabled": s.t_s_enabled,
            "t_b_enabled": s.t_b_enabled,
            "disabled_alerts": list(s.disabled_alerts),
        }

    def _fund_to_dict(self, f: FundConfig) -> dict:
        return {
            "name": f.name,
            "nickname": f.nickname,
            "cooldown_minutes": f.cooldown_minutes,
            "daily_change_up": list(f.daily_change_up),
            "daily_change_down": list(f.daily_change_down),
            "retracement_threshold": f.retracement_threshold,
            "bounce_threshold": f.bounce_threshold,
            "disabled_alerts": list(f.disabled_alerts),
        }

    def _apply_stock_change(self, code: str):
        """单只股票 add/update 后：找到并 rebuild 该只"""
        with self._lock:
            if self.monitor is None:
                return
            cfg = self.store.get()
            target = cfg.find_stock(code)
            self._remove_stock_runtime(code)
            if target is not None and target.enabled:
                self.monitor.add_stock(code, self._stock_to_dict(target))

    def _remove_stock_runtime(self, code: str):
        with self._lock:
            if self.monitor is None:
                return
            self.monitor.stocks.pop(code, None)
            self.monitor.price_history.pop(code, None)
            self.monitor.notification_cooldown.pop(code, None)
            self.monitor.price_alert_status.pop(code, None)
            self.monitor.yesterday_close.pop(code, None)
            self.monitor.price_high_alerted_abs.pop(code, None)
            self.monitor.price_low_alerted_abs.pop(code, None)
            self.monitor.price_high_alerted_daily.pop(code, None)
            self.monitor.price_low_alerted_daily.pop(code, None)
            self.monitor.peak_since_high_alert.pop(code, None)
            self.monitor.valley_since_low_alert.pop(code, None)
            self.monitor.t_events.pop(code, None)
            self.monitor._latest_quote.pop(code, None)
            self.monitor._limit_state.pop(code, None)
            self.monitor._seal_history.pop(code, None)
            self.monitor._low_seal_fired.pop(code, None)
            self.monitor._exhaust_fired.pop(code, None)

    def _apply_fund_change(self, code: str):
        with self._lock:
            if self.monitor is None:
                return
            cfg = self.store.get()
            target = cfg.find_fund(code)
            self._remove_fund_runtime(code)
            if target is not None and target.enabled:
                self.monitor.add_fund(code, self._fund_to_dict(target))

    def _remove_fund_runtime(self, code: str):
        with self._lock:
            if self.monitor is None:
                return
            self.monitor.stocks.pop(code, None)
            self.monitor.price_history.pop(code, None)
            self.monitor.notification_cooldown.pop(code, None)
            self.monitor.price_alert_status.pop(code, None)
            self.monitor.yesterday_close.pop(code, None)
            self.monitor.price_high_alerted_abs.pop(code, None)
            self.monitor.price_low_alerted_abs.pop(code, None)
            self.monitor.price_high_alerted_daily.pop(code, None)
            self.monitor.price_low_alerted_daily.pop(code, None)
            self.monitor.peak_since_high_alert.pop(code, None)
            self.monitor.valley_since_low_alert.pop(code, None)
            self.monitor.t_events.pop(code, None)

    def _apply_runtime_changes(self):
        """webhook/templates/轮询间隔变化时更新 monitor 字段（不重建实例）"""
        with self._lock:
            if self.monitor is None:
                return
            cfg = self.store.get()
            self.monitor.dingding_webhook = cfg.dingding_webhook
            self.monitor.at_mobiles = list(cfg.at_mobiles)
            self.monitor.at_user_ids = list(cfg.at_user_ids)
            self.interval_seconds = cfg.poll_interval_seconds
            self.monitor._limit_exhaust_seconds = cfg.limit_seal_exhaust_seconds
            self.monitor._limit_exhaust_samples = cfg.limit_seal_exhaust_samples
            if cfg.disguise_templates:
                self.monitor.disguise_templates = {
                    k: list(v) for k, v in cfg.disguise_templates.items()
                }
            enabled_stock_codes = {s.code for s in cfg.stocks if s.enabled}
            for code in list(self.monitor.stocks.keys()):
                if code not in enabled_stock_codes and cfg.find_fund(code) is None:
                    self._remove_stock_runtime(code)
            for s in cfg.stocks:
                if s.enabled and s.code not in self.monitor.stocks:
                    self.monitor.add_stock(s.code, self._stock_to_dict(s))
            enabled_fund_codes = {f.code for f in cfg.funds if f.enabled}
            for code in list(self.monitor.stocks.keys()):
                if code not in enabled_fund_codes and cfg.find_stock(code) is None:
                    self._remove_fund_runtime(code)
            for f in cfg.funds:
                if f.enabled and f.code not in self.monitor.stocks:
                    self.monitor.add_fund(f.code, self._fund_to_dict(f))

    def _loop(self):
        """后台循环：每轮检查所有股票，同一轮次多条通知合为一条发送，避免钉钉限流"""
        logger.info("监控循环启动")
        while self._running:
            try:
                sleep_seconds = StockMonitor._seconds_until_next_check(self.interval_seconds)

                if sleep_seconds == self.interval_seconds and self.monitor is not None and self.monitor.dingding_webhook:
                    cfg = self.store.get()

                    # 每日状态重置（开盘后首次检查时执行）
                    today = datetime.now().date()
                    if self.monitor._reset_date is None or today != self.monitor._reset_date:
                        t_events_map = {s.code: list(s.t_events) for s in cfg.stocks if s.enabled}
                        self.monitor.daily_reset(t_events_map)
                        self.monitor._reset_date = today
                        logger.info(f"每日监控状态已重置 ({today})")

                    self._sync_enabled(cfg)
                    self.monitor._batch_mode = True
                    self.monitor._alert_buffer.clear()
                    codes = [s.code for s in cfg.stocks if s.enabled]
                    if codes:
                        prices = self.monitor.fetch_batch_prices(codes)
                        for code in codes:
                            if code not in prices:
                                continue
                            try:
                                self.monitor.check_stock_alerts(code, override_price=prices[code])
                            except Exception as e:
                                logger.error(f"检查 {code} 时异常: {e}")
                                self.stats["last_error"] = str(e)
                    fund_codes = [f.code for f in cfg.funds if f.enabled]
                    if fund_codes:
                        fund_prices = self.monitor.fetch_fund_prices(fund_codes)
                        for code in fund_codes:
                            if code not in fund_prices:
                                continue
                            try:
                                self.monitor.check_stock_alerts(code, override_price=fund_prices[code])
                            except Exception as e:
                                logger.error(f"检查基金 {code} 时异常: {e}")
                                self.stats["last_error"] = str(e)
                    self.monitor._batch_mode = False
                    n_alerts = len(self.monitor._alert_buffer)
                    self.monitor.flush_alerts()
                    if n_alerts > 0:
                        self.stats["alert_count"] += n_alerts
                        self.stats["last_alert_at"] = int(time.time())
                    self.stats["last_check_at"] = int(time.time())
                    self.stats["check_count"] += 1

                for _ in range(int(sleep_seconds)):
                    if not self._running:
                        break
                    time.sleep(1)
            except Exception as e:
                logger.error(f"监控循环异常: {e}", exc_info=True)
                self.stats["last_error"] = str(e)
        logger.info("监控循环退出")

    def _sync_enabled(self, cfg: Config):
        """与 cfg 对齐 stocks / funds 字典（启用/禁用）"""
        if self.monitor is None:
            return
        enabled_codes = {s.code for s in cfg.stocks if s.enabled}
        enabled_codes |= {f.code for f in cfg.funds if f.enabled}
        for code in list(self.monitor.stocks.keys()):
            if code not in enabled_codes:
                self._remove_stock_runtime(code)
        for s in cfg.stocks:
            if s.enabled and s.code not in self.monitor.stocks:
                self.monitor.add_stock(s.code, self._stock_to_dict(s))
        for f in cfg.funds:
            if f.enabled and f.code not in self.monitor.stocks:
                self.monitor.add_fund(f.code, self._fund_to_dict(f))
