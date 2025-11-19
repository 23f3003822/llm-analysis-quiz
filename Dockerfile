# Use a stable Python runtime with apt available
FROM python:3.11-slim

# Install system deps required by Playwright and pdf tools
# (This list comes from Playwright docs + common PDF deps)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    wget \
    git \
    curl \
    gnupg \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpangocairo-1.0-0 \
    libgtk-3-0 \
    libx11-xcb1 \
    libxss1 \
    fonts-liberation \
    libwoff1 \
    libgdk-pixbuf2.0-0 \
    poppler-utils \
    build-essential \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Copy requirements first for Docker layer caching
COPY requirements.txt /app/requirements.txt

# Install Python deps
RUN pip install --upgrade pip setuptools wheel \
 && pip install --no-cache-dir -r /app/requirements.txt

# Install Playwright browsers (runs as root inside the container)
RUN python -m playwright install --with-deps

# Copy app
COPY . /app

# Expose port (Render will set PORT env var)
EXPOSE 8080

# Set env defaults (override on Render dashboard)
ENV QUIZ_SECRET="s3cr3t-llm-2025"
ENV PORT=8080
ENV PYTHONUNBUFFERED=1

# Start the app
CMD ["python", "app.py"]
