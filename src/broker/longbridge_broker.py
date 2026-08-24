"""
Long Bridge (长桥证券) broker implementation for the live trading engine.

This adapter is the main live path used by ``run.py --live``.
It supports:
- API key credentials from env vars or config
- sandbox/prod endpoint override via env/config
- audit logging to ``logs/trades-YYYYMMDD.jsonl``
"""
import dataclasses
import fcntl
import json
import logging
import os
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from enum import Enum
from threading import Lock
from urllib.parse import urlparse

import longbridge.openapi as lb

from ..config.runtime_values import get_runtime_env
from ..config.runtime_paths import resolve_logs_dir, resolve_state_dir
from ..safety.execution_authorizer import authorize_mutation
from .base import (
    AccountInfo,
    BrokerBase,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)

logger = logging.getLogger(__name__)
_SHARED_SNAPSHOT_LOCK = Lock()
_BROKER_API_LOCK_DIR: Path | None = None
_BROKER_API_LOCK_FILE: Path | None = None
_BROKER_API_LOCK_FD: int | None = None

def _acquire_broker_api_lock(lock_dir: Path, timeout_seconds: float = 15.0) -> bool:
    """Acquire an inter-process advisory lock on the broker API.
    Returns True if lock acquired. The fd is stored globally; call
    _release_broker_api_lock() to unlock."""
    global _BROKER_API_LOCK_DIR, _BROKER_API_LOCK_FILE, _BROKER_API_LOCK_FD
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "longbridge_api.lock"
    _BROKER_API_LOCK_DIR = lock_dir
    _BROKER_API_LOCK_FILE = lock_path
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(fd, str(os.getpid()).encode())
            os.fsync(fd)
            _BROKER_API_LOCK_FD = fd
            return True
        except (BlockingIOError, OSError):
            time.sleep(random.uniform(0.3, 1.0))
            continue
    logger.warning("Timed out waiting for broker API lock after %.1fs", timeout_seconds)
    return False


def _release_broker_api_lock() -> None:
    """Release the inter-process broker API lock by closing the fd."""
    global _BROKER_API_LOCK_FD
    fd = _BROKER_API_LOCK_FD
    _BROKER_API_LOCK_FD = None
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass

TRUE_VALUES = {"1", "true", "yes", "y", "on"}

DEFAULT_PROD_HTTP_URL = "https://openapi.longbridge.com"
DEFAULT_PROD_QUOTE_WS_URL = "wss://openapi-quote.longbridge.com/v2"
DEFAULT_PROD_TRADE_WS_URL = "wss://openapi-trade.longbridge.com/v2"


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def _jsonable(value):
    try:
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Enum):
            return value.value
        if callable(value):
            return getattr(value, "__name__", repr(value))
        if dataclasses.is_dataclass(value):
            return {k: _jsonable(v) for k, v in dataclasses.asdict(value).items()}
        if isinstance(value, dict):
            return {str(k): _jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_jsonable(v) for v in value]
        if hasattr(value, "__dict__"):
            try:
                attrs = vars(value)
            except Exception:
                attrs = {}
            return {
                key: _jsonable(val)
                for key, val in attrs.items()
                if not key.startswith("_")
            }
        return str(value)
    except Exception:
        return repr(value)


def _first_item(value):
    if isinstance(value, (list, tuple)) and value:
        return value[0]
    return value


def _balance_field(value, *names, default=0.0):
    value = _first_item(value)
    for name in names:
        if isinstance(value, dict):
            if name in value and value[name] is not None:
                return value[name]
        elif hasattr(value, name):
            attr = getattr(value, name)
            if attr is not None:
                return attr
    return default


def _quote_field(value, *names, default=0.0):
    value = _first_item(value)
    for name in names:
        if isinstance(value, dict):
            if name in value and value[name] is not None:
                return value[name]
        elif hasattr(value, name):
            attr = getattr(value, name)
            if attr is not None:
                return attr
    return default


def _enum_token(value) -> str:
    """Normalize SDK enum-like objects to a stable string token."""
    if value is None:
        return ""
    raw = getattr(value, "value", value)
    return str(raw)


def _normalize_base_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper().split(".")[0]


def _longbridge_symbol(symbol: str) -> str:
    base = _normalize_base_symbol(symbol)
    if not base:
        return ""
    return base if "." in base else f"{base}.US"


def _is_rate_limit_error(error: Exception | str) -> bool:
    message = str(error or "").strip().lower()
    return "rate limit" in message or "too many request" in message or "too many requests" in message


def _global_reduce_only_enabled() -> bool:
    try:
        state_dir = resolve_state_dir(Path(__file__).resolve().parents[2])
        flags_path = state_dir / "trading_flags.json"
        flags = json.loads(flags_path.read_text(encoding="utf-8"))
        return bool(flags.get("reduce_only_all", False))
    except Exception:
        # Direct live broker calls fail closed when the shared flag cannot be read.
        return True


class LongBridgeBroker(BrokerBase):
    """Long Bridge broker with explicit production and sandbox separation.

    Sandbox connections may read broker data and submit only to explicitly
    configured non-production endpoints. Production Longbridge endpoints are
    rejected in sandbox mode before any SDK order call is made.
    """

    def __init__(
        self,
        app_key: str = "",
        app_secret: str = "",
        access_token: str = "",
        account_type: str = "",
        region: str = "cn",
        environment: str = "prod",
        http_url: str | None = None,
        quote_ws_url: str | None = None,
        trade_ws_url: str | None = None,
        log_path: str | None = None,
        audit_dir: str | None = None,
        allow_live_order: bool = False,
        execution_mode: str | None = None,
    ):
        self._app_key = (
            get_runtime_env("LONGBRIDGE_APP_KEY")
            or get_runtime_env("LONGBRIDGE_API_KEY")
            or app_key
        )
        self._app_secret = (
            get_runtime_env("LONGBRIDGE_APP_SECRET")
            or get_runtime_env("LONGBRIDGE_API_SECRET")
            or app_secret
        )
        self._access_token = get_runtime_env("LONGBRIDGE_ACCESS_TOKEN", access_token)
        self._account_type = get_runtime_env("LONGBRIDGE_ACCOUNT_TYPE", account_type).strip().lower()
        self._region = get_runtime_env("LONGBRIDGE_REGION", region)
        self._environment = get_runtime_env("LONGBRIDGE_ENV", environment).strip().lower()
        self._allow_live_order = bool(allow_live_order)
        self._execution_mode = str(execution_mode or os.environ.get("QUANTCAIRN_EXECUTION_MODE", "")).strip().upper()

        self._http_url = get_runtime_env("LONGBRIDGE_HTTP_URL", http_url or "") or None
        self._quote_ws_url = get_runtime_env("LONGBRIDGE_QUOTE_WS_URL", quote_ws_url or "") or None
        self._trade_ws_url = get_runtime_env("LONGBRIDGE_TRADE_WS_URL", trade_ws_url or "") or None

        if self._environment == "sandbox":
            self._http_url = self._http_url or get_runtime_env("LONGBRIDGE_SANDBOX_HTTP_URL")
            self._quote_ws_url = self._quote_ws_url or get_runtime_env("LONGBRIDGE_SANDBOX_QUOTE_WS_URL")
            self._trade_ws_url = self._trade_ws_url or get_runtime_env("LONGBRIDGE_SANDBOX_TRADE_WS_URL")

        self._sdk_log_path = get_runtime_env("LONGBRIDGE_LOG_PATH", log_path or "") or None

        audit_dir_value = get_runtime_env("LONGBRIDGE_AUDIT_DIR", audit_dir or "") or None
        if audit_dir_value:
            self._audit_dir = Path(audit_dir_value)
        else:
            self._audit_dir = resolve_logs_dir(Path(__file__).resolve().parents[2])
        try:
            self._audit_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            fallback = resolve_logs_dir(Path(__file__).resolve().parents[2])
            fallback.mkdir(parents=True, exist_ok=True)
            self._audit_dir = fallback
        self._connected = False
        self._sdk_config: Optional[lb.Config] = None
        self._trade_ctx: Optional[lb.TradeContext] = None
        self._quote_ctx: Optional[lb.QuoteContext] = None
        self._account_cache = AccountInfo(cash=0, equity=0, buying_power=0, positions=[])
        self._positions_cache: list[Position] = []
        self._positions_snapshot_reliable = False
        self._account_snapshot_reliable = False
        self._last_connect_error = ""
        self._last_positions_error = ""
        self._last_account_error = ""
        self._sandbox_first_run_confirmed = False
        self._sandbox_first_run_summary: dict[str, object] = {}
        self._sandbox_bootstrap_ticker = ""
        self._account_cache_fetched_at = 0.0
        self._positions_cache_fetched_at = 0.0
        self._active_orders_cache: dict[str, list[Order]] = {}
        self._active_orders_cache_fetched_at: dict[str, float] = {}
        self._active_orders_retry_not_before: dict[str, float] = {}
        self._account_retry_not_before = 0.0
        self._positions_retry_not_before = 0.0
        ttl_env = os.environ.get("LONGBRIDGE_CACHE_TTL_SECONDS")
        ttl_seconds = float(ttl_env) if ttl_env else 180.0
        self._account_cache_ttl_seconds = max(15.0, ttl_seconds)
        self._positions_cache_ttl_seconds = max(15.0, ttl_seconds)
        retry_env = os.environ.get("LONGBRIDGE_RETRY_BACKOFF_SECONDS")
        retry_seconds = float(retry_env) if retry_env else 45.0
        self._cache_retry_backoff_seconds = max(
            15.0,
            min(
                retry_seconds,
                max(self._account_cache_ttl_seconds, self._positions_cache_ttl_seconds),
            ),
        )
        state_root = resolve_state_dir(Path(__file__).resolve().parents[2])
        self._shared_snapshot_dir = state_root / "broker_cache"
        self._shared_snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._shared_positions_path = self._shared_snapshot_dir / "longbridge_positions.json"
        self._shared_account_path = self._shared_snapshot_dir / "longbridge_account.json"
        self._sandbox_bootstrap_path = self._shared_snapshot_dir / "longbridge_sandbox_bootstrap.json"
        active_ttl_env = os.environ.get("LONGBRIDGE_ACTIVE_ORDERS_CACHE_TTL_SECONDS")
        active_ttl_seconds = float(active_ttl_env) if active_ttl_env else 20.0
        self._active_orders_cache_ttl_seconds = max(5.0, min(active_ttl_seconds, 300.0))

    def _shared_cache_ttl_seconds(self) -> float:
        raw_value = os.environ.get("LONGBRIDGE_SHARED_CACHE_TTL_SECONDS", "").strip()
        try:
            ttl = float(raw_value) if raw_value else 180.0
        except ValueError:
            ttl = 180.0
        return max(30.0, min(ttl, 900.0))

    def _load_shared_snapshot(self, path: Path) -> dict | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        fetched_at = float(payload.get("fetched_at") or 0.0)
        if fetched_at <= 0:
            return None
        if (time.time() - fetched_at) > self._shared_cache_ttl_seconds():
            return None
        return payload

    def _write_shared_snapshot(self, path: Path, payload: dict) -> None:
        wrapped = {
            "fetched_at": time.time(),
            "payload": payload,
        }
        # Unique temp file per process to avoid cross-engine race conditions
        suffix = f".tmp.{os.getpid()}.{random.randint(10000, 99999)}"
        temp_path = path.with_suffix(path.suffix + suffix)
        with _SHARED_SNAPSHOT_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_text(json.dumps(wrapped, ensure_ascii=False, default=str), encoding="utf-8")
            temp_path.replace(path)
            # Clean up any stale tmp files from other processes
            for f in path.parent.glob(path.name + ".tmp.*"):
                if f.name != temp_path.name:
                    try:
                        f.unlink()
                    except OSError:
                        pass

    def _position_to_payload(self, position: Position) -> dict:
        return {
            "ticker": position.ticker,
            "quantity": position.quantity,
            "avg_entry_price": position.avg_entry_price,
            "current_price": position.current_price,
            "market_value": position.market_value,
            "unrealized_pnl": position.unrealized_pnl,
            "unrealized_pnl_pct": position.unrealized_pnl_pct,
        }

    def _positions_from_payload(self, payload: list[dict]) -> list[Position]:
        rows: list[Position] = []
        for item in payload or []:
            if not isinstance(item, dict):
                continue
            try:
                rows.append(
                    Position(
                        ticker=str(item.get("ticker") or ""),
                        quantity=int(item.get("quantity") or 0),
                        avg_entry_price=float(item.get("avg_entry_price") or 0.0),
                        current_price=float(item.get("current_price") or 0.0),
                        market_value=float(item.get("market_value") or 0.0),
                        unrealized_pnl=float(item.get("unrealized_pnl") or 0.0),
                        unrealized_pnl_pct=float(item.get("unrealized_pnl_pct") or 0.0),
                    )
                )
            except Exception:
                continue
        return rows

    def _account_to_payload(self, account: AccountInfo) -> dict:
        return {
            "cash": account.cash,
            "equity": account.equity,
            "buying_power": account.buying_power,
            "positions": [self._position_to_payload(position) for position in account.positions],
        }

    def _account_from_payload(self, payload: dict) -> AccountInfo:
        positions = self._positions_from_payload(payload.get("positions") or [])
        return AccountInfo(
            cash=float(payload.get("cash") or 0.0),
            equity=float(payload.get("equity") or 0.0),
            buying_power=float(payload.get("buying_power") or 0.0),
            positions=positions,
        )

    def _can_reuse_positions_cache(self, now: float) -> bool:
        return bool(
            self._positions_cache
            and self._positions_cache_fetched_at > 0
            and (now - self._positions_cache_fetched_at) <= (self._positions_cache_ttl_seconds * 2)
        )

    def _can_reuse_account_cache(self, now: float) -> bool:
        return bool(
            self._account_cache_fetched_at > 0
            and (now - self._account_cache_fetched_at) <= (self._account_cache_ttl_seconds * 2)
        )

    def _active_orders_cache_path(self, ticker: str) -> Path:
        return self._shared_snapshot_dir / f"longbridge_active_orders_{_normalize_base_symbol(ticker)}.json"

    def _can_reuse_active_orders_cache(self, ticker: str, now: float) -> bool:
        fetched_at = float(self._active_orders_cache_fetched_at.get(_normalize_base_symbol(ticker)) or 0.0)
        return (
            _normalize_base_symbol(ticker) in self._active_orders_cache
            and fetched_at > 0
            and (now - fetched_at) <= (self._active_orders_cache_ttl_seconds * 2)
        )

    def invalidate_cache(self) -> None:
        """Force the next account/position read to hit the broker."""
        self._account_cache_fetched_at = 0.0
        self._positions_cache_fetched_at = 0.0
        self._active_orders_cache_fetched_at = {}
        self._account_retry_not_before = 0.0
        self._positions_retry_not_before = 0.0
        self._active_orders_retry_not_before = {}

    def is_positions_snapshot_reliable(self) -> bool:
        """Whether the latest positions response is confirmed by the broker."""
        return self._positions_snapshot_reliable

    def is_account_snapshot_reliable(self) -> bool:
        """Whether the latest account response is confirmed by the broker."""
        return self._account_snapshot_reliable

    def last_connect_error(self) -> str:
        return self._last_connect_error

    def last_positions_error(self) -> str:
        return self._last_positions_error

    def last_account_error(self) -> str:
        return self._last_account_error

    def _audit_path(self) -> Path:
        return self._audit_dir / f"trades-{datetime.now().strftime('%Y%m%d')}.jsonl"

    def _sandbox_bootstrap_fingerprint(self) -> dict[str, str]:
        return {
            "environment": self._environment,
            "account_type": self._account_type,
            "region": self._region,
            "http_url": str(self._http_url or ""),
            "quote_ws_url": str(self._quote_ws_url or ""),
            "trade_ws_url": str(self._trade_ws_url or ""),
        }

    def _load_sandbox_bootstrap_state(self) -> dict | None:
        path = self._sandbox_bootstrap_path
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        if not bool(payload.get("confirmed", False)):
            return None
        fingerprint = payload.get("fingerprint")
        if not isinstance(fingerprint, dict):
            return None
        if fingerprint != self._sandbox_bootstrap_fingerprint():
            return None
        return payload

    def _write_sandbox_bootstrap_state(self, payload: dict) -> None:
        try:
            record = {
                "confirmed": bool(payload.get("confirmed", False)),
                "confirmed_at": payload.get("confirmed_at") or datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "ticker": str(payload.get("ticker") or self._sandbox_bootstrap_ticker or ""),
                "fingerprint": self._sandbox_bootstrap_fingerprint(),
                "summary": _jsonable(payload.get("summary") or {}),
            }
            temp_path = self._sandbox_bootstrap_path.with_suffix(
                self._sandbox_bootstrap_path.suffix + f".tmp.{os.getpid()}.{random.randint(10000, 99999)}"
            )
            temp_path.write_text(json.dumps(record, ensure_ascii=False, default=str), encoding="utf-8")
            temp_path.replace(self._sandbox_bootstrap_path)
        except Exception as exc:
            logger.warning("Sandbox bootstrap state write skipped: %s", exc)

    def sandbox_first_run_confirmed(self) -> bool:
        return bool(self._sandbox_first_run_confirmed)

    def sandbox_first_run_summary(self) -> dict[str, object]:
        return dict(self._sandbox_first_run_summary or {})

    def get_orders(self, ticker: str = "") -> Optional[list[Order]]:
        """Compatibility alias for the sandbox bootstrap flow."""
        return self.get_active_orders(ticker or self._sandbox_bootstrap_ticker)

    def _write_audit(self, action: str, request: dict, response: dict, *, ok: bool, error: str | None = None) -> None:
        try:
            record = {
                "timestamp": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
                "broker": "longbridge",
                "environment": self._environment,
                "region": self._region,
                "action": action,
                "ok": ok,
                "request": _jsonable(request),
                "response": _jsonable(response),
            }
            if error:
                record["error"] = error

            with self._audit_path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception as audit_error:
            logger.warning("Long Bridge audit write skipped for %s: %s", action, audit_error)

    def _build_config(self) -> lb.Config:
        """Build a LongBridge SDK config from credentials and endpoint overrides."""
        return lb.Config.from_apikey(
            self._app_key,
            self._app_secret,
            self._access_token,
            http_url=self._http_url,
            quote_ws_url=self._quote_ws_url,
            trade_ws_url=self._trade_ws_url,
            log_path=self._sdk_log_path,
        )

    def _is_sandbox_mode(self) -> bool:
        return self._environment == "sandbox"

    def _sandbox_safety_issues(self) -> list[str]:
        """Return reasons sandbox startup/order flow should be blocked."""
        if not self._is_sandbox_mode():
            return []
        issues: list[str] = []
        if self._account_type not in {"paper", "demo"}:
            issues.append("sandbox mode requires paper/demo account_type")
        if self._allow_live_order:
            issues.append("sandbox mode requires allow_live_order=false")
        endpoint_defaults = {
            "http_url": DEFAULT_PROD_HTTP_URL,
            "quote_ws_url": DEFAULT_PROD_QUOTE_WS_URL,
            "trade_ws_url": DEFAULT_PROD_TRADE_WS_URL,
        }
        for name, expected in endpoint_defaults.items():
            value = getattr(self, f"_{name}")
            if not value:
                issues.append(f"sandbox mode requires {name}")
                continue
            parsed = urlparse(value)
            if parsed.scheme not in {"http", "https", "ws", "wss"}:
                issues.append(f"sandbox {name} has invalid scheme: {value}")
                continue
            if str(value).strip().rstrip("/") != expected:
                issues.append(f"sandbox {name} must use the official Longbridge endpoint: {expected}")
        return issues

    def _sandbox_endpoints_are_safe(self) -> bool:
        return not self._sandbox_safety_issues()

    def connect(self) -> bool:
        if not self._app_key or not self._app_secret or not self._access_token:
            logger.error(
                "Long Bridge credentials not configured. Set config.yaml fields "
                "or LONGBRIDGE_APP_KEY / LONGBRIDGE_APP_SECRET / LONGBRIDGE_ACCESS_TOKEN."
            )
            return False

        if self._environment == "sandbox" and not (self._http_url and self._quote_ws_url and self._trade_ws_url):
            error_msg = (
                "Long Bridge sandbox selected but endpoint URLs are incomplete. "
                "Set the official Longbridge http_url, quote_ws_url and trade_ws_url."
            )
            logger.error(error_msg)
            self._last_connect_error = error_msg
            self._write_audit(
                "connect",
                {
                    "environment": self._environment,
                    "region": self._region,
                    "http_url": self._http_url,
                    "quote_ws_url": self._quote_ws_url,
                    "trade_ws_url": self._trade_ws_url,
                },
                {"connected": False, "reason": "sandbox endpoint URLs incomplete"},
                ok=False,
                error=error_msg,
            )
            return False
        if self._environment == "sandbox":
            sandbox_issues = self._sandbox_safety_issues()
            if sandbox_issues:
                error_msg = "; ".join(sandbox_issues)
                logger.error(error_msg)
                self._last_connect_error = error_msg
                self._write_audit(
                    "connect",
                    {
                        "environment": self._environment,
                        "account_type": self._account_type,
                        "region": self._region,
                        "http_url": self._http_url,
                        "quote_ws_url": self._quote_ws_url,
                        "trade_ws_url": self._trade_ws_url,
                    },
                    {"connected": False, "reason": error_msg},
                    ok=False,
                    error=error_msg,
                )
                return False
        if self._is_sandbox_mode():
            bootstrap_state = self._load_sandbox_bootstrap_state()
            if bootstrap_state is not None:
                self._sandbox_first_run_confirmed = True
                self._sandbox_first_run_summary = dict(bootstrap_state.get("summary") or {})
                self._sandbox_bootstrap_ticker = str(bootstrap_state.get("ticker") or self._sandbox_bootstrap_ticker or "")
                logger.info("Sandbox first-run bootstrap already confirmed; BUY flow allowed after read checks")
            else:
                self._sandbox_first_run_confirmed = False
                self._sandbox_first_run_summary = {
                    "confirmed": False,
                    "reason": "sandbox first-run bootstrap pending",
                }
                logger.info("Sandbox first-run bootstrap pending; automatic trading stays read-only until confirmed")

        try:
            self._sdk_config = self._build_config()
            self._trade_ctx = lb.TradeContext(self._sdk_config)
            self._quote_ctx = lb.QuoteContext(self._sdk_config)
            self._connected = True
            self._last_connect_error = ""
            logger.info(
                "✅ Long Bridge connected (environment: %s, region: %s)",
                self._environment,
                self._region,
            )
            self._write_audit(
                "connect",
                {
                    "environment": self._environment,
                    "account_type": self._account_type,
                    "region": self._region,
                    "http_url": self._http_url,
                    "quote_ws_url": self._quote_ws_url,
                    "trade_ws_url": self._trade_ws_url,
                },
                {"connected": True},
                ok=True,
            )
            return True
        except lb.OpenApiException as e:
            self._last_connect_error = str(e)
            logger.error(f"Long Bridge auth failed: {e}")
            self._write_audit(
                "connect",
                {
                    "environment": self._environment,
                    "account_type": self._account_type,
                    "region": self._region,
                    "http_url": self._http_url,
                    "quote_ws_url": self._quote_ws_url,
                    "trade_ws_url": self._trade_ws_url,
                },
                {"connected": False},
                ok=False,
                error=str(e),
            )
            return False

        except Exception as e:
            self._last_connect_error = str(e)
            logger.error(f"Long Bridge connection failed: {e}")
            self._write_audit(
                "connect",
                {
                    "environment": self._environment,
                    "region": self._region,
                    "http_url": self._http_url,
                    "quote_ws_url": self._quote_ws_url,
                    "trade_ws_url": self._trade_ws_url,
                },
                {"connected": False},
                ok=False,
                error=str(e),
            )
            return False

    def confirm_sandbox_first_run(self, ticker: str = "") -> dict[str, object]:
        """Run the sandbox read-only bootstrap and persist the confirmation."""
        summary: dict[str, object] = {
            "confirmed": False,
            "reason": "",
            "ticker": _normalize_base_symbol(ticker or self._sandbox_bootstrap_ticker),
            "account_ok": False,
            "positions_ok": False,
            "orders_ok": False,
            "positions_count": 0,
            "orders_count": 0,
        }
        if not self._is_sandbox_mode():
            summary["reason"] = "sandbox bootstrap is only available in sandbox mode"
            self._sandbox_first_run_summary = summary
            return summary
        if not self.is_connected():
            summary["reason"] = "broker not connected"
            self._sandbox_first_run_summary = summary
            return summary
        fingerprint_match = self._load_sandbox_bootstrap_state()
        if fingerprint_match is not None:
            self._sandbox_first_run_confirmed = True
            cached_summary = dict(fingerprint_match.get("summary") or {})
            cached_summary.setdefault("confirmed", True)
            cached_summary.setdefault("reason", "sandbox bootstrap already confirmed")
            self._sandbox_first_run_summary = cached_summary
            if not self._sandbox_bootstrap_ticker:
                self._sandbox_bootstrap_ticker = str(fingerprint_match.get("ticker") or summary["ticker"] or "")
            return dict(self._sandbox_first_run_summary)
        try:
            self._sandbox_bootstrap_ticker = summary["ticker"] if isinstance(summary["ticker"], str) else ""
            positions = self.get_positions() or []
            account = self.get_account()
            orders = self.get_orders(self._sandbox_bootstrap_ticker) if self._sandbox_bootstrap_ticker else []
            summary["positions_count"] = len(positions)
            summary["orders_count"] = len(orders or [])
            summary["positions_ok"] = bool(self.is_positions_snapshot_reliable())
            summary["account_ok"] = account is not None and self.is_account_snapshot_reliable()
            summary["orders_ok"] = orders is not None
            summary["confirmed"] = bool(summary["account_ok"] and summary["positions_ok"] and summary["orders_ok"])
            if summary["confirmed"]:
                summary["reason"] = "sandbox bootstrap confirmed"
                self._sandbox_first_run_confirmed = True
                self._sandbox_first_run_summary = dict(summary)
                self._write_sandbox_bootstrap_state(
                    {
                        "confirmed": True,
                        "ticker": self._sandbox_bootstrap_ticker,
                        "summary": self._sandbox_first_run_summary,
                    }
                )
                self._write_audit(
                    "sandbox_first_run_confirmed",
                    {"ticker": self._sandbox_bootstrap_ticker},
                    _jsonable(self._sandbox_first_run_summary),
                    ok=True,
                )
                return dict(self._sandbox_first_run_summary)
            summary["reason"] = "sandbox bootstrap read-only checks incomplete"
        except Exception as exc:
            summary["reason"] = str(exc)
            logger.warning("Sandbox first-run confirmation failed: %s", exc)
        self._sandbox_first_run_confirmed = False
        self._sandbox_first_run_summary = dict(summary)
        self._write_audit(
            "sandbox_first_run_confirmed",
            {"ticker": self._sandbox_bootstrap_ticker},
            _jsonable(summary),
            ok=False,
            error=str(summary.get("reason") or "sandbox bootstrap failed"),
        )
        return dict(summary)

    def disconnect(self) -> None:
        self._connected = False
        self._trade_ctx = None
        self._quote_ctx = None
        self._sdk_config = None
        logger.info("Long Bridge disconnected")

    def is_connected(self) -> bool:
        return self._connected and self._trade_ctx is not None

    def place_order(
        self,
        ticker: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType = OrderType.MARKET,
        limit_price: Optional[float] = None,
        current_bid: float = 0.0,
        current_ask: float = 0.0,
        notes: str = "",
    ) -> Order:
        request = {
            "ticker": ticker,
            "side": side.value,
            "quantity": quantity,
            "order_type": order_type.value,
            "limit_price": limit_price,
            "current_bid": current_bid,
            "current_ask": current_ask,
            "notes": notes,
        }
        if self._is_sandbox_mode() and not self._sandbox_endpoints_are_safe():
            sandbox_issues = self._sandbox_safety_issues()
            response = {
                "status": "rejected",
                "reason": "; ".join(sandbox_issues) or "sandbox safety check failed",
            }
            self._write_audit(
                "place_order",
                request,
                response,
                ok=False,
                error="sandbox_safety_check_failed",
            )
            return Order(
                order_id="",
                ticker=ticker,
                side=side,
                order_type=order_type,
                quantity=quantity,
                status=OrderStatus.REJECTED,
                notes=response["reason"],
            )
        if self._is_sandbox_mode() and not self._sandbox_first_run_confirmed:
            response = {
                "status": "rejected",
                "reason": "sandbox first-run bootstrap not confirmed",
            }
            self._write_audit(
                "place_order",
                request,
                response,
                ok=False,
                error="sandbox_first_run_pending",
            )
            return Order(
                order_id="",
                ticker=ticker,
                side=side,
                order_type=order_type,
                quantity=quantity,
                status=OrderStatus.REJECTED,
                notes="Sandbox first-run bootstrap not confirmed; read-only mode",
            )
        if not self.is_connected():
            response = {"status": "rejected", "reason": "not connected"}
            self._write_audit("place_order", request, response, ok=False, error="Not connected to Long Bridge")
            return Order(
                order_id="",
                ticker=ticker,
                side=side,
                order_type=order_type,
                quantity=quantity,
                status=OrderStatus.REJECTED,
                notes="Not connected to Long Bridge",
            )
        if side == OrderSide.BUY and _global_reduce_only_enabled() and not self._is_sandbox_mode():
            response = {"status": "rejected", "reason": "global reduce-only blocks live BUY"}
            self._write_audit(
                "place_order",
                request,
                response,
                ok=False,
                error="global_reduce_only",
            )
            return Order(
                order_id="",
                ticker=ticker,
                side=side,
                order_type=order_type,
                quantity=quantity,
                status=OrderStatus.REJECTED,
                notes="Global reduce-only blocks live BUY",
            )
        authorization = authorize_mutation(execution_mode=self._execution_mode)
        if not authorization.allowed:
            response = {"status": "rejected", "reason": authorization.reason_code}
            self._write_audit("place_order", request, response, ok=False, error=authorization.reason_code)
            return Order(order_id="", ticker=ticker, side=side, order_type=order_type,
                         quantity=quantity, status=OrderStatus.REJECTED, notes=authorization.reason_code)

        try:
            lb_side = lb.OrderSide.Buy if side == OrderSide.BUY else lb.OrderSide.Sell
            lb_type = lb.OrderType.MO if order_type == OrderType.MARKET else lb.OrderType.LO
            submit_price = limit_price
            if lb_type == lb.OrderType.MO and not submit_price:
                submit_price = current_ask if side == OrderSide.BUY else current_bid
            lb_symbol = _longbridge_symbol(ticker)

            logger.info(
                "🔴 [LIVE/%s] %s %s %s @ %s",
                self._environment,
                side.value,
                quantity,
                ticker,
                "MKT" if order_type == OrderType.MARKET else f"${limit_price:.2f}",
            )

            result: lb.SubmitOrderResponse = self._trade_ctx.submit_order(
                symbol=lb_symbol,
                order_type=lb_type,
                side=lb_side,
                submitted_quantity=quantity,
                time_in_force=lb.TimeInForceType.Day,
                submitted_price=submit_price,
            )
            result_fields = []
            try:
                if isinstance(result, dict):
                    result_fields = sorted(str(key) for key in result.keys())
                else:
                    result_fields = sorted(str(key) for key in vars(result).keys() if not str(key).startswith("_"))
            except Exception:
                result_fields = []
            logger.debug(
                "LongBridge submit_order debug: type=%s repr=%s fields=%s error_field=%s",
                type(result).__name__,
                repr(result),
                result_fields,
                getattr(result, "error", None) if not isinstance(result, dict) else result.get("error"),
            )

            response = {
                "order_id": str(getattr(result, "order_id", "") or ""),
                "status": str(getattr(result, "status", "submitted") or "submitted"),
                "raw": _jsonable(result),
            }
            self._write_audit("place_order", request, response, ok=True)
            self.invalidate_cache()
            logger.info("  → Order ID: %s", response["order_id"])
            return Order(
                order_id=response["order_id"],
                ticker=ticker,
                side=side,
                order_type=order_type,
                quantity=quantity,
                limit_price=limit_price,
                status=OrderStatus.PENDING,
                notes=f"Live order {response['order_id'][:12]}...",
            )
        except lb.OpenApiException as e:
            error_msg = str(e)
            logger.error(f"Long Bridge order rejected: {error_msg}")
            self._write_audit(
                "place_order", request,
                {"error": error_msg, "status": "REJECTED"},
                ok=False, error=error_msg,
            )
            return Order(
                order_id="",
                ticker=ticker,
                side=side,
                order_type=order_type,
                quantity=quantity,
                status=OrderStatus.REJECTED,
                notes=f"API rejection: {error_msg}",
            )
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Long Bridge order error: {error_msg}")
            self._write_audit(
                "place_order", request,
                {"error": error_msg, "status": "REJECTED"},
                ok=False, error=error_msg,
            )
            return Order(
                order_id="",
                ticker=ticker,
                side=side,
                order_type=order_type,
                quantity=quantity,
                status=OrderStatus.REJECTED,
                notes=str(e),
            )

    def cancel_order(self, order_id: str) -> bool:
        request = {"order_id": order_id}
        authorization = authorize_mutation(execution_mode=self._execution_mode)
        if not authorization.allowed:
            self._write_audit("cancel_order", request, {"status": "rejected"}, ok=False, error=authorization.reason_code)
            return False
        if not self.is_connected():
            self._write_audit("cancel_order", request, {"status": "rejected"}, ok=False, error="Not connected")
            return False
        try:
            result = self._trade_ctx.cancel_order(order_id=order_id)
            self._write_audit("cancel_order", request, {"result": _jsonable(result)}, ok=True)
            self.invalidate_cache()
            logger.info("Canceled order %s", order_id)
            return True
        except Exception as e:
            logger.error(f"Cancel order failed: {e}")
            self._write_audit("cancel_order", request, {"error": str(e)}, ok=False, error=str(e))
            return False

    def get_order(self, order_id: str) -> Optional[Order]:
        request = {"order_id": order_id}
        if not self.is_connected():
            self._write_audit("get_order", request, {"status": "rejected"}, ok=False, error="Not connected")
            return None
        try:
            # The SDK declares order_id as positional-only. Passing it by keyword
            # causes every reconciliation request to fail after a live submission.
            od: lb.OrderDetail = self._trade_ctx.order_detail(order_id)
            status_map = {
                _enum_token(lb.OrderStatus.Filled): OrderStatus.FILLED,
                _enum_token(lb.OrderStatus.PartialFilled): OrderStatus.PARTIALLY_FILLED,
                _enum_token(lb.OrderStatus.Rejected): OrderStatus.REJECTED,
                _enum_token(lb.OrderStatus.Canceled): OrderStatus.CANCELLED,
                _enum_token(lb.OrderStatus.Expired): OrderStatus.CANCELLED,
                _enum_token(lb.OrderStatus.New): OrderStatus.PENDING,
                _enum_token(lb.OrderStatus.PendingCancel): OrderStatus.PENDING,
                _enum_token(lb.OrderStatus.WaitToNew): OrderStatus.PENDING,
            }
            mapped_status = status_map.get(_enum_token(od.status), OrderStatus.PENDING)
            if mapped_status in (
                OrderStatus.FILLED,
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.CANCELLED,
                OrderStatus.REJECTED,
            ):
                self.invalidate_cache()
            response = {"order": _jsonable(od), "mapped_status": mapped_status.value}
            self._write_audit("get_order", request, response, ok=True)
            return Order(
                order_id=od.order_id,
                ticker=od.symbol,
                side=OrderSide.BUY if od.side == lb.OrderSide.Buy else OrderSide.SELL,
                order_type=OrderType.MARKET if od.order_type == lb.OrderType.MO else OrderType.LIMIT,
                quantity=int(od.quantity or 0),
                limit_price=float(getattr(od, "price", 0.0) or 0.0) or None,
                filled_quantity=int(od.executed_quantity or 0),
                avg_fill_price=float(od.executed_price or 0.0),
                status=mapped_status,
                notes=str(od.msg or ""),
            )
        except Exception as e:
            logger.error(f"Get order failed: {e}")
            self._write_audit("get_order", request, {"error": str(e)}, ok=False, error=str(e))
            return None

    def get_active_orders(self, ticker: str) -> Optional[list[Order]]:
        """Return unresolved orders for a symbol so engine startup can fail closed."""
        symbol = _normalize_base_symbol(ticker)
        now = time.time()
        retry_not_before = float(self._active_orders_retry_not_before.get(symbol) or 0.0)
        if now < retry_not_before:
            return list(self._active_orders_cache.get(symbol) or [])
        cached_fetched_at = float(self._active_orders_cache_fetched_at.get(symbol) or 0.0)
        if cached_fetched_at > 0 and (now - cached_fetched_at) < self._active_orders_cache_ttl_seconds:
            return list(self._active_orders_cache.get(symbol) or [])
        shared_active_orders = self._load_shared_snapshot(self._active_orders_cache_path(symbol))
        if shared_active_orders is not None:
            shared_payload = shared_active_orders.get("payload") or {}
            if isinstance(shared_payload, dict):
                orders_payload = shared_payload.get("orders") or []
                orders = []
                for item in orders_payload:
                    if not isinstance(item, dict):
                        continue
                    try:
                        side_value = str(item.get("side") or "").strip().upper()
                        status_value = str(item.get("status") or "").strip().upper()
                        order_type_value = str(item.get("order_type") or "").strip().upper()
                        raw_limit_price = item.get("limit_price")
                        if raw_limit_price in (None, ""):
                            raw_limit_price = item.get("price")
                        orders.append(Order(
                            order_id=str(item.get("order_id") or ""),
                            ticker=str(item.get("ticker") or symbol),
                            side=OrderSide.BUY if side_value == "BUY" else OrderSide.SELL,
                            order_type=OrderType.LIMIT if order_type_value == "LIMIT" else OrderType.MARKET,
                            quantity=int(item.get("quantity") or 0),
                            limit_price=float(raw_limit_price) if raw_limit_price not in (None, "") else None,
                            filled_quantity=int(item.get("filled_quantity") or 0),
                            avg_fill_price=float(item.get("avg_fill_price") or 0.0),
                            status=OrderStatus.PARTIALLY_FILLED if status_value == "PARTIALLY_FILLED" else OrderStatus.PENDING,
                            notes=str(item.get("notes") or ""),
                        ))
                    except Exception:
                        continue
                self._active_orders_cache[symbol] = orders
                self._active_orders_cache_fetched_at[symbol] = float(
                    shared_active_orders.get("fetched_at") or now
                )
                self._active_orders_retry_not_before[symbol] = now
                return list(orders)
        if not self.is_connected():
            return []
        try:
            today_orders = getattr(self._trade_ctx, "today_orders", None)
            if callable(today_orders):
                details = today_orders(_longbridge_symbol(ticker))
            else:
                details = []
            active_statuses = (
                _enum_token(getattr(lb.OrderStatus, "New", "New")),
                _enum_token(getattr(lb.OrderStatus, "PartialFilled", "PartialFilled")),
                _enum_token(getattr(lb.OrderStatus, "PendingCancel", "PendingCancel")),
                _enum_token(getattr(lb.OrderStatus, "WaitToNew", "WaitToNew")),
                _enum_token(getattr(lb.OrderStatus, "NotReported", "NotReported")),
                _enum_token(getattr(lb.OrderStatus, "ProtectedNotReported", "ProtectedNotReported")),
                _enum_token(getattr(lb.OrderStatus, "VarietiesNotReported", "VarietiesNotReported")),
            )
            orders = []
            for od in details or []:
                status_token = _enum_token(od.status)
                if status_token not in active_statuses:
                    continue
                orders.append(Order(
                    order_id=od.order_id,
                    ticker=od.symbol,
                    side=OrderSide.BUY if od.side == lb.OrderSide.Buy else OrderSide.SELL,
                    order_type=OrderType.MARKET if od.order_type == lb.OrderType.MO else OrderType.LIMIT,
                    quantity=int(od.quantity or 0),
                    limit_price=float(getattr(od, "price", 0.0) or 0.0) or None,
                    filled_quantity=int(od.executed_quantity or 0),
                    avg_fill_price=float(od.executed_price or 0.0),
                    status=(
                        OrderStatus.PARTIALLY_FILLED
                        if status_token == _enum_token(lb.OrderStatus.PartialFilled)
                        else OrderStatus.PENDING
                    ),
                    notes=str(od.msg or ""),
                ))
            self._active_orders_cache[symbol] = list(orders)
            self._active_orders_cache_fetched_at[symbol] = now
            self._active_orders_retry_not_before[symbol] = now
            self._write_shared_snapshot(
                self._active_orders_cache_path(symbol),
                {
                    "orders": [
                        {
                            "order_id": order.order_id,
                            "ticker": order.ticker,
                            "side": order.side.value,
                            "order_type": order.order_type.value,
                            "quantity": order.quantity,
                            "limit_price": order.limit_price,
                            "filled_quantity": order.filled_quantity,
                            "avg_fill_price": order.avg_fill_price,
                            "status": order.status.value,
                            "notes": order.notes,
                        }
                        for order in orders
                    ],
                    "reliable": True,
                    "error": "",
                },
            )
            self._write_audit(
                "get_active_orders",
                {"ticker": ticker},
                {"count": len(orders), "order_ids": [o.order_id for o in orders]},
                ok=True,
            )
            return orders
        except Exception as exc:
            logger.error("Get active orders failed: %s", exc)
            self._active_orders_retry_not_before[symbol] = now + self._cache_retry_backoff_seconds
            if _is_rate_limit_error(exc) and self._can_reuse_active_orders_cache(symbol, now):
                logger.warning("Get active orders rate limited; reusing cached active-orders snapshot for %s", symbol)
                return list(self._active_orders_cache.get(symbol) or [])
            self._write_audit(
                "get_active_orders",
                {"ticker": ticker},
                {"error": str(exc)},
                ok=False,
                error=str(exc),
            )
            # None distinguishes an API failure from a confirmed empty result.
            return None

    def _try_lock_or_recheck_shared(self, now: float) -> str | None:
        """Try to acquire the broker API lock. If another engine holds it,
        re-check the shared cache after a brief delay and return the outcome.
        Returns None if the caller should return the cached value, or the lock
        outcome ('acquired' or 'deferred') if the caller should continue."""
        lock_dir = self._shared_snapshot_dir
        if _acquire_broker_api_lock(lock_dir, timeout_seconds=8.0):
            # Lock acquired — check shared cache one more time (another engine
            # may have refreshed it while we waited).
            shared = self._load_shared_snapshot(self._shared_positions_path)
            if shared is not None:
                payload = shared.get("payload") or {}
                if isinstance(payload, dict):
                    positions = self._positions_from_payload(payload.get("positions") or [])
                    if positions:
                        self._positions_cache = positions
                        self._positions_snapshot_reliable = bool(payload.get("reliable", True))
                        self._last_positions_error = str(payload.get("error") or "")
                        self._positions_cache_fetched_at = float(shared.get("fetched_at") or now)
                        self._positions_retry_not_before = now
                        _release_broker_api_lock()
                        return None  # tell caller to return cached
            return "acquired"
        # Could not acquire lock — wait and use shared cache
        shared = self._load_shared_snapshot(self._shared_positions_path)
        if shared is not None:
            payload = shared.get("payload") or {}
            if isinstance(payload, dict):
                positions = self._positions_from_payload(payload.get("positions") or [])
                if positions:
                    self._positions_cache = positions
                    self._positions_snapshot_reliable = bool(payload.get("reliable", True))
                    self._last_positions_error = str(payload.get("error") or "")
                    self._positions_cache_fetched_at = float(shared.get("fetched_at") or now)
                    self._positions_retry_not_before = now
                    return None  # tell caller to return cached
        # Fall through with stale cache
        self._positions_snapshot_reliable = False
        self._last_positions_error = "API lock timeout; using stale cache"
        return None

    def get_positions(self) -> list[Position]:
        request = {}
        now = time.time()
        if now < self._positions_retry_not_before:
            return list(self._positions_cache)
        if (
            self._positions_cache_fetched_at > 0
            and (now - self._positions_cache_fetched_at) < self._positions_cache_ttl_seconds
        ):
            return list(self._positions_cache)
        shared_positions = self._load_shared_snapshot(self._shared_positions_path)
        if shared_positions is not None:
            shared_payload = shared_positions.get("payload") or {}
            if isinstance(shared_payload, dict):
                positions = self._positions_from_payload(shared_payload.get("positions") or [])
                if positions:
                    self._positions_cache = positions
                    self._positions_snapshot_reliable = bool(shared_payload.get("reliable", True))
                    self._last_positions_error = str(shared_payload.get("error") or "")
                    self._positions_cache_fetched_at = float(shared_positions.get("fetched_at") or now)
                    self._positions_retry_not_before = now
                    return list(self._positions_cache)
        if not self.is_connected():
            self._positions_snapshot_reliable = False
            self._last_positions_error = "Not connected"
            self._write_audit("get_positions", request, {"positions": []}, ok=False, error="Not connected")
            return list(self._positions_cache)

        # Inter-process lock: only one engine hits the broker at a time
        lock_result = self._try_lock_or_recheck_shared(now)
        if lock_result is None:
            return list(self._positions_cache)
        try:
            resp: lb.StockPositionsResponse = self._trade_ctx.stock_positions()
            positions = []
            quote_map: dict[str, float] = {}
            raw_positions = []
            for channel in resp.channels or []:
                for p in channel.positions or []:
                    raw_positions.append(p)

            if raw_positions and self._quote_ctx:
                try:
                    symbols = [_longbridge_symbol(p.symbol) for p in raw_positions if _longbridge_symbol(p.symbol)]
                    if symbols:
                        quote_resp = self._quote_ctx.quote(symbols=symbols)
                        quote_items = quote_resp if isinstance(quote_resp, (list, tuple)) else [quote_resp]
                        for item in quote_items:
                            symbol = _normalize_base_symbol(
                                _quote_field(item, "symbol", "code", "ticker", default="")
                            )
                            price = float(_quote_field(item, "last_done", "price", "last_price", default=0.0) or 0.0)
                            if symbol and price > 0:
                                quote_map[symbol] = price
                except Exception as e:
                    logger.warning(f"Quote enrichment for positions failed: {e}")

            for p in raw_positions:
                ticker = _normalize_base_symbol(p.symbol)
                quantity = int(float(getattr(p, "quantity", 0) or 0))
                avg_entry_price = float(getattr(p, "cost_price", 0.0) or 0.0)
                current_price = float(quote_map.get(ticker, 0.0) or 0.0)
                if current_price <= 0:
                    current_price = avg_entry_price
                market_value = round(quantity * current_price, 3)
                cost_value = quantity * avg_entry_price
                unrealized_pnl = round((current_price - avg_entry_price) * quantity, 3)
                unrealized_pnl_pct = round((unrealized_pnl / cost_value * 100.0), 3) if cost_value > 0 else 0.0
                positions.append(
                    Position(
                        ticker=ticker,
                        quantity=quantity,
                        avg_entry_price=avg_entry_price,
                        current_price=round(current_price, 3),
                        market_value=market_value,
                        unrealized_pnl=unrealized_pnl,
                        unrealized_pnl_pct=unrealized_pnl_pct,
                    )
                )
            self._positions_cache = positions
            self._positions_snapshot_reliable = True
            self._last_positions_error = ""
            self._positions_cache_fetched_at = now
            self._positions_retry_not_before = now
            self._write_shared_snapshot(
                self._shared_positions_path,
                {
                    "positions": [self._position_to_payload(position) for position in positions],
                    "reliable": True,
                    "error": "",
                },
            )
            self._write_audit("get_positions", request, {"positions": positions}, ok=True)
            return positions
        except Exception as e:
            logger.error(f"Get positions failed: {e}")
            self._last_positions_error = str(e)
            if _is_rate_limit_error(e) and self._can_reuse_positions_cache(now):
                logger.warning("Get positions rate limited; reusing cached positions snapshot")
                self._positions_snapshot_reliable = True
            else:
                self._positions_snapshot_reliable = False
            self._positions_retry_not_before = now + self._cache_retry_backoff_seconds
            if self._positions_cache:
                self._write_shared_snapshot(
                    self._shared_positions_path,
                    {
                        "positions": [self._position_to_payload(position) for position in self._positions_cache],
                        "reliable": self._positions_snapshot_reliable,
                        "error": self._last_positions_error,
                    },
                )
            self._write_audit("get_positions", request, {"error": str(e)}, ok=False, error=str(e))
            return list(self._positions_cache)
        finally:
            _release_broker_api_lock()

    def get_position_for_ticker(self, ticker: str) -> Optional[Position]:
        target = _normalize_base_symbol(ticker)
        for p in self.get_positions():
            if _normalize_base_symbol(p.ticker) == target:
                return p
        return None

    def get_account(self) -> AccountInfo:
        request = {}
        now = time.time()
        if now < self._account_retry_not_before:
            return self._account_cache
        if (
            self._account_cache_fetched_at > 0
            and (now - self._account_cache_fetched_at) < self._account_cache_ttl_seconds
        ):
            return self._account_cache
        shared_account = self._load_shared_snapshot(self._shared_account_path)
        if shared_account is not None:
            shared_payload = shared_account.get("payload") or {}
            if isinstance(shared_payload, dict):
                account = self._account_from_payload(shared_payload)
                self._account_cache = account
                self._account_snapshot_reliable = bool(shared_payload.get("reliable", True))
                self._last_account_error = str(shared_payload.get("error") or "")
                self._account_cache_fetched_at = float(shared_account.get("fetched_at") or now)
                self._account_retry_not_before = now
                if account.positions:
                    self._positions_cache = list(account.positions)
                    self._positions_cache_fetched_at = self._account_cache_fetched_at
                return self._account_cache
        if not self.is_connected():
            self._account_snapshot_reliable = False
            self._last_account_error = "Not connected"
            self._write_audit(
                "get_account",
                request,
                {"cash": 0, "equity": 0, "buying_power": 0},
                ok=False,
                error="Not connected",
            )
            return self._account_cache

        try:
            if not self._positions_cache:
                self.get_positions()
            bal = self._trade_ctx.account_balance()
            cash = float(_balance_field(bal, "total_cash", "cash", "cash_balance", "available_cash", default=0) or 0)
            equity = float(_balance_field(bal, "net_assets", "equity", "net_liquidation", "total_equity", default=0) or 0)
            bp = float(_balance_field(bal, "buy_power", "buying_power", "available_buying_power", default=0) or 0)

            account = AccountInfo(
                cash=round(cash, 2),
                equity=round(equity, 2),
                buying_power=round(bp, 2),
                positions=list(self._positions_cache),
            )
            self._account_cache = account
            self._account_snapshot_reliable = True
            self._last_account_error = ""
            self._account_cache_fetched_at = now
            self._account_retry_not_before = now
            self._write_shared_snapshot(
                self._shared_account_path,
                {
                    **self._account_to_payload(account),
                    "reliable": True,
                    "error": "",
                },
            )
            self._write_audit("get_account", request, account, ok=True)
            return self._account_cache
        except Exception as e:
            logger.error(f"Get account failed: {e}")
            self._last_account_error = str(e)
            if _is_rate_limit_error(e) and self._can_reuse_account_cache(now):
                logger.warning("Get account rate limited; reusing cached account snapshot")
                self._account_snapshot_reliable = True
            else:
                self._account_snapshot_reliable = False
            self._account_retry_not_before = now + self._cache_retry_backoff_seconds
            if self._account_cache_fetched_at > 0:
                self._write_shared_snapshot(
                    self._shared_account_path,
                    {
                        **self._account_to_payload(self._account_cache),
                        "reliable": self._account_snapshot_reliable,
                        "error": self._last_account_error,
                    },
                )
            self._write_audit("get_account", request, {"error": str(e)}, ok=False, error=str(e))
            return self._account_cache

    def get_realtime_quote(self, ticker: str):
        request = {"ticker": ticker}
        if not self.is_connected() or not self._quote_ctx:
            self._write_audit("get_realtime_quote", request, {"quote": None}, ok=False, error="Not connected")
            return None
        try:
            resp = self._quote_ctx.quote(symbols=[_longbridge_symbol(ticker)])
            self._write_audit("get_realtime_quote", request, {"quote": resp}, ok=True)
            return resp
        except Exception as e:
            logger.warning(f"Quote fetch failed: {e}")
            self._write_audit("get_realtime_quote", request, {"error": str(e)}, ok=False, error=str(e))
            return None
