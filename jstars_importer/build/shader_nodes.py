"""Rebuild a stage material's colour from its compiled RSX fragment program.

Stage materials have no fixed layer recipe: every $MAT has its own Cg
program (``opbb_shader_<mat>_fu.pcg``) that blends its COLORMAPs with splat
textures, vertex-colour weights, baked-in tint constants and a slope term.
This module runs that program symbolically and turns the part that does not
depend on lighting into Blender nodes.

1. Emulate the microcode per component into an interned expression DAG.
   Runtime uniforms stay symbolic (UVOFFSETn scroll offsets are taken as 0).
   The view-space frame is replaced by an identity tangent frame
   (TEX6 normal = +Z, TEX7 tangent = +X, facing = +1), so the normal that the
   program dots with the light direction comes out as a tangent-space normal.
   A dot product with UPDIRVIEW becomes "world normal . up".
2. Albedo = the largest sub-expression of the output colour that depends on a
   COLORMAP (or vertex colour) but not on lights, the lightmap or other
   runtime inputs. The lightmap itself is not wired.
3. Emit nodes. x/y/z expressions with the same structure become one vector
   node; the rest are built per channel and combined.

The game shades in gamma space, so the textures and vertex colours are read
raw (Non-Color / float colours) and the albedo is converted with gamma 2.2.
"""
import math


from ..formats import rsx

# ---------------------------------------------------------------- expressions
LEAVES = ("c", "tex", "col", "uv", "uni", "in", "updot")
COMMUTATIVE = ("add", "mul", "min", "max")


class Expr:
    __slots__ = ("op", "args", "val", "id", "shape", "tags", "size")


class Dag:
    """Hash-consed scalar expressions with light algebraic simplification."""

    def __init__(self, tex_tag):
        self.table = {}
        self.tex_tag = tex_tag      # unit -> "color" | "tex" | "lightmap" | "in"
        self.coords = {}            # (u id, v id) -> (u, v) texture coordinates

    # -- construction
    def _intern(self, op, args=(), val=None):
        key = (op, tuple(a.id for a in args), val)
        e = self.table.get(key)
        if e is not None:
            return e
        e = Expr()
        e.op, e.args, e.val, e.id = op, tuple(args), val, len(self.table)
        if op == "c":
            e.shape, e.tags, e.size = ("c",), frozenset(), 1
        elif op in LEAVES:
            e.shape = (op,) + tuple(v for v in (val if isinstance(val, tuple) else (val,))[:-1]) \
                if op in ("tex", "col", "uv", "uni", "in") else (op,)
            e.tags = frozenset(self._leaf_tags(op, val))
            e.size = 1
        else:
            e.shape = (op,) + tuple(a.shape for a in args)
            e.tags = frozenset().union(*(a.tags for a in args))
            e.size = 1 + sum(a.size for a in args)
        self.table[key] = e
        return e

    def _leaf_tags(self, op, val):
        """Tags: color / tex / vcol / geo are material data; anything in BAD is
        runtime state. calpha marks a colour texture's alpha channel; lightN /
        lightmapN carry the channel so the albedo search stays per channel."""
        if op == "tex":
            t = self.tex_tag(val[0])
            if t == "lightmap":
                return ("lightmap", "lightmap%d" % val[3])
            return ("calpha",) if t == "color" and val[3] == 3 else (t,)
        if op == "col":
            return ("vcol",)
        if op in ("uv", "updot"):
            return ("geo",)
        if op == "uni" and "LIGHT" in val[0]:
            return ("light", "light%d" % val[1])
        return ("in",)          # other uniforms and inputs

    def const(self, v):
        return self._intern("c", (), float(v))

    def leaf(self, op, *val):
        return self._intern(op, (), tuple(val) if val else None)

    @staticmethod
    def is_c(e, v=None):
        return e.op == "c" and (v is None or e.val == v)

    def _order(self, a, b):
        ka = (a.op == "c", a.shape, a.id)
        kb = (b.op == "c", b.shape, b.id)
        return (a, b) if ka <= kb else (b, a)

    def add(self, a, b):
        if self.is_c(a) and self.is_c(b):
            return self.const(a.val + b.val)
        if self.is_c(a, 0.0):
            return b
        if self.is_c(b, 0.0):
            return a
        a, b = self._order(a, b)
        # x*c1 + x*c2 -> x*(c1 + c2);  x + x*c -> x*(1 + c)
        fa = (a.args[0], a.args[1].val) if a.op == "mul" and self.is_c(a.args[1]) else (a, 1.0)
        fb = (b.args[0], b.args[1].val) if b.op == "mul" and self.is_c(b.args[1]) else (b, 1.0)
        if fa[0] is fb[0] and a.op != "c":
            return self.mul(fa[0], self.const(fa[1] + fb[1]))
        # (x + c1) + c2 -> x + (c1 + c2)
        if self.is_c(b) and a.op == "add" and self.is_c(a.args[1]):
            return self.add(a.args[0], self.const(a.args[1].val + b.val))
        # q + f * (p - q) -> mix(q, p, f)
        for q, m in ((a, b), (b, a)):
            if m.op == "mul":
                for f, d in (m.args, m.args[::-1]):
                    if d.op == "add":
                        p, nq = d.args
                        for p_, nq_ in ((p, nq), (nq, p)):
                            if nq_.op == "mul" and self.is_c(nq_.args[1], -1.0) and nq_.args[0] is q:
                                return self.mix(q, p_, f)
        return self._intern("add", (a, b))

    def mul(self, a, b):
        if self.is_c(a) and self.is_c(b):
            return self.const(a.val * b.val)
        a, b = self._order(a, b)
        if self.is_c(b, 0.0):
            return b
        if self.is_c(b, 1.0):
            return a
        if self.is_c(b) and a.op == "mul" and self.is_c(a.args[1]):
            return self.mul(a.args[0], self.const(a.args[1].val * b.val))
        if self.is_c(b, -1.0) and a.op == "add":
            return self.add(self.neg(a.args[0]), self.neg(a.args[1]))
        return self._intern("mul", (a, b))

    def neg(self, a):
        return self.mul(a, self.const(-1.0))

    def mix(self, a, b, f):
        if self.is_c(f, 0.0) or a is b:
            return a
        if self.is_c(f, 1.0):
            return b
        if self.is_c(a) and self.is_c(b) and self.is_c(f):
            return self.const(a.val + (b.val - a.val) * f.val)
        return self._intern("mix", (a, b, f))

    _UNARY = {
        "abs": abs, "frc": lambda x: x - math.floor(x), "flr": math.floor,
        "rcp": lambda x: 1.0 / x if x else 3.4e38,
        "rsq": lambda x: 1.0 / math.sqrt(abs(x)) if x else 3.4e38,
        "ex2": lambda x: 2.0 ** min(x, 127.0), "lg2": lambda x: math.log2(x) if x > 0 else -3.4e38,
        "sat": lambda x: min(max(x, 0.0), 1.0), "cos": math.cos, "sin": math.sin,
    }

    def un(self, op, a):
        if self.is_c(a):
            return self.const(self._UNARY[op](a.val))
        if op == "sat" and a.op == "sat":
            return a
        if op == "abs" and a.op in ("abs", "sat"):
            return a
        return self._intern(op, (a,))

    _BINARY = {
        "min": min, "max": max,
        "slt": lambda a, b: float(a < b), "sge": lambda a, b: float(a >= b),
        "sle": lambda a, b: float(a <= b), "sgt": lambda a, b: float(a > b),
        "sne": lambda a, b: float(a != b), "seq": lambda a, b: float(a == b),
    }

    def bin(self, op, a, b):
        if self.is_c(a) and self.is_c(b):
            return self.const(self._BINARY[op](a.val, b.val))
        if op in COMMUTATIVE:
            a, b = self._order(a, b)
        return self._intern(op, (a, b))


# ---------------------------------------------------------------- emulation
FRAME = {"TEX6": (0.0, 0.0, 1.0), "TEX7": (1.0, 0.0, 0.0)}


class Result:
    __slots__ = ("color", "alpha", "normal", "out", "dag", "unlit")


LIGHT_UNIFORMS = ("DIRECTIONALLIGHTCOLOR", "AMBIENTLIGHTCOLOR", "POINTLIGHT0COLOR", "SUN_COLOR")


def run_program(prog, threshold=0.5):
    """Symbolically execute a fragment Program. Returns Result.

    threshold: the material's OpBinaryThreshold, used for alpha-tested
    programs (they compare an alpha value against the BINARYTHRESHOLD uniform).
    """
    samplers = prog.samplers

    def tex_tag(unit):
        slot = samplers.get(unit, ("", "", ""))[2].upper()
        if slot.startswith("COLORMAP"):
            return "color"
        if slot.startswith("TRANSPARENCYMAP"):
            return "tmap"
        if slot:
            return "tex"
        if samplers.get(unit, ("", "", ""))[1] == "TEXUNIT6":
            return "lightmap"
        return "in"             # screen-space noise (TEXUNIT5), shadow maps, ...

    dag = Dag(tex_tag)
    regs = {}
    normal = None
    zero = dag.const(0.0)

    def inp(attr, c):
        name = rsx.INPUTS[attr] if attr < len(rsx.INPUTS) else "IN%d" % attr
        if name == "COL0":
            return dag.leaf("col", c)
        if name in FRAME and c < 3:
            return dag.const(FRAME[name][c])
        if name == "SSA":
            return dag.const(1.0)
        if name.startswith("TEX") and int(name[3:]) < 4 and c < 2:
            return dag.leaf("uv", int(name[3:]), c)
        return dag.leaf("in", name, c)

    def read(ins, k):
        s = ins.src[k]
        if s.kind == "temp":
            r = regs.get(("H" if s.half else "R", s.index))
            vals = [r[c] if r and r[c] is not None else zero for c in s.swizzle]
        elif s.kind == "input":
            vals = [inp(ins.input, c) for c in s.swizzle]
        elif s.kind == "const":
            u = prog.uniform_at(ins.offset + 16)
            if u is None:
                vals = [dag.const(ins.const[c]) for c in s.swizzle]
            elif u.startswith("UVOFFSET"):
                vals = [zero] * 4
            else:
                vals = [dag.leaf("uni", u, c) for c in s.swizzle]
        else:
            vals = [zero] * 4
        if s.abs:
            vals = [dag.un("abs", v) for v in vals]
        if s.neg:
            vals = [dag.neg(v) for v in vals]
        return vals

    def uniform_of(ins, k):
        s = ins.src[k]
        return prog.uniform_at(ins.offset + 16) if s.kind == "const" else None

    out_reg = ("R", 0)
    pre_pack = None
    alpha_test = None
    instrs = prog.instructions()
    for ins in instrs:
        op = ins.op
        if op in ("NOP", "FENCT", "FENCB"):
            continue
        a, b, c = read(ins, 0), read(ins, 1), read(ins, 2)
        res = None
        if op == "MOV":
            res = a
        elif op == "MUL":
            res = [dag.mul(x, y) for x, y in zip(a, b)]
        elif op == "ADD":
            res = [dag.add(x, y) for x, y in zip(a, b)]
        elif op == "MAD":
            res = [dag.add(dag.mul(x, y), z) for x, y, z in zip(a, b, c)]
        elif op in ("DP2", "DP3", "DP4"):
            n = {"DP2": 2, "DP3": 3, "DP4": 4}[op]
            us = (uniform_of(ins, 0), uniform_of(ins, 1))
            if "UPDIRVIEW" in us:
                v = dag.leaf("updot")
            else:
                if "DIRECTIONALLIGHTDIRECTION" in us and normal is None:
                    normal = (b if us[0] else a)[:3]
                v = zero
                for i in range(n):
                    v = dag.add(v, dag.mul(a[i], b[i]))
            res = [v] * 4
        elif op in ("MIN", "MAX"):
            res = [dag.bin(op.lower(), x, y) for x, y in zip(a, b)]
        elif op in ("SLT", "SGE", "SLE", "SGT", "SNE", "SEQ"):
            res = [dag.bin(op.lower(), x, y) for x, y in zip(a, b)]
            us = (uniform_of(ins, 0), uniform_of(ins, 1))
            if "BINARYTHRESHOLD" in us and alpha_test is None:
                # alpha test: value >= threshold (operands swapped for SLE etc.)
                alpha_test = (b if us[0] else a)[0]
        elif op in ("FRC", "FLR"):
            res = [dag.un(op.lower(), x) for x in a]
        elif op in ("RCP", "RSQ", "EX2", "LG2", "COS", "SIN"):
            res = [dag.un(op.lower(), a[0])] * 4
        elif op == "POW":
            res = [dag.un("ex2", dag.mul(dag.un("lg2", a[0]), b[0]))] * 4
        elif op == "LRP":
            res = [dag.mix(z, y, x) for x, y, z in zip(a, b, c)]
        elif op == "DIV":
            if ins.end:
                pre_pack = a        # out = colour / max(r, g, b, 0.25): HDR packing
            r = dag.un("rcp", b[0])
            res = [dag.mul(x, r) for x in a]
        elif op == "DIVSQ":
            r = dag.un("rsq", b[0])
            res = [dag.mul(x, r) for x in a]
        elif op == "NRM":
            ln = dag.un("rsq", dag.add(dag.add(dag.mul(a[0], a[0]), dag.mul(a[1], a[1])), dag.mul(a[2], a[2])))
            res = [dag.mul(x, ln) for x in a[:3]] + [a[3]]
        elif op in ("TEX", "TXB", "TXL", "TXP"):
            u, v = a[0], a[1]
            if op == "TXP":
                r = dag.un("rcp", a[3])
                u, v = dag.mul(u, r), dag.mul(v, r)
            res = [dag.leaf("tex", ins.tex_unit, u.id, v.id, ch) for ch in range(4)]
            dag.coords[(u.id, v.id)] = (u, v)
        else:
            res = [dag.leaf("in", "op_" + op, ch) for ch in range(4)]
        if ins.no_dest:
            continue
        if ins.scale != 1.0:
            s = dag.const(ins.scale)
            res = [dag.mul(x, s) for x in res]
        if ins.sat:
            res = [dag.un("sat", x) for x in res]
        key = ("H" if ins.dst_half else "R", ins.dst)
        cur = regs.setdefault(key, [None] * 4)
        for k in range(4):
            if ins.mask[k]:
                # Conditional writes (fades, dithering, fog) are skipped
                # unless the register has no value yet.
                if ins.conditional and cur[k] is not None:
                    continue
                cur[k] = res[k]
        if key in (("R", 0), ("H", 0)):
            out_reg = key

    out = regs.get(out_reg) or [zero] * 4
    out = [v if v is not None else zero for v in out]
    if pre_pack is not None:
        # Undo the packing; the pre-pack w is the real alpha (0.25 when opaque)
        out = list(pre_pack[:4])
    r = Result()
    r.dag = dag
    r.out = out
    lit = any(u in prog.uniforms for u in LIGHT_UNIFORMS)
    r.unlit = False
    r.color = [_albedo(e, k) for k, e in enumerate(out[:3])]
    if any(e is None for e in r.color):
        # Unlit programs, or colour that never meets a light (sky, fog cards)
        r.color = [_largest_clean(e, COLOUR) for e in out[:3]]
        if all(e is not None for e in r.color):
            r.unlit = not lit
        elif all(Dag.is_c(e) for e in out[:3]):
            r.color, r.unlit = list(out[:3]), True
        elif lit and not any(t in e.tags for e in out[:3] for t in COLOUR):
            r.color = [dag.const(1.0)] * 3        # lit, untextured: white
    if alpha_test is not None and _clean(alpha_test):
        r.alpha = dag.bin("sge", alpha_test, dag.const(threshold))
    else:
        a = _largest_clean(out[3], ("calpha", "tmap"))
        r.alpha = a if a is not None and not Dag.is_c(a) else None
    if normal is not None and all(_clean(x) for x in normal) and \
            not all(Dag.is_c(x) for x in normal):
        r.normal = list(normal)
    else:
        r.normal = None
    return r


BAD = frozenset(("in", "light", "lightmap"))
COLOUR = ("color", "calpha", "vcol")       # sources of surface colour


def _clean(e):
    return not (e.tags & BAD)


def _walk(root):
    seen = set()
    stack = [root]
    while stack:
        e = stack.pop()
        if e.id in seen:
            continue
        seen.add(e.id)
        yield e
        stack.extend(e.args)


def _albedo(root, k):
    """Channel k of the surface colour: the light-independent factor that the
    program multiplies by lightmap channel k (preferred) or by light colour k.
    (Output packing divides each channel by max(r, g, b), so every channel's
    DAG holds all three products: the channel tag picks the right one.)"""
    best = None
    for e in _walk(root):
        if e.op != "mul":
            continue
        for a, b in (e.args, e.args[::-1]):
            if not (_clean(a) and any(t in a.tags for t in COLOUR)):
                continue
            rank = 2 if "lightmap%d" % k in b.tags else 1 if "light%d" % k in b.tags else 0
            if rank and (best is None or (rank, a.size) > best[0]):
                best = ((rank, a.size), a)
    return best[1] if best else None


def _largest_clean(root, need):
    best = None
    for e in _walk(root):
        if _clean(e) and any(t in e.tags for t in need) and (best is None or e.size > best.size):
            best = e
    return best


def light_only(prog):
    """True when a program outputs lighting alone: it reads light uniforms but
    no texture and no vertex colour. Stages use these for shadow meshes
    (``sdw``). Constant-colour programs (black pit fills) are not included."""
    if prog.samplers or not any("LIGHT" in u for u in prog.uniforms):
        return False
    col0 = rsx.INPUTS.index("COL0")
    return not any(ins.input == col0 and any(s.kind == "input" for s in ins.src)
                   for ins in prog.instructions())


# ---------------------------------------------------------------- node emission
class Emitter:
    def __init__(self, nt, images, uv_names, sampler_uv, up_axis, x0=-300):
        self.nt = nt
        self.images = images            # unit -> bpy image or None
        self.uv_names = uv_names        # TEXn -> UV map name
        self.sampler_uv = sampler_uv    # unit -> UV map name (from the mesh's sampler list)
        self.up_axis = up_axis
        self.memo = {}
        self.vmemo = {}
        self.cols = {}
        self.x0 = x0
        self.normal_socket = None

    # -- helpers
    def node(self, kind, depth, label=None):
        n = self.nt.nodes.new(kind)
        y = self.cols.get(depth, 0)
        self.cols[depth] = y - 180
        n.location = (self.x0 - 200 * depth, y)
        n.hide = kind in ("ShaderNodeMath", "ShaderNodeVectorMath", "ShaderNodeSeparateColor",
                          "ShaderNodeSeparateXYZ", "ShaderNodeCombineXYZ")
        if label:
            n.label = label
        return n

    def link(self, value, sock):
        if isinstance(value, (int, float)):
            if sock.type in ("VECTOR", "RGBA"):
                v = (float(value),) * 3
                sock.default_value = v + (1.0,) if sock.type == "RGBA" else v
            else:
                sock.default_value = float(value)
        elif isinstance(value, tuple):
            if sock.type == "RGBA":
                sock.default_value = tuple(value[:3]) + (1.0,)
            elif sock.type == "VECTOR":
                sock.default_value = tuple(value[:3])
            else:
                sock.default_value = float(value[0])
        else:
            self.nt.links.new(value, sock)

    @staticmethod
    def sock(node, name, kind=None):
        for s in node.inputs:
            if s.name == name and s.enabled and (kind is None or s.type == kind):
                return s
        for s in node.inputs:
            if s.name == name and (kind is None or s.type == kind):
                return s
        return node.inputs[name]

    @staticmethod
    def out(node, name, kind=None):
        for s in node.outputs:
            if s.name == name and s.enabled and (kind is None or s.type == kind):
                return s
        return node.outputs[name]

    # -- leaves
    def uv_vector(self, n, depth):
        key = ("uvnode", n)
        if key not in self.memo:
            nd = self.node("ShaderNodeUVMap", depth, "TEXCOORD%d" % n)
            if n < len(self.uv_names):
                nd.uv_map = self.uv_names[n]
            self.memo[key] = nd.outputs["UV"]
        return self.memo[key]

    def texture(self, unit, u, v, depth):
        key = ("img", unit, u.id, v.id)
        if key in self.memo:
            return self.memo[key]
        img = self.images.get(unit)
        if img is None:
            self.memo[key] = None
            return None
        nd = self.node("ShaderNodeTexImage", depth + 1)
        nd.image = img
        nd.label = "unit %d" % unit
        nd.hide = False
        if u.op == "uv" and v.op == "uv" and u.val == (u.val[0], 0) and v.val == (u.val[0], 1):
            n = u.val[0]
            uvn = self.node("ShaderNodeUVMap", depth + 2, "TEXCOORD%d" % n)
            uvn.uv_map = self.sampler_uv.get(unit) or (self.uv_names[n] if n < len(self.uv_names) else "")
            self.nt.links.new(uvn.outputs["UV"], nd.inputs["Vector"])
        else:
            comb = self.node("ShaderNodeCombineXYZ", depth + 2, "game UV")
            self.link(self.s(u, depth + 3), comb.inputs[0])
            flip = self.node("ShaderNodeMath", depth + 2, "1 - v")
            flip.operation = "SUBTRACT"
            flip.inputs[0].default_value = 1.0
            self.link(self.s(v, depth + 3), flip.inputs[1])
            self.nt.links.new(flip.outputs[0], comb.inputs[1])
            self.nt.links.new(comb.outputs[0], nd.inputs["Vector"])
        self.memo[key] = nd
        return nd

    def channel(self, nd_color, nd_alpha, ch, key, depth):
        if ch == 3:
            return nd_alpha
        k = ("sep",) + key
        if k not in self.memo:
            sep = self.node("ShaderNodeSeparateColor", depth)
            self.nt.links.new(nd_color, sep.inputs[0])
            self.memo[k] = sep
        return self.memo[k].outputs[ch]

    def color_attr(self, depth):
        if "colattr" not in self.memo:
            nd = self.node("ShaderNodeVertexColor", depth + 1, "COL0 (blend weights)")
            nd.layer_name = ""          # each stage mesh has one colour set
            self.memo["colattr"] = nd
        return self.memo["colattr"]

    def updot(self, depth):
        if "updot" not in self.memo:
            dot = self.node("ShaderNodeVectorMath", depth, "normal . up")
            dot.operation = "DOT_PRODUCT"
            if self.normal_socket is not None:
                self.nt.links.new(self.normal_socket, dot.inputs[0])
            else:
                geo = self.node("ShaderNodeNewGeometry", depth + 1)
                self.nt.links.new(geo.outputs["Normal"], dot.inputs[0])
            dot.inputs[1].default_value = self.up_axis
            self.memo["updot"] = dot.outputs["Value"]
        return self.memo["updot"]

    # -- scalar
    _MATH = {"add": "ADD", "mul": "MULTIPLY", "min": "MINIMUM", "max": "MAXIMUM", "abs": "ABSOLUTE",
             "frc": "FRACT", "flr": "FLOOR", "rsq": "INVERSE_SQRT", "cos": "COSINE", "sin": "SINE",
             "slt": "LESS_THAN", "sgt": "GREATER_THAN"}

    def s(self, e, depth=0):
        """Scalar expression -> float constant or output socket."""
        if e.op == "c":
            return e.val
        if e.id in self.memo:
            return self.memo[e.id]
        r = self._s(e, depth)
        self.memo[e.id] = r
        return r

    def _s(self, e, depth):
        op = e.op
        if op == "tex":
            unit, uid, vid, ch = e.val
            u, v = self._coords[(uid, vid)]
            nd = self.texture(unit, u, v, depth)
            if nd is None:
                return 1.0
            return self.channel(nd.outputs["Color"], nd.outputs["Alpha"], ch, (unit, uid, vid), depth)
        if op == "col":
            ca = self.color_attr(depth)
            return self.channel(ca.outputs["Color"], ca.outputs["Alpha"], e.val[0], ("col",), depth)
        if op == "uv":
            n, comp = e.val
            k = ("uvsep", n)
            if k not in self.memo:
                sep = self.node("ShaderNodeSeparateXYZ", depth)
                self.nt.links.new(self.uv_vector(n, depth + 1), sep.inputs[0])
                self.memo[k] = sep
            sock = self.memo[k].outputs[comp]
            if comp == 1:       # Blender V was flipped at import: game v = 1 - V
                m = self.node("ShaderNodeMath", depth)
                m.operation = "SUBTRACT"
                m.inputs[0].default_value = 1.0
                self.nt.links.new(sock, m.inputs[1])
                sock = m.outputs[0]
            return sock
        if op == "updot":
            return self.updot(depth)
        if op in ("uni", "in"):
            return 0.0
        if op == "mix":
            m = self.node("ShaderNodeMix", depth, "mix")
            m.data_type = "FLOAT"
            m.clamp_factor = False
            self.link(self.s(e.args[2], depth + 1), self.sock(m, "Factor", "VALUE"))
            self.link(self.s(e.args[0], depth + 1), self.sock(m, "A", "VALUE"))
            self.link(self.s(e.args[1], depth + 1), self.sock(m, "B", "VALUE"))
            return self.out(m, "Result", "VALUE")
        m = self.node("ShaderNodeMath", depth)
        args = e.args
        if op == "sat":
            m.operation, m.use_clamp = "ADD", True
            m.inputs[1].default_value = 0.0     # Math inputs default to 0.5
            args = (e.args[0], None)
        elif op == "rcp":
            m.operation = "DIVIDE"
            m.inputs[0].default_value = 1.0
            self.link(self.s(e.args[0], depth + 1), m.inputs[1])
            return m.outputs[0]
        elif op == "ex2":
            m.operation = "POWER"
            m.inputs[0].default_value = 2.0
            self.link(self.s(e.args[0], depth + 1), m.inputs[1])
            return m.outputs[0]
        elif op == "lg2":
            m.operation = "LOGARITHM"
            m.inputs[1].default_value = 2.0
            args = (e.args[0], None)
        elif op in ("sge", "sle"):
            m.operation = "LESS_THAN" if op == "sge" else "GREATER_THAN"
            inv = self.node("ShaderNodeMath", depth)
            inv.operation = "SUBTRACT"
            inv.inputs[0].default_value = 1.0
            self.nt.links.new(m.outputs[0], inv.inputs[1])
            for i, a in enumerate(args):
                self.link(self.s(a, depth + 1), m.inputs[i])
            m.location.x -= 120
            return inv.outputs[0]
        elif op in ("seq", "sne"):
            m.operation = "COMPARE"
            m.inputs[2].default_value = 0.0
            for i, a in enumerate(args):
                self.link(self.s(a, depth + 1), m.inputs[i])
            if op == "seq":
                return m.outputs[0]
            inv = self.node("ShaderNodeMath", depth)
            inv.operation = "SUBTRACT"
            inv.inputs[0].default_value = 1.0
            self.nt.links.new(m.outputs[0], inv.inputs[1])
            return inv.outputs[0]
        else:
            m.operation = self._MATH[op]
        for i, a in enumerate(args):
            if a is not None:
                self.link(self.s(a, depth + 1), m.inputs[i])
        return m.outputs[0]

    # -- vector
    _VMATH = {"add": "ADD", "mul": "MULTIPLY", "min": "MINIMUM", "max": "MAXIMUM",
              "abs": "ABSOLUTE", "frc": "FRACTION", "flr": "FLOOR"}

    def v(self, trip, depth=0):
        """Three scalar expressions -> constant tuple, float or vector/colour socket."""
        key = tuple(e.id for e in trip)
        if key in self.vmemo:
            return self.vmemo[key]
        r = self._v(trip, depth)
        self.vmemo[key] = r
        return r

    def _v(self, trip, depth):
        x, y, z = trip
        if x is y is z:
            return self.s(x, depth)
        if all(e.op == "c" for e in trip):
            return tuple(e.val for e in trip)
        if all(e.op == "tex" for e in trip) and x.val[:3] == y.val[:3] == z.val[:3] and \
                (x.val[3], y.val[3], z.val[3]) == (0, 1, 2):
            u, v = self._coords[(x.val[1], x.val[2])]
            nd = self.texture(x.val[0], u, v, depth)
            return 1.0 if nd is None else nd.outputs["Color"]
        if all(e.op == "col" for e in trip) and (x.val[0], y.val[0], z.val[0]) == (0, 1, 2):
            return self.color_attr(depth).outputs["Color"]
        if x.op == y.op == z.op and x.op in self._VMATH and len(x.args) == len(y.args) == len(z.args):
            m = self.node("ShaderNodeVectorMath", depth)
            m.operation = self._VMATH[x.op]
            for i in range(len(x.args)):
                self.link(self.v((x.args[i], y.args[i], z.args[i]), depth + 1), m.inputs[i])
            return m.outputs[0]
        if x.op == y.op == z.op == "sat":
            inner = self.v(tuple(e.args[0] for e in trip), depth + 1)
            lo = self.node("ShaderNodeVectorMath", depth, "saturate")
            lo.operation = "MINIMUM"
            self.link(inner, lo.inputs[0])
            lo.inputs[1].default_value = (1.0, 1.0, 1.0)
            hi = self.node("ShaderNodeVectorMath", depth)
            hi.operation = "MAXIMUM"
            self.nt.links.new(lo.outputs[0], hi.inputs[0])
            hi.inputs[1].default_value = (0.0, 0.0, 0.0)
            return hi.outputs[0]
        if x.op == y.op == z.op == "mix":
            m = self.node("ShaderNodeMix", depth, "mix")
            m.data_type = "VECTOR"
            m.clamp_factor = False
            f = tuple(e.args[2] for e in trip)
            if f[0] is f[1] is f[2]:
                m.factor_mode = "UNIFORM"
                self.link(self.s(f[0], depth + 1), self.sock(m, "Factor", "VALUE"))
            else:
                m.factor_mode = "NON_UNIFORM"
                self.link(self.v(f, depth + 1), self.sock(m, "Factor", "VECTOR"))
            self.link(self.v(tuple(e.args[0] for e in trip), depth + 1), self.sock(m, "A", "VECTOR"))
            self.link(self.v(tuple(e.args[1] for e in trip), depth + 1), self.sock(m, "B", "VECTOR"))
            return self.out(m, "Result", "VECTOR")
        comb = self.node("ShaderNodeCombineXYZ", depth)
        for i, e in enumerate(trip):
            self.link(self.s(e, depth + 1), comb.inputs[i])
        return comb.outputs[0]


def build_nodes(mat, prog, images, uv_names, sampler_uv, up_axis, bsdf, threshold=0.5):
    """Wire Base Color (or Emission), Normal and Alpha of ``bsdf`` from ``prog``.

    images: texture unit -> bpy image. Returns False when the program gives
    nothing usable (the caller then falls back to slot-order wiring).
    """
    res = run_program(prog, threshold)
    if any(e is None for e in res.color):
        return False
    nt = mat.node_tree
    em = Emitter(nt, images, uv_names, sampler_uv, up_axis, x0=bsdf.location.x - 600)
    em._coords = res.dag.coords
    for img in images.values():
        if img is not None:
            img.colorspace_settings.name = "Non-Color"
            img.alpha_mode = "CHANNEL_PACKED"

    if res.normal is not None:
        nv = em.v(tuple(res.normal), 2)
        enc = em.node("ShaderNodeVectorMath", 1, "tangent normal -> colour")
        enc.operation = "MULTIPLY_ADD"
        em.link(nv, enc.inputs[0])
        enc.inputs[1].default_value = (0.5, 0.5, 0.5)
        enc.inputs[2].default_value = (0.5, 0.5, 0.5)
        nm = em.node("ShaderNodeNormalMap", 0)
        nm.hide = False
        if uv_names:
            nm.uv_map = uv_names[0]
        nt.links.new(enc.outputs[0], nm.inputs["Color"])
        nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
        em.normal_socket = nm.outputs["Normal"]
        nm.location = (bsdf.location.x - 250, bsdf.location.y - 500)

    albedo = em.v(tuple(res.color), 2)
    gamma = em.node("ShaderNodeGamma", 1, "gamma -> linear")
    gamma.hide = False
    em.link(albedo, gamma.inputs["Color"])
    gamma.inputs["Gamma"].default_value = 2.2
    gamma.location = (bsdf.location.x - 250, bsdf.location.y)
    if res.unlit:
        # Unlit in the game (sky, effects, flat-colour cards): emit the colour.
        bsdf.inputs["Base Color"].default_value = (0.0, 0.0, 0.0, 1.0)
        nt.links.new(gamma.outputs["Color"], bsdf.inputs["Emission Color"])
        bsdf.inputs["Emission Strength"].default_value = 1.0
        mat["srd_unlit"] = True
    else:
        nt.links.new(gamma.outputs["Color"], bsdf.inputs["Base Color"])

    if res.alpha is not None and not Dag.is_c(res.alpha):
        a = em.s(res.alpha, 2)
        if not isinstance(a, float):
            nt.links.new(a, bsdf.inputs["Alpha"])
            try:
                mat.surface_render_method = "DITHERED"
            except AttributeError:
                mat.blend_method = "HASHED"
    mat["srd_shader"] = prog.name
    return True
