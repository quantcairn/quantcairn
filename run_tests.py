"""Minimal test runner to execute project tests without pytest installed.

Usage:
    .venv/bin/python run_tests.py
"""
import importlib.util
import sys


def run_module(name):
    mod = importlib.import_module(name)
    return mod


def main():
    # Run our single test module
    try:
        m = run_module('tests.test_fetcher')
        print('tests.test_fetcher executed successfully')
    except Exception as e:
        print('Test run failed:', e)
        raise


if __name__ == '__main__':
    main()
