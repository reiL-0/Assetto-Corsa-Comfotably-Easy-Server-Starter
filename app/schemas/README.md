# app/schemas — pydantic schemas of the API

**Purpose:** the bodies the panel and the calendar send and the shapes the manager answers with. Pure data: no I/O, no manager state.

- `servers.py`: `ServerIn/Out`, `SessionIn` (+ `OptionsIn`, `WeatherIn`, `DynamicTrackIn`, `EntryIn`), `AppliedOut`, and the small bodies of the weather / CSP / limits / wake / chat / admin-command routes. Moved out of `app/servers.py` unchanged.

**Interactions:** `app/servers.py` re-exports every name (`from app.servers import SessionIn` still works for `league`, `events`, `stewards`, tests); `app/services/ini_generator.py` uses `Scalar`.
