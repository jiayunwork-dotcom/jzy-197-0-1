"""WSGI 入口：gunicorn/waitress 或 `python wsgi.py` 都可启动。"""
import os

from app.api import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
