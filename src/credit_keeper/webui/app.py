"""FastAPI web UI for credit-keeper credential pool management."""

from __future__ import annotations

import math
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..db import CredentialDB

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(db_path: str, warning_threshold_pct: float = 0.0) -> FastAPI:
    """Create and return a configured FastAPI application."""
    app = FastAPI(title="credit-keeper Web UI")
    db = CredentialDB(db_path)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        total = db.get_credential_count()
        exhausted = db.get_exhausted_count()
        dead = db.get_dead_count()
        warning = db.get_warning_count(warning_threshold_pct)
        # Warning is per-client and Exhausted/Dead are per-credential; clamp to
        # avoid negative when buckets overlap.
        active = max(0, total - exhausted - dead - warning)

        # Compute total usage summary from latest snapshots
        latest_usage = db.get_latest_usage_per_credential()
        total_current_usage = sum(
            u.get("current_usage", 0) or 0 for u in latest_usage.values()
        )
        total_usage_limit = sum(
            u.get("usage_limit", 0) or 0 for u in latest_usage.values()
        )

        # Build per-user share data for display
        usage_shares = []
        for client_id, u in latest_usage.items():
            current = u.get("current_usage", 0) or 0
            limit = u.get("usage_limit", 0) or 0
            share_pct = (current / total_current_usage * 100) if total_current_usage > 0 else 0
            usage_shares.append({
                "client_id": client_id,
                "current_usage": current,
                "usage_limit": limit,
                "share_pct": round(share_pct, 1),
            })
        usage_shares.sort(key=lambda x: x["current_usage"], reverse=True)

        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "total": total,
                "active": active,
                "warning": warning,
                "exhausted": exhausted,
                "dead": dead,
                "total_current_usage": total_current_usage,
                "total_usage_limit": total_usage_limit,
                "usage_shares": usage_shares,
            },
        )

    @app.get("/credentials", response_class=HTMLResponse)
    def credentials(request: Request) -> HTMLResponse:
        creds = db.get_all_credentials()
        latest_usage = db.get_latest_usage_per_credential()
        # Compute total current usage for share calculation
        total_current_usage = sum(
            u.get("current_usage", 0) or 0 for u in latest_usage.values()
        )
        warning_client_ids = {
            cid for cid in latest_usage.keys()
            if db.is_in_warning(cid, warning_threshold_pct)
        }
        return templates.TemplateResponse(
            request,
            "credentials.html",
            {
                "credentials": creds,
                "latest_usage": latest_usage,
                "total_current_usage": total_current_usage,
                "warning_client_ids": warning_client_ids,
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
