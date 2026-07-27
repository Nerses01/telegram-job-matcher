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


def _must_have_satisfied(must_haves, matched_skills, post_text: str) -> bool:
    """True if at least one must-have skill is present in the posting.

    Accepts either the AI's `matched_skills` or a literal word-boundary hit in
    the raw post text. The union is deliberate: a single hallucinated omission
    by an 8B model would otherwise silently delete a job the user demanded.
    """
    if not must_haves:
        return True

    matched_lower = {s.casefold() for s in matched_skills or ()}
    for skill in must_haves:
        if skill.casefold() in matched_lower:
            return True
        if re.search(rf"(?<!\w){re.escape(skill)}(?!\w)", post_text, re.IGNORECASE):
            return True
    return False


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


async def _process_post(channel: str, post: dict, subscribers: list) -> None:
    """Score one post against every CV-bearing subscriber and alert on matches.
    Failures are isolated per subscriber so one bad send never drops the rest."""
    post_text = post["text"]
    for sub in subscribers:
        try:
            result = await evaluate_job_match(
                sub.cv_text, post_text,
                skills=sub.skills, must_haves=sub.must_haves,
            )
            pct = result.get("match_percentage", 0)
            # .get with defaults keeps older/mocked results working unchanged.
            work_mode = result.get("work_mode", "unknown")
            matched = result.get("matched_skills", []) or []

            # Hard gates run before the threshold check purely so the logs name
            # the real reason a post was dropped.
            if not _work_mode_allowed(sub.work_mode, work_mode):
                logger.info(
                    f"User {sub.user_id}: suppressed — post is {work_mode}, "
                    f"user wants {sub.work_mode}"
                )
                continue

            if not _must_have_satisfied(sub.must_haves, matched, post_text):
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

    for post in new_posts:
        if await db.has_seen_post(post["text"]):
            continue
        await _process_post(channel, post, subscribers)

    # Advance the watermark only after offering posts to real subscribers.
    await db.set_channel_watermark(channel, max_id, bootstrapped=1)


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
