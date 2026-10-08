"""Pure functions: one ACSP event in, a description of the infraction out (or None). No network, no database."""

import math

from app.live import acsp

# ponytail: the speed unit of CLIENT_EVENT is not verified against a real server (km/h assumed) and these cut-offs are guesses that only
# keep touch-and-go noise out. Tune them from the recorded incidents of the shadow phase.
WALL_MIN_SPEED = 15.0
CONTACT_MIN_SPEED = 5.0


BEHIND_DEG = 35.0  # the other car is within this angle of where I am heading: I was driving into it
SAME_WAY_DEG = 70.0  # and it was going roughly the same way as me (a rear-end hit, not a head-on)
MOVING = 3.0  # m/s: a car slower than this is not "driving into" anything


def _angle(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Degrees between two flat vectors (180 if one has no length)."""
    na, nb = math.hypot(*a), math.hypot(*b)
    if not na or not nb:
        return 180.0
    return math.degrees(math.acos(max(-1.0, min(1.0, (a[0] * b[0] + a[1] * b[1]) / (na * nb)))))


def at_fault(a: dict, b: dict) -> tuple[int | None, str]:
    """Who ran into whom from each car's last `car_update` ({pos, velocity}; x/z are the ground plane, speed in m/s).
    -> (0 if `a` is to blame, 1 if `b`, None if unclear, why). Only a clear rear-end hit is blamed; a side-by-side touch is left to the stewards.
    ponytail: positions are the last ~5 Hz sample, so a very light touch can read the wrong way; judge with the evidence, not just this."""
    for i, (me, other) in enumerate(((a, b), (b, a))):
        v = (me["velocity"][0], me["velocity"][2])
        to_other = (other["pos"][0] - me["pos"][0], other["pos"][2] - me["pos"][2])
        if math.hypot(*v) >= MOVING and _angle(v, to_other) <= BEHIND_DEG and _angle(v, (other["velocity"][0], other["velocity"][2])) <= SAME_WAY_DEG:
            return i, "venía detrás y avanzando hacia el otro auto"
    return None, "contacto lateral o poco claro: lo decide el comisario"


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
