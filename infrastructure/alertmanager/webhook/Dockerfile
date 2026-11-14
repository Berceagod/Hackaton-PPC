FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY server.py .
RUN useradd --system --uid 10001 hook
USER hook
EXPOSE 9099
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9099/healthz', timeout=2).status==200 else 1)"
CMD ["python", "server.py"]
