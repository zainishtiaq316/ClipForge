# Multi-stage build: compile the React UI with Node, then ship it inside a slim
# Python image. FFmpeg comes from the imageio-ffmpeg wheel, so no apt package is needed.

FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CLIPFORGE_HOST=0.0.0.0 \
    CLIPFORGE_OPEN_BROWSER=0 \
    CLIPFORGE_DATA_DIR=/data \
    CLIPFORGE_FRONTEND_DIST=/app/frontend/dist

# OpenCV (headless) still needs glib at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./
COPY --from=frontend /build/dist /app/frontend/dist

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 clipforge && mkdir -p /data && chown clipforge /data
USER clipforge

EXPOSE 8000
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["python", "-m", "app"]
