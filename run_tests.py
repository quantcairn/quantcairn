"""Minimal test runner to execute project tests without pytest installed.

Usage:
    .venv/bin/python run_tests.py
"""
from importlib import import_module
import inspect
from pathlib import Path


def main():
    tests_dir = Path(__file__).resolve().parent / "tests"
    test_files = sorted(
        path for path in tests_dir.glob("test_*.py") if path.name != "__init__.py"
    )
    for path in test_files:
        module_name = f"tests.{path.stem}"
        module = import_module(module_name)
        if hasattr(module, "run_test_direct"):
            module.run_test_direct()
            print(f"{module_name} executed successfully")
        else:
            ran_any = False
            for name in sorted(dir(module)):
                if not name.startswith("test_"):
                    continue
                fn = getattr(module, name)
                if not callable(fn):
                    continue
                sig = inspect.signature(fn)
                if any(
                    param.default is inspect._empty
                    and param.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                    for param in sig.parameters.values()
                ):
                    continue
                fn()
                ran_any = True
            if ran_any:
                print(f"{module_name} executed successfully")
            else:
                print(f"{module_name} skipped (no runnable tests)")


if __name__ == '__main__':
    main()
