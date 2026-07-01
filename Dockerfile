FROM python:3.12-slim

WORKDIR /app

# Install runtime dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY *.py ./

# The SQLite DB is created at runtime; mount a volume here to persist it.
ENV DB_NAME=/data/job_matcher.db
VOLUME ["/data"]

# TELEGRAM_BOT_TOKEN and GROQ_API_KEY must be supplied at run time, e.g.:
#   docker run --env-file .env -v tg_agent_data:/data <image>
CMD ["python", "main.py"]
