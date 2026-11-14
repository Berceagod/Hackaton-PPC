FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN useradd --system --uid 10001 filegrid && chown -R filegrid /app
USER filegrid

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/livez', timeout=2).status==200 else 1)"

# Several worker processes (slice G): CPU-heavy uploads (multipart parsing, SHA-256, AES)
# in one category do not starve another category of CPU. Each worker has its own pools,
# bulkheads and breaker state; the outbox drainer is safe to run in every worker.
ENV UVICORN_WORKERS=4
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port 8000 --no-access-log --workers ${UVICORN_WORKERS}"]
