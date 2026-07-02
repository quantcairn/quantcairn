"""Minimal test runner to execute project tests without pytest installed.

Usage:
    .venv/bin/python run_tests.py
"""
from importlib import import_module
import inspect
import os
from pathlib import Path
import tempfile


def main():
    # Keep simulated order and startup events out of the production audit trail.
    audit_dir = tempfile.TemporaryDirectory(prefix="soxs-test-audit-")
    state_dir = tempfile.TemporaryDirectory(prefix="soxs-test-state-")
    previous_audit_dir = os.environ.get("SOXS_RUNTIME_AUDIT_DIR")
    previous_state_dir = os.environ.get("SOXS_STATE_DIR")
    os.environ["SOXS_RUNTIME_AUDIT_DIR"] = audit_dir.name
    os.environ["SOXS_STATE_DIR"] = state_dir.name
    tests_dir = Path(__file__).resolve().parent / "tests"
    try:
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
    finally:
        if previous_audit_dir is None:
            os.environ.pop("SOXS_RUNTIME_AUDIT_DIR", None)
        else:
            os.environ["SOXS_RUNTIME_AUDIT_DIR"] = previous_audit_dir
        if previous_state_dir is None:
            os.environ.pop("SOXS_STATE_DIR", None)
        else:
            os.environ["SOXS_STATE_DIR"] = previous_state_dir
        audit_dir.cleanup()
        state_dir.cleanup()


if __name__ == '__main__':
    main()
