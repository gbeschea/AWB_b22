# Slim multi-stage image for AWB Hub. Small, fast cold start, non-root.
# Build:  docker build -t awb-hub .
# Run:    docker run -p 8000:8000 --env-file .env awb-hub
#   (AWB_B2_ENC_KEY is REQUIRED — the app refuses to boot without it.)

# ---- builder: resolve deps into an isolated venv ----
FROM python:3.13-slim AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install -r requirements.txt

# ---- runtime: copy the venv + app, drop privileges ----
FROM python:3.13-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/opt/venv/bin:$PATH"
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY . .
RUN adduser --disabled-password --gecos "" appuser && chown -R appuser /app
USER appuser
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
