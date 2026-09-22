# api / worker-llm / worker-ops share this image (§3.2).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    RB_PDF_BACKEND=unoserver

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt backend/requirements-llm.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && pip install -r requirements-llm.txt \
    && pip install "celery[gevent]>=5.4"

COPY backend/ ./backend/
COPY docs/ ./docs/
WORKDIR /app/backend

# Non-root, with the storage volume owned by the same uid.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /storage && chown -R app:app /storage /app
USER app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
