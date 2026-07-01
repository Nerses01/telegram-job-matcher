# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A Telegram bot that autonomously monitors public Telegram job channels, deduplicates posts, and uses Groq (LLaMA 3.1) to score each post against a user's CV. Users are alerted only when a match meets their configured percentage threshold. Async throughout (`asyncio`, `aiogram` v3, `aiosqlite`, `httpx`).

## Commands

```bash
pip install -r requirements.txt          # runtime deps
pip install -r requirements-dev.txt      # + pytest, pytest-asyncio, respx
```

- **Run the app** (bot + background monitor together): `python main.py`
- **Run the test suite:** `pytest` (config in `pytest.ini`, `asyncio_mode = auto`)
- **Run a single test:** `pytest tests/test_monitor.py::test_late_cv_gap_and_no_double_alert`
- **Scraper smoke test:** `python scraper.py` (hits a hardcoded live channel)
- **AI matcher smoke test:** `python ai_engine.py` (hardcoded sample CV/job; needs a real key)
- **Docker:** `docker build -t tg-job-matcher .` then `docker run --env-file .env -v tg_agent_data:/data tg-job-matcher`

Requires a `.env` file (gitignored) with `TELEGRAM_BOT_TOKEN` and `GROQ_API_KEY`. `config.py` validates both at import and raises `RuntimeError` if either is missing. The SQLite DB (`job_matcher.db`) is created automatically via `db.init_db()`.

## Architecture

`main.py` is the entry point. `main()` runs `start_bot()` (aiogram polling) and `job_monitor_task()` (a loop sleeping `POLL_INTERVAL_SECONDS` between passes) concurrently, and shuts both down cleanly on exit.

The monitor pass is the core pipeline and spans every module. It is split into testable units:
- `run_monitor_pass()` — sweeps all channels, isolating per-channel failures.
- `process_channel(channel)` — the dedup + scoring logic for one channel (see Dedup below).
- `_process_post(...)` — scores one post against each CV-bearing subscriber and sends alerts, isolating per-subscriber failures.

Module responsibilities:
- **`config.py`** — single source of truth for all tunables (poll interval, thresholds, HTTP timeout, Groq model, retry policy, CV size limits), validated secrets, `setup_logging()`, and `normalize_channel()` (the one channel-normalization helper, used by bot/db/scraper).
- **`bot.py`** — aiogram handlers, FSM (`waiting_for_cv`, `waiting_for_channel`), inline-keyboard flows. Exports `bot`, `dp`, `start_bot`. CV uploads are read into memory (`BytesIO`), size/extension-validated, never written to disk. `/add_channel` validates the channel is reachable before saving.
- **`database.py`** — all persistence via `aiosqlite` (per-call connections; `DB_NAME` is a module global so tests can point it at a temp file). Tables: `users`, `subscriptions` (indexed on both columns), `channel_state` (the dedup watermark), `processed_posts` (exact-repost suppression, pruned by `prune_old_posts`).
- **`scraper.py`** — login-free scraping of `https://t.me/s/<channel>` with `httpx` + BeautifulSoup, with bounded retry/backoff. Returns `[{"text", "url", "post_id"}]`.
- **`ai_engine.py`** — Groq `AsyncGroq` client, retry/backoff on transient errors. `_parse_result()` clamps `match_percentage` to `0..100` and coerces types. Returns `{"match_percentage": int, "reason": str}`; never raises.

## Key conventions & gotchas

- **Dedup is a per-channel watermark, not a global hash set.** `channel_state.last_post_id` is the newest `post_id` (from Telegram's `data-post` id) that has been offered to a CV-bearing subscriber. The rules in `process_channel`:
  - **First sight of a channel** (`bootstrapped = 0`): record the watermark at the current max id and **alert nothing** — this is the "skip existing posts on subscribe" behavior. The first pass after deploy is a one-time quiet pass per channel.
  - **No CV-bearing subscriber this pass:** hold the new posts (do **not** advance the watermark) so they get scored once a CV is added — this closes the "CV uploaded late" gap.
  - Only advance `last_post_id` after posts are offered to ≥1 CV-bearing subscriber.
  - `processed_posts` is now secondary — exact-repost suppression only, checked via `has_seen_post()` / `mark_post_seen()` (the old check-and-insert `is_post_new` was removed).
- **Inherent scraping limit:** posts that scroll out of the ~20-message public preview before any CV exists are unrecoverable (login-free scraping only).
- **Per-call DB connections:** every `database.py` function opens/closes its own `aiosqlite.connect(DB_NAME)`. No pool (deliberate — personal scale). `PRAGMA journal_mode=WAL` and indexes are set in `init_db()`.
- **Scraping is HTML-fragile:** depends on Telegram's public-preview CSS classes (`tgme_widget_message_wrap`, `tgme_widget_message_text`, the `data-post` attribute). If scraping silently returns `[]`, suspect a Telegram markup change first.
- **Alerts use HTML parse mode with `html.escape()`** on all interpolated post text/reason — raw scraped markup can otherwise break Telegram entity parsing and abort the pass.
- **The AI prompt assumes the CV is LaTeX** (`ai_engine.SYSTEM_PROMPT` tells the model to ignore LaTeX commands). Plain-text CVs still work.
- **Tests** monkeypatch `database.DB_NAME` to a temp file and mock `main.fetch_channel_posts` / `main.evaluate_job_match` / `main.bot.send_message`. `tests/conftest.py` sets dummy secrets before importing project modules so `config`'s import-time validation passes.
