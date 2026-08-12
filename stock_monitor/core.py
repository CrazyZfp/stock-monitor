#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股票监控助手 - 智能监控A股股票价格变化
为了避免同事发现，通知内容做了自然语言伪装
"""

import os
import requests
import time
import json
import hashlib
from datetime import datetime, timedelta, date
import threading
import logging
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import cn_stock_holidays.data as shsz

logger = logging.getLogger(__name__)

class StockMonitor:
    def __init__(self, dingding_webhook: str, at_mobiles: list[str] | None = None, at_user_ids: list[str] | None = None,
                 *, dingding_keyword: str = "",
                 notify_mode: str = "single",
                 notify_channels: dict[str, bool] | None = None,
                 notify_priority: list[str] | None = None,
                 email_smtp_host: str = "", email_smtp_port: int = 465,
                 email_username: str = "", email_password: str = "",
                 email_from_addr: str = "", email_to_addrs: list[str] | None = None,
                 email_use_ssl: bool = True):
        """
        初始化股票监控器

        Args:
            dingding_webhook: 钉钉群机器人的Webhook地址
            dingding_keyword: 非空时，钉钉每条通知开头拼接该关键词
            at_mobiles: 通知时 @ 的手机号列表
            at_user_ids: 通知时 @ 的用户 ID 列表
            notify_mode: "multi"(全发) | "single"(优先级回退)
            notify_channels: 通道开关 {"dingding": bool, "email": bool}
            notify_priority: 优先级顺序（single 模式按此回退）
            email_*: SMTP 邮箱配置
        """
        self.notify_mode = notify_mode if notify_mode in ("multi", "single") else "single"
        self.notify_channels = dict(notify_channels or {"dingding": True, "email": False})
        self.notify_priority = list(notify_priority or ["dingding", "email"])
        self.dingding_webhook = dingding_webhook
        self.dingding_keyword = dingding_keyword
        # 邮箱配置
        self.email_smtp_host = email_smtp_host
        self.email_smtp_port = email_smtp_port
        self.email_username = email_username
        self.email_password = email_password
        self.email_from_addr = email_from_addr or email_username
        self.email_to_addrs = list(email_to_addrs or [])
        self.email_use_ssl = email_use_ssl
        if not any(self.channel_ready(ch) for ch in self.notify_priority):
            logger.warning("无可用通知通道：所有通道未开启或未完整配置，发送通知将失败。")
        self.at_mobiles = list(at_mobiles or [])
        self.at_user_ids = list(at_user_ids or [])
        self.stocks = {}  # 监控的股票配置
        self.price_history = {}  # 价格历史记录
        self.notification_cooldown = {}  # 通知冷却时间
        self.price_alert_status = {}  # 记录价格告警状态（用于反转检测）
        self.running = True
        self._holiday_lib_available = True  # cn-stock-holidays 是否可用
        self._alert_buffer: list[str] = []  # 批量聚合通知（同一轮检查的多条消息合为一条发送）
        self._batch_mode: bool = False

        # 昨收价（用于当日涨跌百分比）
        self.yesterday_close: dict[str, float] = {}
        # 绝对价格告警状态（单档）
        self.price_high_alerted_abs: dict[str, set[int]] = {}
        self.price_low_alerted_abs: dict[str, set[int]] = {}
        # 当日涨跌百分比告警状态（多档）
        self.price_high_alerted_daily: dict[str, set[int]] = {}
        self.price_low_alerted_daily: dict[str, set[int]] = {}
        # 回撤 / 反弹跟踪（合并，无论从哪条途径触发）
        self.peak_since_high_alert: dict[str, float] = {}
        self.valley_since_low_alert: dict[str, float] = {}
        # 回撤 / 反弹触发许可标志：daily_up 触发时设 True，retracement 触发后设 False
        self.retracement_armed: dict[str, bool] = {}
        self.bounce_armed: dict[str, bool] = {}
        # 盈亏告警状态：跨过持仓成本线时通知一次（True=已通知该侧）
        self._profit_alerted: dict[str, bool] = {}
        self._loss_alerted: dict[str, bool] = {}
        # 做T事件（运行时 + 持久化存于 config.json）
        self.t_events: dict[str, list[dict]] = {}
        # 当日已触发的 T 事件 ID 集合
        self.t_events_triggered: dict[str, set[str]] = {}
        # 最近一次状态重置日期
        self._reset_date: Optional[date] = None

        # 价格数据延迟（API 时间戳与本地时间之差，秒）
        self._price_latency: Optional[float] = None

        # 涨跌停封单告警运行时状态
        self._latest_quote: dict[str, dict] = {}        # 最新行情快照（含买卖盘）
        self._limit_state: dict[str, dict] = {}          # {is_limit_up, is_limit_down, _init}
        self._seal_history: dict[str, list] = {}         # 封板期间 (ts, seal_vol_股) 历史
        self._low_seal_fired: dict[str, bool] = {}
        self._exhaust_fired: dict[str, bool] = {}
        # 全局参数（由 manager._apply_runtime_changes 下发）
        self._limit_exhaust_seconds: int = 30
        self._limit_exhaust_samples: int = 3

        # 合约品种集合（add_crypto 注册，用于 monitor_loop 区分 24/7 轮询）
        self._crypto_codes: set[str] = set()
        # 基金代码集合（add_fund 注册，用于 _market_of 区分 stock/fund）
        self._fund_codes: set[str] = set()
        # 币安合约 symbol -> pricePrecision 缓存（启动时拉取 exchangeInfo）
        self._crypto_precision: dict[str, int] = {}

        # 伪装消息模板（看起来像普通聊天）
        self.disguise_templates = {
            'price_high': [
                "🟢 {name}"
            ],
            'price_low': [
                "🔴 {name}"
            ],
            'daily_up': [
                "📈 {name} 当日涨幅达{tier_index}档 {tier_threshold}（{daily_change}）",
            ],
            'daily_down': [
                "📉 {name} 当日跌幅达{tier_index}档 {tier_threshold}（{daily_change}）",
            ],
            'surge_up': [
                "⏫️ {name},{speed_change}({time})",
            ],
            'surge_down': [
                "⏬️ {name},{speed_change}({time})"
            ],
            'retracement': [
                "🔻 {name} 回撤 {retracement}（峰值 {peak_price}，当前 {price}）"
            ],
            'bounce': [
                "🟢 {name} 反弹 {bounce}（谷值 {valley_price}，当前 {price}）"
            ],
            'profit': [
                "🟢 {name} 盈利 {profit_pct}（成本 {position_cost}，当前 {price}）"
            ],
            'loss': [
                "🔴 {name} 亏损 {profit_pct}（成本 {position_cost}，当前 {price}）"
            ],
            't_sell': [
                "🔻 {name} 做T可买回：{t_price}→{price}（跌{t_threshold}%）{t_quantity}"
            ],
            't_buy': [
                "🟢 {name} 做T可卖出：{t_price}→{price}（涨{t_threshold}%）{t_quantity}"
            ],
            'limit_up': [
                "🔴 {name} 涨停 封单{sealed_lots}手 {sealed_amount}万元"
            ],
            'limit_up_broken': [
                "🟡 {name} 涨停开板 现{price}"
            ],
            'limit_up_low_seal': [
                "⚠️ {name} 涨停封单不足{seal_min_lots}手 现{sealed_lots}手"
            ],
            'limit_up_exhaust': [
                "⚠️ {name} 涨停封单将尽 预计{seal_eta_seconds}秒耗尽"
            ],
            'limit_down': [
                "🟢 {name} 跌停 封单{sealed_lots}手 {sealed_amount}万元"
            ],
            'limit_down_broken': [
                "🟡 {name} 跌停开板 现{price}"
            ],
            'limit_down_low_seal': [
                "⚠️ {name} 跌停封单不足{seal_min_lots}手 现{sealed_lots}手"
            ],
            'limit_down_exhaust': [
                "⚠️ {name} 跌停封单将尽 预计{seal_eta_seconds}秒耗尽"
            ],
        }
        # 市场覆盖模板：{"stock": {alert_type: [...]}, "fund": {...}, "crypto": {...}}
        self.market_templates: dict[str, dict[str, list[str]]] = {"stock": {}, "fund": {}, "crypto": {}}
    
    def add_stock(self, stock_code: str, config: Dict):
        """
        添加要监控的股票
        
        Args:
            stock_code: 股票代码，如 'sh600000'（沪市）或 'sz000001'（深市）
            config: 监控配置，包含：
                - name: 股票名称
                - price_high: 绝对价格高价阈值
                - price_low: 绝对价格低价阈值
                - speed_threshold: 涨速阈值（百分比，监控窗口内）
                - speed_window: 涨速窗口（分钟）
                - cooldown_minutes: 同类通知冷却时间（分钟）
        """
        config.setdefault('_market', 'stock')
        self.stocks[stock_code] = config
        self.price_history[stock_code] = []
        self.notification_cooldown[stock_code] = {
            'price_high': None,
            'price_low': None,
            'daily_up': None,
            'daily_down': None,
            'surge_up': None,
            'surge_down': None,
            'retracement': None,
            'bounce': None,
            't_sell': None,
            't_buy': None,
            'profit': None,
            'loss': None,
        }
        self.price_alert_status[stock_code] = {
            'above_high': False,
            'below_low': False,
            '_high_init': False,
            '_low_init': False,

        }
        self.yesterday_close[stock_code] = 0.0
        self.price_high_alerted_abs[stock_code] = set()
        self.price_low_alerted_abs[stock_code] = set()
        self.price_high_alerted_daily[stock_code] = set()
        self.price_low_alerted_daily[stock_code] = set()
        self.peak_since_high_alert[stock_code] = 0.0
        self.valley_since_low_alert[stock_code] = float('inf')
        # 盈亏告警状态
        self._profit_alerted[stock_code] = False
        self._loss_alerted[stock_code] = False
        # 涨跌停封单告警状态
        self._latest_quote[stock_code] = {}
        self._limit_state[stock_code] = {'is_limit_up': False, 'is_limit_down': False, '_init': False}
        self._seal_history[stock_code] = []
        self._low_seal_fired[stock_code] = False
        self._exhaust_fired[stock_code] = False
        self.t_events[stock_code] = list(config.get('t_events', []))
        logger.info(f"添加监控股票: {config['name']} ({stock_code})")

    def add_fund(self, fund_code: str, config: Dict):
        """添加基金到监控"""
        config.setdefault('_market', 'fund')
        self.stocks[fund_code] = config
        self.price_history[fund_code] = []
        self.notification_cooldown[fund_code] = {
            'daily_up': None,
            'daily_down': None,
            'retracement': None,
            'bounce': None,
            'profit': None,
            'loss': None,
        }
        self.price_alert_status[fund_code] = {}
        self.yesterday_close[fund_code] = 0.0
        self.price_high_alerted_abs[fund_code] = set()
        self.price_low_alerted_abs[fund_code] = set()
        self.price_high_alerted_daily[fund_code] = set()
        self.price_low_alerted_daily[fund_code] = set()
        self.peak_since_high_alert[fund_code] = 0.0
        self.valley_since_low_alert[fund_code] = float('inf')
        self.retracement_armed[fund_code] = False
        self.bounce_armed[fund_code] = False
        self._profit_alerted[fund_code] = False
        self._loss_alerted[fund_code] = False
        self.t_events[fund_code] = []
        self._fund_codes.add(fund_code)
        logger.info(f"添加监控基金: {config['name']} ({fund_code})")

    def add_crypto(self, crypto_code: str, config: Dict):
        """添加币安合约到监控（复用告警框架，但不启用涨跌停封单告警）"""
        config.setdefault('_market', 'crypto')
        self.stocks[crypto_code] = config
        self.price_history[crypto_code] = []
        self.notification_cooldown[crypto_code] = {
            'price_high': None,
            'price_low': None,
            't_sell': None,
            't_buy': None,
            'profit': None,
            'loss': None,
        }
        self.price_alert_status[crypto_code] = {
            'above_high': False,
            'below_low': False,
            '_high_init': False,
            '_low_init': False,
        }
        self.yesterday_close[crypto_code] = 0.0
        self.price_high_alerted_abs[crypto_code] = set()
        self.price_low_alerted_abs[crypto_code] = set()
        self.price_high_alerted_daily[crypto_code] = set()
        self.price_low_alerted_daily[crypto_code] = set()
        self.peak_since_high_alert[crypto_code] = 0.0
        self.valley_since_low_alert[crypto_code] = float('inf')
        self.retracement_armed[crypto_code] = False
        self.bounce_armed[crypto_code] = False
        self._profit_alerted[crypto_code] = False
        self._loss_alerted[crypto_code] = False
        self.t_events[crypto_code] = list(config.get('t_events', []))
        # 合约无涨跌停：不初始化 _limit_state/_seal_history，check_limit_status 会自动 return 跳过
        self._crypto_codes.add(crypto_code)
        # 价格精度默认 2，fetch_crypto_prices 时从 exchangeInfo 更新
        config.setdefault('price_precision', 2)
        logger.info(f"添加监控合约: {config['name']} ({crypto_code})")

    # 天天基金 FundValuationLast 新估值接口（2026-07 fundgz JSONP 停用后替代）
    FUND_VALUATION_HOSTS = [
        "https://fundcomapi.tiantianfunds.com",
        "https://fundcomapi.eastmoney.com",
    ]
    FUND_VALUATION_PATH = "/mm/newCore/FundValuationLast"
    FUND_VALUATION_FIELDS = "FCODE,SHORTNAME,GSZZL,GZTIME,GSZ,NAV,PDATE"

    def fetch_fund_prices(self, fund_codes: list[str]) -> dict[str, float]:
        """批量获取基金估算净值（天天基金 FundValuationLast 接口）

        旧接口 fundgz.1234567.com.cn/js/{code}.js 已于 2026-07 停用（301→404），
        改用官方 H5 同款 FundValuationLast JSON 接口，支持批量请求。
        部分主动管理型基金 GSZ 可能为 null，此时仅更新上一净值、不写入估值历史。
        """
        if not fund_codes:
            return {}
        import requests
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://fund.eastmoney.com/',
        }
        params = {
            'FCODES': ','.join(fund_codes),
            'FIELDS': self.FUND_VALUATION_FIELDS,
        }

        data = None
        for host in self.FUND_VALUATION_HOSTS:
            url = host + self.FUND_VALUATION_PATH
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=10)
                if resp.status_code != 200:
                    logger.error(f"基金估值接口 {host} 失败: HTTP {resp.status_code}")
                    continue
                payload = resp.json()
                if isinstance(payload, dict) and payload.get("success") and isinstance(payload.get("data"), list):
                    data = payload["data"]
                    break
                logger.error(f"基金估值接口 {host} 返回异常: {str(payload)[:200]}")
            except Exception as e:
                logger.error(f"基金估值接口 {host} 异常: {e}")
                continue

        if data is None:
            return {}

        results = {}
        now = datetime.now()
        cutoff = now - timedelta(hours=24)
        for item in data:
            code = item.get("FCODE")
            if not code or code not in fund_codes:
                continue
            nav = item.get("NAV")
            try:
                last_nav = float(nav) if nav is not None else 0.0
            except (ValueError, TypeError):
                last_nav = 0.0
            if last_nav > 0:
                self.yesterday_close[code] = last_nav
            gsz = item.get("GSZ")
            try:
                gsz = float(gsz) if gsz is not None else None
            except (ValueError, TypeError):
                gsz = None
            if gsz is not None and gsz > 0:
                self.price_history.setdefault(code, []).append({
                    'time': now,
                    'price': gsz,
                })
                self.price_history[code] = [p for p in self.price_history[code] if p['time'] > cutoff]
                results[code] = gsz
        return results

    # 币安合约（USDⓈ-M 永续 fapi / COIN-M 永续 dapi）公开行情接口
    CRYPTO_HOSTS = {
        "fapi": "https://fapi.binance.com",
        "dapi": "https://dapi.binance.com",
    }

    def fetch_crypto_exchange_info(self):
        """拉取 fapi + dapi exchangeInfo，缓存永续合约 symbol -> pricePrecision"""
        import requests
        for prefix, host in self.CRYPTO_HOSTS.items():
            url = f"{host}/{prefix}/v1/exchangeInfo"
            try:
                resp = requests.get(url, timeout=15)
                if resp.status_code != 200:
                    logger.error(f"合约 exchangeInfo {host} 失败: HTTP {resp.status_code}")
                    continue
                data = resp.json()
                for sym in data.get("symbols", []):
                    # 仅永续：fapi 默认永续；dapi 需 contractType==PERPETUAL
                    if prefix == "dapi" and sym.get("contractType") != "PERPETUAL":
                        continue
                    symbol = sym.get("symbol")
                    precision = sym.get("pricePrecision")
                    if symbol and precision is not None:
                        self._crypto_precision[f"{prefix}:{symbol}"] = int(precision)
            except Exception as e:
                logger.error(f"合约 exchangeInfo {host} 异常: {e}")
        logger.info(f"合约精度缓存已加载: {len(self._crypto_precision)} 个 symbol")

    def fetch_crypto_prices(self, crypto_codes: list[str]) -> dict[str, float]:
        """批量获取币安合约最新价（按 fapi:/dapi: 前缀分组请求 /v1/ticker/24hr）"""
        if not crypto_codes:
            return {}
        import requests
        # 首次调用时懒加载精度缓存
        if not self._crypto_precision:
            self.fetch_crypto_exchange_info()

        headers = {"User-Agent": "Mozilla/5.0"}
        now = datetime.now()
        cutoff = now - timedelta(hours=1)
        results: dict[str, float] = {}

        # 按网关前缀分组
        groups: dict[str, list[str]] = {}
        for code in crypto_codes:
            prefix, _, symbol = code.partition(":")
            if prefix not in self.CRYPTO_HOSTS:
                logger.warning(f"未知合约前缀: {code}")
                continue
            groups.setdefault(prefix, []).append(symbol)

        for prefix, symbols in groups.items():
            host = self.CRYPTO_HOSTS[prefix]
            # 逐 symbol 请求（24hr 端点带 symbol 参数权重=1，无参数返回全部权重高）
            for symbol in symbols:
                url = f"{host}/{prefix}/v1/ticker/24hr?symbol={symbol}"
                try:
                    resp = requests.get(url, headers=headers, timeout=10)
                    if resp.status_code != 200:
                        logger.error(f"合约行情 {symbol} 失败: HTTP {resp.status_code}")
                        continue
                    data = resp.json()
                    # fapi 返回 dict；dapi 返回 list（单 symbol 取 [0]）
                    item = data[0] if isinstance(data, list) else data
                    last_price = float(item.get("lastPrice", 0))
                    if last_price <= 0:
                        continue
                    prev_close = 0.0
                    try:
                        prev_close = float(item.get("prevClosePrice", 0))
                    except (ValueError, TypeError):
                        pass
                    code = f"{prefix}:{symbol}"
                    if prev_close > 0:
                        self.yesterday_close[code] = prev_close
                    # 更新运行时价格精度（用于 generate_disguise_message 格式化）
                    precision = self._crypto_precision.get(code)
                    if precision is not None and code in self.stocks:
                        self.stocks[code]['price_precision'] = precision
                    self.price_history.setdefault(code, []).append({
                        'time': now,
                        'price': last_price,
                    })
                    self.price_history[code] = [p for p in self.price_history[code] if p['time'] > cutoff]
                    results[code] = last_price
                except Exception as e:
                    logger.error(f"合约行情 {symbol} 异常: {e}")
        return results

    def get_stock_price(self, stock_code: str) -> Optional[float]:
        """
        获取股票实时价格（使用公开API）
        
        注意：这里使用新浪财经的公开接口，实际使用时可能需要更换
        """
        try:
            # 新浪财经API（示例，可能需要调整）
            url = f"http://hq.sinajs.cn/list={stock_code}"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': 'https://finance.sina.com.cn'
            }
            logger.info(f"获取 {stock_code} 信息")   
            response = requests.get(url, headers=headers, timeout=10)
            response.encoding = 'gbk'
            
            if response.status_code == 200:
                data = response.text
                # 解析数据格式
                if 'var hq_str_' in data:
                    parts = data.split('"')[1].split(',')
                    if len(parts) > 3:
                        current_price = float(parts[3])  # 当前价格
                        try:
                            self.yesterday_close[stock_code] = float(parts[2])  # 昨收
                        except (ValueError, IndexError):
                            self.yesterday_close[stock_code] = 0.0
                        
                        # 记录价格历史
                        timestamp = datetime.now()
                        self.price_history[stock_code].append({
                            'time': timestamp,
                            'price': current_price
                        })
                        
                        # 保持最近一小时的数据
                        cutoff_time = timestamp - timedelta(hours=1)
                        self.price_history[stock_code] = [
                            p for p in self.price_history[stock_code] 
                            if p['time'] > cutoff_time
                        ]
                        
                        return current_price
        except Exception as e:
            logger.error(f"获取股票{stock_code}价格失败: {e}")
        
        return None

    def fetch_batch_prices(self, stock_codes: list[str]) -> dict[str, float]:
        """批量获取多只股票实时价格（一次 HTTP 请求）"""
        if not stock_codes:
            return {}
        url = "http://hq.sinajs.cn/list=" + ",".join(stock_codes)
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://finance.sina.com.cn'
        }
        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.encoding = 'gbk'
            if response.status_code != 200:
                logger.error(f"批量获取价格失败: HTTP {response.status_code}")
                return {}
            results = {}
            self._price_latency = None
            for line in response.text.strip().split("\n"):
                line = line.strip()
                if not line.startswith("var hq_str_"):
                    continue
                # 提取股票代码
                code_start = line.index("hq_str_") + 7
                code_end = line.index("=", code_start)
                code = line[code_start:code_end]
                parts = line.split('"')[1].split(",")
                if len(parts) <= 3:
                    continue
                try:
                    current_price = float(parts[3])
                    yesterday = 0.0
                    try:
                        yesterday = float(parts[2])
                    except (ValueError, IndexError):
                        pass
                    self.yesterday_close[code] = yesterday
                    # 解析买卖盘（用于涨跌停封单判定：买一量/价 parts[10/11]，卖一量/价 parts[20/21]）
                    bid1_vol = ask1_vol = 0.0
                    bid1_price = ask1_price = 0.0
                    try:
                        bid1_vol = float(parts[10])
                        bid1_price = float(parts[11])
                        ask1_vol = float(parts[20])
                        ask1_price = float(parts[21])
                    except (ValueError, IndexError):
                        pass
                    self._latest_quote[code] = {
                        'name': parts[0] if parts else '',
                        'prev_close': yesterday,
                        'price': current_price,
                        'bid1_vol': bid1_vol,
                        'bid1_price': bid1_price,
                        'ask1_vol': ask1_vol,
                        'ask1_price': ask1_price,
                    }
                    # 解析 API 返回的时间戳计算延迟
                    if len(parts) > 31:
                        try:
                            api_dt = datetime.strptime(f"{parts[30]} {parts[31]}", "%Y-%m-%d %H:%M:%S")
                            api_dt = api_dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                            latency = (datetime.now(ZoneInfo("Asia/Shanghai")) - api_dt).total_seconds()
                            self._price_latency = max(self._price_latency or 0, latency)
                        except (ValueError, IndexError):
                            pass
                    timestamp = datetime.now()
                    self.price_history.setdefault(code, []).append({
                        'time': timestamp,
                        'price': current_price,
                    })
                    cutoff = timestamp - timedelta(hours=1)
                    self.price_history[code] = [p for p in self.price_history[code] if p['time'] > cutoff]
                    results[code] = current_price
                except (ValueError, IndexError):
                    continue
            return results
        except Exception as e:
            logger.error(f"批量获取价格异常: {e}")
            return {}

    @staticmethod
    def _round_half_up_2(x: float) -> float:
        """A 股价格四舍五入到 2 位（round half up，非 banker）"""
        from decimal import Decimal, ROUND_HALF_UP
        return float(Decimal(str(x)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

    @staticmethod
    def _limit_ratio(code: str, name: str) -> float:
        """按板块 + ST 标识返回涨跌幅限制（小数）"""
        nm = (name or '').strip().upper()
        if nm.startswith('*ST') or nm.startswith('ST'):
            return 0.05
        c = code.lower()
        if c.startswith(('sh688', 'sz300', 'sz301')):
            return 0.20
        if c.startswith('bj'):
            return 0.30
        return 0.10

    def _compute_limit_prices(self, code: str, name: str, prev_close: float):
        """返回 (涨停价, 跌停价, ratio)；prev_close<=0 返回 (None,None,0)"""
        if not prev_close or prev_close <= 0:
            return None, None, 0.0
        ratio = StockMonitor._limit_ratio(code, name)
        up = StockMonitor._round_half_up_2(prev_close * (1 + ratio))
        down = StockMonitor._round_half_up_2(prev_close * (1 - ratio))
        return up, down, ratio

    def _send_limit_alert(self, alert_type: str, config: Dict, current_price: float,
                         limit_price: float, seal_vol: float, prev_close: float,
                         stock_code: str, *, seal_eta_seconds: Optional[int] = None):
        """组装涨跌停封单告警消息并发送"""
        seal_lots = int(round(seal_vol / 100.0)) if seal_vol > 0 else 0
        sealed_amount = (seal_vol * limit_price / 10000.0) if (seal_vol > 0 and limit_price) else 0.0
        daily_change = None
        if prev_close > 0:
            daily_change = (current_price - prev_close) / prev_close * 100
        self.send_dingding_notification(
            self.generate_disguise_message(
                alert_type, config, current_price,
                limit_price=limit_price,
                sealed_lots=seal_lots,
                sealed_amount=sealed_amount,
                seal_min_lots=config.get('limit_seal_min_lots'),
                seal_eta_seconds=seal_eta_seconds,
                daily_change=daily_change,
            )
        )

    def check_limit_status(self, stock_code: str, current_price: float):
        """涨跌停封单告警：封板 / 开板 / 封单不足 / 封单将尽"""
        if stock_code not in self._limit_state:
            return  # 基金或未初始化
        q = self._latest_quote.get(stock_code, {})
        if not q:
            return
        prev_close = q.get('prev_close', 0.0) or self.yesterday_close.get(stock_code, 0.0)
        up_price, down_price, ratio = self._compute_limit_prices(stock_code, q.get('name', ''), prev_close)
        if up_price is None or ratio == 0:
            return
        config = self.stocks[stock_code]
        disabled = set(config.get('disabled_alerts', []))
        state = self._limit_state[stock_code]
        bid1_price = q.get('bid1_price', 0.0)
        ask1_price = q.get('ask1_price', 0.0)
        bid1_vol = q.get('bid1_vol', 0.0)
        ask1_vol = q.get('ask1_vol', 0.0)

        # 涨停：现价≈涨停价 且 无卖盘；跌停对称
        sealed_up = abs(current_price - up_price) < 0.005 and (ask1_price == 0 or ask1_vol == 0)
        sealed_down = abs(current_price - down_price) < 0.005 and (bid1_price == 0 or bid1_vol == 0)
        prev_up = state['is_limit_up']
        prev_down = state['is_limit_down']

        if not state['_init']:
            state['_init'] = True
            state['is_limit_up'] = sealed_up
            state['is_limit_down'] = sealed_down
            self._seal_history[stock_code] = []
            self._low_seal_fired[stock_code] = False
            self._exhaust_fired[stock_code] = False
            return

        lim_price = up_price if sealed_up else (down_price if sealed_down else None)

        # --- 封板 / 开板（涨停）---
        if sealed_up and not prev_up:
            self._seal_history[stock_code] = [(datetime.now(), bid1_vol)]
            self._low_seal_fired[stock_code] = False
            self._exhaust_fired[stock_code] = False
            if 'limit_up' not in disabled and self.check_cooldown(stock_code, 'limit_up'):
                self._send_limit_alert('limit_up', config, current_price, up_price, bid1_vol, prev_close, stock_code)
                self.update_cooldown(stock_code, 'limit_up')
        elif (not sealed_up) and prev_up:
            if 'limit_up_broken' not in disabled and self.check_cooldown(stock_code, 'limit_up_broken'):
                self._send_limit_alert('limit_up_broken', config, current_price, up_price, 0.0, prev_close, stock_code)
                self.update_cooldown(stock_code, 'limit_up_broken')
            self._seal_history[stock_code] = []
            self._low_seal_fired[stock_code] = False
            self._exhaust_fired[stock_code] = False

        # --- 封板 / 开板（跌停）---
        if sealed_down and not prev_down:
            self._seal_history[stock_code] = [(datetime.now(), ask1_vol)]
            self._low_seal_fired[stock_code] = False
            self._exhaust_fired[stock_code] = False
            if 'limit_down' not in disabled and self.check_cooldown(stock_code, 'limit_down'):
                self._send_limit_alert('limit_down', config, current_price, down_price, ask1_vol, prev_close, stock_code)
                self.update_cooldown(stock_code, 'limit_down')
        elif (not sealed_down) and prev_down:
            if 'limit_down_broken' not in disabled and self.check_cooldown(stock_code, 'limit_down_broken'):
                self._send_limit_alert('limit_down_broken', config, current_price, down_price, 0.0, prev_close, stock_code)
                self.update_cooldown(stock_code, 'limit_down_broken')
            self._seal_history[stock_code] = []
            self._low_seal_fired[stock_code] = False
            self._exhaust_fired[stock_code] = False

        state['is_limit_up'] = sealed_up
        state['is_limit_down'] = sealed_down

        # --- 封板期间：封单不足 / 封单将尽 ---
        if not sealed_up and not sealed_down:
            return
        seal_vol = bid1_vol if sealed_up else ask1_vol
        low_type = 'limit_up_low_seal' if sealed_up else 'limit_down_low_seal'
        exhaust_type = 'limit_up_exhaust' if sealed_up else 'limit_down_exhaust'
        if seal_vol <= 0:
            return
        now = datetime.now()
        hist = self._seal_history.setdefault(stock_code, [])
        hist.append((now, seal_vol))
        n = max(2, self._limit_exhaust_samples)
        if len(hist) > n:
            self._seal_history[stock_code] = hist[-n:]
            hist = self._seal_history[stock_code]
        seal_lots = seal_vol / 100.0

        # 封单不足
        min_lots = config.get('limit_seal_min_lots')
        if min_lots:
            if seal_lots < min_lots and not self._low_seal_fired[stock_code]:
                if low_type not in disabled and self.check_cooldown(stock_code, low_type):
                    self._send_limit_alert(low_type, config, current_price, lim_price, seal_vol, prev_close, stock_code)
                    self.update_cooldown(stock_code, low_type)
                self._low_seal_fired[stock_code] = True
            elif seal_lots >= min_lots:
                self._low_seal_fired[stock_code] = False

        # 封单将尽：按最近 N 个样本的平均消耗速度预测 ETA
        if len(hist) >= self._limit_exhaust_samples:
            t0, v0 = hist[0]
            t1, v1 = hist[-1]
            elapsed = (t1 - t0).total_seconds()
            consumed = v0 - v1  # 正数 = 封单被消耗
            if elapsed > 0 and consumed > 0:
                rate = consumed / elapsed  # 股/秒
                eta = v1 / rate
                if eta <= self._limit_exhaust_seconds and not self._exhaust_fired[stock_code]:
                    if exhaust_type not in disabled and self.check_cooldown(stock_code, exhaust_type):
                        self._send_limit_alert(exhaust_type, config, current_price, lim_price,
                                               seal_vol, prev_close, stock_code,
                                               seal_eta_seconds=int(round(eta)))
                        self.update_cooldown(stock_code, exhaust_type)
                    self._exhaust_fired[stock_code] = True
                elif eta > self._limit_exhaust_seconds:
                    self._exhaust_fired[stock_code] = False

    def check_cooldown(self, stock_code: str, alert_type: str) -> bool:
        """
        检查是否在冷却时间内
        
        Returns:
            True: 可以发送通知
            False: 在冷却时间内
        """
        last_time = self.notification_cooldown.get(stock_code, {}).get(alert_type)
        if last_time is None:
            return True

        cooldown_minutes = self.stocks[stock_code].get('cooldown_minutes', 5)
        cooldown_end = last_time + timedelta(minutes=cooldown_minutes)

        return datetime.now() > cooldown_end
    
    def update_cooldown(self, stock_code: str, alert_type: str):
        """更新通知冷却时间"""
        self.notification_cooldown.setdefault(stock_code, {})[alert_type] = datetime.now()
    
    def _market_of(self, stock_info: Dict) -> str:
        """根据 stock_info 配置字典返回市场标识：stock / fund / crypto"""
        mkt = stock_info.get('_market') if isinstance(stock_info, dict) else None
        if mkt in ('stock', 'fund', 'crypto'):
            return mkt
        return 'stock'

    def generate_disguise_message(self, alert_type: str, stock_info: Dict,
                                 current_price: float, threshold: float = None, *,
                                 tier_index: int = None, tier_threshold: float = None,
                                 peak_price: float = None, valley_price: float = None,
                                 daily_change: float = None, speed_change: float = None,
                                 retrace_pct: float = None, bounce_pct: float = None,
                                 t_type: str = None, t_price: float = None,
                                 t_threshold: float = None, t_quantity: int = None,
                                 limit_price: float = None,
                                 sealed_lots: int = None,
                                 sealed_amount: float = None,
                                 seal_min_lots: int = None,
                                 seal_eta_seconds: int = None,
                                 position_cost: float = None,
                                 profit_pct: float = None,) -> str:
        import random

        # 模板回退：市场覆盖 → 全局基础 → 空则不发（返回空字符串）
        market = self._market_of(stock_info)
        mkt_map = self.market_templates.get(market) or {}
        templates = mkt_map.get(alert_type) or self.disguise_templates.get(alert_type) or []
        if not templates:
            return ""
        template = random.choice(templates)

        # 价格精度：合约品种按 exchangeInfo 的 pricePrecision，A 股/基金默认 2
        precision = int(stock_info.get('price_precision', 2) if isinstance(stock_info, dict) else 2)
        price_str = f"{current_price:.{precision}f}"
        threshold_str = f"{threshold:.{precision}f}" if threshold is not None else ""

        name_val = (stock_info.get('name') or '').strip()
        nickname_val = (stock_info.get('nickname') or name_val).strip()

        def _fmt_price(v):
            return f"{v:.{precision}f}" if v is not None else ""

        # 合约方向/倍率（仅合约有，股票/基金为空）
        direction_val = stock_info.get('direction') if isinstance(stock_info, dict) else None
        direction_label = {'long': '多', 'short': '空'}.get(direction_val, '') if direction_val else ''
        leverage_val = stock_info.get('leverage') if isinstance(stock_info, dict) else None
        leverage_str = f"{int(leverage_val)}倍" if leverage_val else ''

        message = template.format(
            name=name_val,
            nickname=nickname_val,
            price=price_str,
            threshold=threshold_str,
            time=str(stock_info.get('speed_window', 5)),
            tier_index=str(tier_index) if tier_index is not None else "",
            tier_threshold=f"{tier_threshold:.2f}%" if tier_threshold is not None else "",
            peak_price=_fmt_price(peak_price),
            valley_price=_fmt_price(valley_price),
            daily_change=f"{daily_change:+.2f}%" if daily_change is not None else "",
            speed_change=f"{speed_change:+.2f}%" if speed_change is not None else "",
            retracement=f"{retrace_pct:+.2f}%" if retrace_pct is not None else "",
            bounce=f"{bounce_pct:+.2f}%" if bounce_pct is not None else "",
            t_type=str(t_type) if t_type is not None else "",
            t_price=_fmt_price(t_price),
            t_threshold=f"{t_threshold:.2f}%" if t_threshold is not None else "",
            t_quantity=f" {t_quantity}手" if t_quantity is not None else "",
            limit_price=_fmt_price(limit_price),
            sealed_lots=f"{sealed_lots:,}" if sealed_lots is not None else "",
            sealed_amount=f"{sealed_amount:,.2f}" if sealed_amount is not None else "",
            seal_min_lots=f"{seal_min_lots:,}" if seal_min_lots is not None else "",
            seal_eta_seconds=str(seal_eta_seconds) if seal_eta_seconds is not None else "",
            position_cost=_fmt_price(position_cost),
            profit_pct=f"{profit_pct:+.2f}%" if profit_pct is not None else "",
            direction=direction_label,
            leverage=leverage_str,
        )

        message += '.'
        return message
    
    def send_dingding_notification(self, message: str) -> bool:
        """发送通知（向后兼容的别名）"""
        return self.send_notification(message)

    def channel_ready(self, channel: str) -> bool:
        """通道是否已开启且配置完整可发送。"""
        if not self.notify_channels.get(channel):
            return False
        if channel == "dingding":
            return bool(self.dingding_webhook)
        if channel == "email":
            return bool(self.email_smtp_host and self.email_username and self.email_to_addrs)
        return False

    def send_notification(self, message: str) -> bool:
        """发送通知。空消息丢弃；multi 模式全发，single 模式按优先级回退到首个成功。

        返回是否至少有一个通道送达（批量模式下入缓冲视为已送达，将随本轮一起发送）。
        """
        if not message or not message.strip():
            return False
        if self._batch_mode:
            self._alert_buffer.append(message)
            return True
        if self.notify_mode == "multi":
            ok = False
            for ch in self.notify_priority:
                if self.channel_ready(ch):
                    if self._dispatch(ch, message):
                        ok = True
            return ok
        # single 模式：按优先级找首个开启且完整的通道，失败回退下一个，直到成功
        for ch in self.notify_priority:
            if not self.channel_ready(ch):
                continue
            ok = self._dispatch(ch, message)
            if ok:
                return True
            logger.warning(f"通道 {ch} 发送失败，尝试回退到下一个通道")
        logger.error("所有通知通道均发送失败")
        return False

    def _dispatch(self, channel: str, message: str) -> bool:
        """实际发送到指定通道，返回是否成功。"""
        if channel == "email":
            return self._send_email(message)
        return self._do_send(message)

    def _do_send(self, message: str) -> bool:
        content = message
        if self.dingding_keyword:
            content = self.dingding_keyword + content
        at_mobiles = self.at_mobiles if self.at_mobiles else None
        at_user_ids = self.at_user_ids if self.at_user_ids else None
        try:
            at_payload: dict[str, object] = {"isAtAll": False}
            if at_mobiles:
                at_payload["atMobiles"] = at_mobiles
            if at_user_ids:
                at_payload["atUserIds"] = at_user_ids
            payload = {
                "msgtype": "text",
                "text": {
                    "content": content
                },
                "at": at_payload,
            }
            
            headers = {'Content-Type': 'application/json'}
            response = requests.post(
                self.dingding_webhook,
                data=json.dumps(payload),
                headers=headers,
                timeout=10
            )
            
            if response.status_code == 200:
                errcode = 0
                errmsg = ""
                try:
                    resp = response.json()
                    errcode = resp.get("errcode", 0)
                    errmsg = resp.get("errmsg", "")
                except ValueError:
                    pass
                if errcode == 0:
                    logger.info(f"钉钉通知发送成功: {message[:50]}...")
                    return True
                else:
                    logger.error(f"钉钉通知被拒: errcode={errcode} errmsg={errmsg} 消息: {message[:50]}...")
                    return False
            else:
                logger.error(f"钉钉通知发送失败: {response.status_code}")
                return False
                
        except Exception as e:
            logger.error(f"发送钉钉通知异常: {e}")
            return False

    def _send_email(self, message: str) -> bool:
        """通过 SMTP 发送邮件通知，返回是否成功。"""
        if not (self.email_smtp_host and self.email_username and self.email_to_addrs):
            logger.error("邮箱通知未完整配置（host/username/to_addrs），跳过发送")
            return False
        import smtplib
        from email.mime.text import MIMEText
        try:
            subject = "stock-monitor 告警"
            msg = MIMEText(message, "plain", "utf-8")
            msg["Subject"] = subject
            from_addr = self.email_from_addr or self.email_username
            msg["From"] = from_addr
            msg["To"] = ", ".join(self.email_to_addrs)
            if self.email_use_ssl:
                server = smtplib.SMTP_SSL(self.email_smtp_host, self.email_smtp_port, timeout=10)
            else:
                server = smtplib.SMTP(self.email_smtp_host, self.email_smtp_port, timeout=10)
                server.starttls()
            try:
                server.login(self.email_username, self.email_password)
                server.sendmail(from_addr, self.email_to_addrs, msg.as_string())
                logger.info(f"邮件通知发送成功: {message[:50]}...")
                return True
            finally:
                try:
                    server.quit()
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"发送邮件通知异常: {e}")
            return False

    def flush_alerts(self):
        if not self._alert_buffer:
            return
        message = '\n'.join(self._alert_buffer)
        self._alert_buffer.clear()
        self.send_notification(message)

    def _get_high_tiers(self, config: dict) -> list[float]:
        tiers = []
        if config.get('price_high') is not None:
            tiers.append(config['price_high'])
        return tiers

    def _get_low_tiers(self, config: dict) -> list[float]:
        tiers = []
        if config.get('price_low') is not None:
            tiers.append(config['price_low'])
        return tiers

    def _get_change_high_tiers(self, config: dict) -> list[float]:
        return sorted(t for t in config.get('daily_change_up', []) if t is not None)

    def _get_change_low_tiers(self, config: dict) -> list[float]:
        return sorted(t for t in config.get('daily_change_down', []) if t is not None)

    def check_price_threshold(self, stock_code: str, current_price: float):
        config = self.stocks[stock_code]
        disabled = set(config.get('disabled_alerts', []))
        alert_status = self.price_alert_status[stock_code]
        alerted_abs_high = self.price_high_alerted_abs[stock_code]
        alerted_abs_low = self.price_low_alerted_abs[stock_code]
        alerted_daily_high = self.price_high_alerted_daily[stock_code]
        alerted_daily_low = self.price_low_alerted_daily[stock_code]

        # 当日涨跌幅（所有通知类型共用）
        yesterday = self.yesterday_close.get(stock_code, 0.0)
        daily_change_pct = None
        if yesterday > 0:
            daily_change_pct = (current_price - yesterday) / yesterday * 100

        # --- 1. 单档绝对价格阈值（price_high / price_low）---
        high_tiers = self._get_high_tiers(config)
        low_tiers = self._get_low_tiers(config)

        if not alert_status.get('_high_init', False):
            for idx, tier_price in enumerate(high_tiers, start=1):
                if current_price > tier_price:
                    alerted_abs_high.add(idx)
                    self.peak_since_high_alert[stock_code] = max(
                        self.peak_since_high_alert.get(stock_code, 0.0), current_price
                    )
            alert_status['_high_init'] = True
        else:
            for idx, tier_price in enumerate(high_tiers, start=1):
                if current_price > tier_price:
                    if idx not in alerted_abs_high:
                        if 'price_high' not in disabled and self.check_cooldown(stock_code, 'price_high'):
                            self.send_dingding_notification(
                                self.generate_disguise_message(
                                    'price_high', config, current_price,
                                    threshold=tier_price,
                                    daily_change=daily_change_pct,
                                )
                            )
                            self.update_cooldown(stock_code, 'price_high')
                        alerted_abs_high.add(idx)
                        self.peak_since_high_alert[stock_code] = current_price

        if not alert_status.get('_low_init', False):
            for idx, tier_price in enumerate(low_tiers, start=1):
                if current_price < tier_price:
                    alerted_abs_low.add(idx)
                    self.valley_since_low_alert[stock_code] = min(
                        self.valley_since_low_alert.get(stock_code, float('inf')), current_price
                    )
            alert_status['_low_init'] = True
        else:
            for idx, tier_price in enumerate(low_tiers, start=1):
                if current_price < tier_price:
                    if idx not in alerted_abs_low:
                        if 'price_low' not in disabled and self.check_cooldown(stock_code, 'price_low'):
                            self.send_dingding_notification(
                                self.generate_disguise_message(
                                    'price_low', config, current_price,
                                    threshold=tier_price,
                                    daily_change=daily_change_pct,
                                )
                            )
                            self.update_cooldown(stock_code, 'price_low')
                        alerted_abs_low.add(idx)
                        self.valley_since_low_alert[stock_code] = current_price

        # --- 2. 多档当日涨跌百分比（daily_change_up / daily_change_down）---
        yesterday = self.yesterday_close.get(stock_code, 0.0)
        daily_change_pct = None
        if yesterday > 0:
            daily_change_pct = (current_price - yesterday) / yesterday * 100

        if daily_change_pct is not None:
            change_high = self._get_change_high_tiers(config)
            change_low = self._get_change_low_tiers(config)

            # 先找出本次新增触发的最高档位，低档只标记不发送
            new_high_idx = None
            for idx, tier_pct in enumerate(change_high, start=1):
                if daily_change_pct >= tier_pct and idx not in alerted_daily_high:
                    alerted_daily_high.add(idx)
                    new_high_idx = idx

            if new_high_idx is not None:
                tier_pct = change_high[new_high_idx - 1]
                cooldown_key = f'daily_up_tier_{new_high_idx}'
                if 'daily_up' not in disabled and self.check_cooldown(stock_code, cooldown_key):
                    self.send_dingding_notification(
                        self.generate_disguise_message(
                            'daily_up', config, current_price,
                            threshold=tier_pct,
                            tier_index=new_high_idx, tier_threshold=tier_pct,
                            daily_change=daily_change_pct,
                        )
                    )
                    self.update_cooldown(stock_code, cooldown_key)
                self.retracement_armed[stock_code] = True
                self.peak_since_high_alert[stock_code] = current_price

            new_low_idx = None
            for idx, tier_pct in enumerate(change_low, start=1):
                if daily_change_pct <= -tier_pct and idx not in alerted_daily_low:
                    alerted_daily_low.add(idx)
                    new_low_idx = idx

            if new_low_idx is not None:
                tier_pct = change_low[new_low_idx - 1]
                cooldown_key = f'daily_down_tier_{new_low_idx}'
                if 'daily_down' not in disabled and self.check_cooldown(stock_code, cooldown_key):
                    self.send_dingding_notification(
                        self.generate_disguise_message(
                            'daily_down', config, current_price,
                            threshold=tier_pct,
                            tier_index=new_low_idx, tier_threshold=tier_pct,
                            daily_change=daily_change_pct,
                        )
                    )
                    self.update_cooldown(stock_code, cooldown_key)
                self.bounce_armed[stock_code] = True
                self.valley_since_low_alert[stock_code] = current_price

        # --- 3. 回撤检测（从高位回落）---
        retrace_th = config.get('retracement_threshold')
        if retrace_th is not None and 'retracement' not in disabled and self.retracement_armed.get(stock_code, False):
            peak = self.peak_since_high_alert.get(stock_code, 0.0)
            if current_price > peak:
                self.peak_since_high_alert[stock_code] = current_price
                peak = current_price
            if peak > 0 and current_price < peak:
                drop_pct = (peak - current_price) / peak * 100
                if drop_pct >= retrace_th:
                    if self.check_cooldown(stock_code, 'retracement'):
                        self.send_dingding_notification(
                            self.generate_disguise_message(
                                'retracement', config, current_price,
                                retrace_pct=-drop_pct,
                                peak_price=peak,
                                daily_change=daily_change_pct,
                            )
                        )
                        self.update_cooldown(stock_code, 'retracement')
                    self.retracement_armed[stock_code] = False
                    self.price_high_alerted_daily[stock_code].clear()
                    self.price_high_alerted_abs[stock_code].clear()
                    alert_status['_high_init'] = False

        # --- 4. 反弹检测（从低位回升）---
        bounce_th = config.get('bounce_threshold')
        if bounce_th is not None and 'bounce' not in disabled and self.bounce_armed.get(stock_code, False):
            valley = self.valley_since_low_alert.get(stock_code, float('inf'))
            if current_price < valley:
                self.valley_since_low_alert[stock_code] = current_price
                valley = current_price
            if valley < float('inf') and valley > 0 and current_price > valley:
                rise_pct = (current_price - valley) / valley * 100
                if rise_pct >= bounce_th:
                    if self.check_cooldown(stock_code, 'bounce'):
                        self.send_dingding_notification(
                            self.generate_disguise_message(
                                'bounce', config, current_price,
                                bounce_pct=rise_pct,
                                valley_price=valley,
                                daily_change=daily_change_pct,
                            )
                        )
                        self.update_cooldown(stock_code, 'bounce')
                    self.bounce_armed[stock_code] = False
                    self.price_low_alerted_daily[stock_code].clear()
                    self.price_low_alerted_abs[stock_code].clear()
                    alert_status['_low_init'] = False
    
    def _profit_loss_params(self, stock_code: str):
        """返回 (direction_sign, leverage) 用于盈亏计算。

        股票/基金：方向 +1，杠杆 1。
        合约：多 +1 / 空 -1，杠杆取配置 leverage（空=1）。
        """
        config = self.stocks.get(stock_code, {})
        if stock_code in self._crypto_codes:
            sign = -1 if config.get('direction') == 'short' else 1
            lev = config.get('leverage')
            lev = float(lev) if lev else 1.0
            return sign, lev
        return 1, 1

    def check_profit_loss(self, stock_code: str, current_price: float):
        """检查盈亏：现价 vs 持仓成本，跨过成本线时通知一次。

        股票/基金：现价 > 成本 即盈利。
        合约：多(空)方向时现价 >(<) 成本 为盈利；盈亏% = 价格变动% × 方向符号 × 杠杆。
        """
        config = self.stocks[stock_code]
        position_cost = config.get('position_cost')
        if position_cost is None or position_cost <= 0:
            return
        disabled = set(config.get('disabled_alerts', []))
        # 当日涨跌幅（消息里附带）
        yesterday = self.yesterday_close.get(stock_code, 0.0)
        daily_change = None
        if yesterday > 0:
            daily_change = (current_price - yesterday) / yesterday * 100

        sign, leverage = self._profit_loss_params(stock_code)
        # 实际持仓盈亏百分比（含方向与杠杆）
        profit_pct = (current_price - position_cost) / position_cost * 100 * sign * leverage
        is_profit = profit_pct > 0

        if is_profit:
            if not self._profit_alerted.get(stock_code, False):
                if 'profit' not in disabled and self.check_cooldown(stock_code, 'profit'):
                    self.send_dingding_notification(
                        self.generate_disguise_message(
                            'profit', config, current_price,
                            position_cost=position_cost, profit_pct=profit_pct,
                            daily_change=daily_change,
                        )
                    )
                    self.update_cooldown(stock_code, 'profit')
                self._profit_alerted[stock_code] = True
                self._loss_alerted[stock_code] = False
        else:
            if not self._loss_alerted.get(stock_code, False):
                if 'loss' not in disabled and self.check_cooldown(stock_code, 'loss'):
                    self.send_dingding_notification(
                        self.generate_disguise_message(
                            'loss', config, current_price,
                            position_cost=position_cost, profit_pct=profit_pct,
                            daily_change=daily_change,
                        )
                    )
                    self.update_cooldown(stock_code, 'loss')
                self._loss_alerted[stock_code] = True
                self._profit_alerted[stock_code] = False

    def check_surge_alert(self, stock_code: str, current_price: float):
        """
        检查涨跌幅告警
        
        Args:
            stock_code: 股票代码
            current_price: 当前价格
        """
        config = self.stocks[stock_code]
        disabled = set(config.get('disabled_alerts', []))

        # 当日涨跌幅
        yesterday = self.yesterday_close.get(stock_code, 0.0)
        surge_daily = None
        if yesterday > 0:
            surge_daily = (current_price - yesterday) / yesterday * 100

        if config.get('speed_threshold') is not None and len(self.price_history[stock_code]) > 1:
            # 获取指定时间前的价格
            speed_window = config.get('speed_window', 5)
            period_start = datetime.now() - timedelta(minutes=speed_window)
            
            # 查找周期开始时的价格
            historical_prices = [
                p for p in self.price_history[stock_code] 
                if p['time'] >= period_start
            ]
            
            if historical_prices:
                oldest_price = historical_prices[0]['price']
                if oldest_price <= 0:
                    return
                price_change_percent = ((current_price - oldest_price) / oldest_price) * 100
                
                # 涨跌百分比精确到小数点后2位
                price_change_percent = round(price_change_percent, 2)
                
                # 检查暴涨
                if price_change_percent > config['speed_threshold']:
                    if 'surge_up' not in disabled and self.check_cooldown(stock_code, 'surge_up'):
                        message = self.generate_disguise_message(
                            'surge_up', config, current_price, 
                            speed_change=price_change_percent,
                            daily_change=surge_daily,
                        )
                        self.send_dingding_notification(message)
                        self.update_cooldown(stock_code, 'surge_up')
                
                # 检查暴跌
                elif price_change_percent < -config['speed_threshold']:
                    if 'surge_down' not in disabled and self.check_cooldown(stock_code, 'surge_down'):
                        message = self.generate_disguise_message(
                            'surge_down', config, current_price,
                            speed_change=price_change_percent,
                            daily_change=surge_daily,
                        )
                        self.send_dingding_notification(message)
                        self.update_cooldown(stock_code, 'surge_down')
    
    def check_t_events(self, stock_code: str, current_price: float):
        """检查做T事件：S（先卖后买）价格跌 T% 通知 / B（先买后卖）价格涨 T% 通知"""
        config = self.stocks[stock_code]
        disabled = set(config.get('disabled_alerts', []))
        threshold = config.get('t_threshold')
        s_enabled = config.get('t_s_enabled', True)
        b_enabled = config.get('t_b_enabled', True)
        if not s_enabled and not b_enabled:
            return
        # 当日涨跌幅
        yesterday = self.yesterday_close.get(stock_code, 0.0)
        t_daily = None
        if yesterday > 0:
            t_daily = (current_price - yesterday) / yesterday * 100
        events = self.t_events.get(stock_code, [])
        if not events:
            return
        remaining = []
        for ev in events:
            ev_price = ev['price']
            alert_type = 't_sell' if ev['type'] == 'S' else 't_buy'
            if ev['type'] == 'S':
                if not s_enabled:
                    remaining.append(ev)
                    continue
                target = ev.get('target_price')
                if target is not None and target > 0:
                    should_trigger = current_price <= target
                else:
                    if threshold is None or threshold <= 0:
                        remaining.append(ev)
                        continue
                    should_trigger = current_price <= ev_price * (1 - threshold / 100)
                if should_trigger:
                    self.t_events_triggered.setdefault(stock_code, set()).add(ev["id"])
                    if 't_sell' not in disabled and self.check_cooldown(stock_code, alert_type):
                        self.send_dingding_notification(
                            self.generate_disguise_message(
                                alert_type, config, current_price,
                                t_price=ev_price, t_threshold=threshold, t_type='S',
                                t_quantity=ev.get('quantity'),
                                daily_change=t_daily,
                            )
                        )
                        self.update_cooldown(stock_code, alert_type)
                    continue  # 事件触发，移除
            else:  # B
                if not b_enabled:
                    remaining.append(ev)
                    continue
                target = ev.get('target_price')
                if target is not None and target > 0:
                    should_trigger = current_price >= target
                else:
                    if threshold is None or threshold <= 0:
                        remaining.append(ev)
                        continue
                    should_trigger = current_price >= ev_price * (1 + threshold / 100)
                if should_trigger:
                    self.t_events_triggered.setdefault(stock_code, set()).add(ev["id"])
                    if 't_buy' not in disabled and self.check_cooldown(stock_code, alert_type):
                        self.send_dingding_notification(
                            self.generate_disguise_message(
                                alert_type, config, current_price,
                                t_price=ev_price, t_threshold=threshold, t_type='B',
                                t_quantity=ev.get('quantity'),
                                daily_change=t_daily,
                            )
                        )
                        self.update_cooldown(stock_code, alert_type)
                    continue  # 事件触发，移除
            remaining.append(ev)
        if len(remaining) != len(events):
            self.t_events[stock_code] = remaining

    def daily_reset(self, stock_t_events: dict[str, list[dict]]):
        """每日重置所有通知状态，重新加载 T 事件（保留 stock_code 键，重置内部值）"""
        for v in self.notification_cooldown.values():
            v.clear()
        for v in self.price_alert_status.values():
            v.clear()
            v.update({'_high_init': False, '_low_init': False})
        for v in self.price_high_alerted_abs.values():
            v.clear()
        for v in self.price_low_alerted_abs.values():
            v.clear()
        for v in self.price_high_alerted_daily.values():
            v.clear()
        for v in self.price_low_alerted_daily.values():
            v.clear()
        for code in self.peak_since_high_alert:
            self.peak_since_high_alert[code] = 0.0
        for code in self.valley_since_low_alert:
            self.valley_since_low_alert[code] = float('inf')
        for code in self.retracement_armed:
            self.retracement_armed[code] = False
        for code in self.bounce_armed:
            self.bounce_armed[code] = False
        for code in self._profit_alerted:
            self._profit_alerted[code] = False
        for code in self._loss_alerted:
            self._loss_alerted[code] = False
        # 涨跌停封单状态重置
        for st in self._limit_state.values():
            st['is_limit_up'] = False
            st['is_limit_down'] = False
            st['_init'] = False
        for code in self._seal_history:
            self._seal_history[code] = []
        for code in self._low_seal_fired:
            self._low_seal_fired[code] = False
        for code in self._exhaust_fired:
            self._exhaust_fired[code] = False
        for code, events in stock_t_events.items():
            if code in self.t_events:
                self.t_events[code] = list(events)
        self.t_events_triggered.clear()
        logger.info("每日通知状态已重置")

    def check_stock_alerts(self, stock_code: str, override_price: float | None = None):
        """检查单个股票的警报条件"""
        if stock_code not in self.stocks:
            return
        
        config = self.stocks[stock_code]
        current_price = override_price if override_price is not None else self.get_stock_price(stock_code)
        
        if current_price is None or current_price <= 0:
            return
        
        # 1. 检查价格阈值反转
        self.check_price_threshold(stock_code, current_price)
        
        # 2. 检查涨跌幅告警
        self.check_surge_alert(stock_code, current_price)
        
        # 3. 检查做T事件
        self.check_t_events(stock_code, current_price)

        # 4. 检查涨跌停封单
        self.check_limit_status(stock_code, current_price)

        # 5. 检查盈亏（持仓成本对比）
        self.check_profit_loss(stock_code, current_price)
    
    @staticmethod
    def is_trading_day(date_obj) -> bool:
        """
        判断给定日期是否为 A 股交易日

        使用 cn-stock-holidays (rainx/cn_stock_holidays) 库过滤周末 + 法定节假日。
        库加载失败时降级为"仅排除周末"。
        """
        if date_obj.weekday() >= 5:
            return False
        try:
            return date_obj not in shsz.get_cached()
        except Exception as e:
            logger.warning(f"节假日库调用失败，降级为仅看周末: {e}")
            return True

    @staticmethod
    def is_trading_time(now: Optional[datetime] = None) -> bool:
        """
        判断当前是否处于 A 股交易时段

        A 股连续竞价时间：工作日 9:30-11:30, 13:00-15:00 (北京时间)
        从 9:20（集合竞价）开始轮询准备，以便开盘即获取报价。
        法定节假日通过 cn-stock-holidays 过滤
        """
        if now is None:
            now = datetime.now(ZoneInfo("Asia/Shanghai"))
        elif now.tzinfo is None:
            now = now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))

        if not StockMonitor.is_trading_day(now.date()):
            return False

        current_time = now.time()
        morning_start = datetime.strptime("09:20", "%H:%M").time()
        morning_end = datetime.strptime("11:30", "%H:%M").time()
        afternoon_start = datetime.strptime("13:00", "%H:%M").time()
        afternoon_end = datetime.strptime("15:00", "%H:%M").time()

        return (morning_start <= current_time <= morning_end or
                afternoon_start <= current_time <= afternoon_end)

    @staticmethod
    def _seconds_until_next_check(interval_seconds: int = 30) -> float:
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        today = now.date()
        t = now.time()

        morning_start = datetime.strptime("09:20", "%H:%M").time()
        morning_end = datetime.strptime("11:30", "%H:%M").time()
        afternoon_start = datetime.strptime("13:00", "%H:%M").time()
        afternoon_end = datetime.strptime("15:00", "%H:%M").time()

        if not StockMonitor.is_trading_day(today):
            return StockMonitor._seconds_until_next_trading_day(now, morning_start)

        if t < morning_start:
            return (datetime.combine(today, morning_start, tzinfo=ZoneInfo("Asia/Shanghai")) - now).total_seconds()
        if t <= morning_end:
            return interval_seconds
        if t < afternoon_start:
            return (datetime.combine(today, afternoon_start, tzinfo=ZoneInfo("Asia/Shanghai")) - now).total_seconds()
        if t <= afternoon_end:
            return interval_seconds
        return StockMonitor._seconds_until_next_trading_day(now, morning_start)

    @staticmethod
    def _seconds_until_next_trading_day(now: datetime, target_time) -> float:
        d = now.date() + timedelta(days=1)
        for _ in range(14):
            if StockMonitor.is_trading_day(d):
                break
            d += timedelta(days=1)
        next_start = datetime.combine(d, target_time, tzinfo=ZoneInfo("Asia/Shanghai"))
        return (next_start - now).total_seconds()

    def monitor_loop(self, interval_seconds: int = 30):
        """监控循环（同一轮次多条通知合为一条发送，避免钉钉限流）

        合约品种 24/7 交易，始终按 interval_seconds 轮询，不受 A 股交易时段限制。
        """
        logger.info("开始股票监控...")

        while self.running:
            try:
                sleep_seconds = StockMonitor._seconds_until_next_check(interval_seconds)
                # 有合约品种时，休眠不超过一个轮询周期（合约 24/7）
                if self._crypto_codes:
                    sleep_seconds = interval_seconds
                if sleep_seconds == interval_seconds or self._crypto_codes:
                    self._batch_mode = True
                    self._alert_buffer.clear()
                    # A 股 / 基金（仅交易时段）
                    if sleep_seconds == interval_seconds:
                        codes = [c for c in self.stocks if c not in self._crypto_codes]
                        if codes:
                            prices = self.fetch_batch_prices(codes)
                            for code in codes:
                                if code not in prices:
                                    continue
                                self.check_stock_alerts(code, override_price=prices[code])
                    # 合约（始终轮询）
                    if self._crypto_codes:
                        crypto_codes = list(self._crypto_codes)
                        prices = self.fetch_crypto_prices(crypto_codes)
                        for code in crypto_codes:
                            if code not in prices:
                                continue
                            self.check_stock_alerts(code, override_price=prices[code])
                    self._batch_mode = False
                    self.flush_alerts()

                time.sleep(sleep_seconds)
                
            except KeyboardInterrupt:
                logger.info("接收到中断信号，停止监控...")
                self.running = False
            except Exception as e:
                logger.error(f"监控循环异常: {e}")
                time.sleep(60)  # 异常后等待1分钟再试
    
    def stop(self):
        """停止监控"""
        self.running = False
        logger.info("股票监控已停止")

# 配置示例
def setup_monitor_example():
    """
    示例配置函数

    请通过环境变量 DINGDING_WEBHOOK 设置钉钉机器人 Webhook 地址
    (在钉钉群中添加自定义机器人即可获取)
    """

    # 0. 同步最新节假日数据（cn-stock-holidays 每日从 GitHub 拉取最新节假日表）
    try:
        shsz.sync_data()
        logger.info(f"已同步节假日表，本地缓存 {len(shsz.get_cached())} 条")
    except Exception as e:
        logger.warning(f"节假日表同步失败（不影响启动，将使用内置数据）: {e}")

    # 1. 从环境变量读取钉钉机器人 Webhook
    DINGDING_WEBHOOK = os.environ.get("DINGDING_WEBHOOK")

    # 2. 创建监控器
    monitor = StockMonitor(DINGDING_WEBHOOK)

    # 3. 添加要监控的股票
    # 股票代码格式：沪市 sh600000，深市 sz000001

    monitor.add_stock("sz300115", {
        "name": "CY",
        "price_high": 45,  # 超过45元提醒
        "price_low": 42.5,   # 低于42.5元提醒
        "surge_threshold": 2.5,  # 5分钟内涨跌超过2.5%提醒
        "surge_period": 5,       # 监控周期5分钟
        "cooldown_minutes": 5   # 同类通知冷却5分钟
    })

    return monitor

def main():
    """主函数"""
    print("=" * 60)
    print("股票监控助手 - 智能监控A股股票")
    print("=" * 60)
    print("注意：本工具发送的通知已做伪装处理")
    print("请通过环境变量 DINGDING_WEBHOOK 设置钉钉机器人 Webhook 地址")
    print("  export DINGDING_WEBHOOK='https://oapi.dingtalk.com/robot/send?access_token=...'")
    print("=" * 60)
    
    try:
        # 设置监控器
        monitor = setup_monitor_example()
        
        # 启动监控（每30秒检查一次）
        monitor.monitor_loop(interval_seconds=30)
        
    except KeyboardInterrupt:
        print("\n监控已停止")
    except Exception as e:
        logger.error(f"程序运行异常: {e}")
        print(f"程序异常: {e}")
        print("请检查配置后重试")

if __name__ == "__main__":
    main()
