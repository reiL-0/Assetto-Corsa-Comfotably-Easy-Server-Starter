"""One-off: fill track_records from the acServer result files already on disk (every lap with no cuts, any session).

    ACM_DATA_DIR=<data dir> python ops/backfill_records.py [results dir or file ...]     (default: <data dir>/instances/*/results)

Run it with the manager's own environment (its .env) so it writes to the real database. Safe to repeat: a record is only replaced by a better lap.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import records   # noqa: E402
from app.config import settings   # noqa: E402


def laps(path: Path):
    d = json.loads(path.read_text())
    for lap in d.get("Laps") or []:
        yield d.get("TrackName"), d.get("TrackConfig"), lap


def main(args: list[str]) -> None:
    roots = [Path(a) for a in args] or sorted((Path(settings.data_dir) / "instances").glob("*/results"))
    files = sorted(f for r in roots for f in ([r] if r.is_file() else r.glob("*.json")))
    seen = kept = 0
    for f in files:
        try:
            rows = list(laps(f))
        except (OSError, ValueError):
            print("skipped (unreadable):", f.name)
            continue
        for track, config, lap in rows:
            seen += 1
            kept += records.submit(track, config, lap.get("CarModel") or "", str(lap.get("DriverGuid") or ""), lap.get("DriverName") or "", int(lap.get("LapTime") or 0), int(lap.get("Cuts") or 0))
    print(f"{len(files)} files, {seen} laps read, {kept} records written")


if __name__ == "__main__":
    main(sys.argv[1:])
