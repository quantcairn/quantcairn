from __future__ import annotations

import hashlib
import inspect
import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from zoneinfo import ZoneInfo

import pandas as pd

from src.backtest.dataset_validation import validate_backtest_dataset
from src.config.runtime_values import get_runtime_env, has_longbridge_runtime_credentials


US_EASTERN = ZoneInfo("America/New_York")
UTC = timezone.utc
DEFAULT_PAGE_SIZE = 1000
DEFAULT_MAX_RETRIES = 3
DEFAULT_REQUEST_INTERVAL_SECONDS = 0.25
_TIMESTAMP_RE = re.compile(r'timestamp:\s*"(?P<timestamp>[^"]+)"')


class LongbridgeHistoryError(RuntimeError):
    pass


def _canonical_symbol(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    return raw


def _symbol_slug(symbol: str) -> str:
    return _canonical_symbol(symbol).replace(".", "_")


def _frequency_label(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"1d", "daily", "day"}:
        return "daily"
    if raw in {"5m", "15m", "30m", "1h"}:
        return raw
    raise LongbridgeHistoryError(f"Unsupported frequency: {value!r}")


def _frequency_for_api(value: str) -> str:
    label = _frequency_label(value)
    return "1d" if label == "daily" else label


def _frequency_step(value: str) -> timedelta:
    label = _frequency_label(value)
    mapping = {
        "5m": timedelta(minutes=5),
        "15m": timedelta(minutes=15),
        "30m": timedelta(minutes=30),
        "1h": timedelta(hours=1),
        "daily": timedelta(days=1),
    }
    return mapping[label]


def _latest_complete_trade_day(now: datetime | None = None) -> date:
    current = now.astimezone(US_EASTERN) if now else datetime.now(US_EASTERN)
    market_close = current.replace(hour=16, minute=0, second=0, microsecond=0)
    candidate = current.date()
    if current.weekday() >= 5 or current < market_close:
        candidate = (current - timedelta(days=1)).date()
    while candidate.weekday() >= 5:
        candidate = (datetime.combine(candidate, dt_time(0, 0), tzinfo=US_EASTERN) - timedelta(days=1)).date()
    return candidate


def _session_label(regular_only: bool) -> str:
    return "regular" if regular_only else "all"


def _is_weekday(timestamp: datetime) -> bool:
    return timestamp.weekday() < 5


def _session_bucket(timestamp_utc: datetime) -> str:
    local = timestamp_utc.astimezone(US_EASTERN)
    if not _is_weekday(local):
        return "weekend"
    minutes = local.hour * 60 + local.minute
    if minutes < 9 * 60 + 30:
        return "pre_market"
    if minutes < 16 * 60:
        return "regular"
    return "after_hours"


def _adjustment_label(value: Any) -> str:
    name = getattr(value, "name", None)
    if name:
        return str(name).lower()
    text = str(value or "").strip().lower()
    if "forward" in text:
        return "forward_adjust"
    if "no" in text and "adjust" in text:
        return "no_adjust"
    return text or "unknown"


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def _sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_iso_timestamp(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        return None
    return ts.astimezone(UTC)


def _parse_epoch_timestamp(raw: Any) -> datetime | None:
    try:
        numeric = float(str(raw).strip())
    except Exception:
        return None
    if numeric <= 0:
        return None
    seconds = numeric / 1000.0 if numeric >= 1_000_000_000_000 else numeric
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except Exception:
        return None


def _timestamp_from_repr(raw: Any) -> datetime | None:
    try:
        text = repr(raw)
    except Exception:
        return None
    match = _TIMESTAMP_RE.search(text)
    if not match:
        return None
    return _parse_iso_timestamp(match.group("timestamp"))


def _parse_timestamp(value: Any, *, raw: Any | None = None) -> datetime:
    candidates: list[tuple[str, datetime]] = []
    ambiguous_naive = False

    def _add_candidate(label: str, candidate: datetime | None) -> None:
        if candidate is not None:
            candidates.append((label, candidate.astimezone(UTC)))

    if isinstance(value, datetime):
        if value.tzinfo is None:
            ambiguous_naive = True
        else:
            _add_candidate("value_datetime", value)
    elif isinstance(value, (int, float)):
        _add_candidate("value_epoch", _parse_epoch_timestamp(value))
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped:
            parsed = _parse_iso_timestamp(stripped)
            if parsed is not None:
                _add_candidate("value_iso", parsed)
            else:
                epoch_candidate = _parse_epoch_timestamp(stripped)
                if epoch_candidate is not None:
                    _add_candidate("value_epoch", epoch_candidate)
                else:
                    ambiguous_naive = True
    elif value is not None:
        if hasattr(value, "timestamp"):
            attr = getattr(value, "timestamp")
            if isinstance(attr, datetime):
                if attr.tzinfo is None:
                    ambiguous_naive = True
                else:
                    _add_candidate("value_attr_datetime", attr)
            elif isinstance(attr, (int, float)):
                _add_candidate("value_attr_epoch", _parse_epoch_timestamp(attr))
            elif isinstance(attr, str):
                parsed = _parse_iso_timestamp(attr)
                if parsed is not None:
                    _add_candidate("value_attr_iso", parsed)
                else:
                    epoch_candidate = _parse_epoch_timestamp(attr)
                    if epoch_candidate is not None:
                        _add_candidate("value_attr_epoch", epoch_candidate)
                    else:
                        ambiguous_naive = True
        else:
            ambiguous_naive = True

    if raw is not None:
        repr_candidate = _timestamp_from_repr(raw)
        if repr_candidate is not None:
            _add_candidate("repr", repr_candidate)
        payload: dict[str, Any] | None = None
        if isinstance(raw, dict):
            payload = raw
        else:
            dunder = getattr(raw, "__dict__", None)
            if callable(dunder):
                try:
                    candidate_payload = dunder()
                    if isinstance(candidate_payload, dict):
                        payload = candidate_payload
                except Exception:
                    payload = None
        if payload is not None:
            for key in ("timestamp", "time", "datetime", "date", "t"):
                payload_value = payload.get(key)
                if payload_value is None:
                    continue
                if isinstance(payload_value, datetime):
                    if payload_value.tzinfo is None:
                        ambiguous_naive = True
                    else:
                        _add_candidate(f"payload_{key}", payload_value)
                elif isinstance(payload_value, (int, float)):
                    _add_candidate(f"payload_{key}_epoch", _parse_epoch_timestamp(payload_value))
                elif isinstance(payload_value, str):
                    parsed = _parse_iso_timestamp(payload_value)
                    if parsed is not None:
                        _add_candidate(f"payload_{key}_iso", parsed)
                    else:
                        epoch_candidate = _parse_epoch_timestamp(payload_value)
                        if epoch_candidate is not None:
                            _add_candidate(f"payload_{key}_epoch", epoch_candidate)

    if not candidates:
        if ambiguous_naive:
            raise LongbridgeHistoryError("ambiguous_naive_timestamp")
        raise LongbridgeHistoryError("unsupported_timestamp_type")

    unique = {candidate.isoformat() for _, candidate in candidates}
    if len(unique) > 1:
        raise LongbridgeHistoryError("inconsistent_timestamp_sources")

    return candidates[0][1].astimezone(UTC)


def _row_value(row: Any, *names: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        for name in names:
            if name in row and row[name] is not None:
                return row[name]
        return default
    for name in names:
        if hasattr(row, name):
            value = getattr(row, name)
            if value is not None:
                return value
    return default


def _resolve_enum(module: Any, enum_name: str, candidate_names: Iterable[str]) -> Any:
    enum_obj = module if not enum_name else getattr(module, enum_name, None)
    if enum_obj is None:
        return None
    for name in candidate_names:
        if hasattr(enum_obj, name):
            return getattr(enum_obj, name)
        upper = name.upper()
        if hasattr(enum_obj, upper):
            return getattr(enum_obj, upper)
        lower = name.lower()
        if hasattr(enum_obj, lower):
            return getattr(enum_obj, lower)
    return None


def _extract_records(response: Any) -> list[Any]:
    if response is None:
        return []
    if isinstance(response, list):
        return list(response)
    if isinstance(response, tuple):
        return list(response)
    if isinstance(response, dict):
        for key in ("candlesticks", "candles", "items", "data", "records", "result", "list"):
            value = response.get(key)
            if isinstance(value, (list, tuple)):
                return list(value)
        return [response]
    for attr in ("candlesticks", "candles", "items", "data", "records", "result", "list"):
        value = getattr(response, attr, None)
        if isinstance(value, (list, tuple)):
            return list(value)
    if hasattr(response, "__iter__") and not isinstance(response, (str, bytes)):
        try:
            return list(response)
        except Exception:
            pass
    return [response]


@dataclass(slots=True)
class DownloadArtifact:
    symbol: str
    frequency: str
    start: str
    end: str
    adjustment: str
    trade_session: str
    source: str
    rows: int
    path: str
    sha256: str
    validation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "frequency": self.frequency,
            "start": self.start,
            "end": self.end,
            "adjustment": self.adjustment,
            "trade_session": self.trade_session,
            "source": self.source,
            "rows": self.rows,
            "path": self.path,
            "sha256": self.sha256,
            "validation": self.validation,
        }


class LongbridgeHistoryDownloader:
    def __init__(
        self,
        *,
        output_dir: str | Path,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_retries: int = DEFAULT_MAX_RETRIES,
        request_interval_seconds: float = DEFAULT_REQUEST_INTERVAL_SECONDS,
        regular_session_only: bool = True,
        adjustment: str = "auto",
        session_label: str | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.page_size = int(page_size)
        self.max_retries = int(max_retries)
        self.request_interval_seconds = float(request_interval_seconds)
        self.regular_session_only = bool(regular_session_only)
        self.session_label = session_label or _session_label(self.regular_session_only)
        self.adjustment_mode = str(adjustment or "auto").strip().lower()
        self._sdk = None
        self._ctx = None
        self._adjustment_value = None
        self._trade_session_value = None

    def _load_sdk(self):
        if self._sdk is not None:
            return self._sdk
        try:
            import longbridge.openapi as lb  # type: ignore
        except Exception as exc:  # pragma: no cover - exercised in hidden env
            raise LongbridgeHistoryError(f"longbridge.openapi unavailable: {exc}") from exc
        self._sdk = lb
        return lb

    def _build_context(self):
        if self._ctx is not None:
            return self._ctx
        if not has_longbridge_runtime_credentials():
            raise LongbridgeHistoryError("longbridge credentials unavailable")
        lb = self._load_sdk()
        app_key = get_runtime_env("LONGBRIDGE_APP_KEY") or get_runtime_env("LONGBRIDGE_API_KEY") or ""
        app_secret = get_runtime_env("LONGBRIDGE_APP_SECRET") or get_runtime_env("LONGBRIDGE_API_SECRET") or ""
        access_token = get_runtime_env("LONGBRIDGE_ACCESS_TOKEN") or ""
        config = lb.Config.from_apikey(
            app_key,
            app_secret,
            access_token,
            http_url=get_runtime_env("LONGBRIDGE_HTTP_URL") or get_runtime_env("LONGBRIDGE_BASE_URL"),
            quote_ws_url=get_runtime_env("LONGBRIDGE_QUOTE_WS_URL"),
            trade_ws_url=get_runtime_env("LONGBRIDGE_TRADE_WS_URL"),
            log_path=get_runtime_env("LONGBRIDGE_LOG_PATH"),
        )
        self._ctx = lb.QuoteContext(config)
        return self._ctx

    def _resolve_adjustment(self) -> Any:
        if self._adjustment_value is not None:
            return self._adjustment_value
        lb = self._load_sdk()
        adjust_type = getattr(lb, "AdjustType", None)
        if adjust_type is None:
            self._adjustment_value = "no_adjust"
            return self._adjustment_value
        if self.adjustment_mode in {"auto", "forward_adjust", "forward"}:
            self._adjustment_value = _resolve_enum(adjust_type, "", ["ForwardAdjust", "Forward", "FORWARD_ADJUST"])
            if self._adjustment_value is None:
                self._adjustment_value = "forward_adjust"
            return self._adjustment_value
        if self.adjustment_mode in {"no_adjust", "noadjust", "none"}:
            self._adjustment_value = _resolve_enum(adjust_type, "", ["NoAdjust", "NO_ADJUST"])
            if self._adjustment_value is None:
                self._adjustment_value = "no_adjust"
            return self._adjustment_value
        self._adjustment_value = self.adjustment_mode
        return self._adjustment_value

    def _resolve_period(self, frequency: str) -> Any:
        lb = self._load_sdk()
        period = getattr(lb, "Period", None)
        if period is None:
            return _frequency_for_api(frequency)
        label = _frequency_label(frequency)
        candidate_names = {
            "5m": ["Min_5", "Min5", "MIN_5", "MIN5"],
            "15m": ["Min_15", "Min15", "MIN_15", "MIN15"],
            "30m": ["Min_30", "Min30", "MIN_30", "MIN30"],
            "1h": ["Min_60", "Min60", "MIN_60", "MIN60", "Hour", "Hourly"],
            "daily": ["Day", "Daily", "DAY"],
        }[label]
        value = _resolve_enum(period, "", candidate_names)
        if value is not None:
            return value
        return _frequency_for_api(frequency)

    def _resolve_trade_session(self) -> Any:
        if self._trade_session_value is not None:
            return self._trade_session_value
        lb = self._load_sdk()
        trade_sessions = getattr(lb, "TradeSessions", None)
        if trade_sessions is None:
            trade_sessions = getattr(lb, "TradeSession", None)
        if trade_sessions is None:
            self._trade_session_value = self.session_label
            return self._trade_session_value
        self._trade_session_value = _resolve_enum(trade_sessions, "", ["All", "ALL", "Regular", "Intraday"])
        if self._trade_session_value is None:
            self._trade_session_value = self.session_label
        return self._trade_session_value

    def _history_method(self, ctx: Any):
        for name in ("history_candlesticks_by_offset", "candlesticks_by_offset", "history_candlesticks", "candlesticks"):
            method = getattr(ctx, name, None)
            if callable(method):
                return name, method
        raise LongbridgeHistoryError("QuoteContext does not provide a historical candlestick method")

    def _history_uses_forward_time(self, method: Any) -> bool:
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            return False
        params = signature.parameters
        if "forward" in params and "time" in params:
            return True
        return "offset" not in params and "forward" in params

    def _call_history_method(
        self,
        method: Any,
        *,
        symbol: str,
        frequency: str,
        count: int,
        offset: int,
        end_time: datetime | None,
        forward: bool | None = None,
    ) -> Any:
        adjustment = self._resolve_adjustment()
        trade_session = self._resolve_trade_session()
        period_value = self._resolve_period(frequency)
        candidate_kwargs = [
            {
                "symbol": symbol,
                "period": period_value,
                "count": count,
                "offset": offset,
                "forward": forward,
                "adjust_type": adjustment,
                "trade_session": trade_session,
                "trade_sessions": trade_session,
                "session": trade_session,
                "time": end_time,
                "to": end_time,
                "end_time": end_time,
                "end": end_time,
            },
            {
                "symbol": symbol,
                "period": period_value,
                "count": count,
                "offset": offset,
                "forward": forward,
                "adjust_type": adjustment,
                "trade_session": trade_session,
                "trade_sessions": trade_session,
            },
            {
                "symbol": symbol,
                "period": period_value,
                "count": count,
                "offset": offset,
                "forward": forward,
                "adjust_type": adjustment,
                "session": trade_session,
            },
            {
                "symbol": symbol,
                "period": period_value,
                "count": count,
                "offset": offset,
                "forward": forward,
                "adjust_type": adjustment,
            },
            {
                "symbol": symbol,
                "period": period_value,
                "count": count,
                "offset": offset,
                "forward": forward,
            },
            {
                "symbol": symbol,
                "period": period_value,
                "count": count,
                "forward": forward,
                "time": end_time,
                "to": end_time,
                "adjust_type": adjustment,
                "trade_session": trade_session,
                "trade_sessions": trade_session,
            },
        ]
        last_error: Exception | None = None
        for args in (
            ((symbol, period_value, adjustment, False if forward is None else forward, count, end_time, trade_session) if trade_session is not None else (symbol, period_value, adjustment, False if forward is None else forward, count, end_time)),
            (symbol, period_value, adjustment, False if forward is None else forward, count, end_time),
            (symbol, period_value, count, offset),
            (symbol, period_value, count),
        ):
            try:
                return method(*args)
            except TypeError as exc:
                last_error = exc
                continue
        for kwargs in candidate_kwargs:
            filtered = self._filter_kwargs(method, kwargs)
            try:
                return method(**filtered)
            except TypeError as exc:
                last_error = exc
                continue
        raise LongbridgeHistoryError(f"historical query failed: {last_error}")

    def _filter_kwargs(self, method: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            return {k: v for k, v in kwargs.items() if v is not None}
        parameters = signature.parameters
        accepts_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values())
        if accepts_kwargs:
            return {k: v for k, v in kwargs.items() if v is not None}
        allowed = {
            name
            for name, param in parameters.items()
            if param.kind
            in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }
        }
        return {k: v for k, v in kwargs.items() if v is not None and k in allowed}

    def _filter_regular_session_rows(self, rows: list[dict[str, Any]], frequency: str) -> list[dict[str, Any]]:
        if not self.regular_session_only or _frequency_label(frequency) == "daily":
            return rows
        filtered: list[dict[str, Any]] = []
        for row in rows:
            ts = row.get("timestamp")
            if not isinstance(ts, datetime):
                continue
            if _session_bucket(ts) == "regular":
                filtered.append(row)
        return filtered

    def _normalise_records(self, response: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in _extract_records(response):
            row: dict[str, Any] = {}
            row["symbol"] = _canonical_symbol(_row_value(item, "symbol", "ticker"))
            row["timestamp"] = _parse_timestamp(_row_value(item, "timestamp", "time", "datetime", "date", "t"), raw=item)
            row["open"] = _row_value(item, "open", "o")
            row["high"] = _row_value(item, "high", "h")
            row["low"] = _row_value(item, "low", "l")
            row["close"] = _row_value(item, "close", "c")
            row["volume"] = _row_value(item, "volume", "v", default=0)
            row["turnover"] = _row_value(item, "turnover", "amount", "value", default=0)
            rows.append(row)
        return rows

    def _page_to_rows(
        self,
        *,
        ctx: Any,
        symbol: str,
        frequency: str,
        offset: int,
        end_time: datetime | None,
        forward: bool | None = None,
    ) -> list[dict[str, Any]]:
        name, method = self._history_method(ctx)
        last_error: Exception | None = None
        response = None
        for attempt in range(1, max(1, self.max_retries) + 1):
            try:
                response = self._call_history_method(
                    method,
                    symbol=symbol,
                    frequency=frequency,
                    count=self.page_size,
                    offset=offset,
                    end_time=end_time,
                    forward=forward,
                )
                last_error = None
                break
            except LongbridgeHistoryError as exc:
                last_error = exc
                if attempt >= max(1, self.max_retries):
                    raise
                if self.request_interval_seconds > 0:
                    time.sleep(self.request_interval_seconds)
        if last_error is not None and response is None:
            raise last_error
        rows = self._normalise_records(response)
        return rows

    def _dedupe_sort_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: dict[datetime, dict[str, Any]] = {}
        for row in rows:
            ts = row.get("timestamp")
            if not isinstance(ts, datetime):
                continue
            deduped[ts] = row
        ordered = [deduped[ts] for ts in sorted(deduped)]
        return ordered

    def _prepare_rows(
        self,
        *,
        symbol: str,
        frequency: str,
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        ctx = self._build_context()
        symbol = _canonical_symbol(symbol)
        frequency = _frequency_label(frequency)
        if frequency == "daily":
            end_time = datetime.combine(end_date + timedelta(days=1), dt_time(0, 0), tzinfo=US_EASTERN).astimezone(UTC)
        else:
            end_time = datetime.combine(end_date, dt_time(15, 59, 59), tzinfo=US_EASTERN).astimezone(UTC)
        step = _frequency_step(frequency)

        rows: list[dict[str, Any]] = []
        latest_seen: datetime | None = None
        attempts = 0
        name, method = self._history_method(ctx)
        forward_mode = self._history_uses_forward_time(method)
        anchor_time = end_time
        offset = 0
        while True:
            attempts += 1
            page = self._page_to_rows(
                ctx=ctx,
                symbol=symbol,
                frequency=_frequency_for_api(frequency),
                offset=offset,
                end_time=anchor_time,
                forward=False if forward_mode else None,
            )
            if not page:
                break
            rows.extend(page)
            page_timestamps = [row["timestamp"] for row in page if isinstance(row.get("timestamp"), datetime)]
            if page_timestamps:
                earliest = min(page_timestamps)
                latest_seen = max(page_timestamps) if latest_seen is None else max(latest_seen, max(page_timestamps))
                if earliest.astimezone(UTC).date() <= start_date:
                    break
                if forward_mode:
                    anchor_time = earliest - step
                else:
                    offset += len(page)
            if len(page) < self.page_size:
                break
            if attempts > 10_000:
                raise LongbridgeHistoryError("pagination runaway")
            if self.request_interval_seconds > 0:
                time.sleep(self.request_interval_seconds)
        filtered = []
        for row in rows:
            ts = row.get("timestamp")
            if not isinstance(ts, datetime):
                continue
            if ts.date() < start_date:
                continue
            if ts.date() > end_date:
                continue
            filtered.append(row)
        filtered = self._dedupe_sort_rows(filtered)
        filtered = self._filter_regular_session_rows(filtered, frequency=frequency)
        return filtered

    def _validate_rows(self, rows: list[dict[str, Any]], symbol: str, frequency: str) -> pd.DataFrame:
        if not rows:
            raise LongbridgeHistoryError(f"no rows returned for {symbol} {frequency}")
        frame = pd.DataFrame(rows)
        frame["symbol"] = symbol
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        frame["frequency"] = frequency
        frame["timezone"] = "UTC"
        frame["trade_session"] = self.session_label
        frame["source"] = "longbridge_quote_api"
        frame = frame.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
        frame = frame.sort_values("timestamp", ascending=True)
        frame = frame.drop_duplicates(subset=["timestamp"], keep="last")
        if frame.empty:
            raise LongbridgeHistoryError(f"validation removed all rows for {symbol} {frequency}")
        invalid_mask = (
            (frame["high"] < frame["low"])
            | (frame["open"] < frame["low"])
            | (frame["open"] > frame["high"])
            | (frame["close"] < frame["low"])
            | (frame["close"] > frame["high"])
        )
        invalid_count = int(invalid_mask.sum())
        if invalid_count:
            frame = frame.loc[~invalid_mask].copy()
        if frame.empty:
            raise LongbridgeHistoryError(f"validation removed all rows for {symbol} {frequency}")
        if frame["timestamp"].duplicated().any():
            raise LongbridgeHistoryError(f"duplicate timestamps detected for {symbol} {frequency}")
        if frame["timestamp"].dt.tz is None:
            frame["timestamp"] = frame["timestamp"].dt.tz_localize(UTC)
        else:
            frame["timestamp"] = frame["timestamp"].dt.tz_convert(UTC)
        frame.attrs["invalid_ohlc_count"] = invalid_count
        return frame

    def _file_name(self, symbol: str, frequency: str, start_date: date) -> str:
        symbol_part = _symbol_slug(symbol)
        frequency_label = _frequency_label(frequency)
        suffix = "" if self.adjustment_mode in {"auto", "forward_adjust", "forward"} else f"_{self.adjustment_mode}"
        return f"{symbol_part}_{frequency_label}_{start_date.strftime('%Y%m%d')}_latest{suffix}.csv"

    def _write_atomic_csv(self, frame: pd.DataFrame, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        frame.to_csv(tmp_path, index=False)
        tmp_path.replace(path)

    def _validate_pair(
        self,
        *,
        symbol_path: Path,
        symbol: str,
        benchmark_path: Path,
        benchmark: str,
        expected_frequency: str,
    ) -> dict[str, Any]:
        report = validate_backtest_dataset(
            symbol_csv=symbol_path,
            benchmark_csv=benchmark_path,
            symbol=symbol,
            benchmark=benchmark,
            expected_frequency=expected_frequency,
        )
        payload = report.to_dict()
        payload["eligible_for_backtest"] = bool(report.eligible_for_backtest)
        payload["frequency_match"] = bool(report.frequency_match)
        return payload

    def download_symbol_frequency(
        self,
        symbol: str,
        frequency: str,
        *,
        start_date: date,
        end_date: date,
    ) -> DownloadArtifact:
        rows = self._prepare_rows(symbol=symbol, frequency=frequency, start_date=start_date, end_date=end_date)
        frame = self._validate_rows(rows, symbol=_canonical_symbol(symbol), frequency=_frequency_label(frequency))
        output_path = self.output_dir / _file_name_for(symbol, frequency, start_date, self.adjustment_mode)
        self._write_atomic_csv(frame, output_path)
        sha256 = _sha256_of_file(output_path)
        artifact = DownloadArtifact(
            symbol=_canonical_symbol(symbol),
            frequency=_frequency_label(frequency),
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            adjustment=_adjustment_label(self._resolve_adjustment()),
            trade_session=self.session_label,
            source="longbridge_quote_api",
            rows=len(frame),
            path=str(output_path),
            sha256=sha256,
            validation={
                "invalid_ohlc_count": int(frame.attrs.get("invalid_ohlc_count", 0)),
                "filtered_rows": int(frame.attrs.get("invalid_ohlc_count", 0)),
            },
        )
        return artifact

    def download_many(
        self,
        symbols: Sequence[str],
        frequencies: Sequence[str],
        *,
        intraday_start: str | date,
        daily_start: str | date,
        validation_pairs: Sequence[tuple[str, str, str]] | None = None,
    ) -> dict[str, Any]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        end_date = _latest_complete_trade_day()
        intraday_start_date = intraday_start if isinstance(intraday_start, date) else datetime.fromisoformat(str(intraday_start)).date()
        daily_start_date = daily_start if isinstance(daily_start, date) else datetime.fromisoformat(str(daily_start)).date()
        artifacts: list[DownloadArtifact] = []
        for symbol in symbols:
            for frequency in frequencies:
                start_date = daily_start_date if _frequency_label(frequency) == "daily" else intraday_start_date
                artifact = self.download_symbol_frequency(symbol, frequency, start_date=start_date, end_date=end_date)
                artifacts.append(artifact)
        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "longbridge_quote_api",
            "session_label": self.session_label,
            "regular_session_only": self.regular_session_only,
            "adjustment": _adjustment_label(self._resolve_adjustment()),
            "end_date": end_date.isoformat(),
            "files": [artifact.to_dict() for artifact in artifacts],
        }
        (self.output_dir / "manifest.json").write_text(json.dumps(_jsonable(manifest), indent=2, ensure_ascii=False), encoding="utf-8")
        validation_report = self._build_validation_report(artifacts, validation_pairs or self.default_validation_pairs())
        (self.output_dir / "validation_report.json").write_text(
            json.dumps(_jsonable(validation_report), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return {"manifest": manifest, "validation_report": validation_report, "files": [artifact.to_dict() for artifact in artifacts]}

    def default_validation_pairs(self) -> list[tuple[str, str, str]]:
        return [
            ("SOXS.US", "SOXX.US", "15m"),
            ("SOXS.US", "SOXX.US", "daily"),
            ("SOXS.US", "SMH.US", "15m"),
            ("SOXS.US", "SMH.US", "daily"),
            ("AAPL.US", "QQQ.US", "15m"),
            ("AAPL.US", "QQQ.US", "daily"),
            ("AAPL.US", "SPY.US", "15m"),
            ("AAPL.US", "SPY.US", "daily"),
        ]

    def _build_validation_report(
        self,
        artifacts: list[DownloadArtifact],
        validation_pairs: Sequence[tuple[str, str, str]],
    ) -> dict[str, Any]:
        by_symbol_frequency: dict[tuple[str, str], Path] = {}
        for artifact in artifacts:
            by_symbol_frequency[(artifact.symbol, artifact.frequency)] = Path(artifact.path)
        validations: list[dict[str, Any]] = []
        for symbol, benchmark, frequency in validation_pairs:
            symbol_file = by_symbol_frequency.get((_canonical_symbol(symbol), _frequency_label(frequency)))
            benchmark_file = by_symbol_frequency.get((_canonical_symbol(benchmark), _frequency_label(frequency)))
            if symbol_file is None or benchmark_file is None:
                validations.append(
                    {
                        "symbol": _canonical_symbol(symbol),
                        "benchmark": _canonical_symbol(benchmark),
                        "frequency": _frequency_label(frequency),
                        "benchmark_status": "MISSING_DATA",
                        "frequency_match": False,
                        "overlap_ratio": 0.0,
                        "future_data_risk": False,
                        "duplicate_count": 0,
                        "invalid_ohlc_count": 0,
                        "missing_value_count": 0,
                        "eligible_for_backtest": False,
                    }
                )
                continue
            payload = self._validate_pair(
                symbol_path=symbol_file,
                symbol=_canonical_symbol(symbol),
                benchmark_path=benchmark_file,
                benchmark=_canonical_symbol(benchmark),
                expected_frequency=_frequency_label(frequency),
            )
            validations.append(
                {
                    "symbol": payload.get("symbol"),
                    "benchmark": payload.get("benchmark_symbol"),
                    "frequency": payload.get("expected_frequency"),
                    "benchmark_status": payload.get("benchmark_mapping_status"),
                    "frequency_match": payload.get("frequency_match"),
                    "overlap_ratio": payload.get("overlap_ratio"),
                    "future_data_risk": payload.get("future_data_risk"),
                    "duplicate_count": payload.get("duplicate_timestamp_count"),
                    "invalid_ohlc_count": payload.get("invalid_ohlc_count"),
                    "missing_value_count": payload.get("missing_value_count"),
                    "eligible_for_backtest": payload.get("eligible_for_backtest"),
                    "fail_closed_reason": payload.get("fail_closed_reason"),
                }
            )
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "longbridge_quote_api",
            "regular_session_only": self.regular_session_only,
            "adjustment": _adjustment_label(self._resolve_adjustment()),
            "validations": validations,
        }


def _file_name_for(symbol: str, frequency: str, start_date: date, adjustment_mode: str) -> str:
    symbol_part = _symbol_slug(symbol)
    frequency_label = _frequency_label(frequency)
    suffix = "" if adjustment_mode in {"auto", "forward_adjust", "forward"} else f"_{adjustment_mode}"
    return f"{symbol_part}_{frequency_label}_{start_date.strftime('%Y%m%d')}_latest{suffix}.csv"
