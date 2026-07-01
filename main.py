import asyncio
import html
import logging

import config
import database as db
from bot import bot, start_bot
from scraper import fetch_channel_posts
from ai_engine import evaluate_job_match

logger = logging.getLogger(__name__)


def _format_alert(pct: int, reason: str, post_url: str, post_text: str) -> str:
    """Build an HTML alert with all interpolated text escaped, so a job post
    containing stray markup can never break Telegram entity parsing."""
    safe_reason = html.escape(reason)
    preview = html.escape(post_text[:config.PREVIEW_CHARS])
    return (
        f"🎯 <b>New Match Found!</b> ({pct}%)\n\n"
        f"Reason: {safe_reason}\n\n"
        f'🔗 <a href="{html.escape(post_url)}">View Original Post</a>\n\n'
        f"--- Preview ---\n{preview}…"
    )


async def _process_post(channel: str, post: dict, subscribers: list) -> None:
    """Score one post against every CV-bearing subscriber and alert on matches.
    Failures are isolated per subscriber so one bad send never drops the rest."""
    post_text = post["text"]
    for user_id, cv_text, threshold in subscribers:
        try:
            result = await evaluate_job_match(cv_text, post_text)
            pct = result.get("match_percentage", 0)
            if pct >= threshold:
                await bot.send_message(
                    user_id,
                    _format_alert(pct, result.get("reason", ""), post["url"], post_text),
                    parse_mode="HTML",
                )
            else:
                logger.info(f"User {user_id}: match {pct}% below threshold {threshold}%")
        except Exception as e:
            logger.error(f"Failed processing post for user {user_id} in {channel}: {e}")
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
