"""A server's weather plan, played to CSP clients as `cspcmd.weather_set_v2` chat commands (the way CM's dynamic-conditions plugin does).

`WeatherDirector` is owned by a `supervisor.Instance`. Every `PERIOD` seconds it asks `weatherplan.Weather` for the conditions of the session that is
running (the session clock of the ACSP board says which session and how far in; in live mode the weather of the chosen place, refreshed every few minutes)
and broadcasts them; a car that finishes loading gets the latest one at once. The simulated date follows the server's time-of-day settings
(SUN_ANGLE, TIME_OF_DAY_MULT).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

from app.live import acsp, cspcmd
from app.live.weatherplan import Weather, fetch_live

log = logging.getLogger("acmanager.cspweather")
PERIOD = 5.0   # seconds between broadcasts (and the time CSP is told to take to reach each one)


def command(state: dict, unix: int) -> str:
    return cspcmd.weather_set_v2(timestamp=unix, current=state["current"], upcoming=state["upcoming"], transition=state["transition"], time_to_apply=PERIOD,
                                 ambient=state["ambient"], road=state["road"], grip=state["grip"], humidity=state.get("humidity", 0.5),
                                 wind_deg=state.get("wind_deg", 0.0), wind_kmh=state.get("wind_kmh", 0.0), pressure=state.get("pressure", 1013.0),
                                 rain=state["rain"], wetness=state["wetness"], water=state["water"])


class WeatherDirector:
    def __init__(self, inst, data: dict, server_cfg: dict | None = None) -> None:
        self.inst, self.weather = inst, Weather(data)
        self.live_at = -1e9
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
            w, board = self.weather, client.board
            if w.mode == "live" and time.monotonic() - self.live_at > float(w.plan.get("live", {}).get("refresh_min", 10)) * 60:
                self.live_at = time.monotonic()
                live = w.plan.get("live", {})
                data = await asyncio.to_thread(fetch_live, float(live.get("lat", 0)), float(live.get("lon", 0)))
                if data:
                    w.live = data
                else:
                    log.warning("live weather of server %s: no answer", self.inst.server_id)
            if not w.start_session(board.session.get("session_type")):
                continue   # this session has no entry: the server's own weather applies
            state = w.step(PERIOD, board.elapsed_ms() / 1000)
            if state:
                self.last = command(state, self._unix())
                client.send(acsp.encode_broadcast_chat(self.last))

    def greet(self, car_id: int) -> None:
        if self.last and self.inst.acsp:
            self.inst.acsp.send(acsp.encode_send_chat(car_id, self.last))
