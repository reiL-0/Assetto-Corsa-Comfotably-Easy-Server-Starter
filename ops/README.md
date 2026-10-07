# ops — instalar y vigilar el manager (fuera del manager)

**Purpose:** dejar el manager instalado de forma repetible en una máquina nueva y avisar al Discord de la liga cuando algo falla, desde un proceso aparte (un manager caído no puede avisar de sí mismo).

## Files
| Archivo | Qué es |
|---|---|
| `watchdog.py` | Script (solo stdlib) que corre cada 2 min. Comprueba `manager` (`/healthz`), `backup` (el `status.json` de `opr-backup`: último respaldo ok, subida a Drive ok, no más viejo de 36 h), `disk` (partición al ≥ 85 %) y las URLs extra de `WATCHDOG_URLS`. Un fallo se anuncia tras 2 corridas seguidas (sin ruido por reinicios), se repite cada 6 h mientras siga y se avisa «se recuperó». Estado en `/var/lib/acm-watchdog/state.json`. `python3 ops/watchdog.py --selftest`. |
| `acm-watchdog.service` / `.timer` | Unidades de systemd (root, `oneshot`). Leen `/etc/acm.env` (de ahí sale `ACM_DISCORD_STATUS_WEBHOOK`, el mismo webhook de «servidor encendido/apagado») y, si existe, `/etc/acm-watchdog.env` para ajustar las variables `WATCHDOG_*`. |

| `install.sh` | Instalador idempotente para una máquina limpia (Debian 13 / Ubuntu 24.04): paquetes, usuario `acm` con linger (los límites de CPU/RAM por servidor corren en su systemd de usuario), carpetas, `app/` + venv, `/etc/acm.env` (se crea vacío, **nunca se sobrescribe**), `acm.service` (con `KillMode=process` para que los acServer sobrevivan a un reinicio, `Nice=-5` y, si se pide, `CPUAffinity`), el vigilante y su temporizador; opcional `--ufw` (ssh + 9600-9699 tcp/udp, sin 80/443 porque la web va por túnel), `--db` (restaura la base del respaldo, se niega a pisar una existente) y `--no-start`. `DRY=1` solo imprime. Espera `/healthz` al final. **No trae** el binario `acServer` ni `content/` (los pones tú) ni los secretos. |

## Máquina nueva (migración, reconstrucción)
1. `git clone` del manager y `sudo ops/install.sh --ufw` (antes, `DRY=1` para ver qué hará).
2. Llenar `/etc/acm.env` con los secretos de tu gestor de contraseñas (no van en el respaldo).
3. Poner `acServer` y `content/` en `/opt/acserver` (los mods salen del catálogo: nombre + enlace oficial).
4. Restaurar los datos: `restore.py` del respaldo cifrado (ver `OPR WP/ops`) y `--db .../db/manager.db`, y copiar los `results/` de cada instancia.
5. `systemctl restart acm`. Las ligas, eventos, tokens y sanciones vuelven con la base.
- Lo que **no** automatiza: la web y su base, `cloudflared`/WireGuard y el DNS (van con la migración de CDMX).

## Variables del vigilante (todas opcionales)
`WATCHDOG_MANAGER` (`http://127.0.0.1:8080`), `WATCHDOG_BACKUP_STATUS`, `WATCHDOG_DISK_PATH` (`/`), `WATCHDOG_DISK_PCT` (`85`), `WATCHDOG_URLS` (`sitio=http://127.0.0.1:5100/,tunel=http://10.8.0.2:8080/healthz`; para vigilar el túnel de CDMX basta agregar su otro extremo), `WATCHDOG_STATE`.

## Instalar solo el vigilante (en una máquina que ya tiene el manager; `deploy.sh` no copia `ops/`)
    scp ops/watchdog.py root@VPS:/opt/acm/watchdog.py
    scp ops/acm-watchdog.service ops/acm-watchdog.timer root@VPS:/etc/systemd/system/
    ssh root@VPS 'systemctl daemon-reload && systemctl enable --now acm-watchdog.timer'

## Interactions
- **Lee:** `/healthz` del manager, `status.json` del respaldo (`OPR WP/ops/backup.py`), el disco. **Escribe:** su estado y un POST al webhook de estado de Discord.
- Si el POST a Discord falla no guarda que avisó: reintenta en la siguiente corrida.
