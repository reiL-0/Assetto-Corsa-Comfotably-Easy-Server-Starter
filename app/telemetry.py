"""Ingest from the in-game OPR Telemetry app (clients/OPRTelemetry): the driver's own
pedals/steer/heading, merged into the live map next to what ACSP already reports.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import supervisor

router = APIRouter(prefix="/telemetry", tags=["telemetry"])


class Vec3(BaseModel):
    x: float
    y: float
    z: float


class Sample(BaseModel):
    steamId: str  # SteamID64 == ACSP driver_guid
    pos: Vec3
    rotation: Vec3  # radians, x = heading
    speedKmh: float
    gear: int  # 0=R, 1=N, 2=1st... (same as ACSP)
    rpm: int
    throttle: float
    brake: float
    clutch: float
    steerAngle: float  # degrees


@router.post("/ingest", status_code=204)
def ingest(body: Sample) -> None:
    """No auth: the driver is found by steamId among the cars connected right now.
    409 driver not connected to any running server · 429 faster than 20 Hz.
    ponytail: anyone who knows a connected driver's SteamID64 can feed that car fake
    pedals/steer (cosmetic live-map data only). Sign samples if that ever matters.
    """
    hits = [
        (inst.acsp, car_id)
        for inst in supervisor.live()
        for car_id, car in inst.acsp.cars.items()
        if car.get("driver_guid") == body.steamId
    ]
    if not hits:
        raise HTTPException(409, "driver not connected to any running server")
    sample = body.model_dump(exclude={"steamId"})
    accepted = [client.add_telemetry(car_id, sample) for client, car_id in hits]  # no short-circuit
    if not any(accepted):
        raise HTTPException(429, "slow down")
