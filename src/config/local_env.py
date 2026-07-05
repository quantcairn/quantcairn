from __future__ import annotations

import os
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[2]
LOCAL_AI_ENV_PATH = PROJECT_DIR / ".env.ai_selector.local"


def load_local_ai_env(path: Path | None = None) -> bool:
    target = path or LOCAL_AI_ENV_PATH
    if not target.exists():
        return False
    loaded = False
    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if not key or key in os.environ:
            continue
        os.environ[key] = value
        loaded = True
    return loaded
