# Use Playwright official Python image that includes browsers + deps (matching Playwright runtime)
FROM mcr.microsoft.com/playwright/python:v1.56.1-jammy

# Create app directory
WORKDIR /app

# Copy requirements and install Python deps
COPY requirements.txt /app/requirements.txt

# Upgrade pip then install packages
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r /app/requirements.txt

# Copy app code
COPY . /app

# Expose port used by Render (PORT env var)
ENV PORT 8080

# Start the app with gunicorn (production)
CMD ["gunicorn", "-w", "4", "-k", "gevent", "--bind", "0.0.0.0:$PORT", "app:app"]
