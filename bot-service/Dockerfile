# Dockerfile (use this)
FROM python:3.11-slim

WORKDIR /app

# Install OS deps required by Pillow, psycopg2-binary, build wheels etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    build-essential \
    libpq-dev \
    libffi-dev \
    libjpeg62-turbo-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy only requirements first for better caching
COPY requirements.txt /app/

RUN pip install --upgrade pip setuptools wheel
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . /app

ENV PYTHONUNBUFFERED=1

# If your bot uses long-polling run bot.py; if you use webhooks or uvicorn change this
CMD ["python", "bot.py"]