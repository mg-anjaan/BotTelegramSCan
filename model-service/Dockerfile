# model-service/Dockerfile (paste this into your model-service folder)
FROM python:3.11-slim

# create app dir
WORKDIR /app

# install system deps needed for onnxruntime patching + build
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc patchelf ca-certificates wget curl procps file binutils && \
    rm -rf /var/lib/apt/lists/*

# copy only the requirements for faster cache
COPY requirements.txt /app/requirements.txt

# upgrade pip and install deps
RUN python -m pip install --upgrade pip
RUN pip install -r /app/requirements.txt

# attempt to find and clear executable-stack on onnxruntime shared objects
# (harmless if onnxruntime not installed)
RUN set -eux; \
    if [ -d /usr/local/lib/python3.11/site-packages/onnxruntime ]; then \
      echo "Found onnxruntime; clearing execstack on .so files..."; \
      find /usr/local/lib/python3.11/site-packages/onnxruntime -type f -name "*.so" -print -exec patchelf --clear-execstack {} \; || true; \
      echo "Done patchelf step."; \
    else \
      echo "onnxruntime directory not present - skipping patchelf."; \
    fi

# copy app code
COPY . /app

# sanity: show any onnxruntime .so files and GNU_STACK flags (optional, helps debug)
RUN set -eux; \
    find /usr/local/lib/python3.11/site-packages -type f -name "onnxruntime_pybind11_state*.so" -print -exec readelf -l {} \; || true

# use uvicorn (example) — adjust app:app to your entrypoint
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]