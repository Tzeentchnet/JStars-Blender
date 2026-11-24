"""RSX (PS3) shader programs from ``*_shader.srd`` files.

Each ``$VSD`` / ``$PSD`` block holds one compiled Cg program. The block data is
a parameter table (inputs, uniforms, samplers, strings) and the program
microcode sits inside the block's ``$RSI`` data at the resource offset.

Only fragment programs are decoded. See docs/FORMATS.md for the layouts.
"""
import struct

from . import srd as S

# Fragment opcodes (6 bits from the destination word, plus bit 31 of SRC1).
OPS = {
    0x00: "NOP", 0x01: "MOV", 0x02: "MUL", 0x03: "ADD", 0x04: "MAD", 0x05: "DP3",
    0x06: "DP4", 0x07: "DST", 0x08: "MIN", 0x09: "MAX", 0x0A: "SLT", 0x0B: "SGE",
    0x0C: "SLE", 0x0D: "SGT", 0x0E: "SNE", 0x0F: "SEQ", 0x10: "FRC", 0x11: "FLR",
    0x12: "KIL", 0x13: "PK4", 0x14: "UP4", 0x15: "DDX", 0x16: "DDY", 0x17: "TEX",
    0x18: "TXP", 0x19: "TXD", 0x1A: "RCP", 0x1B: "RSQ", 0x1C: "EX2", 0x1D: "LG2",
    0x1E: "LIT", 0x1F: "LRP", 0x20: "STR", 0x21: "SFL", 0x22: "COS", 0x23: "SIN",
    0x24: "PK2", 0x25: "UP2", 0x26: "POW", 0x27: "PKB", 0x28: "UPB", 0x29: "PK16",
    0x2A: "UP16", 0x2B: "BEM", 0x2C: "PKG", 0x2D: "UPG", 0x2E: "DP2A", 0x2F: "TXL",
    0x31: "TXB", 0x33: "TEXBEM", 0x34: "TXPBEM", 0x35: "BEMLUM", 0x36: "REFL",
    0x37: "TIMESWTEX", 0x38: "DP2", 0x39: "NRM", 0x3A: "DIV", 0x3B: "DIVSQ",
    0x3C: "LIF", 0x3D: "FENCT", 0x3E: "FENCB",
    0x40: "BRK", 0x41: "CAL", 0x42: "IFE", 0x43: "LOOP", 0x44: "REP", 0x45: "RET",
}

# Fragment input registers (the destination word's input-attribute field).
INPUTS = ("WPOS", "COL0", "COL1", "FOGC", "TEX0", "TEX1", "TEX2", "TEX3", "TEX4",
          "TEX5", "TEX6", "TEX7", "TEX8", "TEX9", "SSA")

TEXTURE_OPS = ("TEX", "TXP", "TXB", "TXL", "TXD")
SCALE = {0: 1.0, 1: 2.0, 2: 4.0, 3: 8.0, 5: 0.5, 6: 0.25, 7: 0.125}


def _hswap(w):
    return ((w >> 16) | (w << 16)) & 0xFFFFFFFF


class Src:
    __slots__ = ("kind", "index", "half", "swizzle", "neg", "abs")

    def __init__(self, word, abs_bit):
        self.kind = ("temp", "input", "const", "?")[word & 3]
        self.index = (word >> 2) & 0x3F
        self.half = bool((word >> 8) & 1)
        self.swizzle = tuple((word >> (9 + 2 * k)) & 3 for k in range(4))
        self.neg = bool((word >> 17) & 1)
        self.abs = bool((word >> abs_bit) & 1)


class Instr:
    __slots__ = ("offset", "op", "end", "dst", "dst_half", "mask", "no_dest", "sat",
                 "scale", "input", "tex_unit", "src", "const", "conditional")

    def __str__(self):
        def s(x):
            if x.kind == "temp":
                r = ("H%d" if x.half else "R%d") % x.index
            elif x.kind == "input":
                r = INPUTS[self.input] if self.input < len(INPUTS) else "IN%d" % self.input
            else:
                r = "C%s" % (self.const,)
            sw = "".join("xyzw"[c] for c in x.swizzle)
            r += "" if sw == "xyzw" else "." + sw
            r = "|%s|" % r if x.abs else r
            return "-" + r if x.neg else r
        d = "" if self.no_dest else ("H%d" if self.dst_half else "R%d") % self.dst
        m = "".join("xyzw"[k] for k in range(4) if self.mask[k])
        return "%04x %s%s %s.%s, %s%s" % (self.offset, self.op, "_SAT" if self.sat else "", d, m,
                                          ", ".join(s(x) for x in self.src),
                                          " tex%d" % self.tex_unit if self.op in TEXTURE_OPS else "")


def decode_fragment(code):
    """RSX fragment microcode -> [Instr]. Inline constants are folded into
    the instruction that reads them (``Instr.const``, 4 floats)."""
    out = []
    n = len(code) // 16
    i = 0
    while i < n:
        d, s0, s1, s2 = (_hswap(w) for w in struct.unpack_from(">4I", code, 16 * i))
        ins = Instr()
        ins.offset = 16 * i
        opc = ((d >> 24) & 0x3F) | (((s1 >> 31) & 1) << 6)
        ins.op = OPS.get(opc, "OP%02X" % opc)
        ins.end = bool(d & 1)
        ins.dst = (d >> 1) & 0x3F
        ins.dst_half = bool((d >> 7) & 1)
        ins.mask = tuple(bool((d >> (9 + k)) & 1) for k in range(4))
        ins.input = (d >> 13) & 0xF
        ins.tex_unit = (d >> 17) & 0xF
        ins.no_dest = bool((d >> 30) & 1)
        ins.sat = bool((d >> 31) & 1)
        ins.scale = SCALE.get((s1 >> 28) & 7, 1.0)
        # exec-if bits (lt, eq, gt) in SRC0: all set = unconditional
        ins.conditional = ((s0 >> 18) & 7) != 7
        ins.src = [Src(s0, 29), Src(s1, 18), Src(s2, 18)]
        ins.const = None
        i += 1
        if any(x.kind == "const" for x in ins.src) and i < n:
            words = [_hswap(w) for w in struct.unpack_from(">4I", code, 16 * i)]
            ins.const = struct.unpack(">4f", struct.pack(">4I", *words))   # at ins.offset + 16
            i += 1
        out.append(ins)
        if ins.end:
            break
    return out


class Program:
    """One $PSD / $VSD block."""
    __slots__ = ("name", "kind", "code", "inputs", "uniforms", "samplers")

    def instructions(self):
        return decode_fragment(self.code)

    def uniform_at(self, const_offset):
        """Name of the uniform patched into the inline constant at a byte offset, or None."""
        for name, offs in self.uniforms.items():
            if const_offset in offs:
                return name
        return None


def parse_program(block):
    """$PSD/$VSD block -> Program.

    Data header: ``u32 4, u16 inputs_off, u16 outputs_off, u16 patch_off,
    u16 uniforms_off, u16 samplers_off, u8 n_inputs, u8 n_outputs,
    u8 n_uniforms, u8 n_samplers``.
    Uniform (12 bytes): ``u16 name, u16 semantic, u16 patch_entry, ...``;
    the patch entry is ``u16 list_off, u8 count, u8``, the list is ``u32``
    byte offsets of the inline constants in the microcode.
    Sampler (10 bytes): ``u16 name, u16 semantic, u8 unit, u8 dim,
    u16 slot_name, u16``.
    """
    d = block.data
    p = Program()
    r = block.rsi
    p.name = r.name if r else ""
    p.kind = block.tag
    loc, size = (r.resources[0] if r and r.resources else (0, 0))
    p.code = r.raw[loc:loc + size] if r else b""
    in_off, _out_off, _patch, uni_off, smp_off = struct.unpack_from(">5H", d, 4)
    n_in, _n_out, n_uni, n_smp = d[0x0E], d[0x0F], d[0x10], d[0x11]
    p.inputs = [struct.unpack_from(">BB", d, in_off + 2 * k) for k in range(n_in)]
    p.uniforms = {}
    for k in range(n_uni):
        o = uni_off + 12 * k
        name_o, sem_o, pe = struct.unpack_from(">HHH", d, o)
        lst, cnt = struct.unpack_from(">HB", d, pe)
        offs = [struct.unpack_from(">I", d, lst + 4 * j)[0] for j in range(cnt)]
        p.uniforms[S._cstr(d, sem_o) or S._cstr(d, name_o)] = offs
    p.samplers = {}
    for k in range(n_smp):
        o = smp_off + 10 * k
        name_o, sem_o, unit, _dim, slot_o = struct.unpack_from(">HHBBH", d, o)
        slot = S._cstr(d, slot_o) if slot_o else ""
        p.samplers[unit] = (S._cstr(d, name_o), S._cstr(d, sem_o), slot)
    return p


def parse_same_table(value):
    """``SameTable`` RSI parameter: ``u32 n`` then n strings, read as
    (alias, program with identical code) pairs."""
    n = struct.unpack_from(">I", value, 0)[0]
    strs = value[4:].split(b"\0")[:n]
    return {strs[k].decode("ascii", "replace"): strs[k + 1].decode("ascii", "replace")
            for k in range(0, len(strs) - 1, 2)}


def load_programs(srd_bytes, kinds=("$PSD",)):
    """All programs of a *_shader.srd, by name (``opbb_shader_<mat>_fu.pcg``).

    Identical programs are stored once. The first block's RSI carries a
    ``SameTable`` parameter that maps every dropped name to the kept one;
    those names are resolved here, so every material's name is present.
    """
    out = {}
    same = {}
    for b in S.parse_blocks(srd_bytes):
        r = b.rsi
        if r is not None and "SameTable" in r.params:
            try:
                same.update(parse_same_table(r.params["SameTable"]))
            except (struct.error, IndexError):
                pass
        if b.tag in kinds:
            try:
                p = parse_program(b)
            except (struct.error, IndexError):
                continue
            out[p.name] = p
    for alias in same:
        target, hops = alias, 0
        while target not in out and target in same and hops < 32:
            target, hops = same[target], hops + 1
        if target in out and alias not in out:
            out[alias] = out[target]
    return out
