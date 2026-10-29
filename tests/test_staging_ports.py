"""Staging and production can never share a port: staging's range (ACM_PORT_RANGE_START/END=9800/9900, ops/staging_setup.sh) gives blocks and internal HTTP
ports that production's (9600/9700 defaults) never use, and production's own ports do not move."""
from app.services.ini_generator import ports


def used(start: int, end: int) -> set[int]:
    return {p for base in range(start, end, 4) for p in ports(base, start, end).values()}


def test_staging_range_never_meets_production():
    staging, prod = used(9800, 9900), used(9600, 9700)
    assert not staging & prod
    assert min(staging) == 9800 and max(staging) == 9924   # blocks 9800-9899, internal HTTP 9900-9924: what the firewall rule (9800:9899) and the docs say
    assert ports(9600, 9600, 9700)["http_internal"] == 9700   # production unchanged
