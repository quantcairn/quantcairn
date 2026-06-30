"""Scoring package."""

__all__ = ["Scorer"]


def __getattr__(name: str):
    if name == "Scorer":
        from .scorer import Scorer

        return Scorer
    raise AttributeError(name)
