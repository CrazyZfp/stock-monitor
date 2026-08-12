"""配置持久化层

JSON 格式配置文件，原子写（tmp + rename），支持热重载回调。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ========== 默认配置 ==========

DEFAULT_TEMPLATES = {
    "price_high": ["🟢 {name} 突破 {threshold}"],
    "price_low": ["🔴 {name} 跌破 {threshold}"],
    "daily_up": ["📈 {name} 当日涨幅达{tier_index}档 {tier_threshold}（{daily_change}）"],
    "daily_down": ["📉 {name} 当日跌幅达{tier_index}档 {tier_threshold}（{daily_change}）"],
    "surge_up": ["⏫️ {name},{speed_change}({time})"],
    "surge_down": ["⏬️ {name},{speed_change}({time})"],
    "retracement": ["🔻 {name} 回撤 {retracement}（峰值 {peak_price}，当前 {price}）"],
    "bounce": ["🟢 {name} 反弹 {bounce}（谷值 {valley_price}，当前 {price}）"],
    "profit": ["🟢 {name} {direction}{leverage} 盈利 {profit_pct}（成本 {position_cost}，当前 {price}）"],
    "loss": ["🔴 {name} {direction}{leverage} 亏损 {profit_pct}（成本 {position_cost}，当前 {price}）"],
    "t_sell": ["🔻 {name} 做T可买回：{t_price}→{price}（跌{t_threshold}%）{t_quantity}"],
    "t_buy": ["🟢 {name} 做T可卖出：{t_price}→{price}（涨{t_threshold}%）{t_quantity}"],
    "limit_up": ["🔴 {name} 涨停 封单{sealed_lots}手 {sealed_amount}万元"],
    "limit_up_broken": ["🟡 {name} 涨停开板 现{price}"],
    "limit_up_low_seal": ["⚠️ {name} 涨停封单不足{seal_min_lots}手 现{sealed_lots}手"],
    "limit_up_exhaust": ["⚠️ {name} 涨停封单将尽 预计{seal_eta_seconds}秒耗尽"],
    "limit_down": ["🟢 {name} 跌停 封单{sealed_lots}手 {sealed_amount}万元"],
    "limit_down_broken": ["🟡 {name} 跌停开板 现{price}"],
    "limit_down_low_seal": ["⚠️ {name} 跌停封单不足{seal_min_lots}手 现{sealed_lots}手"],
    "limit_down_exhaust": ["⚠️ {name} 跌停封单将尽 预计{seal_eta_seconds}秒耗尽"],
}

DEFAULT_STOCKS: list[dict] = []


def default_config_path() -> Path:
    """根据操作系统选择合适的配置目录"""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "stock-monitor"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "stock-monitor"
    return base / "config.json"


def default_log_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "stock-monitor"
    return Path.home() / ".local" / "share" / "stock-monitor" / "logs"


# ========== 数据模型 ==========

@dataclass
class StockConfig:
    code: str
    name: str
    nickname: str = ""
    position_cost: Optional[float] = None
    price_high: Optional[float] = None
    price_low: Optional[float] = None
    speed_threshold: Optional[float] = None      # 涨速阈值（监控窗口内）
    speed_window: int = 5                          # 涨速窗口（分钟）
    cooldown_minutes: int = 5
    enabled: bool = True
    # 多档当日涨跌百分比
    daily_change_up: list[float] = field(default_factory=list)
    daily_change_down: list[float] = field(default_factory=list)
    # 回撤 / 反弹
    retracement_threshold: Optional[float] = None
    bounce_threshold: Optional[float] = None
    # 涨跌停封单告警：封单手数低于此值触发封单不足告警（空=不监控）
    limit_seal_min_lots: Optional[int] = None
    # 做T
    t_threshold: Optional[float] = None
    t_events: list[dict] = field(default_factory=list)
    t_s_enabled: bool = True
    t_b_enabled: bool = True
    # 通知类型独立开关（空列表=全部启用）
    disabled_alerts: list[str] = field(default_factory=list)

    def get_high_tiers(self) -> list[float]:
        """返回绝对价格单档阈值 (price_high)"""
        if self.price_high is not None:
            return [self.price_high]
        return []

    def get_low_tiers(self) -> list[float]:
        """返回绝对价格单档阈值 (price_low)"""
        if self.price_low is not None:
            return [self.price_low]
        return []

    def get_change_high_tiers(self) -> list[float]:
        """返回当日上涨百分比多档阈值"""
        return sorted(t for t in self.daily_change_up if t is not None)

    def get_change_low_tiers(self) -> list[float]:
        """返回当日下跌百分比多档阈值"""
        return sorted(t for t in self.daily_change_down if t is not None)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StockConfig":
        # 新旧字段名兼容
        speed_th = d.get("speed_threshold") or d.get("surge_threshold")
        speed_win = d.get("speed_window") or d.get("surge_period", 5)
        dc_up = d.get("daily_change_up") or d.get("price_tiers_high", [])
        dc_down = d.get("daily_change_down") or d.get("price_tiers_low", [])
        # 迁移旧版字符串 created_at → int 时间戳
        t_events_raw = d.get("t_events", [])
        t_events = []
        for ev in t_events_raw:
            ev = dict(ev)
            ca = ev.get("created_at")
            if isinstance(ca, str):
                try:
                    dt = datetime.strptime(ca, "%Y-%m-%d %H:%M:%S")
                    ev["created_at"] = int(dt.timestamp())
                except ValueError:
                    ev["created_at"] = None
            t_events.append(ev)

        return cls(
            code=d["code"],
            name=d["name"],
            nickname=d.get("nickname", ""),
            position_cost=d.get("position_cost"),
            price_high=d.get("price_high"),
            price_low=d.get("price_low"),
            speed_threshold=speed_th,
            speed_window=int(speed_win) if speed_win is not None else 5,
            cooldown_minutes=int(d.get("cooldown_minutes", 5)),
            enabled=bool(d.get("enabled", True)),
            daily_change_up=list(dc_up) if dc_up is not None else [],
            daily_change_down=list(dc_down) if dc_down is not None else [],
            retracement_threshold=d.get("retracement_threshold"),
            bounce_threshold=d.get("bounce_threshold"),
            limit_seal_min_lots=d.get("limit_seal_min_lots"),
            t_threshold=d.get("t_threshold"),
            t_events=t_events,
            t_s_enabled=bool(d.get("t_s_enabled", True)),
            t_b_enabled=bool(d.get("t_b_enabled", True)),
            disabled_alerts=list(d.get("disabled_alerts", [])),
        )


@dataclass
class FundConfig:
    code: str
    name: str
    nickname: str = ""
    position_cost: Optional[float] = None
    cooldown_minutes: int = 5
    enabled: bool = True
    daily_change_up: list[float] = field(default_factory=list)
    daily_change_down: list[float] = field(default_factory=list)
    retracement_threshold: Optional[float] = None
    bounce_threshold: Optional[float] = None
    disabled_alerts: list[str] = field(default_factory=list)

    def get_change_high_tiers(self) -> list[float]:
        return sorted(t for t in self.daily_change_up if t is not None)

    def get_change_low_tiers(self) -> list[float]:
        return sorted(t for t in self.daily_change_down if t is not None)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FundConfig":
        return cls(
            code=d["code"],
            name=d["name"],
            nickname=d.get("nickname", ""),
            position_cost=d.get("position_cost"),
            cooldown_minutes=int(d.get("cooldown_minutes", 5)),
            enabled=bool(d.get("enabled", True)),
            daily_change_up=list(d.get("daily_change_up", [])),
            daily_change_down=list(d.get("daily_change_down", [])),
            retracement_threshold=d.get("retracement_threshold"),
            bounce_threshold=d.get("bounce_threshold"),
            disabled_alerts=list(d.get("disabled_alerts", [])),
        )


@dataclass
class CryptoConfig:
    code: str          # "fapi:BTCUSDT" 或 "dapi:BTCUSD_PERP"
    name: str
    nickname: str = ""
    position_cost: Optional[float] = None
    direction: str = "long"   # "long"(多) / "short"(空)
    leverage: Optional[float] = None   # 倍率，空=1
    price_high: Optional[float] = None
    price_low: Optional[float] = None
    daily_change_up: list[float] = field(default_factory=list)
    daily_change_down: list[float] = field(default_factory=list)
    cooldown_minutes: int = 5
    enabled: bool = True
    t_threshold: Optional[float] = None
    t_events: list[dict] = field(default_factory=list)
    t_s_enabled: bool = True
    t_b_enabled: bool = True
    disabled_alerts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CryptoConfig":
        t_events_raw = d.get("t_events", [])
        t_events = []
        for ev in t_events_raw:
            ev = dict(ev)
            ca = ev.get("created_at")
            if isinstance(ca, str):
                try:
                    dt = datetime.strptime(ca, "%Y-%m-%d %H:%M:%S")
                    ev["created_at"] = int(dt.timestamp())
                except ValueError:
                    ev["created_at"] = None
            t_events.append(ev)
        direction = d.get("direction", "long")
        if direction not in ("long", "short"):
            direction = "long"
        return cls(
            code=d["code"],
            name=d["name"],
            nickname=d.get("nickname", ""),
            position_cost=d.get("position_cost"),
            direction=direction,
            leverage=d.get("leverage"),
            price_high=d.get("price_high"),
            price_low=d.get("price_low"),
            daily_change_up=list(d.get("daily_change_up", [])),
            daily_change_down=list(d.get("daily_change_down", [])),
            cooldown_minutes=int(d.get("cooldown_minutes", 5)),
            enabled=bool(d.get("enabled", True)),
            t_threshold=d.get("t_threshold"),
            t_events=t_events,
            t_s_enabled=bool(d.get("t_s_enabled", True)),
            t_b_enabled=bool(d.get("t_b_enabled", True)),
            disabled_alerts=list(d.get("disabled_alerts", [])),
        )


# ========== 通知市场 ==========
# 模板按市场区分：stock / fund / crypto；未配置的 alert_type 回退到全局基础模板
TEMPLATE_MARKETS = ("stock", "fund", "crypto")

# 通知模式
NOTIFY_MODES = ("multi", "single")
# 通知通道
NOTIFY_CHANNELS = ("dingding", "email")
# 通道默认优先级（single 模式按此顺序回退）
DEFAULT_NOTIFY_PRIORITY = ["dingding", "email"]


@dataclass
class Config:
    # ===== 通知通道 =====
    notify_mode: str = "single"               # "multi"(全发) | "single"(优先级回退)
    notify_channels: dict[str, bool] = field(default_factory=lambda: {"dingding": True, "email": False})
    notify_priority: list[str] = field(default_factory=lambda: list(DEFAULT_NOTIFY_PRIORITY))
    # 钉钉
    dingding_webhook: str = ""
    dingding_keyword: str = ""               # 非空时，钉钉每条通知开头拼接该关键词
    at_mobiles: list[str] = field(default_factory=list)
    at_user_ids: list[str] = field(default_factory=list)
    # 邮箱（SMTP）
    email_smtp_host: str = ""                  # 如 smtp.qq.com
    email_smtp_port: int = 465                 # 465→SSL, 587→STARTTLS
    email_username: str = ""                   # 发件邮箱
    email_password: str = ""                   # SMTP 授权码
    email_from_addr: str = ""                  # 发件地址，默认同 username
    email_to_addrs: list[str] = field(default_factory=list)
    email_use_ssl: bool = True                 # True→SMTP_SSL, False→SMTP+STARTTLS
    # ===== 全局参数 =====
    poll_interval_seconds: int = 30
    # 涨跌停封单将尽告警参数
    limit_seal_exhaust_seconds: int = 30       # 预测封单将在多少秒内耗尽时告警
    limit_seal_exhaust_samples: int = 3        # 计算平均消耗速度的轮询周期数
    # ===== 通知模板 =====
    disguise_templates: dict[str, list[str]] = field(default_factory=dict)            # 全局基础模板
    market_templates: dict[str, dict[str, list[str]]] = field(default_factory=dict)  # 按市场覆盖
    # ===== 标的 =====
    stocks: list[StockConfig] = field(default_factory=list)
    funds: list[FundConfig] = field(default_factory=list)
    cryptos: list[CryptoConfig] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "notify_mode": self.notify_mode,
            "notify_channels": dict(self.notify_channels),
            "notify_priority": list(self.notify_priority),
            "dingding_webhook": self.dingding_webhook,
            "dingding_keyword": self.dingding_keyword,
            "at_mobiles": list(self.at_mobiles),
            "at_user_ids": list(self.at_user_ids),
            "email_smtp_host": self.email_smtp_host,
            "email_smtp_port": self.email_smtp_port,
            "email_username": self.email_username,
            "email_password": self.email_password,
            "email_from_addr": self.email_from_addr,
            "email_to_addrs": list(self.email_to_addrs),
            "email_use_ssl": self.email_use_ssl,
            "poll_interval_seconds": self.poll_interval_seconds,
            "limit_seal_exhaust_seconds": self.limit_seal_exhaust_seconds,
            "limit_seal_exhaust_samples": self.limit_seal_exhaust_samples,
            "disguise_templates": self.disguise_templates,
            "market_templates": self.market_templates,
            "stocks": [s.to_dict() for s in self.stocks],
            "funds": [f.to_dict() for f in self.funds],
            "cryptos": [c.to_dict() for c in self.cryptos],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        loaded_templates = d.get("disguise_templates", {})
        merged_templates = dict(DEFAULT_TEMPLATES)
        merged_templates.update(loaded_templates)
        # 市场模板：保留三个 market key，缺失补空 dict
        loaded_market = d.get("market_templates", {})
        normalized_market: dict[str, dict[str, list[str]]] = {}
        for mkt in TEMPLATE_MARKETS:
            tmap = loaded_market.get(mkt) or {}
            normalized_market[mkt] = {
                at: list(t) for at, t in tmap.items() if isinstance(t, list)
            }
        channel = d.get("notify_channel", "dingding")
        if channel not in NOTIFY_CHANNELS:
            channel = "dingding"
        # 通知模式：multi(全发) / single(优先级回退)
        mode = d.get("notify_mode", "single")
        if mode not in NOTIFY_MODES:
            mode = "single"
        # 通道开关：缺失时从旧 notify_channel 迁移（单一开启）
        channels_raw = d.get("notify_channels", {})
        if channels_raw:
            notify_channels = {ch: bool(channels_raw.get(ch, False)) for ch in NOTIFY_CHANNELS}
        else:
            other = "email" if channel == "dingding" else "dingding"
            notify_channels = {channel: True, other: False}
        # 优先级：缺失时 = 默认优先级；校验补齐缺失通道
        priority_raw = d.get("notify_priority") or list(DEFAULT_NOTIFY_PRIORITY)
        notify_priority = [p for p in priority_raw if p in NOTIFY_CHANNELS]
        for ch in NOTIFY_CHANNELS:
            if ch not in notify_priority:
                notify_priority.append(ch)
        return cls(
            notify_mode=mode,
            notify_channels=notify_channels,
            notify_priority=notify_priority,
            dingding_webhook=d.get("dingding_webhook", ""),
            dingding_keyword=d.get("dingding_keyword", ""),
            at_mobiles=list(d.get("at_mobiles", [])),
            at_user_ids=list(d.get("at_user_ids", [])),
            email_smtp_host=d.get("email_smtp_host", ""),
            email_smtp_port=int(d.get("email_smtp_port", 465)),
            email_username=d.get("email_username", ""),
            email_password=d.get("email_password", ""),
            email_from_addr=d.get("email_from_addr", ""),
            email_to_addrs=list(d.get("email_to_addrs", [])),
            email_use_ssl=bool(d.get("email_use_ssl", True)),
            poll_interval_seconds=int(d.get("poll_interval_seconds", 30)),
            limit_seal_exhaust_seconds=int(d.get("limit_seal_exhaust_seconds", 30)),
            limit_seal_exhaust_samples=int(d.get("limit_seal_exhaust_samples", 3)),
            disguise_templates=merged_templates,
            market_templates=normalized_market,
            stocks=[StockConfig.from_dict(x) for x in d.get("stocks", [])],
            funds=[FundConfig.from_dict(x) for x in d.get("funds", [])],
            cryptos=[CryptoConfig.from_dict(x) for x in d.get("cryptos", [])],
        )

    def get_templates_for(self, market: str, alert_type: str) -> list[str]:
        """取模板：市场覆盖优先，空列表/缺失回退全局基础模板。"""
        mkt_map = self.market_templates.get(market) or {}
        tpls = mkt_map.get(alert_type)
        if tpls:
            return list(tpls)
        return list(self.disguise_templates.get(alert_type, []))

    def channel_ready(self, channel: str) -> bool:
        """通道是否已开启且配置完整可发送。"""
        if not self.notify_channels.get(channel):
            return False
        if channel == "dingding":
            return bool(self.dingding_webhook)
        if channel == "email":
            return bool(self.email_smtp_host and self.email_username and self.email_to_addrs)
        return False

    def find_stock(self, code: str) -> Optional[StockConfig]:
        for s in self.stocks:
            if s.code == code:
                return s
        return None

    def find_fund(self, code: str) -> Optional[FundConfig]:
        for f in self.funds:
            if f.code == code:
                return f
        return None

    def find_crypto(self, code: str) -> Optional[CryptoConfig]:
        for c in self.cryptos:
            if c.code == code:
                return c
        return None


# ========== 存储 ==========

class ConfigStore:
    """JSON 文件持久化 + 变更回调

    所有写操作：① 原子写入 ② 触发 on_change 回调（用于 monitor 热重载）
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_config_path()
        self._lock = threading.RLock()
        self._config: Optional[Config] = None
        self._listeners: list[Callable[[Config], None]] = []

    # ----- 读取 -----
    def load(self) -> Config:
        """读取配置：文件不存在则用默认值；JSON 损坏则抛错"""
        with self._lock:
            if not self.path.exists():
                logger.info(f"配置文件不存在: {self.path}, 使用默认配置")
                cfg = self._default_with_env()
                self._config = cfg
                return cfg
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    raw = json.load(f)
            except json.JSONDecodeError as e:
                raise ValueError(f"配置文件 JSON 损坏: {self.path}: {e}") from e
            cfg = Config.from_dict(raw)
            # 环境变量回填：若文件未设 webhook 但环境变量有
            if not cfg.dingding_webhook:
                env = os.environ.get("DINGDING_WEBHOOK")
                if env:
                    cfg.dingding_webhook = env
            self._config = cfg
            return cfg

    def get(self) -> Config:
        """获取当前内存中的配置（首次会触发 load）"""
        with self._lock:
            if self._config is None:
                self._config = self.load()
            return self._config

    # ----- 写入 -----
    def save(self, cfg: Config):
        """原子写：写 .tmp 再 rename"""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(cfg.to_dict(), f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            self._config = cfg
            self._fire(cfg)

    def update(self, mutator: Callable[[Config], None]):
        """读-改-写模式：mutator 接受 cfg 实例，原地修改后自动持久化"""
        with self._lock:
            cfg = self.get()
            mutator(cfg)
            self.save(cfg)

    # ----- 变更订阅 -----
    def on_change(self, cb: Callable[[Config], None]):
        self._listeners.append(cb)

    def _fire(self, cfg: Config):
        for cb in list(self._listeners):
            try:
                cb(cfg)
            except Exception as e:
                logger.error(f"配置变更回调失败: {e}", exc_info=True)

    # ----- helpers -----
    def _default_with_env(self) -> Config:
        cfg = Config(
            dingding_webhook=os.environ.get("DINGDING_WEBHOOK", ""),
            disguise_templates=DEFAULT_TEMPLATES,
            market_templates={mkt: {} for mkt in TEMPLATE_MARKETS},
            stocks=list(DEFAULT_STOCKS),
        )
        return cfg
