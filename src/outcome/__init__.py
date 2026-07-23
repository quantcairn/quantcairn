"""Learning outcome collection — append-only, idempotent, read-only.

Reads from PaperPortfolioState, runtime audit logs, and SelectionBundle,
never calls a broker, never triggers orders, never mutates trading state.
"""
