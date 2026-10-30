import asyncio
import json
import logging
import re
import time

from groq import (
    AsyncGroq,
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)

import config

logger = logging.getLogger(__name__)

# AsyncGroq picks up the key from the argument; config validated it at import.
client = AsyncGroq(api_key=config.GROQ_API_KEY)

# Exceptions worth retrying (transient); everything else fails fast to fallback.
_RETRYABLE = (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError)

SYSTEM_PROMPT = """You are an expert technical recruiter and ATS.
Your task is to compare a candidate's CV against a job posting.

CANDIDATE CV IS IN LATEX FORMAT.
- Ignore LaTeX formatting commands like \\section, \\textbf, \\item.
- Focus on extracting the underlying skills, project experience, and tech stack.
- Evaluate the match percentage based on the technical requirements of the job.

You must also classify the job's WORK MODE using these strict rules:
- "remote": the posting explicitly states remote / work from home / distributed / удалённо.
- "office": the posting explicitly requires working on-site at an office or location.
- "hybrid": the posting states a mix, e.g. "3 days per week in the office".
- "unknown": ANYTHING ELSE.

CRITICAL: Do NOT infer the work mode from the company's headquarters, a city
name, a relocation offer, a visa requirement, or a timezone requirement. If the
posting does not clearly state how the work is performed, you MUST answer
"unknown". When in doubt, always answer "unknown".

Score honestly. A posting built on a different technology stack than the
candidate's is a WEAK match even when both are software roles. Do not inflate
the score with generic overlap such as version control, Linux, or agile process.

If the candidate provides a list of key skills, also return which of those
skills the job posting EXPLICITLY names (or names by an obvious abbreviation).
Copy those entries verbatim from the candidate's list. Never include a skill
merely because a role like this usually involves it — if the posting does not
say it, leave it out. Return an empty list if none apply or no list was given.

Output your evaluation strictly in JSON format:
{
    "match_percentage": <int>,
    "reason": "<string>",
    "work_mode": "remote" | "office" | "hybrid" | "unknown",
    "matched_skills": ["<string>", ...]
}
"""

SKILL_EXTRACTION_PROMPT = """You extract a candidate's technical skills from their CV.

The CV may be in LaTeX; ignore formatting commands.

Rules:
- Return ONLY concrete, searchable skills: programming languages, frameworks,
  libraries, databases, tools, platforms, and technical domain specialisms.
- Do NOT return soft skills, job titles, company names, degrees, or sentences.
- Use canonical names ("PostgreSQL" not "postgres db", "JavaScript" not "JS").
- Each entry must be a short noun phrase, not a description.
- Return at most 40 entries, most important first.

Output strictly in JSON format:
{
    "skills": ["<string>", ...]
}
"""


# time.monotonic() before which Groq is known to be rate limited (e.g. the daily
# token quota is spent). Calls before then fail fast without touching the API.
_blocked_until = 0.0

_RETRY_IN_RE = re.compile(
    r"try again in (?:(\d+)h)?(?:(\d+)m(?!s))?(?:([\d.]+)s)?(?:([\d.]+)ms)?"
)


def _retry_after_seconds(error) -> float | None:
    """How long a RateLimitError asks us to wait, or None if it doesn't say.

    Prefers the `retry-after` header; falls back to Groq's message text
    ("Please try again in 7m12.5s").
    """
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            return float(headers.get("retry-after"))
        except (TypeError, ValueError):
            pass
    match = _RETRY_IN_RE.search(str(error))
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds, millis = (float(g) if g else 0.0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


async def _complete_json(system_prompt: str, user_prompt: str):
    """Run a JSON-mode completion with bounded exponential backoff.

    Returns (raw_json_text, error_message) — exactly one is not None.
    Never raises, so callers always have a defined fallback path.
    """
    global _blocked_until
    if time.monotonic() < _blocked_until:
        return None, "API Error: rate limited until quota resets."

    last_error = None
    for attempt in range(config.RETRY_ATTEMPTS):
        try:
            response = await client.chat.completions.create(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                model=config.GROQ_MODEL,
                temperature=config.GROQ_TEMPERATURE,
                response_format={"type": "json_object"},
            )
            return response.choices[0].message.content, None
        except _RETRYABLE as e:
            last_error = e
            delay = config.RETRY_BASE_DELAY * (2 ** attempt)
            if isinstance(e, RateLimitError):
                wait = _retry_after_seconds(e)
                if wait is not None and wait > config.RATE_LIMIT_MAX_WAIT_SECONDS:
                    # Retrying in seconds cannot outlast a quota that resets in
                    # minutes or hours; pause all AI calls until then instead.
                    _blocked_until = time.monotonic() + wait
                    logger.warning(
                        f"Groq rate limit needs a {wait:.0f}s wait; pausing AI "
                        f"calls until then ({e!r})"
                    )
                    return None, "API Error: rate limited until quota resets."
                if wait is not None:
                    delay = max(delay, wait)
            logger.warning(
                f"Groq attempt {attempt + 1}/{config.RETRY_ATTEMPTS} failed "
                f"({e!r}); retrying in {delay:.1f}s"
            )
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Groq API error: {e}")
            return None, f"API Error: {e}"

    logger.error(f"Groq gave up after {config.RETRY_ATTEMPTS} attempts: {last_error!r}")
    return None, "API Error: retries exhausted."


def _normalize_skills(raw) -> list:
    """Clean an arbitrary skill list into display-ready, deduped strings.

    Single definition of skill normalization, shared by CV extraction, AI result
    parsing, and manual entry via /skills. Dedupes case-insensitively while
    preserving the first-seen casing (so "PostgreSQL" survives, not "postgresql").
    """
    if not isinstance(raw, (list, tuple)):
        return []

    cleaned = []
    seen = set()
    for item in raw:
        if not isinstance(item, str):
            item = str(item)
        skill = " ".join(item.split()).strip(" ,;.")
        if not skill or len(skill) > config.MAX_SKILL_LENGTH:
            continue
        key = skill.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(skill)
        if len(cleaned) >= config.MAX_SKILLS:
            break
    return cleaned


def _parse_result(result_text: str) -> dict:
    """Parse and sanitize the model's JSON into a clamped, typed result."""
    fallback = {
        "match_percentage": 0,
        "reason": "Failed to parse AI output.",
        "work_mode": "unknown",
        "matched_skills": [],
    }
    try:
        parsed = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        logger.error("Failed to parse JSON from Groq response.")
        return fallback
    if not isinstance(parsed, dict):
        return fallback

    try:
        pct = int(parsed.get("match_percentage", 0))
    except (TypeError, ValueError):
        pct = 0
    pct = max(0, min(100, pct))  # clamp to a sane 0..100 range

    reason = str(parsed.get("reason") or "").strip() or "No reason provided."

    # Anything unexpected collapses to "unknown", which passes the work-mode
    # gate — model uncertainty must never silently suppress a job.
    work_mode = str(parsed.get("work_mode") or "").strip().lower()
    if work_mode not in config.WORK_MODES_DETECTED:
        work_mode = "unknown"

    matched = _normalize_skills(parsed.get("matched_skills"))[
        : config.MAX_MATCHED_SKILLS_IN_ALERT
    ]

    return {
        "match_percentage": pct,
        "reason": reason,
        "work_mode": work_mode,
        "matched_skills": matched,
    }


async def evaluate_job_match(
    cv_text: str, job_post_text: str, skills=None, must_haves=None
) -> dict:
    """
    Sends the CV and Job Post to Groq to calculate a match percentage,
    classify the job's work mode, and report which curated skills it requires.

    Returns a dict with 'match_percentage' (int 0..100), 'reason' (str),
    'work_mode' (str) and 'matched_skills' (list).
    Never raises: if the API call fails, the 0-score fallback also carries
    'failed': True so callers can retry the post later, not treat it as scored.
    """
    # Only add the skills block when there is something to say, so users without
    # curated skills get a prompt byte-identical to the pre-filter behavior.
    skills_block = ""
    if skills:
        skills_block = "\nCANDIDATE'S CURATED KEY SKILLS: " + ", ".join(skills)
        if must_haves:
            skills_block += (
                "\nThe candidate requires at least one of: " + ", ".join(must_haves)
            )
        skills_block += (
            "\nWeight the score toward these skills; they are the candidate's own "
            "correction of what their CV emphasizes.\n"
        )

    prompt = (
        f"CANDIDATE CV:\n{cv_text}\n{skills_block}\n---\n\n"
        f"JOB POSTING:\n{job_post_text}\n\nEvaluate the match and return JSON."
    )

    raw, error = await _complete_json(SYSTEM_PROMPT, prompt)
    if error is not None:
        return {
            "match_percentage": 0,
            "reason": error,
            "work_mode": "unknown",
            "matched_skills": [],
            "failed": True,
        }
    return _parse_result(raw)


async def extract_skills_from_cv(cv_text: str) -> list:
    """Extract a normalized skill list from a CV. Returns [] on any failure, so
    a Groq outage can never block saving the CV itself."""
    if not cv_text or not cv_text.strip():
        return []

    prompt = (
        "CANDIDATE CV:\n"
        f"{cv_text[:config.CV_CHARS_FOR_SKILL_EXTRACTION]}\n\n"
        "Extract the skills and return JSON."
    )

    raw, error = await _complete_json(SKILL_EXTRACTION_PROMPT, prompt)
    if error is not None:
        logger.error(f"Skill extraction failed: {error}")
        return []

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.error("Skill extraction returned unparsable JSON.")
        return []

    if not isinstance(parsed, dict):
        return []
    return _normalize_skills(parsed.get("skills"))


# --- Quick Isolated Test Hook ---
if __name__ == "__main__":
    config.setup_logging()

    async def test():
        test_cv = "I am a Backend Developer using C++ and Qt."
        test_job = "Looking for a Mid-Level C++ Engineer with QML experience. Fully remote."

        print("Evaluating match via Groq...")
        result = await evaluate_job_match(test_cv, test_job, skills=["C++", "Qt"])
        print("Result:", result)

        print("\nExtracting skills from CV...")
        print("Skills:", await extract_skills_from_cv(test_cv))

    asyncio.run(test())
