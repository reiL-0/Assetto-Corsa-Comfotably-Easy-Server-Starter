import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlmodel import Session

from app import metrics, schedule, servers
from app.api.v1 import api_router
from app.config import settings
from app.db import engine, init_db
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
    metrics.purge()
    with Session(engine) as sess:
        n = await servers.adopt_running(sess)
    if n:
        log.info("re-attached to %d running acServer(s)", n)
    log.info("store ready at %s", settings.resolved_db_path())
    log.info("serve_ui=%s cors_origins=%s", settings.serve_ui, settings.cors_origins)
    ticker = asyncio.create_task(schedule.run_forever())
    yield
    ticker.cancel()


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


@app.middleware("http")
async def count_server_errors(request: Request, call_next):
    """Every 5xx lands in the metrics log (including the ones raised as exceptions)."""
    try:
        response = await call_next(request)
    except Exception:
        metrics.log(0, "http_5xx", name=request.url.path[:120], value=500)
        raise
    if response.status_code >= 500:
        metrics.log(0, "http_5xx", name=request.url.path[:120], value=response.status_code)
    return response


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
