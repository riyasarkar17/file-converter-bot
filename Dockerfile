# ────────────────────────────────────────────────────────────────────────────
# Stage 1 — builder
# Install Python dependencies into a virtual environment so we can copy
# only what's needed into the final slim image.
# ────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build-time OS deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create venv and install Python deps
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt


# ────────────────────────────────────────────────────────────────────────────
# Stage 2 — runtime
# ────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Install runtime-only OS dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    # poppler-utils for pdf2image
    poppler-utils \
    # ffmpeg for audio/video (optional bonus features)
    ffmpeg \
    # libpq for PostgreSQL connections
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Non-root user for security
RUN groupadd -r botuser && useradd -r -g botuser botuser

WORKDIR /app

# Copy the venv from the builder stage
COPY --from=builder /opt/venv /opt/venv

# Make sure the venv is on the PATH
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Copy application source
COPY --chown=botuser:botuser . .

# Create writable directories for temp files, logs, and the SQLite DB
RUN mkdir -p temp logs \
    && chown -R botuser:botuser /app

USER botuser

# Health-check: verify Python can import the main module
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "from config.settings import settings; print('OK')"

CMD ["python", "main.py"]
