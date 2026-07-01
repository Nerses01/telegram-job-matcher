import aiosqlite

import config
import database


async def test_user_roundtrip(temp_db):
    uid = 42
    assert await database.register_or_get_user(uid) == (None, config.DEFAULT_THRESHOLD)

    await database.update_user_cv(uid, "Python developer")
    await database.update_user_threshold(uid, 75)
    assert await database.register_or_get_user(uid) == ("Python developer", 75)


async def test_subscription_roundtrip(temp_db):
    uid = 1
    assert await database.add_subscription(uid, "@ChanOne") is True
    # Duplicate (normalizes to the same username) is rejected.
    assert await database.add_subscription(uid, "https://t.me/chanone") is False

    assert await database.get_user_subscriptions(uid) == ["chanone"]
    assert await database.get_all_unique_channels() == ["chanone"]

    await database.remove_subscription(uid, "chanone")
    assert await database.get_user_subscriptions(uid) == []


async def test_blank_channel_not_added(temp_db):
    assert await database.add_subscription(1, "   ") is False


async def test_subscribed_users_require_cv(temp_db):
    uid = 7
    await database.register_or_get_user(uid)
    await database.add_subscription(uid, "chan")
    # No CV yet -> excluded.
    assert await database.get_subscribed_users_with_cvs("chan") == []

    await database.update_user_cv(uid, "cv text")
    rows = await database.get_subscribed_users_with_cvs("chan")
    assert rows == [(uid, "cv text", config.DEFAULT_THRESHOLD)]


async def test_channel_watermark(temp_db):
    assert await database.get_channel_state("chan") == (0, 0)

    await database.set_channel_watermark("chan", 500, bootstrapped=1)
    assert await database.get_channel_state("chan") == (500, 1)

    await database.set_channel_watermark("chan", 600)
    assert await database.get_channel_state("chan") == (600, 1)


async def test_seen_posts(temp_db):
    assert await database.has_seen_post("hello") is False
    await database.mark_post_seen("hello")
    assert await database.has_seen_post("hello") is True
    # Idempotent — marking twice does not raise.
    await database.mark_post_seen("hello")


async def test_prune_old_posts(temp_db):
    await database.mark_post_seen("fresh post")
    async with aiosqlite.connect(temp_db) as db:
        await db.execute(
            "INSERT INTO processed_posts (post_hash, processed_at) "
            "VALUES (?, datetime('now', '-40 days'))",
            ("stale",),
        )
        await db.commit()

    removed = await database.prune_old_posts(30)
    assert removed == 1
    assert await database.has_seen_post("fresh post") is True


async def test_user_stats(temp_db):
    uid = 3
    await database.register_or_get_user(uid)
    await database.update_user_cv(uid, "abcdef")
    await database.add_subscription(uid, "a")
    await database.add_subscription(uid, "b")

    stats = await database.get_user_stats(uid)
    assert stats == {
        "channel_count": 2,
        "has_cv": True,
        "cv_length": 6,
        "threshold": config.DEFAULT_THRESHOLD,
    }
