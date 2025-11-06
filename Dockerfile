# Python 3.11 slim image
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/app/data/bot.sqlite

WORKDIR /app

# System deps
RUN apt-get update -y && apt-get install -y --no-install-recommends \
    tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY bot.py ./

# Create data dir for SQLite
RUN mkdir -p /app/data

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "bot.py"]
