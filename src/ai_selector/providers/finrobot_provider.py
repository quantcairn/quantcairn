from __future__ import annotations

import csv
import importlib.util
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..config import AISelectorRuntimeConfig, load_runtime_config


logger = logging.getLogger(__name__)


def _ticker_seed(ticker: str) -> int:
    return sum(ord(ch) for ch in str(ticker or "").upper())


def _clamp_score(value: Any, default: float = 50.0) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        score = float(default)
    return max(0.0, min(100.0, score))


class FinRobotProvider:
    def __init__(self, config: AISelectorRuntimeConfig | None = None) -> None:
        self.config = config or load_runtime_config()

    def analyze(self, tickers: list) -> dict:
        results: dict[str, dict[str, Any]] = {}
        started_at = time.monotonic()
        for ticker in [str(item or "").strip().upper() for item in tickers if str(item or "").strip()]:
            if (time.monotonic() - started_at) >= self._total_budget_seconds():
                results[ticker] = self._mock_result(
                    ticker,
                    reason="finrobot_budget_exhausted",
                    status="SKIPPED_BUDGET",
                    timed_out=False,
                    budget_exhausted=True,
                )
                continue
            try:
                if self._is_available():
                    results[ticker] = self._analyze_with_compatible_interface(ticker)
                else:
                    results[ticker] = self._mock_result(
                        ticker,
                        reason="finrobot_not_installed",
                        status="UNAVAILABLE",
                        timed_out=False,
                        budget_exhausted=False,
                    )
            except subprocess.TimeoutExpired as exc:
                logger.warning("FinRobot analyze timeout for %s: %s", ticker, exc)
                results[ticker] = self._mock_result(
                    ticker,
                    reason="finrobot_timeout",
                    status="TIMEOUT",
                    timed_out=True,
                    budget_exhausted=False,
                )
            except Exception as exc:
                logger.warning("FinRobot analyze fallback for %s: %s", ticker, exc)
                results[ticker] = self._mock_result(
                    ticker,
                    reason="finrobot_error",
                    status="MALFORMED_RESPONSE",
                    timed_out=False,
                    budget_exhausted=False,
                )
        return results

    def _is_available(self) -> bool:
        return (
            importlib.util.find_spec("finrobot") is not None
            or importlib.util.find_spec("finrobot_zh") is not None
            or importlib.util.find_spec("finrobot_equity") is not None
            or self._resolve_source_path() is not None
        )

    def _analyze_with_compatible_interface(self, ticker: str) -> dict[str, Any]:
        source_path = self._resolve_source_path()
        for module_name in ("finrobot", "finrobot_zh", "finrobot_equity"):
            spec = importlib.util.find_spec(module_name)
            if spec is None:
                continue
            module = __import__(module_name, fromlist=["*"])
            for attr in ("analyze_equity", "run_equity_research", "analyze"):
                fn = getattr(module, attr, None)
                if callable(fn):
                    payload = fn(ticker)
                    return self._normalize_result(ticker, payload)
        if source_path is not None:
            if not self._has_required_runtime():
                raise RuntimeError("finrobot_missing_runtime_credentials")
            return self._run_with_source_path(ticker, source_path)
        raise RuntimeError("no_compatible_finrobot_callable")

    def _has_required_runtime(self) -> bool:
        config_file = str(self.config.finrobot_config_file or "").strip()
        if config_file:
            return True
        return bool(str(os.environ.get("OPENAI_API_KEY", "")).strip())

    def _timeout_seconds(self) -> int:
        raw_value = str(os.environ.get("SOXS_FINROBOT_TIMEOUT_SECONDS", "45") or "45").strip()
        try:
            timeout = int(raw_value)
        except (TypeError, ValueError):
            timeout = 45
        return max(5, min(timeout, 300))

    def _total_budget_seconds(self) -> int:
        raw_value = str(os.environ.get("SOXS_FINROBOT_TOTAL_BUDGET_SECONDS", "20") or "20").strip()
        try:
            budget = int(raw_value)
        except (TypeError, ValueError):
            budget = 20
        return max(self._timeout_seconds(), min(budget, 300))

    def _resolve_source_path(self) -> Path | None:
        value = str(self.config.finrobot_path or "").strip()
        if not value:
            return None
        path = Path(value).expanduser()
        if not path.exists():
            return None
        if (path / "finrobot_equity").exists():
            return path
        return None

    def _run_with_source_path(self, ticker: str, source_path: Path) -> dict[str, Any]:
        config_file = str(self.config.finrobot_config_file or "").strip()
        if not config_file:
            raise RuntimeError("finrobot_config_missing")
        output_dir = Path(self.config.finrobot_output_dir or (source_path / "output"))
        analysis_dir = output_dir / ticker / "analysis"
        script_path = source_path / "finrobot_equity" / "core" / "src" / "generate_financial_analysis.py"
        if not analysis_dir.exists():
            cmd = [
                self.config.finrobot_python,
                str(script_path),
                "--company-ticker",
                ticker,
                "--company-name",
                ticker,
                "--config-file",
                config_file,
                "--generate-text-sections",
            ]
            env = os.environ.copy()
            env.setdefault("PYTHONIOENCODING", "utf-8")
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(source_path),
                env=env,
                timeout=self._timeout_seconds(),
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or "finrobot_source_run_failed")
        payload = {
            "analysis_dir": str(analysis_dir),
            "analysis_csv": str(analysis_dir / "financial_metrics_and_forecasts.csv"),
            "ratios_csv": str(analysis_dir / "ratios_raw_data.csv"),
            "valuation_overview_file": str(analysis_dir / "valuation_overview.txt"),
            "investment_overview_file": str(analysis_dir / "investment_overview.txt"),
            "risks_file": str(analysis_dir / "risks.txt"),
            "major_takeaways_file": str(analysis_dir / "major_takeaways.txt"),
        }
        return self._normalize_result(ticker, payload)

    def _normalize_result(self, ticker: str, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            if any(str(key).endswith("_csv") or str(key).endswith("_file") for key in payload.keys()):
                parsed = self._parse_research_artifacts(payload)
                fundamental = parsed["fundamental_score"]
                valuation = parsed["valuation_score"]
                earnings = parsed["earnings_score"]
                risk = parsed["risk_score"]
                confidence = parsed["confidence"]
                reason = parsed["reason"]
            else:
                fundamental = _clamp_score(
                    payload.get("fundamental_score")
                    or payload.get("fundamental")
                    or payload.get("quality_score"),
                    61.0,
                )
                valuation = _clamp_score(
                    payload.get("valuation_score")
                    or payload.get("valuation"),
                    59.0,
                )
                earnings = _clamp_score(
                    payload.get("earnings_score")
                    or payload.get("earnings")
                    or payload.get("filings_score"),
                    60.0,
                )
                risk = _clamp_score(
                    payload.get("risk_score")
                    or payload.get("risk")
                    or payload.get("risk_assessment_score"),
                    63.0,
                )
                confidence = float(payload.get("confidence") or 0.74)
                reason = str(
                    payload.get("reason")
                    or payload.get("summary")
                    or payload.get("investment_thesis")
                    or "FinRobot analysis completed"
                )
        else:
            text = str(payload or "")
            fundamental = self._score_from_text(text, positive=("balance sheet", "cash flow", "growth", "margin"))
            valuation = self._score_from_text(text, positive=("valuation", "discount", "upside"), negative=("overvalued", "expensive"))
            earnings = self._score_from_text(text, positive=("earnings", "guidance", "estimate beat"), negative=("miss", "downgrade"))
            risk = self._score_from_text(text, positive=("low risk", "strong moat", "resilient"), negative=("debt", "litigation", "volatile"))
            confidence = 0.70
            reason = text[:240] or "FinRobot text output"

        return {
            "ticker": ticker,
            "fundamental_score": fundamental,
            "valuation_score": valuation,
            "earnings_score": earnings,
            "risk_score": risk,
            "confidence": max(0.0, min(1.0, confidence)),
            "reason": reason,
            "source": "finrobot",
            "fallback": False,
            "mock_used": False,
            "timed_out": False,
            "budget_exhausted": False,
            "status": "COMPLETE",
            "raw": payload,
        }

    def _parse_research_artifacts(self, payload: dict[str, Any]) -> dict[str, Any]:
        text_chunks = []
        numeric_rows = []
        for key, value in payload.items():
            path = Path(str(value))
            if not path.exists():
                continue
            if path.suffix.lower() == ".csv":
                numeric_rows.extend(self._read_csv_rows(path))
            elif path.suffix.lower() in {".txt", ".md", ".html"}:
                try:
                    text_chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
                except Exception:
                    continue
        joined_text = "\n".join(text_chunks)
        fundamental = self._score_numeric_keywords(
            numeric_rows,
            positive=("revenue", "gross margin", "operating margin", "free cash flow", "roe", "roic"),
            negative=("decline", "negative cash flow", "impairment"),
            default=61.0,
        )
        valuation = self._score_numeric_keywords(
            numeric_rows,
            positive=("discount", "fair value", "upside", "dcf", "ebitda"),
            negative=("overvalued", "premium", "expensive"),
            default=59.0,
        )
        earnings = self._score_numeric_keywords(
            numeric_rows,
            positive=("eps", "earnings", "guidance", "forecast", "growth"),
            negative=("miss", "downgrade", "contraction"),
            default=60.0,
        )
        risk = self._score_from_text(
            joined_text,
            positive=("manageable risk", "strong balance sheet", "resilient", "liquidity"),
            negative=("risk", "debt", "uncertainty", "litigation", "downside"),
        )
        confidence = 0.76 if numeric_rows or joined_text else 0.60
        reason = joined_text[:240] or "FinRobot research artifacts parsed"
        return {
            "fundamental_score": fundamental,
            "valuation_score": valuation,
            "earnings_score": earnings,
            "risk_score": risk,
            "confidence": confidence,
            "reason": reason,
        }

    def _read_csv_rows(self, path: Path) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    rows.append({str(k or ""): str(v or "") for k, v in row.items()})
        except Exception:
            return []
        return rows

    def _score_numeric_keywords(
        self,
        rows: list[dict[str, str]],
        *,
        positive: tuple[str, ...],
        negative: tuple[str, ...],
        default: float,
    ) -> float:
        score = float(default)
        matched = 0
        for row in rows:
            haystack = " ".join([str(k) + " " + str(v) for k, v in row.items()]).lower()
            for token in positive:
                if token in haystack:
                    score += 3.5
                    matched += 1
            for token in negative:
                if token in haystack:
                    score -= 3.0
                    matched += 1
            for value in row.values():
                number = self._extract_number(value)
                if number is None:
                    continue
                if number > 0:
                    score += 0.2
                elif number < 0:
                    score -= 0.2
        if matched == 0:
            return _clamp_score(default, default)
        return _clamp_score(score, default)

    def _extract_number(self, value: Any) -> float | None:
        text = str(value or "").strip().replace(",", "").replace("%", "")
        if not text:
            return None
        try:
            return float(text)
        except (TypeError, ValueError):
            return None

    def _score_from_text(
        self,
        text: str,
        *,
        positive: tuple[str, ...] = (),
        negative: tuple[str, ...] = (),
    ) -> float:
        score = 59.0
        lowered = text.lower()
        for token in positive:
            if token in lowered:
                score += 6.0
        for token in negative:
            if token in lowered:
                score -= 5.0
        return _clamp_score(score, 59.0)

    def _mock_result(
        self,
        ticker: str,
        *,
        reason: str,
        status: str = "SKIPPED_BUDGET",
        timed_out: bool = False,
        budget_exhausted: bool = False,
    ) -> dict[str, Any]:
        return {
            "ticker": ticker,
            "fundamental_score": 50.0,
            "valuation_score": 50.0,
            "earnings_score": 50.0,
            "risk_score": 50.0,
            "confidence": 0.5,
            "reason": f"Fallback FinRobot mock for {ticker}: {reason}",
            "source": "finrobot_mock",
            "fallback": True,
            "mock_used": True,
            "timed_out": bool(timed_out),
            "budget_exhausted": bool(budget_exhausted),
            "status": status,
        }
