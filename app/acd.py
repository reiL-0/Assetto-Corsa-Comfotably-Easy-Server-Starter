"""data.acd: the car's `data/` folder packed the way Kunos (and Content Manager) do it, which is what acServer loads and what the players' checksum covers.

Flat list of entries: int32 name length, name, int32 size, then each byte as an int32 (byte + key[i % len(key)]) & 0xFF. The key comes from the car's FOLDER name.
Checked against the 128 data.acd of the league's server (decrypts all of them), so `key` is the real algorithm."""
import shutil
import struct
from pathlib import Path


MAX_PACK = 32 << 20   # a real car's data/ is a few hundred KB


def _w(x: int) -> int:   # C# int32 wrap-around (the key's divisions need the value as the game's int, not a Python bignum)
    return (x + 2**31) % 2**32 - 2**31


def key(folder: str) -> bytes:
    s = [ord(c) for c in folder]
    n = len(s)
    a = sum(s)
    b = 0
    for i in range(0, n - 1, 2):
        b = _w(_w(b * s[i]) - s[i + 1])
    c = 0
    for i in range(1, n - 3, 3):
        c = _w(int(_w(c * s[i]) / (s[i + 1] + 0x1B)) - 0x1B - s[i - 1])
    d = 0x1683
    for i in range(1, n):
        d -= s[i]
    e = 0x42
    for i in range(1, n - 4, 4):
        e = _w((s[i] + 0xF) * e * (s[i - 1] + 0xF) + 0x16)
    f = 0x65
    for i in range(0, n - 2, 2):
        f -= s[i]
    g = 0xAB
    for i in range(0, n - 2, 2):
        g %= s[i]
    h = 0xAB
    for i in range(n - 1):
        h = _w(int(h / s[i]) + s[i + 1])
    return "-".join(str(v & 0xFF) for v in (a, b, c, d, e, f, g, h)).encode()


def order(name: str) -> str:
    """Where Content Manager puts a file in the pack. It does not sort: it writes what `Directory.GetFiles` returns (AcTools/AcdFile/Acd.cs, FromDirectory), and NTFS lists names by their UPPER-CASE code units
    (so `_` comes after every letter: `flames.ini` before `flame_presets.ini`, `__cm_*` last). Same bytes as theirs = the pilot's own data.acd has the same checksum as the server's.
    Matches 128 of the 129 real data.acd we hold (the odd one out was packed by something else)."""
    return name.upper()


def pack(files: dict[str, bytes], folder: str) -> bytes:
    k = key(folder)
    out = bytearray()
    for name, data in files.items():
        nb = name.encode()
        out += struct.pack("<i", len(nb)) + nb + struct.pack("<i", len(data))
        body = bytearray(4 * len(data))   # each byte as a little-endian int32: only the low byte is set
        body[::4] = bytes((v + k[i % len(k)]) & 0xFF for i, v in enumerate(data))
        out += body
    return bytes(out)


def unpack(buf: bytes, folder: str) -> dict[str, bytes]:
    k, out, p = key(folder), {}, 0
    while p < len(buf):
        (nl,) = struct.unpack_from("<i", buf, p)
        name = buf[p + 4 : p + 4 + nl].decode()
        p += 4 + nl
        (dl,) = struct.unpack_from("<i", buf, p)
        raw = struct.unpack_from(f"<{dl}i", buf, p + 4)
        p += 4 + 4 * dl
        out[name] = bytes((v - k[i % len(k)]) & 0xFF for i, v in enumerate(raw))
    return out


def pack_folder(car_dir: Path) -> bool:
    """If `car_dir` has a `data/` folder and no data.acd, writes data.acd from it and deletes `data/`. True if it did. Only when `data/` holds just files (acd is flat) and not more than MAX_PACK bytes."""
    data = car_dir / "data"
    if (car_dir / "data.acd").is_file() or not data.is_dir():
        return False
    entries = sorted(data.iterdir(), key=lambda p: order(p.name))
    if not entries or any(not p.is_file() for p in entries) or sum(p.stat().st_size for p in entries) > MAX_PACK:
        return False   # a subfolder (acd is flat: it would be lost) or an absurd size: left as it came, the car stays «not usable»
    files = {p.name: p.read_bytes() for p in entries}
    (car_dir / "data.acd").write_bytes(pack(files, car_dir.name))
    shutil.rmtree(data)
    return True
