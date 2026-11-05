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

## Grabación del campo y calibración sin servidor

Activa antes de empezar la sesión `ACM_FIELD_RECORD_SERVERS=2,7` (IDs del gestor)
o `ACM_FIELD_RECORD_SERVERS='*'` para todos. Vacío o ausente lo desactiva. No hay
migración. Se graba también cuando los automatismos están apagados; para probar
incidentes después, pasa `neutralization.mode=shadow` en las reglas de replay.

Los archivos están en `ACM_DATA_DIR/race-audit/field/`, separados por servidor y
sesión: `server-{id}-session-{inicio_unix_ms}-{uuid}-{parte}.jsonl`. Cada parte
tiene un tope de 20 MiB; no se sobrescriben partes anteriores. La retención total
es de 200 MiB y elimina primero los archivos **cerrados** más antiguos por mtime.
El escritor cierra la sesión anterior de un servidor al recibir una nueva, y
cierra una parte al alcanzar el tope. Nunca elimina un archivo abierto de otro
servidor; si solo quedan archivos abiertos y no hay espacio, cuenta un error y
descarta esa escritura. El cierre normal cierra todos. Se guardan JSONL sin
comprimir; para archivar usa gzip sobre archivos cerrados. Copia la serie con el
gestor detenido para obtener una captura estable. La misma
cola FIFO de 4096 entradas y el mismo worker de `raceaudit` escriben ambos
registros, con permisos 0600/0700. El productor no hace I/O de archivos ni espera
espacio en la cola. `fieldrec.stats()` y los `stats` del endpoint de auditoría
exponen `queued`, `written`, `errors`, `dropped` y `pending`, compartidos con la
auditoría. Saturación y fallos de disco pierden entradas; `close()` drena ambos
registros durante el shutdown, con el mismo timeout existente.

El formato `acm-field-v1` es JSONL: cada línea es una entrada de un detector o un
evento, con `server_id`, `session` (identificador opaco por instancia de carrera),
`session_type`, `seq`, `ts` (segundos Unix), `monotonic`, `flag`, `in_pit`, `kind`,
`context` y `args`. `kind` identifica el método real de `RaceControl`. Los campos
`args.field.$field` o `args.samples` contienen muestras por slot:
`$sample` guarda `progress`, `stamp`, `since`, `epoch`, `generation` y
`completed_laps`; `connected:true` señala los slots presentes. La ausencia de un
slot en el siguiente campo es una desconexión; una reconexión conserva su nueva
generación. `args.speeds` contiene km/h por slot. Los informes de pitlane se
conservan con su hora original, para reproducir sus caducidades de 4/30 s.
`in_pit` es la vista de 30 s, informativa; el replay aplica los informes reales.

Se conservan formación, pace car, bandera y configuración de VSC/FCY, cruces de
línea, contactos e informes de excursiones de give_back. Los argumentos y las
reglas se copian antes de ejecutar la entrada, y las llamadas internas no se
graban dos veces: el reproductor deja que el código real las ejecute. No se
copian nombres, tokens ni GUIDs: para seguir al pace car basta conservar si hay
un conductor configurado, junto al slot. El monitor actual consume entradas a
4 Hz; se conserva esa cadencia para no perder reinicios de temporizadores ni
muestras intermedias. Una reducción a 2 Hz cambiaría los datos vistos por los
jueces y no garantiza las mismas decisiones.

Ejemplos desde la raíz del checkout, usando el entorno Python del gestor:

```sh
# Directorio: lee archivos por mtime; filtra un servidor.
python ops/replay_field.py /ruta/data/race-audit/field --server-id 2 \
  --rules '{"neutralization":{"mode":"shadow"}}' > replay.json

# Un archivo JSONL o JSONL.gz; reglas parciales sobre las grabadas.
python ops/replay_field.py carrera.jsonl.gz \
  --rules '{"neutralization":{"mode":"shadow","spin":{"stopped_s":7}},"detectors":{"give_back":{"enabled":true,"window_s":30,"seconds":5}}}'

# También acepta --rules reglas.json. Varias opciones --sweep forman un producto.
python ops/replay_field.py /ruta/data/race-audit/field --server-id 2 \
  --rules '{"neutralization":{"mode":"shadow"}}' \
  --sweep slow_car.pace_ratio=0.5,0.6,0.7 --sweep spin.stopped_s=5,7,10

# Calibración del juez continuo de ritmo.
python ops/replay_field.py carrera.jsonl --sweep detectors.pace.sustained_s=1.5,2,3
```

Stdout contiene un único JSON con `summary` y `decisions` (`kind`, `ts`,
`monotonic`, coches y evidencia). Un barrido devuelve `sweep`, con parámetros,
resumen y decisiones por combinación. Las reglas pasan por los mismos modelos
del reglamento. Como ajuste exclusivo del replay, `detectors.pace` admite
`sustained_s`, `max_gap_s` y `fresh_s` positivos: aplica y restaura las constantes
del juez real durante cada pasada; no cambia el reglamento ni el gestor activo. Se importan los jueces y `RaceControl`, sin arrancar
FastAPI, procesos, red ni base de datos. El reloj simulado avanza con cada entrada;
los IDs de decisiones se reinician de forma determinista y se restauran al salir.

El replay reproduce entradas observadas: cambiar umbrales puede cambiar las
banderas calculadas, pero no inventa cómo habrían conducido los pilotos bajo esas
banderas. Usa `shadow` para comparar detección sobre la misma carrera. Empieza a
grabar antes de la sesión: no se reconstruye el historial previo. `summary.gaps`
señala saltos en `seq` (por descarte o rotaciones perdidas), y `complete:false`
marca también capturas que empiezan ya bajo cautela. La última línea incompleta y un gzip truncado conservan las líneas completas
anteriores: avisan por stderr y el CLI incluye `summary.discarded`, con recuento
de fragmentos/streams descartados, y `complete:false`. Los errores en líneas
completas siguen siendo errores. Los recortes de presupuesto causan
`truncated_input` y `complete:false`; no se ejecuta esa entrada parcial.
Una pérdida de eventos completos al final solo se detecta con los contadores
del grabador. El motor NASCAR
armado **no está soportado**: el replay aborta con error, incluso en práctica o
clasificación, si alguna entrada seleccionada tiene `nascar:true`. Desarma NASCAR
antes de iniciar la grabación. No se implementa su replay en este cambio; los jueces de
neutralización, verde virtual, ritmo/delta, adelantamientos y give_back utilizan
su implementación real. Las llamadas grabadas usan un reloj por instancia fijado al instante de entrada,
incluidas sus llamadas internas; el replay entrega exactamente esos dos relojes.


### Presupuesto y tamaño del campo

Antes de copiar argumentos se comprueba la cola: llena implica un descarte y
cero llamadas a encode. Bajo el lock solo se hace la copia acotada y se conserva
el orden/reloj; el escritor hace `json.dumps`. Cada snapshot admite 4096 nodos,
32768 caracteres totales, 512 por cadena, 64 elementos por contenedor y
profundidad 10, con `[truncated]`. El campo se copia hasta 64 coches sin construir
primero una lista ilimitada. Estos límites conservan íntegra la entrada normal
de 24 coches. Las secuencias avanzan también con descartes.

Estimación sintética con **24 coches a 4 Hz**, cinco entradas por tick
(incident, autoflags, pass, pace y give_back), reglas de neutralización completas,
modelos de coche, velocidades y progresos variables: un minuto produjo 1200
entradas; extrapolado son **295 MiB/h sin comprimir y 13 MiB/h con gzip nivel 6**.
Para planificación, reserva **15–30 MiB comprimidos por hora y servidor**;
incidentes, cruces, pitlane y mayor variación aumentan el tamaño. No es una
medición de carrera real ni un límite garantizado de compresión. El límite de
disco efectivo sigue siendo 200 MiB globales sin comprimir: con esa carga retiene
aproximadamente 40 minutos para un servidor, menos con varios. Archiva partes
cerradas fuera de ese directorio si necesitas conservar una carrera completa.

### Inventario de entradas y exclusiones

Auditoría de llamadores en `app/`, incluidos routers: `app/racecontrol.py`
(monitor, cruces y endpoints), `app/live/acsp.py` (contactos/configuración) y
`app/live/racecontrol.py` (tick, excursiones y cuts). `fieldrec.METHODS` y
`fieldrec.EXCLUDED` clasifican **todos** los métodos públicos de RaceControl;
la prueba estructural exige clasificación y decorador para cada entrada.

| Métodos | Tratamiento / llamadores |
|---|---|
| incident_step, neutralization_clear_tick, green_if_due, pace_tick, pass_tick, autoflags_tick, give_back_tick | Grabados; monitor y cruce de línea del adaptador |
| sample, evaluate | Grabados; tick del juez de gracia en live/racecontrol |
| give_back_report, contact | Grabados; on_excursion y ACSP |
| give_back_forgive, give_back_due_now, give_back_restore | Grabados; endpoints/monitor. Las obligaciones preentrada incluyen ID original, coche y t0; replay remapea por sesión al ID que creó, también para rollback. Un ID sin correspondencia se omite con aviso, nunca se aplica a otra obligación. |
| report_pit, set_flag, set_pace_car, follow_pace_driver, set_pace_config, pit_exception | Grabados; endpoints, hub y monitor |
| start_formation, formation_lap, formation_green, caution_lap, caution_tick, restart_plan, stage_break | Grabados; cruces, monitor y endpoints de procedimiento |
| configure_nascar | Excluido: arma el motor explícitamente no soportado; ACSP/endpoints. on_lap y finish_tick mutan solo ese motor y también quedan fuera del alcance. |
| notify, drop_hold, take_blocked_warning | Excluidos: presentación y consumo de avisos; no alimentan decisiones de jueces |
| add_penalty, clear_penalty | Excluidos: ledger manual de drive-through, no entrada de detectores; las sanciones automáticas se regeneran dentro de los jueces |
| add_cuts | Excluido: contador independiente, no consumido por los jueces del campo; on_cuts |
| audit | Excluido: salida; replay instala su colector |
| without_pace_car, pace_car_state, caution_eta, grace_left, due, auto_for, auto_live, judging_continuously, delta_block, autoflags_view, give_back_peek, give_back_view, cuts_view, snapshot | Solo lectura/filtros; panel, hub y llamadas internas |

Las asignaciones externas de reglas/kill switches/pace/configuración y formación
se capturan en `context` antes de cada entrada; incluye `flag_modes` del monitor.
Nombres, GUIDs, propiedad de sanciones y persistencia HTTP no se reproducen: el
monitor ya transforma cambios de propietario en give_back_forgive grabado.
