from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable


class TTLCache:
    """Cache in memoria con TTL, una entry per risorsa.

    Un lock per chiave evita lo stampede: alla scadenza N richieste
    concorrenti sulla stessa select farebbero N fetch su People.
    """

    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._data: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _fresh(self, key: str) -> tuple[bool, Any]:
        entry = self._data.get(key)
        if entry is None:
            return False, None
        stored_at, value = entry
        if self._ttl <= 0 or (time.monotonic() - stored_at) >= self._ttl:
            return False, None
        return True, value

    async def get_or_set(
        self, key: str, factory: Callable[[], Awaitable[Any]]
    ) -> Any:
        hit, value = self._fresh(key)
        if hit:
            return value
        async with self._lock(key):
            # Ricontrollo dentro il lock: chi ha atteso trova gia' il dato.
            hit, value = self._fresh(key)
            if hit:
                return value
            value = await factory()
            self._data[key] = (time.monotonic(), value)
            return value

    def invalidate(self, key: str = "") -> None:
        if key:
            self._data.pop(key, None)
            return
        self._data.clear()
