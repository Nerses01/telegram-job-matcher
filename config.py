"""Central configuration: tunables, validated secrets, and shared helpers.

Importing this module loads `.env` and fails fast (RuntimeError) if a required
secret is missing, so the app never starts half-configured. Tests provide dummy
values via tests/conftest.py before importing project modules.
"""
import logging
import os

from dotenv import load_dotenv

load_dotenv()

# --- Secrets ---------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")


def _validate_secrets() -> None:
    missing = [
        name
        for name, value in (
            ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
            ("GROQ_API_KEY", GROQ_API_KEY),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Add them to your .env file (see README.md)."
        )


_validate_secrets()

# --- Storage ---------------------------------------------------------------
DB_NAME = os.getenv("DB_NAME", "job_matcher.db")
# Delete deduplication hashes older than this. Newness is gated by the
# per-channel watermark, so pruning old hashes only affects repost suppression.
PROCESSED_POST_RETENTION_DAYS = 30

# --- Monitor loop ----------------------------------------------------------
POLL_INTERVAL_SECONDS = 600  # 10 minutes between channel sweeps

# --- Matching thresholds ---------------------------------------------------
DEFAULT_THRESHOLD = 60
THRESHOLD_MIN = 15
THRESHOLD_MAX = 90
THRESHOLD_STEP = 5

# --- Scraper ---------------------------------------------------------------
HTTP_TIMEOUT = 10.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# --- Groq / AI -------------------------------------------------------------
GROQ_MODEL = "llama-3.1-8b-instant"
GROQ_TEMPERATURE = 0.1

# --- CV upload -------------------------------------------------------------
MAX_CV_BYTES = 512 * 1024  # 512 KiB is a generous ceiling for text/LaTeX CVs
ALLOWED_CV_EXTENSIONS = (".txt", ".tex", ".md")

# --- Work mode filter ------------------------------------------------------
DEFAULT_WORK_MODE = "any"  # preserves pre-filter behavior for existing users
WORK_MODE_PREFS = ("any", "remote", "office", "hybrid")      # user-selectable
WORK_MODES_DETECTED = ("remote", "office", "hybrid", "unknown")  # AI-returnable

# Hard gate: preference -> the detected modes allowed through. "unknown" is in
# every set on purpose — an undetectable work mode must still alert, so model
# uncertainty degrades into "notify anyway" rather than silently dropping a job.
# Strict by design: a hybrid post does NOT reach a remote-only seeker. Loosening
# this is a one-line edit here, which is why the matrix lives in config.
WORK_MODE_COMPATIBILITY = {
    "any": {"remote", "office", "hybrid", "unknown"},
    "remote": {"remote", "unknown"},
    "office": {"office", "unknown"},
    "hybrid": {"hybrid", "unknown"},
}

WORK_MODE_LABELS = {
    "any": "🌐 Any",
    "remote": "🏠 Remote",
    "office": "🏢 On-site",
    "hybrid": "🔀 Hybrid",
    "unknown": "❓ Not specified",
}

# --- Skills filter ---------------------------------------------------------
MAX_SKILLS = 40
MAX_SKILL_LENGTH = 40
SKILLS_PAGE_SIZE = 8              # skills per page in the /skills keyboard
MAX_MATCHED_SKILLS_IN_ALERT = 8
CV_CHARS_FOR_SKILL_EXTRACTION = 12000

# --- Alerts ----------------------------------------------------------------
PREVIEW_CHARS = 500  # how much of a matched post to include in the alert

# --- Networking retries ----------------------------------------------------
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 1.0  # seconds; grows exponentially per attempt


def setup_logging(level: int = logging.INFO) -> None:
    """Configure the root logger once, from the process entry point."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def normalize_channel(raw: str) -> str:
    """Reduce any channel reference to its bare lowercase username.

    Accepts ``@name``, ``name``, ``t.me/name``, ``https://t.me/s/name``,
    trailing slashes, and ``?query`` suffixes. This is the single source of
    truth for channel normalization used by the bot, DB, and scraper.
    """
    s = raw.strip()
    for marker in ("t.me/s/", "t.me/"):
        if marker in s:
            s = s.split(marker, 1)[1]
            break
    s = s.split("?")[0].split("/")[0]  # drop query string and any trailing path
    return s.replace("@", "").strip().lower()
