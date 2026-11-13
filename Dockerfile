FROM python:3.11-slim

# Prevent Python from writing .pyc files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Install system dependencies, curl, and build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy package config and install Python dependencies
COPY pyproject.toml requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# Install Chromium and required OS dependencies for Playwright
RUN playwright install --with-deps chromium

# Copy application source code
COPY tender_mcp/ /app/tender_mcp/
COPY scrapers/ /app/scrapers/
COPY scripts/ /app/scripts/
COPY keywords.json /app/keywords.json
COPY README.md /app/README.md

# Install package in editable mode
RUN pip install --no-cache-dir -e .

EXPOSE 8000

# Start dashboard dynamically binding to Render's assigned $PORT (or 8000 default)
CMD ["sh", "-c", "uvicorn tender_mcp.web.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
