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
