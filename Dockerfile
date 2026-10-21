FROM python:3.11-slim

# Prevent Python from writing .pyc files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Install system dependencies, cron, and Chromium dependencies for Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    cron \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Copy pyproject.toml and install Python dependencies
COPY pyproject.toml /app/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# Install Chromium browser for Playwright
RUN playwright install chromium

# Copy application source code
COPY tender_mcp/ /app/tender_mcp/
COPY scripts/ /app/scripts/
COPY keywords.json /app/keywords.json
COPY README.md /app/README.md

# Re-install package in editable/installed mode
RUN pip install --no-cache-dir -e .

EXPOSE 8000

# Default command starts the web dashboard
CMD ["uvicorn", "tender_mcp.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
