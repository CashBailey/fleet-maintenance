FROM node:22-bookworm-slim AS web
WORKDIR /build
COPY package.json package-lock.json tsconfig*.json vite.config.ts eslint.config.js ./
COPY frontend ./frontend
RUN npm ci && npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock ./
RUN apt-get update \
    && apt-get install --yes --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.lock
COPY backend ./backend
COPY --from=web /build/backend/frontend_dist ./backend/frontend_dist
WORKDIR /app/backend
RUN DJANGO_SECRET_KEY=build-only-static-collection-key-not-valid-at-runtime-0123456789 \
    python manage.py collectstatic --noinput
RUN mkdir -p /data/media && chown -R 65532:65532 /data
ENV FLEETLINE_REQUIRE_STRONG_SECRET=1
USER 65532:65532
CMD ["gunicorn", "fleetops.wsgi:application", "--bind", "0.0.0.0:8088", "--workers", "2", "--access-logfile", "-", "--error-logfile", "-"]
