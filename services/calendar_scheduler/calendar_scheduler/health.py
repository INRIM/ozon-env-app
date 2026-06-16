from __future__ import annotations

import time
from pathlib import Path


def write_heartbeat(path: str) -> None:
    """Aggiorna il file heartbeat con l'epoch corrente (healthcheck container)."""
    Path(path).write_text(str(int(time.time())), encoding="utf-8")


def is_healthy(path: str, max_age_seconds: int) -> bool:
    p = Path(path)
    if not p.exists():
        return False
    try:
        last = int(p.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return False
    return (time.time() - last) <= max_age_seconds
