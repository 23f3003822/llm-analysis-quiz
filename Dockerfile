FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install --with-deps
COPY . /app
ENV QUIZ_SECRET="s3cr3t-llm-2025"
CMD ["python","app.py"]
