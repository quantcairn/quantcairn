# Candidate Walk-Forward Execution Smoke Acceptance

## 1. 验收范围

本次验收仅覆盖 candidate Walk-Forward 执行链路的离线 smoke 验证，具体包括：

- `PENDING_WALK_FORWARD` dry-run
- apply 正常执行
- `WALK_FORWARD_COMPLETE` 状态更新
- 重复 apply 拦截
- 前置门槛 fail closed
- 执行中失败进入 `WALK_FORWARD_FAILED`
- Walk-Forward 产物生成
- 状态历史一致性
- Broker / Trade API 安全隔离

本次明确不验证：

- 策略真实盈利能力
- 正式参数稳定性
- 标准生产窗口配置
- Shadow 准入资格
- Paper 资格
- Live 资格

## 2. 基线

- baseline commit: `fb2496c`
- repository: `soxs-range-arbitrage`
- worktree status: `clean`
- Longbridge called: `false`
- Trade API called: `false`
- Broker created: `false`
- TradeContext created: `false`
- Order submitted: `false`

## 3. 重要测试限制

本次 smoke harness 对 Walk-Forward 窗口做了临时缩小，只用于快速验证真实 runner、状态机、产物和失败路径。

说明如下：

- 该窗口配置不是正式研究配置
- 本次 `aggregate OOS return` 不能用于策略准入
- 本次 smoke 不能作为 `PENDING_SHADOW` 的依据
- 正式准入必须重新使用标准 Walk-Forward 配置运行

## 4. 正常候选结果

candidate_id: `wf_smoke_good`

- initial_status: `PENDING_WALK_FORWARD`
- dry_run_applied: `false`
- dry_run_started: `false`
- final_status: `WALK_FORWARD_COMPLETE`
- windows: `96`
- active_window_ratio: `0.020833`
- no_trade_window_ratio: `0.979167`
- aggregate_oos_return: `0.010875`
- evidence_status: `INSUFFICIENT_EVIDENCE`
- profitability_status: `INELIGIBLE`
- deployment_status: `INELIGIBLE`

结论：

- 技术执行成功
- 产物生成成功
- 状态更新成功
- 策略资格不通过
- 不允许推进到 `PENDING_SHADOW`

## 5. dry-run 验收

dry-run 结果：

- `applied=false`
- `candidate_current_status=PENDING_WALK_FORWARD`
- `proposed_status=PENDING_WALK_FORWARD`
- `walk_forward_started=false`
- `all_trading_flags_false=true`
- 状态历史未变化
- 未生成正式 Walk-Forward 产物

## 6. apply 验收

apply 结果：

- Walk-Forward 成功执行
- 最终状态为 `WALK_FORWARD_COMPLETE`
- 只新增一条合法状态历史
- 未自动进入 `PENDING_SHADOW`
- 所有交易开关持续为 `false`

## 7. 产物验收

以下文件均已生成且可解析：

- `walk_forward_run_audit.json`
- `walk_forward_run_summary.csv`
- `walk_forward_metrics.json`
- `window_results.csv`
- `selected_parameters.csv`
- `oos_equity.csv`
- `oos_drawdown.csv`
- `window_failures.csv`
- `parameter_stability.json`
- `report.md`

一致性检查结果：

- OOS 时间戳升序
- 无重复时间戳
- `candidate_id` 一致
- `symbol / benchmarks / timeframe / strategy_family` 一致
- `quote_api_used=false`
- `trade_api_used=false`
- `broker_used=false`
- `trade_context_initialized=false`

## 8. 重复执行验收

重复使用相同 `candidate_id` 再次 apply 的结果：

- 被拒绝
- 错误：`candidate_must_be_pending_walk_forward`
- 未重复运行
- 未重复追加状态历史
- 未覆盖已有产物

## 9. 前置失败路径

candidate_id: `wf_smoke_bad_preflight`

记录：

- failure: `unsupported_strategy_family:trend_following`
- final_status: `PENDING_WALK_FORWARD`
- execution_started: `false`
- Walk-Forward 目录未生成
- 未误标 `WALK_FORWARD_FAILED`
- fail closed 正常

## 10. 执行中失败路径

candidate_id: `wf_smoke_bad_exec_3`

记录：

- final_status: `WALK_FORWARD_FAILED`
- failure_stage: `execution`
- failure_reason: `RuntimeError:smoke_execution_failure`
- 未进入 `PENDING_SHADOW`
- 安全位全部为 `false`

## 11. 状态语义验收

`WALK_FORWARD_COMPLETE` 只表示：

- 技术执行完成
- 产物完整
- 状态和审计成功写入

它不表示：

- 策略盈利
- 证据充分
- 可启动 Shadow
- 可进入 Paper
- 可进入 Live

本次候选的资格状态为：

- `evidence_status=INSUFFICIENT_EVIDENCE`
- `profitability_status=INELIGIBLE`
- `deployment_status=INELIGIBLE`

因此状态必须停留在：

- `WALK_FORWARD_COMPLETE`

不得推进：

- `PENDING_SHADOW`

## 12. 安全边界

安全确认如下：

- Longbridge called: `false`
- Quote API used: `false`
- Trade API called: `false`
- Broker created: `false`
- TradeContext created: `false`
- Order submitted: `false`
- Shadow started: `false`
- Paper started: `false`
- Live started: `false`
- Production trading behavior modified: `false`

## 13. 验收结论

最终验收结论：

- `ACCEPTED_FOR_WALK_FORWARD_EXECUTION_INFRASTRUCTURE`

同时标记：

- `NOT_ELIGIBLE_FOR_SHADOW_ADMISSION`

原因：

- smoke 使用缩小窗口
- active_window_ratio 过低
- no_trade_window_ratio 过高
- evidence_status 不通过
- profitability_status 不通过
- deployment_status 不通过

## 14. 下一阶段要求

下一阶段不是直接做 Shadow admission。

必须先：

1. 使用标准 Walk-Forward 窗口重新运行真实候选
2. 不使用 smoke 缩短配置
3. 重新评估：
   - `active_window_ratio`
   - `no_trade_window_ratio`
   - `aggregate OOS return`
   - `median window return`
   - `positive window ratio`
   - `parameter stability`
   - `benchmark sensitivity`
   - `evidence_status`
   - `profitability_status`
   - `deployment_status`
4. 只有三层资格符合门槛，才允许人工申请 `PENDING_SHADOW`

## 15. 签署栏

- Acceptance status: `ACCEPTED_FOR_WALK_FORWARD_EXECUTION_INFRASTRUCTURE`
- Strategy eligibility status: `NOT_ELIGIBLE_FOR_SHADOW_ADMISSION`
- Baseline commit: `fb2496c`
- Generated at UTC: `2026-07-13T10:49:34Z`
- Reviewed by: Codex
- Notes: 本次仅验证 candidate Walk-Forward 执行基础设施与状态机，不代表正式研究结论或准入建议。
