import httpx
import respx
from httpx import Response

import scraper

HTML = """
<html><body>
  <div class="tgme_widget_message_wrap">
    <div class="tgme_widget_message" data-post="testchan/100">
      <div class="tgme_widget_message_text">First job post</div>
    </div>
  </div>
  <div class="tgme_widget_message_wrap">
    <div class="tgme_widget_message" data-post="testchan/101">
      <div class="tgme_widget_message_text">Second job post</div>
    </div>
  </div>
</body></html>
"""


@respx.mock
async def test_fetch_parses_posts():
    respx.get("https://t.me/s/testchan").mock(return_value=Response(200, text=HTML))

    posts = await scraper.fetch_channel_posts("@TestChan")

    assert [p["post_id"] for p in posts] == [100, 101]
    assert posts[0]["text"] == "First job post"
    assert posts[0]["url"] == "https://t.me/testchan/100"


@respx.mock
async def test_fetch_non_200_returns_empty():
    respx.get("https://t.me/s/ghost").mock(return_value=Response(404))
    assert await scraper.fetch_channel_posts("ghost") == []


@respx.mock
async def test_fetch_retries_then_gives_up(monkeypatch):
    # Avoid real backoff sleeps.
    async def _no_sleep(_):
        return None
    monkeypatch.setattr(scraper.asyncio, "sleep", _no_sleep)

    route = respx.get("https://t.me/s/flaky").mock(
        side_effect=httpx.TimeoutException("boom")
    )

    assert await scraper.fetch_channel_posts("flaky") == []
    assert route.call_count == scraper.config.RETRY_ATTEMPTS
