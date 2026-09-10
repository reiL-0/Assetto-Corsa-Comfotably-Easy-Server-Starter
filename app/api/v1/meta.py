from fastapi import APIRouter

router = APIRouter()


@router.get("/version")
def version() -> dict[str, str]:
    return {"version": "0.0.0-dev", "api": "v1"}
