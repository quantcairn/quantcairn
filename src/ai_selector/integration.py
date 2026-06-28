from __future__ import annotations


class AISelector:
    def get_signals(self):
        return [
            {
                "ticker": "NVDA",
                "score": 90,
                "volatility": 0.03,
                "regime": "TREND",
            }
        ]
