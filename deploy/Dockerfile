# RGNR8 owner web app + delivery worker (Python side).
# The TypeScript ledger service runs as its own image (deploy/Dockerfile.ledger)
# and its migrations run in the release step; this image serves the owner
# API/dashboard and the delivery worker.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Runtime third-party deps. The rgnr8_* packages themselves are imported from
# source (deploy/_pathsetup.py globs every packages/*/src) — no build step.
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# Copy ALL package sources, not a hand-picked subset: the web/ops import graph
# pulls in ~18 rgnr8_* packages, and copying only a few is what shipped an
# unbootable image before. .dockerignore keeps node_modules/dist/caches out.
COPY packages /app/packages
COPY deploy   /app/deploy

WORKDIR /app/deploy
EXPOSE 8080

# Health check hits the readiness endpoint (no auth).
HEALTHCHECK --interval=30s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request,os,sys; \
u=f'http://127.0.0.1:{os.environ.get(\"RGNR8_PORT\",\"8080\")}/ready'; \
sys.exit(0 if urllib.request.urlopen(u,timeout=2).status==200 else 1)"

# Bind gunicorn to the configured port (default 8080).
CMD ["sh", "-c", "gunicorn entry:application --workers ${RGNR8_WORKERS:-4} --bind 0.0.0.0:${RGNR8_PORT:-8080} --access-logfile -"]
