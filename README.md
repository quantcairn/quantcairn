# 🎯 SOXS Range Arbitrage — 区间套利交易系统

SOXS（三倍做空半导体ETF）的区间震荡套利系统。在震荡行情中，自动化捕捉支撑位买入、阻力位卖出的波段机会。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 复制配置样例并编辑
cp config.sample.yaml config.local.yaml
# 或将 config.sample.yaml 复制为 config.yaml

# 3. 编辑 config.local.yaml 或 config.yaml，设置你的 range 参数
#    support_price: 28.50   (你观察到的买入区间)
#    resistance_price: 30.50 (你观察到的卖出区间)

# 4. 运行测试
python run_tests.py

# 5. 可选安装开发依赖
pip install -r dev-requirements.txt

# 3. 验证配置
python run.py --dry-run

# 4. 启动模拟交易（不涉及真实资金）
python run.py --paper

# 5. 模拟交易 + Web面板
python run.py --paper --dashboard
# 浏览器打开 http://localhost:8080

# 6. 回测历史数据
python run.py --backtest
```

## 两种区间模式

### 手动模式（推荐）
根据你的盘前观察，在 `config.yaml` 中手动设定：
```yaml
range:
  mode: manual
  support_price: 28.50    # 你的观察：支撑位/买入触发价
  resistance_price: 30.50 # 你的观察：阻力位/卖出触发价
  tolerance_pct: 0.3      # 价格在±0.3%范围内触发
```

### 自动模式
系统自动根据最近N根K线识别震荡区间：
```yaml
range:
  mode: auto
  auto_lookback: 50       # 回顾50根5分钟K线
  auto_refresh_minutes: 15 # 每15分钟重新计算区间
```

## 风控规则（不可绕过）

| 规则 | 说明 | 默认值 |
|------|------|--------|
| 止损 | 跌破支撑位的2%立即平仓 | 2.0% |
| 日亏损上限 | 当日亏损超$500停止交易 | $500 |
| 连续亏损熔断 | 连续3笔亏损暂停30分钟 | 3笔 |
| 仓位上限 | 最多持仓300股 | 300股 |
| 冷却时间 | 成交后30秒内不重复交易 | 30秒 |
| 交易时间 | 仅美东9:30-16:00 | — |

## TradingView 集成

1. 打开 TradingView，加载 SOXS 图表
2. 将 `tradingview/soxs_range_strategy.pine` 复制到 Pine Editor
3. 添加到图表，设置支撑/阻力参数
4. 配置告警（可选webhook推送到Python后端）

## 环境变量覆盖

```bash
# 临时覆盖价格参数
SOXS_SUPPORT=28.00 SOXS_RESISTANCE=29.50 python run.py --paper

# 临时调大仓位
SOXS_SIZE=200 python run.py --paper
```

## 测试与验证

推荐使用项目自带的 `run_tests.py`，避免依赖 `pytest` 环境。示例：

```bash
.venv/bin/python run_tests.py
```

如果需要验证配置文件合法性：

```bash
.venv/bin/python run.py --dry-run
```

## 配置文件加载顺序

`run.py` 会按以下优先级加载配置文件：

1. `config.local.yaml`
2. `config.yaml`
3. `config.sample.yaml`

如果希望指定特定配置文件，请使用：

```bash
python run.py --config path/to/config.yaml
```

## 监控与健康检查

- `health_check.sh`：检查 `launchd` 服务、API 端口、文件描述符和日志中的错误痕迹
- `monitor.sh`：每 15 分钟记录系统快照到 `snapshots.log`

运行方式：

```bash
bash health_check.sh
bash monitor.sh
```

如果要持续监控，可以通过 macOS `launchd` 或 cron 调度 `monitor.sh` 定期执行，并将 `snapshots.log` 归档或发送告警。
## 实盘交易（谨慎！）

```bash
# 1. 先在模拟盘跑3天以上，确认策略有效
python run.py --paper --dashboard

# 2. 配置长桥API
# 编辑 config.yaml:
#   broker.longbridge.enabled: true
#   broker.longbridge.app_key: "your_key"
#   broker.longbridge.app_secret: "your_secret"
#   broker.longbridge.access_token: "your_token"
#   broker.longbridge.environment: "sandbox"   # 或 "prod"
#   broker.longbridge.http_url / quote_ws_url / trade_ws_url

# 也可以直接用环境变量：
#   LONGBRIDGE_API_KEY / LONGBRIDGE_API_SECRET
#   LONGBRIDGE_ACCESS_TOKEN
#   LONGBRIDGE_ENV=sandbox
#   LONGBRIDGE_HTTP_URL / LONGBRIDGE_QUOTE_WS_URL / LONGBRIDGE_TRADE_WS_URL
#   LONGBRIDGE_LOG_PATH=logs
#   LONGBRIDGE_AUDIT_DIR=logs

# 3. 安装长桥SDK
pip install longbridge

# 4. 启动实盘
python run.py --live --dashboard
```

实盘主路径会把每一次交易请求和响应追加到 `logs/trades-YYYYMMDD.jsonl`，方便回查和审计。
如果当前运行目录不可写，可以用 `LONGBRIDGE_AUDIT_DIR` 指到一个可写目录。

## 项目结构

```
soxs-range-arbitrage/
├── config.yaml           # 主配置（改动这里）
├── run.py                # 入口脚本
├── requirements.txt
├── src/
│   ├── config/loader.py  # 配置加载
│   ├── data/fetcher.py   # yfinance价格获取
│   ├── strategy/range_detector.py  # 策略核心
│   ├── engine/trading_engine.py    # 主循环引擎
│   ├── broker/
│   │   ├── base.py        # 券商抽象接口
│   │   ├── paper_broker.py         # 模拟交易
│   │   └── longbridge_broker.py    # 长桥实盘
│   ├── risk/manager.py    # 风控管理
│   ├── notifier/alerts.py # 通知系统
│   └── dashboard/server.py # Web监控面板
└── tradingview/
    └── soxs_range_strategy.pine  # Pine Script
```

## 自动启动（可选）

推荐将 `launchd/com.soxs.arbitrage.plist`、`launchd/com.soxs.arbitrage.stop.plist` 与 `launchd/com.soxs.ai_selector.plist` 复制到 `~/Library/LaunchAgents/` 并使用 `launchctl load` 加载。或者使用 `cron` 调度 `auto_trade.sh start|stop`。其中 AI 选股由 `scripts/ai_selector_wrapper.py` 在美东时间 `09:00` 自动执行一次，再由交易启动任务接管。示例如下：

```bash
# 使用 launchd（示例）
mkdir -p ~/Library/LaunchAgents
cp launchd/com.soxs.arbitrage.plist ~/Library/LaunchAgents/
cp launchd/com.soxs.arbitrage.stop.plist ~/Library/LaunchAgents/
cp launchd/com.soxs.ai_selector.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.soxs.arbitrage.plist
launchctl load ~/Library/LaunchAgents/com.soxs.arbitrage.stop.plist
launchctl load ~/Library/LaunchAgents/com.soxs.ai_selector.plist

# 或使用 crontab（示例）
# 每天 21:25 启动交易引擎（AI 选股已在美东 09:00 单独完成）
25 21 * * * /Users/chenwei/soxs-range-arbitrage/auto_trade.sh start
# 每天 04:05 停止
5 4 * * * /Users/chenwei/soxs-range-arbitrage/auto_trade.sh stop
```

## AI 选股日报

- 每日 AI 选股报告保存在 `reports/`，文件名格式 `ai_selection_YYYYMMDD.md`。
- Top5 自动生成的配置文件位于 `configs/TOP1.yaml` 到 `configs/TOP5.yaml`。
- 本项目包含一个 AI 选股演示脚本 `scripts/run_ai_selector.py`（在线）和 `scripts/generate_offline_demo.py`（离线合成示例），可以用于验证从选股到配置写入的完整流程。

示例：
```bash
# 运行离线演示并生成报告与 TOP 配置
.venv/bin/python scripts/generate_offline_demo.py

# 运行 AI 选股（在线模式，可能需要网络）
.venv/bin/python scripts/run_ai_selector.py
```

## 免责声明

本系统仅供学习和研究用途。使用本系统进行实盘交易的风险由用户自行承担。请确保在实盘前充分测试，并设置合理的风控参数。
