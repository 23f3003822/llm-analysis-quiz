# Use Playwright official image with browsers & deps preinstalled
FROM mcr.microsoft.com/playwright/python:latest

WORKDIR /app

# Copy requirements and install
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip setuptools wheel \
 && pip install --no-cache-dir -r /app/requirements.txt

# Copy app
COPY . /app

# Environment defaults (override on Render)
ENV QUIZ_SECRET="s3cr3t-llm-2025"
ENV PORT=8080
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python", "app.py"]
