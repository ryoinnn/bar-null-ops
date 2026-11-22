FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
# 出資者向けの月次資料（PDF）に埋め込む日本語フォント（IPAex ゴシック。docs/investor_report.md）
RUN apt-get update && apt-get install -y --no-install-recommends fonts-ipaexfont-gothic && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY sales.toml asana_ids.json ./
# 店舗ごとの設定（Bar null は上の sales.toml。ほかの店舗は stores/<店舗>/。環境変数 SALES_TOML で選ぶ）
COPY stores ./stores
COPY src ./src
# root では動かさない
USER nobody
# ROLE=webhook / worker（同じイメージを2つのサービスで使う）
CMD exec uvicorn barnull_sales.main:app --host 0.0.0.0 --port ${PORT:-8080}
