"""Offline checks of the format layer against a real disc image.

    set JSTARS_ISO=path\\to\\disc.iso
    python -m pytest tests -q        (or: python tests/test_formats.py)

Skipped when JSTARS_ISO is not set. Nothing from the disc is written.
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from jstars_importer.catalog import Catalog  # noqa: E402
from jstars_importer.build import shader_nodes  # noqa: E402
from jstars_importer.formats import rsx, san, srd, texture  # noqa: E402

ISO = os.environ.get("JSTARS_ISO")


@unittest.skipUnless(ISO and os.path.exists(ISO), "set JSTARS_ISO to run disc tests")
class DiscTests(unittest.TestCase):
    cat = None

    @classmethod
    def setUpClass(cls):
        cls.cat = Catalog()
        cls.cat.scan(ISO)

    def test_index(self):
        self.assertGreater(len(self.cat.models()), 100)
        self.assertGreaterEqual(len([s for s in self.cat.stages() if s["id"] < 12]), 24)

    def test_character_model(self):
        m = next(m for m in self.cat.models() if m["kind"] == "Battle")
        f = srd.SrdFile(*self.cat.srd_triplet(m["pak"], m["srd"]))
        self.assertTrue(f.skeleton)
        W = [None] * len(f.skeleton)
        for b in f.skeleton:
            W[b.index] = (W[b.parent] @ b.local) if b.parent >= 0 else b.local
            np.testing.assert_allclose(W[b.index] @ b.inv_bind, np.eye(4), atol=1e-4)
        agree = disagree = 0
        for vb in f.vertex_blocks.values():
            attrs, tris = f.mesh_arrays(vb)
            P, N = attrs[(1, 0)][:, :3], attrs[(2, 0)][:, :3]
            fn = np.cross(P[tris[:, 1]] - P[tris[:, 0]], P[tris[:, 2]] - P[tris[:, 0]])
            d = (fn * N[tris].sum(1)).sum(1)
            agree += (d > 0).sum()
            disagree += (d < 0).sum()
            if (7, 0) in attrs:
                np.testing.assert_allclose(attrs[(7, 0)].sum(1), 1.0, atol=1e-3)
            if (8, 0) in attrs:
                ix = attrs[(8, 0)]
                self.assertTrue(np.issubdtype(ix.dtype, np.integer))
                self.assertTrue(((ix >= 0) & (ix < max(1, len(vb.bones)))).all())
                if len(vb.bones) > 1:
                    # multi-bone meshes must reference more than bone 0
                    self.assertGreater(ix.max(), 0)
        self.assertGreater(agree, disagree * 10)

    def test_textures(self):
        m = next(m for m in self.cat.models() if m["kind"] == "Battle")
        f = srd.SrdFile(*self.cat.srd_triplet(m["pak"], m["srd"]))
        for t in list(f.textures.values())[:5]:
            px = texture.decode_rgba(t.fmt, t.width, t.height, f.texture_bytes(t))
            self.assertEqual(px.shape, (t.height, t.width, 4))
            self.assertIsNotNone(texture.make_dds(t.fmt, t.width, t.height, f.texture_bytes(t), t.mips))

    def test_stage_shaders(self):
        pak = "map/battle_map/map00_m.pak"
        ents = self.cat.pak_entries(pak)
        progs = rsx.load_programs(ents["map00_shader.srd"])
        f = srd.SrdFile(*self.cat.srd_triplet(pak, "map00.srd"))
        # every material of the main map resolves to a program (SameTable aliases included)
        for m in f.meshes:
            fu = [n for n in f.materials[m.mat].shaders if n.endswith("_fu.pcg")]
            self.assertTrue(fu and fu[0] in progs, m.mat)

        def units(e, seen=None):
            seen = set() if seen is None else seen
            if e.id in seen:
                return set()
            seen.add(e.id)
            u = {e.val[0]} if e.op == "tex" else {"COL0"} if e.op == "col" else set()
            for a in e.args:
                u |= units(a, seen)
            return u

        # a_L141: splat texture (COLORMAP0) blends COLORMAP1/2; no lightmap in the albedo
        p = progs[next(n for n in f.materials["a_L141"].shaders if n.endswith("_fu.pcg"))]
        r = shader_nodes.run_program(p)
        self.assertTrue(all(c is not None for c in r.color))
        self.assertEqual(units(r.color[0]), {0, 1, 2})
        self.assertIsNotNone(r.normal)
        # a_a_a_lambert108: three layers weighted by vertex colour
        p = progs[next(n for n in f.materials["a_a_a_lambert108"].shaders if n.endswith("_fu.pcg"))]
        r = shader_nodes.run_program(p)
        self.assertEqual(units(r.color[0]), {0, 1, 2, "COL0"})
        # lambert138 (sdo shadow objects): constant black
        p = progs["opbb_shader_lambert138_fu.pcg"]
        r = shader_nodes.run_program(p)
        self.assertEqual([c.val for c in r.color], [0.0, 0.0, 0.0])

    def test_motion_quaternions(self):
        ms = next(s for s in self.cat.motion_sets() if s["kind"] == "Fighter")
        ents = self.cat.pak_entries(ms["pak"])
        for name in ms["motions"][:10]:
            for mo in san.parse_san(ents[name]):
                for tr in mo.tracks:
                    if len(tr.r_vals):
                        np.testing.assert_allclose(np.linalg.norm(tr.r_vals, axis=1), 1.0, atol=1e-4)


class OfflineTests(unittest.TestCase):
    def test_strips(self):
        tris = srd.strip_to_triangles(np.array([0, 1, 2, 3, 0xFFFF, 4, 5, 6]))
        self.assertEqual(tris.tolist(), [[0, 1, 2], [2, 1, 3], [4, 5, 6]])

    def test_quat_decode_identity(self):
        # idx 3 (w dropped), x = y = z = 0 -> fields at the midpoint
        mid = 0x7FFFF
        v = np.array([(3 << 60) | (mid << 40) | (mid << 20) | mid], np.uint64)
        q = san.decode_quats(v)[0]
        np.testing.assert_allclose(q, [1, 0, 0, 0], atol=1e-5)

    def test_quat_decode_field_order(self):
        # idx 3 (w dropped); x lives in the LOW 20 bits. A 90 deg turn about X
        # (w = x = 1/sqrt2) puts x at the field maximum.
        mid, top = 0x7FFFF, 0xFFFFF
        v = np.array([(3 << 60) | (mid << 40) | (mid << 20) | top], np.uint64)
        q = san.decode_quats(v)[0]
        np.testing.assert_allclose(q, [0.70710678, 0.70710678, 0, 0], atol=1e-5)
        # idx 0 (x dropped): fields are y, z, w from the low bits
        v = np.array([(0 << 60) | (top << 40) | (mid << 20) | mid], np.uint64)
        q = san.decode_quats(v)[0]
        np.testing.assert_allclose(q, [0.70710678, 0.70710678, 0, 0], atol=1e-5)

    def test_u32_blend_indices(self):
        # one vertex, stream stride 16: weights would follow; indices (0x14) at offset 0
        vb = srd.VertexBlock()
        vb.count = 1
        vb.attrs = [(0, 0, srd.SEM_INDICES, 0, 0x14)]
        vb.streams = [(0, 16)]
        buf = np.array([4, 0, 7, 1], ">u4").tobytes()
        ix = srd.decode_vertices(vb, buf)[(srd.SEM_INDICES, 0)]
        self.assertEqual(ix.tolist(), [[4, 0, 7, 1]])

    @staticmethod
    def _fp(op, dst=0, mask="xyzw", inp=0, unit=0, srcs=(("temp", 0),), end=False, const=None):
        """Encode one RSX fragment instruction (plus inline constant)."""
        swz = 0 | (1 << 2) | (2 << 4) | (3 << 6)
        d = (int(end) | (dst << 1) | sum(1 << (9 + "xyzw".index(c)) for c in mask)
             | (inp << 13) | (unit << 17) | ((op & 0x3F) << 24))
        words = [d]
        for k in range(3):
            kind, idx = srcs[k] if k < len(srcs) else ("temp", 0)
            w = {"temp": 0, "input": 1, "const": 2}[kind] | (idx << 2) | (swz << 9)
            if k == 0:
                w |= 7 << 18                     # unconditional
            words.append(w)
        hs = [((w >> 16) | (w << 16)) & 0xFFFFFFFF for w in words]
        out = np.array(hs, ">u4").tobytes()
        if const is not None:
            c = np.array(const, ">f4").view(">u4")
            out += np.array([((int(w) >> 16) | (int(w) << 16)) & 0xFFFFFFFF for w in c], ">u4").tobytes()
        return out

    def test_fragment_program(self):
        # R0 = tex0(TEX0); R1 = tex6(TEX2) * 2; R0.xyz = R0 * R1; R0 = R0 / max(...)
        code = (self._fp(0x17, dst=0, inp=4, unit=0, srcs=(("input", 0),))
                + self._fp(0x17, dst=1, inp=6, unit=6, srcs=(("input", 0),))
                + self._fp(0x02, dst=1, mask="xyz", srcs=(("temp", 1), ("const", 0)), const=(2, 2, 2, 2))
                + self._fp(0x02, dst=0, mask="xyz", srcs=(("temp", 0), ("temp", 1)))
                + self._fp(0x01, dst=0, mask="w", srcs=(("const", 0),), const=(0.25, 0, 0, 0))
                + self._fp(0x3A, dst=0, srcs=(("temp", 0), ("temp", 0)), end=True))
        ins = rsx.decode_fragment(code)
        self.assertEqual([i.op for i in ins], ["TEX", "TEX", "MUL", "MUL", "MOV", "DIV"])
        self.assertEqual(ins[2].const, (2.0, 2.0, 2.0, 2.0))
        self.assertTrue(ins[-1].end)
        p = rsx.Program()
        p.name, p.kind, p.code, p.inputs = "test_fu.pcg", "$PSD", code, []
        p.uniforms = {"DIRECTIONALLIGHTCOLOR": []}
        p.samplers = {0: ("samp00", "", "COLORMAP0"), 6: ("lightMap", "TEXUNIT6", "")}
        r = shader_nodes.run_program(p)
        # albedo = the colour map alone; the lightmap and the packing are stripped
        self.assertEqual([(c.op, c.val[0], c.val[3]) for c in r.color], [("tex", 0, 0), ("tex", 0, 1), ("tex", 0, 2)])
        self.assertIsNone(r.alpha)
        self.assertFalse(r.unlit)

    def test_dxt1_solid(self):
        blk = np.zeros((1, 8), np.uint8)
        blk[0, 0:2] = [0x00, 0xF8]   # pure red, index 0
        px = texture.decode_rgba(texture.FMT_DXT1, 4, 4, blk.tobytes())
        self.assertTrue((px[..., 0] == 255).all() and (px[..., 1] == 0).all())


if __name__ == "__main__":
    unittest.main()
