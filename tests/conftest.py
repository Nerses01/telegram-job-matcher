"""Shared test setup.

Dummy secrets are set BEFORE any project module is imported, so `config`'s
import-time validation passes without a real `.env` and without touching any
live Telegram/Groq credentials.
"""
import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_DUMMY_TOKEN")
os.environ.setdefault("GROQ_API_KEY", "gsk_test_dummy")

import pytest_asyncio  # noqa: E402

import database  # noqa: E402


@pytest_asyncio.fixture
async def temp_db(tmp_path, monkeypatch):
    """Point the DB layer at a throwaway SQLite file and initialize the schema."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(database, "DB_NAME", db_path)
    await database.init_db()
    return db_path
