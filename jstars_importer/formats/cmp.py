"""Decoder for the game's $CMP compressed files.

Layout (big-endian): 0x20-byte header, +0x10 = unpacked size, then chunks
``tag, u32 unpacked, u32 chunk size incl. 16-byte header, u32 x``:

* ``$CL0`` LZ tokens. Even t: literal run of t>>1 bytes. Odd t: copy
  (t>>1)&0x1F bytes from distance ((t>>6)<<8)|next byte.
* ``$CLH`` the same LZ stream, Huffman coded inside a ``$CH0`` block.
* ``$CH0`` Huffman coded raw bytes.
* ``$CR0`` stored.

Format credit: docs/archive/jstars-notes/tools/cmp_unpack.py in the
jsp-vs-ex project. This version uses a table-driven Huffman decoder and slice
copies so multi-megabyte stage textures decode in seconds, not minutes.
"""
import struct


def _lz(d, p, end, out):
    base = len(out)
    while p < end:
        t = d[p]
        p += 1
        if not t & 1:
            n = t >> 1
            out += d[p:p + n]
            p += n
        else:
            n = (t >> 1) & 0x1F
            dist = ((t >> 6) << 8) | d[p]
            p += 1
            if dist == 0 or dist > len(out) - base:
                raise ValueError("bad LZ distance %d" % dist)
            if dist >= n:
                s = len(out) - dist
                out += out[s:s + n]
            else:
                pat = out[-dist:]
                out += (pat * (n // dist + 1))[:n]


def _huff(d, p):
    if d[p:p + 4] != b"$CH0":
        raise ValueError("expected $CH0")
    out_n, in_n, cnt = struct.unpack_from(">III", d, p + 4)
    if cnt == 1:
        return bytes([d[p + 19]]) * out_n
    codes = []
    q = p + 16
    maxlen = 0
    for _ in range(cnt):
        c = d[q] | (d[q + 1] << 8)
        ln = d[q + 2] >> 3
        codes.append((ln, c, d[q + 3]))
        if ln > maxlen:
            maxlen = ln
        q += 4
    if maxlen > 20:
        return _huff_slow(d, q, p + in_n, out_n, codes)
    L = maxlen
    # table[prefix padded to L bits] = sym | len << 8
    table = [0] * (1 << L)
    for ln, c, sym in codes:
        shift = L - ln
        start = c << shift
        v = sym | (ln << 8)
        for i in range(start, start + (1 << shift)):
            table[i] = v
    res = bytearray(out_n)
    end = p + in_n
    window = 0
    nbits = 0
    mask = (1 << L) - 1
    wmask = (1 << (L + 8)) - 1
    i = 0
    dd = d
    while i < out_n:
        while nbits < L:
            window = ((window << 8) | (dd[q] if q < end else 0)) & wmask
            q += 1
            nbits += 8
        e = table[(window >> (nbits - L)) & mask]
        ln = e >> 8
        if ln == 0:
            raise ValueError("bad Huffman code")
        res[i] = e & 0xFF
        i += 1
        nbits -= ln
    return bytes(res)


def _huff_slow(d, q, end, out_n, codes):
    tab = {(ln, c): s for ln, c, s in codes}
    res = bytearray()
    acc = ln = 0
    bitpos, lim = q * 8, end * 8
    while len(res) < out_n:
        if bitpos >= lim:
            raise ValueError("Huffman stream ran out")
        acc = (acc << 1) | ((d[bitpos >> 3] >> (7 - (bitpos & 7))) & 1)
        ln += 1
        bitpos += 1
        s = tab.get((ln, acc))
        if s is not None:
            res.append(s)
            acc = ln = 0
        elif ln > 31:
            raise ValueError("no Huffman code")
    return bytes(res)


def is_cmp(data):
    return data[:4] == b"$CMP"


def decode(d):
    """Return the decompressed bytes of a $CMP blob (or the blob unchanged)."""
    if d[:4] != b"$CMP":
        return d
    total = struct.unpack_from(">I", d, 16)[0]
    out = bytearray()
    p = 0x20
    while p + 16 <= len(d) and d[p] == 0x24:  # '$'
        tag = d[p:p + 4]
        a, n, _x = struct.unpack_from(">III", d, p + 4)
        base = len(out)
        if tag == b"$CL0":
            _lz(d, p + 16, p + n, out)
        elif tag == b"$CLH":
            h = _huff(d, p + 16)
            if h[:4] != b"$CL0":
                raise ValueError("$CLH without inner $CL0")
            _lz(h, 16, len(h), out)
        elif tag == b"$CR0":
            out += d[p + 16:p + n]
        elif tag == b"$CH0":
            out += _huff(d, p)
        else:
            raise ValueError("unknown $CMP chunk %r" % tag)
        if len(out) - base != a:
            raise ValueError("chunk size mismatch")
        if n == 0:
            break
        p += n
    if len(out) != total:
        raise ValueError("$CMP size mismatch %d != %d" % (len(out), total))
    return bytes(out)
