"""Read-only outcome collection helpers for paper portfolio state.

This module deliberately does not call a broker or mutate learning datasets.
It provides the paper portfolio snapshot linkage used by later outcome
collection stages.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from src.broker.paper_portfolio_state import read_paper_portfolio_state


class OutcomeCollector:
    """Read-only collector facade for current paper portfolio state."""

    def __init__(self, *, paper_state_path: str | Path | None = None):
        self.paper_state_path = paper_state_path

    def load_paper_portfolio_state(self) -> dict[str, Any] | None:
        return read_paper_portfolio_state(self.paper_state_path)


def load_paper_portfolio_snapshot(path: str | Path | None = None) -> dict[str, Any] | None:
    return read_paper_portfolio_state(path)
