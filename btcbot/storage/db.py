"""SQLite connection helpers — mirrors the parent project pattern."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from ..config import CONFIG

_SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text()


async def init_db(path: Path | None = None) -> None:
    p = path or CONFIG.db_path
    async with aiosqlite.connect(p) as conn:
        await conn.executescript(_SCHEMA_SQL)
        cur = await conn.execute("PRAGMA table_info(trades)")
        columns = {row[1] for row in await cur.fetchall()}
        for name, declaration in (
            ("signal_fair_prob", "REAL"),
            ("signal_net_edge", "REAL"),
            ("fee_usd", "REAL NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                await conn.execute(f"ALTER TABLE trades ADD COLUMN {name} {declaration}")
        await conn.commit()


@asynccontextmanager
async def connect(path: Path | None = None) -> AsyncIterator[aiosqlite.Connection]:
    p = path or CONFIG.db_path
    async with aiosqlite.connect(p) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        yield conn
