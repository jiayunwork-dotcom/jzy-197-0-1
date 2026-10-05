#!/bin/sh
set -e

# 等待 PostgreSQL 可连接（compose 只保证容器启动，不保证数据库就绪）
echo "等待数据库 ${DATABASE_URL} ..."
python - <<'PY'
import os, time, sys
import psycopg
dsn = os.environ.get(
    "DATABASE_URL", "postgresql://recon:recon@db:5432/reconciliation"
)
for i in range(60):
    try:
        with psycopg.connect(dsn, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        print("数据库已就绪")
        break
    except Exception as exc:
        print(f"  尚未就绪({i}): {type(exc).__name__}")
        time.sleep(1)
else:
    print("数据库等待超时", file=sys.stderr)
    sys.exit(1)
PY

echo "初始化 schema ..."
python -c "from storage.postgres import PostgresRepository; PostgresRepository().init_schema()"

echo "启动 API (gunicorn) ..."
exec gunicorn --bind 0.0.0.0:8000 --workers ${GUNICORN_WORKERS:-2} --timeout 60 \
    "api.app:create_app()"
