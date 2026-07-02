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
    "t_sell": ["🔻 {name} 做T可买回：{t_price}→{price}（跌{t_threshold}%）"],
    "t_buy": ["🟢 {name} 做T可卖出：{t_price}→{price}（涨{t_threshold}%）"],
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
        return cls(
            code=d["code"],
            name=d["name"],
            nickname=d.get("nickname", ""),
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
            t_threshold=d.get("t_threshold"),
            t_events=list(d.get("t_events", [])),
            t_s_enabled=bool(d.get("t_s_enabled", True)),
            t_b_enabled=bool(d.get("t_b_enabled", True)),
            disabled_alerts=list(d.get("disabled_alerts", [])),
        )


@dataclass
class Config:
    dingding_webhook: str = ""
    at_mobiles: list[str] = field(default_factory=list)
    at_user_ids: list[str] = field(default_factory=list)
    poll_interval_seconds: int = 30
    disguise_templates: dict[str, list[str]] = field(default_factory=dict)
    stocks: list[StockConfig] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "dingding_webhook": self.dingding_webhook,
            "at_mobiles": list(self.at_mobiles),
            "at_user_ids": list(self.at_user_ids),
            "poll_interval_seconds": self.poll_interval_seconds,
            "disguise_templates": self.disguise_templates,
            "stocks": [s.to_dict() for s in self.stocks],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        loaded_templates = d.get("disguise_templates", {})
        merged_templates = dict(DEFAULT_TEMPLATES)
        merged_templates.update(loaded_templates)
        return cls(
            dingding_webhook=d.get("dingding_webhook", ""),
            at_mobiles=list(d.get("at_mobiles", [])),
            at_user_ids=list(d.get("at_user_ids", [])),
            poll_interval_seconds=int(d.get("poll_interval_seconds", 30)),
            disguise_templates=merged_templates,
            stocks=[StockConfig.from_dict(x) for x in d.get("stocks", [])],
        )

    def find_stock(self, code: str) -> Optional[StockConfig]:
        for s in self.stocks:
            if s.code == code:
                return s
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
            stocks=list(DEFAULT_STOCKS),
        )
        return cfg
