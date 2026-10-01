"""
wsgi.py — production entry point for gunicorn.

    gunicorn -c gunicorn.conf.py wsgi:app
"""
from app import app


if __name__ == "__main__":
    app.run()