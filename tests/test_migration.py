"""Migration safety for databases created before the filter columns existed.

These tests deliberately do NOT use the `temp_db` fixture: that fixture already
runs the current `init_db()`, so it would never exercise the upgrade path. Each
test hand-builds the OLD schema first, then migrates it.
"""
import aiosqlite
import pytest_asyncio

import config
import database


async def _build_legacy_db(path: str):
    """Create the pre-filter schema and populate it with realistic data."""
    async with aiosqlite.connect(path) as db:
        await db.execute("""
            CREATE TABLE users (
                user_id INTEGER PRIMARY KEY,
                cv_text TEXT,
                match_threshold INTEGER DEFAULT 60
            )
        """)
        await db.execute("""
            CREATE TABLE subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                channel_username TEXT,
                UNIQUE(user_id, channel_username)
            )
        """)
        await db.execute("""
            CREATE TABLE channel_state (
                channel_username TEXT PRIMARY KEY,
                last_post_id     INTEGER NOT NULL DEFAULT 0,
                bootstrapped     INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE processed_posts (
                post_hash TEXT PRIMARY KEY,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "INSERT INTO users (user_id, cv_text, match_threshold) VALUES (?, ?, ?)",
            (718545627, "My real CV text", 65),
        )
        await db.execute(
            "INSERT INTO subscriptions (user_id, channel_username) VALUES (?, ?)",
            (718545627, "remotegeekjob"),
        )
        await db.execute(
            "INSERT INTO channel_state (channel_username, last_post_id, bootstrapped) "
            "VALUES (?, ?, ?)",
            ("remotegeekjob", 41004, 1),
        )
        await db.commit()


@pytest_asyncio.fixture
async def legacy_db(tmp_path, monkeypatch):
    path = str(tmp_path / "legacy.db")
    await _build_legacy_db(path)
    monkeypatch.setattr(database, "DB_NAME", path)
    return path


async def _columns(path: str, table: str) -> set:
    async with aiosqlite.connect(path) as db:
        async with db.execute(f"PRAGMA table_info({table})") as cursor:
            return {row[1] for row in await cursor.fetchall()}


async def test_migration_adds_column_and_preserves_data(legacy_db):
    assert "work_mode_pref" not in await _columns(legacy_db, "users")

    await database.init_db()

    assert "work_mode_pref" in await _columns(legacy_db, "users")

    # Existing rows are backfilled to the default, so behavior is unchanged.
    assert await database.get_user_work_mode(718545627) == config.DEFAULT_WORK_MODE

    # Nothing else was disturbed.
    assert await database.register_or_get_user(718545627) == ("My real CV text", 65)
    assert await database.get_user_subscriptions(718545627) == ["remotegeekjob"]
    assert await database.get_channel_state("remotegeekjob") == (41004, 1)


async def test_migration_is_idempotent(legacy_db):
    await database.init_db()
    await database.init_db()  # must not raise
    await database.init_db()

    assert await database.register_or_get_user(718545627) == ("My real CV text", 65)
    assert await database.get_channel_state("remotegeekjob") == (41004, 1)


async def test_migration_never_clobbers_existing_preference(legacy_db):
    await database.init_db()
    await database.set_user_work_mode(718545627, "remote")

    await database.init_db()  # re-running must not reset the user's choice

    assert await database.get_user_work_mode(718545627) == "remote"


async def test_migration_creates_skills_table(legacy_db):
    await database.init_db()

    uid = 718545627
    assert await database.get_user_skills(uid) == []
    await database.add_user_skills(uid, ["Python"])
    assert len(await database.get_user_skills(uid)) == 1


async def test_fresh_db_has_column_without_migration(tmp_path, monkeypatch):
    """A brand-new DB gets the column from CREATE TABLE, not from the ALTER."""
    path = str(tmp_path / "fresh.db")
    monkeypatch.setattr(database, "DB_NAME", path)
    await database.init_db()

    assert "work_mode_pref" in await _columns(path, "users")
    await database.register_or_get_user(1)
    assert await database.get_user_work_mode(1) == config.DEFAULT_WORK_MODE
