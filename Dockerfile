FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1     DEBIAN_FRONTEND=noninteractive     CONFIG_DIR=/config     RECORDINGS_DIR=/recordings

RUN apt-get update && apt-get install -y --no-install-recommends \
    tini \
    ffmpeg \
    chromium \
    chromium-driver \
    curl \
    gcc \
    g++ \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install-deps chromium 2>/dev/null || true

COPY app/ ./app/

RUN mkdir -p /config /recordings

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
