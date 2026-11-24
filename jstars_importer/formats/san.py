"""J-Stars ``.san`` motions: an SRD file holding one ``$ANM`` block.

$ANM header (BE): u32 version (4), f32 length in frames, u32 track table
offset, ..., u16 track count at +0x1C.

Track record, 40 bytes::

    u32 name, u32 nT, u32 nR, u32 nS,
    u32 timesT, u32 timesR, u32 timesS,     -> float32 key times (frames)
    u32 valuesT, u32 valuesR, u32 valuesS

Translation / scale values are float4 (xyz + pad). Rotation values are 64-bit
"smallest three" quaternions: bits 60-61 = index of the dropped component in
x,y,z,w order, then three 20-bit fields holding the other components in
x,y,z,w order starting from the low bits (0-19, 20-39, 40-59), each mapped
to [-1/sqrt2, 1/sqrt2].
"""
import struct

import numpy as np

from .srd import parse_blocks, _cstr

_INV_SQRT2 = 0.7071067811865476


class Track:
    __slots__ = ("name", "t_times", "t_vals", "r_times", "r_vals", "s_times", "s_vals")


class Motion:
    __slots__ = ("name", "length", "tracks")


def decode_quats(raw):
    """(n,) uint64 -> (n, 4) float64 quaternions in w, x, y, z order.

    Packing (x, y, z, w component order): bits 60-61 = index of the dropped
    component, then the other three in x, y, z, w order starting from the
    LOW bits (bits 0-19 first, 20-39 second, 40-59 third).
    """
    v = raw.astype(np.uint64)
    idx = ((v >> np.uint64(60)) & np.uint64(3)).astype(np.int64)
    comps = np.stack([((v >> np.uint64(s)) & np.uint64(0xFFFFF)).astype(np.float64)
                      for s in (0, 20, 40)], 1)
    comps = (comps / 0xFFFFF * 2.0 - 1.0) * _INV_SQRT2
    big = np.sqrt(np.clip(1.0 - (comps * comps).sum(1), 0.0, 1.0))
    xyzw = np.empty((len(v), 4))
    for d in range(4):
        m = idx == d
        if not m.any():
            continue
        others = [c for c in range(4) if c != d]
        xyzw[m, d] = big[m]
        xyzw[np.ix_(m, others)] = comps[m]
    return xyzw[:, [3, 0, 1, 2]]


def _floats(a, off, n, width):
    if n == 0:
        return np.zeros((0, width), np.float64)
    return np.frombuffer(a, ">f4", n * width, off).reshape(n, width).astype(np.float64)


def parse_anm(a, name=""):
    m = Motion()
    m.name = name
    m.length = struct.unpack_from(">f", a, 4)[0]
    toff = struct.unpack_from(">I", a, 8)[0]
    count = struct.unpack_from(">H", a, 0x1C)[0]
    m.tracks = []
    for i in range(count):
        r = struct.unpack_from(">10I", a, toff + 40 * i)
        t = Track()
        t.name = _cstr(a, r[0])
        nT, nR, nS = r[1], r[2], r[3]
        try:
            t.t_times = _floats(a, r[4], nT, 1)[:, 0]
            t.r_times = _floats(a, r[5], nR, 1)[:, 0]
            t.s_times = _floats(a, r[6], nS, 1)[:, 0]
            t.t_vals = _floats(a, r[7], nT, 4)[:, :3]
            t.r_vals = decode_quats(np.frombuffer(a, ">u8", nR, r[8])) if nR else np.zeros((0, 4))
            t.s_vals = _floats(a, r[9], nS, 4)[:, :3]
        except ValueError:
            continue
        m.tracks.append(t)
    return m


def parse_san(data, name=""):
    """Return a list of Motion (normally one) from .san bytes."""
    out = []
    for b in parse_blocks(data):
        if b.tag == "$ANM":
            r = b.rsi
            out.append(parse_anm(b.data, r.name if r and r.name else name))
    return out
