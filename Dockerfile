# Use Playwright official image that includes browsers + deps
FROM mcr.microsoft.com/playwright/python:v1.56.0-jammy

# Create app directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

# Copy app code
COPY . /app

# Expose port (Render uses $PORT)
ENV PORT 8080

# Use gunicorn to run the app in production (4 workers)
CMD ["gunicorn", "-w", "4", "-k", "gevent", "--bind", "0.0.0.0:$PORT", "app:app"]
