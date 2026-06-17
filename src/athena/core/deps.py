"""Shared FastAPI dependencies for the API layer.

`get_conn` is the one place a request gets a database connection. Every module's
HTTP layer (aegis, users, later mentor) depends on this instead of opening its
own — so connection handling lives in exactly one spot. Kept out of db.py so
that module stays framework-free (pure SQLite, importable without FastAPI).
"""
from __future__ import annotations

from fastapi import Request

from athena.core import db


def get_conn(request: Request):
    """Per-request DB connection, opened from the path the app booted with.
    A FastAPI dependency: it runs before the handler and cleans up after."""
    conn = db.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()
