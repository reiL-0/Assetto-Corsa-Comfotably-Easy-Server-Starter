# clients — programas que hablan con el manager

| Elemento | Qué es |
|---|---|
| `OPRTelemetry/` | App Python que corre **dentro** de Assetto Corsa y envía la telemetría del auto local al backend (README propio). |
| `probe.py` | Prueba el endpoint sin abrir AC: envía telemetría sintética (un auto en círculos) a `POST /api/v1/telemetry/ingest` usando el mismo `opr_sender.Sender`. `python clients/probe.py --url http://localhost:8080 --steam-id 7656… --hz 8`. |
