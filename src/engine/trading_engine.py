"""
Trading Engine: main loop that orchestrates all components.

Flow:
  1. Fetch price → 2. Feed strategy → 3. Generate signal
  → 4. Risk check → 5. Place order → 6. Notify → 7. Log

Runs in paper or live mode.
"""
import json
import logging
import math
import os
import random
import time
import calendar
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

import pytz

from ..config.loader import AppConfig
from ..data.fetcher import PriceFetcher, PriceDataError
from ..strategy.range_detector import RangeDetector, SignalType
from ..risk.manager import RiskManager, TradeRecord
from ..risk.instrument_profile import LEVERAGED_ETF_REGISTRY
from ..risk.portfolio import PortfolioRisk
from ..portfolio import PortfolioManager
from ..broker.base import BrokerBase, OrderSide, OrderStatus, OrderType
from ..broker.paper_broker import PaperBroker
from ..notifier.alerts import Notifier
from .position_sizing import determine_buy_quantity
from .ranked_position_policy import calculate_ranked_target_allocations
from ..order.order_state import OrderStateManager
from ..safety.live_guard import LiveGuard
from ..reports.pretrade_report import PretradeReport
from ..openalpha.config import load_runtime_config as load_ai_selector_runtime_config
from ..openalpha.integration import AISelector
from ..openalpha.selection_state import (
    current_top_config_symbols,
    has_live_top_configs,
    load_selection_state,
    selection_state_path,
    verify_selection_state,
    verify_live_startup_selection,
)
from ..openalpha import selection_state as selection_state_module
from ..openalpha.selection_bundle import load_committed_selection_bundle
from ..openalpha.selection_report import load_latest_ai_selection_state
from ..utils.market_calendar import required_selection_date
from ..config.runtime_paths import resolve_logs_dir, resolve_reports_dir, resolve_state_dir

logger = logging.getLogger(__name__)
PROJECT_DIR = Path(__file__).resolve().parents[2]
STATE_DIR = resolve_state_dir(PROJECT_DIR)
INVERSE_ETF_SYMBOLS = {"SOXS", "SQQQ", "SPXU", "SDOW", "FAZ"}

# Try to import pytz, fall back if not available
try:
    import pytz as _pytz
    HAS_PYTZ = True
except ImportError:
    HAS_PYTZ = False


@dataclass
class AISelectionDecision:
    enabled: bool = False
    active: bool = False
    selection_mode: str = "DISABLED"
    top10: list[dict] | None = None
    top3: list[dict] | None = None
    signal_for_ticker: Optional[dict] = None
    regime: str = "DISABLED"
    strategy: str = "range_detector"
    risk_approved: bool = True
    allocation_weight: float = 0.0
    fallback_reason: str = ""
    fallback_used: bool = False
    result_quality: str = ""
    research_admission: str = ""


def _audit_log_path() -> Path:
    configured_dir = os.environ.get("SOXS_RUNTIME_AUDIT_DIR", "").strip()
    log_dir = Path(configured_dir).expanduser().resolve() if configured_dir else resolve_logs_dir(PROJECT_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"trades-{datetime.now().strftime('%Y%m%d')}.jsonl"


def append_runtime_audit(record: dict) -> None:
    payload = dict(record or {})
    payload.setdefault(
        "timestamp",
        datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
    )
    with _audit_log_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def is_inverse_etf_symbol(symbol: str) -> bool:
    return str(symbol or "").strip().upper().split(".")[0] in INVERSE_ETF_SYMBOLS


def _positive_finite_float(value) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(result) or result <= 0:
        return 0.0
    return result


def _positive_int(value) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0


def check_exit_conditions(
    symbol,
    current_price,
    avg_cost,
    position_qty,
    is_inverse_etf=False,
    mode="normal",
):
    symbol_norm = str(symbol or "").strip().upper().split(".")[0]
    cost = _positive_finite_float(avg_cost)
    price = _positive_finite_float(current_price)
    decision = {
        "should_exit": False,
        "reason": None,
        "trigger_price": None,
        "avg_cost": cost,
        "position_qty": _positive_int(position_qty),
        "symbol": symbol_norm,
        "mode": mode,
    }
    qty = decision["position_qty"]
    if qty <= 0 or cost <= 0 or price <= 0:
        return decision

    # Strategy-specific SOXS exit semantics:
    # +5% from average cost is stop_loss; -10% is take_profit.
    if symbol_norm == "SOXS":
        stop_trigger = round(cost * 1.05, 6)
        take_trigger = round(cost * 0.90, 6)
        if price >= stop_trigger:
            decision["should_exit"] = True
            decision["reason"] = "stop_loss"
            decision["trigger_price"] = stop_trigger
        elif price <= take_trigger:
            decision["should_exit"] = True
            decision["reason"] = "take_profit"
            decision["trigger_price"] = take_trigger
        return decision

    stop_trigger = round(cost * 0.95, 6)
    take_trigger = round(cost * 1.10, 6)
    if mode == "orphan":
        take_trigger = None
        stop_trigger = round(cost * 0.92, 6)
        if price <= stop_trigger:
            decision["should_exit"] = True
            decision["reason"] = "stop_loss"
            decision["trigger_price"] = stop_trigger
        return decision

    if price <= stop_trigger:
        decision["should_exit"] = True
        decision["reason"] = "stop_loss"
        decision["trigger_price"] = stop_trigger
    elif price >= take_trigger:
        decision["should_exit"] = True
        decision["reason"] = "take_profit"
        decision["trigger_price"] = take_trigger
    return decision


class TradingEngine:
    """
    Main trading engine. Orchestrates price fetching, strategy evaluation,
    risk management, and order execution.
    """

    def __init__(
        self,
        config: AppConfig,
        ignore_trading_hours: bool = False,
        startup_role: str = "standard",
        portfolio_risk: Optional[PortfolioRisk] = None,
    ):
        self.config = config
        self.ticker = config.ticker
        self.mode = config.mode
        self._ignore_trading_hours = ignore_trading_hours
        self._startup_role = str(startup_role or "standard").strip().lower()
        state_dir = resolve_state_dir(PROJECT_DIR)

        # Initialize components
        self.fetcher = PriceFetcher(
            ticker=config.ticker,
            poll_interval=config.data.poll_interval_seconds,
        )

        self.strategy = RangeDetector(
            ticker=config.ticker,
            mode=config.range.mode,
            support_price=config.range.support_price,
            resistance_price=config.range.resistance_price,
            tolerance_pct=config.range.tolerance_pct,
            auto_lookback=config.range.auto_lookback,
            auto_refresh_minutes=config.range.auto_refresh_minutes,
            trend_ma_period=config.trend_filter.ma_period,
            trend_enabled=config.trend_filter.enabled,
            trend_min_strength=config.trend_filter.min_trend_strength,
            min_profit_per_trade=config.range.min_profit_per_trade,
            min_range_width_pct=config.range.min_range_width_pct,
            quick_stop_pct=config.range.quick_stop_pct,
            post_entry_cooldown_seconds=config.range.post_entry_cooldown_seconds,
        )

        self.risk = RiskManager(
            stop_loss_pct=config.risk.stop_loss_pct,
            daily_loss_limit=config.risk.daily_loss_limit,
            max_consecutive_losses=config.risk.max_consecutive_losses,
            max_position=config.position.max_position,
            max_drawdown_pct=config.risk.max_drawdown_pct,
            cool_down_seconds=config.position.cool_down_seconds,
            order_failure_cooldown_seconds=config.risk.order_failure_cooldown_seconds,
            state_path=state_dir / "risk" / f"{self.ticker.upper()}.json",
        )

        # Broker setup
        if config.mode in {"live", "sandbox"} and config.broker.longbridge.enabled:
            from ..broker.longbridge_broker import LongBridgeBroker
            self.broker: BrokerBase = LongBridgeBroker(
                app_key=config.broker.longbridge.app_key,
                app_secret=config.broker.longbridge.app_secret,
                access_token=config.broker.longbridge.access_token,
                account_type=config.broker.longbridge.account_type,
                region=config.broker.longbridge.region,
                environment=config.broker.longbridge.environment,
                http_url=config.broker.longbridge.http_url,
                quote_ws_url=config.broker.longbridge.quote_ws_url,
                trade_ws_url=config.broker.longbridge.trade_ws_url,
                log_path=config.broker.longbridge.log_path,
                allow_live_order=config.broker.longbridge.allow_live_order,
                execution_mode=("LIVE_EXECUTION" if config.mode == "live" else "LIVE_OBSERVE_ONLY"),
            )
            logger.info("Using Long Bridge (%s) broker", config.mode.upper())
        else:
            self.broker = PaperBroker(
                initial_cash=config.position.initial_capital,
                persist_portfolio_state=(config.mode == "paper"),
            )
            logger.info(f"Using Paper Trading broker (initial capital: ${config.position.initial_capital:,.2f})")

        # Order state tracking: dedup, cooldown, buying-power block
        self.order_state = OrderStateManager(
            ticker=config.ticker,
            mode=config.mode,
            cooldown_seconds=config.risk.order_failure_cooldown_seconds,
            state_dir=state_dir,
        )

        self.notifier = Notifier(
            console=config.notifications.console,
            macos_notification=config.notifications.macos_notification,
            webhook_url=config.notifications.webhook_url,
            trade_summary_interval=config.notifications.trade_summary_interval,
            telegram_bot_token=config.notifications.telegram_bot_token,
            telegram_chat_id=config.notifications.telegram_chat_id,
        )

        # State
        self._running = False
        self._entry_price: Optional[float] = None
        self._position_shares: int = 0
        self._last_signal_type: Optional[SignalType] = None
        self._start_time: Optional[datetime] = None
        self._latest_account = None
        self._latest_position = None
        self._latest_snapshot_at: Optional[datetime] = None
        self._trade_in_progress = False
        self._last_signal_reason: str = "暂无"
        self._pending_order: Optional[dict] = None
        self._last_exit_check_at = 0.0
        self._pending_order_state_path = (
            state_dir / "pending_orders" / f"{self.ticker.upper()}.json"
        )
        self._position_sync_state_path = (
            state_dir / "position_sync" / f"{self.ticker.upper()}.json"
        )
        self._sell_lock_path = (
            state_dir / "sell_locks" / f"{self.ticker.upper()}.lock"
        )
        self._exit_fence_path = (
            state_dir / "exit_fences" / f"{self.ticker.upper()}.json"
        )
        self._position_sync_fence: Optional[dict] = None
        self._load_pending_order()
        self._load_position_sync_fence()
        # ── Live safety: all live engines start in reduce-only by default ──
        # B1: restore reduce-only live startup gate
        # B3: AI 0/3 selection also leaves engine in reduce-only
        self._live_arming_status: str = "DISARMED"  # DISARMED | REDUCE_ONLY | ARMED
        if self.mode == "live":
            self._reduce_only = True  # Always start reduce-only
            self._live_arming_status = "REDUCE_ONLY"
        else:
            self._reduce_only = bool(getattr(config.position, "reduce_only", False))
        if self.mode == "live" and (
            self._position_sync_fence
            or (
                self._pending_order
                and str(self._pending_order.get("side") or "").upper() == "SELL"
            )
        ):
            self._acquire_sell_lock("restored_sell_protection")

        # NY timezone
        self._ny_tz = _pytz.timezone("America/New_York") if HAS_PYTZ else None
        self._ai_selection = AISelectionDecision()
        self._ai_selection_signature: tuple[str, ...] | None = None

        # Signal & order cooldown
        self._signal_last_time: float = 0.0
        self._last_order_time: float = 0.0
        self._live_guard_verdict: dict | None = None

        # Consecutive data error tracking
        self._consecutive_data_errors: int = 0

        # Wire RiskManager ticker
        self.risk.set_ticker(self.ticker)

        # Portfolio-level risk (shared across engines, optional)
        self.portfolio_risk: Optional[PortfolioRisk] = portfolio_risk
        portfolio_cfg = getattr(config, "portfolio", None)
        self.portfolio_manager = PortfolioManager(
            max_positions=int(getattr(portfolio_cfg, "max_positions", 3) or 3),
            max_total_exposure=float(getattr(portfolio_cfg, "max_total_exposure", 1.0) or 1.0),
            max_total_risk=float(getattr(portfolio_cfg, "max_total_risk", 0.05) or 0.05),
            leveraged_etf_max_single_position=float(
                getattr(portfolio_cfg, "leveraged_etf_max_single_position", 0.15) or 0.15
            ),
            leveraged_etf_max_group_exposure=float(
                getattr(portfolio_cfg, "leveraged_etf_max_group_exposure", 0.50) or 0.50
            ),
        )
        logger.info(
            "Portfolio guard enabled: %s | max_positions: %s | max_total_exposure: %s | "
            "max_total_risk: %s | leveraged_etf_max_single_position: %s | "
            "leveraged_etf_max_group_exposure: %s",
            str(bool(getattr(portfolio_cfg, "enabled", False))).lower(),
            getattr(portfolio_cfg, "max_positions", 3),
            getattr(portfolio_cfg, "max_total_exposure", 1.0),
            getattr(portfolio_cfg, "max_total_risk", 0.05),
            getattr(portfolio_cfg, "leveraged_etf_max_single_position", 0.15),
            getattr(portfolio_cfg, "leveraged_etf_max_group_exposure", 0.50),
        )

    def _seed_auto_range(self) -> None:
        """Seed auto range from recent OHLCV history so it's ready immediately."""
        if self.config.range.mode != "auto":
            return

        candles = self.fetcher.get_ohlcv(period="5d", interval="5m")
        if candles and self.strategy.seed_from_ohlcv(candles):
            rs = self.strategy.get_range_state()
            logger.info(
                f"Seeded auto range: ${rs.support:.2f} – ${rs.resistance:.2f} "
                f"({rs.spread_pct:.1f}% spread)"
            )
            return

        if self._seed_auto_range_from_report():
            rs = self.strategy.get_range_state()
            logger.warning(
                "Seeded auto range from AI report fallback: $%.2f – $%.2f (%.1f%% spread)",
                rs.support,
                rs.resistance,
                rs.spread_pct,
            )
            return

        logger.warning("Could not seed auto range — waiting for live data")

    def _seed_auto_range_from_report(self) -> bool:
        try:
            data = load_latest_ai_selection_state(PROJECT_DIR)
        except Exception:
            return False
        candidates = data.get("top5") if isinstance(data, dict) else None
        if not isinstance(candidates, list):
            return False
        for item in candidates:
            if not isinstance(item, dict):
                continue
            if str(item.get("ticker") or "").strip().upper() != self.ticker.upper():
                continue
            try:
                support = float(item.get("range_low"))
                resistance = float(item.get("range_high"))
            except (TypeError, ValueError):
                return False
            return self.strategy.apply_auto_range(
                support,
                resistance,
                confidence=0.2,
                source="ai_report_fallback",
            )
        return False

    def _try_arm_live_ordering(self) -> None:
        """B1+B2+B3: Require ALL safety conditions before enabling live BUY.

        Called once during startup after broker connect, LiveGuard, and
        SelectionBundle checks have completed.  On success, lifts
        reduce_only to False and sets arming status to ARMED.
        """
        if self.mode != "live":
            return
        if self._live_arming_status == "ARMED":
            return  # Already armed

        blocking: list[str] = []

        # ── B2: allow_live_order AND logic ──────────────────────────
        lb = self.config.broker.longbridge
        top_allow = bool(getattr(lb, "allow_live_order", False))

        # Check config.local.yaml for live-order permission.
        # Must be explicitly true — environment=prod alone does NOT
        # grant live-order authority.
        local_allow = False
        try:
            from ..config.runtime_values import load_private_longbridge_config
            private = load_private_longbridge_config()
            local_allow = bool(private.get("allow_live_order", False))
        except Exception:
            pass

        broker_live = (
            bool(getattr(lb, "enabled", False))
            and str(getattr(lb, "environment", "") or "").strip().lower() == "prod"
            and str(getattr(lb, "account_type", "") or "").strip().lower()
            not in {"paper", "demo", "sandbox"}
        )

        live_guard_ok = bool(
            (self._live_guard_verdict or {}).get("allowed_to_open_new_positions", False)
        )

        # ── B3: selection authority check ───────────────────────────
        selection_active = False
        ticker_selected = False
        try:
            from ..openalpha.selection_state import (
                load_selection_state,
                current_top_config_symbols,
            )
            from ..utils.market_calendar import required_selection_date
            state = load_selection_state()
            if state:
                sel_date = str(state.get("et_date") or "")
                req_date = required_selection_date()
                selected_symbols = [
                    str(s or "").strip().upper()
                    for s in (state.get("selected_symbols") or state.get("selection_symbols") or [])
                    if str(s or "").strip()
                ]
                ticker_selected = self.ticker.upper() in selected_symbols
                selection_active = (
                    len(selected_symbols) > 0
                    and bool(sel_date)
                    and bool(req_date)
                )
        except Exception:
            pass

        # ── Assess ──────────────────────────────────────────────────
        effective_allow = all([
            top_allow,
            local_allow,
            broker_live,
            live_guard_ok,
            selection_active,
            ticker_selected,
        ])

        if not top_allow:
            blocking.append("top_allow_live_order=false")
        if not local_allow:
            blocking.append("local_allow_live_order=false")
        if not broker_live:
            blocking.append("broker_not_prod_live")
        if not live_guard_ok:
            blocking.append("live_guard_blocked")
        if not selection_active:
            blocking.append("no_active_selection")
        if not ticker_selected:
            blocking.append("ticker_not_in_selected_symbols")

        audit = {
            "ticker": self.ticker,
            "live_order_gate": {
                "top_allow_live_order": top_allow,
                "local_allow_live_order": local_allow,
                "broker_live": broker_live,
                "live_guard_ok": live_guard_ok,
                "selection_active": selection_active,
                "ticker_selected": ticker_selected,
                "effective_allow": effective_allow,
                "blocking_reasons": blocking,
            },
            "arming_status": self._live_arming_status,
            "reduce_only": self._reduce_only,
        }
        self._write_runtime_audit("live_arming_check", **audit)

        if effective_allow:
            self._reduce_only = False
            self._live_arming_status = "ARMED"
            logger.info(
                "🔓 Live ordering ARMED for %s — all safety gates passed. "
                "New BUY positions allowed.",
                self.ticker,
            )
        else:
            logger.warning(
                "🔒 Live ordering BLOCKED for %s: %s. "
                "Engine running in reduce-only mode.",
                self.ticker,
                ", ".join(blocking),
            )

    def _ai_fallback_policy(self) -> tuple[bool, bool, float]:
        ai_cfg = getattr(self.config, "ai_selector", None)
        allow_paper = bool(getattr(ai_cfg, "allow_fallback_paper_entries", False))
        allow_live = bool(getattr(ai_cfg, "allow_fallback_live_entries", False))
        try:
            multiplier = float(
                getattr(ai_cfg, "fallback_paper_position_multiplier", 0.25) or 0.25
            )
        except (TypeError, ValueError):
            multiplier = 0.25
        return allow_paper, allow_live, max(0.0, multiplier)

    def _build_ai_buy_plan(self, acct, current_price: float, ask: float) -> dict:
        cash = float(getattr(acct, "cash", 0.0) or 0.0)
        buying_power = float(getattr(acct, "buying_power", 0.0) or 0.0)
        # Ordinary BUY sizing must stay cash-constrained; do not expand sizing
        # from margin buying power.
        available_cash = max(0.0, min(cash, buying_power) if buying_power > 0 else cash)
        if not self._ranked_position_policy_enabled():
            available_cash = self._cap_ai_available_cash(available_cash, acct)
        execution_price = ask if ask > 0 else current_price
        ranked_allocation = self._ranked_position_policy_allocation(acct)
        configured_size = self.config.position.size_per_trade
        if ranked_allocation is not None:
            available_cash = min(available_cash, float(ranked_allocation.get("available_increment_notional") or 0.0))
            configured_size = int(ranked_allocation.get("target_shares") or 0)
        original_target_shares = determine_buy_quantity(
            current_price=current_price,
            available_cash=available_cash,
            configured_size=configured_size,
            max_position=self.config.position.max_position,
            execution_price=execution_price,
        )
        return {
            "cash": cash,
            "buying_power": buying_power,
            "available_cash": available_cash,
            "execution_price": execution_price,
            "original_target_shares": int(original_target_shares or 0),
            "ranked_allocation": ranked_allocation,
        }

    def _ranked_position_policy_enabled(self) -> bool:
        policy = getattr(self.config, "position_policy", None)
        if policy is None:
            return False
        if self.mode != "paper":
            return False
        return (
            str(getattr(policy, "mode", "") or "").strip().lower() == "ranked_aggressive"
            and bool(getattr(policy, "paper_position_policy_enabled", False))
        )

    def _ranked_position_policy_allocation(self, acct) -> dict | None:
        if not self._ranked_position_policy_enabled():
            return None
        top3 = list(getattr(self._ai_selection, "top3", None) or [])
        if not top3:
            return {
                "allocation_status": "BLOCKED",
                "allocation_reason": "selection_not_active",
                "available_increment_notional": 0.0,
            }
        positions = getattr(acct, "positions", None)
        if positions is None:
            try:
                positions = self.broker.get_positions()
            except Exception:
                positions = []
        result = calculate_ranked_target_allocations(
            top3,
            account_equity=float(getattr(acct, "equity", 0.0) or 0.0),
            current_positions=positions,
            current_cash=float(getattr(acct, "cash", 0.0) or 0.0),
            policy=getattr(self.config, "position_policy", None),
            selection_mode=str(getattr(self._ai_selection, "selection_mode", "") or ""),
            result_quality=str(getattr(self._ai_selection, "result_quality", "") or ""),
            research_admission=str(getattr(self._ai_selection, "research_admission", "") or ""),
        )
        symbol = str(self.ticker or "").strip().upper().split(".")[0]
        for row in result.get("target_allocations", []):
            if str(row.get("symbol") or "").strip().upper().split(".")[0] == symbol:
                return dict(row)
        return {
            "allocation_status": "BLOCKED",
            "allocation_reason": "symbol_not_in_formal_top",
            "available_increment_notional": 0.0,
        }

    def _emit_risk_decision(self, payload: dict) -> None:
        record = {
            "ticker": self.ticker,
            "symbol": self.ticker,
            "execution_mode": self.mode,
            "reduce_only": self._reduce_only,
            **(payload or {}),
        }
        logger.info(
            "risk_decision: %s",
            json.dumps(record, ensure_ascii=False, sort_keys=True, default=str),
        )
        self._write_runtime_audit("risk_decision", **record)

    # ---- Main Loop ----

    def run(self) -> None:
        """Start the main trading loop. Blocks until interrupted."""
        self._running = True
        self._start_time = datetime.now()

        # Connect broker
        if not self.broker.connect():
            self.notifier.alert("Failed to connect to broker", "error")
            return

        if self.mode == "sandbox":
            confirm_fn = getattr(self.broker, "confirm_sandbox_first_run", None)
            if callable(confirm_fn):
                try:
                    bootstrap = confirm_fn(self.ticker)
                    if isinstance(bootstrap, dict):
                        if bootstrap.get("confirmed"):
                            logger.info(
                                "Sandbox first-run bootstrap confirmed for %s; BUY flow enabled after read-only checks",
                                self.ticker,
                            )
                        else:
                            logger.warning(
                                "Sandbox first-run bootstrap pending for %s: %s",
                                self.ticker,
                                bootstrap.get("reason") or "read-only checks incomplete",
                            )
                    elif bootstrap:
                        logger.info(
                            "Sandbox first-run bootstrap confirmed for %s; BUY flow enabled after read-only checks",
                            self.ticker,
                        )
                    else:
                        logger.warning(
                            "Sandbox first-run bootstrap pending for %s", self.ticker
                        )
                except Exception as exc:
                    logger.warning("Sandbox first-run bootstrap failed for %s: %s", self.ticker, exc)

        if self.mode == "live" and not self._verify_live_startup_safety():
            self.broker.disconnect()
            return

        if self.mode == "live" and not self._adopt_active_live_order():
            self.broker.disconnect()
            return

        # ---- LiveGuard pre-flight check (live only) ----
        if self.mode == "live":
            guard = LiveGuard()
            context = {"mode": self.mode, "broker": self.broker, "ticker": self.ticker}
            self._live_guard_verdict = guard.validate_live_start(context)
            for err in self._live_guard_verdict.get("errors", []):
                logger.error("LiveGuard error: %s", err)
            for warn in self._live_guard_verdict.get("warnings", []):
                logger.warning("LiveGuard warning: %s", warn)

            # Generate pretrade report
            report_ctx = dict(context)
            report_ctx["live_guard_verdict"] = self._live_guard_verdict
            report = PretradeReport.generate(report_ctx)
            report.write()

            if not self._live_guard_verdict.get("allowed_to_open_new_positions", False):
                self._reduce_only = True
                self._live_arming_status = "REDUCE_ONLY"
                logger.info("LiveGuard: reduce-only mode enforced (new positions blocked)")
            else:
                logger.info("LiveGuard: all checks passed — new positions allowed")

            # ── B1+B2+B3: Attempt to arm live ordering ──
            self._try_arm_live_ordering()

        # Do NOT re-run AI Selector at live engine startup — only read existing state.
        if self.mode != "live":
            self._initialize_ai_selector()

        # Seed auto range from historical data (so it's ready immediately)
        self._seed_auto_range()

        self._print_header()

        # Startup jitter: stagger initial polls to keep engines desynchronized
        startup_delay = random.uniform(0, 15.0)
        if startup_delay > 0.5:
            logger.info("🕐 Startup jitter: sleeping %.1fs to desync from other engines", startup_delay)
            self._running_for_sleep = self._running
            for _ in range(int(startup_delay)):
                if not self._running:
                    break
                time.sleep(1)

        self._loop_error_count = 0
        try:
            while self._running:
                try:
                    self._run_one_loop()
                except Exception as exc:
                    self._loop_error_count += 1
                    logger.exception("Unhandled exception in main loop (#%d): %s", self._loop_error_count, exc)
                    self._last_signal_reason = f"循环异常 ({exc})，{self._loop_error_count}/5"
                    if self._loop_error_count >= 5:
                        logger.critical("Too many consecutive loop errors — shutting down engine")
                        self.notifier.alert(f"引擎异常关闭：连续 {self._loop_error_count} 次循环错误", "error")
                        break
                    self._sleep_with_status(10, "循环异常保护，10秒后重试")
                    continue

        except KeyboardInterrupt:
            logger.info("\nShutdown requested...")
        finally:
            self._shutdown()

    def _run_one_loop(self) -> None:
        """Execute one iteration of the main trading loop."""
        loop_start = time.time()

        # 1. Check trading hours
        if not self._is_trading_hours():
            self._refresh_broker_snapshots(outside_trading_hours=True)
            # Randomize after-hours sleep (45-75s) to desync multiple engines
            after_hours_sleep = random.randint(45, 75)
            self._sleep_with_status(after_hours_sleep, "Outside trading hours")
            return

        # 2. Fetch price
        try:
            quote = self.fetcher.get_quote()
        except PriceDataError as exc:
            self._consecutive_data_errors += 1
            logger.warning(
                "PriceDataError (%d/10) for %s: %s",
                self._consecutive_data_errors,
                self.ticker,
                exc,
            )
            if self._consecutive_data_errors > 10:
                logger.critical(
                    "Too many consecutive data errors (%d) — halting trading for %s",
                    self._consecutive_data_errors,
                    self.ticker,
                )
                self.notifier.alert(
                    f"数据获取连续失败 {self._consecutive_data_errors} 次，已停止 {self.ticker} 交易",
                    "error",
                )
                self._running = False
                return
            self._last_signal_reason = f"数据获取失败 ({self._consecutive_data_errors}/10): {exc}"
            self._sleep_with_status(15, "Price data unavailable, retrying")
            return

        if quote is None or quote.price <= 0:
            # get_quote returned None — this shouldn't happen now that PriceDataError
            # is raised above, but guard defensively
            self._sleep_with_status(5, "Waiting for valid price data")
            return

        # Reset consecutive errors on successful fetch
        self._consecutive_data_errors = 0

        current_price = quote.price

        # 3. Feed price + volume to strategy
        self.strategy.feed_price(current_price)
        if quote.high_1m is not None and quote.low_1m is not None:
            self.strategy.feed_volume_bar(
                high=quote.high_1m,
                low=quote.low_1m,
                close=current_price,
                volume=quote.volume,
            )

        # 4. Auto-refresh range if needed
        if self.config.range.mode == "auto" and self.strategy.needs_auto_refresh():
            candles = self.fetcher.get_ohlcv(period="1d", interval="5m")
            if candles:
                for c in candles[-self.strategy.auto_lookback:]:
                    self.strategy.feed_volume_bar(
                        high=c.high, low=c.low,
                        close=c.close, volume=c.volume,
                    )
            self.strategy.update_auto_range()

        # 5. Get current position
        pos = self.broker.get_position_for_ticker(self.ticker)
        positions_reliable = getattr(
            self.broker, "is_positions_snapshot_reliable", lambda: True
        )()
        if self.mode == "live" and not positions_reliable:
            self._last_signal_reason = "券商持仓状态无法确认，已暂停本轮交易"
            logger.error("%s: %s", self.ticker, self._last_signal_reason)
            # Random sleep (15-25s) to desync retry timing across engines
            retry_sleep = random.randint(15, 25)
            self._sleep_with_status(retry_sleep, self._last_signal_reason)
            return
        observed_shares = pos.quantity if pos else 0
        self._position_shares = self._apply_position_sync_fence(observed_shares)
        has_position = self._position_shares > 0

        # 6. Sync equity for risk calculations
        acct = self.broker.get_account()
        self._latest_position = pos
        self._latest_account = acct
        self._latest_snapshot_at = datetime.now()
        self.risk.update_equity(acct.equity)

        # 6a. Update portfolio-level risk position values
        if self.portfolio_risk is not None:
            if pos and pos.quantity > 0:
                position_market_value = max(0.0, float(getattr(pos, "market_value", 0) or 0.0))
                if position_market_value <= 0 and current_price > 0:
                    position_market_value = pos.quantity * current_price
                self.portfolio_risk.set_position_value(self.ticker, position_market_value)
            else:
                self.portfolio_risk.set_position_value(self.ticker, 0.0)

        if pos:
            self._position_shares = self._apply_position_sync_fence(pos.quantity)
            if self._position_shares > 0 and pos.avg_entry_price > 0:
                self._entry_price = pos.avg_entry_price
        elif not self._pending_order and not self._position_sync_fence:
            self._position_shares = 0
            self._entry_price = None

        # 6b. Update broker price (for P&L calc)
        if isinstance(self.broker, PaperBroker):
            self.broker.update_price(self.ticker, current_price)

        # 6c. Reconcile pending live orders before evaluating new signals.
        self._reconcile_pending_order()
        has_position = self._position_shares > 0

        # 6d. Dynamic exit checks run independently from the range signal.
        if self._should_run_exit_check(loop_start):
            self._last_exit_check_at = loop_start
            self._run_dynamic_exit_check(current_price, quote.bid)
            has_position = self._position_shares > 0

        if self.mode == "paper":
            self._initialize_ai_selector()

        # 7. Evaluate strategy (with signal cooldown)
        now_ts = time.time()
        signal_interval = getattr(self.config, 'signal_interval_seconds', 60)
        if now_ts - self._signal_last_time < signal_interval:
            # Skip full signal evaluation; only do stop-loss + heartbeat
            if has_position and self._entry_price and not self._pending_order:
                rs = self.strategy.get_range_state()
                sl_check = self.risk.check_stop_loss(current_price, self._entry_price, rs.support)
                if not sl_check.allowed:
                    self._handle_stop_loss(
                        self.strategy.evaluate(current_price, has_position),
                        current_price, quote.bid)
            rs = self.strategy.get_range_state()
            self.notifier.heartbeat(
                self.ticker, current_price, rs,
                trend_info=self.strategy.get_trend_info(),
                halted=self.risk._halted,
            )
            elapsed = time.time() - loop_start
            time.sleep(max(0, self.config.data.poll_interval_seconds - elapsed))
            return
        self._signal_last_time = now_ts
        signal = self.strategy.evaluate(current_price, has_position)
        self._last_signal_type = signal.type
        self._last_signal_reason = signal.reason

        # 8. Act on signal — skip non-critical signals during halt
        is_halted = self.risk._halted

        if self._pending_order:
            self._last_signal_reason = (
                f"等待订单成交：{self._pending_order['side']} {self._pending_order['order_id'][:12]}"
            )
        elif self._position_sync_fence:
            pass
        elif signal.type == SignalType.BUY and not has_position:
            # ---- BUY pre-check chain (order: dedup → cooldown → position → buying-power) ----
            if self._reduce_only:
                self._last_signal_reason = "HOLD: 仅减仓模式，今晚不新开仓"
            elif self.order_state.has_pending_order:
                self._last_signal_reason = (
                    f"HOLD: 已有待成交订单 {self.order_state.pending_order_id[:12]}"
                )
            elif self.order_state.is_blocked:
                self._last_signal_reason = self.order_state.blocked_reason
            elif has_position:
                self._last_signal_reason = "HOLD: 已有持仓，跳过买入"
            elif not self._ai_entry_allowed():
                plan = self._build_ai_buy_plan(acct, current_price, quote.ask)
                fallback_used = bool(self._ai_selection.fallback_used)
                allow_paper, allow_live, multiplier = self._ai_fallback_policy()
                self._last_signal_reason = self._blocked_ai_reason()
                self._emit_risk_decision(
                    {
                        "ticker": self.ticker,
                        "mode": self.mode,
                        "signal": signal.type.value,
                        "fallback_used": fallback_used,
                        "allow_fallback_paper_entries": allow_paper,
                        "allow_fallback_live_entries": allow_live,
                        "risk_approved": False,
                        "blocked_by": "ai_entry_gate",
                        "reason": self._last_signal_reason,
                        "original_target_shares": int(plan["original_target_shares"]),
                        "adjusted_target_shares": 0,
                        "position_multiplier": multiplier if fallback_used else 1.0,
                        "current_price": current_price,
                        "buying_power": float(getattr(acct, "buying_power", 0.0) or 0.0),
                        "available_cash": float(plan["available_cash"]),
                        "required_cash": float(plan["original_target_shares"]) * float(plan["execution_price"]),
                        "portfolio_guard_enabled": self._portfolio_guard_enabled(),
                        "portfolio_allowed": None,
                        "portfolio_reason": "not_evaluated",
                        "order_state_blocked": bool(self.order_state.is_blocked),
                        "order_state_reason": self.order_state.blocked_reason,
                        "final_action": "blocked",
                        "reduce_only": self._reduce_only,
                    }
                )
            elif not is_halted:
                self._handle_buy_signal(signal, current_price, quote.ask)
            else:
                self._last_signal_reason = "HOLD: 交易暂停中"

        elif signal.type == SignalType.SELL and has_position:
            self._handle_sell_signal(signal, current_price, quote.bid)

        elif signal.type == SignalType.STOP_LOSS and has_position:
            self._handle_stop_loss(signal, current_price, quote.bid)

        # 9. Risk: check stop loss (always fires, even during halt)
        if has_position and self._entry_price and not self._pending_order:
            rs = self.strategy.get_range_state()
            sl_check = self.risk.check_stop_loss(
                current_price, self._entry_price, rs.support
            )
            if not sl_check.allowed:
                self._handle_stop_loss(signal, current_price, quote.bid)

        # 10. Heartbeat display (always show, with halt indicator)
        rs = self.strategy.get_range_state()
        self.notifier.heartbeat(
            self.ticker, current_price, rs,
            trend_info=self.strategy.get_trend_info(),
            halted=is_halted,
        )

        # 11. Show signal in console
        if signal.type not in (SignalType.HOLD, SignalType.TREND_BLOCK):
            if not is_halted:
                self.notifier.signal(
                    self.ticker, signal.type.value, signal.price, signal.reason
                )

        # 12. Sleep until next poll (with jitter to desynchronize multiple engines)
        elapsed = time.time() - loop_start
        base_sleep = self.config.data.poll_interval_seconds - elapsed
        # ±20% jitter per poll cycle
        jitter = random.uniform(0.8, 1.2)
        sleep_time = max(0, base_sleep * jitter)
        time.sleep(sleep_time)

    def _refresh_broker_snapshots(self, outside_trading_hours: bool = False) -> None:
        """Refresh cached position/account snapshots without evaluating signals."""
        # After hours: poll less frequently to avoid API rate limits
        if outside_trading_hours:
            # Skip every other after-hours poll via random skip
            if random.random() < 0.5:
                return
        try:
            pos = self.broker.get_position_for_ticker(self.ticker)
            positions_reliable = getattr(
                self.broker, "is_positions_snapshot_reliable", lambda: True
            )()
            if self.mode == "live" and not positions_reliable:
                self._last_signal_reason = "券商持仓状态无法确认，已暂停仓位同步"
                logger.error("%s: %s", self.ticker, self._last_signal_reason)
                return

            acct = self.broker.get_account()
            account_reliable = getattr(
                self.broker, "is_account_snapshot_reliable", lambda: True
            )()
            if self.mode == "live" and not account_reliable:
                self._last_signal_reason = "券商账户状态无法确认，已暂停账户同步"
                logger.error("%s: %s", self.ticker, self._last_signal_reason)
                return

            observed_shares = pos.quantity if pos else 0
            self._position_shares = self._apply_position_sync_fence(observed_shares)
            self._latest_position = pos
            self._latest_account = acct
            self._latest_snapshot_at = datetime.now()
            self.risk.update_equity(acct.equity)

            # Check if buying power changed — lift blocked state if so
            if self.mode == "live" and hasattr(acct, "buying_power"):
                self.order_state.maybe_clear_block_on_bp_change(
                    float(getattr(acct, "buying_power", 0.0) or 0.0)
                )

            if pos:
                self._position_shares = self._apply_position_sync_fence(pos.quantity)
                if self._position_shares > 0 and pos.avg_entry_price > 0:
                    self._entry_price = pos.avg_entry_price
            elif not self._pending_order and not self._position_sync_fence:
                self._position_shares = 0
                self._entry_price = None

            if outside_trading_hours and self.mode == "live" and self._position_shares > 0:
                self._last_signal_reason = "盘后仅同步真实持仓，不执行新交易"
        except Exception as exc:
            logger.exception("Failed to refresh broker snapshots for %s: %s", self.ticker, exc)

    def stop(self) -> None:
        """Gracefully stop the engine."""
        self._running = False

    # ---- Signal Handlers ----

    def _handle_buy_signal(self, signal, current_price: float, ask: float) -> None:
        """Handle a BUY signal with auto position sizing."""
        if self._reduce_only:
            self._last_signal_reason = "仅减仓模式：今晚不新开仓"
            return

        # ── B5: exit fence — block re-buy after risk/orphan exits ──
        if self._exit_fence_active():
            self._last_signal_reason = "BUY blocked: exit fence active (risk/orphan exit cooldown)"
            return

        self._trade_in_progress = True
        try:
            try:
                acct = self.broker.get_account()
            except Exception as exc:
                self._last_signal_reason = "组合风控启用但无法读取账户/持仓，禁止新买入"
                logger.warning("Failed to read broker account for %s buy path: %s", self.ticker, exc)
                self.notifier.alert(self._last_signal_reason, "warning")
                self._write_runtime_audit(
                    "portfolio_risk_blocked",
                    ticker=self.ticker,
                    quantity=0,
                    price=current_price,
                    reason="portfolio_state_unavailable",
                    current_exposure=None,
                    projected_exposure=None,
                    current_leveraged_etf_exposure=None,
                    projected_leveraged_etf_exposure=None,
                    warning="account_unavailable",
                    portfolio_enabled=self._portfolio_guard_enabled(),
                )
                return
            plan = self._build_ai_buy_plan(acct, current_price, ask)
            cash = float(plan["cash"])
            buying_power = float(plan["buying_power"])
            available_cash = float(plan["available_cash"])
            shares = int(plan["original_target_shares"])
            execution_price = float(plan["execution_price"])
            fallback_used = bool(self._ai_selection.fallback_used)
            allow_paper, allow_live, multiplier = self._ai_fallback_policy()
            policy_reason = "ai_entry_allowed"
            adjusted_shares = shares

            if fallback_used:
                if self.mode == "live":
                    policy_reason = "fallback_used_live_blocked"
                elif self.mode == "paper":
                    if not allow_paper:
                        policy_reason = "fallback_used_blocked"
                    else:
                        adjusted_shares = int(math.floor(shares * multiplier))
                        if adjusted_shares < 1:
                            policy_reason = "fallback_reduced_size_below_minimum"
                        else:
                            policy_reason = "fallback_used_paper_allowed_with_reduced_size"
                else:
                    policy_reason = "fallback_used_blocked"

            if fallback_used and policy_reason not in {
                "fallback_used_paper_allowed_with_reduced_size",
            }:
                self._last_signal_reason = policy_reason
                self._emit_risk_decision(
                    {
                        "ticker": self.ticker,
                        "mode": self.mode,
                        "signal": getattr(signal.type, "value", str(signal.type)),
                        "fallback_used": fallback_used,
                        "allow_fallback_paper_entries": allow_paper,
                        "allow_fallback_live_entries": allow_live,
                        "risk_approved": False,
                        "blocked_by": "ai_fallback_policy",
                        "reason": policy_reason,
                        "original_target_shares": shares,
                        "adjusted_target_shares": 0,
                        "position_multiplier": multiplier if self.mode == "paper" else 0.0,
                        "current_price": current_price,
                        "buying_power": buying_power,
                        "available_cash": available_cash,
                        "required_cash": shares * execution_price,
                        "portfolio_guard_enabled": self._portfolio_guard_enabled(),
                        "portfolio_allowed": None,
                        "portfolio_reason": "not_evaluated",
                        "order_state_blocked": bool(self.order_state.is_blocked),
                        "order_state_reason": self.order_state.blocked_reason,
                        "final_action": "blocked",
                        "reduce_only": self._reduce_only,
                    }
                )
                return

            if fallback_used and adjusted_shares < 1:
                self._last_signal_reason = policy_reason
                self._emit_risk_decision(
                    {
                        "ticker": self.ticker,
                        "mode": self.mode,
                        "signal": getattr(signal.type, "value", str(signal.type)),
                        "fallback_used": fallback_used,
                        "allow_fallback_paper_entries": allow_paper,
                        "allow_fallback_live_entries": allow_live,
                        "risk_approved": False,
                        "blocked_by": "ai_fallback_policy",
                        "reason": policy_reason,
                        "original_target_shares": shares,
                        "adjusted_target_shares": 0,
                        "position_multiplier": multiplier,
                        "current_price": current_price,
                        "buying_power": buying_power,
                        "available_cash": available_cash,
                        "required_cash": shares * execution_price,
                        "portfolio_guard_enabled": self._portfolio_guard_enabled(),
                        "portfolio_allowed": None,
                        "portfolio_reason": "not_evaluated",
                        "order_state_blocked": bool(self.order_state.is_blocked),
                        "order_state_reason": self.order_state.blocked_reason,
                        "final_action": "blocked",
                        "reduce_only": self._reduce_only,
                    }
                )
                return

            if fallback_used and adjusted_shares != shares:
                shares = adjusted_shares

            if shares <= 0:
                ranked_allocation = plan.get("ranked_allocation")
                ranked_reason = ""
                if isinstance(ranked_allocation, dict):
                    ranked_reason = str(ranked_allocation.get("allocation_reason") or "").strip()
                self._last_signal_reason = ranked_reason or (
                    f"买入数量为 0：购买力 ${buying_power:.2f} / 现金 ${cash:.2f} "
                    f"不足以买入 ${max(current_price, ask):.2f} 的标的"
                )
                self.notifier.alert(self._last_signal_reason, "warning")
                self._emit_risk_decision(
                    {
                        "ticker": self.ticker,
                        "mode": self.mode,
                        "signal": getattr(signal.type, "value", str(signal.type)),
                        "fallback_used": fallback_used,
                        "allow_fallback_paper_entries": allow_paper,
                        "allow_fallback_live_entries": allow_live,
                        "risk_approved": False,
                        "blocked_by": "position_policy" if ranked_reason else "position_sizing",
                        "reason": ranked_reason or "insufficient_shares",
                        "original_target_shares": int(plan["original_target_shares"]),
                        "adjusted_target_shares": int(shares),
                        "position_multiplier": multiplier if fallback_used else 1.0,
                        "current_price": current_price,
                        "buying_power": buying_power,
                        "available_cash": available_cash,
                        "required_cash": shares * execution_price,
                        "portfolio_guard_enabled": self._portfolio_guard_enabled(),
                        "portfolio_allowed": None,
                        "portfolio_reason": "not_evaluated",
                        "order_state_blocked": bool(self.order_state.is_blocked),
                        "order_state_reason": self.order_state.blocked_reason,
                        "final_action": "blocked",
                        "reduce_only": self._reduce_only,
                        "ranked_allocation": ranked_allocation,
                    }
                )
                return

            self._emit_risk_decision(
                {
                    "ticker": self.ticker,
                    "mode": self.mode,
                    "signal": getattr(signal.type, "value", str(signal.type)),
                    "fallback_used": fallback_used,
                    "allow_fallback_paper_entries": allow_paper,
                    "allow_fallback_live_entries": allow_live,
                    "risk_approved": True,
                    "blocked_by": "",
                    "reason": policy_reason,
                    "original_target_shares": int(plan["original_target_shares"]),
                    "adjusted_target_shares": int(shares),
                    "position_multiplier": multiplier if fallback_used else 1.0,
                    "current_price": current_price,
                    "buying_power": buying_power,
                    "available_cash": available_cash,
                    "required_cash": shares * execution_price,
                    "portfolio_guard_enabled": self._portfolio_guard_enabled(),
                    "portfolio_allowed": None,
                    "portfolio_reason": "not_evaluated",
                    "order_state_blocked": bool(self.order_state.is_blocked),
                    "order_state_reason": self.order_state.blocked_reason,
                    "final_action": "buy_candidate",
                    "reduce_only": self._reduce_only,
                    "ranked_allocation": plan.get("ranked_allocation"),
                }
            )

            # ---- Pre-trade buying-power check ----
            bp_ok, bp_reason = self.order_state.check_buying_power(
                price=execution_price,
                quantity=shares,
                available_cash=available_cash,
            )
            if not bp_ok:
                self._last_signal_reason = bp_reason
                self.notifier.alert(bp_reason, "warning")
                # Record as rejected without even sending to broker
                self._emit_risk_decision(
                    {
                        "ticker": self.ticker,
                        "mode": self.mode,
                        "signal": getattr(signal.type, "value", str(signal.type)),
                        "fallback_used": fallback_used,
                        "allow_fallback_paper_entries": allow_paper,
                        "allow_fallback_live_entries": allow_live,
                        "risk_approved": False,
                        "blocked_by": "buying_power",
                        "reason": bp_reason,
                        "original_target_shares": int(plan["original_target_shares"]),
                        "adjusted_target_shares": int(shares),
                        "position_multiplier": multiplier if fallback_used else 1.0,
                        "current_price": current_price,
                        "buying_power": buying_power,
                        "available_cash": available_cash,
                        "required_cash": shares * execution_price,
                        "portfolio_guard_enabled": self._portfolio_guard_enabled(),
                        "portfolio_allowed": None,
                        "portfolio_reason": "not_evaluated",
                        "order_state_blocked": bool(self.order_state.is_blocked),
                        "order_state_reason": bp_reason,
                        "final_action": "blocked",
                        "reduce_only": self._reduce_only,
                    }
                )
                self.order_state.record_rejected(
                    order_id="BP_CHECK",
                    reason=bp_reason,
                    quantity=shares,
                    price=current_price,
                    buying_power=buying_power,
                )
                return

            # ---- Per-engine risk check ----
            entry_check = self.risk.check_entry(
                current_price, shares, self._position_shares
            )

            if not entry_check.allowed:
                self._last_signal_reason = entry_check.reason
                self.notifier.alert(entry_check.reason, "warning")
                self._emit_risk_decision(
                    {
                        "ticker": self.ticker,
                        "mode": self.mode,
                        "signal": getattr(signal.type, "value", str(signal.type)),
                        "fallback_used": fallback_used,
                        "allow_fallback_paper_entries": allow_paper,
                        "allow_fallback_live_entries": allow_live,
                        "risk_approved": False,
                        "blocked_by": "risk_manager",
                        "reason": entry_check.reason,
                        "original_target_shares": int(plan["original_target_shares"]),
                        "adjusted_target_shares": int(shares),
                        "position_multiplier": multiplier if fallback_used else 1.0,
                        "current_price": current_price,
                        "buying_power": buying_power,
                        "available_cash": available_cash,
                        "required_cash": shares * execution_price,
                        "portfolio_guard_enabled": self._portfolio_guard_enabled(),
                        "portfolio_allowed": None,
                        "portfolio_reason": "not_evaluated",
                        "order_state_blocked": bool(self.order_state.is_blocked),
                        "order_state_reason": self.order_state.blocked_reason,
                        "final_action": "blocked",
                        "reduce_only": self._reduce_only,
                    }
                )
                return

            # ---- Optional portfolio-level guard before broker submission ----
            if self._portfolio_guard_enabled():
                portfolio_state, portfolio_warning = self._build_portfolio_state()
                if portfolio_state is None:
                    reason = "portfolio_state_unavailable"
                    self._last_signal_reason = (
                        "组合风控启用但无法读取账户/持仓，禁止新买入"
                    )
                    self.notifier.alert(self._last_signal_reason, "warning")
                    self._emit_risk_decision(
                        {
                            "ticker": self.ticker,
                            "mode": self.mode,
                            "signal": getattr(signal.type, "value", str(signal.type)),
                            "fallback_used": fallback_used,
                            "allow_fallback_paper_entries": allow_paper,
                            "allow_fallback_live_entries": allow_live,
                            "risk_approved": False,
                            "blocked_by": "portfolio_guard",
                            "reason": reason,
                            "original_target_shares": int(plan["original_target_shares"]),
                            "adjusted_target_shares": int(shares),
                            "position_multiplier": multiplier if fallback_used else 1.0,
                            "current_price": current_price,
                            "buying_power": buying_power,
                            "available_cash": available_cash,
                            "required_cash": shares * execution_price,
                            "portfolio_guard_enabled": True,
                            "portfolio_allowed": False,
                            "portfolio_reason": reason,
                            "order_state_blocked": bool(self.order_state.is_blocked),
                            "order_state_reason": self.order_state.blocked_reason,
                            "final_action": "blocked",
                            "reduce_only": self._reduce_only,
                        }
                    )
                    self._write_runtime_audit(
                        "portfolio_risk_blocked",
                        ticker=self.ticker,
                        quantity=shares,
                        price=ask if ask > 0 else current_price,
                        reason=reason,
                        current_exposure=None,
                        projected_exposure=None,
                        current_leveraged_etf_exposure=None,
                        projected_leveraged_etf_exposure=None,
                        warning=portfolio_warning or "portfolio_state_unavailable",
                        portfolio_enabled=True,
                    )
                    return

                proposed_order = {
                    "ticker": self.ticker,
                    "side": "BUY",
                    "quantity": shares,
                    "price": ask if ask > 0 else current_price,
                    "target_capital": shares * (ask if ask > 0 else current_price),
                    "reduce_only": False,
                    "regime": getattr(signal, "regime", None) or "UNKNOWN",
                }
                portfolio_check = self.portfolio_manager.check_portfolio_risk(
                    proposed_order,
                    portfolio_state,
                )
                if not bool(portfolio_check.get("allowed", False)):
                    reason = str(portfolio_check.get("reason") or "portfolio_blocked")
                    self._last_signal_reason = f"组合风控阻止买入：{reason}"
                    self.notifier.alert(self._last_signal_reason, "warning")
                    self._emit_risk_decision(
                        {
                            "ticker": self.ticker,
                            "mode": self.mode,
                            "signal": getattr(signal.type, "value", str(signal.type)),
                            "fallback_used": fallback_used,
                            "allow_fallback_paper_entries": allow_paper,
                            "allow_fallback_live_entries": allow_live,
                            "risk_approved": False,
                            "blocked_by": "portfolio_guard",
                            "reason": reason,
                            "original_target_shares": int(plan["original_target_shares"]),
                            "adjusted_target_shares": int(shares),
                            "position_multiplier": multiplier if fallback_used else 1.0,
                            "current_price": current_price,
                            "buying_power": buying_power,
                            "available_cash": available_cash,
                            "required_cash": shares * execution_price,
                            "portfolio_guard_enabled": True,
                            "portfolio_allowed": False,
                            "portfolio_reason": reason,
                            "order_state_blocked": bool(self.order_state.is_blocked),
                            "order_state_reason": self.order_state.blocked_reason,
                            "final_action": "blocked",
                            "reduce_only": self._reduce_only,
                        }
                    )
                    self._write_runtime_audit(
                        "portfolio_risk_blocked",
                        ticker=self.ticker,
                        quantity=shares,
                        price=ask if ask > 0 else current_price,
                        reason=reason,
                        current_exposure=portfolio_check.get("current_exposure"),
                        projected_exposure=portfolio_check.get("projected_exposure"),
                        current_leveraged_etf_exposure=portfolio_check.get("current_leveraged_etf_exposure"),
                        projected_leveraged_etf_exposure=portfolio_check.get("projected_leveraged_etf_exposure"),
                        portfolio_enabled=True,
                    )
                    return

            # ---- Portfolio-level risk check (correlation, exposure) ----
            if self.portfolio_risk is not None:
                correlation_factor = self.portfolio_risk.check_correlation_limit(
                    self.ticker, self.ticker
                )
                reduced_shares = shares
                if correlation_factor < 1.0:
                    reduced_shares = int(shares * correlation_factor)
                if reduced_shares < 1:
                    sector_hint = LEVERAGED_ETF_REGISTRY.get(
                        self.ticker, {}
                    ).get("sector", "")
                    self._last_signal_reason = (
                        f"组合相关性限制：同一{sector_hint}杠杆ETF限70%，"
                        f"计算后无有效买入数量"
                    )
                    self.notifier.alert(self._last_signal_reason, "warning")
                    self._emit_risk_decision(
                        {
                            "ticker": self.ticker,
                            "mode": self.mode,
                            "signal": getattr(signal.type, "value", str(signal.type)),
                            "fallback_used": fallback_used,
                            "allow_fallback_paper_entries": allow_paper,
                            "allow_fallback_live_entries": allow_live,
                            "risk_approved": False,
                            "blocked_by": "risk_manager",
                            "reason": self._last_signal_reason,
                            "original_target_shares": int(plan["original_target_shares"]),
                            "adjusted_target_shares": 0,
                            "position_multiplier": multiplier if fallback_used else 1.0,
                            "current_price": current_price,
                            "buying_power": buying_power,
                            "available_cash": available_cash,
                            "required_cash": shares * execution_price,
                            "portfolio_guard_enabled": self._portfolio_guard_enabled(),
                            "portfolio_allowed": None,
                            "portfolio_reason": "not_evaluated",
                            "order_state_blocked": bool(self.order_state.is_blocked),
                            "order_state_reason": self.order_state.blocked_reason,
                            "final_action": "blocked",
                            "reduce_only": self._reduce_only,
                        }
                    )
                    return
                if reduced_shares != shares:
                    logger.info(
                        "PortfolioRisk: correlation limit reduced %s buy from %d → %d shares",
                        self.ticker,
                        shares,
                        reduced_shares,
                    )
                    shares = reduced_shares

                total_exposure = self.portfolio_risk.get_total_exposure()
                cost = shares * (ask if ask > 0 else current_price)
                projected_exposure = total_exposure + cost
                equity = max(1.0, float(getattr(acct, "equity", 0.0) or 0.0))
                exposure_pct = (projected_exposure / equity * 100.0)
                if exposure_pct > 500.0:
                    self._last_signal_reason = (
                        f"组合总敞口 ${projected_exposure:.2f} ({exposure_pct:.1f}%) 超过500%，"
                        f"已阻止开仓"
                    )
                    self.notifier.alert(self._last_signal_reason, "warning")
                    return

            # ---- Update portfolio position value ----
            if self.portfolio_risk is not None:
                estimated_value = shares * (ask if ask > 0 else current_price)
                self.portfolio_risk.set_position_value(self.ticker, estimated_value)

            # Place order
            order = self.broker.place_order(
                ticker=self.ticker,
                side=OrderSide.BUY,
                quantity=shares,
                order_type=OrderType.MARKET,
                current_bid=current_price,
                current_ask=ask,
            )

            if order is None:
                self._last_signal_reason = "下单失败：券商返回空结果"
                self.notifier.alert(self._last_signal_reason, "error")
                self.order_state.record_rejected(
                    order_id="NONE",
                    reason="券商返回空结果",
                    quantity=shares,
                    price=current_price,
                    buying_power=buying_power,
                )
                return

            if order.status == OrderStatus.PENDING:
                self.order_state.record_submitted(order.order_id, "BUY")
                self._remember_pending_order(
                    order=order,
                    side="BUY",
                    signal_type=signal.type.value,
                )
                self.notifier.order_submitted(
                    self.ticker, "BUY", order.quantity, order.order_id, mode=self.mode
                )

            if order.status == OrderStatus.REJECTED:
                rejected_notes = order.notes or "券商拒绝"
                self._last_signal_reason = f"买单被拒绝：{rejected_notes}"
                self.notifier.alert(self._last_signal_reason, "warning")
                self.order_state.record_rejected(
                    order_id=getattr(order, "order_id", "") or "REJECTED",
                    reason=rejected_notes,
                    quantity=shares,
                    price=current_price,
                    buying_power=buying_power,
                )

            # --- update state ---
            if order.status.value in ("FILLED", "PARTIALLY_FILLED"):
                self.order_state.record_filled(order.order_id)
                self._entry_price = order.avg_fill_price
                if self._entry_price is not None:
                    self._position_shares += order.filled_quantity
                self.strategy.record_entry(order.avg_fill_price)  # Quick stop tracking
                order_id = str(getattr(order, "order_id", "") or "")
                self.notifier.trade(
                    self.ticker,
                    "BUY",
                    order.filled_quantity,
                    order.avg_fill_price,
                    mode=self.mode,
                    fill_id=order_id or None,
                    event_id=f"{self.mode}:{self.ticker}:BUY:{order_id}" if order_id else None,
                    notification_key=f"{self.mode}:{self.ticker}:BUY:{order_id}" if order_id else None,
                )
                self._last_signal_reason = (
                    f"已买入 {order.filled_quantity} 股 @ ${order.avg_fill_price:.2f}"
                )
        finally:
            self._trade_in_progress = False

    def _portfolio_guard_enabled(self) -> bool:
        portfolio_cfg = getattr(self.config, "portfolio", None)
        return bool(getattr(portfolio_cfg, "enabled", False))

    def _build_portfolio_state(self) -> tuple[Optional[dict], Optional[str]]:
        """Build the portfolio snapshot used by PortfolioManager.

        Returns (state, warning). When state is None the caller should fail closed
        for BUY orders but continue to allow SELL / reduce_only paths.
        """
        try:
            account = None
            positions = None
            try:
                account = self.broker.get_account()
            except Exception as exc:
                logger.warning("Failed to read account for portfolio guard on %s: %s", self.ticker, exc)
                account = self._latest_account
                if account is None:
                    return None, "account_unavailable"

            try:
                positions = getattr(account, "positions", None)
                if positions is None:
                    positions = self.broker.get_positions()
            except Exception as exc:
                logger.warning("Failed to read positions for portfolio guard on %s: %s", self.ticker, exc)
                positions = getattr(account, "positions", None)
                if positions is None and self._latest_position is not None:
                    positions = [self._latest_position]
                if positions is None:
                    return None, "positions_unavailable"

            portfolio_positions: dict[str, dict[str, float]] = {}
            for pos in positions or []:
                ticker = str(getattr(pos, "ticker", "") or "").strip().upper().split(".")[0]
                if not ticker:
                    continue
                quantity = float(getattr(pos, "quantity", 0) or 0)
                market_value = float(getattr(pos, "market_value", 0.0) or 0.0)
                if market_value <= 0.0 and quantity > 0:
                    current_price = float(getattr(pos, "current_price", 0.0) or 0.0)
                    avg_price = float(getattr(pos, "avg_entry_price", 0.0) or 0.0)
                    market_value = quantity * max(current_price, avg_price)
                portfolio_positions[ticker] = {
                    "market_value": float(max(0.0, market_value)),
                    "quantity": float(max(0.0, quantity)),
                }

            cash = float(getattr(account, "cash", 0.0) or 0.0)
            equity = float(getattr(account, "equity", 0.0) or 0.0)
            return (
                {
                    "account_equity": equity,
                    "cash": cash,
                    "positions": portfolio_positions,
                },
                None,
            )
        except Exception as exc:
            logger.exception("Failed to build portfolio state for %s: %s", self.ticker, exc)
            return None, "portfolio_state_unavailable"

    def _handle_sell_signal(self, signal, current_price: float, bid: float) -> None:
        """Handle a SELL signal (take profit at resistance)."""
        if not self._acquire_sell_lock("range_sell"):
            self._last_signal_reason = "卖出已跳过：同标的卖出锁已生效"
            return
        self._trade_in_progress = True
        try:
            order = self.broker.place_order(
                ticker=self.ticker,
                side=OrderSide.SELL,
                quantity=self._position_shares,
                order_type=OrderType.MARKET,
                current_bid=bid,
                current_ask=current_price,
            )

            if order is None:
                self._last_signal_reason = "卖出下单失败：券商返回空结果"
                self.notifier.alert(self._last_signal_reason, "error")
                self._release_sell_lock("order_failed_none")
                return

            if order.status == OrderStatus.PENDING:
                self._remember_pending_order(
                    order=order,
                    side="SELL",
                    signal_type=signal.type.value,
                )
                self.notifier.order_submitted(
                    self.ticker, "SELL", order.quantity, order.order_id, mode=self.mode
                )

            if order.status.value in ("FILLED", "PARTIALLY_FILLED"):
                pnl = self._calculate_pnl(
                    order.avg_fill_price, int(order.filled_quantity or 0)
                )
                order_id = str(getattr(order, "order_id", "") or "")
                self.notifier.trade(
                    self.ticker,
                    "SELL",
                    order.filled_quantity,
                    order.avg_fill_price,
                    pnl,
                    mode=self.mode,
                    fill_id=order_id or None,
                    event_id=f"{self.mode}:{self.ticker}:SELL:{order_id}" if order_id else None,
                    notification_key=f"{self.mode}:{self.ticker}:SELL:{order_id}" if order_id else None,
                )

                # Record the trade
                self.risk.record_trade(TradeRecord(
                    entry_time=datetime.now(),
                    exit_time=datetime.now(),
                    entry_price=self._entry_price or 0,
                    exit_price=order.avg_fill_price,
                    shares=order.filled_quantity,
                    pnl=pnl or 0,
                    pnl_pct=((order.avg_fill_price - (self._entry_price or order.avg_fill_price))
                             / (self._entry_price or 1) * 100),
                    side="LONG",
                ))

                self._position_shares = max(
                    0, self._position_shares - int(order.filled_quantity or 0)
                )
                self._set_position_sync_fence(self._position_shares)
                if self._position_shares <= 0:
                    self._entry_price = None
                    self.strategy.clear_entry()
            if order.status == OrderStatus.REJECTED:
                self._release_sell_lock("order_rejected")
        finally:
            self._trade_in_progress = False

    def _handle_stop_loss(self, signal, current_price: float, bid: float) -> None:
        """Handle stop loss: exit position immediately."""
        if not self._acquire_sell_lock("strategy_stop_loss"):
            self._last_signal_reason = "止损卖出已跳过：同标的卖出锁已生效"
            return
        self._trade_in_progress = True
        try:
            self.notifier.alert(
                f"STOP LOSS triggered! Exiting at ${current_price:.2f}",
                "halt",
            )

            order = self.broker.place_order(
                ticker=self.ticker,
                side=OrderSide.SELL,
                quantity=self._position_shares,
                order_type=OrderType.MARKET,
                current_bid=bid,
                current_ask=current_price,
            )

            if order is None:
                self._last_signal_reason = "止损卖出下单失败：券商返回空结果"
                self.notifier.alert(self._last_signal_reason, "error")
                self._release_sell_lock("order_failed_none")
                return

            if order.status == OrderStatus.PENDING:
                self._remember_pending_order(
                    order=order,
                    side="SELL",
                    signal_type=signal.type.value,
                )
                self.notifier.order_submitted(
                    self.ticker, "SELL", order.quantity, order.order_id, mode=self.mode
                )

            if order.status.value in ("FILLED", "PARTIALLY_FILLED"):
                pnl = self._calculate_pnl(
                    order.avg_fill_price, int(order.filled_quantity or 0)
                )
                self.risk.record_trade(TradeRecord(
                    entry_time=datetime.now(),
                    exit_time=datetime.now(),
                    entry_price=self._entry_price or 0,
                    exit_price=order.avg_fill_price,
                    shares=order.filled_quantity,
                    pnl=pnl or 0,
                    pnl_pct=((order.avg_fill_price - (self._entry_price or order.avg_fill_price))
                             / (self._entry_price or 1) * 100),
                    side="LONG",
                ))
                self._position_shares = max(
                    0, self._position_shares - int(order.filled_quantity or 0)
                )
                self._set_position_sync_fence(self._position_shares)
                if self._position_shares <= 0:
                    self._entry_price = None
                    self.strategy.clear_entry()
            if order.status == OrderStatus.REJECTED:
                self._release_sell_lock("order_rejected")
        finally:
            self._trade_in_progress = False

    # ---- Helpers ----

    def _should_run_exit_check(self, now_ts: float) -> bool:
        return (now_ts - self._last_exit_check_at) >= 60.0

    def _initialize_ai_selector(self) -> None:
        runtime = load_ai_selector_runtime_config()
        signature = self._ai_selection_signature_for_runtime(runtime)
        if signature == self._ai_selection_signature:
            return
        self._ai_selection_signature = signature
        self._ai_selection = AISelectionDecision(
            enabled=runtime.enabled,
            selection_mode="DISABLED" if not runtime.enabled else "UNAVAILABLE",
        )
        logger.info("AI selector enabled %s", str(runtime.enabled).lower())
        self._write_runtime_audit(
            "ai_selector_status",
            ai_selector_enabled=runtime.enabled,
            universe=runtime.universe,
            selection_mode=self._ai_selection.selection_mode,
        )
        if not runtime.enabled:
            return

        selection_context = self._load_ai_selection_context(runtime)
        selection_mode = str(selection_context.get("selection_mode") or "").strip().upper() or "UNAVAILABLE"
        selection_reason = str(selection_context.get("selection_reason") or "").strip() or "unknown"
        cached_selection = selection_context.get("cached_selection")
        top3: list[dict] = []
        top10: list[dict] = []
        ai_meta: dict[str, object] = {}
        if cached_selection is not None:
            top3, top10, ai_meta = cached_selection
        if selection_mode != "ACTIVE":
            logger.warning(
                "AI selection %s, new paper entries disabled%s",
                selection_mode.lower(),
                f" ({selection_reason})" if selection_reason else "",
            )
            self._ai_selection.selection_mode = selection_mode
            self._ai_selection.fallback_reason = selection_reason
            self._ai_selection.active = False
            self._write_runtime_audit(
                "ai_selector_inactive",
                ai_selector_enabled=runtime.enabled,
                selection_mode=selection_mode,
                selection_reason=selection_reason,
                fallback_reason=selection_reason,
                universe=runtime.universe,
            )
            return
        if not top3:
            logger.warning("AI selector returned no signals, new paper entries disabled")
            self._ai_selection.selection_mode = "BLOCKED"
            self._ai_selection.fallback_reason = "empty_ai_signals"
            self._ai_selection.active = False
            self._write_runtime_audit(
                "ai_selector_inactive",
                ai_selector_enabled=runtime.enabled,
                selection_mode="BLOCKED",
                selection_reason="empty_ai_signals",
                fallback_reason="empty_ai_signals",
                universe=runtime.universe,
            )
            return

        signal_for_ticker = next(
            (item for item in top3 if str(item.get("ticker") or "").upper() == self.ticker.upper()),
            None,
        )
        regime = self._detect_market_regime(top3, signal_for_ticker)
        strategy = self._route_strategy(regime, signal_for_ticker)
        allocation_weight = self._allocate_portfolio_weight(top3, signal_for_ticker)
        result_quality = str(ai_meta.get("result_quality") or "").strip().upper()
        research_admission = str(ai_meta.get("research_admission") or "").strip().upper()
        risk_approved = self._preapprove_ai_risk(
            regime,
            allocation_weight,
            signal_for_ticker,
            fallback_used=bool(ai_meta.get("fallback_used", False)),
        )
        self._ai_selection = AISelectionDecision(
            enabled=True,
            active=True,
            selection_mode="ACTIVE",
            top10=top10,
            top3=top3,
            signal_for_ticker=signal_for_ticker,
            regime=regime,
            strategy=strategy,
            risk_approved=risk_approved,
            allocation_weight=allocation_weight,
            fallback_used=bool(ai_meta.get("fallback_used", False)),
            result_quality=result_quality,
            research_admission=research_admission,
        )
        logger.info("AI selector top10 candidates: %s", [item.get("ticker") for item in top10])
        logger.info("AI selector selected top3: %s", [item.get("ticker") for item in top3])
        logger.info(
            "AI selector ticker=%s final_score=%s reason=%s regime=%s strategy=%s risk_approved=%s fallback_used=%s",
            self.ticker,
            signal_for_ticker.get("score") if signal_for_ticker else None,
            signal_for_ticker.get("reason") if signal_for_ticker else "not_selected",
            regime,
            strategy,
            risk_approved,
            bool(ai_meta.get("fallback_used", False)),
        )
        self._write_runtime_audit(
            "ai_selector_decision",
            ai_selector_enabled=True,
            top10_candidates=top10,
            selected_top3=top3,
            final_score=signal_for_ticker.get("score") if signal_for_ticker else None,
            ai_reason=signal_for_ticker.get("reason") if signal_for_ticker else "not_selected",
            regime=regime,
            strategy=strategy,
            risk_approved=risk_approved,
            fallback_used=bool(ai_meta.get("fallback_used", False)),
            result_quality=result_quality,
            research_admission=research_admission,
        )

    def _ai_selection_signature_for_runtime(self, runtime) -> tuple[str, ...]:
        state = load_selection_state()
        bundle = load_committed_selection_bundle(PROJECT_DIR)
        state_path = selection_state_path()
        state_mtime = str(state_path.stat().st_mtime_ns) if state_path.exists() else "0"
        manifest_path = selection_state_path().parent / "selection_bundle_manifest.json"
        manifest_mtime = str(manifest_path.stat().st_mtime_ns) if manifest_path.exists() else "0"
        report_path = None
        if isinstance(bundle, dict):
            bundle_root = bundle.get("bundle_root")
            if isinstance(bundle_root, Path):
                report_path = bundle_root / "ai_selection_report.json"
        if report_path is None:
            report_path = self._resolve_selection_report_path(state if isinstance(state, dict) else None)
        report_mtime = str(report_path.stat().st_mtime_ns) if report_path.exists() else "0"
        return (
            "enabled" if bool(getattr(runtime, "enabled", False)) else "disabled",
            state_mtime,
            manifest_mtime,
            report_mtime,
            str(state.get("et_date") or "") if isinstance(state, dict) else "",
            str(state.get("selection_run_id") or "") if isinstance(state, dict) else "",
            str(state.get("top_sync_run_id") or "") if isinstance(state, dict) else "",
            str(state.get("selection_stage") or "") if isinstance(state, dict) else "",
            str(state.get("result_quality") or "") if isinstance(state, dict) else "",
            str(state.get("research_admission") or "") if isinstance(state, dict) else "",
        )

    def _load_ai_selection_context(self, runtime) -> dict[str, object]:
        bundle = load_committed_selection_bundle(PROJECT_DIR)
        state = load_selection_state()
        manifest_path = selection_state_path().parent / "selection_bundle_manifest.json"
        if manifest_path.exists() and not isinstance(bundle, dict):
            return {
                "selection_mode": "INVALID",
                "selection_reason": "committed_selection_bundle_unavailable",
                "cached_selection": None,
            }
        if not isinstance(state, dict) and not isinstance(bundle, dict):
            return {
                "selection_mode": "UNAVAILABLE",
                "selection_reason": "selection_state_missing",
                "cached_selection": None,
            }
        if not HAS_PYTZ or self._ny_tz is None:
            return {
                "selection_mode": "UNAVAILABLE",
                "selection_reason": "timezone_unavailable",
                "cached_selection": None,
            }

        required_day = self._required_selection_date()
        if isinstance(bundle, dict) and isinstance(bundle.get("state"), dict):
            state = dict(bundle.get("state") or {})
        state_day = str(state.get("et_date") or "").strip()
        if state_day != required_day:
            return {
                "selection_mode": "STALE",
                "selection_reason": f"selection_state_date_mismatch:{state_day or 'missing'}",
                "cached_selection": None,
            }

        ok, reason, verified_state = verify_selection_state(required_et_date=required_day, state=state)
        if not ok:
            normalized_reason = str(reason or "selection_state_invalid")
            if normalized_reason == "selection_state_date_mismatch":
                selection_mode = "STALE"
            elif normalized_reason == "missing_top_slot":
                selection_mode = "UNAVAILABLE"
            else:
                selection_mode = "BLOCKED"
            return {
                "selection_mode": selection_mode,
                "selection_reason": normalized_reason,
                "cached_selection": None,
                "verified_state": verified_state,
            }

        report_path = None
        if isinstance(bundle, dict):
            bundle_root = bundle.get("bundle_root")
            if isinstance(bundle_root, Path):
                report_path = bundle_root / "ai_selection_report.json"
        if report_path is None:
            report_path = self._resolve_selection_report_path(state if isinstance(state, dict) else None)
        if not report_path.exists():
            return {
                "selection_mode": "UNAVAILABLE",
                "selection_reason": "selection_report_missing",
                "cached_selection": None,
            }
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            return {
                "selection_mode": "INVALID",
                "selection_reason": "selection_report_invalid",
                "cached_selection": None,
            }
        if not isinstance(payload, dict):
            return {
                "selection_mode": "INVALID",
                "selection_reason": "selection_report_invalid",
                "cached_selection": None,
            }

        selection_date = str(payload.get("selection_date") or "").strip()
        if selection_date and selection_date != required_day:
            return {
                "selection_mode": "STALE",
                "selection_reason": f"selection_report_date_mismatch:{selection_date}",
                "cached_selection": None,
            }

        selection_stage = str(payload.get("selection_stage") or "").strip().upper()
        result_quality = str(payload.get("result_quality") or "").strip().upper()
        research_admission = str(payload.get("research_admission") or "").strip().upper()
        top3 = payload.get("top3") if isinstance(payload.get("top3"), list) else []
        top10 = payload.get("top10") if isinstance(payload.get("top10"), list) else []
        if not top10:
            top10 = payload.get("top5") if isinstance(payload.get("top5"), list) else []
        top3 = [dict(item) for item in top3 if isinstance(item, dict)]
        top10 = [dict(item) for item in top10 if isinstance(item, dict)]
        if selection_stage in {"PRELIMINARY", "STALE"}:
            return {
                "selection_mode": "STALE",
                "selection_reason": f"selection_stage_{selection_stage.lower()}",
                "cached_selection": None,
            }
        if selection_stage == "INVALID" or result_quality == "INVALID":
            return {
                "selection_mode": "INVALID",
                "selection_reason": "result_quality_invalid",
                "cached_selection": None,
            }
        if research_admission == "BLOCKED":
            return {
                "selection_mode": "BLOCKED",
                "selection_reason": "research_admission_blocked",
                "cached_selection": None,
            }
        if not top3:
            return {
                "selection_mode": "BLOCKED",
                "selection_reason": "empty_ai_signals",
                "cached_selection": None,
            }

        cached_selection = self._load_cached_ai_selection(runtime, bundle=bundle)
        if cached_selection is None:
            return {
                "selection_mode": "BLOCKED",
                "selection_reason": "cached_selection_missing",
                "cached_selection": None,
            }
        top3, top10, payload = cached_selection
        return {
            "selection_mode": "ACTIVE",
            "selection_reason": "ok",
            "cached_selection": (top3, top10, payload),
            "verified_state": verified_state,
        }

    def _load_cached_ai_selection(
        self,
        runtime,
        bundle: dict[str, object] | None = None,
    ) -> tuple[list[dict], list[dict], dict] | None:
        state = bundle.get("state") if isinstance(bundle, dict) else load_selection_state()
        if not isinstance(state, dict):
            return None
        if not HAS_PYTZ or self._ny_tz is None:
            return None
        required_day = self._required_selection_date()
        if str(state.get("et_date") or "").strip() != required_day:
            return None
        report_path: Path | None = None
        payload = bundle.get("report") if isinstance(bundle, dict) else None
        if isinstance(bundle, dict):
            bundle_root = bundle.get("bundle_root")
            if isinstance(bundle_root, Path):
                report_path = bundle_root / "ai_selection_report.json"
        if not isinstance(payload, dict):
            report_path = report_path or self._resolve_selection_report_path(state)
            if not report_path.exists():
                return None
            try:
                payload = json.loads(report_path.read_text(encoding="utf-8"))
            except Exception:
                return None
        if not isinstance(payload, dict):
            return None
        if isinstance(bundle, dict):
            manifest = bundle.get("manifest")
            run_ids = {
                str(source.get("selection_run_id") or "").strip()
                for source in (manifest, state, payload)
                if isinstance(source, dict) and str(source.get("selection_run_id") or "").strip()
            }
            if len(run_ids) > 1:
                return None
        selection_date = str(payload.get("selection_date") or "").strip()
        if selection_date and selection_date != required_day:
            return None
        top3 = payload.get("top3") if isinstance(payload.get("top3"), list) else []
        top10 = payload.get("top10") if isinstance(payload.get("top10"), list) else []
        if not top10:
            top10 = payload.get("top5") if isinstance(payload.get("top5"), list) else []
        top3 = [dict(item) for item in top3 if isinstance(item, dict)]
        top10 = [dict(item) for item in top10 if isinstance(item, dict)]
        if not top3:
            return None
        logger.info(
            "AI selector reused cached daily selection for %s from %s",
            required_day,
            report_path or "committed_bundle",
        )
        self._write_runtime_audit(
            "ai_selector_cache_hit",
            ai_selector_enabled=runtime.enabled,
            cache_report_path=str(report_path or "committed_bundle"),
            cache_et_date=required_day,
            cached_top3=[item.get("ticker") for item in top3],
            fallback_used=bool(payload.get("fallback_used", False)),
        )
        return top3, top10, payload

    def _resolve_selection_report_path(self, state: dict[str, object] | None = None) -> Path:
        report_path_raw = str(state.get("report_path") or "").strip() if isinstance(state, dict) else ""
        candidates: list[Path] = []
        if report_path_raw:
            path = Path(report_path_raw)
            if path.is_absolute():
                candidates.append(path)
            else:
                candidates.extend(
                    [
                        selection_state_module.PROJECT_DIR / report_path_raw,
                        PROJECT_DIR / report_path_raw,
                        selection_state_path().parent.parent / report_path_raw,
                        resolve_reports_dir(PROJECT_DIR) / Path(report_path_raw).name,
                    ]
                )
        candidates.append(resolve_reports_dir(PROJECT_DIR) / "ai_selection_latest.json")
        for candidate in candidates:
            try:
                if candidate.exists():
                    return candidate
            except Exception:
                continue
        return candidates[0] if candidates else resolve_reports_dir(PROJECT_DIR) / "ai_selection_latest.json"

    def _required_selection_date(self) -> str:
        if not HAS_PYTZ or self._ny_tz is None:
            return ""
        return required_selection_date(datetime.now(self._ny_tz))

    def _detect_market_regime(self, top3: list[dict], signal_for_ticker: Optional[dict]) -> str:
        if signal_for_ticker is None:
            return "NO_SELECTION"

        confidences: list[float] = []
        missing_confidence = 0
        for item in top3:
            raw_confidence = item.get("confidence") if isinstance(item, dict) else None
            try:
                confidence = float(raw_confidence) if raw_confidence is not None else None
            except (TypeError, ValueError):
                confidence = None
            if confidence is None:
                missing_confidence += 1
                continue
            confidences.append(confidence)

        if confidences:
            avg_confidence = sum(confidences) / len(confidences)
        else:
            avg_confidence = 0.5

        if missing_confidence and len(confidences) < len(top3):
            # 缺失 confidence 代表该条结果没有提供置信度，不应按 0 处理。
            # 使用中性阈值，避免把整组 selection 误判成 EVENT。
            avg_confidence = max(avg_confidence, 0.5)

        if avg_confidence < 0.30:
            return "EVENT"
        return "NORMAL"

    def _route_strategy(self, regime: str, signal_for_ticker: Optional[dict]) -> str:
        if regime == "EVENT":
            return "blocked"
        if signal_for_ticker is None:
            return "watch_only"
        return "range_detector"

    def _allocate_portfolio_weight(self, top3: list[dict], signal_for_ticker: Optional[dict]) -> float:
        if signal_for_ticker is None or not top3:
            return 0.0
        return min(0.30, 1.0 / len(top3))

    def _preapprove_ai_risk(
        self,
        regime: str,
        allocation_weight: float,
        signal_for_ticker: Optional[dict],
        fallback_used: bool = False,
    ) -> bool:
        if signal_for_ticker is None:
            return False
        if regime == "EVENT":
            return False
        if fallback_used:
            allow_paper, _allow_live, _multiplier = self._ai_fallback_policy()
            if self.mode == "live":
                return False
            if self.mode == "paper":
                return bool(allow_paper)
            return False
        return 0.0 < allocation_weight <= 0.30

    def _ai_entry_allowed(self) -> bool:
        if not self._ai_selection.enabled:
            return True
        if not self._ai_selection.active:
            return False
        if str(getattr(self._ai_selection, "selection_mode", "") or "").upper() != "ACTIVE":
            return False
        if self._ai_selection.regime == "EVENT":
            return False
        return bool(self._ai_selection.signal_for_ticker and self._ai_selection.risk_approved)

    def _blocked_ai_reason(self) -> str:
        if not self._ai_selection.enabled:
            return "AI 选股未启用"
        if not self._ai_selection.active:
            mode = str(getattr(self._ai_selection, "selection_mode", "") or "UNAVAILABLE").upper()
            return f"AI 选股状态不可用（{mode}:{self._ai_selection.fallback_reason or 'unknown'}），纸面盘禁止新开仓"
        if self._ai_selection.fallback_used:
            allow_paper, _allow_live, _multiplier = self._ai_fallback_policy()
            if self.mode == "live":
                return "fallback_used_live_blocked"
            if self.mode == "paper" and not allow_paper:
                return "AI 选股已降级到回退数据，fallback_used_blocked"
            return "fallback_used_paper_allowed_with_reduced_size"
        if self._ai_selection.regime == "EVENT":
            return "AI 市场状态为 EVENT，禁止开新仓"
        if not self._ai_selection.signal_for_ticker:
            return "当前标的不在 AI Top3，跳过新开仓"
        if not self._ai_selection.risk_approved:
            return "AI 风控预检未通过，跳过新开仓"
        return "AI 选股未批准新开仓"

    def _cap_ai_available_cash(self, available_cash: float, acct) -> float:
        if not self._ai_selection.enabled or not self._ai_selection.active:
            return available_cash
        if not self._ai_selection.signal_for_ticker:
            return 0.0
        equity = float(getattr(acct, "equity", 0.0) or 0.0)
        if equity <= 0 or self._ai_selection.allocation_weight <= 0:
            return available_cash
        allocation_cap = equity * min(0.30, self._ai_selection.allocation_weight)
        return max(0.0, min(available_cash, allocation_cap))

    def _verify_live_startup_safety(self) -> bool:
        """Fail closed unless live startup has verified selection and broker data."""
        top_symbols = {
            str(symbol or "").strip().upper() for symbol in current_top_config_symbols()
        }
        if (
            self._startup_role != "orphan_monitor"
            and has_live_top_configs()
            and self.ticker.upper() in top_symbols
        ):
            required_date = required_selection_date(datetime.now(pytz.timezone("America/New_York")))
            ok, reason, _state = verify_live_startup_selection(required_et_date=required_date)
            if not ok:
                self._last_signal_reason = f"实盘启动已阻止：当天选股状态无效（{reason}）"
                self._write_runtime_audit(
                    "startup_safety_check",
                    broker_position_verified=False,
                    broker_account_verified=False,
                    startup_allowed=False,
                    reason="selection_state_invalid",
                    selection_state_reason=reason,
                    required_et_date=required_date,
                    startup_role=self._startup_role,
                )
                self.notifier.alert(self._last_signal_reason, "error")
                return False

        invalidate = getattr(self.broker, "invalidate_cache", None)
        if callable(invalidate):
            invalidate()
        positions = self.broker.get_positions()
        positions_reliable = getattr(
            self.broker, "is_positions_snapshot_reliable", lambda: True
        )()
        if not positions_reliable:
            self._last_signal_reason = "实盘启动已阻止：券商持仓无法确认"
            self._write_runtime_audit(
                "startup_safety_check",
                broker_position_verified=False,
                broker_account_verified=False,
                startup_allowed=False,
                reason="broker_position_verification_failed",
                startup_role=self._startup_role,
            )
            self.notifier.alert(self._last_signal_reason, "error")
            return False

        account = self.broker.get_account()
        account_reliable = getattr(
            self.broker, "is_account_snapshot_reliable", lambda: True
        )()
        if not account_reliable:
            self._last_signal_reason = "实盘启动已阻止：券商账户快照无法确认"
            self._write_runtime_audit(
                "startup_safety_check",
                broker_position_verified=True,
                broker_account_verified=False,
                startup_allowed=False,
                reason="broker_account_verification_failed",
                startup_role=self._startup_role,
            )
            self.notifier.alert(self._last_signal_reason, "error")
            return False

        self._write_runtime_audit(
            "startup_safety_check",
            broker_position_verified=True,
            broker_account_verified=True,
            startup_allowed=True,
            positions=[
                {
                    "symbol": str(getattr(pos, "ticker", "") or "").split(".")[0].upper(),
                    "quantity": int(getattr(pos, "quantity", 0) or 0),
                }
                for pos in positions or []
                if int(getattr(pos, "quantity", 0) or 0) > 0
            ],
            equity=float(getattr(account, "equity", 0.0) or 0.0),
            startup_role=self._startup_role,
        )
        return True

    def _has_active_sell_protection(self) -> bool:
        if self.mode == "live" and self._sell_lock_path.exists():
            return True
        if self._position_sync_fence:
            return True
        if self._pending_order and str(self._pending_order.get("side") or "").upper() == "SELL":
            return True
        return False

    def _write_runtime_audit(self, phase: str, **fields) -> None:
        append_runtime_audit(
            {
                "phase": phase,
                "ticker": self.ticker,
                "symbol": self.ticker,
                "execution_mode": self.mode,
                "reduce_only": self._reduce_only,
                **fields,
            }
        )

    def _run_dynamic_exit_check(self, current_price: float, bid: float) -> None:
        pos = self.broker.get_position_for_ticker(self.ticker)
        positions_reliable = getattr(
            self.broker, "is_positions_snapshot_reliable", lambda: True
        )()
        if self.mode == "live" and not positions_reliable:
            self._last_signal_reason = "动态退出跳过：券商持仓未确认"
            return
        if pos is None:
            return
        confirmed_qty = self._apply_position_sync_fence(pos.quantity)
        if confirmed_qty <= 0:
            return
        avg_cost = float(getattr(pos, "avg_entry_price", 0.0) or 0.0)
        decision = check_exit_conditions(
            self.ticker,
            current_price,
            avg_cost,
            confirmed_qty,
            is_inverse_etf=is_inverse_etf_symbol(self.ticker),
            mode="normal",
        )
        if not decision["should_exit"]:
            return
        if self._has_active_sell_protection():
            self._last_signal_reason = f"动态退出待执行：{decision['reason']}，但卖出保护已生效"
            self._write_runtime_audit(
                "risk_exit_trigger",
                reason=decision["reason"],
                current_price=current_price,
                avg_cost=avg_cost,
                quantity=confirmed_qty,
                mode="normal",
                trigger_price=decision["trigger_price"],
                broker_position_verified=positions_reliable,
                skipped=True,
                skip_reason="sell_protection_active",
            )
            return
        self._submit_reduce_order(
            quantity=confirmed_qty,
            current_price=current_price,
            execution_price=bid if bid > 0 else current_price,
            reason=str(decision["reason"]),
            mode="normal",
            avg_cost=avg_cost,
            broker_position_verified=positions_reliable,
            trigger_price=decision["trigger_price"],
        )

    def _submit_reduce_order(
        self,
        *,
        quantity: int,
        current_price: float,
        execution_price: float,
        reason: str,
        mode: str,
        avg_cost: Optional[float] = None,
        broker_position_verified: bool = True,
        trigger_price: Optional[float] = None,
        audit_phase: str = "risk_exit_trigger",
    ) -> Optional[str]:
        if quantity <= 0:
            return None
        if not self._acquire_sell_lock(f"{mode}:{reason}"):
            self._last_signal_reason = "动态退出已跳过：同标的卖出锁已生效"
            self._write_runtime_audit(
                audit_phase,
                reason=reason,
                quantity=quantity,
                mode=mode,
                skipped=True,
                skip_reason="cross_process_sell_lock_active",
            )
            return None
        self._trade_in_progress = True
        order_id = None
        try:
            order = self.broker.place_order(
                ticker=self.ticker,
                side=OrderSide.SELL,
                quantity=quantity,
                order_type=OrderType.MARKET,
                current_bid=execution_price,
                current_ask=current_price,
                notes=f"{mode}:{reason}",
            )
            order_id = str(getattr(order, "order_id", "") or "")
            self._write_runtime_audit(
                audit_phase,
                reason=reason,
                current_price=current_price,
                avg_cost=avg_cost if avg_cost is not None else self._entry_price,
                quantity=quantity,
                mode=mode,
                trigger_price=trigger_price if trigger_price is not None else current_price,
                broker_position_verified=broker_position_verified,
                order_id=order_id or None,
                order={
                    "side": "sell",
                    "qty": quantity,
                    "type": "market",
                },
                response={"status": str(order.status.value).lower()},
            )

            if order.status == OrderStatus.PENDING:
                self._remember_pending_order(
                    order=order,
                    side="SELL",
                    signal_type=reason.upper(),
                )
                self.notifier.order_submitted(
                    self.ticker, "SELL", order.quantity, order.order_id, mode=self.mode
                )

            if order.status.value in ("FILLED", "PARTIALLY_FILLED"):
                filled_qty = int(order.filled_quantity or 0)
                pnl = self._calculate_pnl(order.avg_fill_price, filled_qty)
                self.notifier.trade(
                    self.ticker,
                    "SELL",
                    filled_qty,
                    order.avg_fill_price,
                    pnl,
                    mode=self.mode,
                    fill_id=order_id or None,
                    event_id=f"{self.mode}:{self.ticker}:SELL:{order_id}" if order_id else None,
                    notification_key=f"{self.mode}:{self.ticker}:SELL:{order_id}" if order_id else None,
                )
                if self._entry_price and order.avg_fill_price:
                    self.risk.record_trade(TradeRecord(
                        entry_time=datetime.now(),
                        exit_time=datetime.now(),
                        entry_price=self._entry_price,
                        exit_price=order.avg_fill_price,
                        shares=filled_qty,
                        pnl=pnl or 0.0,
                        pnl_pct=((order.avg_fill_price - self._entry_price) / self._entry_price * 100),
                        side="LONG",
                    ))
                self._position_shares = max(0, self._position_shares - filled_qty)
                self._set_position_sync_fence(self._position_shares)
                if self._position_shares <= 0:
                    self._entry_price = None
                    self.strategy.clear_entry()
                self._last_signal_reason = f"动态退出已执行：{reason}"
            if order.status == OrderStatus.REJECTED:
                self._release_sell_lock("order_rejected")
            return order_id or None
        finally:
            self._trade_in_progress = False

    def _calculate_pnl(
        self, exit_price: float, shares: Optional[int] = None
    ) -> Optional[float]:
        """Calculate realized P&L for current position."""
        if self._entry_price is None:
            return None
        quantity = self._position_shares if shares is None else shares
        return (exit_price - self._entry_price) * quantity

    def _remember_pending_order(self, order, side: str, signal_type: str) -> None:
        """Store a submitted live order so later fills can be reconciled."""
        self._pending_order = {
            "order_id": order.order_id,
            "side": side,
            "signal_type": signal_type,
            "requested_quantity": int(order.quantity or 0),
            "acknowledged_filled_quantity": int(order.filled_quantity or 0),
            "entry_price_before": self._entry_price,
            "created_at": datetime.now(),
        }
        self._persist_pending_order()
        self._last_signal_reason = f"订单已提交，等待成交：{side} {order.quantity} 股"

    def _adopt_active_live_order(self) -> bool:
        """Block startup unless broker-side open orders are safely accounted for."""
        get_active_orders = getattr(self.broker, "get_active_orders", None)
        if not callable(get_active_orders):
            return True
        retry_count = 3
        retry_delay = 6
        for idx in range(retry_count):
            active_orders = get_active_orders(self.ticker)
            if active_orders is not None:
                break
            if idx < (retry_count - 1):
                self._last_signal_reason = (
                    f"启动自检限流，{self.ticker} 活动订单核对重试 {idx + 1}/{retry_count - 1}"
                )
                time.sleep(retry_delay * (idx + 1))
        else:
            active_orders = None
        if active_orders is None:
            self.notifier.alert(
                f"{self.ticker} 无法核对活动订单，拒绝启动实盘交易",
                "error",
            )
            return False
        if self._pending_order:
            known_id = str(self._pending_order.get("order_id") or "")
            unknown = [order for order in active_orders if str(order.order_id) != known_id]
            if unknown:
                self.notifier.alert(
                    f"{self.ticker} 存在 {len(unknown)} 笔未接管活动订单，拒绝启动",
                    "error",
                )
                return False
            return True
        if len(active_orders) > 1:
            self.notifier.alert(
                f"{self.ticker} 存在 {len(active_orders)} 笔活动订单，拒绝启动",
                "error",
            )
            return False
        if len(active_orders) == 1:
            order = active_orders[0]
            if order.side == OrderSide.SELL:
                if not self._acquire_sell_lock("recovered_active_sell"):
                    logger.critical("Cannot acquire sell lock for recovered sell order on %s — aborting startup", self.ticker)
                    return False
            else:
                self._release_sell_lock("active_order_is_buy")
            self._remember_pending_order(
                order,
                "BUY" if order.side == OrderSide.BUY else "SELL",
                "RECOVERED",
            )
            self._last_signal_reason = (
                f"已接管券商活动订单：{self._pending_order['side']} "
                f"{str(order.order_id)[:12]}"
            )
        elif not self._position_sync_fence:
            self._release_sell_lock("startup_no_active_sell")
        return True

    def _acquire_sell_lock(self, reason: str) -> bool:
        """Atomically reserve this symbol's live SELL path across processes."""
        if self.mode != "live":
            return True
        path = self._sell_lock_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "symbol": self.ticker.upper(),
            "pid": os.getpid(),
            "reason": reason,
            "created_at": datetime.now().isoformat(),
        }
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # Check if stale: read the lock, verify PID alive, check mtime
            try:
                stale_data = json.loads(path.read_text(encoding="utf-8"))
                stale_pid = int(stale_data.get("pid", 0) or 0)
                stale_mtime = path.stat().st_mtime
                now = time.time()
                pid_alive = False
                if stale_pid > 0:
                    try:
                        os.kill(stale_pid, 0)
                        pid_alive = True
                    except OSError:
                        pid_alive = False
                # Stale if PID dead OR lock older than 6 hours AND PID doesn't match us
                if (not pid_alive) or (now - stale_mtime > 21600):
                    logger.warning(
                        "Removing stale sell lock for %s (pid=%s, age=%.0fs, alive=%s)",
                        self.ticker, stale_pid, now - stale_mtime, pid_alive,
                    )
                    path.unlink(missing_ok=True)
                    try:
                        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    except FileExistsError:
                        pass
                    else:
                        try:
                            os.write(descriptor, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                        finally:
                            os.close(descriptor)
                        self._write_runtime_audit("sell_lock_acquired_after_stale_cleanup", lock_path=str(path), reason=reason)
                        return True
            except (json.JSONDecodeError, OSError, ValueError):
                pass
            self._write_runtime_audit(
                "sell_lock_blocked",
                lock_path=str(path),
                reason=reason,
            )
            return False
        try:
            os.write(descriptor, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        finally:
            os.close(descriptor)
        self._write_runtime_audit(
            "sell_lock_acquired",
            lock_path=str(path),
            reason=reason,
        )
        return True

    def _release_sell_lock(self, reason: str) -> None:
        if self.mode != "live":
            return
        try:
            existed = self._sell_lock_path.exists()
            self._sell_lock_path.unlink(missing_ok=True)
            if existed:
                self._write_runtime_audit(
                    "sell_lock_released",
                    lock_path=str(self._sell_lock_path),
                    reason=reason,
                )
                # ── B5: Write exit fence on risk/orphan exits ──
                if "orphan" in reason or "risk" in reason:
                    self._set_exit_fence(reason)
        except OSError as exc:
            logger.error("Could not release sell lock for %s: %s", self.ticker, exc)

    # ── B5: exit fence ─────────────────────────────────────────────

    def _exit_fence_active(self) -> bool:
        """Return True if a risk/orphan exit fence is still in effect."""
        try:
            if not self._exit_fence_path.exists():
                return False
            data = json.loads(self._exit_fence_path.read_text(encoding="utf-8"))
            expires_at = float(data.get("expires_at", 0) or 0)
            if time.time() > expires_at:
                self._exit_fence_path.unlink(missing_ok=True)
                return False
            return True
        except Exception:
            return False

    def _set_exit_fence(self, reason: str, duration_seconds: int = 600) -> None:
        """Prevent TOP engines from re-buying after a risk/orphan exit."""
        self._exit_fence_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "symbol": self.ticker.upper(),
            "source": reason,
            "created_at": time.time(),
            "expires_at": time.time() + duration_seconds,
            "reason": reason,
        }
        try:
            self._exit_fence_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            self._write_runtime_audit(
                "exit_fence_set",
                symbol=self.ticker.upper(),
                reason=reason,
                duration_seconds=duration_seconds,
            )
        except OSError:
            pass

    def _load_pending_order(self) -> None:
        """Restore an unresolved live order so restarts cannot submit duplicates."""
        path = self._pending_order_state_path
        if not path.exists():
            return
        try:
            pending = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(pending, dict) or not pending.get("order_id"):
                return
            created_at = pending.get("created_at")
            if isinstance(created_at, str):
                pending["created_at"] = datetime.fromisoformat(created_at)
            self._pending_order = pending
            self._last_signal_reason = (
                f"恢复待成交订单：{pending.get('side', 'UNKNOWN')} "
                f"{str(pending['order_id'])[:12]}"
            )
        except Exception as exc:
            logger.error("Pending-order state is invalid for %s: %s", self.ticker, exc)
            # Fail closed: a corrupt order-state file must not permit new orders.
            self._pending_order = {
                "order_id": "STATE_ERROR",
                "side": "UNKNOWN",
                "requested_quantity": 0,
                "acknowledged_filled_quantity": 0,
                "created_at": datetime.now(),
            }

    def _persist_pending_order(self) -> None:
        path = self._pending_order_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(self._pending_order or {})
        created_at = payload.get("created_at")
        if isinstance(created_at, datetime):
            payload["created_at"] = created_at.isoformat()
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(path)

    def _clear_pending_order(self) -> None:
        self._pending_order = None
        try:
            self._pending_order_state_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.error("Could not clear pending-order state for %s: %s", self.ticker, exc)

    def _load_position_sync_fence(self) -> None:
        """Restore the post-sell fence that protects against stale broker positions."""
        path = self._position_sync_state_path
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            expected = int(payload["expected_max_quantity"])
            if expected < 0:
                raise ValueError("expected quantity cannot be negative")
            self._position_sync_fence = payload
        except Exception as exc:
            logger.error("Position-sync state is invalid for %s: %s", self.ticker, exc)
            # A corrupt fence must fail closed instead of permitting another sell.
            self._position_sync_fence = {
                "expected_max_quantity": 0,
                "created_at": datetime.now().isoformat(),
                "state_error": True,
            }

    def _set_position_sync_fence(self, expected_max_quantity: int) -> None:
        """Block another sell until the broker confirms the post-fill quantity."""
        if self.mode != "live":
            return
        self._position_sync_fence = {
            "expected_max_quantity": max(0, int(expected_max_quantity)),
            "created_at": datetime.now().isoformat(),
        }
        path = self._position_sync_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(self._position_sync_fence, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(path)

    def _clear_position_sync_fence(self) -> None:
        self._position_sync_fence = None
        try:
            self._position_sync_state_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.error("Could not clear position-sync state for %s: %s", self.ticker, exc)
        if not (
            self._pending_order
            and str(self._pending_order.get("side") or "").upper() == "SELL"
        ):
            self._release_sell_lock("broker_position_confirmed")

    def _apply_position_sync_fence(self, observed_quantity: int) -> int:
        """Accept a broker position only after it reflects the latest sell fill."""
        observed = max(0, int(observed_quantity or 0))
        if self.mode != "live" or not self._position_sync_fence:
            return observed
        expected = int(self._position_sync_fence.get("expected_max_quantity", 0) or 0)
        if observed <= expected:
            self._clear_position_sync_fence()
            return observed
        self._last_signal_reason = (
            f"等待券商确认卖出后仓位：预期不超过 {expected} 股，当前仍显示 {observed} 股"
        )
        return expected

    def _reconcile_pending_order(self) -> None:
        """Pull the latest broker status for any submitted live order."""
        pending = self._pending_order
        if not pending or not pending.get("order_id"):
            return

        try:
            order = self.broker.get_order(pending["order_id"])
        except Exception as exc:
            logger.warning("Failed to reconcile pending order %s: %s", pending.get("order_id", "?"), exc)
            return
        if order is None:
            return

        previous_filled = int(pending.get("acknowledged_filled_quantity", 0) or 0)
        current_filled = int(order.filled_quantity or 0)
        delta_filled = max(0, current_filled - previous_filled)

        if delta_filled > 0:
            if pending["side"] == "BUY":
                self._apply_buy_fill(order, delta_filled)
            else:
                self._apply_sell_fill(order, delta_filled, pending)
            pending["acknowledged_filled_quantity"] = current_filled
            self._persist_pending_order()

        if order.status == OrderStatus.PARTIALLY_FILLED:
            self._last_signal_reason = (
                f"订单部分成交：{pending['side']} {current_filled}/{pending['requested_quantity']} 股"
            )
            return

        if order.status == OrderStatus.FILLED:
            self._clear_pending_order()
            return

        if order.status == OrderStatus.CANCELLED:
            self._last_signal_reason = f"订单已取消：{pending['side']} {pending['order_id'][:12]}"
            self._clear_pending_order()
            if not self._position_sync_fence:
                self._release_sell_lock("order_cancelled")
            return

        if order.status == OrderStatus.REJECTED:
            self._last_signal_reason = f"订单被拒绝：{order.notes or pending['order_id'][:12]}"
            self.notifier.alert(self._last_signal_reason, "warning")
            self._clear_pending_order()
            if not self._position_sync_fence:
                self._release_sell_lock("order_rejected")

    def _apply_buy_fill(self, order, filled_quantity: int) -> None:
        fill_price = float(order.avg_fill_price or self._entry_price or 0.0)
        if fill_price > 0:
            self._entry_price = fill_price
            self.strategy.record_entry(fill_price)
        self._position_shares = max(self._position_shares, int(order.filled_quantity or 0))
        order_id = str(getattr(order, "order_id", "") or "")
        fill_total = int(getattr(order, "filled_quantity", 0) or filled_quantity or 0)
        notification_key = (
            f"{self.mode}:{self.ticker}:BUY:{order_id}:{fill_total}"
            if order_id
            else None
        )
        self.notifier.trade(
            self.ticker,
            "BUY",
            filled_quantity,
            fill_price or 0.0,
            mode=self.mode,
            fill_id=f"{order_id}:{fill_total}" if order_id else None,
            event_id=notification_key,
            notification_key=notification_key,
        )
        self._last_signal_reason = (
            f"买单已成交 {filled_quantity} 股 @ ${fill_price:.2f}"
            if fill_price > 0
            else f"买单已成交 {filled_quantity} 股"
        )

    def _apply_sell_fill(self, order, filled_quantity: int, pending: dict) -> None:
        fill_price = float(order.avg_fill_price or 0.0)
        entry_price = float(pending.get("entry_price_before") or self._entry_price or 0.0)
        pnl = ((fill_price - entry_price) * filled_quantity) if entry_price > 0 and fill_price > 0 else None

        order_id = str(getattr(order, "order_id", "") or "")
        fill_total = int(getattr(order, "filled_quantity", 0) or filled_quantity or 0)
        notification_key = (
            f"{self.mode}:{self.ticker}:SELL:{order_id}:{fill_total}"
            if order_id
            else None
        )
        self.notifier.trade(
            self.ticker,
            "SELL",
            filled_quantity,
            fill_price or 0.0,
            pnl,
            mode=self.mode,
            fill_id=f"{order_id}:{fill_total}" if order_id else None,
            event_id=notification_key,
            notification_key=notification_key,
        )

        if entry_price > 0 and fill_price > 0:
            self.risk.record_trade(TradeRecord(
                entry_time=pending.get("created_at") or datetime.now(),
                exit_time=datetime.now(),
                entry_price=entry_price,
                exit_price=fill_price,
                shares=filled_quantity,
                pnl=pnl or 0.0,
                pnl_pct=((fill_price - entry_price) / entry_price * 100),
                side="LONG",
            ))

        remaining = max(0, self._position_shares - filled_quantity)
        self._position_shares = remaining
        self._set_position_sync_fence(remaining)
        if remaining <= 0:
            self._entry_price = None
            self.strategy.clear_entry()
        self._last_signal_reason = (
            f"卖单已成交 {filled_quantity} 股 @ ${fill_price:.2f}"
            if fill_price > 0
            else f"卖单已成交 {filled_quantity} 股"
        )

    def _is_trading_hours(self) -> bool:
        """Check if we're currently in US regular trading hours."""
        # The anytime override is for paper simulations only. Live execution
        # must always remain inside the verified regular-market session.
        if self._ignore_trading_hours and self.mode != "live":
            return True

        th = self.config.trading_hours

        if not HAS_PYTZ or self._ny_tz is None:
            # Live trading must fail closed when its configured timezone is unavailable.
            return self.mode != "live"

        now_ny = datetime.now(self._ny_tz)
        session_end = self._market_session_end(now_ny.date())
        if session_end is None:
            return False

        # Parse time strings
        start_h, start_m = map(int, th.start.split(":"))
        end_h, end_m = map(int, session_end.split(":"))

        current_minutes = now_ny.hour * 60 + now_ny.minute
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m

        return start_minutes <= current_minutes <= end_minutes

    def _market_session_end(self, session_date: date) -> Optional[str]:
        """Return the NYSE close time, or None for weekends and major holidays."""
        if session_date.weekday() >= 5 or session_date in self._market_holidays(session_date.year):
            return None
        if session_date in self._market_early_closes(session_date.year):
            return self.config.trading_hours.early_close
        return self.config.trading_hours.end

    @classmethod
    def _market_holidays(cls, year: int) -> set[date]:
        def nth_weekday(month: int, weekday: int, n: int) -> date:
            first = date(year, month, 1)
            day = 1 + ((weekday - first.weekday()) % 7) + (n - 1) * 7
            return date(year, month, day)

        def last_weekday(month: int, weekday: int) -> date:
            day = calendar.monthrange(year, month)[1]
            value = date(year, month, day)
            return value - timedelta(days=(value.weekday() - weekday) % 7)

        def observed(value: date) -> date:
            if value.weekday() == 5:
                return value - timedelta(days=1)
            if value.weekday() == 6:
                return value + timedelta(days=1)
            return value

        easter = cls._easter_sunday(year)
        holidays = {
            observed(date(year, 1, 1)),
            nth_weekday(1, 0, 3),
            nth_weekday(2, 0, 3),
            easter - timedelta(days=2),
            last_weekday(5, 0),
            observed(date(year, 6, 19)),
            observed(date(year, 7, 4)),
            nth_weekday(9, 0, 1),
            nth_weekday(11, 3, 4),
            observed(date(year, 12, 25)),
        }
        # A Monday-observed New Year can belong to the following calendar year.
        next_new_year = observed(date(year + 1, 1, 1))
        if next_new_year.year == year:
            holidays.add(next_new_year)
        return holidays

    @classmethod
    def _market_early_closes(cls, year: int) -> set[date]:
        holidays = cls._market_holidays(year)

        def previous_session(value: date) -> date:
            value -= timedelta(days=1)
            while value.weekday() >= 5 or value in holidays:
                value -= timedelta(days=1)
            return value

        thanksgiving = next(
            value for value in holidays if value.month == 11 and value.weekday() == 3
        )
        closes = {
            previous_session(date(year, 7, 4)),
            thanksgiving + timedelta(days=1),
        }
        christmas_eve = date(year, 12, 24)
        if christmas_eve.weekday() < 5 and christmas_eve not in holidays:
            closes.add(christmas_eve)
        return closes

    @staticmethod
    def _easter_sunday(year: int) -> date:
        """Gregorian Easter date used to derive the Good Friday closure."""
        a = year % 19
        b, c = divmod(year, 100)
        d, e = divmod(b, 4)
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i, k = divmod(c, 4)
        month_seed = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * month_seed) // 451
        month = (h + month_seed - 7 * m + 114) // 31
        day = (h + month_seed - 7 * m + 114) % 31 + 1
        return date(year, month, day)

    def _sleep_with_status(self, seconds: int, reason: str) -> None:
        """Sleep with status message."""
        logger.debug(f"Sleeping {seconds}s: {reason}")
        # Break sleep into 1s chunks to stay responsive
        for _ in range(seconds):
            if not self._running:
                break
            time.sleep(1)

    def _print_header(self) -> None:
        """Print startup banner."""
        rs = self.strategy.get_range_state()
        print("\n" + "=" * 60)
        print(f"  🎯 SOXS Range Arbitrage Engine")
        print(f"  Mode: {self.mode.upper()}{' (24/7)' if self._ignore_trading_hours else ''}")
        print(f"  Ticker: {self.ticker}")
        print(f"  Range: ${rs.support:.2f} – ${rs.resistance:.2f} "
              f"({rs.spread_dollars:.2f} spread, {rs.spread_pct:.1f}%)")
        print(f"  Position: {self.config.position.size_per_trade} shares/trade, "
              f"{self.config.position.max_position} max")
        print(f"  Trend Filter: {'✅ ON' if self.config.trend_filter.enabled else '❌ OFF'} "
              f"(MA{self.config.trend_filter.ma_period}, "
              f"min strength {self.config.trend_filter.min_trend_strength}%)")
        print(f"  Stop Loss: {self.config.risk.stop_loss_pct}%")
        print(f"  Daily Loss Limit: ${self.config.risk.daily_loss_limit}")
        print("=" * 60 + "\n")

    def _shutdown(self) -> None:
        """Clean shutdown."""
        self._running = False

        # Show final summary
        stats = self.risk.get_stats()
        self.notifier.summary(stats)

        # Close broker connection
        self.broker.disconnect()

        run_time = datetime.now() - self._start_time if self._start_time else None
        print(f"\n🏁 Engine stopped. Run time: {run_time}")
        print(f"   Trades: {stats['total_trades']} | "
              f"Win rate: {stats['win_rate']}% | "
              f"Total P&L: ${stats['total_pnl']:+.2f}")
