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
import sys

import cn_stock_holidays.data as shsz

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

class StockMonitor:
    def __init__(self, dingding_webhook: str, at_mobiles: list[str] | None = None, at_user_ids: list[str] | None = None):
        """
        初始化股票监控器

        Args:
            dingding_webhook: 钉钉群机器人的Webhook地址
            at_mobiles: 通知时 @ 的手机号列表
            at_user_ids: 通知时 @ 的用户 ID 列表
        """
        if not dingding_webhook:
            logger.warning(
                "钉钉 Webhook 未配置。发送通知将失败，请通过 Web UI 或环境变量 DINGDING_WEBHOOK 设置。"
            )
        self.dingding_webhook = dingding_webhook
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
        # 做T事件（运行时 + 持久化存于 config.json）
        self.t_events: dict[str, list[dict]] = {}
        # 当日已触发的 T 事件 ID 集合
        self.t_events_triggered: dict[str, set[str]] = {}
        # 最近一次状态重置日期
        self._reset_date: Optional[date] = None

        # 价格数据延迟（API 时间戳与本地时间之差，秒）
        self._price_latency: Optional[float] = None

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
            't_sell': [
                "🔻 {name} 做T可买回：{t_price}→{price}（跌{t_threshold}%）"
            ],
            't_buy': [
                "🟢 {name} 做T可卖出：{t_price}→{price}（涨{t_threshold}%）"
            ],
        }
    
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
        self.t_events[stock_code] = list(config.get('t_events', []))
        logger.info(f"添加监控股票: {config['name']} ({stock_code})")
    
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

    def check_cooldown(self, stock_code: str, alert_type: str) -> bool:
        """
        检查是否在冷却时间内
        
        Returns:
            True: 可以发送通知
            False: 在冷却时间内
        """
        last_time = self.notification_cooldown[stock_code].get(alert_type)
        if last_time is None:
            return True

        cooldown_minutes = self.stocks[stock_code].get('cooldown_minutes', 5)
        cooldown_end = last_time + timedelta(minutes=cooldown_minutes)

        return datetime.now() > cooldown_end
    
    def update_cooldown(self, stock_code: str, alert_type: str):
        """更新通知冷却时间"""
        self.notification_cooldown[stock_code][alert_type] = datetime.now()
    
    def generate_disguise_message(self, alert_type: str, stock_info: Dict,
                                 current_price: float, threshold: float = None, *,
                                 tier_index: int = None, tier_threshold: float = None,
                                 peak_price: float = None, valley_price: float = None,
                                 daily_change: float = None, speed_change: float = None,
                                 retrace_pct: float = None, bounce_pct: float = None,
                                 t_type: str = None, t_price: float = None,
                                 t_threshold: float = None,) -> str:
        import random

        template = random.choice(self.disguise_templates[alert_type])

        price_str = f"{current_price:.2f}"
        threshold_str = f"{threshold:.2f}" if threshold is not None else ""

        name_val = (stock_info.get('name') or '').strip()
        nickname_val = (stock_info.get('nickname') or name_val).strip()

        message = template.format(
            name=name_val,
            nickname=nickname_val,
            price=price_str,
            threshold=threshold_str,
            time=str(stock_info.get('speed_window', 5)),
            tier_index=str(tier_index) if tier_index is not None else "",
            tier_threshold=f"{tier_threshold:.2f}%" if tier_threshold is not None else "",
            peak_price=f"{peak_price:.2f}" if peak_price is not None else "",
            valley_price=f"{valley_price:.2f}" if valley_price is not None else "",
            daily_change=f"{daily_change:+.2f}%" if daily_change is not None else "",
            speed_change=f"{speed_change:+.2f}%" if speed_change is not None else "",
            retracement=f"{retrace_pct:+.2f}%" if retrace_pct is not None else "",
            bounce=f"{bounce_pct:+.2f}%" if bounce_pct is not None else "",
            t_type=str(t_type) if t_type is not None else "",
            t_price=f"{t_price:.2f}" if t_price is not None else "",
            t_threshold=f"{t_threshold:.2f}%" if t_threshold is not None else "",
        )

        message += '.'
        return message
    
    def send_dingding_notification(self, message: str):
        """
        发送钉钉通知
        
        Args:
            message: 通知消息
        """
        if self._batch_mode:
            self._alert_buffer.append(message)
            return
        self._do_send(message)

    def _do_send(self, message: str):
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
                    "content": message
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
                logger.info(f"钉钉通知发送成功: {message[:50]}...")
            else:
                logger.error(f"钉钉通知发送失败: {response.status_code}")
                
        except Exception as e:
            logger.error(f"发送钉钉通知异常: {e}")
    
    def flush_alerts(self):
        if not self._alert_buffer:
            return
        message = '\n'.join(self._alert_buffer)
        self._alert_buffer.clear()
        self._do_send(message)

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

            for idx, tier_pct in enumerate(change_high, start=1):
                if daily_change_pct >= tier_pct:
                    if idx not in alerted_daily_high:
                        cooldown_key = f'daily_up_tier_{idx}'
                        if 'daily_up' not in disabled and self.check_cooldown(stock_code, cooldown_key):
                            self.send_dingding_notification(
                                self.generate_disguise_message(
                                    'daily_up', config, current_price,
                                    threshold=tier_pct,
                                    tier_index=idx, tier_threshold=tier_pct,
                                    daily_change=daily_change_pct,
                                )
                            )
                            self.update_cooldown(stock_code, cooldown_key)
                        alerted_daily_high.add(idx)
                        self.retracement_armed[stock_code] = True
                        self.peak_since_high_alert[stock_code] = current_price

            for idx, tier_pct in enumerate(change_low, start=1):
                if daily_change_pct <= -tier_pct:
                    if idx not in alerted_daily_low:
                        cooldown_key = f'daily_down_tier_{idx}'
                        if 'daily_down' not in disabled and self.check_cooldown(stock_code, cooldown_key):
                            self.send_dingding_notification(
                                self.generate_disguise_message(
                                    'daily_down', config, current_price,
                                    threshold=tier_pct,
                                    tier_index=idx, tier_threshold=tier_pct,
                                    daily_change=daily_change_pct,
                                )
                            )
                            self.update_cooldown(stock_code, cooldown_key)
                        alerted_daily_low.add(idx)
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
                                daily_change=t_daily,
                            )
                        )
                        self.update_cooldown(stock_code, alert_type)
                    continue  # 事件触发，移除
            remaining.append(ev)
        if len(remaining) != len(events):
            self.t_events[stock_code] = remaining

    def daily_reset(self, stock_t_events: dict[str, list[dict]]):
        """每日重置所有通知状态，重新加载 T 事件"""
        self.notification_cooldown.clear()
        self.price_alert_status.clear()
        self.price_high_alerted_abs.clear()
        self.price_low_alerted_abs.clear()
        self.price_high_alerted_daily.clear()
        self.price_low_alerted_daily.clear()
        self.peak_since_high_alert.clear()
        self.valley_since_low_alert.clear()
        self.retracement_armed.clear()
        self.bounce_armed.clear()
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
        """监控循环（同一轮次多条通知合为一条发送，避免钉钉限流）"""
        logger.info("开始股票监控...")

        while self.running:
            try:
                sleep_seconds = StockMonitor._seconds_until_next_check(interval_seconds)
                if sleep_seconds == interval_seconds:
                    self._batch_mode = True
                    self._alert_buffer.clear()
                    codes = list(self.stocks.keys())
                    if codes:
                        prices = self.fetch_batch_prices(codes)
                        for code in codes:
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
