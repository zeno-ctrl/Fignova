FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 AUTOFIGURE_DATA_DIR=/data
RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-writer-nogui poppler-utils fonts-dejavu fonts-liberation fonts-crosextra-carlito \
      fonts-crosextra-caladea fonts-noto-core util-linux \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 autofigure && mkdir -p /data && chown autofigure /data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY autofigure ./autofigure
COPY web ./web
USER autofigure
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["python", "-m", "autofigure", "serve", "--host", "0.0.0.0", "--port", "8000"]
