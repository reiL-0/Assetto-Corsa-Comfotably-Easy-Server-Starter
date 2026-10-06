"""A server's weather plan, played to CSP clients as `cspcmd.weather_set_v2` chat commands (the way CM's dynamic-conditions plugin does).

`WeatherDirector` is owned by a `supervisor.Instance`. Every `PERIOD` seconds it advances the plan (`weatherplan.Conditions`: rain, wetness and
puddles build up and dry like a track) and broadcasts the result; a car that finishes loading gets the latest one at once. The plan's clock starts
when the director does (server start), and the simulated date follows the server's time-of-day settings (SUN_ANGLE, TIME_OF_DAY_MULT).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

from app.live import acsp, cspcmd
from app.live.weatherplan import Conditions, Plan

log = logging.getLogger("acmanager.cspweather")
PERIOD = 5.0   # seconds between broadcasts (and the time CSP is told to take to reach each one)


def plan_from(data: dict) -> Conditions:
    plan = Plan([(float(s), int(t)) for s, t in data["steps"]], float(data.get("transition_s", 30)))
    return Conditions(plan, ambient=float(data.get("ambient", 20)), loop=float(data.get("loop_s") or 0) or None)


def command(state: dict, unix: int) -> str:
    return cspcmd.weather_set_v2(timestamp=unix, current=state["current"], upcoming=state["upcoming"], transition=state["transition"], time_to_apply=PERIOD,
                                 ambient=state["ambient"], road=state["road"], grip=state["grip"], humidity=min(1.0, 0.5 + 0.4 * state["rain"]),
                                 rain=state["rain"], wetness=state["wetness"], water=state["water"])


class WeatherDirector:
    def __init__(self, inst, data: dict, server_cfg: dict | None = None) -> None:
        self.inst, self.cond = inst, plan_from(data)
        srv = (server_cfg or {}).get("SERVER", {})
        self.mult = float(srv.get("TIME_OF_DAY_MULT", 1) or 1)
        minutes = 780 + float(srv.get("SUN_ANGLE", 0) or 0) * 60 / 16   # the same mapping the Control AC form uses (angle 0 = 13:00)
        self.t0 = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=minutes)
        self.clock0, self.last = time.monotonic(), ""
        self.task = asyncio.ensure_future(self.run())

    def stop(self) -> None:
        self.task.cancel()

    def _unix(self) -> int:
        return int(self.t0.timestamp() + (time.monotonic() - self.clock0) * self.mult)

    async def run(self) -> None:
        while self.inst.running:
            await asyncio.sleep(PERIOD)
            client = self.inst.acsp
            if not client or not self.inst.running:
                continue
            client.on_client_loaded = self.greet
            self.last = command(self.cond.step(PERIOD), self._unix())
            client.send(acsp.encode_broadcast_chat(self.last))

    def greet(self, car_id: int) -> None:
        if self.last and self.inst.acsp:
            self.inst.acsp.send(acsp.encode_send_chat(car_id, self.last))
