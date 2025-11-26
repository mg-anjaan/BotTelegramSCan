# /bot-service/Dockerfile
FROM python:3.11-slim

WORKDIR /app

# system libs needed by Pillow etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev build-essential libjpeg62-turbo-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy and install bot-service requirements (explicit path)
COPY bot-service/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy bot-service source code
COPY bot-service/ /app/

ENV PYTHONUNBUFFERED=1

# Railway sets PORT but bot doesn't serve HTTP; still include default
ENV PORT=8000

CMD ["python", "bot.py"]