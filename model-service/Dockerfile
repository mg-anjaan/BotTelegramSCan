FROM python:3.11-slim

WORKDIR /app

COPY model-service/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY model-service /app

ENV PYTHONUNBUFFERED=1

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]