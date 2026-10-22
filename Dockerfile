# ------------- Stage 1: build the Viito React frontend -------------
FROM node:20-alpine AS frontend-build
WORKDIR /build
COPY server/webapp/frontend/package*.json ./
RUN npm ci
COPY server/webapp/frontend/ ./
RUN npm run build

# ------------- Stage 2: Python runtime (serves the API AND the built UI) ---
# FastAPI (server/main.py) serves the built SPA from
# server/webapp/frontend/dist alongside the /api routes, so a single
# container exposes the whole Viito application.
FROM python:3.12-slim
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server ./server
# Runtime-required: attributes_classifier.py imports tools/gen_attribute_spec.py
# at module load time and reads tools/attr_defs/*.json as the action schemas.
COPY tools ./tools

# Frontend build produced in stage 1.
COPY --from=frontend-build /build/dist ./server/webapp/frontend/dist

# Writable state directories (overridden by bind mounts in docker-compose.yml).
RUN mkdir -p \
      /app/server/webapp/uploads \
      /app/server/memory/uploads \
      /app/memory

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]