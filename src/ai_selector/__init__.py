"""AI selector package entry."""

__all__ = ["AIStrategySelector", "AISelector"]


def __getattr__(name: str):
    if name == "AIStrategySelector":
        from .selector import AIStrategySelector

        return AIStrategySelector
    if name == "AISelector":
        from .integration import AISelector

        return AISelector
    raise AttributeError(name)
