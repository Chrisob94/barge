from __future__ import annotations

import json
import os
from pathlib import Path

MAX = 20


def state_file() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
    return Path(base).expanduser() / "barge" / "recent.json"


def load(path: Path | None = None) -> list[str]:
    path = path or state_file()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return [x for x in data if isinstance(x, str)][:MAX] if isinstance(data, list) else []


def remember(spec: str, path: Path | None = None) -> list[str]:
    path = path or state_file()
    items = [spec] + [x for x in load(path) if x != spec]
    items = items[:MAX]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(items, indent=1))
    except OSError:
        pass  # not worth crashing over
    return items
