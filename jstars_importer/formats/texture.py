"""Texture decoding for J-Stars $TXR resources (PS3 RSX formats).

Everything is vectorised with numpy: a 1024x1024 DXT5 decodes in a few
milliseconds instead of seconds.

Format bytes seen on the disc:
  0x0F  DXT1 (BC1)
  0x11  DXT5 (BC3)                colour + alpha
  0x13  DXT5 (BC3)                used for normal maps
  0x01  A8R8G8B8, 32 bpp          (rare)
  0x04  16 bpp                    (rare; decoded as R5G6B5)
"""
import struct

import numpy as np

FMT_DXT1 = 0x0F
FMT_DXT3 = 0x10
FMT_DXT5 = 0x11
FMT_DXT5_N = 0x13
FMT_ARGB8 = 0x01

BLOCK_BYTES = {FMT_DXT1: 8, FMT_DXT3: 16, FMT_DXT5: 16, FMT_DXT5_N: 16}


def is_block_compressed(fmt):
    return fmt in BLOCK_BYTES


def mip_size(fmt, w, h):
    if fmt in BLOCK_BYTES:
        return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * BLOCK_BYTES[fmt]
    if fmt == 0x04:
        return w * h * 2
    return w * h * 4


def _expand565(c):
    c = c.astype(np.uint32)
    r = (c >> 11) & 31
    g = (c >> 5) & 63
    b = c & 31
    return np.stack(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)), -1).astype(np.int32)


def _color_block(blk, dxt1):
    """blk: (n, 8) uint8 colour part. Returns (n, 16, 4) uint8 RGBA."""
    c0 = blk[:, 0].astype(np.uint16) | (blk[:, 1].astype(np.uint16) << 8)
    c1 = blk[:, 2].astype(np.uint16) | (blk[:, 3].astype(np.uint16) << 8)
    p0 = _expand565(c0)
    p1 = _expand565(c1)
    n = blk.shape[0]
    pal = np.empty((n, 4, 4), np.int32)
    pal[:, 0, :3] = p0
    pal[:, 1, :3] = p1
    pal[:, :, 3] = 255
    four = (c0 > c1) | (not dxt1)
    four = np.broadcast_to(four, (n,)) if np.ndim(four) == 0 else four
    f = four[:, None]
    pal[:, 2, :3] = np.where(f, (2 * p0 + p1) // 3, (p0 + p1) // 2)
    pal[:, 3, :3] = np.where(f, (p0 + 2 * p1) // 3, 0)
    if dxt1:
        pal[:, 3, 3] = np.where(four, 255, 0)
    bits = (blk[:, 4].astype(np.uint32) | (blk[:, 5].astype(np.uint32) << 8)
            | (blk[:, 6].astype(np.uint32) << 16) | (blk[:, 7].astype(np.uint32) << 24))
    idx = (bits[:, None] >> (2 * np.arange(16, dtype=np.uint32))[None, :]) & 3
    return np.take_along_axis(pal, idx[:, :, None].astype(np.intp).repeat(4, 2), 1).astype(np.uint8)


def _alpha_dxt5(blk):
    a0 = blk[:, 0].astype(np.int32)
    a1 = blk[:, 1].astype(np.int32)
    n = blk.shape[0]
    pal = np.empty((n, 8), np.int32)
    pal[:, 0] = a0
    pal[:, 1] = a1
    big = (a0 > a1)
    for i in range(1, 7):
        pal[:, 1 + i] = ((7 - i) * a0 + i * a1) // 7
    small = ~big
    for i in range(1, 5):
        pal[small, 1 + i] = ((5 - i) * a0[small] + i * a1[small]) // 5
    pal[small, 6] = 0
    pal[small, 7] = 255
    bits = np.zeros(n, np.uint64)
    for j in range(6):
        bits |= blk[:, 2 + j].astype(np.uint64) << np.uint64(8 * j)
    idx = (bits[:, None] >> (np.uint64(3) * np.arange(16, dtype=np.uint64))[None, :]) & np.uint64(7)
    return np.take_along_axis(pal, idx.astype(np.intp), 1).astype(np.uint8)


def _alpha_dxt3(blk):
    nib = np.empty((blk.shape[0], 16), np.uint8)
    nib[:, 0::2] = blk[:, :8] & 15
    nib[:, 1::2] = blk[:, :8] >> 4
    return nib * 17


def _unblock(px, w, h):
    """(nblocks, 16, 4) -> (h, w, 4) cropping padding."""
    bw, bh = max(1, (w + 3) // 4), max(1, (h + 3) // 4)
    img = px.reshape(bh, bw, 4, 4, 4).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 4)
    return img[:h, :w]


def _morton_unswizzle(arr, w, h):
    """Undo RSX (Morton / Z-order) swizzling for power-of-two textures."""
    if w & (w - 1) or h & (h - 1):
        return arr
    ys, xs = np.mgrid[0:h, 0:w]
    idx = np.zeros((h, w), np.int64)
    bit = 0
    x = xs.copy()
    y = ys.copy()
    mw, mh = w, h
    shift = 0
    while mw > 1 or mh > 1:
        if mw > 1:
            idx |= (x & 1) << shift
            x >>= 1
            shift += 1
            mw >>= 1
        if mh > 1:
            idx |= (y & 1) << shift
            y >>= 1
            shift += 1
            mh >>= 1
    flat = arr.reshape(w * h, -1)
    return flat[idx.reshape(-1)].reshape(h, w, -1)


def decode_rgba(fmt, w, h, data, swizzled=False):
    """Decode mip 0 to an (h, w, 4) uint8 RGBA array (top row first)."""
    buf = np.frombuffer(data, np.uint8)
    if fmt in BLOCK_BYTES:
        bs = BLOCK_BYTES[fmt]
        nb = max(1, (w + 3) // 4) * max(1, (h + 3) // 4)
        if buf.size < nb * bs:
            buf = np.concatenate([buf, np.zeros(nb * bs - buf.size, np.uint8)])
        blk = buf[:nb * bs].reshape(nb, bs)
        if fmt == FMT_DXT1:
            px = _color_block(blk, True)
        else:
            px = _color_block(blk[:, 8:], False)
            px[:, :, 3] = _alpha_dxt5(blk[:, :8]) if fmt != FMT_DXT3 else _alpha_dxt3(blk[:, :8])
        return np.ascontiguousarray(_unblock(px, w, h))
    if fmt == 0x04:
        need = w * h * 2
        c = np.frombuffer(data[:need].ljust(need, b"\0"), ">u2")
        rgb = _expand565(c).astype(np.uint8)
        out = np.concatenate([rgb, np.full((rgb.shape[0], 1), 255, np.uint8)], 1).reshape(h, w, 4)
    else:
        need = w * h * 4
        raw = np.frombuffer(data[:need].ljust(need, b"\0"), np.uint8).reshape(h, w, 4)
        out = raw[:, :, [1, 2, 3, 0]]          # A R G B -> R G B A
    if swizzled:
        out = _morton_unswizzle(out, w, h)
    return np.ascontiguousarray(out)


# ----------------------------------------------------------------------
# DDS writer (lets Blender decode block-compressed textures natively)
def make_dds(fmt, w, h, data, mips):
    """Return DDS file bytes for a block-compressed texture, or None."""
    fourcc = {FMT_DXT1: b"DXT1", FMT_DXT3: b"DXT3", FMT_DXT5: b"DXT5", FMT_DXT5_N: b"DXT5"}.get(fmt)
    if fourcc is None:
        return None
    # Only keep the mips that are fully present.
    total = 0
    keep = 0
    mw, mh = w, h
    for _ in range(max(1, mips)):
        s = mip_size(fmt, mw, mh)
        if total + s > len(data):
            break
        total += s
        keep += 1
        mw, mh = max(1, mw // 2), max(1, mh // 2)
    if keep == 0:
        return None
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000 | (0x20000 if keep > 1 else 0)
    caps = 0x1000 | ((0x8 | 0x400000) if keep > 1 else 0)
    hdr = struct.pack("<4sIIIIIII44s" "II4sIIIII" "IIIII",
                      b"DDS ", 124, flags, h, w, mip_size(fmt, w, h), 0, keep, b"\0" * 44,
                      32, 0x4, fourcc, 0, 0, 0, 0, 0,
                      caps, 0, 0, 0, 0)
    assert len(hdr) == 128
    return hdr + bytes(data[:total])


def has_alpha(rgba):
    return bool((rgba[:, :, 3] < 250).any())


def mean_alpha(fmt, w, h, data):
    """Mean alpha of mip 0 in [0, 1]. Formats without alpha report 1.0."""
    if fmt in (FMT_DXT1, 0x04, FMT_DXT5_N) or not data:
        return 1.0
    if fmt in (FMT_DXT5, FMT_DXT3):
        nb = max(1, (w + 3) // 4) * max(1, (h + 3) // 4)
        buf = np.frombuffer(data, np.uint8)
        if buf.size < nb * 16:
            return 1.0
        blk = buf[:nb * 16].reshape(nb, 16)[:, :8]
        a = _alpha_dxt5(blk) if fmt == FMT_DXT5 else _alpha_dxt3(blk)
        return float(a.mean()) / 255.0
    return float(decode_rgba(fmt, w, h, data)[..., 3].mean()) / 255.0
