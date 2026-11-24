"""Spike Chunsoft SRD resource format, PS3 big-endian flavour (J-Stars Victory VS+).

An ``.srd`` holds the headers; bulk data lives in the companion ``.srdi``
(vertex/index buffers, location flag 0x2xxxxxxx) and ``.srdv`` (textures, and
stage vertex buffers, flag 0x4xxxxxxx).

Block: ``tag[4] u32 data_len u32 sub_len u32 unk`` (BE), data padded to 16,
then child blocks padded to 16.

All layouts below were recovered from the game's own files; see docs/FORMATS.md.
"""
import struct

import numpy as np

LOC_MASK = 0x0FFFFFFF


def _cstr(b, o):
    if o >= len(b):
        return ""
    e = b.find(b"\x00", o)
    if e < 0:
        e = len(b)
    return b[o:e].decode("ascii", "replace")


class Block:
    __slots__ = ("tag", "data", "children")

    def __init__(self, tag, data, children):
        self.tag, self.data, self.children = tag, data, children

    @property
    def rsi(self):
        for c in self.children:
            if c.tag == "$RSI":
                return Rsi(c.data)
        return None


def parse_blocks(buf, start=0, end=None):
    end = len(buf) if end is None else end
    out = []
    p = start
    while p + 16 <= end:
        tag = buf[p:p + 4]
        if tag[:1] != b"$":
            break
        dl, sl = struct.unpack_from(">II", buf, p + 4)
        data = buf[p + 16:p + 16 + dl]
        q = (p + 16 + dl + 15) & ~15
        kids = parse_blocks(buf, q, q + sl) if sl else []
        out.append(Block(tag.decode("ascii", "replace"), data, kids))
        p = (q + sl + 15) & ~15
    return out


class Rsi:
    """Resource info: external data locations, typed parameters and a name."""

    def __init__(self, d):
        self.raw = d
        nres = d[3]
        np_, _np2, po, _po2, no = struct.unpack_from(">HHHHI", d, 4)
        self.resources = []
        for k in range(nres):
            loc, size, a, b = struct.unpack_from(">IIII", d, 16 + 16 * k)
            self.resources.append((loc, size))
        self.params = {}
        for k in range(np_):
            o = po + 16 * k
            if o + 12 > len(d):
                break
            nmo, vo, sz = struct.unpack_from(">III", d, o)
            self.params[_cstr(d, nmo)] = d[vo:vo + sz]
        self.name = _cstr(d, no)

    def param_floats(self, key):
        v = self.params.get(key)
        if not v or len(v) % 4:
            return None
        return struct.unpack(">%df" % (len(v) // 4), v)


def resource_bytes(loc, size, srdi, srdv, srd=None):
    off = loc & LOC_MASK
    kind = loc >> 28
    src = srdv if kind == 4 else srdi if kind == 2 else srd
    if src is None or off + size > len(src):
        return None
    return src[off:off + size]


# ----------------------------------------------------------------------
class Texture:
    __slots__ = ("name", "width", "height", "pitch", "fmt", "mips", "flags", "loc", "size")


def parse_txr(block):
    d = block.data
    t = Texture()
    t.flags = (d[4] << 8) | d[5]
    t.width, t.height, t.pitch = struct.unpack_from(">HHH", d, 6)
    t.fmt = d[12]
    t.mips = d[13]
    r = block.rsi
    t.name = r.name if r else ""
    t.loc, t.size = (r.resources[0] if r and r.resources else (0, 0))
    return t


class TexInstance:
    __slots__ = ("name", "textures", "params")


def parse_txi(block):
    d = block.data
    ti = TexInstance()
    n = d[12]
    ti.textures = [_cstr(d, struct.unpack_from(">I", d, 16 + 4 * k)[0]) for k in range(n)]
    r = block.rsi
    ti.name = r.name if r else ""
    ti.params = r.params if r else {}
    return ti


class Material:
    __slots__ = ("name", "maps", "params", "floats", "shaders")


def parse_mat(block):
    d = block.data
    m = Material()
    m.floats = struct.unpack_from(">3f", d, 4)
    mo = struct.unpack_from(">H", d, 0x14)[0]
    cnt = d[0x16]
    m.maps = []
    for k in range(cnt):
        a, b = struct.unpack_from(">HH", d, mo + 4 * k)
        m.maps.append((_cstr(d, b), _cstr(d, a)))     # (slot e.g. COLORMAP0, texture instance)
    r = block.rsi
    m.name = r.name if r else ""
    m.params = r.params if r else {}
    # OpShaderInfo: u32 offsets of the program names inside the RSI data
    # (opbb_shader_<mat>_<pass>.vcg/.pcg; stage shaders live in *_shader.srd).
    si = m.params.get("OpShaderInfo")
    m.shaders = []
    if r and si and len(si) % 4 == 0:
        m.shaders = [_cstr(r.raw, o) for o in struct.unpack(">%dI" % (len(si) // 4), si) if o < len(r.raw)]
    return m


class MeshRef:
    __slots__ = ("name", "vtx", "mat", "color_sets", "uv_sets", "samplers")


def parse_msh(block):
    """+4 vtx name, +6 material name, +0xA/+0xC/+0xE list offsets,
    +0x10 counts: colour sets, UV sets, samplers. Samplers are (slot, uv set) pairs."""
    d = block.data
    m = MeshRef()
    vo, mo, _a, co, uo, so = struct.unpack_from(">HHHHHH", d, 4)
    nc, nu, ns = d[0x10], d[0x11], d[0x12]
    m.vtx = _cstr(d, vo)
    m.mat = _cstr(d, mo)
    m.color_sets = [_cstr(d, struct.unpack_from(">H", d, co + 2 * k)[0]) for k in range(nc)]
    m.uv_sets = [_cstr(d, struct.unpack_from(">H", d, uo + 2 * k)[0]) for k in range(nu)]
    m.samplers = {}
    for k in range(ns):
        a, b = struct.unpack_from(">HH", d, so + 4 * k)
        m.samplers[_cstr(d, a)] = _cstr(d, b)
    r = block.rsi
    m.name = r.name if r else m.vtx
    return m


# ----------------------------------------------------------------------
SEM_POSITION, SEM_NORMAL, SEM_COLOR, SEM_TEXCOORD = 1, 2, 3, 4
SEM_TANGENT, SEM_BINORMAL, SEM_WEIGHTS, SEM_INDICES = 5, 6, 7, 8


class VertexBlock:
    __slots__ = ("name", "count", "prim", "attrs", "streams", "bones", "vloc", "vsize", "iloc", "isize",
                 "set_names")


def parse_vtx(block):
    d = block.data
    v = VertexBlock()
    attr_off = struct.unpack_from(">H", d, 4)[0]
    nattr = d[6]
    v.prim = d[7]
    v.count = struct.unpack_from(">I", d, 8)[0]
    nstreams = d[0x0F]
    _bone_root, stream_off, _float_off, bone_list = struct.unpack_from(">HHHH", d, 0x10)
    nbones = d[0x18]
    v.attrs = []
    v.set_names = {}
    for k in range(nattr):
        a = d[attr_off + 8 * k:attr_off + 8 * k + 8]
        # u16 set-name offset, offset, stream, semantic, usage index, format code
        v.attrs.append((a[2], a[3], a[4], a[5], a[6]))
        name_off = (a[0] << 8) | a[1]
        if name_off and a[4] in (SEM_TEXCOORD, SEM_COLOR):
            v.set_names[(a[4], a[5])] = _cstr(d, name_off)
    v.streams = []
    for k in range(nstreams):
        off = struct.unpack_from(">I", d, stream_off + 8 * k)[0]
        v.streams.append((off, d[stream_off + 8 * k + 4]))
    v.bones = []
    if nbones and bone_list:
        for k in range(nbones):
            v.bones.append(_cstr(d, struct.unpack_from(">H", d, bone_list + 2 * k)[0]))
    r = block.rsi
    v.name = r.name if r else ""
    res = r.resources if r else []
    v.vloc, v.vsize = res[0] if len(res) > 0 else (0, 0)
    v.iloc, v.isize = res[1] if len(res) > 1 else (0, 0)
    return v


def _unpack_cmp(words):
    """Signed 11:11:10 packed normals (RSX CMP) -> (n, 3) float32."""
    w = words.astype(np.uint32)
    x = (w & 0x7FF).astype(np.int32)
    y = ((w >> 11) & 0x7FF).astype(np.int32)
    z = ((w >> 22) & 0x3FF).astype(np.int32)
    x = np.where(x >= 1024, x - 2048, x) / 1023.0
    y = np.where(y >= 1024, y - 2048, y) / 1023.0
    z = np.where(z >= 512, z - 1024, z) / 511.0
    return np.stack((x, y, z), 1).astype(np.float32)


def decode_vertices(vb, vbuf):
    """Return dict: semantic/usage -> numpy array (n, k)."""
    out = {}
    n = vb.count
    for off, stream, sem, usage, code in vb.attrs:
        if stream >= len(vb.streams):
            continue
        soff, stride = vb.streams[stream]
        if stride == 0 or stride % 4 or off % 4:
            continue
        need = soff + stride * n
        if need > len(vbuf):
            continue
        words = np.frombuffer(vbuf, ">u4", n * stride // 4, soff).reshape(n, stride // 4)
        col = off // 4
        fmt, cnt = code >> 4, code & 15
        if code == 0x39:
            val = _unpack_cmp(words[:, col])
        elif code == 0x3A:
            b = words[:, col].astype(">u4").view(np.uint8).reshape(n, 4)
            val = b.astype(np.float32) / 255.0
        elif fmt == 0:
            val = words[:, col:col + cnt].copy().view(">f4").astype(np.float32)
        elif fmt == 1:
            # u32 integers (blend indices, 0x14). Reading these as float gives
            # denormals that all round to bone 0.
            val = words[:, col:col + cnt].astype(np.int64)
        else:
            continue
        out[(sem, usage)] = val
    return out


def strip_to_triangles(idx, prim=2):
    """u16 triangle strip(s) with 0xFFFF restart -> (m, 3) int32 triangles."""
    idx = np.asarray(idx, dtype=np.int64)
    if prim != 2:
        n = len(idx) // 3 * 3
        return idx[:n].reshape(-1, 3).astype(np.int32)
    if len(idx) < 3:
        return np.zeros((0, 3), np.int32)
    restart = idx == 0xFFFF
    pos = np.arange(len(idx))
    # start of the strip segment that each position belongs to
    seg = np.maximum.accumulate(np.where(restart, pos + 1, 0))
    a, b, c = idx[:-2], idx[1:-1], idx[2:]
    ok = ~(restart[:-2] | restart[1:-1] | restart[2:])
    ok &= (a != b) & (b != c) & (a != c)
    odd = ((pos[:-2] - seg[:-2]) & 1).astype(bool)
    tri = np.stack((a, b, c), 1)
    tri[odd] = tri[odd][:, [1, 0, 2]]
    return tri[ok].astype(np.int32)


# ----------------------------------------------------------------------
class TreeNode:
    __slots__ = ("name", "depth", "refs", "matrix", "parent", "typ")


def parse_tre(block):
    d = block.data
    rec_off, count = struct.unpack_from(">HH", d, 4)
    mat_off = struct.unpack_from(">I", d, 12)[0]
    nodes = []
    stack = []
    for i in range(count):
        no, ro, nref, depth, _flag, typ, mi = struct.unpack_from(">IIBBBBI", d, rec_off + 16 * i)
        n = TreeNode()
        n.name = _cstr(d, no)
        n.depth = depth
        n.typ = typ
        n.refs = [_cstr(d, struct.unpack_from(">I", d, ro + 8 * k)[0]) for k in range(nref)] if nref else []
        n.matrix = None
        if mat_off and (typ & 1):
            mo = mat_off + 64 * mi
            if mo + 64 <= len(d):
                # stored row-major for row vectors: transpose for column vectors
                n.matrix = np.array(struct.unpack_from(">16f", d, mo), np.float64).reshape(4, 4).T
        while stack and stack[-1].depth >= depth:
            stack.pop()
        n.parent = stack[-1] if stack else None
        stack.append(n)
        nodes.append(n)
    r = block.rsi
    return nodes, (r.params if r else {}), (r.name if r else "")


# ----------------------------------------------------------------------
class Bone:
    __slots__ = ("name", "index", "depth", "parent", "local", "inv_bind")


def _m43(vals):
    """12 floats: X axis, Y axis, Z axis, translation (row vectors) -> 4x4 column-vector matrix."""
    m = np.eye(4)
    rows = np.array(vals, np.float64).reshape(4, 3)
    m[:3, :3] = rows[:3].T
    m[:3, 3] = rows[3]
    return m


def parse_skl(block):
    d = block.data
    rec_off, count = struct.unpack_from(">HH", d, 4)
    bones = []
    stack = []
    for i in range(count):
        o = rec_off + 128 * i
        no, _pad, depth = struct.unpack_from(">HHH", d, o)
        f = struct.unpack_from(">24f", d, o + 8)
        b = Bone()
        b.name = _cstr(d, no)
        b.index = i
        b.depth = depth
        b.local = _m43(f[:12])
        b.inv_bind = _m43(f[12:24])
        while stack and stack[-1].depth >= depth:
            stack.pop()
        b.parent = stack[-1].index if stack else -1
        stack.append(b)
        bones.append(b)
    return bones


# ----------------------------------------------------------------------
class Collision:
    __slots__ = ("name", "verts", "tris", "params")


def parse_col(block):
    d = block.data
    node_off = struct.unpack_from(">I", d, 4)[0]
    nnodes = struct.unpack_from(">H", d, 8)[0]
    verts = []
    tris = []
    base = 0
    for k in range(nnodes):
        o = node_off + 32 * k
        if o + 32 > len(d):
            break
        leaf_off = struct.unpack_from(">I", d, o + 24)[0]
        leaf_idx = d[o + 29]  # flag word: depth, leaf index (0xFF = inner node), 2 flag bytes
        if leaf_idx == 0xFF:
            continue
        vo, io, nv, ni = struct.unpack_from(">IIHH", d, leaf_off)
        if vo + 16 * nv > len(d) or io + 2 * ni > len(d):
            continue
        v = np.frombuffer(d, ">f4", nv * 4, vo).reshape(nv, 4)[:, :3].astype(np.float32)
        ix = np.frombuffer(d, ">u2", ni, io)
        t = strip_to_triangles(ix)
        verts.append(v)
        tris.append(t + base)
        base += nv
    c = Collision()
    r = block.rsi
    c.name = r.name if r else "collision"
    c.params = r.params if r else {}
    c.verts = np.concatenate(verts) if verts else np.zeros((0, 3), np.float32)
    c.tris = np.concatenate(tris) if tris else np.zeros((0, 3), np.int32)
    return c


# ----------------------------------------------------------------------
class SrdFile:
    """Parsed view over one .srd (+ optional .srdi/.srdv bytes)."""

    def __init__(self, srd, srdi=None, srdv=None, name=""):
        self.name = name
        self.srd, self.srdi, self.srdv = srd, srdi, srdv
        self.blocks = parse_blocks(srd)
        self.textures = {}
        self.texinst = {}
        self.materials = {}
        self.meshes = []
        self.vertex_blocks = {}
        self.trees = []
        self.skeleton = None
        self.collisions = []
        self.scene_name = ""
        for b in self.blocks:
            t = b.tag
            try:
                if t == "$TXR":
                    tx = parse_txr(b)
                    self.textures[tx.name.lower()] = tx
                elif t == "$TXI":
                    ti = parse_txi(b)
                    self.texinst[ti.name] = ti
                elif t == "$MAT":
                    m = parse_mat(b)
                    self.materials[m.name] = m
                elif t == "$MSH":
                    self.meshes.append(parse_msh(b))
                elif t == "$VTX":
                    v = parse_vtx(b)
                    self.vertex_blocks[v.name] = v
                elif t == "$TRE":
                    self.trees.append(parse_tre(b))
                elif t == "$SKL":
                    self.skeleton = parse_skl(b)
                elif t == "$COL":
                    self.collisions.append(b)
                elif t == "$SCN":
                    r = b.rsi
                    self.scene_name = r.name if r else ""
            except (struct.error, IndexError, ValueError):
                # A malformed block should not sink the whole import.
                continue

    def texture_bytes(self, tx):
        return resource_bytes(tx.loc, tx.size, self.srdi, self.srdv, self.srd)

    def mesh_arrays(self, vb):
        """Decode one vertex block: (attributes dict, triangles) or None."""
        vbuf = resource_bytes(vb.vloc, vb.vsize, self.srdi, self.srdv, self.srd)
        ibuf = resource_bytes(vb.iloc, vb.isize, self.srdi, self.srdv, self.srd)
        if vbuf is None or ibuf is None:
            return None
        attrs = decode_vertices(vb, vbuf)
        idx = np.frombuffer(ibuf, ">u2", len(ibuf) // 2)
        tris = strip_to_triangles(idx, vb.prim)
        if len(tris):
            tris = tris[(tris < vb.count).all(1)]
        return attrs, tris
