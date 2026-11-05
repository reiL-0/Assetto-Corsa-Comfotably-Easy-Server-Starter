# Neutralización automática: fases 3–6

Las reglas se leen de `Server.session.rulebook_snapshot`, aplicado al arrancar la
sesión del evento. Editar el reglamento de la liga no modifica esa sesión.
`neutralization.mode` admite `off`, `shadow` y `live`. El director puede desactivar
el automatismo con `POST …/race-control/autoflags` y
`{"kind":"neutralization","off":true}`.

## Verde tras VSC/FCY

El monitor mantiene un intervalo de pista limpia con telemetría reciente. Una
velocidad de parada (`STOP_KMH`, también utilizado por spin), una velocidad por
debajo de `slow_car.pace_ratio` respecto a la mediana de los otros coches en
movimiento, o datos ausentes/viejos interrumpen el intervalo inmediatamente: no se
espera a que venza el tiempo de confirmación de un incidente. Los coches en boxes,
el pace car y los miembros que no compiten quedan fuera de la comprobación.

Los paquetes `lap_completed` usan el flujo existente `auto_green` →
`RaceControl.green_if_due`. La primera pasada del líder completa una vuelta
parcial; las siguientes cuentan vueltas completas. Solo cuenta el líder actual,
fuera de boxes, en carrera y con telemetría reciente. Un cambio de líder,
generación o anclaje de posición descarta la vuelta parcial; no concede vueltas.

Con `green_after_laps` vueltas completas y `green_clear_s` segundos limpios, el
modo `live` pone verde. Una pista sucia retrasa la decisión hasta la siguiente
pasada válida del líder. Al llegar a `green_after_laps + green_max_extra_laps`
se avisa una sola vez al director, sin forzar verde. Se sigue comprobando en las
vueltas posteriores. En `shadow` la decisión «habría vuelto a verde en vuelta N»
se guarda una sola vez en `autoflags.log`, sin bandera ni notices visibles.

El verde manual cancela todos los contadores. Una nueva VSC/FCY los reinicia,
incluso si sustituye otra neutralización. Las salidas detrás de SC físico conservan su procedimiento; el verde virtual
entrega las acciones al adaptador NASCAR existente cuando ese motor está armado.

## Tope de sanciones automáticas

`neutralization.max_auto_time_penalty_s` es un entero entre 1 y 60 (10 por defecto,
también en snapshots antiguos). Se aplica independientemente de `mode` y del kill
switch de neutralización. Los avisos automáticos de cuts y las sanciones
automáticas give_back se limitan al tope aplicado; el motivo conserva
`recortada de X a Y s`. Confirmar un aviso de cuts conserva esa propuesta y su
anotación. El contador de cuts y la calibración muestran también el máximo efectivo.

Las sanciones explícitas del director, incluidas las give_back ordenadas a mano,
conservan su valor y autor. Los jueces de ritmo/delta/adelantamientos bajo VSC/FCY
actualmente solo imponen drive-through o registran una infracción; no imponen
segundos y no cambian. Comparten `_pen_cars` por coche, generación y bandera, con
lo que dos jueces no duplican la sanción de esa neutralización.

No hay migración de base de datos ni cambio de `schema_version`. Se añade el campo
opcional del reglamento; las formas HTTP permanecen iguales, pero `cuts_rule.cap`
y el máximo de cuts en la calibración reflejan el menor de ambos topes.

Para resultados, el snapshot solo se utiliza si `rulebook_league_id` coincide con la liga del resultado; de lo contrario se usa `effective(c)`. No se persiste el momento de aplicación, por lo que no se distinguen eventos antiguos de la misma liga (límite `ponytail:` en `app/stewards/auto.py`).


## Safety car por accidente muy grave (fase 6)

Sin motor NASCAR armado, `severe.cars` del snapshot aplicado selecciona SC.
En `shadow` solo queda la decisión en el log, sin comandos ni avisos visibles.
En `live`, con pace car configurado y telemetría reciente en esta sesión, se
reutiliza `RaceControl.set_flag("yellow", field=...)`: es la misma ruta del
panel de bandera amarilla sin NASCAR. El hub existente lee `pacecar.mode=lead`.
No se modifican Lua/CSP ni la IA. Sin pace car disponible se mantiene la bandera
y se avisa «SC recomendado, sácalo a mano».

Solo se actúa en carrera, fuera de formación, después de observar movimiento
y parada sostenida con datos recientes. Puede escalar una VSC/FCY iniciada por
este automatismo; respeta las banderas del director y no repite un SC activo.
El procedimiento existente cuenta las vueltas del pace car: automático de 3–6
vueltas, aviso si la pista sigue ocupada, `to go`, entrada a boxes y verde al
cruzar el líder de relanzamiento. NASCAR conserva su detector y procedimiento.

No se añaden temporizadores. El verde manual descarta la caution existente y
cancela su relanzamiento. `SC_INCIDENT_COOLDOWN_S` (120 s desde el final del SC)
impide encadenar despliegues; el detector conserva además su deduplicación por
incidente y ventana. El kill switch bloquea el despliegue y los pasos automáticos
de relanzamiento de este SC (también al cambiar mode fuera de live); el director
conserva el control manual. Las pruebas usan dobles, no validan conducción real
dentro de Assetto Corsa.
Un verde con el pace car fuera y sin «to go» mantiene `pacecar.mode=in` al entrar en boxes y después de aparcar, para que el hub complete el retorno y conserve el freno. Un nuevo yellow, `set_pace_car`, cambio de sesión/nueva carrera o reset de estado cancela este retorno independiente, sin activar el relanzamiento NASCAR.

## Auditoría de control de carrera

El gestor escribe `<ACM_DATA_DIR>/race-audit/events.jsonl` (por defecto
`data/race-audit/events.jsonl`). Un único archivo incluye todos los servidores;
rota a los 10 MiB, con diez copias `.1`…`.10` (unos 110 MiB retenidos).
Directorio 0700 y archivos 0600. El servicio instalado en `ops/install.sh` usa
`ACM_DATA_DIR` y la unidad `acm`; el journal se consulta con
`journalctl -u acm --since today | rg 'race audit'`. Staging tiene su propio
directorio, definido en `ops/staging_setup.sh`.

Cada línea es un objeto JSON con `ts` UTC ISO 8601, `monotonic` (reloj local del
proceso), `server_id`, `session` (tipo/nombre cuando se conocen), `kind` y evidencia.
Los eventos de RaceControl incluyen `rulebook_revision`, `mode`, slots y su mapa
`guids`, sin credenciales. Los resultados usan el guid y el nombre del archivo.
`session` o la revisión pueden ser nulos si todavía no se conoce el snapshot.
No hay muestras continuas de telemetría: las velocidades aparecen al decidir y
aproximadamente cada cinco segundos durante una neutralización.

`neutralization_decision` describe la evaluación (`executed=false`);
`neutralization_applied` confirma la actuación live (`executed=true`).
`neutralization_gate` registra cambios de modo, kill switch y coches con datos
viejos; no simula una decisión contrafactual con datos descartados.
`neutralization_discard` explica cooldown o pace car ausente. `flag` conserva
`source` (director, incident, neutralization, NASCAR, formación), parámetros y
acciones. `state_change` permite reconstruir bandera, pace car, `pace_park`,
procedimiento caution y sanciones en memoria. Los jueces guardan la decisión y
si se aplicó; las banderas automáticas guardan el modo efectivo. `executed` en
una bandera automática live indica habilitación para servirla al hub, no prueba
que el cliente la haya mostrado.

El verde registra vueltas contadas, pista limpia, estado periódico, decisión y
extensión/aviso al director. Los eventos give_back y cuts conservan obligaciones,
ventanas y contadores. Las propuestas de sanciones de resultados terminan en
`_staged`, con `persistence=pending_commit` y valor original/final/tope; al contar
resultados, el commit exitoso produce `_created`, `persistence=committed`.
Las sanciones explícitas, los perdones y descartes también se registran.
El JSONL no es una transacción conjunta con la base de datos.

La escritura usa un hilo único y una FIFO de 4096 eventos; el productor no accede
al disco ni a la red. La cola saturada se descarta con contador antes de copiar o serializar. La evidencia
se copia con límites de 256 nodos, 8192 caracteres totales, 512 por cadena,
32 elementos por contenedor y profundidad 6; los recortes llevan `[truncated]`.
El escritor serializa esa copia acotada. Si un paso supera las 200 decisiones
retenidas por el historial de banderas, se escribe `audit_gap` con `lost`. `raceaudit.stats()` y la respuesta HTTP exponen `queued` (admitidos),
`written`, `errors`, `dropped` y `pending`. Un error de escritura no llega al
control de carrera; el siguiente evento reintenta abrir el archivo. El apagado
normal espera hasta ocho segundos; una terminación abrupta o disco bloqueado
pueden perder eventos pendientes. Un proceso gestor por directorio, como el resto
del control de carrera; no coordina rotación entre varios procesos.

Lectura con los mismos permisos de race-control (steward/admin):
`GET /api/v1/servers/{id}/race-control/audit?since=2026-11-05T00:00:00Z&limit=100`.
Si falla la lectura, devuelve `read_error` y conserva los contadores.
Devuelve `{events, stats}`, últimos eventos en orden de escritura; `since` es
inclusivo y `limit` admite 1–1000. La lectura no espera a la cola y puede omitir
los eventos aún pendientes. Recorre archivos retenidos desde el más reciente.

```sh
# Seguir un servidor en vivo
tail -F "$ACM_DATA_DIR/race-audit/events.jsonl" | jq -c 'select(.server_id == 2)'
# Decisiones, evidencia y ejecución; incluir las copias rotadas si hace falta
jq -c 'select(.server_id == 2 and (.kind | startswith("neutralization_")))' \
   "$ACM_DATA_DIR/race-audit/events.jsonl"
# Sanciones y topes aplicados
jq -c 'select(has("original_seconds")) | {ts,kind,driver_guid,original_seconds,final_seconds,cap,persistence}' \
   "$ACM_DATA_DIR/race-audit/events.jsonl"
```
