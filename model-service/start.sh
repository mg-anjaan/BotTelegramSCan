#!/bin/sh
echo "Starting uvicorn on 0.0.0.0:${PORT:-8080}"
exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1
