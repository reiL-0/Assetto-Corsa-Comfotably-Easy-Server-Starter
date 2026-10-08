"""A server's weather plan, played to CSP clients as `cspcmd.weather_set_v2` chat commands (the way CM's dynamic-conditions plugin does).

`WeatherDirector` is owned by a `supervisor.Instance`. Every `update_s` seconds (30 by default) it asks `weatherplan.Weather` for the conditions of the session that is
running (the session clock of the ACSP board says which session and how far in; in live mode the weather of the chosen place, refreshed every few minutes)
and broadcasts them; a car that finishes loading gets the latest one at once. The simulated date follows the server's time-of-day settings
(SUN_ANGLE, TIME_OF_DAY_MULT).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

from app.live import acsp, cspcmd, tracktime
from app.live.weatherplan import Weather, fetch_live

log = logging.getLogger("acmanager.cspweather")
PERIOD = 30.0   # default seconds between broadcasts (plan["update_s"]); also the time CSP is told to take to reach each one. The official plugin used a minute:
                # every command makes each client recompute clouds and rain, and transitions are where players with weaker PCs lose frames
KEEPALIVE = 60.0   # an unchanged weather is still repeated this often (a late joiner is greeted separately)


def command(state: dict, unix: int, period: float = PERIOD) -> str:
    return cspcmd.weather_set_v2(timestamp=unix, current=state["current"], upcoming=state["upcoming"], transition=state["transition"], time_to_apply=period,
                                 ambient=state["ambient"], road=state["road"], grip=state["grip"], humidity=state.get("humidity", 0.5),
                                 wind_deg=state.get("wind_deg", 0.0), wind_kmh=state.get("wind_kmh", 0.0), pressure=state.get("pressure", 1013.0),
                                 rain=state["rain"], wetness=state["wetness"], water=state["water"])


class WeatherDirector:
    def __init__(self, inst, data: dict, server_cfg: dict | None = None) -> None:
        self.inst, self.weather = inst, Weather(data)
        self.period = float(data.get("update_s") or PERIOD)
        self.live_at, self.sent_key, self.sent_at = -1e9, None, -1e9
        srv = (server_cfg or {}).get("SERVER", {})
        self.mult = float(srv.get("TIME_OF_DAY_MULT", 1) or 1)
        sun = data.get("sun_angle")   # the plan's own sun angle (set live from Control AC) wins over the server's
        minutes = 780 + float(srv.get("SUN_ANGLE", 0) or 0 if sun is None else sun) * 60 / 16   # the same mapping the Control AC form uses (angle 0 = 13:00)
        self.t0 = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=minutes)
        self.track, self.layout, self.tz = str(srv.get("TRACK") or ""), str(srv.get("CONFIG_TRACK") or ""), data.get("timezone")
        self.clock0, self.last = time.monotonic(), ""
        self.task = asyncio.get_running_loop().create_task(self.run())   # needs the event loop: build it from async code (see servers._restart_weather)

    def stop(self) -> None:
        self.task.cancel()

    def _unix(self) -> int:
        return int(self.t0.timestamp() + (time.monotonic() - self.clock0) * self.mult)

    async def run(self) -> None:
        from app import content   # lazy: content pulls in the whole upload stack
        off = await asyncio.to_thread(tracktime.offset_seconds, content._tracks_dir(), self.track, self.layout, self.tz)
        self.t0 -= timedelta(seconds=off)   # the clients read the timestamp in the track's local time: send the wanted time minus that offset
        delay = min(2.0, self.period)   # first pass soon so a change made in Control AC shows at once
        while self.inst.running:
            await asyncio.sleep(delay)
            delay = self.period
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
            state = w.step(self.period, board.elapsed_ms() / 1000)
            if state:
                self.last = command(state, self._unix(), self.period)
                key = (state["current"], state["upcoming"], round(state["transition"], 2), round(state["ambient"]), round(state["wind_kmh"]), round(state["rain"], 2))
                if key != self.sent_key or time.monotonic() - self.sent_at >= KEEPALIVE:   # nothing new: do not make every client recompute
                    self.sent_key, self.sent_at = key, time.monotonic()
                    client.send(acsp.encode_broadcast_chat(self.last))

    def greet(self, car_id: int) -> None:
        if self.last and self.inst.acsp:
            self.inst.acsp.send(acsp.encode_send_chat(car_id, self.last))
