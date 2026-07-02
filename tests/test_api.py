"""API endpoint 测试（使用 FastAPI TestClient）"""
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from stock_monitor.config import ConfigStore
from stock_monitor.webui.app import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_app(config_path=tmp_path / "config.json", interval_seconds=3600)
    with TestClient(app) as c:
        yield c


class TestStocksAPI:
    def test_list_empty(self, client: TestClient):
        """空列表应返回空数组"""
        r = client.get("/api/stocks")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_includes_quote_field(self, client: TestClient):
        """股票列表应包含 quote 字段"""
        client.post("/api/stocks", json={"code": "sz1", "name": "A"})
        s = client.get("/api/stocks").json()[0]
        assert "quote" in s
        assert s["quote"] == {"price": None, "change_percent": None, "as_of": None}

    def test_create_stock(self, client: TestClient):
        """创建股票应返回完整数据"""
        payload = {
            "code": "sz300115", "name": "CY", "nickname": "我的茅台",
            "price_high": 45, "price_low": 42.5,
            "speed_threshold": 2.5,
        }
        r = client.post("/api/stocks", json=payload)
        assert r.status_code == 201
        data = r.json()
        assert data["code"] == "sz300115"
        assert data["name"] == "CY"
        assert data["nickname"] == "我的茅台"

    def test_create_duplicate(self, client: TestClient):
        """重复创建应返回 409"""
        payload = {"code": "sz1", "name": "A"}
        client.post("/api/stocks", json=payload)
        r = client.post("/api/stocks", json=payload)
        assert r.status_code == 409

    def test_update_stock(self, client: TestClient):
        """更新股票应生效"""
        client.post("/api/stocks", json={"code": "sz1", "name": "A"})
        r = client.put("/api/stocks/sz1", json={"code": "sz1", "name": "A2", "price_high": 99, "nickname": "阿A"})
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "A2"
        assert data["price_high"] == 99
        assert data["nickname"] == "阿A"

    def test_update_code_mismatch(self, client: TestClient):
        """URL 路径 code 与请求体 code 不一致应返回 400"""
        client.post("/api/stocks", json={"code": "sz1", "name": "A"})
        r = client.put("/api/stocks/sz1", json={"code": "sz2", "name": "A"})
        assert r.status_code == 400

    def test_delete(self, client: TestClient):
        """删除股票后列表应变空"""
        client.post("/api/stocks", json={"code": "sz1", "name": "A"})
        r = client.delete("/api/stocks/sz1")
        assert r.status_code == 200
        assert client.get("/api/stocks").json() == []

    def test_delete_nonexistent(self, client: TestClient):
        """删除不存在的股票应返回 404"""
        r = client.delete("/api/stocks/missing")
        assert r.status_code == 404

    def test_patch_enabled(self, client: TestClient):
        """通过 PATCH 禁用股票应生效"""
        client.post("/api/stocks", json={"code": "sz1", "name": "A"})
        r = client.patch("/api/stocks/sz1/enabled", json={"enabled": False})
        assert r.status_code == 200
        s = client.get("/api/stocks").json()[0]
        assert s["enabled"] is False


class TestTemplatesAPI:
    def test_get_default(self, client: TestClient):
        """默认模板应包含所有告警类型"""
        r = client.get("/api/templates")
        assert "price_high" in r.json()
        assert "daily_up" in r.json()
        assert "daily_down" in r.json()

    def test_put(self, client: TestClient):
        """更新模板后 GET 应返回新值"""
        r = client.put("/api/templates", json={"templates": {"price_high": ["🟢 {name}"]}})
        assert r.status_code == 200
        assert client.get("/api/templates").json()["price_high"] == ["🟢 {name}"]


class TestWebhookAPI:
    def test_get_empty(self, client: TestClient):
        """未设置 webhook 时 set 应为 False"""
        r = client.get("/api/settings/webhook")
        assert r.status_code == 200
        assert r.json()["set"] is False

    def test_put(self, client: TestClient):
        """设置 webhook 后应被打码返回"""
        r = client.put("/api/settings/webhook", json={"webhook": "https://example.com/x?access_token=secret"})
        assert r.status_code == 200
        # GET 应打码
        g = client.get("/api/settings/webhook").json()
        assert g["set"] is True
        assert "secret" not in g["webhook"]
        assert "****" in g["webhook"]


class TestConfigAPI:
    def test_get_masks_webhook(self, client: TestClient):
        """GET /api/config 中的 webhook 应打码"""
        client.put("/api/settings/webhook", json={"webhook": "https://x?access_token=ABCDEF"})
        cfg = client.get("/api/config").json()
        assert "ABCDEF" not in cfg["dingding_webhook"]

    def test_replace_config(self, client: TestClient):
        """整体替换配置后股票列表应更新"""
        payload = {
            "dingding_webhook": "https://x",
            "disguise_templates": {"price_high": ["T"]},
            "stocks": [{"code": "sz1", "name": "A"}],
        }
        r = client.put("/api/config", json=payload)
        assert r.status_code == 200
        assert len(client.get("/api/stocks").json()) == 1


class TestImportExport:
    def test_export(self, client: TestClient):
        """导出应包含所有股票"""
        client.post("/api/stocks", json={"code": "sz1", "name": "A"})
        r = client.get("/api/export")
        assert r.status_code == 200
        data = r.json()
        assert any(s["code"] == "sz1" for s in data["stocks"])

    def test_import(self, client: TestClient, tmp_path: Path):
        """导入配置后股票列表应更新"""
        import json
        path = tmp_path / "import.json"
        path.write_text(json.dumps({
            "stocks": [{"code": "sz2", "name": "B"}],
            "disguise_templates": {},
        }))
        with path.open("rb") as f:
            r = client.post("/api/import", files={"file": ("import.json", f, "application/json")})
        assert r.status_code == 200
        assert client.get("/api/stocks").json()[0]["code"] == "sz2"


class TestStatus:
    def test_status_shape(self, client: TestClient):
        """status 接口应返回所有必要字段"""
        r = client.get("/api/status")
        assert r.status_code == 200
        data = r.json()
        for k in ("running", "check_count", "stocks", "config_path"):
            assert k in data


class TestActions:
    def test_test_notify(self, client: TestClient):
        """测试通知接口应正常响应（不论 webhook 是否配置）"""
        r = client.post("/api/actions/test-notify",
                         json={"message": "test"})
        assert r.status_code in (200, 400)

    def test_sync_holidays(self, client: TestClient):
        """同步节假日应返回成功"""
        r = client.post("/api/actions/sync-holidays")
        assert r.status_code == 200
        assert "count" in r.json()


class TestTemplatePreview:
    def test_preview_price_high(self, client: TestClient):
        """高价告警模板预览应正确渲染占位符"""
        client.post("/api/stocks", json={
            "code": "sz1", "name": "测试股", "nickname": "我的票",
            "price_high": 45.0,
        })
        r = client.post("/api/templates/preview", json={
            "template": "🟢 {name} 突破 {threshold}",
            "alert_type": "price_high",
            "stock_code": "sz1",
        })
        assert r.status_code == 200
        data = r.json()
        assert "🟢 测试股 突破 45.00" in data["rendered"]
        assert data["stock_code"] == "sz1"

    def test_preview_name_uses_official_name_not_nickname(self, client: TestClient):
        """{name} 应使用官方名称而非昵称"""
        client.post("/api/stocks", json={"code": "sz1", "name": "测试股", "nickname": "我的票", "price_high": 10.0})
        r = client.post("/api/templates/preview", json={
            "template": "📈 {name}",
            "alert_type": "price_high",
            "stock_code": "sz1",
        })
        assert r.status_code == 200
        # {name} 应使用官方名称，非昵称
        assert "📈 测试股" in r.json()["rendered"]
        assert "我的票" not in r.json()["rendered"]

    def test_preview_nickname_uses_nickname(self, client: TestClient):
        """{nickname} 应使用自定义昵称"""
        client.post("/api/stocks", json={"code": "sz1", "name": "测试股", "nickname": "我的票", "price_high": 10.0})
        r = client.post("/api/templates/preview", json={
            "template": "📈 {nickname}",
            "alert_type": "price_high",
            "stock_code": "sz1",
        })
        assert r.status_code == 200
        assert "📈 我的票" in r.json()["rendered"]

    def test_preview_nickname_falls_back_to_name(self, client: TestClient):
        """未设置昵称时 {nickname} 应回退到官方名称"""
        client.post("/api/stocks", json={"code": "sz1", "name": "测试股", "price_high": 10.0})
        r = client.post("/api/templates/preview", json={
            "template": "📈 {nickname}",
            "alert_type": "price_high",
            "stock_code": "sz1",
        })
        assert r.status_code == 200
        assert "📈 测试股" in r.json()["rendered"]

    def test_preview_surge(self, client: TestClient):
        """涨速告警模板预览应包含涨幅百分比和时间窗口"""
        client.post("/api/stocks", json={
            "code": "sz1", "name": "A",
            "speed_threshold": 2.5, "speed_window": 5,
        })
        r = client.post("/api/templates/preview", json={
            "template": "⏫️ {name} {speed_change}({time})",
            "alert_type": "surge_up",
        })
        assert r.status_code == 200
        data = r.json()
        # 阈值+0.5, +号, speed_change 自带 % 后缀
        assert "⏫️ A +3.00%(5)" in data["rendered"]

    def test_preview_surge_down_uses_minus(self, client: TestClient):
        """下跌涨速预览应显示负号"""
        client.post("/api/stocks", json={"code": "sz1", "name": "A", "speed_threshold": 2.0})
        r = client.post("/api/templates/preview", json={
            "template": "⏬️ {name} {speed_change}",
            "alert_type": "surge_down",
        })
        assert r.status_code == 200
        assert "⏬️ A -2.50%" in r.json()["rendered"]

    def test_preview_no_stock_uses_sample(self, client: TestClient):
        """没有股票时用通用样例数据"""
        r = client.post("/api/templates/preview", json={
            "template": "📊 {name} {price}",
            "alert_type": "price_high",
        })
        assert r.status_code == 200
        data = r.json()
        assert "示例股票" in data["rendered"]
        assert data["stock_code"] is None

    def test_preview_unknown_placeholder(self, client: TestClient):
        """未知占位符应返回 400 错误"""
        r = client.post("/api/templates/preview", json={
            "template": "{nonexistent_field}",
            "alert_type": "price_high",
        })
        assert r.status_code == 400
        assert "{nonexistent_field}" in r.json()["detail"] or "占位符" in r.json()["detail"]

    def test_preview_retracement(self, client: TestClient):
        """回撤告警模板预览应正确渲染回撤百分比和峰值"""
        client.post("/api/stocks", json={"code": "sz1", "name": "A", "price_high": 45.0})
        r = client.post("/api/templates/preview", json={
            "template": "🔻 {name} 回撤 {retracement} 峰值 {peak_price}",
            "alert_type": "retracement",
            "stock_code": "sz1",
        })
        assert r.status_code == 200
        data = r.json()
        assert "🔻 A 回撤 -3.00% 峰值 10.50" in data["rendered"]

    def test_preview_bounce(self, client: TestClient):
        """反弹告警模板预览应正确渲染反弹百分比和谷值"""
        client.post("/api/stocks", json={"code": "sz1", "name": "A", "price_low": 40.0})
        r = client.post("/api/templates/preview", json={
            "template": "🟢 {name} 反弹 {bounce} 谷值 {valley_price}",
            "alert_type": "bounce",
            "stock_code": "sz1",
        })
        assert r.status_code == 200
        data = r.json()
        assert "🟢 A 反弹 +3.00% 谷值 9.50" in data["rendered"]

    def test_preview_daily_up_tier_placeholders(self, client: TestClient):
        """当日涨幅多档占位符应渲染正确档位信息"""
        client.post("/api/stocks", json={"code": "sz1", "name": "A", "daily_change_up": [5.0, 10.0]})
        r = client.post("/api/templates/preview", json={
            "template": "第{tier_index}档 {tier_threshold}",
            "alert_type": "daily_up",
            "stock_code": "sz1",
        })
        assert r.status_code == 200
        assert "第1档 5.00%" in r.json()["rendered"]

    def test_preview_daily_down_tier_placeholders(self, client: TestClient):
        """当日跌幅多档占位符应渲染正确档位信息"""
        client.post("/api/stocks", json={"code": "sz1", "name": "A", "daily_change_down": [5.0]})
        r = client.post("/api/templates/preview", json={
            "template": "第{tier_index}档 {tier_threshold}",
            "alert_type": "daily_down",
            "stock_code": "sz1",
        })
        assert r.status_code == 200
        assert "第1档 5.00%" in r.json()["rendered"]

    def test_preview_daily_up_change(self, client: TestClient):
        """当日涨幅变化量应带正号渲染"""
        client.post("/api/stocks", json={"code": "sz1", "name": "A", "daily_change_up": [5.0]})
        r = client.post("/api/templates/preview", json={
            "template": "{daily_change}",
            "alert_type": "daily_up",
            "stock_code": "sz1",
        })
        assert r.status_code == 200
        assert "+5.00%" in r.json()["rendered"]

    def test_preview_daily_down_change(self, client: TestClient):
        """当日跌幅变化量应带负号渲染"""
        client.post("/api/stocks", json={"code": "sz1", "name": "A", "daily_change_down": [5.0]})
        r = client.post("/api/templates/preview", json={
            "template": "{daily_change}",
            "alert_type": "daily_down",
            "stock_code": "sz1",
        })
        assert r.status_code == 200
        assert "-5.00%" in r.json()["rendered"]


class TestMultiTierStock:
    def test_create_with_tiers(self, client: TestClient):
        """创建含多档阈值的股票应正确存储"""
        r = client.post("/api/stocks", json={
            "code": "sz1", "name": "A", "price_high": 45.0, "price_low": 40.0,
            "daily_change_up": [50.0, 55.0],
            "daily_change_down": [38.0, 35.0],
            "retracement_threshold": 5.0,
            "bounce_threshold": 3.0,
        })
        assert r.status_code == 201
        data = r.json()
        assert data["daily_change_up"] == [50.0, 55.0]
        assert data["daily_change_down"] == [38.0, 35.0]
        assert data["retracement_threshold"] == 5.0
        assert data["bounce_threshold"] == 3.0

    def test_list_includes_tier_fields(self, client: TestClient):
        """股票列表接口应包含多档字段"""
        client.post("/api/stocks", json={
            "code": "sz1", "name": "A",
            "daily_change_up": [50.0],
        })
        r = client.get("/api/stocks")
        data = r.json()[0]
        assert "daily_change_up" in data
        assert data["daily_change_up"] == [50.0]


class TestStaticUI:
    def test_root_serves_html(self, client: TestClient):
        """根路径应返回 index.html"""
        r = client.get("/")
        assert r.status_code == 200
        # 首次访问应返回 index.html
        assert "<html" in r.text.lower() or "<!doctype" in r.text.lower()
