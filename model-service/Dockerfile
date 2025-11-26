# Dockerfile
FROM python:3.11-slim

# optionally set non-root user later if you want
WORKDIR /app

# system deps often needed by onnxruntime (build-friendly)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      gcc libsndfile1 libglib2.0-0 libgomp1 libstdc++6 ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# copy requirements
COPY requirements.txt .

# pin onnxruntime to a modern release (you might adjust)
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# copy code
COPY . .

# uvicorn entry for FastAPI (or replace with your start command)
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]