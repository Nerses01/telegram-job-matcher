import aiosqlite
import hashlib

import config

# Module-level so tests can monkeypatch `database.DB_NAME` to a temp file.
DB_NAME = config.DB_NAME


async def init_db():
    """Initializes the database, creates tables, and applies indexes/PRAGMAs."""
    async with aiosqlite.connect(DB_NAME) as db:
        # WAL persists in the DB file; set once. Improves concurrent read/write.
        await db.execute("PRAGMA journal_mode=WAL")

        # Users table: stores user state, CV text, and matching preference
        await db.execute(f"""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                cv_text TEXT,
                match_threshold INTEGER DEFAULT {config.DEFAULT_THRESHOLD}
            )
        """)

        # Subscriptions table: links users to the channels they want to track
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                channel_username TEXT,
                UNIQUE(user_id, channel_username),
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)

        # Per-channel high-water mark: the newest post id seen, plus a flag for
        # whether the channel has been bootstrapped (first sight recorded). This
        # is the primary "newness" gate — see main.job_monitor_task.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channel_state (
                channel_username TEXT PRIMARY KEY,
                last_post_id     INTEGER NOT NULL DEFAULT 0,
                bootstrapped     INTEGER NOT NULL DEFAULT 0
            )
        """)

        # Processed posts table: exact-repost suppression (secondary to the
        # watermark). Pruned periodically via prune_old_posts().
        await db.execute("""
            CREATE TABLE IF NOT EXISTS processed_posts (
                post_hash TEXT PRIMARY KEY,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Indexes for the non-PK columns the monitor queries on every pass.
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_subs_channel ON subscriptions(channel_username)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions(user_id)"
        )
        await db.commit()

# --- User Management Operations ---

async def register_or_get_user(user_id: int):
    """Ensures a user exists in the database and returns their record."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
        )
        await db.commit()
        async with db.execute("SELECT cv_text, match_threshold FROM users WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone()

async def update_user_cv(user_id: int, cv_text: str):
    """Saves or updates the text extraction of a user's CV."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET cv_text = ? WHERE user_id = ?", (cv_text, user_id)
        )
        await db.commit()

async def update_user_threshold(user_id: int, threshold: int):
    """Updates the matching percentage limit for a specific user."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET match_threshold = ? WHERE user_id = ?", (threshold, user_id)
        )
        await db.commit()

# --- Subscription Operations ---

async def add_subscription(user_id: int, channel_username: str) -> bool:
    """Links a user to a channel. Returns True if added, False if already exists."""
    channel_clean = config.normalize_channel(channel_username)
    if not channel_clean:
        return False
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "INSERT INTO subscriptions (user_id, channel_username) VALUES (?, ?)",
                (user_id, channel_clean)
            )
            await db.commit()
            return True
    except aiosqlite.IntegrityError:
        return False

async def remove_subscription(user_id: int, channel_username: str):
    """Unregisters a channel subscription for a user."""
    channel_clean = config.normalize_channel(channel_username)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "DELETE FROM subscriptions WHERE user_id = ? AND channel_username = ?",
            (user_id, channel_clean)
        )
        await db.commit()

async def get_user_subscriptions(user_id: int) -> list:
    """Returns all channel usernames a specific user is tracking."""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT channel_username FROM subscriptions WHERE user_id = ?", (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

# --- Global Aggregation Operations (For Scraper Engine) ---

async def get_all_unique_channels() -> list:
    """Returns a list of all unique channels tracked by the system to minimize network calls."""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT DISTINCT channel_username FROM subscriptions") as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

async def get_subscribed_users_with_cvs(channel_username: str) -> list:
    """Returns a list of tuples (user_id, cv_text, match_threshold) for a channel."""
    async with aiosqlite.connect(DB_NAME) as db:
        query = """
            SELECT u.user_id, u.cv_text, u.match_threshold
            FROM users u
            JOIN subscriptions s ON u.user_id = s.user_id
            WHERE s.channel_username = ? AND u.cv_text IS NOT NULL
        """
        async with db.execute(query, (channel_username,)) as cursor:
            return await cursor.fetchall()

# --- Channel Watermark (primary newness gate) ---

async def get_channel_state(channel_username: str) -> tuple:
    """Returns (last_post_id, bootstrapped) for a channel, creating a zero row if absent."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR IGNORE INTO channel_state (channel_username) VALUES (?)",
            (channel_username,),
        )
        await db.commit()
        async with db.execute(
            "SELECT last_post_id, bootstrapped FROM channel_state WHERE channel_username = ?",
            (channel_username,),
        ) as cursor:
            row = await cursor.fetchone()
            return (row[0], row[1]) if row else (0, 0)

async def set_channel_watermark(channel_username: str, last_post_id: int, bootstrapped: int = 1):
    """Records the newest post id seen for a channel and marks it bootstrapped."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            INSERT INTO channel_state (channel_username, last_post_id, bootstrapped)
            VALUES (?, ?, ?)
            ON CONFLICT(channel_username) DO UPDATE SET
                last_post_id = excluded.last_post_id,
                bootstrapped = excluded.bootstrapped
            """,
            (channel_username, last_post_id, bootstrapped),
        )
        await db.commit()

# --- Post Deduplication Management ---

def calculate_post_hash(text: str) -> str:
    """Generates a stable unique hash for a job post text block."""
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

async def has_seen_post(text: str) -> bool:
    """Read-only check: has this exact post text been processed before?"""
    post_hash = calculate_post_hash(text)
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT 1 FROM processed_posts WHERE post_hash = ?", (post_hash,)
        ) as cursor:
            return await cursor.fetchone() is not None

async def mark_post_seen(text: str):
    """Records a post's hash so exact reposts are suppressed later."""
    post_hash = calculate_post_hash(text)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR IGNORE INTO processed_posts (post_hash) VALUES (?)", (post_hash,)
        )
        await db.commit()

async def prune_old_posts(max_age_days: int = config.PROCESSED_POST_RETENTION_DAYS) -> int:
    """Deletes processed-post hashes older than max_age_days. Returns rows removed."""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "DELETE FROM processed_posts WHERE processed_at < datetime('now', ?)",
            (f"-{max_age_days} days",),
        )
        await db.commit()
        return cursor.rowcount

# --- Stats ---

async def get_user_stats(user_id: int) -> dict:
    """Returns a summary dict for the /stats command."""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT cv_text, match_threshold FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            user_row = await cursor.fetchone()
        async with db.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE user_id = ?", (user_id,)
        ) as cursor:
            channel_count = (await cursor.fetchone())[0]

    cv_text, threshold = (user_row or (None, config.DEFAULT_THRESHOLD))
    return {
        "channel_count": channel_count,
        "has_cv": bool(cv_text),
        "cv_length": len(cv_text) if cv_text else 0,
        "threshold": threshold,
    }
