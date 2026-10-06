"""What a player must have installed to join (Custom Shaders Patch and the apps and mods the league asks for).

One list for the whole league, kept in `Setting` key `requirements`: `{"csp_build": 3898, "items": [{"name", "url"}]}`.
- `csp_build` > 0 is the Custom Shaders Patch build the league asks for; 0 = none. It is **shown, not enforced**: the way CSP documents
  for a stock server (writing TRACK as `csp/<build>/../<track>`) does not work with the native Linux acServer, which opens
  `content/tracks/csp/<track>` and cannot find the track's files (tested: surfaces.ini and the checksums are lost), so it is not used.
- `items` are the downloads the league asks for (Helicorsa...). acServer cannot force an app onto a client either.
Both are shown to the players in the server's page in Content Manager (`wake.details` appends them to the description) and in the
sign-up announcement (`{requisitos}`).
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session

from app.db import SessionDep, engine
from app.models import Setting

router = APIRouter(prefix="/requirements", tags=["requirements"])
KEY = "requirements"


class Item(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(max_length=500, pattern=r"^https?://\S+$")


class RequirementsIn(BaseModel):
    csp_build: int = Field(default=0, ge=0, le=99999)
    items: list[Item] = Field(default=[], max_length=10)

    @field_validator("items")
    @classmethod
    def _no_dupes(cls, v: list[Item]) -> list[Item]:
        if len({i.url for i in v}) != len(v):
            raise ValueError("the same link twice")
        return v


def current() -> dict:
    with Session(engine) as s:
        row = s.get(Setting, KEY)
    return {"csp_build": 0, "items": [], **(row.value if row and row.value else {})}


def lines(req: dict | None = None) -> list[str]:
    req = req or current()
    out = [f"Custom Shaders Patch build {req['csp_build']} o superior (obligatorio)"] if req["csp_build"] else []
    return out + [f"{i['name']}: {i['url']}" for i in req["items"]]


@router.get("")
def get() -> dict:
    return current()


@router.put("")
def put(body: RequirementsIn, sess: SessionDep) -> dict:
    """Shown in the lobby page and the announcements from the next time they are built."""
    sess.merge(Setting(key=KEY, value=body.model_dump()))
    sess.commit()
    return current()
