"""Pure functions: one ACSP event in, a description of the infraction out (or None). No network, no database."""

from app.live import acsp

# ponytail: the speed unit of CLIENT_EVENT is not verified against a real server (km/h assumed) and these cut-offs are guesses that only
# keep touch-and-go noise out. Tune them from the recorded incidents of the shadow phase.
WALL_MIN_SPEED = 15.0
CONTACT_MIN_SPEED = 5.0


def detect(event: dict) -> dict | None:
    """-> {kind, car_id, other_car_id, speed, value, world_pos} for a wall hit, a contact or a lap with cuts."""
    t = event["type"]
    if t == "client_event":
        env = event["event_type"] == acsp.COLLISION_WITH_ENV
        if (env or event["event_type"] == acsp.COLLISION_WITH_CAR) and event["speed"] >= (WALL_MIN_SPEED if env else CONTACT_MIN_SPEED):
            return {"kind": "wall" if env else "contact", "car_id": event["car_id"], "other_car_id": event["other_car_id"],
                    "speed": event["speed"], "value": 0, "world_pos": event["world_pos"]}
    elif t == "lap_completed" and event["cuts"]:
        return {"kind": "cuts", "car_id": event["car_id"], "other_car_id": None, "speed": 0.0, "value": event["cuts"], "world_pos": []}
    return None
