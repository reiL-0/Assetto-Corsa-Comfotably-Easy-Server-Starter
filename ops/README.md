# ops — vigilancia del manager (fuera del manager)

**Purpose:** avisar al Discord de la liga cuando algo falla, desde un proceso aparte (un manager caído no puede avisar de sí mismo).

## Files
| Archivo | Qué es |
|---|---|
| `watchdog.py` | Script (solo stdlib) que corre cada 2 min. Comprueba `manager` (`/healthz`), `backup` (el `status.json` de `opr-backup`: último respaldo ok, subida a Drive ok, no más viejo de 36 h), `disk` (partición al ≥ 85 %) y las URLs extra de `WATCHDOG_URLS`. Un fallo se anuncia tras 2 corridas seguidas (sin ruido por reinicios), se repite cada 6 h mientras siga y se avisa «se recuperó». Estado en `/var/lib/acm-watchdog/state.json`. `python3 ops/watchdog.py --selftest`. |
| `acm-watchdog.service` / `.timer` | Unidades de systemd (root, `oneshot`). Leen `/etc/acm.env` (de ahí sale `ACM_DISCORD_STATUS_WEBHOOK`, el mismo webhook de «servidor encendido/apagado») y, si existe, `/etc/acm-watchdog.env` para ajustar las variables `WATCHDOG_*`. |

## Variables (todas opcionales)
`WATCHDOG_MANAGER` (`http://127.0.0.1:8080`), `WATCHDOG_BACKUP_STATUS`, `WATCHDOG_DISK_PATH` (`/`), `WATCHDOG_DISK_PCT` (`85`), `WATCHDOG_URLS` (`sitio=http://127.0.0.1:5100/,tunel=http://10.8.0.2:8080/healthz`; para vigilar el túnel de CDMX basta agregar su otro extremo), `WATCHDOG_STATE`.

## Instalar (una vez; `deploy.sh` no copia `ops/`)
    scp ops/watchdog.py root@VPS:/opt/acm/watchdog.py
    scp ops/acm-watchdog.service ops/acm-watchdog.timer root@VPS:/etc/systemd/system/
    ssh root@VPS 'systemctl daemon-reload && systemctl enable --now acm-watchdog.timer'

## Interactions
- **Lee:** `/healthz` del manager, `status.json` del respaldo (`OPR WP/ops/backup.py`), el disco. **Escribe:** su estado y un POST al webhook de estado de Discord.
- Si el POST a Discord falla no guarda que avisó: reintenta en la siguiente corrida.
