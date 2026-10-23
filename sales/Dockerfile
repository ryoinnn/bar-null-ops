FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY sales.toml asana_ids.json ./
COPY src ./src
# root では動かさない
USER nobody
# ROLE=webhook / worker（同じイメージを2つのサービスで使う）
CMD exec uvicorn barnull_sales.main:app --host 0.0.0.0 --port ${PORT:-8080}
