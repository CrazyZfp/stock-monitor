# Stock Monitor

A 股股价监控 + 钉钉机器人通知 + Web 管理界面。

## 功能特性

- **实时轮询**：通过新浪财经 API 批量获取股价，盘中按可配置间隔检查
- **智能休眠**：非交易时段自动休眠到下一个开盘时间（节假日至节后）
- **多类型告警**：
  - 绝对价格阈值（高价/低价）
  - 当日涨跌幅多档
  - 涨速监控（窗口内快速变动）
  - 高位回撤 / 低位反弹
  - T+0 做T 事件（先卖后买 / 先买后卖）
- **钉钉bot Webhook通知**：通知模板可自定义，通过钉钉机器人发送告警消息
- **Web 管理界面**：股票 CRUD、模板编辑、Webhook 配置、状态监控
- **交易日历**：自动识别交易日（含法定节假日）
- **服务管理**：支持 `systemd --user`（Linux）和 `launchd`（macOS）

## 快速开始

### 依赖

- Python >= 3.13
- [uv](https://docs.astral.sh/uv/)（推荐）或 pip

### 安装

```bash
git clone <repo-url> stock-monitor
cd stock-monitor
uv sync
```

### 配置

通过 Web UI 设置钉钉 Webhook、通知模板和股票监控参数。
配置文件存储在 `~/.config/stock-monitor/config.json`，也可直接编辑。

### 运行

```bash
# CLI 模式（终端直接运行）
uv run stock-monitor

# Web UI 模式
uv run uvicorn stock_monitor.webui.app:create_app --factory --host 0.0.0.0 --port 8765
```

打开浏览器访问 `http://localhost:8765` 进入管理界面。

### 服务管理

```bash
# Linux (systemd --user)
bash service.sh install

# macOS (launchd)
bash service.sh install

# 其他命令
bash service.sh start|stop|restart|status|logs
```

## 告警类型

| 类型 | 说明 | 触发条件 |
|---|---|---|
| `price_high` / `price_low` | 绝对价格阈值 | 股价突破指定价格（如 >45.0 或 <42.5） |
| `daily_up` / `daily_down` | 当日涨跌幅多档 | 涨幅/跌幅超过配置的档位百分比（如 2%, 5%, 8%） |
| `surge_up` / `surge_down` | 涨速 | 窗口内（默认 5 分钟）价格变动超过阈值百分比 |
| `retracement` | 高位回撤 | 从近期高点回落超过阈值百分比（需先触发上涨） |
| `bounce` | 低位反弹 | 从近期低点回升超过阈值百分比（需先触发下跌） |
| `t_sell` / `t_buy` | T+0 做T | S事件：价格从卖出价跌到目标价（或阈值百分比）；B事件反之 |

详细告警规则见 [docs/NOTIFICATIONS.md](docs/NOTIFICATIONS.md)。

## 项目结构

```
stock-monitor/
├── stock_monitor/             # 核心包
│   ├── core.py               # 监控循环、价格检查、通知发送
│   ├── config.py             # 配置数据模型（Config、StockConfig）
│   ├── manager.py            # 监控管理器、热重载、T事件管理
│   ├── cli.py                # CLI 入口
│   └── webui/                # FastAPI Web 界面
│       ├── app.py            # 应用工厂
│       ├── api.py            # REST API 路由
│       └── static/           # 前端静态资源（HTML / CSS / JS）
├── service.sh                # 跨平台服务管理脚本
├── service/                  # systemd / launchd 模板
├── tests/                    # 测试（pytest）
│   ├── test_calendar.py      # 交易日历测试
│   ├── test_config.py        # 配置数据模型测试
│   ├── test_manager.py       # 监控管理器测试
│   └── test_api.py           # API 端点测试
├── docs/                    # 文档
│   └── NOTIFICATIONS.md     # 告警规则文档
├── pyproject.toml            # 项目元数据与依赖
└── uv.lock                   # 锁定依赖版本
```

## License

MIT

