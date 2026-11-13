"""
wsgi_windows.py — production entry point for the Windows company server.

Runs Waitress bound to localhost only. IIS (the reverse proxy) is the
sole path in from the network.

    python wsgi_windows.py
"""
from app import app
from waitress import serve

if __name__ == "__main__":
    serve(app, host="127.0.0.1", port=5000, threads=4)