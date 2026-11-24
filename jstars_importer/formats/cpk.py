"""CRI CPK archive reader (@UTF tables, XOR-masked TOC, CRILAYLA)."""
import struct

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None


def _unmask(data):
    """CRI @UTF XOR mask: m = 0x655F, m *= 0x4115 per byte."""
    if np is not None:
        n = len(data)
        # The mask sequence only depends on position; build it with python ints
        # once per call (tables are small, < a few MB).
        mask = bytearray(n)
        m = 0x655F
        for i in range(n):
            mask[i] = m & 0xFF
            m = (m * 0x4115) & 0xFFFFFFFF
        return (np.frombuffer(data, np.uint8) ^ np.frombuffer(bytes(mask), np.uint8)).tobytes()
    out = bytearray(data)
    m = 0x655F
    for i in range(len(out)):
        out[i] ^= m & 0xFF
        m = (m * 0x4115) & 0xFFFFFFFF
    return bytes(out)


class UtfTable:
    def __init__(self, data):
        if data[:4] != b"@UTF":
            data = _unmask(data)
        if data[:4] != b"@UTF":
            raise ValueError("bad @UTF table")
        self.raw = data
        size = struct.unpack_from(">I", data, 4)[0]
        b = data[8:8 + size]
        rows_off, str_off, data_off, name_off = struct.unpack_from(">IIII", b, 0)
        ncols, row_len, nrows = struct.unpack_from(">HHI", b, 16)
        self.name = self._str(b, str_off, name_off)
        self.rows = []
        cols = []
        p = 24
        for _ in range(ncols):
            flags = b[p]
            if flags == 0:      # some tables pad with an extra 3 bytes
                p += 4
                flags = b[p]
            noff = struct.unpack_from(">I", b, p + 1)[0]
            p += 5
            storage, typ = flags & 0xF0, flags & 0x0F
            const = None
            if storage == 0x30:
                const, p = self._value(b, p, typ, str_off, data_off)
            cols.append((self._str(b, str_off, noff), storage, typ, const))
        self.columns = [c[0] for c in cols]
        for r in range(nrows):
            p = rows_off + r * row_len
            row = {}
            for cname, storage, typ, const in cols:
                if storage == 0x50:
                    row[cname], p = self._value(b, p, typ, str_off, data_off)
                elif storage == 0x30:
                    row[cname] = const
                else:
                    row[cname] = None
            self.rows.append(row)

    @staticmethod
    def _str(b, str_off, off):
        s = str_off + off
        e = b.index(b"\x00", s)
        return b[s:e].decode("utf-8", "replace")

    def _value(self, b, p, typ, str_off, data_off):
        if typ in (0, 1):
            return (b[p] if typ == 0 else struct.unpack_from(">b", b, p)[0]), p + 1
        if typ in (2, 3):
            return struct.unpack_from(">H" if typ == 2 else ">h", b, p)[0], p + 2
        if typ in (4, 5):
            return struct.unpack_from(">I" if typ == 4 else ">i", b, p)[0], p + 4
        if typ in (6, 7):
            return struct.unpack_from(">Q" if typ == 6 else ">q", b, p)[0], p + 8
        if typ == 8:
            return struct.unpack_from(">f", b, p)[0], p + 4
        if typ == 0xA:
            return self._str(b, str_off, struct.unpack_from(">I", b, p)[0]), p + 4
        if typ == 0xB:
            off, ln = struct.unpack_from(">II", b, p)
            return b[data_off + off:data_off + off + ln], p + 8
        raise ValueError("unknown @UTF type %x" % typ)


class CpkFile:
    __slots__ = ("dir", "name", "offset", "size", "extract_size", "id")

    def __init__(self, d, n, off, size, esize, fid):
        self.dir, self.name, self.offset = d, n, off
        self.size, self.extract_size, self.id = size, esize, fid

    @property
    def path(self):
        return (self.dir + "/" + self.name) if self.dir else self.name


def read_cpk_toc(read):
    """read(offset, length) -> bytes, relative to the start of the CPK.

    Returns a list of CpkFile with absolute offsets (relative to the CPK start).
    """
    head = read(0, 16)
    if head[:4] != b"CPK ":
        raise ValueError("not a CPK")
    pkt = struct.unpack_from("<I", head, 8)[0]
    hdr = UtfTable(read(16, pkt)).rows[0]
    toc_off = hdr.get("TocOffset") or 0
    content_off = hdr.get("ContentOffset") or 0
    files = []
    if toc_off:
        th = read(toc_off, 16)
        tpkt = struct.unpack_from("<I", th, 8)[0]
        toc = UtfTable(read(toc_off + 16, tpkt))
        base = min(toc_off, content_off) if content_off else toc_off
        for r in toc.rows:
            files.append(CpkFile(r.get("DirName") or "", r.get("FileName") or "",
                                 base + (r.get("FileOffset") or 0), r.get("FileSize") or 0,
                                 r.get("ExtractSize") or r.get("FileSize") or 0, r.get("ID") or 0))
    else:
        # ID-only CPK (ITOC). Files are laid out in ID order from ContentOffset.
        itoc_off = hdr.get("ItocOffset")
        align = hdr.get("Align") or 0x800
        ih = read(itoc_off, 16)
        itoc = UtfTable(read(itoc_off + 16, struct.unpack_from("<I", ih, 8)[0])).rows[0]
        entries = {}
        for key, idcol, scol, ecol in (("DataL", "ID", "FileSize", "ExtractSize"),
                                       ("DataH", "ID", "FileSize", "ExtractSize")):
            if itoc.get(key):
                for r in UtfTable(itoc[key]).rows:
                    entries[r[idcol]] = (r[scol], r[ecol])
        off = content_off
        for fid in sorted(entries):
            s, e = entries[fid]
            files.append(CpkFile("", "%05d.bin" % fid, off, s, e, fid))
            off += s
            if off % align:
                off += align - off % align
    return files


def crilayla_decompress(data):
    """Decompress a CRILAYLA block (as used by CPK file compression)."""
    if data[:8] != b"CRILAYLA":
        return data
    usize, hoff = struct.unpack_from("<II", data, 8)
    prefix = data[0x10 + hoff:0x10 + hoff + 0x100]
    out = bytearray(usize + 0x100)
    out[:0x100] = prefix
    src = data
    pos = len(src) - 1
    bitpool = 0
    bitsleft = 0
    bytes_out = 0
    vle_lens = (2, 3, 5, 8)

    def getbits(n):
        nonlocal pos, bitpool, bitsleft
        v = 0
        produced = 0
        while produced < n:
            if bitsleft == 0:
                bitpool = src[pos]
                bitsleft = 8
                pos -= 1
            take = min(bitsleft, n - produced)
            v = (v << take) | ((bitpool >> (bitsleft - take)) & ((1 << take) - 1))
            bitsleft -= take
            produced += take
        return v

    out_end = 0x100 + usize - 1
    while bytes_out < usize:
        if getbits(1):
            back = getbits(13) + 3
            count = 3
            for vl in vle_lens:
                lv = getbits(vl)
                count += lv
                if lv != (1 << vl) - 1:
                    break
            else:
                while True:
                    lv = getbits(8)
                    count += lv
                    if lv != 255:
                        break
            for _ in range(count):
                out[out_end - bytes_out] = out[out_end - bytes_out + back]
                bytes_out += 1
        else:
            out[out_end - bytes_out] = getbits(8)
            bytes_out += 1
    return bytes(out)
