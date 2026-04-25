"""Хранение пользовательских настроек и истории чатов в SQLite."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,
    provider       TEXT,
    model          TEXT,
    mode           TEXT DEFAULT 'agent',  -- 'agent' | 'chat'
    system_prompt  TEXT,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS api_keys (
    user_id     INTEGER NOT NULL,
    provider    TEXT NOT NULL,
    encrypted   TEXT NOT NULL,
    base_url    TEXT,
    updated_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, provider)
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    role        TEXT NOT NULL,           -- 'user' | 'assistant' | 'tool' | 'system'
    content     TEXT NOT NULL,           -- JSON-encoded payload
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages (user_id, id);
"""


@dataclass
class UserSettings:
    user_id: int
    provider: str | None
    model: str | None
    mode: str
    system_prompt: str | None


@dataclass
class ApiKey:
    provider: str
    encrypted: str
    base_url: str | None


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        return self._conn

    # ------------------------------------------------------------------ users

    async def ensure_user(self, user_id: int) -> UserSettings:
        async with self.conn.execute(
            "SELECT user_id, provider, model, mode, system_prompt FROM users WHERE user_id=?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            await self.conn.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
            await self.conn.commit()
            return UserSettings(user_id=user_id, provider=None, model=None, mode="agent", system_prompt=None)
        return UserSettings(
            user_id=row[0],
            provider=row[1],
            model=row[2],
            mode=row[3] or "agent",
            system_prompt=row[4],
        )

    async def update_user(
        self,
        user_id: int,
        *,
        provider: str | None = None,
        model: str | None = None,
        mode: str | None = None,
        system_prompt: str | None = None,
    ) -> None:
        await self.ensure_user(user_id)
        fields: list[str] = []
        values: list[Any] = []
        if provider is not None:
            fields.append("provider=?")
            values.append(provider)
        if model is not None:
            fields.append("model=?")
            values.append(model)
        if mode is not None:
            fields.append("mode=?")
            values.append(mode)
        if system_prompt is not None:
            fields.append("system_prompt=?")
            values.append(system_prompt)
        if not fields:
            return
        fields.append("updated_at=CURRENT_TIMESTAMP")
        values.append(user_id)
        await self.conn.execute(
            f"UPDATE users SET {', '.join(fields)} WHERE user_id=?",
            values,
        )
        await self.conn.commit()

    # --------------------------------------------------------------- api keys

    async def upsert_key(
        self, user_id: int, provider: str, encrypted: str, base_url: str | None
    ) -> None:
        await self.ensure_user(user_id)
        await self.conn.execute(
            """
            INSERT INTO api_keys (user_id, provider, encrypted, base_url)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, provider) DO UPDATE SET
                encrypted=excluded.encrypted,
                base_url=excluded.base_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (user_id, provider, encrypted, base_url),
        )
        await self.conn.commit()

    async def get_key(self, user_id: int, provider: str) -> ApiKey | None:
        async with self.conn.execute(
            "SELECT provider, encrypted, base_url FROM api_keys WHERE user_id=? AND provider=?",
            (user_id, provider),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return ApiKey(provider=row[0], encrypted=row[1], base_url=row[2])

    async def list_keys(self, user_id: int) -> list[ApiKey]:
        async with self.conn.execute(
            "SELECT provider, encrypted, base_url FROM api_keys WHERE user_id=? ORDER BY provider",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [ApiKey(provider=r[0], encrypted=r[1], base_url=r[2]) for r in rows]

    async def delete_key(self, user_id: int, provider: str) -> None:
        await self.conn.execute(
            "DELETE FROM api_keys WHERE user_id=? AND provider=?",
            (user_id, provider),
        )
        await self.conn.commit()

    # --------------------------------------------------------------- messages

    async def append_message(self, user_id: int, role: str, content: dict[str, Any]) -> None:
        await self.conn.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, json.dumps(content, ensure_ascii=False)),
        )
        await self.conn.commit()

    async def get_history(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for role, content in reversed(rows):
            payload = json.loads(content)
            payload.setdefault("role", role)
            out.append(payload)
        return out

    async def clear_history(self, user_id: int) -> None:
        await self.conn.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
        await self.conn.commit()
