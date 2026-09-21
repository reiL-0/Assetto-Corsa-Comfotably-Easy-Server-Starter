"""Lectura de la telemetria del auto local.

Usa el modulo `ac` para pedales / marcha / rpm / velocidad / posicion / steer,
y `opr_mmap` para la orientacion. Devuelve el dict ya con el esquema exacto
que espera `POST /api/telemetry/ingest`, o None si no estamos en un auto.
"""
try:
    import ac
    import acsys
except ImportError:  # fuera de AC (para poder importar el modulo en dev)
    ac = None
    acsys = None

import opr_mmap


def _r(v, n):
    return round(v, n)


def read(car_id=0):
    """Payload listo para enviar, o None si getCarState no devuelve nada util
    (menus, repeticion sin auto, etc.)."""
    if ac is None:
        return None

    pos = ac.getCarState(car_id, acsys.CS.WorldPosition) or (0.0, 0.0, 0.0)
    px, py, pz = pos[0], pos[1], pos[2]
    speed = ac.getCarState(car_id, acsys.CS.SpeedKMH) or 0.0

    if px == 0.0 and py == 0.0 and pz == 0.0 and speed == 0.0:
        return None

    gas = ac.getCarState(car_id, acsys.CS.Gas) or 0.0
    brake = ac.getCarState(car_id, acsys.CS.Brake) or 0.0
    clutch = ac.getCarState(car_id, acsys.CS.Clutch)
    if clutch is None:
        clutch = 1.0  # 1.0 = embrague suelto en AC
    gear = ac.getCarState(car_id, acsys.CS.Gear) or 0       # 0=R, 1=N, 2=1a... (igual que ACSP)
    rpm = ac.getCarState(car_id, acsys.CS.RPM) or 0.0
    steer = ac.getCarState(car_id, acsys.CS.Steer) or 0.0   # grados

    hpr = opr_mmap.heading_pitch_roll()
    heading, pitch, roll = hpr if hpr is not None else (0.0, 0.0, 0.0)

    return {
        "pos": {"x": _r(px, 3), "y": _r(py, 3), "z": _r(pz, 3)},
        "rotation": {"x": _r(heading, 4), "y": _r(pitch, 4), "z": _r(roll, 4)},
        "speedKmh": _r(speed, 1),
        "gear": int(round(gear)),
        "rpm": int(round(rpm)),
        "throttle": _r(gas, 3),
        "brake": _r(brake, 3),
        "clutch": _r(clutch, 3),
        "steerAngle": _r(steer, 2),
    }


def shutdown():
    opr_mmap.close()
