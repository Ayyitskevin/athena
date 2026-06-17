"""The Athena web application: a thin FastAPI layer over core and the modules.

`create_app()` is an *application factory* — call it to build a fresh app.
Tests call it with a throwaway database; the server calls it once for real.
No global app state, no surprises.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from athena import config
from athena.core import db


def create_app(db_path: str | Path | None = None) -> FastAPI:
    resolved_db = Path(db_path) if db_path is not None else config.DB_PATH

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup: bring the schema up to date before serving any request,
        # so the database is always the right shape. Stash the path for handlers.
        conn = db.connect(resolved_db)
        db.migrate(conn)
        conn.close()
        app.state.db_path = resolved_db
        yield
        # Shutdown: nothing to clean up yet.

    app = FastAPI(title="Athena", lifespan=lifespan)

    @app.get("/healthz")
    def healthz():
        """Liveness check — cheap, no DB hit. Used by tests and (later) monitoring."""
        return {"status": "ok"}

    return app


# The instance the server runs:  uvicorn athena.main:app
app = create_app()
