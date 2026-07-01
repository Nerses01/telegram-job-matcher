import asyncio
import logging

import httpx
from bs4 import BeautifulSoup

import config

logger = logging.getLogger(__name__)


async def _fetch_html(url: str) -> str | None:
    """GET the preview page with bounded exponential-backoff retries.

    Returns the HTML body on HTTP 200, or None on a non-200 or after all
    attempts are exhausted (transport errors / timeouts).
    """
    headers = {"User-Agent": config.USER_AGENT}
    last_error = None
    for attempt in range(config.RETRY_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
                response = await client.get(url, headers=headers, follow_redirects=True)
            if response.status_code == 200:
                return response.text
            # A non-200 (e.g. 404 for a bad channel) is terminal, not retryable.
            logger.error(f"Failed to fetch {url}. Status code: {response.status_code}")
            return None
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_error = e
            delay = config.RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning(
                f"Fetch attempt {attempt + 1}/{config.RETRY_ATTEMPTS} for {url} "
                f"failed ({e!r}); retrying in {delay:.1f}s"
            )
            await asyncio.sleep(delay)
    logger.error(f"Giving up on {url} after {config.RETRY_ATTEMPTS} attempts: {last_error!r}")
    return None


async def fetch_channel_posts(channel_username: str) -> list:
    """
    Asynchronously scrapes the public web history of a Telegram channel.
    Returns a list of dicts, each with 'text', 'url', and 'post_id' (int).
    """
    clean_username = config.normalize_channel(channel_username)
    url = f"https://t.me/s/{clean_username}"

    html = await _fetch_html(url)
    if html is None:
        return []

    try:
        soup = BeautifulSoup(html, "html.parser")
        posts = []

        # Each wrap holds both the message text and the post metadata.
        message_wraps = soup.find_all(class_="tgme_widget_message_wrap")

        for wrap in message_wraps:
            text_element = wrap.find(class_="tgme_widget_message_text")
            msg_element = wrap.find(class_="tgme_widget_message")

            if text_element and msg_element and msg_element.has_attr("data-post"):
                text = text_element.get_text(separator="\n").strip()
                # data-post looks like: "username/1234"
                post_ref = msg_element["data-post"]
                try:
                    post_id = int(post_ref.split("/")[-1])
                except (ValueError, IndexError):
                    logger.warning(f"Could not parse post id from {post_ref!r}; skipping")
                    continue

                posts.append({
                    "text": text,
                    "url": f"https://t.me/{post_ref}",
                    "post_id": post_id,
                })

        return posts

    except Exception as e:
        logger.error(f"Error parsing {clean_username}: {e}")
        return []


# --- Quick Isolated Test Hook ---
if __name__ == "__main__":
    import sys

    # Windows consoles default to cp1252, which can't encode emoji in post text.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    config.setup_logging()

    async def test():
        test_channel = "revacancy_global"
        print(f"Scraping public posts from: {test_channel}...")
        results = await fetch_channel_posts(test_channel)
        print(f"Successfully retrieved {len(results)} posts!")
        if results:
            newest = results[-1]
            print("\n--- Sample Post Preview ---")
            print(f"post_id={newest['post_id']} url={newest['url']}")
            print(newest["text"][:250] + "\n...")

    asyncio.run(test())
