"""Minimal test runner to execute project tests without pytest installed.

Usage:
    .venv/bin/python run_tests.py
"""
import importlib.util
import sys


def main():
    try:
        import tests.test_fetcher as tf
        if hasattr(tf, 'run_test_direct'):
            tf.run_test_direct()
            print('tests.test_fetcher executed successfully')
        else:
            raise RuntimeError('run_test_direct() not found in tests.test_fetcher')
    except Exception as e:
        print('Test run failed:', e)
        raise


if __name__ == '__main__':
    main()
