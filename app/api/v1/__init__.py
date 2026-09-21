"""Versioned public API. Every client — the bundled UI included — talks only to
this router. Nothing the UI can do is missing here.
"""

from fastapi import APIRouter, Depends

from app.auth import guard
from app.auth import router as auth_router
from app.championship import router as championship_router
from app.content import router as content_router
from app.servers import router as servers_router
from app.servers import steward as steward_router
from app.telemetry import router as telemetry_router

api_router = APIRouter(prefix="/api/v1")


@api_router.get("/version", tags=["meta"])
def version() -> dict[str, str]:
    return {"version": "0.0.0-dev", "api": "v1"}


api_router.include_router(auth_router)
# Reads: any logged-in user, except servers (config carries ADMIN_PASSWORD, plus logs) -> steward.
# Writes: admin. Steward-only moderation routes live on their own router.
api_router.include_router(servers_router, dependencies=[Depends(guard("steward"))])
api_router.include_router(steward_router)
api_router.include_router(telemetry_router)  # public: the in-game app has no login, it sends its SteamID64
api_router.include_router(content_router, dependencies=[Depends(guard())])
api_router.include_router(championship_router, dependencies=[Depends(guard())])
