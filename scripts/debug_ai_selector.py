#!/usr/bin/env python3
"""Compatibility wrapper for the AI stock selection runner."""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.run_ai_stock_selection import main


if __name__ == "__main__":
    main()
