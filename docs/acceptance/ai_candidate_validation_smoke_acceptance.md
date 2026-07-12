# AI Candidate Validation Smoke Acceptance

## 1. 验收范围

本次验收仅覆盖以下离线能力：

- AI 候选写入
- 状态机合法迁移
- 非法候选拒绝
- 幂等性
- 状态历史追加
- 重启恢复
- Dashboard 只读展示
- 与交易系统隔离

本次明确未验证以下内容：

- 历史行情下载
- 数据质量
- 回测
- Walk-Forward
- Shadow 长期运行
- Paper
- 实盘

## 2. 基线

- commit: `33a1ca2`
- branch: `codex/paper-broker-hardening`
- smoke 临时目录: `/tmp/candidate_validation_smoke/`
- 工作区在测试前后均保持干净

## 3. 测试候选结果

| candidate | asset_type | selected_at | benchmarks | strategy_family | risk_profile status | final validation_status | rejection_reason |
| --- | --- | --- | --- | --- | --- | --- | --- |
| AAPL.US | common_stock | 2026-07-10T09:30:00+08:00 | QQQ.US / SPY.US | trend_following | balanced | PENDING_DATA_VALIDATION |  |
| AAPL.US | common_stock | 2026-07-10T12:00:00+08:00 | QQQ.US / SPY.US | trend_following | balanced | PENDING_DATA_VALIDATION |  |
| AMD.US | common_stock | 2026-07-10T10:00:00+08:00 | SOXX.US / SMH.US | trend_following | balanced | PENDING_DATA_VALIDATION |  |
| SOXS.US | inverse_etf | 2026-07-10T11:00:00+08:00 | SOXX.US / SMH.US | inverse_etf_range | strict | PENDING_DATA_VALIDATION |  |
| LABD.US | leveraged_etf | 2026-07-10T13:00:00+08:00 | XLE.US / USO.US | leveraged_range | missing / weak | REJECTED | missing_or_weak_risk_profile |
| SOXL.US | leveraged_etf | 2026-07-10T14:00:00+08:00 | SOXX.US / SMH.US | leveraged_range | strict | REJECTED | missing_or_weak_risk_profile |

## 4. 合法状态迁移

合法路径如下：

AI_CANDIDATE
→ CLASSIFIED
→ BENCHMARK_ASSIGNED
→ STRATEGY_ASSIGNED
→ PENDING_DATA_VALIDATION

本次验收确认：

- 到达 `PENDING_DATA_VALIDATION` 后停止
- 未自动下载数据
- 未自动回测
- 未自动启动 Shadow

## 5. 非法迁移验证

本次验收确认以下行为均 fail closed：

- 重复执行相同迁移：fail closed
- `AI_CANDIDATE` 跳 Shadow：禁止
- `AI_CANDIDATE` 跳 Paper：禁止
- `AI_CANDIDATE` 跳 Live：禁止
- 缺少严格 risk_profile 的 inverse / leveraged ETF：拒绝

## 6. 幂等与持久化

本次验收确认：

- 同一 `candidate_id` 重复写入不产生重复候选
- 相同 `symbol` 不同 `selected_at` 生成不同 `candidate_id`
- `candidates.jsonl` 行数: `5`
- `candidate_status_history.jsonl` 行数: `23`
- `candidate_validation_summary.csv` 行数: `5`
- 状态历史只追加
- 重启后可恢复状态
- 文件损坏时 fail closed

## 7. Dashboard 验收

本次验收确认：

- `GET /api/candidates/status = SAFE`
- `GET /api/status` 中 candidate status = SAFE
- 页面存在 `AI Candidate Validation` 卡片

页面不存在以下内容：

- 启动 Shadow
- 批准 Paper
- 批准 Live
- 买入
- 卖出
- 提交订单
- Paper / Prod 切换

## 8. 安全边界

本次验收确认以下项全部为 false / 未发生：

- Longbridge called: `false`
- Trade API called: `false`
- TradeContext created: `false`
- Broker write path used: `false`
- Order created: `false`
- Shadow started: `false`
- Paper started: `false`
- Live started: `false`
- Production behavior modified: `false`

## 9. 验收结论

最终状态：

`ACCEPTED_FOR_PENDING_DATA_VALIDATION`

本次验收结论：

- AI 候选只能进入候选库
- AI 不具备交易资格授予权
- 候选不得自动进入数据下载、回测、Shadow、Paper 或 Live
- 下一阶段必须由人工触发离线数据验证

## 10. 下一阶段门槛

下一阶段名称：

`Manual Offline Dataset Validation`

进入条件：

- `validation_status = PENDING_DATA_VALIDATION`
- `symbol / market / asset_type` 完整
- `benchmark` 已分配
- `strategy_family` 已分配
- `risk_profile` 合格
- 人工明确指定 `candidate_id`
- 本阶段仍禁止 Paper / Live / Trade API

## 签署栏

- Acceptance status: `ACCEPTED_FOR_PENDING_DATA_VALIDATION`
- Baseline commit: `33a1ca2`
- Generated at UTC: `2026-07-12`
- Reviewed by: ``
- Notes: 本次仅做离线候选准入 smoke 验收，未触发任何交易写路径。
