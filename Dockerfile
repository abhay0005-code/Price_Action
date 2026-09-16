# ---- build the Next.js frontend into a static export (web/out) ----
FROM node:22-alpine AS frontend
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
ENV NEXT_TELEMETRY_DISABLED=1
# `next build` with output:"export" produces ./out (the full UI as static files)
RUN npm run build

# ---- python backend (serves BOTH the API and the exported UI) ----
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.railway.txt .
RUN pip install --no-cache-dir -r requirements.railway.txt

COPY . .
# Overwrite any stale web/out copied from the context with the freshly built one.
COPY --from=frontend /web/out ./web/out

EXPOSE 8000
CMD sh -c 'uvicorn server.main:app --host 0.0.0.0 --port ${PORT:-8000}'