from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse

STATIC = Path(__file__).parent / "static"
INDEX = STATIC / "index.html"

_PLACEHOLDER = HTMLResponse(
    "<!doctype html><meta charset=utf-8><title>AC Server Manager</title>"
    "<h1>AC Server Manager</h1>"
    "<p>Frontend not built yet &mdash; run <code>make web</code>.</p>"
    "<p>API docs: <a href='/api/docs'>/api/docs</a> &middot; "
    "health: <a href='/healthz'>/healthz</a></p>"
)


def mount_spa(app: FastAPI) -> None:
    """Serve the built frontend from app/static, falling back to index.html for
    client-side routes. Register this AFTER all API routers."""

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        target = STATIC / full_path
        if full_path and target.is_file():
            return FileResponse(target)
        if INDEX.is_file():
            return FileResponse(INDEX)
        return _PLACEHOLDER
