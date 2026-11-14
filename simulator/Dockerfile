FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependintele intai, ca Docker sa tina pasul in cache cand se schimba doar codul
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY sim.py .

RUN useradd --create-home --uid 10001 sim
USER sim

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"

CMD ["python", "sim.py"]
