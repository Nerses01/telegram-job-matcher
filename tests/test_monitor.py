from unittest.mock import AsyncMock

import database
import main


def _posts(ids):
    return [
        {"text": f"post {i}", "url": f"https://t.me/chan/{i}", "post_id": i}
        for i in ids
    ]


def _patch_pipeline(monkeypatch, posts, work_mode="unknown", matched_skills=None):
    """Wire up fetch/evaluate/send mocks and return them."""
    fetch = AsyncMock(return_value=posts)
    evaluate = AsyncMock(return_value={
        "match_percentage": 100,
        "reason": "great",
        "work_mode": work_mode,
        "matched_skills": matched_skills if matched_skills is not None else [],
    })
    send = AsyncMock()
    monkeypatch.setattr(main, "fetch_channel_posts", fetch)
    monkeypatch.setattr(main, "evaluate_job_match", evaluate)
    monkeypatch.setattr(main.bot, "send_message", send)
    return fetch, evaluate, send


async def _ready_user(uid=1, channel="chan", work_mode=None, must_haves=()):
    """Create a CV-bearing subscriber, optionally with filters configured."""
    await database.register_or_get_user(uid)
    await database.update_user_cv(uid, "Python developer")
    await database.add_subscription(uid, channel)
    if work_mode:
        await database.set_user_work_mode(uid, work_mode)
    if must_haves:
        await database.add_user_skills(uid, list(must_haves))
        for skill_id, _, _ in await database.get_user_skills(uid):
            await database.toggle_skill_must_have(uid, skill_id)
    return uid


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


# --- Work-mode hard gate -----------------------------------------------------

async def test_work_mode_mismatch_suppresses_alert(temp_db, monkeypatch):
    """A perfect 100% score must NOT alert if the work mode conflicts."""
    await _ready_user(work_mode="remote")
    fetch, _, send = _patch_pipeline(monkeypatch, _posts([100]), work_mode="office")
    await main.run_monitor_pass()  # bootstrap

    fetch.return_value = _posts([100, 101])
    await main.run_monitor_pass()

    send.assert_not_called()
    # The watermark still advances: the post WAS offered, just filtered out.
    assert await database.get_channel_state("chan") == (101, 1)


async def test_work_mode_match_alerts(temp_db, monkeypatch):
    await _ready_user(work_mode="remote")
    fetch, _, send = _patch_pipeline(monkeypatch, _posts([100]), work_mode="remote")
    await main.run_monitor_pass()

    fetch.return_value = _posts([100, 101])
    await main.run_monitor_pass()

    assert send.call_count == 1


async def test_unknown_work_mode_still_alerts(temp_db, monkeypatch):
    """Explicit requirement: an undetectable work mode must never suppress."""
    await _ready_user(work_mode="remote")
    fetch, _, send = _patch_pipeline(monkeypatch, _posts([100]), work_mode="unknown")
    await main.run_monitor_pass()

    fetch.return_value = _posts([100, 101])
    await main.run_monitor_pass()

    assert send.call_count == 1


async def test_hybrid_post_suppressed_for_remote_seeker(temp_db, monkeypatch):
    """Strict matrix: hybrid does not satisfy a remote-only preference."""
    await _ready_user(work_mode="remote")
    fetch, _, send = _patch_pipeline(monkeypatch, _posts([100]), work_mode="hybrid")
    await main.run_monitor_pass()

    fetch.return_value = _posts([100, 101])
    await main.run_monitor_pass()

    send.assert_not_called()


async def test_any_preference_allows_office(temp_db, monkeypatch):
    """Default 'any' preserves pre-filter behavior."""
    await _ready_user()  # defaults to "any"
    fetch, _, send = _patch_pipeline(monkeypatch, _posts([100]), work_mode="office")
    await main.run_monitor_pass()

    fetch.return_value = _posts([100, 101])
    await main.run_monitor_pass()

    assert send.call_count == 1


def test_work_mode_allowed_matrix():
    assert main._work_mode_allowed("remote", "remote")
    assert main._work_mode_allowed("remote", "unknown")
    assert not main._work_mode_allowed("remote", "office")
    assert not main._work_mode_allowed("remote", "hybrid")
    assert main._work_mode_allowed("any", "office")
    # An unrecognized preference fails open rather than muting the user.
    assert main._work_mode_allowed("nonsense", "office")


# --- Must-have skills gate ---------------------------------------------------

async def test_must_have_absent_suppresses(temp_db, monkeypatch):
    await _ready_user(must_haves=["Rust"])
    fetch, _, send = _patch_pipeline(monkeypatch, _posts([100]), matched_skills=[])
    await main.run_monitor_pass()

    # Post text is "post 101" — contains no "Rust".
    fetch.return_value = _posts([100, 101])
    await main.run_monitor_pass()

    send.assert_not_called()


async def test_ai_claim_alone_does_not_satisfy_must_have(temp_db, monkeypatch):
    """The model over-reports matches, so its claim must not defeat the gate.

    Here the AI insists "Rust" matched, but the posting never says it — the
    alert must still be suppressed.
    """
    await _ready_user(must_haves=["Rust"])
    fetch, _, send = _patch_pipeline(monkeypatch, _posts([100]), matched_skills=["Rust"])
    await main.run_monitor_pass()

    fetch.return_value = _posts([100, 101])  # text is "post 101" — no Rust
    await main.run_monitor_pass()

    send.assert_not_called()


async def test_must_have_found_in_post_text_alerts(temp_db, monkeypatch):
    """Regex fallback: the AI omitted the skill, but the post plainly says it."""
    await _ready_user(must_haves=["Rust"])
    fetch, _, send = _patch_pipeline(monkeypatch, _posts([100]), matched_skills=[])
    await main.run_monitor_pass()

    fetch.return_value = [
        {"text": "Hiring a Rust engineer", "url": "https://t.me/chan/101", "post_id": 101}
    ]
    await main.run_monitor_pass()

    assert send.call_count == 1


def test_must_have_satisfied_helper():
    # No must-haves configured -> always passes.
    assert main._must_have_satisfied((), "anything")
    # Present in the posting, case-insensitively.
    assert main._must_have_satisfied(("Rust",), "We need a Rust engineer")
    assert main._must_have_satisfied(("Go",), "We use Go here")
    # Absent -> suppressed.
    assert not main._must_have_satisfied(("Rust",), "Python only")
    # Must not match inside a longer word.
    assert not main._must_have_satisfied(("Go",), "We use Google here")
    assert not main._must_have_satisfied(("Java",), "We use JavaScript here")
    # OR across must-haves lets a user cover synonyms.
    assert main._must_have_satisfied(("JavaScript", "JS"), "Strong JS skills")


# --- Skill verification against the posting ---------------------------------

def test_matching_skills_only_returns_skills_present_in_post():
    """Regression: the model claimed Git/Linux/Jira for a post that named none.

    Real case — a C++ candidate was shown "Algorithmic Optimization, STL,
    CMake, Ninja, GDB, MSVC" as matches for a Java/Kotlin/React job.
    """
    skills = [
        "Python", "Algorithmic Optimization", "REST APIs", "STL",
        "CMake", "Ninja", "GDB", "MSVC", "Git", "Linux", "Jira", "CICD",
    ]
    post = (
        "Full Stack Software Engineer | Senior\n"
        "Skills: agile, ci/cd, java, kotlin, python, react, typescript, unit_testing"
    )
    matched = main._matching_skills(skills, post)

    assert "Python" in matched          # genuinely listed
    assert "CICD" in matched            # "ci/cd" joins to "cicd"
    for invented in ("STL", "CMake", "Ninja", "GDB", "MSVC", "Git", "Linux",
                     "Jira", "Algorithmic Optimization", "REST APIs"):
        assert invented not in matched, invented


def test_matching_skills_handles_separators_and_word_boundaries():
    assert main._matching_skills(["Node.js"], "We use Node JS daily") == ["Node.js"]
    assert main._matching_skills(["C++20"], "Modern C++20 codebase") == ["C++20"]
    assert main._matching_skills(["REST APIs"], "Build REST APIs") == ["REST APIs"]
    # Substring traps must not match.
    assert main._matching_skills(["Java"], "JavaScript developer") == []
    assert main._matching_skills(["OOP"], "an infinite loop") == []
    assert main._matching_skills(["Go"], "Google Cloud") == []


def test_matching_skills_empty_inputs():
    assert main._matching_skills([], "anything") == []
    assert main._matching_skills(["Python"], "") == []


async def test_alert_shows_only_verified_skills(temp_db, monkeypatch):
    """End-to-end: the alert must not repeat the model's invented matches."""
    uid = await _ready_user(work_mode="remote")
    await database.add_user_skills(uid, ["Python", "CMake", "Qt6"])

    fetch, _, send = _patch_pipeline(
        monkeypatch, _posts([100]), work_mode="remote",
        matched_skills=["Python", "CMake", "Qt6"],  # model over-claims
    )
    await main.run_monitor_pass()

    fetch.return_value = [{
        "text": "Backend role. Skills: python, django, postgres",
        "url": "https://t.me/chan/101", "post_id": 101,
    }]
    await main.run_monitor_pass()

    assert send.call_count == 1
    body = send.call_args[0][1]
    assert "Python" in body
    assert "CMake" not in body
    assert "Qt6" not in body


def test_format_alert_shows_work_mode_and_skills():
    alert = main._format_alert(
        90, "good", "https://t.me/c/1", "body",
        work_mode="remote", matched_skills=["Python", "Go"],
    )
    assert "Remote" in alert
    assert "Python, Go" in alert

    unspecified = main._format_alert(90, "good", "https://t.me/c/1", "body")
    assert "Not specified" in unspecified


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
