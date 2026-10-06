# app/relay — relé delante de acServer (prueba de viabilidad: clima de CSP dirigido por el servidor)

**Purpose:** acServer de Kunos no puede enviar a los clientes las condiciones de clima de Custom Shaders Patch (lluvia, mojado, transiciones), que son las que
mueven RainFX y la física. AssettoServer lo hace con un paquete UDP extendido y un apretón de manos CSP por TCP. Este paquete es un **relé** que se pone entre
los pilotos y un acServer sin modificar, y añade esos mensajes. Es un experimento (`python -m app.relay`), no está conectado al manager.

## Files
- `protocol.py` — formatos (leídos del código de AssettoServer, **sin verificar con un cliente real**): `frame`/`split_frames` (TCP: longitud u16 LE + carga, el primer byte es el id), `weather_update` (UDP: `0xAB 0x01` + hora, tipo actual y siguiente, transición u16, 10 `Half`: ambiente, asfalto, agarre, viento (°, km/h), humedad, presión, lluvia, mojado, charcos), `csp_handshake_in` (TCP: build mínimo de CSP y «WeatherFX dirigido por el servidor»), `parse_weather_update`. Constantes de ids y los tipos de WeatherFX.
- `weather.py` — `Plan` (pasos `(segundo, tipo)` con transición suave) y `Conditions.step(dt)`: intensidad de lluvia por tipo, mojado y charcos que suben con la lluvia y bajan al secarse, agarre que baja con el mojado.
- `udp.py` — `UdpRelay` (contesta él mismo el ping del lobby `0xC8` con el puerto HTTP **público**: si no, acServer contestaría con su puerto interno y Content Manager se saltaría el relé; probado así en la primera prueba): un socket público; cada cliente tiene su socket hacia el UDP interno de acServer (acServer ve un endpoint por cliente); las respuestas salen por el socket público (el cliente ve una sola dirección). `inject(paquete)` envía a los clientes que ya mandaron `CAR_CONNECT`; `reap()` cierra los inactivos.
- `tcp.py` — `TcpRelay`: copia cliente→servidor tal cual; servidor→cliente por tramas: reescribe el **puerto UDP** de la respuesta de apretón de manos (acServer anuncia el interno) y añade el apretón de manos CSP justo después de él o de la lista de autos (`inject_after`).
- `http.py` — la página del lobby: `/INFO` y `/JSON|…` desde el HTTP interno con `port`, `tport` y `cport` cambiados por los del relé.
- `__main__.py` — la línea de comandos: `--public 9680 --tcp 9690 --udp 9691 --http 9709 --plan "15:0,7:60,15:240" --transition 30` (acServer en los puertos internos; los pilotos usan `--public` para TCP+UDP y `--public + 1` para el lobby; el clima se envía cada segundo).

## Interactions
- **Llama a:** acServer por los puertos internos (TCP, UDP, HTTP en `127.0.0.1`).
- **Lo prueba:** `static/csp/opr_weather_probe.lua` (OPR WP), que con `WATCH = 1` lee las condiciones en el cliente y las manda a `/api/csp-probe`.
- **No lo usa nadie más** todavía: ni el manager ni el supervisor. Si la prueba sale bien, se integra como un servicio opcional por servidor (el `Waker` y `wake.py` ya hacen de intermediario del HTTP).
- **Pruebas:** `tests/test_relay.py` (paquetes, plan, relé UDP con un acServer simulado, relé TCP).

## Lo aprendido en las pruebas (2026-10-06)
- Entrar por el relé funciona (TCP, UDP y lobby); un ping del lobby contestado por acServer destapaba su puerto interno (ahora lo contesta el relé).
- El log de CSP del cliente (`custom_shaders_patch.log`) dice **«Weather FX: operate in fallback mode»** y, con el apretón de manos de CSP inyectado justo tras la respuesta del servidor, **«Requesting car list :: unexpected packet received»**: llegaba antes de tiempo y se ignoraba. AssettoServer lo manda dentro de su «primera actualización» (junto al clima vainilla): por eso `inject_after` es ahora `weather` (tras el primer `WeatherUpdate` del servidor).
- **Corrección:** el «tipo 7» que el script de prueba informó no venía del relé: el acServer de prueba tenía un bloque `7_heavy_clouds_type=7` y lo eligió él al empezar la clasificación (el log del servidor lo muestra). **No hay todavía ninguna prueba de que lo inyectado tenga efecto** (el cliente sigue en «fallback mode», sin lluvia ni mojado). Antes de repetir hay que dejar un único bloque de clima en el servidor de prueba y comparar los bytes con los de un AssettoServer real.

## Lo que la prueba debe responder
1. ¿El cliente acepta el apretón de manos CSP inyectado (cuándo: tras el apretón o tras la lista de autos) y el valor `HANDSHAKE_IN = 0` es el correcto?
2. ¿Acepta paquetes UDP de clima que salen del puerto público del relé?
3. ¿Cambian de verdad la lluvia, el mojado y el agarre en el cliente (se ve en el informe del script de prueba)?
4. ¿Cuánta CPU consume el relé con varios pilotos?
