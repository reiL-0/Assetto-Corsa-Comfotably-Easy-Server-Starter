"""`python -m app.relay`: run the relay in front of an acServer (a feasibility spike for server-driven CSP weather).

    python -m app.relay --public 9680 --tcp 9690 --udp 9691 --http 9709 --plan "15:0,7:60,15:240" --transition 30

acServer runs on the internal ports (TCP_PORT/UDP_PORT/HTTP_PORT in its server_cfg.ini); players use the public ones (`--public` for TCP and UDP,
`--public + 1` for the lobby page). The plan is `type:second` pairs (WeatherFX type ids: 15 clear, 7 rain, 8 heavy rain…): the weather is
broadcast once a second to every client past the handshake.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time

from app.relay import http, protocol as p
from app.relay.tcp import TcpRelay
from app.relay.udp import UdpRelay
from app.relay.weather import Conditions, Plan

log = logging.getLogger("acmanager.relay")


async def main() -> None:
    a = argparse.ArgumentParser()
    a.add_argument("--public", type=int, required=True)
    a.add_argument("--tcp", type=int, required=True)
    a.add_argument("--udp", type=int, required=True)
    a.add_argument("--http", type=int, required=True)
    a.add_argument("--plan", default="15:0")
    a.add_argument("--transition", type=float, default=30)
    a.add_argument("--ambient", type=float, default=20)
    a.add_argument("--min-csp", type=int, default=0)
    a.add_argument("--inject-after", choices=["handshake", "car_list", "none"], default="handshake")
    a.add_argument("--no-weather-fx-flag", action="store_true", help="send the CSP handshake without the «requires WeatherFX» flag")
    args = a.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    loop = asyncio.get_running_loop()
    udp = UdpRelay(args.udp)
    await loop.create_datagram_endpoint(lambda: udp, local_addr=("0.0.0.0", args.public))
    tcp = TcpRelay(args.tcp, args.public, inject_after=None if args.inject_after == "none" else args.inject_after, min_csp=args.min_csp,
                   weather_fx=not args.no_weather_fx_flag)
    await asyncio.start_server(tcp.handle, "0.0.0.0", args.public)
    await asyncio.start_server(http.handler(args.http, args.public, args.public, args.public + 1), "0.0.0.0", args.public + 1)
    plan = Plan([(float(s), int(t)) for t, s in (x.split(":") for x in args.plan.split(","))], args.transition)
    cond = Conditions(plan, ambient=args.ambient)
    log.info("relay up: public %s (tcp+udp) / %s (http) -> acServer tcp %s udp %s http %s; plan %s", args.public, args.public + 1, args.tcp, args.udp, args.http, args.plan)
    tick = 0
    while True:
        await asyncio.sleep(1)
        s = cond.step(1.0)
        n = udp.inject(p.weather_update(unix=int(time.time()), current=s["current"], upcoming=s["upcoming"], transition=s["transition"], ambient=s["ambient"],
                                        road=s["road"], grip=s["grip"], rain=s["rain"], wetness=s["wetness"], water=s["water"]))
        tick += 1
        if tick % 10 == 0:
            udp.reap()
            log.info("t=%ds weather %s->%s %.2f rain %.2f wet %.2f water %.2f | clients %d (past handshake %d) tcp conns %d injected %d udp in/out %d/%d", cond.t,
                     s["current"], s["upcoming"], s["transition"], s["rain"], s["wetness"], s["water"], len(udp.clients), n, tcp.n_conns, tcp.n_injected, udp.n_in, udp.n_out)


if __name__ == "__main__":
    asyncio.run(main())
