"""STPK pack reader. Big-endian; count at +8, 48-byte entries from +16:
u32 offset, u32 size, 8 bytes unknown, 32-byte name. Packs nest."""
import struct

from . import cmp as _cmp


def is_stpk(data):
    return data[:4] == b"STPK"


def read_stpk(data):
    """Return list of (name, bytes) for the pack's direct children."""
    if data[:4] != b"STPK":
        raise ValueError("not an STPK pack")
    count = struct.unpack_from(">I", data, 8)[0]
    first = 16  # +12 is the data alignment
    out = []
    for i in range(count):
        p = first + i * 48
        off, size = struct.unpack_from(">II", data, p)
        name = data[p + 16:p + 48].split(b"\x00", 1)[0].decode("ascii", "replace")
        out.append((name, data[off:off + size] if size else b""))
    return out


def walk(data, prefix=""):
    """Recursively yield (path, bytes) for every leaf file.

    Nested packs and $CMP payloads are expanded in place; the path of a
    nested entry is ``parent/child``.
    """
    for name, blob in read_stpk(data):
        if _cmp.is_cmp(blob):
            blob = _cmp.decode(blob)
        path = prefix + name
        if is_stpk(blob):
            yield from walk(blob, path + "/")
        else:
            yield path, blob


def list_tree(data, prefix=""):
    """Like walk() but yields (path, size) and does not copy leaf bytes."""
    for path, blob in walk(data, prefix):
        yield path, len(blob)
