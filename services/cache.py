"""Basit asenkron TTL önbelleği (aynı anahtar için eşzamanlı istekleri tekilleştirir)."""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any, Awaitable, Callable


class TTLCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _fresh(self, key: str) -> tuple[bool, Any]:
        hit = self._data.get(key)
        if hit and hit[0] > time.monotonic():
            return True, hit[1]
        return False, None

    async def get_or_set(self, key: str, ttl: float, factory: Callable[[], Awaitable[Any]]) -> Any:
        ok, value = self._fresh(key)
        if ok:
            return value
        async with self._locks[key]:
            ok, value = self._fresh(key)
            if ok:
                return value
            value = await factory()          # hata olursa önbelleğe yazılmaz
            self._data[key] = (time.monotonic() + ttl, value)
            return value

    def invalidate(self, prefix: str = "") -> None:
        for key in [k for k in self._data if k.startswith(prefix)]:
            self._data.pop(key, None)
