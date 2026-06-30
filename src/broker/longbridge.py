import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urljoin

import requests

from .broker import BrokerInterface


TRUE_VALUES = {"1", "true", "yes", "y", "on"}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def _utc_now() -> str:
    return datetime.utcnow().isoformat(timespec="milliseconds") + "Z"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _redact_headers(headers: Dict[str, str]) -> Dict[str, str]:
    redacted = dict(headers)
    for key in list(redacted):
        if key.lower() in {"authorization", "x-api-key", "x-lb-signature"}:
            redacted[key] = "***REDACTED***"
    return redacted


class LongBridgeBroker(BrokerInterface):
    """LongBridge REST broker adapter with dry-run and audit logging.

    Environment:
      LONGBRIDGE_API_KEY       API key.
      LONGBRIDGE_API_SECRET    API secret used for request signing.
      LONGBRIDGE_ACCESS_TOKEN  Optional bearer token, if the REST gateway uses it.
      LONGBRIDGE_BASE_URL      Sandbox/prod REST base URL.
      DRY_RUN                  Defaults to true. Set false/0 only for real submission.

    Endpoint paths are intentionally configurable because LongBridge sandbox
    routes may differ by account/API generation:
      LONGBRIDGE_PLACE_ORDER_PATH
      LONGBRIDGE_CANCEL_ORDER_PATH  supports {order_id}
      LONGBRIDGE_ORDER_STATUS_PATH  supports {order_id}
      LONGBRIDGE_POSITIONS_PATH
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: Optional[str] = None,
        access_token: Optional[str] = None,
        dry_run: Optional[bool] = None,
        timeout_seconds: float = 15.0,
        log_dir: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ):
        self.api_key = api_key or os.getenv("LONGBRIDGE_API_KEY")
        self.api_secret = api_secret or os.getenv("LONGBRIDGE_API_SECRET")
        self.access_token = access_token or os.getenv("LONGBRIDGE_ACCESS_TOKEN")
        self.base_url = (base_url or os.getenv("LONGBRIDGE_BASE_URL") or "").rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        self.dry_run = _env_bool("DRY_RUN", True) if dry_run is None else dry_run

        root = _project_root()
        self.log_dir = Path(log_dir) if log_dir else root / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.place_order_path = os.getenv("LONGBRIDGE_PLACE_ORDER_PATH", "/v1/trade/order")
        self.cancel_order_path = os.getenv(
            "LONGBRIDGE_CANCEL_ORDER_PATH", "/v1/trade/order/{order_id}/cancel"
        )
        self.order_status_path = os.getenv(
            "LONGBRIDGE_ORDER_STATUS_PATH", "/v1/trade/order/{order_id}"
        )
        self.positions_path = os.getenv("LONGBRIDGE_POSITIONS_PATH", "/v1/asset/positions")

    def _has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret and self.base_url)

    def _audit_path(self) -> Path:
        return self.log_dir / f"trades-{datetime.now().strftime('%Y%m%d')}.jsonl"

    def _write_audit(self, record: Dict[str, Any]) -> None:
        record.setdefault("timestamp", _utc_now())
        path = self._audit_path()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def _url(self, path: str) -> str:
        if not self.base_url:
            return path
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def _signature(self, method: str, path: str, timestamp: str, nonce: str, body: str) -> str:
        payload = "\n".join([method.upper(), path, timestamp, nonce, body])
        digest = hmac.new(
            self.api_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return digest

    def _headers(self, method: str, path: str, body: str, trace_id: str) -> Dict[str, str]:
        timestamp = _utc_now()
        nonce = uuid.uuid4().hex
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Api-Key": self.api_key or "",
            "X-LB-Timestamp": timestamp,
            "X-LB-Nonce": nonce,
            "X-Trace-Id": trace_id,
        }
        if self.api_secret:
            headers["X-LB-Signature"] = self._signature(method, path, timestamp, nonce, body)
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        action: str,
    ) -> Dict[str, Any]:
        trace_id = str(uuid.uuid4())
        payload = payload or {}
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) if payload else ""

        request_snapshot = {
            "method": method.upper(),
            "url": self._url(path),
            "path": path,
            "json": payload,
            "headers": _redact_headers(self._headers(method, path, body, trace_id)),
        }

        if self.dry_run or not self._has_credentials():
            response = {
                "trace_id": trace_id,
                "status": "simulated",
                "dry_run": True,
                "action": action,
                "request": payload,
            }
            self._write_audit({
                "trace_id": trace_id,
                "action": action,
                "dry_run": True,
                "request": request_snapshot,
                "response": response,
            })
            return response

        headers = self._headers(method, path, body, trace_id)
        started = time.time()
        try:
            resp = self.session.request(
                method.upper(),
                self._url(path),
                json=payload if payload else None,
                headers=headers,
                timeout=self.timeout_seconds,
            )
            elapsed_ms = round((time.time() - started) * 1000, 2)
            try:
                response_body = resp.json()
            except ValueError:
                response_body = {"raw": resp.text}

            result = {
                "trace_id": trace_id,
                "status_code": resp.status_code,
                "ok": resp.ok,
                "body": response_body,
            }
            self._write_audit({
                "trace_id": trace_id,
                "action": action,
                "dry_run": False,
                "elapsed_ms": elapsed_ms,
                "request": request_snapshot,
                "response": result,
            })
            resp.raise_for_status()
            return result
        except Exception as exc:
            elapsed_ms = round((time.time() - started) * 1000, 2)
            error = {
                "trace_id": trace_id,
                "ok": False,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
            self._write_audit({
                "trace_id": trace_id,
                "action": action,
                "dry_run": False,
                "elapsed_ms": elapsed_ms,
                "request": request_snapshot,
                "response": error,
            })
            raise

    def place_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._normalize_order(order)
        result = self._request("POST", self.place_order_path, normalized, action="place_order")
        if result.get("dry_run"):
            result["order_id"] = f"dryrun-{result['trace_id'][:12]}"
            result["status"] = "simulated_submitted"
        return result

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        path = self.cancel_order_path.format(order_id=order_id)
        result = self._request("POST", path, {"order_id": order_id}, action="cancel_order")
        if result.get("dry_run"):
            result["order_id"] = order_id
            result["status"] = "simulated_cancelled"
        return result

    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        path = self.order_status_path.format(order_id=order_id)
        result = self._request("GET", path, None, action="get_order_status")
        if result.get("dry_run"):
            result["order_id"] = order_id
            result["status"] = "simulated_filled"
        return result

    def get_positions(self) -> Dict[str, Any]:
        result = self._request("GET", self.positions_path, None, action="get_positions")
        if result.get("dry_run"):
            result["positions"] = []
        return result

    def _normalize_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        symbol = order.get("symbol") or order.get("ticker")
        side = str(order.get("side", "")).lower()
        quantity = order.get("qty", order.get("quantity"))
        order_type = str(order.get("order_type", order.get("type", "market"))).lower()

        if not symbol:
            raise ValueError("order requires symbol or ticker")
        if side not in {"buy", "sell"}:
            raise ValueError("order side must be buy or sell")
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            raise ValueError("order quantity must be an integer")
        if quantity <= 0:
            raise ValueError("order quantity must be positive")
        if order_type not in {"market", "limit"}:
            raise ValueError("order_type must be market or limit")

        normalized: Dict[str, Any] = {
            "symbol": str(symbol).upper(),
            "side": side,
            "quantity": quantity,
            "order_type": order_type,
        }
        if order.get("price") is not None:
            normalized["price"] = float(order["price"])
        if order.get("time_in_force"):
            normalized["time_in_force"] = order["time_in_force"]
        else:
            normalized["time_in_force"] = "day"
        if order.get("client_order_id"):
            normalized["client_order_id"] = order["client_order_id"]
        else:
            normalized["client_order_id"] = f"soxs-{uuid.uuid4().hex[:16]}"
        if order.get("metadata"):
            normalized["metadata"] = order["metadata"]
        return normalized
