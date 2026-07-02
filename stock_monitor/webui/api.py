"""REST API endpoints"""
from __future__ import annotations

import calendar as cal_mod
import datetime
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from ..config import Config, ConfigStore, StockConfig
from ..manager import MonitorManager

logger = logging.getLogger(__name__)


# ===== Pydantic schemas =====

class StockIn(BaseModel):
    code: str = Field(..., min_length=1, max_length=16)
    name: str
    nickname: str = ""
    price_high: Optional[float] = None
    price_low: Optional[float] = None
    speed_threshold: Optional[float] = None
    speed_window: int = 5
    cooldown_minutes: int = 5
    enabled: bool = True
    daily_change_up: list[float] = Field(default_factory=list)
    daily_change_down: list[float] = Field(default_factory=list)
    retracement_threshold: Optional[float] = None
    bounce_threshold: Optional[float] = None
    t_threshold: Optional[float] = None
    t_events: list[dict] = Field(default_factory=list)
    t_s_enabled: bool = True
    t_b_enabled: bool = True
    disabled_alerts: list[str] = Field(default_factory=list)


class StockPatch(BaseModel):
    enabled: bool


class WebhookIn(BaseModel):
    webhook: str = ""
    at_mobiles: list[str] = Field(default_factory=list)
    at_user_ids: list[str] = Field(default_factory=list)


class TemplatesIn(BaseModel):
    templates: dict[str, list[str]]


class TestNotifyIn(BaseModel):
    message: str = ""


class TemplatePreviewIn(BaseModel):
    template: str
    alert_type: str
    stock_code: Optional[str] = None


class TEventIn(BaseModel):
    type: str  # "S" or "B"
    price: float
    target_price: Optional[float] = None


class StatusOut(BaseModel):
    running: bool
    check_count: int
    alert_count: int
    last_check_at: Optional[float]
    last_alert_at: Optional[float]
    started_at: Optional[float]
    last_error: Optional[str]
    poll_interval_seconds: int = 30
    stocks: list[dict]
    config_path: str


# ===== 工具 =====

def _mask_webhook(url: str) -> str:
    if not url:
        return ""
    # 保留协议 + host，token 段打码
    if "access_token=" in url:
        head, _, token = url.partition("access_token=")
        return f"{head}access_token=****"
    return url[:8] + "****" if len(url) > 12 else "****"


def _build_template_sample(stock, alert_type: str) -> dict:
    """为模板预览生成样例占位符值"""
    base: dict = {
        "name": "示例股票",
        "nickname": "示例股票",
        "price": "10.00",
        "threshold": "10.00",
        "time": "5",
        "tier_index": "1",
        "tier_threshold": "5.00%",
        "peak_price": "10.50",
        "valley_price": "9.50",
        "daily_change": "+1.50%",
        "speed_change": "+1.50%",
        "retracement": "-3.00%",
        "bounce": "+3.00%",
        "t_type": "S",
        "t_price": "10.00",
        "t_threshold": "3.00%",
    }
    if stock is not None:
        base["name"] = stock.name
        base["nickname"] = stock.nickname or stock.name
        base["time"] = str(stock.speed_window)
        base["tier_threshold"] = ""
        base["peak_price"] = ""
        base["valley_price"] = ""
        base["daily_change"] = ""
        base["speed_change"] = ""
        base["retracement"] = ""
        base["bounce"] = ""
        base["t_type"] = ""
        base["t_price"] = ""
        base["t_threshold"] = ""

    if alert_type == "price_high":
        threshold = stock.price_high if stock is not None and stock.price_high is not None else 10.0
        base["threshold"] = f"{threshold:.2f}"
        base["price"] = f"{threshold + 0.5:.2f}"
        base["daily_change"] = "+3.00%"
    elif alert_type == "price_low":
        threshold = stock.price_low if stock is not None and stock.price_low is not None else 5.0
        base["threshold"] = f"{threshold:.2f}"
        base["price"] = f"{threshold - 0.5:.2f}"
        base["daily_change"] = "-3.00%"
    elif alert_type == "daily_up":
        base["daily_change"] = "+5.00%"
        base["tier_threshold"] = "5.00%"
        base["tier_index"] = "1"
        base["threshold"] = ""
        base["price"] = "10.00"
    elif alert_type == "daily_down":
        base["daily_change"] = "-5.00%"
        base["tier_threshold"] = "5.00%"
        base["tier_index"] = "1"
        base["threshold"] = ""
        base["price"] = "10.00"
    elif alert_type in ("surge_up", "surge_down"):
        th = stock.speed_threshold if stock is not None and stock.speed_threshold is not None else 2.5
        sign = "+" if alert_type == "surge_up" else "-"
        base["speed_change"] = f"{sign}{th + 0.5:.2f}%"
        base["daily_change"] = "+2.00%"
        base["threshold"] = ""
        base["price"] = "10.00"
    elif alert_type == "retracement":
        base["retracement"] = "-3.00%"
        base["daily_change"] = "-2.00%"
        base["threshold"] = ""
        base["price"] = "10.00"
        base["peak_price"] = "10.50"
    elif alert_type == "bounce":
        base["bounce"] = "+3.00%"
        base["daily_change"] = "+2.00%"
        base["threshold"] = ""
        base["price"] = "10.00"
        base["valley_price"] = "9.50"
    elif alert_type == "t_sell":
        th = stock.t_threshold if stock is not None and stock.t_threshold is not None else 3.0
        base["t_type"] = "S"
        base["t_price"] = "10.00"
        base["t_threshold"] = f"{th:.2f}%"
        base["price"] = f"{10.0 * (1 - th / 100):.2f}"
        base["daily_change"] = "-2.00%"
        base["threshold"] = ""
    elif alert_type == "t_buy":
        th = stock.t_threshold if stock is not None and stock.t_threshold is not None else 3.0
        base["t_type"] = "B"
        base["t_price"] = "10.00"
        base["t_threshold"] = f"{th:.2f}%"
        base["price"] = f"{10.0 * (1 + th / 100):.2f}"
        base["daily_change"] = "+2.00%"
        base["threshold"] = ""
    return base


# ===== 路由注册 =====

def register_routes(app, manager: MonitorManager, store: ConfigStore):
    router = APIRouter(prefix="/api")

    @router.get("/config")
    def get_config():
        cfg = manager.get_config()
        return {
            "dingding_webhook": _mask_webhook(cfg.dingding_webhook),
            "dingding_webhook_set": bool(cfg.dingding_webhook),
            "disguise_templates": cfg.disguise_templates,
            "stocks": [s.to_dict() for s in cfg.stocks],
        }

    @router.put("/config")
    def replace_config(payload: dict):
        try:
            new_cfg = Config.from_dict(payload)
        except Exception as e:
            raise HTTPException(400, f"配置解析失败: {e}")
        manager.replace_config(new_cfg)
        return {"ok": True}

    # ----- 股票 -----
    @router.get("/stocks")
    def list_stocks():
        quotes = manager.get_quotes()
        return [{
            **s.to_dict(),
            "quote": quotes.get(s.code, {"price": None, "change_percent": None, "as_of": None}),
        } for s in manager.get_config().stocks]

    @router.get("/stocks/search")
    def search_stocks(q: str = Query(..., min_length=1)):
        import requests as _requests
        try:
            url = f"https://suggest3.sinajs.cn/suggest/type=11&key={_requests.utils.quote(q)}"
            headers = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
            resp = _requests.get(url, headers=headers, timeout=5)
            resp.encoding = "gbk"
            text = resp.text.strip()
            # var suggestvalue="..."
            start = text.index('"')
            end = text.rindex('"')
            raw = text[start+1:end]
            results = []
            for part in raw.split(";"):
                fields = part.split(",")
                if len(fields) >= 5:
                    code = fields[3].strip()  # full code e.g. sh600519
                    name = fields[4].strip()
                    if code and name:
                        results.append({"code": code, "name": name})
            return results
        except Exception as e:
            logger.warning(f"股票搜索失败: {e}")
            return []

    @router.post("/stocks", status_code=201)
    def create_stock(stock: StockIn):
        sc = StockConfig(**stock.model_dump())
        if manager.get_config().find_stock(sc.code) is not None:
            raise HTTPException(409, f"股票代码 {sc.code} 已存在")
        manager.upsert_stock(sc)
        return sc.to_dict()

    @router.put("/stocks/{code}")
    def update_stock(code: str, stock: StockIn):
        if code != stock.code:
            raise HTTPException(400, "URL 中的 code 与 body 中的 code 不一致")
        sc = StockConfig(**stock.model_dump())
        # 保留现有 T 事件（UI 表单不发送 t_events，避免清空）
        existing = manager.get_config().find_stock(code)
        if existing is not None and existing.t_events:
            sc.t_events = list(existing.t_events)
        manager.upsert_stock(sc)
        return sc.to_dict()

    @router.delete("/stocks/{code}")
    def delete_stock(code: str):
        if not manager.delete_stock(code):
            raise HTTPException(404, f"股票 {code} 不存在")
        return {"ok": True}

    @router.patch("/stocks/{code}/enabled")
    def patch_enabled(code: str, patch: StockPatch):
        if not manager.patch_stock_enabled(code, patch.enabled):
            raise HTTPException(404, f"股票 {code} 不存在")
        return {"ok": True}

    # ----- 做T事件 -----
    @router.post("/stocks/{code}/t-events", status_code=201)
    def add_t_event(code: str, payload: TEventIn):
        if payload.type not in ("S", "B"):
            raise HTTPException(400, "type 必须为 S 或 B")
        event = manager.add_t_event(code, payload.type, payload.price)
        return event

    @router.put("/stocks/{code}/t-events/{event_id}")
    def update_t_event(code: str, event_id: str, payload: TEventIn):
        event = manager.update_t_event(code, event_id, price=payload.price, target_price=payload.target_price)
        if event is None:
            raise HTTPException(404, f"事件 {event_id} 不存在")
        return event

    @router.delete("/stocks/{code}/t-events/{event_id}")
    def delete_t_event(code: str, event_id: str):
        if not manager.remove_t_event(code, event_id):
            raise HTTPException(404, f"事件 {event_id} 不存在")
        return {"ok": True}

    # ----- 模板 -----
    @router.get("/templates")
    def get_templates():
        return manager.get_config().disguise_templates

    @router.put("/templates")
    def put_templates(payload: TemplatesIn):
        manager.update_templates(payload.templates)
        return {"ok": True}

    @router.post("/templates/preview")
    def preview_template(payload: TemplatePreviewIn):
        VALID_TYPES = ("price_high", "price_low", "daily_up", "daily_down", "surge_up", "surge_down", "retracement", "bounce", "t_sell", "t_buy")
        if payload.alert_type not in VALID_TYPES:
            raise HTTPException(400, f"未知 alert_type: {payload.alert_type}")
        cfg = manager.get_config()
        stock = None
        if payload.stock_code:
            stock = cfg.find_stock(payload.stock_code)
        if stock is None:
            for s in cfg.stocks:
                if s.enabled:
                    stock = s
                    break
        sample = _build_template_sample(stock, payload.alert_type)
        try:
            rendered = payload.template.format(**sample)
        except KeyError as e:
            raise HTTPException(400, f"未知占位符: {{{e.args[0]}}}")
        return {
            "rendered": rendered,
            "sample": sample,
            "stock_code": stock.code if stock else None,
            "stock_name": stock.name if stock else None,
        }

    # ----- Webhook -----
    @router.get("/settings/webhook")
    def get_webhook():
        cfg = manager.get_config()
        return {
            "webhook": _mask_webhook(cfg.dingding_webhook),
            "set": bool(cfg.dingding_webhook),
            "at_mobiles": list(cfg.at_mobiles),
            "at_user_ids": list(cfg.at_user_ids),
        }

    @router.put("/settings/webhook")
    def put_webhook(payload: WebhookIn):
        manager.update_webhook(payload.webhook, at_mobiles=payload.at_mobiles, at_user_ids=payload.at_user_ids)
        return {"ok": True}

    # ----- 状态 -----
    @router.get("/status")
    def get_status():
        return manager.status()

    # ----- 系统设置 -----
    @router.put("/settings/poll-interval")
    def put_poll_interval(payload: dict):
        seconds = int(payload.get("seconds", 30))
        if seconds < 5:
            raise HTTPException(400, "轮询间隔不能小于 5 秒")
        manager.update_poll_interval(seconds)
        return {"ok": True, "poll_interval_seconds": seconds}

    # ----- 动作 -----
    @router.post("/actions/test-notify")
    def test_notify(payload: TestNotifyIn = TestNotifyIn()):
        if not manager.test_notify(payload.message):
            raise HTTPException(400, "监控器未启动或 webhook 未配置")
        return {"ok": True}

    @router.post("/actions/sync-holidays")
    def sync_holidays():
        try:
            import cn_stock_holidays.data as shsz
            shsz.sync_data()
            return {"ok": True, "count": len(shsz.get_cached())}
        except Exception as e:
            raise HTTPException(500, f"同步失败: {e}")

    # ----- 交易日历 -----
    @router.get("/trading-calendar")
    def get_trading_calendar(year: int, month: int):
        import cn_stock_holidays.data as shsz
        holidays = shsz.get_cached()
        days = []
        _, last_day = cal_mod.monthrange(year, month)
        for day in range(1, last_day + 1):
            date_obj = datetime.date(year, month, day)
            is_weekend = date_obj.weekday() >= 5
            is_holiday = date_obj in holidays
            is_trading = not is_weekend and not is_holiday
            days.append({
                "day": day,
                "is_trading": is_trading,
                "is_weekend": is_weekend,
                "is_holiday": is_holiday,
            })
        return {"year": year, "month": month, "days": days}

    # ----- 导入 / 导出 -----
    @router.get("/export")
    def export_config():
        cfg = manager.get_config()
        return cfg.to_dict()

    @router.post("/import")
    async def import_config(file: UploadFile):
        import json
        try:
            raw = await file.read()
            data = json.loads(raw)
            new_cfg = Config.from_dict(data)
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"JSON 解析失败: {e}")
        except Exception as e:
            raise HTTPException(400, f"配置解析失败: {e}")
        manager.replace_config(new_cfg)
        return {"ok": True, "stocks": len(new_cfg.stocks)}

    app.include_router(router)
