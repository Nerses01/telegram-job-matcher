"""Pure-function tests for the /skills keyboard.

Only the parts that need no aiogram plumbing or Telegram mocks: the callback
payloads and pagination arithmetic.
"""
import bot
import config


def _rows(n, skill="Python"):
    return [(i, f"{skill}{i}", 0) for i in range(1, n + 1)]


def _all_callbacks(markup):
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


def test_callback_data_within_telegram_limit():
    """Telegram caps callback_data at 64 bytes — row ids keep us far under it
    even when the skill name is at the maximum allowed length."""
    long_skill = "x" * config.MAX_SKILL_LENGTH
    rows = [(999999, long_skill, 1)]
    markup = bot._skills_keyboard(rows, page=0)

    for data in _all_callbacks(markup):
        assert len(data.encode("utf-8")) <= 64, data


def test_toggle_and_delete_use_row_ids():
    markup = bot._skills_keyboard([(42, "Python", 0)], page=0)
    callbacks = _all_callbacks(markup)
    assert "sk_t_42" in callbacks
    assert "sk_d_42" in callbacks


def test_must_have_is_starred():
    plain = bot._skills_keyboard([(1, "Python", 0)], page=0)
    starred = bot._skills_keyboard([(1, "Python", 1)], page=0)
    assert plain.inline_keyboard[0][0].text.startswith("☆")
    assert starred.inline_keyboard[0][0].text.startswith("⭐")


def test_no_pagination_for_a_single_page():
    markup = bot._skills_keyboard(_rows(config.SKILLS_PAGE_SIZE), page=0)
    assert not any(c.startswith("sk_p_") for c in _all_callbacks(markup))


def test_pagination_appears_and_clamps():
    rows = _rows(config.SKILLS_PAGE_SIZE * 3)

    first = _all_callbacks(bot._skills_keyboard(rows, page=0))
    assert "sk_p_1" in first
    assert "sk_nop" in first  # left arrow is inert on the first page

    # An out-of-range page clamps to the last page instead of erroring.
    clamped = bot._skills_keyboard(rows, page=99)
    assert "3/3" in [b.text for row in clamped.inline_keyboard for b in row]


def test_page_shows_only_its_slice():
    rows = _rows(config.SKILLS_PAGE_SIZE * 2)
    markup = bot._skills_keyboard(rows, page=1)
    toggles = [c for c in _all_callbacks(markup) if c.startswith("sk_t_")]
    assert len(toggles) == config.SKILLS_PAGE_SIZE
    # Page 2 starts after the first page's worth of rows.
    assert toggles[0] == f"sk_t_{config.SKILLS_PAGE_SIZE + 1}"


def test_empty_list_offers_extraction_only_with_cv():
    with_cv = _all_callbacks(bot._skills_keyboard([], page=0, has_cv=True))
    assert "sk_ext" in with_cv
    assert "sk_clr" not in with_cv

    without_cv = _all_callbacks(bot._skills_keyboard([], page=0, has_cv=False))
    assert "sk_ext" not in without_cv
    assert "sk_add" in without_cv


def test_populated_list_offers_clear():
    callbacks = _all_callbacks(bot._skills_keyboard([(1, "Python", 0)], page=0, has_cv=True))
    assert "sk_clr" in callbacks
    assert "sk_ext" not in callbacks
