import hashlib
import logging
from dataclasses import dataclass

import aiosqlite

import config

logger = logging.getLogger(__name__)

# Module-level so tests can monkeypatch `database.DB_NAME` to a temp file.
DB_NAME = config.DB_NAME


@dataclass(frozen=True)
class Subscriber:
    """One CV-bearing subscriber of a channel, with everything the monitor
    needs to score and filter a post for them."""
    user_id: int
    cv_text: str
    match_threshold: int
    work_mode: str
    skills: tuple[str, ...]      # all curated skills (must-haves included)
    must_haves: tuple[str, ...]  # the subset the user marked as required


async def _ensure_column(db, table: str, column: str, ddl: str) -> bool:
    """ALTER TABLE ... ADD COLUMN if `column` is absent. Returns True if added.

    Idempotent, so it is safe on both fresh and already-migrated databases.
    `table`/`column`/`ddl` are internal constants, never user input — SQLite
    cannot parameterize DDL identifiers anyway.
    """
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        existing = {row[1] for row in await cursor.fetchall()}
    if column in existing:
        return False
    await db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    logger.info(f"Migrated {table}: added column {column}")
    return True


async def init_db():
    """Initializes the database, creates tables, and applies indexes/PRAGMAs."""
    async with aiosqlite.connect(DB_NAME) as db:
        # WAL persists in the DB file; set once. Improves concurrent read/write.
        await db.execute("PRAGMA journal_mode=WAL")

        # Users table: stores user state, CV text, and matching preferences
        await db.execute(f"""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                cv_text TEXT,
                match_threshold INTEGER DEFAULT {config.DEFAULT_THRESHOLD},
                work_mode_pref TEXT NOT NULL DEFAULT '{config.DEFAULT_WORK_MODE}'
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

        # Per-user curated skills. Auto-extracted from the CV on upload, then
        # editable via /skills. `must_have = 1` promotes a skill to a hard gate.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_skills (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER NOT NULL,
                skill     TEXT NOT NULL,
                must_have INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)

        # --- Migrations for databases created before these columns existed ---
        # CREATE TABLE IF NOT EXISTS will not add columns to an existing table,
        # so live databases need an explicit (idempotent) ALTER.
        try:
            await _ensure_column(
                db, "users", "work_mode_pref",
                f"work_mode_pref TEXT NOT NULL DEFAULT '{config.DEFAULT_WORK_MODE}'",
            )
        except aiosqlite.OperationalError as e:
            # Loud, but non-fatal: better to run degraded than to refuse to boot.
            logger.error(f"Schema migration failed: {e}")

        # Indexes for the non-PK columns the monitor queries on every pass.
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_subs_channel ON subscriptions(channel_username)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions(user_id)"
        )
        # COLLATE NOCASE lets us store real casing ("PostgreSQL") for display
        # while INSERT OR IGNORE still rejects a duplicate "postgresql".
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_skill_uniq "
            "ON user_skills(user_id, skill COLLATE NOCASE)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_skills_user ON user_skills(user_id)"
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
    """Returns a list of `Subscriber` records for a channel's CV-bearing users.

    Skills for all subscribers are fetched in one batched query rather than
    per-user, so this stays a fixed 2 queries regardless of subscriber count.
    """
    async with aiosqlite.connect(DB_NAME) as db:
        query = """
            SELECT u.user_id, u.cv_text, u.match_threshold, u.work_mode_pref
            FROM users u
            JOIN subscriptions s ON u.user_id = s.user_id
            WHERE s.channel_username = ? AND u.cv_text IS NOT NULL
        """
        async with db.execute(query, (channel_username,)) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            return []

        user_ids = [row[0] for row in rows]
        placeholders = ",".join("?" * len(user_ids))
        skills_by_user: dict[int, list[str]] = {}
        musts_by_user: dict[int, list[str]] = {}
        async with db.execute(
            f"SELECT user_id, skill, must_have FROM user_skills "
            f"WHERE user_id IN ({placeholders})",
            user_ids,
        ) as cursor:
            for uid, skill, must_have in await cursor.fetchall():
                skills_by_user.setdefault(uid, []).append(skill)
                if must_have:
                    musts_by_user.setdefault(uid, []).append(skill)

    return [
        Subscriber(
            user_id=uid,
            cv_text=cv_text,
            match_threshold=threshold,
            work_mode=work_mode or config.DEFAULT_WORK_MODE,
            skills=tuple(skills_by_user.get(uid, ())),
            must_haves=tuple(musts_by_user.get(uid, ())),
        )
        for uid, cv_text, threshold, work_mode in rows
    ]

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

# --- Work mode preference ---

async def get_user_work_mode(user_id: int) -> str:
    """Returns the user's work-mode preference, defaulting when absent."""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT work_mode_pref FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return (row[0] if row and row[0] else config.DEFAULT_WORK_MODE)


async def set_user_work_mode(user_id: int, mode: str):
    """Sets the user's work-mode preference. Caller validates against WORK_MODE_PREFS."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET work_mode_pref = ? WHERE user_id = ?", (mode, user_id)
        )
        await db.commit()

# --- Skills ---
# Every mutation below filters on `user_id` as well as the row id: callback_data
# carries a global user_skills.id, so the ownership guard is what stops a crafted
# callback from editing someone else's row.

async def get_user_skills(user_id: int) -> list:
    """Returns [(id, skill, must_have)], must-haves first then alphabetical."""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT id, skill, must_have FROM user_skills WHERE user_id = ? "
            "ORDER BY must_have DESC, skill COLLATE NOCASE",
            (user_id,),
        ) as cursor:
            return await cursor.fetchall()


async def count_user_skills(user_id: int) -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM user_skills WHERE user_id = ?", (user_id,)
        ) as cursor:
            return (await cursor.fetchone())[0]


async def add_user_skills(user_id: int, skills: list) -> int:
    """Adds skills, ignoring case-insensitive duplicates. Returns how many were
    actually inserted (a partial add when MAX_SKILLS would be exceeded)."""
    if not skills:
        return 0
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM user_skills WHERE user_id = ?", (user_id,)
        ) as cursor:
            current = (await cursor.fetchone())[0]

        room = max(0, config.MAX_SKILLS - current)
        if room == 0:
            return 0

        cursor = await db.executemany(
            "INSERT OR IGNORE INTO user_skills (user_id, skill) VALUES (?, ?)",
            [(user_id, s) for s in skills[:room]],
        )
        await db.commit()
        # rowcount reflects only rows actually inserted (duplicates are ignored).
        return max(0, cursor.rowcount)


async def toggle_skill_must_have(user_id: int, skill_id: int):
    """Flips the must-have flag. Returns the new value, or None if not found."""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT must_have FROM user_skills WHERE id = ? AND user_id = ?",
            (skill_id, user_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        new_value = 0 if row[0] else 1
        await db.execute(
            "UPDATE user_skills SET must_have = ? WHERE id = ? AND user_id = ?",
            (new_value, skill_id, user_id),
        )
        await db.commit()
        return new_value


async def delete_user_skill(user_id: int, skill_id: int) -> bool:
    """Deletes one skill the user owns. Returns True if a row was removed."""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "DELETE FROM user_skills WHERE id = ? AND user_id = ?", (skill_id, user_id)
        )
        await db.commit()
        return cursor.rowcount > 0


async def clear_user_skills(user_id: int) -> int:
    """Removes all of the user's skills. Returns how many were removed."""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "DELETE FROM user_skills WHERE user_id = ?", (user_id,)
        )
        await db.commit()
        return cursor.rowcount

# --- Stats ---

async def get_user_stats(user_id: int) -> dict:
    """Returns a summary dict for the /stats command."""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT cv_text, match_threshold, work_mode_pref FROM users WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            user_row = await cursor.fetchone()
        async with db.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE user_id = ?", (user_id,)
        ) as cursor:
            channel_count = (await cursor.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(must_have), 0) FROM user_skills WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            skill_count, must_have_count = await cursor.fetchone()

    cv_text, threshold, work_mode = (
        user_row or (None, config.DEFAULT_THRESHOLD, config.DEFAULT_WORK_MODE)
    )
    return {
        "channel_count": channel_count,
        "has_cv": bool(cv_text),
        "cv_length": len(cv_text) if cv_text else 0,
        "threshold": threshold,
        "work_mode": work_mode or config.DEFAULT_WORK_MODE,
        "skill_count": skill_count,
        "must_have_count": must_have_count,
    }
