"""Versioned public API. Every client — the bundled UI included — talks only to
this router. Nothing the UI can do is missing here.
"""

from fastapi import APIRouter

from app.championship import router as championship_router
from app.content import router as content_router
from app.servers import router as servers_router

api_router = APIRouter(prefix="/api/v1")


@api_router.get("/version", tags=["meta"])
def version() -> dict[str, str]:
    return {"version": "0.0.0-dev", "api": "v1"}


api_router.include_router(servers_router)
api_router.include_router(content_router)
api_router.include_router(championship_router)
