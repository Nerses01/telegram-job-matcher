import asyncio
import json
import logging

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

Output your evaluation strictly in JSON format:
{
    "match_percentage": <int>,
    "reason": "<string>"
}
"""


def _parse_result(result_text: str) -> dict:
    """Parse and sanitize the model's JSON into a clamped, typed result."""
    try:
        parsed = json.loads(result_text)
    except json.JSONDecodeError:
        logger.error("Failed to parse JSON from Groq response.")
        return {"match_percentage": 0, "reason": "Failed to parse AI output."}

    try:
        pct = int(parsed.get("match_percentage", 0))
    except (TypeError, ValueError):
        pct = 0
    pct = max(0, min(100, pct))  # clamp to a sane 0..100 range

    reason = str(parsed.get("reason") or "").strip() or "No reason provided."
    return {"match_percentage": pct, "reason": reason}


async def evaluate_job_match(cv_text: str, job_post_text: str) -> dict:
    """
    Sends the CV and Job Post to Groq (LLaMA 3) to calculate a match percentage.
    Returns a dict with 'match_percentage' (int 0..100) and 'reason' (str).
    Never raises: any hard failure yields a 0-score fallback.
    """
    prompt = (
        f"CANDIDATE CV:\n{cv_text}\n\n---\n\n"
        f"JOB POSTING:\n{job_post_text}\n\nEvaluate the match and return JSON."
    )

    last_error = None
    for attempt in range(config.RETRY_ATTEMPTS):
        try:
            response = await client.chat.completions.create(
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                model=config.GROQ_MODEL,
                temperature=config.GROQ_TEMPERATURE,
                response_format={"type": "json_object"},
            )
            return _parse_result(response.choices[0].message.content)
        except _RETRYABLE as e:
            last_error = e
            delay = config.RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning(
                f"Groq attempt {attempt + 1}/{config.RETRY_ATTEMPTS} failed "
                f"({e!r}); retrying in {delay:.1f}s"
            )
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Groq API error: {e}")
            return {"match_percentage": 0, "reason": f"API Error: {e}"}

    logger.error(f"Groq gave up after {config.RETRY_ATTEMPTS} attempts: {last_error!r}")
    return {"match_percentage": 0, "reason": "API Error: retries exhausted."}


# --- Quick Isolated Test Hook ---
if __name__ == "__main__":
    config.setup_logging()

    async def test():
        test_cv = "I am a Backend Developer using C++ and Qt."
        test_job = "Looking for a Mid-Level C++ Engineer with QML experience."

        print("Evaluating match via Groq LLaMA 3...")
        result = await evaluate_job_match(test_cv, test_job)
        print("Result:", result)

    asyncio.run(test())
