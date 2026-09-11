import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.api.v1 import api_router
from app.config import settings
from app.db import init_db
from app.web import mount_spa

ADMIN_SERVERS_HTML = Path(__file__).parent / "admin" / "servers.html"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("acmanager")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    log.info("store ready at %s", settings.resolved_db_path())
    log.info("serve_ui=%s cors_origins=%s", settings.serve_ui, settings.cors_origins)
    yield


app = FastAPI(
    title="AC Server Manager",
    version="0.0.0-dev",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/admin/servers", include_in_schema=False)
def admin_servers() -> FileResponse:
    """Standalone server-browser admin page (no build step, plain fetch() to the API)."""
    return FileResponse(ADMIN_SERVERS_HTML)


app.include_router(api_router)

if settings.serve_ui:
    mount_spa(app)  # keep last: registers a catch-all route
