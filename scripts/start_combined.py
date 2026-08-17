#!/usr/bin/env python3
"""Run the combined Top3 dashboard as a foreground process."""
import os
import sys

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_DIR)

from src.config.local_env import load_local_ai_env, sanitize_paper_environment

from src.dashboard.combined import start_combined


def main():
    sanitize_paper_environment()
    load_local_ai_env()
    start_combined(8090)


if __name__ == "__main__":
    main()
