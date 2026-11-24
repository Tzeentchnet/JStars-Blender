"""Minimal read-only ISO 9660 reader (PS3 discs use plain ISO 9660 + Joliet).

Only what the importer needs: walk the directory tree and read file extents.
Multi-extent files (ISO 9660 level 3) are merged into one logical file.
"""
import os
import struct

SECTOR = 2048


class IsoEntry:
    __slots__ = ("path", "extents", "size", "is_dir")

    def __init__(self, path, extents, size, is_dir):
        self.path = path            # "/PS3_GAME/USRDIR/..." style, upper or Joliet case
        self.extents = extents      # list of (lba, length)
        self.size = size
        self.is_dir = is_dir

    @property
    def offset(self):
        """Absolute byte offset of the first extent (contiguous files only)."""
        return self.extents[0][0] * SECTOR


class IsoReader:
    def __init__(self, path):
        self.path = path
        self.fp = open(path, "rb")
        self.entries = {}
        self._joliet = False
        self._read_volume()

    def close(self):
        try:
            self.fp.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ------------------------------------------------------------------
    def _read_volume(self):
        root_rec = None
        sector = 16
        while True:
            self.fp.seek(sector * SECTOR)
            vd = self.fp.read(SECTOR)
            if len(vd) < SECTOR or vd[1:6] != b"CD001":
                break
            vtype = vd[0]
            if vtype == 1 and root_rec is None:
                root_rec = vd[156:156 + 34]
            elif vtype == 2 and vd[88:91] in (b"%/@", b"%/C", b"%/E"):
                root_rec = vd[156:156 + 34]      # prefer Joliet (keeps real names)
                self._joliet = True
            elif vtype == 255:
                break
            sector += 1
        if root_rec is None:
            raise ValueError("Not an ISO 9660 image: %s" % self.path)
        lba = struct.unpack_from("<I", root_rec, 2)[0]
        size = struct.unpack_from("<I", root_rec, 10)[0]
        self._walk(lba, size, "")

    def _decode_name(self, raw):
        if self._joliet:
            name = raw.decode("utf-16-be", "replace")
        else:
            name = raw.decode("ascii", "replace")
        if ";" in name:
            name = name.split(";", 1)[0]
        if name.endswith(".") and not self._joliet:
            name = name[:-1]
        return name

    def _walk(self, lba, size, prefix):
        self.fp.seek(lba * SECTOR)
        data = self.fp.read(size)
        pos = 0
        pending = None   # multi-extent accumulator
        while pos < len(data):
            ln = data[pos]
            if ln == 0:
                pos = (pos // SECTOR + 1) * SECTOR
                continue
            rec = data[pos:pos + ln]
            pos += ln
            ext_lba = struct.unpack_from("<I", rec, 2)[0]
            ext_len = struct.unpack_from("<I", rec, 10)[0]
            flags = rec[25]
            nlen = rec[32]
            raw = rec[33:33 + nlen]
            if raw in (b"\x00", b"\x01"):
                continue
            name = self._decode_name(raw)
            path = prefix + "/" + name
            is_dir = bool(flags & 2)
            if pending is not None and pending.path == path:
                pending.extents.append((ext_lba, ext_len))
                pending.size += ext_len
            else:
                pending = IsoEntry(path, [(ext_lba, ext_len)], ext_len, is_dir)
                self.entries[path.upper()] = pending
            if is_dir:
                self._walk(ext_lba, ext_len, path)
            if not (flags & 0x80):
                pending = None

    # ------------------------------------------------------------------
    def find(self, path):
        return self.entries.get(path.upper().replace("\\", "/"))

    def files(self):
        return [e for e in self.entries.values() if not e.is_dir]

    def read(self, entry, offset=0, length=None):
        """Read bytes from an entry (handles multi-extent)."""
        if length is None:
            length = entry.size - offset
        out = bytearray()
        cur = 0
        for lba, elen in entry.extents:
            if offset < cur + elen and len(out) < length:
                start = max(0, offset - cur)
                n = min(elen - start, length - len(out))
                self.fp.seek(lba * SECTOR + start)
                out += self.fp.read(n)
            cur += elen
        return bytes(out)


def open_source(path):
    """Accept either an .iso image or an extracted disc folder."""
    if os.path.isdir(path):
        return FolderSource(path)
    return IsoReader(path)


class FolderSource:
    """Same API as IsoReader, for an extracted disc folder (has PS3_GAME)."""

    def __init__(self, root):
        self.path = root
        self.entries = {}
        for dp, dn, fn in os.walk(root):
            for f in fn:
                full = os.path.join(dp, f)
                rel = "/" + os.path.relpath(full, root).replace("\\", "/")
                self.entries[rel.upper()] = IsoEntry(rel, [(0, os.path.getsize(full))],
                                                     os.path.getsize(full), False)
        self._root = root

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def find(self, path):
        return self.entries.get(path.upper().replace("\\", "/"))

    def files(self):
        return list(self.entries.values())

    def read(self, entry, offset=0, length=None):
        if length is None:
            length = entry.size - offset
        with open(os.path.join(self._root, entry.path.lstrip("/")), "rb") as f:
            f.seek(offset)
            return f.read(length)
