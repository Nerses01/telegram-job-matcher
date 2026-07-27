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
    assert len(rows) == 1
    sub = rows[0]
    assert sub.user_id == uid
    assert sub.cv_text == "cv text"
    assert sub.match_threshold == config.DEFAULT_THRESHOLD
    assert sub.work_mode == config.DEFAULT_WORK_MODE
    assert sub.skills == ()
    assert sub.must_haves == ()


async def test_subscriber_carries_skills(temp_db):
    uid = 8
    await database.register_or_get_user(uid)
    await database.update_user_cv(uid, "cv")
    await database.add_subscription(uid, "chan")
    await database.add_user_skills(uid, ["Python", "Go"])
    skills = await database.get_user_skills(uid)
    await database.toggle_skill_must_have(uid, skills[0][0])

    sub = (await database.get_subscribed_users_with_cvs("chan"))[0]
    assert set(sub.skills) == {"Python", "Go"}
    assert len(sub.must_haves) == 1


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
        "work_mode": config.DEFAULT_WORK_MODE,
        "skill_count": 0,
        "must_have_count": 0,
    }


# --- Work mode ---------------------------------------------------------------

async def test_work_mode_roundtrip(temp_db):
    uid = 11
    await database.register_or_get_user(uid)
    assert await database.get_user_work_mode(uid) == config.DEFAULT_WORK_MODE

    await database.set_user_work_mode(uid, "remote")
    assert await database.get_user_work_mode(uid) == "remote"


async def test_work_mode_unknown_user_defaults(temp_db):
    assert await database.get_user_work_mode(999) == config.DEFAULT_WORK_MODE


# --- Skills CRUD -------------------------------------------------------------

async def test_add_and_list_skills(temp_db):
    uid = 12
    await database.register_or_get_user(uid)
    added = await database.add_user_skills(uid, ["Python", "Docker"])
    assert added == 2

    rows = await database.get_user_skills(uid)
    assert [r[1] for r in rows] == ["Docker", "Python"]  # alphabetical, none starred


async def test_skills_dedupe_case_insensitively(temp_db):
    uid = 13
    await database.register_or_get_user(uid)
    await database.add_user_skills(uid, ["Python"])
    added = await database.add_user_skills(uid, ["python", "PYTHON"])

    assert added == 0
    rows = await database.get_user_skills(uid)
    assert len(rows) == 1
    assert rows[0][1] == "Python"  # original casing preserved for display


async def test_toggle_must_have_and_ordering(temp_db):
    uid = 14
    await database.register_or_get_user(uid)
    await database.add_user_skills(uid, ["Python", "Docker"])
    rows = await database.get_user_skills(uid)
    docker_id = rows[0][0]

    assert await database.toggle_skill_must_have(uid, docker_id) == 1
    # Must-haves sort first.
    assert (await database.get_user_skills(uid))[0][1] == "Docker"
    assert await database.toggle_skill_must_have(uid, docker_id) == 0


async def test_toggle_missing_skill_returns_none(temp_db):
    await database.register_or_get_user(15)
    assert await database.toggle_skill_must_have(15, 9999) is None


async def test_delete_skill(temp_db):
    uid = 16
    await database.register_or_get_user(uid)
    await database.add_user_skills(uid, ["Python"])
    skill_id = (await database.get_user_skills(uid))[0][0]

    assert await database.delete_user_skill(uid, skill_id) is True
    assert await database.get_user_skills(uid) == []
    assert await database.delete_user_skill(uid, skill_id) is False


async def test_cannot_touch_another_users_skill(temp_db):
    owner, attacker = 17, 18
    await database.register_or_get_user(owner)
    await database.register_or_get_user(attacker)
    await database.add_user_skills(owner, ["Python"])
    skill_id = (await database.get_user_skills(owner))[0][0]

    # Ownership guard: callback_data carries a global row id, so a crafted
    # callback must not be able to edit or delete someone else's skill.
    assert await database.delete_user_skill(attacker, skill_id) is False
    assert await database.toggle_skill_must_have(attacker, skill_id) is None
    assert len(await database.get_user_skills(owner)) == 1


async def test_skills_respect_max_cap(temp_db):
    uid = 19
    await database.register_or_get_user(uid)
    added = await database.add_user_skills(uid, [f"skill{i}" for i in range(config.MAX_SKILLS + 10)])

    assert added == config.MAX_SKILLS
    assert await database.count_user_skills(uid) == config.MAX_SKILLS
    # Full list: further adds are rejected outright.
    assert await database.add_user_skills(uid, ["one-more"]) == 0


async def test_clear_skills(temp_db):
    uid = 20
    await database.register_or_get_user(uid)
    await database.add_user_skills(uid, ["Python", "Go"])
    assert await database.clear_user_skills(uid) == 2
    assert await database.get_user_skills(uid) == []
