"""Versioned public API. Every client — the bundled UI included — talks only to
this router. Nothing the UI can do is missing here.
"""

from fastapi import APIRouter

from app.api.v1 import meta

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(meta.router, tags=["meta"])

# Phase 1+: api_router.include_router(servers.router, prefix="/servers", tags=["servers"])
# Phase 2+: live timing WebSocket, ACSP command endpoints
# Phase 3+: content + file upload/download endpoints
