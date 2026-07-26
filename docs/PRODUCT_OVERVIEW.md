# QuantCairn Product Overview

> **版本**: Alpha v0.12.0  
> **最后更新**: 2026-07-25  
> **目标读者**: 开发者、潜在用户、项目维护者

---

## 1. 产品定位

### QuantCairn 是什么

QuantCairn 是一个 **AI 驱动的量化研究平台**（quantitative research platform），专注于美股候选标的的研究、评分、筛选和模拟验证。

核心能力：

- 每日 9 阶段 AI 选股管道
- 35 只美股 + ETF 的托管 Universe
- 多因子评分模型（波动率、成交量、趋势、可重复性、回撤）
- 模式感知的质量过滤（盘中 / 盘后 / 周末）
- 纸交模拟环境（端到端已验证）
- 管道诊断和按标的淘汰追踪

### QuantCairn 不是什么

| 不是 | 原因 |
|---|---|
| 实盘自动交易机器人 | `allow_live_order=false`，三重独立安全门控关闭 |
| 投资顾问 | 不提供投资建议，仅提供研究工具 |
| 收益保证工具 | 不存在可靠的收益保证 |
| 高频交易系统 | 系统设计目标是每日批量选股，非 HFT |

**关键声明**：交易功能受安全门控保护。当前重点仍然是研究和纸交验证。实盘交易需要显式的多层配置变更，从架构上杜绝误操作。

---

## 2. 当前能力

### 2.1 9 阶段选股管道

| 阶段 | 输入 | 输出 | 说明 |
|---|---|---|---|
| **UNIVERSE** | — | 35 只标的 | 从托管 Universe 加载已启用标的 |
| **UNIVERSE_FILTER** | 35 | ≤50 | 硬上限截断 |
| **MARKET_DATA** | ≤50 | 可用数据 | 独立于评分的 OHLCV 验证 |
| **SCORING_ELIGIBLE** | 可用数据 | 已评分池 | 多因子评分 + 37 个回退 profile |
| **BASE_RANKING** | 已评分池 | 已评分池 | `score_candidate()` 精修 |
| **FORMAL_ELIGIBILITY** | 已评分池 | 正式池 | `formal_scoring_eligibility` 过滤 |
| **DATA_QUALITY** | 正式池 | 通过质量 | 模式感知的价差/成交量/波动率检查 |
| **COMPOSITION_FILTER** | 通过质量 | 多样化 | 行业/相关性多样性选择 |
| **FORMAL_TOP** | 多样化 | TOP K | 最终候选标的 |

**最近验证数据（2026-07-25）**：
- 35 只 Universe → 12 只评分入选 → 质量/组合筛选 → 5 只 TOP 候选
- 管道成功率：14.29%（非交易时段放宽模式）
- 一致性状态：✅ PASS

**每阶段特性**：
- 输入/输出计数
- 淘汰原因代码
- 耗时记录
- 漏斗不变性强制执行（`output ≤ input`）

### 2.2 执行模式

| 模式 | 质量检查 | 交易类型 | 当前状态 |
|---|---|---|---|
| **LIVE** | 严格（实时 bid/ask 价差验证） | `LIVE_TRADABLE` | ⚠️ 安全禁用（`allow_live_order=false`） |
| **PAPER** | 放宽（接受 EOD 数据） | `PAPER_ELIGIBLE` | ✅ 端到端已验证 |
| **RESEARCH** | 放宽（接受 EOD 数据） | `RESEARCH_ONLY` | ✅ 研究输出模式 |

模式选择：
```
1. QUANTCAIRN_EXECUTION_MODE=LIVE|PAPER|RESEARCH (最高优先级)
2. 从 preflight 自动推导: FULL → LIVE, 其他 → RESEARCH
   PAPER 必须显式设置 — 永不自动选择
```

### 2.3 Paper Trading（纸交模拟）

✅ **端到端流程已验证**：

```
Selector (PAPER_ELIGIBLE candidates + confidence)
  → ConfigWriter (mode=paper, reduce_only=False)
  → PaperBroker (simulated BUY/SELL with slippage)
  → Portfolio State (state/paper/{account}/portfolio_state.json)
  → Dashboard (port 8090, read-only)
```

**验证结果**（2026-07-25 验收测试通过）：
- 3 笔模拟订单全部成交（BUY 100 + BUY 50 + SELL 75 AAPL）
- 持仓正确追踪（均价 $180.62，未实现盈亏 +$322.95）
- 投资组合状态持久化到磁盘（1489 字节）
- 账户隔离：纸交组合永不触及实盘券商

### 2.4 Demo 模式（零 API 依赖）

| 特性 | 说明 |
|---|---|
| 合成 OHLCV 数据 | 5 只标的（AAPL, MSFT, NVDA, SPY, TSLA），各 252 行 |
| 确定性 | 种子随机漫步，每次运行结果完全相同 |
| 无外部依赖 | 无需 API 密钥、券商连接、网络访问 |
| 完整管道 | 运行完整 9 阶段管道 |

```bash
# 30 秒快速体验
.venv/bin/python scripts/run_demo_selector.py
```

### 2.5 诊断与可观测性

| 组件 | 文件 | 功能 |
|---|---|---|
| **FunnelTracker** | `funnel_tracker.py` | 每阶段输入/输出计数、耗时、淘汰原因代码、一致性验证 |
| **Pipeline Diagnostics** | `data_diagnostics.py` | 按标的追踪：OHLCV 行数、回退 profile、Universe 过滤通过/失败 |
| **Preflight** | `preflight.py` | 管道执行前的市场状态检测和运行模式推荐 |
| **Status CLI** | `scripts/status.py` | 只读系统状态：执行模式、最后一次运行、纸交组合、系统健康 |

### 2.6 通知与展示

| 渠道 | 说明 |
|---|---|
| **Telegram Bot** | `@QuantCairnPicks` → 频道 `@QuantCairnPicks`；长消息自动分段 |
| **Dashboard** | 端口 8090，Jinja2 HTML，只读，纸交/实盘双模式显示 |

### 2.7 开发者体验

| 工具 | 说明 |
|---|---|
| **pyproject.toml** | 可安装包，含 `[demo]` 和 `[test]` 额外依赖 |
| **quantcairn 命名空间** | 21 个公开 API 符号，顶级导入和子模块导入均支持 |
| **CI** | `.github/workflows/test.yml` — 安装、导入检查、测试、Demo 验证 |
| **环境检查** | `scripts/check_dev_environment.py` |
| **示例代码** | `examples/basic_demo.py` — 最小化 Python API 示例 |
| **测试** | 1075+ 测试，59+ 核心集成测试，131 个测试文件 |

---

## 3. 已完成模块（17 个）

| 模块名称 | 文件位置 | 作用 |
|---|---|---|
| **Selector Pipeline** | `src/openalpha/selector.py` | 9 阶段管道编排，质量过滤，执行模式决策 |
| **Funnel Tracker** | `src/openalpha/funnel_tracker.py` | 每阶段输入/输出计数，一致性验证，诊断报告 |
| **Data Diagnostics** | `src/openalpha/data_diagnostics.py` | 按标的 OHLCV 可用性检查（评分前） |
| **Preflight** | `src/openalpha/preflight.py` | 市场状态检测 → 运行模式决策 |
| **Candidate Ranking** | `src/openalpha/candidate_ranking.py` | 评分精修：流动性/趋势/波动率/风险子评分 |
| **Universe Manager** | `src/universe/manager.py` | 35 只托管标的，JSON 快照持久化，启用/禁用 CLI |
| **Scorer** | `src/scoring/scorer.py` | 30/20/20/15/10 多因子模型 + 37 个回退 profile |
| **Config Writer** | `src/openalpha/config_writer.py` | 输出 TOP{1,2,3}.yaml 供交易引擎消费 |
| **Paper Broker** | `src/broker/paper_broker.py` | 带滑点/佣金的模拟成交 |
| **Portfolio State** | `src/broker/paper_portfolio_state.py` | 纸交组合持久化（v2 架构），JSON 格式 |
| **Trading Engine** | `src/engine/trading_engine.py` | 主循环：获取 → 策略 → 风险 → 下单（纸交/实盘双模式） |
| **Risk & Safety** | `src/risk/` + `src/safety/` | 风险管理 + 三重实盘门控 |
| **Notifier/Alerts** | `src/notifier/alerts.py` | 控制台/macOS/Webhook/Telegram 通知分发 |
| **Dashboard** | `src/dashboard/combined.py` | 只读 HTML 看板（端口 8090） |
| **Demo Runner** | `scripts/run_demo_selector.py` | 零依赖 Demo 体验 |
| **Status CLI** | `scripts/status.py` | 只读系统状态报告 |
| **Package Infrastructure** | `pyproject.toml` + `quantcairn/` | 可安装包，公开 API 命名空间 |

---

## 4. 已实现但未完全集成（8 个）

这些功能存在并且有独立脚本或模块文件，但未完全进入主选择闭环。

| 模块 | 当前状态 | 差距 |
|---|---|---|
| **Market Regime Engine** | `src/regime/` — BULL/SIDEWAYS/BEAR/RISK_OFF 分类已实现，独立检测脚本存在 | 未接入选择器决策逻辑 |
| **Outcome Collector** | `src/outcome/collector.py` — v3 架构（Parquet + JSONL）已就绪，独立脚本 `collect_trade_outcomes.py` 存在 | 未在纸交运行后自动触发 |
| **Weight Advisor** | `src/outcome/weight_advisor.py` — v2 因子分析，增量保护已实现，独立脚本 `run_weight_advisor.py` 存在 | 建议未自动应用，需人工审查 |
| **Governance** | `src/outcome/governance.py` — 状态机 + 人工审批门控已实现 | 审批流程需手动触发 |
| **Backtest** | `src/backtest/` — 15 个文件，完整框架已实现 | 独立运行，未接入管道评估回路 |
| **Strategy Selection** | `src/openalpha/strategy_selection.py` — 候选-策略匹配已实现 | 运行时集成路径不明确 |
| **Candidate Validation** | `src/candidate_validation/` — 18 个文件，编排器已实现 | 完全独立运行，未耦合选择器 |
| **Shadow Observer** | `src/shadow/` — 8 个文件，阴影交易观察已实现 | 独立运行，未耦合选择器 |

---

## 5. 不支持的功能（7 项明确排除）

| 功能 | 排除原因 |
|---|---|
| **自动盈利保证** | 不存在可靠的收益保证机制，系统是研究工具 |
| **自动实盘交易** | 三重独立安全门控关闭（`allow_live_order=false` 在所有层级强制执行） |
| **高频交易（HFT）** | 系统设计目标为每日批量选股，非 HFT |
| **自动修改策略权重** | 治理流程要求 `approved_by_human=True` + 非空原因 |
| **多数据源生产级融合** | Yahoo Finance 主数据源，LongBridge 实时报价，其他属于未来路线图 |
| **投资建议服务** | 系统定位为研究工具，不提供投资建议 |
| **无风险交易** | 不存在无风险交易 |

---

## 6. 用户工作流

### 6.1 快速评估（30 秒，无需 API 密钥）

```bash
git clone https://github.com/quantcairn/quantcairn.git
cd quantcairn
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python scripts/run_demo_selector.py
```

输出：格式化的管道报告，2 个研究候选，Demo 工件。

### 6.2 每日研究选择

```bash
.venv/bin/python scripts/run_ai_selector.py --universe-source managed
```

输出：5 个研究候选，完整管道诊断，Telegram 通知。

### 6.3 PAPER 模拟交易

```bash
QUANTCAIRN_EXECUTION_MODE=PAPER .venv/bin/python scripts/run_ai_selector.py --universe-source managed
```

输出：5 个 `PAPER_ELIGIBLE` 候选，`mode=paper` TOP 配置，供纸交券商使用。

### 6.4 系统健康检查

```bash
.venv/bin/python scripts/status.py
```

输出：执行模式，最后一次运行时间，纸交组合摘要（现金/权益/持仓/未实现盈亏），系统健康状态。

### 6.5 开发环境验证

```bash
.venv/bin/python scripts/check_dev_environment.py   # 验证 Python 版本、包导入、Demo 可用性
.venv/bin/python -m pytest tests/ -q                # 运行全部测试
.venv/bin/python examples/basic_demo.py             # 最小 API 示例
```

---

## 7. 路线图

### ✅ 已完成（v0.12.0-demo）

- [x] 35 只托管 Universe（带 37 个回退 profile）
- [x] 9 阶段选择管道（含漏斗不变性强制执行）
- [x] 模式感知质量过滤（LIVE / PAPER / RESEARCH）
- [x] 纸交端到端流程（选择器 → 券商 → 组合状态 → 看板）
- [x] Demo 模式（确定性合成数据，零依赖）
- [x] 管道诊断报告（按标的淘汰追踪）
- [x] Telegram 通知（长消息分块发送）
- [x] 只读看板（端口 8090，纸交/实盘双模式）
- [x] Python 包基础（`pyproject.toml`，`quantcairn/` 命名空间）
- [x] AI 工程上下文层（`.ai/` — CLAUDE.md, safety.md, architecture.md, DECISION_LOG.md）
- [x] 开源文档（README, CONTRIBUTING, LICENSE, CHANGELOG）
- [x] GitHub Actions CI 工作流
- [x] 运行时状态 CLI（`scripts/status.py`）

### 🔜 v0.13.0 近期

- [ ] 统一评分拒绝原因记录（消除"原因未记录"）
- [ ] 完善结果收集链（纸交后自动触发 Outcome Collector）
- [ ] Dashboard 模式一致性改进（`mode=paper` 与 `QUANTCAIRN_EXECUTION_MODE` 检查）
- [ ] 文档完善（API 文档、快速入门指南）
- [ ] 多数据源 fallback（Alpha Vantage, Polygon）

### 📅 中期

- [ ] Outcome Learning 集成（自动触发结果收集 → 权重建议 → 人工审批）
- [ ] Weight Advisor 集成（建议自动应用到治理审批流程）
- [ ] Regime Engine 集成（市场行情信号接入选择器决策）
- [ ] Backtest 验证框架（管道变更的自动回归测试）
- [ ] Docker 部署（容器化选择器 + 看板栈）

### 🌐 Community

- [ ] 开源基础版本（GitHub 公开仓库）
- [ ] 文档完善（多语言，用户手册）
- [ ] Demo 体验优化（预录市场数据，更快的初次体验）
- [ ] GitHub Discussions 和 Issue 模板

---

## 8. 已知问题

| 问题 | 严重程度 | 状态 |
|---|---|---|
| 非交易时间质量过滤全部拒绝（spread_unavailable） | 中等 | ✅ 已通过模式感知放宽修复（EOD/PAPER/RESEARCH 模式） |
| 评分拒绝原因在诊断中显示"原因未记录" | 低（可观测性差距） | 待修复 — 评分 `reject_reasons` 未传播到漏斗 dropped records |
| Dashboard 历史 TOP 配置可能显示 `mode=live` 而非实际执行模式 | 低（显示问题） | 待修复 — `_load_existing_mode()` 保留旧配置模式 |
| 部分模块已实现但未接入主选择闭环（见第 4 节） | 低（架构问题） | 8 个模块存在独立脚本，集成差距明确 |
| 5 个预存在的测试失败（环境泄漏，`config.local.yaml`） | 低（与选择器无关） | 已知问题，不影响选择器功能 |

---

*本文档反映系统实际状态，基于提交 `1458ad9`（2026-07-25）验证。不求理想化，只求真实性。*
