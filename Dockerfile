# SEO Writer Agent: the Studio (web interface) and the article pipeline.
# Single long-lived process (Flask + in-process job queue). Run with ONE replica: state lives on
# disk (mount a volume at /app/project/output) and the queue is in-memory.

FROM python:3.14-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    UI_HOST=0.0.0.0 \
    UI_PORT=5050 \
    PATH="/app/bin:${PATH}"

WORKDIR /app

# deps first (layer cache): the locked set, the same versions the tests run on
COPY requirements.txt .
RUN pip install -r requirements.txt

# headless Chromium for screenshots/infographics (+ system deps and fonts), the build that
# matches the pinned playwright version
RUN playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*

# the app (.dockerignore is an allowlist: no .env files, no articles, no local state)
COPY . .
RUN mkdir -p project/output

EXPOSE 5050

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:5050/healthz')" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
