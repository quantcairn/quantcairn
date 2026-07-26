# QuantCairn Current System State

> **审计日期**: 2026-07-25
> **Note**: This is a point-in-time system audit snapshot. Some operational details may have changed since the audit date. Cross-reference with `.ai/CLAUDE.md` and `.ai/architecture.md` for current module maps.
>
> **审计范围**: 完整系统 — 模块、链路、功能完成度
> **目标读者**: 项目维护者、新加入的开发者
> **原则**: 只记录当前真实状态，不假设、不计划、不营销

---

## 1. Git 状态

| 属性 | 值 |
|---|---|
| **分支** | `codex/paper-broker-hardening` |
| **HEAD** | `1458ad9` — `feat(cli): add QuantCairn runtime status command` |
| **领先远程** | 10 commits |
| **远程** | `git@github.com:quantcairn/quantcairn.git` |
| **默认分支** | `main` |
| **工作树** | 干净（仅有预存在的未跟踪工件如 `artifacts/`） |

### 现有标签

```
v0.10.9-ai-context               AI 工程上下文层
v0.10.10-beta                    管道完整性加固
v0.10.11-beta                    OpenAlpha 基线
v0.11.0-open-source-ready        开源文档基础
v0.11.0-quantcairn-migration-ready  GitHub 迁移检查点
v0.12.0-demo                     首次可运行的 Demo 发布
v0.12.0-public-beta-ready        公开测试版就绪检查点
```

### 未提交的已修改文件

| 文件 | 状态 |
|---|---|
| `README.md` | 已修改（v0.12.0 发布版精修）— 未提交 |
| `docs/PRODUCT_OVERVIEW.md` | 新建 — 未追踪 |
| `docs/CURRENT_SYSTEM_STATE.md` | 新建（本文件）— 未追踪 |

---

## 2. 产品定位

QuantCairn 是一个 **AI 驱动的量化研究平台**，专注于美股候选标的的研究、评分、筛选和模拟验证。

- 核心用途：每日 9 阶段 AI 选股管道，35 只美股 + ETF 的托管 Universe
- 交易功能受安全门控保护 — `allow_live_order=false` 在所有层级强制执行
- 当前重点：研究和纸交验证，实盘交易被架构阻止
- 不是实盘自动交易机器人、投资顾问或收益保证工具

---

## 3. 当前模块地图

### 核心活跃模块（生产就绪）

| 模块 | 位置 | 代码规模 | 作用 |
|---|---|---|---|
| **Selector Pipeline** | `src/openalpha/selector.py` | ~1500 行 | 9 阶段管道编排、质量过滤、执行模式决策 |
| **Funnel Tracker** | `src/openalpha/funnel_tracker.py` | ~650 行 | 每阶段输入/输出追踪、不变性验证、诊断报告 |
| **Data Diagnostics** | `src/openalpha/data_diagnostics.py` | ~360 行 | 评分前 OHLCV 可用性检查，按标的淘汰原因追踪 |
| **Preflight** | `src/openalpha/preflight.py` | ~260 行 | 市场状态检测 → `run_mode` 决策 |
| **Candidate Ranking** | `src/openalpha/candidate_ranking.py` | ~550 行 | 评分精修：流动性/趋势/波动率/风险/策略适配子评分 |
| **Universe Filter** | `src/openalpha/universe_filter.py` | ~280 行 | 4 项规则检查（价格/成交量/市值/ATR），含回退 ATR 跳过 |
| **Config Writer** | `src/openalpha/config_writer.py` | ~600 行 | 输出 `TOP{1,2,3}.yaml` 供交易引擎消费 |
| **Demo Data** | `src/openalpha/demo_data.py` | ~230 行 | 确定性合成 OHLCV 数据（5 只标的，252 行） |
| **Scorer** | `src/scoring/scorer.py` | ~1091 行 | 30/20/20/15/10 多因子模型 + 37 个回退 profile |
| **Universe Manager** | `src/universe/manager.py` | — | 35 只托管标的，JSON 快照，`manage_universe.py` CLI |
| **Trading Engine** | `src/engine/trading_engine.py` | ~3141 行 | 主循环：获取 → 策略 → 风险 → 下单（纸交/实盘双模式） |
| **Paper Broker** | `src/broker/paper_broker.py` | — | 带滑点/佣金的模拟成交 |
| **Portfolio State** | `src/broker/paper_portfolio_state.py` | — | 纸交组合持久化（v2 架构，JSON 格式） |
| **Risk Manager** | `src/risk/manager.py` | — | 风险管理 |
| **Safety Guards** | `src/safety/` | 2 个文件 | `live_guard.py` + `trading_environment_guard.py`，三重实盘门控 |
| **Notifier** | `src/notifier/alerts.py` | ~1578 行 | 控制台/macOS/Webhook/Telegram 通知分发 |
| **Dashboard** | `src/dashboard/combined.py` | ~8000 行 | 只读 HTML 看板（端口 8090） |

### 已实现但未完全接入主闭环的模块

| 模块 | 位置 | 代码规模 | 当前状态 | 差距 |
|---|---|---|---|---|
| **Market Regime** | `src/regime/` | 4 个文件 | BULL/SIDEWAYS/BEAR/RISK_OFF 已实现，独立检测脚本存在 | 未接入选择器决策逻辑 |
| **Outcome Collector** | `src/outcome/collector.py` | — | v3 架构（Parquet + JSONL），`collect_trade_outcomes.py` 独立脚本存在 | 纸交运行后未自动触发 |
| **Weight Advisor** | `src/outcome/weight_advisor.py` | — | v2 因子 + 增量保护，`run_weight_advisor.py` 独立脚本存在 | 建议未自动应用到治理审批流程 |
| **Governance** | `src/outcome/governance.py` | — | 状态机 + 人工审批门控已实现 | 审批流程需手动触发 |
| **Backtest** | `src/backtest/` | 15 个文件 | 完整框架已实现 | 独立运行，未接入管道评估回路 |
| **Strategy Selection** | `src/openalpha/strategy_selection.py` | ~1100 行 | 候选-策略匹配已实现 | 运行时集成路径不明确 |
| **Candidate Validation** | `src/candidate_validation/` | 18 个文件 | 编排器已实现 | 完全独立运行 |
| **Shadow Observer** | `src/shadow/` | 8 个文件 | 阴影交易观察已实现 | 未耦合选择器 |

### 脚本层（41 个脚本）

核心活跃脚本：
- `scripts/run_ai_selector.py` — 选择器 CLI 入口（2646 行）
- `scripts/ai_selector_wrapper.py` — launchd 调度包装器（138 行）
- `scripts/run_demo_selector.py` — 演示模式入口（532 行）
- `scripts/status.py` — 只读系统状态 CLI（392 行）
- `scripts/check_dev_environment.py` — 开发环境验证（101 行）
- `scripts/diag_market_data.py` — 行情数据诊断（255 行）

---

## 4. 实际运行链路

```
launchd (每 60 秒唤醒)
  └─ ai_selector_wrapper.py
       ├─ 检查：是否为美股交易日？
       ├─ 检查：当前时间 ≈ 09:00 ET ±90 秒？
       ├─ 检查：今天是否已经运行过？
       ├─ 环境：YF_DISABLE_CURL_CFFI=1, OPENALPHA_MAX_SYMBOLS=50
       └─ 执行：scripts/run_ai_selector.py --universe-source managed
            │
            ├─ 1. Preflight（预检）
            │    输入：无（检测实际市场时间、交易日历、数据可用性）
            │    输出：run_mode = FULL | AFTER_MARKET | EOD_ONLY | DEGRADED
            │    文件：src/openalpha/preflight.py : run_preflight()
            │    状态：✅ 活跃 — 决定质量过滤器的严格度
            │
            ├─ 2. Universe（标的池）
            │    输入：UniverseManager.load_snapshot() → 35 只启用标的
            │    输出：35 只标的（未被过滤 — 管道的 UNIVERSE_FILTER 阶段只是上限截断）
            │    文件：src/universe/manager.py → src/openalpha/selector.py:645-649
            │
            ├─ 3. MARKET_DATA（行情数据验证）
            │    输入：35 只标的
            │    输出：35（所有标的均有 ≥60 行 OHLCV 数据或回退 profile）
            │    文件：src/openalpha/data_diagnostics.py : check_data_availability()
            │    注意：独立于评分 — 验证的是数据可用性，非评分是否成功
            │
            ├─ 4. SCORING_ELIGIBLE（评分资格）
            │    输入：35 只标的
            │    输出：12（通过 score_frame 或 _fallback_scored_item）
            │    文件：src/scoring/scorer.py : score_universe() → _score_symbol() → score_frame()
            │    淘汰 23 只的原因（设计如此，非 Bug）:
            │      - score_frame() 基于真实 OHLCV 在以下情况拒绝：
            │        区间太宽(>45%)、区间太窄(<4%)、ATR 太极端、跳空风险、强趋势、
            │        波动不足、价差不够(<3%)
            │      - _load_history() 失败时尝试回退 profile，但非回退模式下
            │        universe filter 以严格模式运行，拒绝价格/成交量不达标
            │    当前差距：淘汰原因被记录为 "scoring_ineligible"，而非具体的
            │    reject_reason（如 "range too tight"、"strong trend"）
            │
            ├─ 5. BASE_RANKING（基准排名）
            │    输入：12 只标的
            │    输出：12（score_candidate() 精修 → 子评分 + 策略推荐）
            │    文件：src/openalpha/candidate_ranking.py : score_candidate()
            │
            ├─ 6. FORMAL_ELIGIBILITY（正式资格）
            │    输入：12
            │    输出：12（formal_scoring_eligibility=True）
            │    文件：selector.py:641-648
            │
            ├─ 7. DATA_QUALITY（实时数据质量）
            │    输入：12
            │    输出（strict 模式）：0 — 全部被 spread_unavailable 拒绝
            │    输出（relaxed 模式）：12 — 跳过价差检查
            │    文件：selector.py : _apply_quality_filters_with_report()
            │    检查顺序: existing_position → missing_data → volume_filter →
            │              spread_unavailable → spread_filter → volatility_filter
            │    关键：非交易时段 spread_unavailable 是最大瓶颈 — bid/ask 不可用
            │
            ├─ 8. COMPOSITION_FILTER（组合多样性）
            │    输入（strict 回退路径）：12（来自 pre-quality pool）
            │    输入（relaxed 路径）：12（quality-passed）
            │    输出：5（greedy diversity selection）
            │    文件：selector.py : _select_diversified_top_k()
            │    规则：correlation≥0.90→-60, sector_same→-35, sector_diff→+8
            │
            ├─ 9. FORMAL_TOP（最终产出）
            │    输入：12（回退路径）或 quality_passed
            │    输出：5 只 TOP 候选
            │    candidate_type 取决于 execution_mode:
            │      LIVE → LIVE_TRADABLE (仅当质量通过)
            │      PAPER → PAPER_ELIGIBLE (含 confidence)
            │      RESEARCH → RESEARCH_ONLY
            │
            ├─ FunnelTracker（审计）
            │    输出：一致性报告 + 诊断报告 + funnel_debug.json
            │    不变性检查：每阶段 output ≤ input
            │
            ├─ Config Writer（写 TOP 配置）
            │    输出：configs/TOP{1,2,3}.yaml
            │    安全：如已存在 live 配置，跳过写入
            │
            └─ Notifier（通知）
                 → Telegram @QuantCairnPicks（长消息分块）
                 → notification_ledger.jsonl（去重追踪）
```

---

## 5. 9 阶段 Selector 流程 — 阶段详细说明

### 5.1 UNIVERSE（标的池构建）

| 属性 | 值 |
|---|---|
| **输入** | 35 只托管标的（来自 `UniverseManager.load_snapshot()`） |
| **输出** | 35 只标的 |
| **备选方案** | 若托管 Universe 不可用 → `_load_local_snapshot()`（传统样例） |
| **文件** | `src/openalpha/selector.py:621-645` |

**当前 35 个符号**：
AAPL, ADBE, AMD, AMZN, AVGO, BAC, CRM, DIA, DIS, GOOGL, INTC, IWM, JNJ, JPM, META, MSFT, NFLX, NVDA, PG, QQQ, SPY, SSO, TSLA, UNH, V, WMT, XLB, XLE, XLF, XLI, XLK, XLU, XLV, XLY, XOM

### 5.2 UNIVERSE_FILTER（数量上限截断）

| 属性 | 值 |
|---|---|
| **输入** | 35 只标的 |
| **输出** | 35（未触发截断 — `OPENALPHA_MAX_SYMBOLS=50`） |
| **文件** | `src/openalpha/selector.py:647-648` |

### 5.3 MARKET_DATA（行情数据可用性）

| 属性 | 值 |
|---|---|
| **输入** | 35 只标的 |
| **输出** | 35（每个标的 ≥60 行 OHLCV 或有回退 profile） |
| **文件** | `src/openalpha/data_diagnostics.py : check_data_availability()` |
| **数据源** | PriceFetcher → Yahoo chart API → yfinance（逐层 fallback） |
| **安全底线** | 若所有标的均失败 → pool 不会被清空，评分仍可通过回退运行 |

### 5.4 SCORING_ELIGIBLE（评分资格）

| 属性 | 值 |
|---|---|
| **输入** | 35 只标的 |
| **输出** | 12（平时）|
| **文件** | `src/scoring/scorer.py : score_universe()` |

**评分模型**（`score_frame`）：
```
Base = 0.30×Volatility + 0.20×Volume + 0.20×Trend + 0.15×Repeatability + 0.10×Drawdown
Final = Base + 0.05×CorrelationBonus
```

**拒绝条件**（任一命中即拒绝）：
- `range_width_pct > 45%` — 区间太宽
- `range_width_pct < 4%` — 区间太窄
- `ATR < 1.0%` — 波动率过低
- `ATR > 12.0%` — 波动率过高
- `gap_rate > 20%` — 跳空风险
- `news_score ≥ 80` — 事件驱动
- `strong_trend == True` — 强趋势
- `too_flat == True` — 波动不足
- `range_spread < 3%` — 区间利润不足

**回退路径**（`_fallback_scored_item`）：
- `_load_history` 失败 → 检查 `FALLBACK_PROFILES`（37 个标的有配置）
- Universe Filter 验证：价格/成交量/市值检查，**跳过 ATR 验证**
- 合成评分：波动率=55+band_pct×1.5，成交量=log10(vol/1M+1)×20+35

**当前差距**：23 个被淘汰的标的在诊断中显示"原因未记录" — 评分拒绝原因未传播到管道的 dropped records

### 5.5 BASE_RANKING（基准排名）

| 属性 | 值 |
|---|---|
| **输入** | 12 只标的 |
| **输出** | 12（`score_candidate()` 精修） |
| **文件** | `src/openalpha/candidate_ranking.py : score_candidate()` |

### 5.6 FORMAL_ELIGIBILITY（正式资格）

| 属性 | 值 |
|---|---|
| **输入** | 12 |
| **输出** | 12（`formal_scoring_eligibility=True`） |
| **文件** | `src/openalpha/selector.py:641-648` |

### 5.7 DATA_QUALITY（实时数据质量检查）

| 属性 | 值 |
|---|---|
| **输入** | 12 |
| **输出（strict）** | 0（spread_unavailable — 非交易时段无实时报价） |
| **输出（relaxed）** | 12（跳过价差/波动率检查） |
| **文件** | `src/openalpha/selector.py : _apply_quality_filters_with_report()` |

**状态机**：

```
existing_position? → 放行（跳过所有检查）

no price/volume? → missing_market_data → 拒绝

volume < 500K? → volume_filter → 拒绝

strict mode?
  YES: bid_ask not confirmed? → spread_unavailable → 拒绝
       spread ≥ 0.5%? → spread_filter → 拒绝
       |3d change| > limit? → volatility_filter → 拒绝
  NO:  跳过 spread 和 volatility 检查
```

### 5.8 COMPOSITION_FILTER（组合多样性）

| 属性 | 值 |
|---|---|
| **输入（strict 回退）** | 12（pre-quality pool） |
| **输入（relaxed）** | 12（quality-passed） |
| **输出** | 5 |
| **文件** | `src/openalpha/selector.py : _select_diversified_top_k()` |

### 5.9 FORMAL_TOP（最终产出）

| 属性 | 值 |
|---|---|
| **输入** | 回退 pool 或 quality-passed |
| **输出** | 5 只候选 |

**路径选择**：

| execution_mode | quality 结果 | candidate_type | formal 从何而来 |
|---|---|---|---|
| LIVE | 全部通过 | LIVE_TRADABLE | quality_passed |
| LIVE | 全部拒绝 | RESEARCH_ONLY | pre-quality pool（回退） |
| PAPER | 全部通过 | PAPER_ELIGIBLE | quality_passed |
| PAPER | 全部拒绝 | PAPER_ELIGIBLE | pre-quality pool（放宽路径） |
| RESEARCH | 全部通过/拒绝 | RESEARCH_ONLY | quality_passed 或 pre-quality pool |

---

## 6. PAPER / LIVE / RESEARCH 执行模式 — 详细状态

### LIVE 模式

| 属性 | 值 |
|---|---|
| **触发方式** | `QUANTCAIRN_EXECUTION_MODE=LIVE` 或 preflight `FULL` 模式 |
| **质量检查** | 严格（必须实时 bid/ask） |
| **交易状态** | **已禁用** — `allow_live_order=false` 在三层强制执行 |
| **candidate_type** | `LIVE_TRADABLE`（仅当质量通过） |
| **文件** | `src/openalpha/selector.py:_resolve_execution_mode()` |

### PAPER 模式

| 属性 | 值 |
|---|---|
| **触发方式** | `QUANTCAIRN_EXECUTION_MODE=PAPER`（必须显式设置 — 永不自动选择） |
| **质量检查** | 放宽（跳过 bid/ask/spread） |
| **交易状态** | **可用** — `PaperBroker` 执行模拟成交 |
| **candidate_type** | `PAPER_ELIGIBLE` |
| **端到端验证** | ✅ 2026-07-25 通过 |
| **配置模式** | `config_writer` 强制 `mode=paper` |
| **组合持久化** | `state/paper/{account}/portfolio_state.json` |

### RESEARCH 模式

| 属性 | 值 |
|---|---|
| **触发方式** | 默认（无 env var 且 preflight 非 FULL） |
| **质量检查** | 放宽 |
| **交易状态** | 无交易 — 仅候选 |
| **candidate_type** | `RESEARCH_ONLY` |

---

## 7. 已完成能力（v0.12.0）

| # | 能力 | 验证方式 |
|---|---|---|
| 1 | 35 只托管 Universe（带 37 个回退 profile） | UniverseManager 快照 + `_load_managed_universe()` |
| 2 | 9 阶段管道 + 漏斗不变性（每阶段 output ≤ input） | FunnelTracker 每次运行验证 |
| 3 | 多因子评分（5 个因子，30/20/20/15/10 权重） | Scorer 每次管道运行均执行 |
| 4 | 模式感知质量过滤（FULL 严格，EOD/AFTER_MARKET/DEGRADED 放宽） | `_quality_mode_is_strict()` 门控 |
| 5 | LIVE/PAPER/RESEARCH 执行模式 | `_resolve_execution_mode()` + env var |
| 6 | 纸交端到端（选择器 → 配置 → 券商 → 组合状态） | 2026-07-25 验收测试通过 |
| 7 | Demo 模式（5 只合成标的，252 行，确定性） | `run_demo_selector.py` 无需 API 密钥即可运行 |
| 8 | 管道诊断 + 按标的淘汰追踪 | `print_diagnostic_report()` 每次运行均输出 |
| 9 | Telegram 通知（长消息分块，去重台账） | @QuantCairnPicks 频道 |
| 10 | 只读看板（端口 8090） | `src/dashboard/combined.py` |
| 11 | 预检市场状态检测 | `src/openalpha/preflight.py` |
| 12 | Python 包编排 | `pyproject.toml` + `quantcairn/` 命名空间（21 个符号） |
| 13 | GitHub Actions CI | `.github/workflows/test.yml` |
| 14 | 开源文档 | README, CONTRIBUTING, LICENSE, CHANGELOG, ROADMAP |
| 15 | AI 工程上下文层 | `.ai/`（4 个文件，749 行） |
| 16 | 系统状态 CLI | `scripts/status.py` |
| 17 | 开发环境检查 CLI | `scripts/check_dev_environment.py` |

---

## 8. 未完全集成功能

| 模块 | 代码位置 | 独立脚本 | 集成差距 |
|---|---|---|---|
| Market Regime Engine | `src/regime/` | `scripts/run_regime_detector.py` | 行情信号未接入选择器决策逻辑 |
| Outcome Collector | `src/outcome/collector.py` | `scripts/collect_trade_outcomes.py` | 纸交运行后未自动触发 |
| Weight Advisor | `src/outcome/weight_advisor.py` | `scripts/run_weight_advisor.py` | 建议未进入治理审批流程 |
| Governance | `src/outcome/governance.py` | `scripts/manage_governance.py` | 审批需手动触发 |
| Backtest | `src/backtest/` | 多个独立脚本 | 未接入管道评估回路 |
| Strategy Selection | `src/openalpha/strategy_selection.py` | — | 集成路径不明确 |
| Candidate Validation | `src/candidate_validation/` | `scripts/run_candidate_validation_scheduler.py` | 完全独立 |
| Shadow Observer | `src/shadow/` | `scripts/run_soxs_shadow.py` | 未耦合选择器 |

---

## 9. 已知问题

| # | 问题 | 严重程度 | 状态 | 根因 |
|---|---|---|---|---|
| 1 | 非交易时间 spread_unavailable 导致所有候选被拒 | 中 | ✅ 已修复（放宽模式） | `_apply_quality_filters_with_report()` 在 strict 模式下检查 bid/ask 确认 |
| 2 | SCORING_ELIGIBLE 淘汰的标的显示"原因未记录" | 低 | 待修复 | `score_frame()` 的 `reject_reasons` 未传播到管道的 dropped records |
| 3 | Dashboard 历史 TOP 配置显示 `mode=live` 而非实际执行模式 | 低 | 待修复 | `_load_existing_mode()` 保留旧配置的 mode 字段 |
| 4 | 8 个模块已实现但未接入主选择闭环 | 低 | 架构债务 | 独立开发，从未耦合 |
| 5 | 5 个预存在测试失败（环境泄漏，`config.local.yaml`） | 低 | 已知问题 | 与选择器功能无关 |

---

## 10. 下一步建议

### 发布准备（立即）

1. 提交 `README.md` 和 `docs/PRODUCT_OVERVIEW.md`，创建最终的公开测试版发布提交
2. 在 `LICENSE` 中确认版权所有者姓名
3. 将 `v0.12.0-public-beta-ready` 标签重新定位到最终发布提交
4. 推送分支和标签到 `origin`

### 可观测性（短期）

5. 将 `score_frame()` 的 `reject_reasons` 传播到管道的 dropped records（修复"原因未记录"）
6. 添加 `QUANTCAIRN_EXECUTION_MODE` 与 TOP 配置 `mode` 之间的一致性检查

### 集成（中期）

7. 在纸交成功后自动触发 Outcome Collector
8. 将 Weight Advisor 建议接入 Governance 审批流程
9. 将 Market Regime 信号接入选择器决策（quality 严格度、评分权重调整）

### 平台（长期）

10. 多数据源 fallback（Alpha Vantage、Polygon）
11. 回测验证框架 — 针对历史数据运行管道变更
12. Docker 部署

---

*本文档反映系统实际状态，基于提交 `1458ad9`。不假设、不计划、不营销。*
