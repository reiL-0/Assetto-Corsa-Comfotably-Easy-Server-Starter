"""Lectura de la orientacion del auto desde la memoria compartida de AC.

El modulo `ac` expone pedales, marcha, rpm, velocidad y posicion, pero NO la
rotacion del auto. Eso vive en el bloque `acpmf_physics` (SPageFilePhysics).
Aca leemos solo los 3 floats que necesitamos: heading, pitch, roll (radianes).

Offsets de SPageFilePhysics (todos los campos son 4 bytes, sin padding):
    0  int   packetId
    4  float gas
    8  float brake
    12 float fuel
    16 int   gear
    20 int   rpms
    24 float steerAngle
    28 float speedKmh
    32 float velocity[3]
    44 float accG[3]
    56 float wheelSlip[4]
    72 float wheelLoad[4]
    88 float wheelsPressure[4]
    104 float wheelAngularSpeed[4]
    120 float tyreWear[4]
    136 float tyreDirtyLevel[4]
    152 float tyreCoreTemperature[4]
    168 float camberRAD[4]
    184 float suspensionTravel[4]
    200 float drs
    204 float tc
    208 float heading   <-- aca
    212 float pitch
    216 float roll
"""
import struct

try:
    import mmap
except ImportError:  # pragma: no cover
    mmap = None

_TAG = "Local\\acpmf_physics"
_MAP_SIZE = 512          # sobra para llegar al offset 208..220
_OFF_HEADING = 208

_mm = None


def _open():
    global _mm
    if _mm is not None or mmap is None:
        return
    try:
        _mm = mmap.mmap(-1, _MAP_SIZE, _TAG, mmap.ACCESS_READ)
    except (OSError, ValueError, TypeError):
        _mm = None  # fuera de Windows / AC no corriendo (el 3er arg es Windows-only)


def heading_pitch_roll():
    """(heading, pitch, roll) en radianes, o None si la memoria compartida
    no esta disponible."""
    _open()
    if _mm is None:
        return None
    try:
        return struct.unpack_from("<fff", _mm, _OFF_HEADING)
    except (OSError, ValueError, struct.error):
        close()
        return None


def close():
    global _mm
    try:
        if _mm is not None:
            _mm.close()
    except Exception:
        pass
    _mm = None
