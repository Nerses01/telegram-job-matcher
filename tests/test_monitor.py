from unittest.mock import AsyncMock

import database
import main


def _posts(ids):
    return [
        {"text": f"post {i}", "url": f"https://t.me/chan/{i}", "post_id": i}
        for i in ids
    ]


def _patch_pipeline(monkeypatch, posts):
    """Wire up fetch/evaluate/send mocks and return them."""
    fetch = AsyncMock(return_value=posts)
    evaluate = AsyncMock(return_value={"match_percentage": 100, "reason": "great"})
    send = AsyncMock()
    monkeypatch.setattr(main, "fetch_channel_posts", fetch)
    monkeypatch.setattr(main, "evaluate_job_match", evaluate)
    monkeypatch.setattr(main.bot, "send_message", send)
    return fetch, evaluate, send


async def test_first_pass_is_quiet(temp_db, monkeypatch):
    # A CV-bearing subscriber exists, yet the first sight of the channel must
    # not alert (skip-existing-posts behavior) and must set the watermark.
    uid = 1
    await database.register_or_get_user(uid)
    await database.update_user_cv(uid, "Python developer")
    await database.add_subscription(uid, "chan")

    _, _, send = _patch_pipeline(monkeypatch, _posts([100, 101, 102]))

    await main.run_monitor_pass()

    send.assert_not_called()
    assert await database.get_channel_state("chan") == (102, 1)


async def test_late_cv_gap_and_no_double_alert(temp_db, monkeypatch):
    uid = 1
    await database.register_or_get_user(uid)  # no CV yet
    await database.add_subscription(uid, "chan")

    fetch, _, send = _patch_pipeline(monkeypatch, _posts([100, 101, 102]))

    # Pass 1: bootstrap, quiet.
    await main.run_monitor_pass()
    send.assert_not_called()
    assert await database.get_channel_state("chan") == (102, 1)

    # New posts arrive; user still has no CV -> held, watermark NOT advanced.
    fetch.return_value = _posts([100, 101, 102, 103, 104])
    await main.run_monitor_pass()
    send.assert_not_called()
    assert await database.get_channel_state("chan") == (102, 1)

    # User uploads a CV -> the held posts (103, 104) are now scored & alerted.
    await database.update_user_cv(uid, "Python developer")
    await main.run_monitor_pass()
    assert send.call_count == 2
    assert await database.get_channel_state("chan") == (104, 1)

    # Idempotency: no newer posts -> no additional alerts.
    await main.run_monitor_pass()
    assert send.call_count == 2


async def test_below_threshold_no_alert(temp_db, monkeypatch):
    uid = 1
    await database.register_or_get_user(uid)
    await database.update_user_cv(uid, "cv")
    await database.update_user_threshold(uid, 80)
    await database.add_subscription(uid, "chan")

    # Bootstrap first.
    fetch, evaluate, send = _patch_pipeline(monkeypatch, _posts([100]))
    await main.run_monitor_pass()

    # A new post that scores below the user's threshold must not alert.
    fetch.return_value = _posts([100, 101])
    evaluate.return_value = {"match_percentage": 50, "reason": "meh"}
    await main.run_monitor_pass()
    send.assert_not_called()
    # Watermark still advances past the offered post.
    assert await database.get_channel_state("chan") == (101, 1)
