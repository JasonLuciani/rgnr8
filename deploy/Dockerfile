# RGNR8 owner web app + delivery worker (Python side).
# The TypeScript ledger migrations run via @rgnr8/migrations in a separate step
# (see DEPLOYMENT.md); this image serves the owner API/dashboard and the worker.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Runtime deps: a WSGI server + the Postgres driver. The rgnr8_* packages are
# imported from source (deploy/_pathsetup.py) — no build step for the Python side.
RUN pip install "gunicorn>=21" "psycopg[binary]>=3.1"

COPY packages/forecast/src   /app/packages/forecast/src
COPY packages/briefing/src   /app/packages/briefing/src
COPY packages/web/src        /app/packages/web/src
COPY packages/runtime/src    /app/packages/runtime/src
COPY packages/ops/src        /app/packages/ops/src
COPY deploy                  /app/deploy

WORKDIR /app/deploy
EXPOSE 8080

# Health check hits the readiness endpoint (no auth).
HEALTHCHECK --interval=30s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request,os,sys; \
u=f'http://127.0.0.1:{os.environ.get(\"RGNR8_PORT\",\"8080\")}/ready'; \
sys.exit(0 if urllib.request.urlopen(u,timeout=2).status==200 else 1)"

# Bind gunicorn to the configured port (default 8080).
CMD ["sh", "-c", "gunicorn entry:application --workers ${RGNR8_WORKERS:-4} --bind 0.0.0.0:${RGNR8_PORT:-8080} --access-logfile -"]
