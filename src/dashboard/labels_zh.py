"""Chinese presentation helpers for the read-only dashboard."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


STATUS_LABELS_ZH = {
    "FINALIZED": "已完成",
    "PRELIMINARY": "初步结果",
    "PREMARKET_REFRESHED": "盘前已刷新",
    "COMPLETE": "完整",
    "COMPLETED": "已完成",
    "DEGRADED": "降级",
    "INVALID": "无效",
    "RESEARCH_READY": "可进入研究",
    "RESEARCH_ONLY": "仅供研究",
    "BLOCKED": "已阻断",
    "ACTIVE": "可用",
    "STALE": "已过期",
    "UNAVAILABLE": "不可用",
    "DISABLED": "已关闭",
    "ENABLED": "已开启",
    "NOT_TRADABLE": "不可交易",
    "TIMEOUT": "超时",
    "SKIPPED_BUDGET": "因预算跳过",
    "MALFORMED_RESPONSE": "返回格式错误",
    "SAFE": "安全",
    "UNSAFE": "不安全",
    "VALID": "有效",
    "FRESH": "新鲜",
    "PASS": "通过",
    "RUNNING": "运行中",
    "PENDING": "等待中",
    "ALLOWED": "允许",
    "CONNECTED": "已连接",
    "NOT_CONNECTED": "未连接",
    "PAPER": "虚拟盘",
    "SANDBOX": "沙盒",
    "LIVE": "实盘",
    "PROD": "实盘",
    "AI_CANDIDATE": "AI 候选",
    "INSUFFICIENT_EVIDENCE": "证据不足",
    "INELIGIBLE": "不符合条件",
    "ELIGIBLE": "符合条件",
    "DRAFT": "草案",
    "APPROVED": "已批准",
    "REVIEW_REQUIRED": "等待人工复核",
    "BACKTESTED": "已回测",
    "WALK_FORWARD_VALIDATED": "滚动验证完成",
    "UNKNOWN": "未知",
    "BULL_TREND": "上涨趋势",
    "BEAR_TREND": "下跌趋势",
    "RANGE": "区间震荡",
    "UNCERTAIN": "环境不明确",
    "FAST_PRELIMINARY": "快速初选",
    "NO_ACTIONABLE_RESEARCH_CANDIDATE": "暂无可执行研究候选",
}


REASON_LABELS_ZH = {
    "low_dollar_volume": "成交额过低",
    "price_out_of_range": "价格超出范围",
    "market_cap_missing": "市值数据缺失",
    "market_cap_too_small": "市值过低",
    "atr_missing": "ATR 波动率缺失",
    "quote_missing": "报价缺失",
    "ohlcv_missing": "历史行情缺失",
    "benchmark_invalid": "基准数据无效",
    "benchmark_alignment_failed": "基准数据未对齐",
    "freshness_invalid": "数据时效无效",
    "stale_data": "数据已过期",
    "history_insufficient": "历史数据不足",
    "history_short": "历史数据不足",
    "scoring_ineligible": "不具备评分资格",
    "score_below_threshold": "分数低于门槛",
    "provider_timeout": "数据源超时",
    "provider_skipped_budget": "因预算限制跳过",
    "entry_quality_too_low": "入场质量不足",
    "leveraged_etf_limit_exceeded": "杠杆 ETF 数量超限",
    "composition_limit": "组合约束限制",
    "top_n_not_filled": "正式候选不足",
    "insufficient_candidates": "候选数量不足",
    "trade_admission_not_tradable": "未取得交易准入",
    "validation_status_ai_candidate": "仍处于研究候选状态",
    "post_filter_removed": "最终后处理移除",
    "final_selection_limit": "最终入选数量限制",
    "missing_quote": "报价缺失",
    "missing_ohlcv": "历史行情缺失",
    "benchmark_missing": "基准数据缺失",
    "daily_data_future": "日线数据来自未来",
    "critical_fallback": "关键数据发生降级",
    "low_liquidity": "流动性不足",
    "market_cap": "市值不符合要求",
    "atr": "波动率不符合要求",
    "provider_failed": "数据源失败",
    "refinement_rejected": "精筛未通过",
    "selection_state_date_mismatch": "选股数据日不一致",
    "missing_top_slot": "TOP 配置槽位缺失",
    "top_config_symbols_do_not_match_selection_state": "正式 TOP 与交易配置不一致",
    "unknown": "其他原因",
    "rank_target_allocation": "按排名目标分配",
    "leveraged_inverse_position_limit": "杠杆/反向 ETF 单仓上限",
    "standard_etf_position_limit": "ETF 单仓上限",
    "common_stock_position_limit": "普通股单仓上限",
    "selection_not_active": "选股未处于可用状态",
    "result_quality_not_complete": "结果质量不是完整",
    "research_admission_not_ready": "研究准入未就绪",
    "invalid_account_equity": "账户权益无效",
    "symbol_not_in_formal_top": "标的不在正式 TOP",
    "position_already_exists": "已有仓位，不自动加仓",
    "max_open_positions_reached": "达到最大持仓数量",
    "leveraged_inverse_count_limit": "杠杆/反向 ETF 数量已达上限",
    "gross_exposure_limit": "总仓位上限",
    "cash_reserve_limit": "现金保留要求",
    "invalid_price": "价格无效",
    "insufficient_buying_power": "可买资金不足",
    "policy_config_missing": "仓位策略配置缺失",
}


def translate_status(value: Any) -> str:
    """Return a Chinese label while retaining the original enum."""
    if value is None or not str(value).strip():
        return "暂无数据"
    original = str(value).strip()
    label = STATUS_LABELS_ZH.get(original.upper())
    return f"{label}（{original}）" if label else original


def translate_reason(value: Any) -> str:
    """Translate a structured reason code without guessing unknown values."""
    if value is None or not str(value).strip():
        return "暂无数据"
    original = str(value).strip()
    label = REASON_LABELS_ZH.get(original.lower())
    return label if label else f"未识别原因（{original}）"


def format_bool(value: Any) -> str:
    if value is True:
        return "是"
    if value is False:
        return "否"
    return "未知"


def format_optional(value: Any, *, empty: str = "暂无数据") -> str:
    if value is None:
        return empty
    if isinstance(value, bool):
        return format_bool(value)
    if isinstance(value, str) and not value.strip():
        return empty
    return str(value)


def format_timestamp(value: Any) -> str:
    """Format timestamps in Asia/Shanghai while preserving invalid input."""
    if value is None or not str(value).strip():
        return "时间未知"
    original = str(value).strip()
    try:
        parsed = datetime.fromisoformat(original.replace("Z", "+00:00"))
    except ValueError:
        return original
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    local = parsed.astimezone(ZoneInfo("Asia/Shanghai"))
    return local.strftime("%Y-%m-%d %H:%M:%S")


def truncate_identifier(value: Any, head: int = 8, tail: int = 6) -> str:
    if value is None or not str(value).strip():
        return "暂无数据"
    original = str(value).strip()
    if len(original) <= head + tail + 1:
        return original
    return f"{original[:head]}…{original[-tail:]}"
