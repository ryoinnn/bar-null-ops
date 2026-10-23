FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi==0.115.12 uvicorn==0.34.2 httpx==0.28.1
COPY main.py .
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
