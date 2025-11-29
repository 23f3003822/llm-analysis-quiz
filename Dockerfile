# Dockerfile — Option A (use Playwright image :latest)
FROM mcr.microsoft.com/playwright/python:latest

WORKDIR /app

COPY requirements.txt /app/requirements.txt

RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

ENV PORT 8080

CMD ["gunicorn", "-w", "4", "-k", "gevent", "--bind", "0.0.0.0:$PORT", "app:app"]
