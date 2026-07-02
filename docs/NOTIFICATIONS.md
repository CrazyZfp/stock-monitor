# 通知规则与限制

## 一、告警类型总览

系统提供 10 类通知，每种可独立开关（通过 `disabled_alerts` 配置）。

| # | alert_type | 中文名 | 触发依据 |
|---|---|---|---|
| 1 | `price_high` | 绝对价格上穿 | 当前价 > 设定阈值 |
| 2 | `price_low` | 绝对价格下破 | 当前价 < 设定阈值 |
| 3 | `daily_up` | 当日涨幅阶梯 | 当日涨幅 ≥ 档位 %（基于昨收） |
| 4 | `daily_down` | 当日跌幅阶梯 | 当日跌幅 ≥ 档位 %（基于昨收） |
| 5 | `surge_up` | 涨速上涨 | 窗口内涨幅 ≥ 阈值 % |
| 6 | `surge_down` | 涨速下跌 | 窗口内跌幅 ≥ 阈值 % |
| 7 | `retracement` | 从高位回撤 | daily_up 触发后，从峰值回落 ≥ 阈值 % |
| 8 | `bounce` | 从低位反弹 | daily_down 触发后，从谷值回升 ≥ 阈值 % |
| 9 | `t_sell` | 做T卖出（S） | 当前价 ≤ 事件价 × (1 − T%) |
| 10 | `t_buy` | 做T买入（B） | 当前价 ≥ 事件价 × (1 + T%) |

---

### 1. price_high — 绝对价格上穿

- **配置字段：** `price_high: float`（单档）
- **触发条件：** `当前价 > price_high`
- **重复触发：** 价格回落到阈值以下后重新上穿可再次触发
- **首次检查：** 若首次启动时价格已超过阈值，标记为已告警但不发通知

### 2. price_low — 绝对价格下破

- **配置字段：** `price_low: float`（单档）
- **触发条件：** `当前价 < price_low`
- **重复触发：** 价格回升到阈值以上后重新下破可再次触发
- **首次检查：** 若首次启动时价格已低于阈值，标记为已告警但不发通知

### 3. daily_up — 当日涨幅阶梯

- **配置字段：** `daily_change_up: list[float]`（多档，如 `[3, 5, 7]` 代表涨 3%、5%、7%）
- **触发条件：** `当日涨幅% ≥ 档位%`，其中 `当日涨幅% = (当前价 − 昨收) / 昨收 × 100`
- **多档独立冷却：** 每档使用独立的冷却键 `daily_up_tier_{idx}`
- **重复触发：** 当日涨幅回落到档位以下后重新上穿可再次触发
- **副作用：** 触发后**武装**回撤检测（`retracement_armed = true`）

### 4. daily_down — 当日跌幅阶梯

- **配置字段：** `daily_change_down: list[float]`（多档，正值，如 `[3, 5]` 代表跌 −3%、−5%）
- **触发条件：** `当日跌幅% ≥ 档位%`，即 `当日涨幅% ≤ −档位%`
- **多档独立冷却：** 每档使用独立的冷却键 `daily_down_tier_{idx}`
- **重复触发：** 当日跌幅回升到档位以下后重新下破可再次触发
- **副作用：** 触发后**武装**反弹检测（`bounce_armed = true`）

### 5. surge_up — 涨速上涨

- **配置字段：** `speed_threshold: float`（百分比阈值），`speed_window: int`（窗口分钟数，默认 5）
- **触发条件：** `(当前价 − 窗口内最早价) / 窗口内最早价 × 100 > speed_threshold`
- **窗口逻辑：** 见第三章"涨速窗口检测原理"
- **冷却：** 独立冷却键 `surge_up`

### 6. surge_down — 涨速下跌

- **配置字段：** 同 `surge_up`
- **触发条件：** `(当前价 − 窗口内最早价) / 窗口内最早价 × 100 < −speed_threshold`
- **冷却：** 独立冷却键 `surge_down`

### 7. retracement — 从高位回撤

- **状态机驱动：** 必须先由 `daily_up` 触发**武装**后，回撤检测才生效
- **峰值跟踪：** `peak_since_high_alert` 记录告警后的最高价（由 `daily_up` / `price_high` 触发时更新）
- **触发条件：**
  - `retracement_armed = true`
  - `(peak − 当前价) / peak × 100 ≥ retracement_threshold`
- **触发后：** 解除武装（`retracement_armed = false`），需下次 `daily_up` 再次武装

### 8. bounce — 从低位反弹

- **状态机驱动：** 必须先由 `daily_down` 触发**武装**后，反弹检测才生效
- **谷值跟踪：** `valley_since_low_alert` 记录告警后的最低价（由 `daily_down` / `price_low` 触发时更新）
- **触发条件：**
  - `bounce_armed = true`
  - `(当前价 − valley) / valley × 100 ≥ bounce_threshold`
- **触发后：** 解除武装（`bounce_armed = false`），需下次 `daily_down` 再次武装

### 9. t_sell — 做T卖出

- **配置字段：** `t_threshold: float`，`t_events: list[dict]`，`t_s_enabled: bool`
- **事件结构：** `{id, type: "S", price, target_price?, created_at}`
- **触发条件（无目标价）：** `当前价 ≤ 事件价 × (1 − t_threshold / 100)`
- **触发条件（有目标价）：** `当前价 ≤ target_price`（直接价格比较，忽略百分比）
- **事件消耗：** 触发后该事件自动从列表中移除

### 10. t_buy — 做T买入

- **配置字段：** 同 `t_sell`，`t_b_enabled: bool`
- **事件结构：** `{id, type: "B", price, target_price?, created_at}`
- **触发条件（无目标价）：** `当前价 ≥ 事件价 × (1 + t_threshold / 100)`
- **触发条件（有目标价）：** `当前价 ≥ target_price`（直接价格比较，忽略百分比）
- **事件消耗：** 触发后该事件自动从列表中移除

---

## 二、冷却机制

- **冷却时长：** 每只股票 `cooldown_minutes`（默认 5 分钟）
- **独立槽位：** 每种通知类型有独立的冷却计时器
- **`daily_up` / `daily_down` 多档独立冷却：** 每档使用 `daily_up_tier_{idx}` / `daily_down_tier_{idx}` 作为冷却键，档位之间互不影响
- **冷却判断：** 上次通知时间 + cooldown_minutes > 当前时间 则不重复通知
- **冷却更新：** 通知发送后立即记录当前时间

---

## 三、涨速窗口检测原理

涨速通知（`surge_up` / `surge_down`）依赖 `price_history` 记录进行检测。

### 数据记录

- 每轮轮询获取价格后，记录一笔 `{time: datetime, price: float}` 到 `price_history`
- 仅保留最近 **1 小时**的数据
- 轮询间隔（默认 30s）决定记录密度

### 检测逻辑

每轮检测时：
1. 计算窗口起点：`now − speed_window 分钟`（默认 5 分钟）
2. 过滤出窗口内的价格记录
3. 取窗口内**最早**一笔作为基准价
4. 计算：`(当前价 − 基准价) / 基准价 × 100`

### 重要限制

- **依赖轮询频率：** 价格变化只在轮询时刻被记录。如果价格在两个轮询之间 (默认 30s) 冲高又回落，涨速计算**完全感知不到**。
- **窗口滑移：** 基准价是"窗口内最早记录"，不是"正好 N 分钟前的价格"。当价格缓慢持续上涨时，窗口的最早记录也会随之滑动，实际有效窗口可能小于 `speed_window`。
- 需要至少 2 笔 `price_history` 记录才能进行比较。

---



## 四、回撤 / 反弹状态机

回撤和反弹是**两阶段状态机**，需要先触发上涨/下跌检测，才能监测反向运动。

### 回撤流程

```
daily_up 触发
  → retracement_armed = true
  → 开始跟踪 peak_since_high_alert（随每次 higher-high 更新）
  → 当 (peak − 当前价) / peak ≥ retracement_threshold
    → 发送 retracement 通知
    → retracement_armed = false（解除武装）
  → 等待下次 daily_up 重新武装
```

### 反弹流程

```
daily_down 触发
  → bounce_armed = true
  → 开始跟踪 valley_since_low_alert（随每次 lower-low 更新）
  → 当 (当前价 − valley) / valley ≥ bounce_threshold
    → 发送 bounce 通知
    → bounce_armed = false（解除武装）
  → 等待下次 daily_down 重新武装
```

### 峰值/谷值共享

`peak_since_high_alert` 和 `valley_since_low_alert` 在**绝对价格**（price_high/low）和**当日涨跌**（daily_up/down）之间共享。任一途径触发都会更新峰值/谷值。

---

## 五、钉钉限流与聚合机制

### 钉钉自定义机器人限制

| 限制维度 | 阈值 | 处罚 |
|---|---|---|
| 发送频率 | 每条机器人 **每分钟最多 20 条** | 超限后**限流 10 分钟**（返回 errcode 410100） |
| 月度配额（标准版） | **3000 条/月**（2025.11 后） | 超限后当月无法发送 |
| IP 限制 | 20 秒内 10000 次 | IP 被封 5 分钟 |

### 聚合机制

为规避钉钉限流，系统在每轮检查中**将多条通知合并为一条发送**：

1. 每轮检查开始时，设置 `_batch_mode = true`
2. 各检查函数触发的通知追加到 `_alert_buffer` 列表
3. 所有股票检查完毕后，调用 `flush_alerts()`
4. 将缓冲区的所有消息用换行符 `\n` 拼接，**一次性**发送到钉钉

此机制确保：即使一轮中多只股票同时触发告警，也只消耗 1 条钉钉配额。

---

## 六、交易时段过滤

### 交易日判断

- 周末（周六、周日）→ 非交易日
- 法定节假日 → 非交易日（使用 `cn-stock-holidays` 库数据，含 2004–2026 年）
- 库加载失败时降级为仅排除周末

### 交易时段判断

- 上午：**09:20 – 11:30**（连续竞价从 09:30 开始，09:20 开始轮询准备）
- 下午：**13:00 – 15:00**
- 非交易时段自动休眠到下一个开盘时间，盘中按配置间隔轮询

---

## 七、disabled_alerts 独立开关

- **配置：** `StockConfig.disabled_alerts: list[str]`
- **缺省值：** 空列表（所有通知类型启用）
- **控制方式：** 在编辑对话框中通过 10 个复选框独立开关

### 行为说明

- 通知被禁用后，**状态机仍然运行**（peak/valley 跟踪、武装标志等不受影响）
- 只有**发送通知**这一步被跳过
- 例如：关闭 `daily_up` 通知后，`daily_up` 仍然会武装 `retracement_armed`，回撤检测可正常工作

---

## 八、模板变量参考

### 全部支持变量

| 变量 | 说明 | 格式示例 |
|---|---|---|
| `{name}` | 股票官方名称 | 贵州茅台 |
| `{nickname}` | 自定义昵称（回退到 name） | 茅台 |
| `{price}` | 当前价格 | 19.80 |
| `{threshold}` | 触发的阈值价格 | 20.00 |
| `{daily_change}` | 当日涨跌幅（基于昨收） | +2.50% |
| `{speed_change}` | 窗口内涨速 | +2.50% |
| `{time}` | 涨速窗口（分钟） | 5 |
| `{tier_index}` | 档位序号 | 2 |
| `{tier_threshold}` | 该档位百分比 | 5.00% |
| `{peak_price}` | 回撤前最高价 | 20.50 |
| `{valley_price}` | 反弹前最低价 | 9.50 |
| `{retracement}` | 回撤百分比 | -3.00% |
| `{bounce}` | 反弹百分比 | +3.00% |
| `{t_type}` | 做T类型 | S / B |
| `{t_price}` | 做T事件记录价 | 10.00 |
| `{t_threshold}` | 做T阈值 | 3.00% |

### 各告警类型可用变量

| alert_type | 可用变量 |
|---|---|
| price_high | name, nickname, price, threshold, daily_change |
| price_low | name, nickname, price, threshold, daily_change |
| daily_up | name, nickname, price, tier_index, tier_threshold, daily_change |
| daily_down | name, nickname, price, tier_index, tier_threshold, daily_change |
| surge_up | name, nickname, price, speed_change, time, daily_change |
| surge_down | name, nickname, price, speed_change, time, daily_change |
| retracement | name, nickname, price, retracement, peak_price, daily_change |
| bounce | name, nickname, price, bounce, valley_price, daily_change |
| t_sell | name, nickname, price, t_type, t_price, t_threshold, daily_change |
| t_buy | name, nickname, price, t_type, t_price, t_threshold, daily_change |

### 模板行为

- 每种告警类型可配置**多条模板**，发送时随机选择一条
- 模板预览可通过 Web UI 或 `POST /api/templates/preview` 接口测试
- 未知占位符会报 400 错误

---

## 九、@提醒行为

- **全局配置：** `at_mobiles: list[str]`（手机号）和 `at_user_ids: list[str]`（用户 ID）
- **生效范围：** 所有通知类型统一 @ 相同的人
- **发送方式：** 钉钉 Webhook 的 `atMobiles` / `atUserIds` 参数
- **消息内容要求：** 钉钉要求消息正文中同时包含 `@手机号` 或 `@userId` 才能正确 @（本系统已在模板中处理）
- **配置位置：** Web UI Webhook 设置表单

---

## 十、轮询间隔与系统行为

### 轮询间隔

- **默认值：** 30 秒
- **下限：** 5 秒（Web UI 限制）
- **配置方式：** Web UI 状态 tab 点击编辑，或 `PUT /api/settings/poll-interval`
- **热重载：** 修改后下一轮立即生效

### 股票间间隔

- 每检查完一只股票，等待 1 秒再检查下一只，避免 API 限流

### 异常恢复

- 检查异常后等待 60 秒再重试（`StockMonitor.monitor_loop`）
- 轮询间隔内的秒级睡眠会检查 `_running` 标志，支持快速停止

### 监测循环流程

```
每 interval_seconds 秒执行一轮：
  1. 设 _batch_mode = true，清空 _alert_buffer
  2. 遍历所有启用的股票：
     2a. 获取当前价（调新浪 API）
     2b. 检查价格阈值（price_high / price_low / daily_up / daily_down / retracement / bounce）
     2c. 检查涨速（surge_up / surge_down）
     2d. 检查做T事件（t_sell / t_buy）
     2e. 等待 1 秒
  3. 设 _batch_mode = false
  4. 调用 flush_alerts() → 将所有缓冲消息合并为一条发送
```
