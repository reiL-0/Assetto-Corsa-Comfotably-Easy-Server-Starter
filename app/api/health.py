from fastapi import APIRouter

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Unversioned liveness probe for load balancers / monitoring."""
    return {"status": "ok"}
