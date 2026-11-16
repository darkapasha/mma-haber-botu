"""aiosqlite tabanlı kalıcı depolama: görülen haberler, gönderilen bildirimler, aboneler."""
from __future__ import annotations

import os
import time

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_news (
    id         TEXT PRIMARY KEY,
    title_key  TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seen_news_title ON seen_news(title_key);
CREATE TABLE IF NOT EXISTS notifications (
    key     TEXT PRIMARY KEY,
    sent_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id    INTEGER PRIMARY KEY,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS subscriptions (
    chat_id    INTEGER NOT NULL,
    sport      TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, sport)
);
"""
# Spor bazlı abonelikten önceki aboneler yalnızca MMA bildirimleri alıyordu. Bir kez çalışır;
# eski tablo silinmez (geri dönüş gerekirse diye), yalnızca kv'ye "taşındı" işareti konur.
_MIGRATE_SUBS = """
INSERT OR IGNORE INTO subscriptions (chat_id, sport, created_at)
    SELECT chat_id, 'mma', created_at FROM subscribers
    WHERE NOT EXISTS (SELECT 1 FROM kv WHERE key = 'subs_migrated');
INSERT OR IGNORE INTO kv (key, value) VALUES ('subs_migrated', '1');
"""


class Storage:
    def __init__(self, path: str) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage.open() çağrılmadı")
        return self._db

    async def open(self) -> None:
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(SCHEMA)
        await self._db.executescript(_MIGRATE_SUBS)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # --- haberler -------------------------------------------------------
    async def news_seen(self, news_id: str, title_key: str) -> bool:
        async with self.db.execute(
            "SELECT 1 FROM seen_news WHERE id = ? OR title_key = ? LIMIT 1", (news_id, title_key)
        ) as cur:
            return await cur.fetchone() is not None

    async def mark_news(self, items: list[tuple[str, str]]) -> None:
        now = int(time.time())
        await self.db.executemany(
            "INSERT OR IGNORE INTO seen_news (id, title_key, created_at) VALUES (?, ?, ?)",
            [(i, t, now) for i, t in items],
        )
        await self.db.commit()

    # --- bildirim kilidi (aynı bildirimin iki kez gitmesini engeller) ---
    async def claim(self, key: str) -> bool:
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO notifications (key, sent_at) VALUES (?, ?)", (key, int(time.time()))
        )
        await self.db.commit()
        return cur.rowcount == 1

    async def release(self, key: str) -> None:
        await self.db.execute("DELETE FROM notifications WHERE key = ?", (key,))
        await self.db.commit()

    # --- aboneler (spor bazlı: "mma", "f1") -------------------------------
    async def add_subscriber(self, chat_id: int, sport: str) -> bool:
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO subscriptions (chat_id, sport, created_at) VALUES (?, ?, ?)",
            (chat_id, sport, int(time.time())),
        )
        await self.db.commit()
        return cur.rowcount == 1

    async def remove_subscriber(self, chat_id: int, sport: str | None = None) -> bool:
        """`sport` verilmezse sohbetin tüm abonelikleri silinir (ör. bot engellendiğinde)."""
        if sport is None:
            cur = await self.db.execute("DELETE FROM subscriptions WHERE chat_id = ?", (chat_id,))
        else:
            cur = await self.db.execute("DELETE FROM subscriptions WHERE chat_id = ? AND sport = ?",
                                        (chat_id, sport))
        await self.db.commit()
        return cur.rowcount > 0

    async def subscribers(self, sport: str | None = None) -> list[int]:
        """`sport` verilmezse herhangi bir spora abone olan tüm sohbetler."""
        if sport is None:
            sql, args = "SELECT DISTINCT chat_id FROM subscriptions", ()
        else:
            sql, args = "SELECT chat_id FROM subscriptions WHERE sport = ?", (sport,)
        async with self.db.execute(sql, args) as cur:
            return [row[0] for row in await cur.fetchall()]

    async def subscriptions_of(self, chat_id: int) -> set[str]:
        async with self.db.execute("SELECT sport FROM subscriptions WHERE chat_id = ?", (chat_id,)) as cur:
            return {row[0] for row in await cur.fetchall()}

    # --- anahtar/değer ---------------------------------------------------
    async def get_kv(self, key: str) -> str | None:
        async with self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

    async def set_kv(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.db.commit()

    async def prune(self, days: int = 30) -> None:
        cutoff = int(time.time()) - days * 86400
        await self.db.execute("DELETE FROM seen_news WHERE created_at < ?", (cutoff,))
        await self.db.execute("DELETE FROM notifications WHERE sent_at < ?", (cutoff,))
        await self.db.commit()
