import asyncio
import html
import logging
import re

import config
import database as db
from bot import bot, start_bot
from scraper import fetch_channel_posts
from ai_engine import evaluate_job_match

logger = logging.getLogger(__name__)


def _work_mode_allowed(pref: str, detected: str) -> bool:
    """Hard gate: may a post with `detected` work mode reach a user wanting `pref`?

    An unrecognized preference fails open (allows everything) so a bad value can
    never silently mute a user's alerts.
    """
    allowed = config.WORK_MODE_COMPATIBILITY.get(pref)
    if allowed is None:
        return True
    return detected in allowed


_TOKEN_RE = re.compile(r"[a-z0-9+#]+")


def _post_variants(post_text: str) -> tuple[set, set]:
    """Index a posting for skill lookup: (whole tokens, joinable runs).

    Adjacent tokens are also joined so a posting saying "ci/cd" or "Node.js"
    matches a skill stored as "CICD" or "Node.js". Matching is by equality
    against these variants rather than substring search, which is what stops
    "Java" from matching inside "JavaScript".
    """
    tokens = _TOKEN_RE.findall(post_text.casefold())
    token_set = set(tokens)
    variants = set(token_set)
    for size in (2, 3):
        for i in range(len(tokens) - size + 1):
            variants.add("".join(tokens[i:i + size]))
    return token_set, variants


def _skill_in_post(skill: str, token_set: set, variants: set) -> bool:
    """True if `skill` is genuinely named in the posting."""
    lowered = skill.casefold()
    compact = "".join(_TOKEN_RE.findall(lowered))
    if not compact:
        return False
    if compact in variants:
        return True
    # Multi-word skills ("REST APIs"): every meaningful word must appear.
    words = {w for w in _TOKEN_RE.findall(lowered) if len(w) >= 3}
    return bool(words) and words <= token_set


def _matching_skills(skills, post_text: str) -> list:
    """Which of the user's skills the posting actually names.

    Computed in code, not taken from the model: an 8B model asked which skills
    a posting requires will happily infer "a software job probably uses Git and
    Linux" and report skills the posting never mentions. That is fine for prose
    but not for something users read as fact — or for gating alerts.
    """
    if not skills:
        return []
    token_set, variants = _post_variants(post_text)
    return [s for s in skills if _skill_in_post(s, token_set, variants)]


def _must_have_satisfied(must_haves, post_text: str) -> bool:
    """True if at least one starred skill is actually named in the posting.

    Deliberately does NOT consult the model's `matched_skills`: because the
    model over-reports, trusting it here would let a hallucinated match defeat
    the gate entirely. Must-haves are OR-ed, so a user who wants to cover
    synonyms can star both "JavaScript" and "JS".
    """
    if not must_haves:
        return True
    return bool(_matching_skills(must_haves, post_text))


def _format_alert(
    pct: int,
    reason: str,
    post_url: str,
    post_text: str,
    work_mode: str = "unknown",
    matched_skills=(),
) -> str:
    """Build an HTML alert with all interpolated text escaped, so a job post
    containing stray markup can never break Telegram entity parsing."""
    safe_reason = html.escape(reason)
    preview = html.escape(post_text[:config.PREVIEW_CHARS])
    mode_label = config.WORK_MODE_LABELS.get(work_mode, config.WORK_MODE_LABELS["unknown"])

    skills_line = ""
    if matched_skills:
        shown = ", ".join(matched_skills[:config.MAX_MATCHED_SKILLS_IN_ALERT])
        skills_line = f"🛠 Matches your skills: {html.escape(shown)}\n"

    return (
        f"🎯 <b>New Match Found!</b> ({pct}%)\n\n"
        f"Reason: {safe_reason}\n\n"
        f"{mode_label}\n"
        f"{skills_line}\n"
        f'🔗 <a href="{html.escape(post_url)}">View Original Post</a>\n\n'
        f"--- Preview ---\n{preview}…"
    )


async def _process_post(channel: str, post: dict, subscribers: list) -> bool:
    """Score one post against every CV-bearing subscriber and alert on matches.

    Returns False, without alerting anyone or marking the post seen, if the AI
    could not score it for some subscriber (e.g. Groq quota spent), so the
    caller can retry it on a later pass. Every subscriber is scored before any
    alert is sent, so that retry can never double-alert. Other failures are
    isolated per subscriber so one bad send never drops the rest.
    """
    post_text = post["text"]
    scored = []
    for sub in subscribers:
        try:
            result = await evaluate_job_match(
                sub.cv_text, post_text,
                skills=sub.skills, must_haves=sub.must_haves,
            )
        except Exception as e:
            logger.error(f"Failed scoring post for user {sub.user_id} in {channel}: {e}")
            continue
        if result.get("failed"):
            return False
        scored.append((sub, result))

    for sub, result in scored:
        try:
            pct = result.get("match_percentage", 0)
            # .get with defaults keeps older/mocked results working unchanged.
            work_mode = result.get("work_mode", "unknown")
            # Verified against the posting text rather than taken from the
            # model, which over-reports matches. See _matching_skills.
            matched = _matching_skills(sub.skills, post_text)

            # Hard gates run before the threshold check purely so the logs name
            # the real reason a post was dropped.
            if not _work_mode_allowed(sub.work_mode, work_mode):
                logger.info(
                    f"User {sub.user_id}: suppressed — post is {work_mode}, "
                    f"user wants {sub.work_mode}"
                )
                continue

            if not _must_have_satisfied(sub.must_haves, post_text):
                logger.info(
                    f"User {sub.user_id}: suppressed — none of the must-have "
                    f"skills {list(sub.must_haves)} present"
                )
                continue

            if pct >= sub.match_threshold:
                await bot.send_message(
                    sub.user_id,
                    _format_alert(
                        pct, result.get("reason", ""), post["url"], post_text,
                        work_mode=work_mode, matched_skills=matched,
                    ),
                    parse_mode="HTML",
                )
            else:
                logger.info(
                    f"User {sub.user_id}: match {pct}% below threshold {sub.match_threshold}%"
                )
        except Exception as e:
            logger.error(f"Failed processing post for user {sub.user_id} in {channel}: {e}")
    await db.mark_post_seen(post_text)
    return True


async def process_channel(channel: str) -> None:
    """Run the dedup + scoring pipeline for a single channel (one pass)."""
    posts = await fetch_channel_posts(channel)
    if not posts:
        return

    posts.sort(key=lambda p: p["post_id"])
    max_id = posts[-1]["post_id"]
    last_id, bootstrapped = await db.get_channel_state(channel)

    # First ever sight of this channel: record the baseline, alert nothing.
    if not bootstrapped:
        await db.set_channel_watermark(channel, max_id, bootstrapped=1)
        logger.info(f"Bootstrapped {channel} at post_id {max_id} (quiet first pass)")
        return

    new_posts = [p for p in posts if p["post_id"] > last_id]
    if not new_posts:
        return

    # Fetch subscribers BEFORE committing newness so we don't burn posts.
    subscribers = await db.get_subscribed_users_with_cvs(channel)
    if not subscribers:
        # No CV-bearing subscriber yet: hold these posts (do NOT advance the
        # watermark) so they get scored once a CV is added.
        logger.info(f"{channel}: holding {len(new_posts)} new posts (no CV-bearing subscribers)")
        return

    done_id = last_id
    for post in new_posts:
        if not await db.has_seen_post(post["text"]):
            if not await _process_post(channel, post, subscribers):
                # AI unavailable: hold this post and everything after it for
                # the next pass instead of recording it as a 0% match.
                logger.warning(
                    f"{channel}: AI unavailable, holding "
                    f"{sum(p['post_id'] >= post['post_id'] for p in new_posts)} "
                    "posts for a later pass"
                )
                break
        done_id = post["post_id"]

    # Advance the watermark only past posts actually offered to subscribers.
    if done_id > last_id:
        await db.set_channel_watermark(channel, done_id, bootstrapped=1)


async def run_monitor_pass() -> None:
    """One full sweep over all tracked channels, isolating per-channel failures."""
    channels = await db.get_all_unique_channels()
    for channel in channels:
        logger.info(f"Checking channel: {channel}")
        try:
            await process_channel(channel)
        except Exception as e:
            logger.error(f"Error processing channel {channel}: {e}")


async def job_monitor_task():
    """Background loop: sweep, prune, sleep, repeat — never crashing the process."""
    logger.info("Starting Job Monitor background task...")
    while True:
        try:
            await run_monitor_pass()
            removed = await db.prune_old_posts()
            if removed:
                logger.info(f"Pruned {removed} old post hashes")
        except Exception as e:
            logger.error(f"Error in monitor loop: {e}")
        await asyncio.sleep(config.POLL_INTERVAL_SECONDS)


async def main():
    """Runs the bot and the monitor concurrently, shutting down cleanly."""
    config.setup_logging()
    # Migrate/create the schema BEFORE starting the monitor: otherwise the
    # monitor's first pass can query columns that do not exist yet.
    await db.init_db()
    monitor = asyncio.create_task(job_monitor_task())
    try:
        await start_bot()
    finally:
        monitor.cancel()
        try:
            await monitor
        except asyncio.CancelledError:
            pass
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
