"""AI Stock Selector package entry."""

__all__ = ["AIStrategySelector"]


def __getattr__(name: str):
    if name == "AIStrategySelector":
        from .selector import AIStrategySelector

        return AIStrategySelector
    raise AttributeError(name)
