"""FastAPI web UI for credit-keeper credential pool management."""

from __future__ import annotations

import math
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..db import CredentialDB

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(db_path: str) -> FastAPI:
    """Create and return a configured FastAPI application."""
    app = FastAPI(title="credit-keeper Web UI")
    db = CredentialDB(db_path)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        total = db.get_credential_count()
        exhausted = db.get_exhausted_count()
        active = total - exhausted

        # Compute total usage summary from latest snapshots
        latest_usage = db.get_latest_usage_per_credential()
        total_current_usage = sum(
            u.get("current_usage", 0) or 0 for u in latest_usage.values()
        )
        total_usage_limit = sum(
            u.get("usage_limit", 0) or 0 for u in latest_usage.values()
        )

        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "total": total,
                "active": active,
                "exhausted": exhausted,
                "total_current_usage": total_current_usage,
                "total_usage_limit": total_usage_limit,
            },
        )

    @app.get("/credentials", response_class=HTMLResponse)
    def credentials(request: Request) -> HTMLResponse:
        creds = db.get_all_credentials()
        latest_usage = db.get_latest_usage_per_credential()
        return templates.TemplateResponse(
            request,
            "credentials.html",
            {
                "credentials": creds,
                "latest_usage": latest_usage,
            },
        )

    @app.get("/usage/{client_id}", response_class=HTMLResponse)
    def usage(request: Request, client_id: str) -> HTMLResponse:
        history = db.get_usage_history(client_id)
        return templates.TemplateResponse(
            request,
            "usage.html",
            {
                "client_id": client_id,
                "history": history,
            },
        )

    @app.get("/requests", response_class=HTMLResponse)
    def requests_page(
        request: Request, page: int = Query(default=1, ge=1)
    ) -> HTMLResponse:
        per_page = 50
        logs = db.get_request_logs(page=page, per_page=per_page)
        total_count = db.get_request_log_count()
        total_pages = max(1, math.ceil(total_count / per_page))
        return templates.TemplateResponse(
            request,
            "requests.html",
            {
                "logs": logs,
                "page": page,
                "total_pages": total_pages,
                "total_count": total_count,
            },
        )

    return app
