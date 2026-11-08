# Auto-Connect Trade Finance - container image
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 AUTO_CONNECT_ENV=production
WORKDIR /app
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-deu poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home appuser
COPY auto_connect ./auto_connect
# code readable but not writable by the app user (host file modes are not trusted)
RUN chmod -R a+rX,go-w /app/auto_connect && mkdir -p /app/state /app/output && chown -R appuser /app/state /app/output
USER appuser
EXPOSE 8088
VOLUME ["/app/state", "/app/output"]
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8088/health').status==200 else 1)"
CMD ["python", "-m", "auto_connect.main", "--server", "--host", "0.0.0.0", "--port", "8088"]
