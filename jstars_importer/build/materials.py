"""Build Blender materials from SRD $MAT / $TXI / $TXR chains.

Sampler slots seen on the disc: COLORMAPn, NORMALMAPn, SPECULARMAPn,
TRANSPARENCYMAPn, REFLECTMAPn, REFLECTIVITYn. Normal maps are DXT5nm
(X in alpha, Y in green); Z is rebuilt in the node tree.
"""
import struct

import bpy

from ..formats import texture as tx
from . import shader_nodes
from .images import load_texture


def _texture_for_instance(srd_files, inst_name):
    """Follow TXI name -> texture file name -> ($TXR, owning SrdFile)."""
    for f in srd_files:
        ti = f.texinst.get(inst_name)
        if ti is None or not ti.textures:
            continue
        tname = ti.textures[0].lower()
        for g in srd_files:
            t = g.textures.get(tname)
            if t is not None:
                return t, g
        return None, None
    # Some materials reference the texture file directly
    for g in srd_files:
        t = g.textures.get(inst_name.lower())
        if t is not None:
            return t, g
    return None, None


def _params_to_props(mat, params):
    for k, v in params.items():
        if not v or len(v) % 4 or len(v) > 64:
            continue
        try:
            vals = struct.unpack(">%df" % (len(v) // 4), v)
        except struct.error:
            continue
        if any(x != x for x in vals):   # NaN marks "unused"
            continue
        mat["srd_" + k] = vals[0] if len(vals) == 1 else list(vals)


class _Nodes:
    def __init__(self, mat):
        self.nt = mat.node_tree
        self.x = -900

    def new(self, kind, x=None, y=0, label=None):
        n = self.nt.nodes.new(kind)
        n.location = (self.x if x is None else x, y)
        if label:
            n.label = label
        return n

    def link(self, a, b):
        self.nt.links.new(a, b)


def _uv_node(nodes, uv_name, y):
    if not uv_name:
        return None
    n = nodes.new("ShaderNodeUVMap", x=-1250, y=y)
    n.uv_map = uv_name
    return n


def _fragment_program(programs, mat_def):
    if not programs or mat_def is None:
        return None
    names = [n for n in getattr(mat_def, "shaders", []) if n.endswith("_fu.pcg")]
    names.append("opbb_shader_%s_fu.pcg" % mat_def.name)
    return next((programs[n] for n in names if n in programs), None)


def _build_from_program(session, srd_files, mat, mat_def, mesh_ref, prog, bsdf, uv_names):
    """Stage path: rebuild the colour from the material's compiled shader."""
    slots = dict((slot, inst) for slot, inst in mat_def.maps)
    samplers = mesh_ref.samplers if mesh_ref else {}
    images, sampler_uv = {}, {}
    for unit, (_name, _sem, slot) in prog.samplers.items():
        inst = slots.get(slot)
        if not inst:
            continue
        t, owner = _texture_for_instance(srd_files, inst)
        if t is None:
            session.warn("Texture for %s/%s not found" % (mat.name, inst))
            continue
        images[unit] = load_texture(session, owner, t, owner.name)
        if samplers.get(slot):
            sampler_uv[unit] = samplers[slot]
    up = (0.0, 0.0, 1.0) if session.pref("convert_axes", True) else (0.0, 1.0, 0.0)
    thr = mat_def.params.get("OpBinaryThreshold")
    thr = struct.unpack(">f", thr[:4])[0] if thr and len(thr) >= 4 else 0.5
    try:
        return shader_nodes.build_nodes(mat, prog, images, uv_names or [], sampler_uv, up, bsdf, thr)
    except Exception as e:      # a bad program must not sink the import
        session.warn("Shader graph for %s failed (%s); using texture slots" % (mat.name, e))
        return False


def build_material(session, srd_files, mat_def, mesh_ref, key, programs=None, uv_names=None):
    """Return a bpy material for (mesh_ref, mat_def). Cached by key.

    programs: fragment programs from the stage's *_shader.srd (name -> rsx.Program).
    When the material's program is there, its colour is rebuilt from it;
    otherwise the texture slots are wired directly.
    """
    if key in session.materials:
        return session.materials[key]
    name = mat_def.name if mat_def else (mesh_ref.mat or "JStars")
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nodes = _Nodes(mat)
    nt = nodes.nt
    bsdf = nt.nodes.get("Principled BSDF")
    out = nt.nodes.get("Material Output")
    if bsdf is None:
        nt.nodes.clear()
        bsdf = nodes.new("ShaderNodeBsdfPrincipled", x=0)
        out = nodes.new("ShaderNodeOutputMaterial", x=300)
        nodes.link(bsdf.outputs[0], out.inputs[0])
    bsdf.inputs["Roughness"].default_value = 0.7
    if "Specular IOR Level" in bsdf.inputs:
        bsdf.inputs["Specular IOR Level"].default_value = 0.25

    if mat_def is not None:
        _params_to_props(mat, mat_def.params)
        mat["srd_material"] = mat_def.name

    prog = _fragment_program(programs, mat_def)
    if prog is not None:
        if _build_from_program(session, srd_files, mat, mat_def, mesh_ref, prog, bsdf, uv_names):
            session.materials[key] = mat
            return mat
        # start over with a clean tree for the slot-order fallback
        for n in list(nt.nodes):
            if n not in (bsdf, out):
                nt.nodes.remove(n)
    samplers = mesh_ref.samplers if mesh_ref else {}
    maps = mat_def.maps if mat_def else []
    y = 300
    base_alpha = None
    normal_set = False
    color_layers = []       # (texture node, $TXR, owning SrdFile) in slot order
    for slot, inst in maps:
        t, owner = _texture_for_instance(srd_files, inst)
        if t is None:
            session.warn("Texture for %s/%s not found" % (name, inst))
            continue
        img = load_texture(session, owner, t, owner.name)
        if img is None:
            continue
        uv = samplers.get(slot)
        node = nodes.new("ShaderNodeTexImage", x=-700, y=y, label=slot)
        node.image = img
        uvn = _uv_node(nodes, uv, y)
        if uvn:
            nodes.link(uvn.outputs["UV"], node.inputs["Vector"])
        y -= 300
        s = slot.upper()
        if s.startswith("COLORMAP"):
            color_layers.append((node, t, owner))
        elif s.startswith("NORMALMAP") and not normal_set:
            if t.fmt == tx.FMT_DXT5_N:
                _dxt5nm(nodes, node, bsdf, uv, y + 300)
            else:
                img.colorspace_settings.name = "Non-Color"
                nm = nodes.new("ShaderNodeNormalMap", x=-250, y=y + 300)
                if uv:
                    nm.uv_map = uv
                nodes.link(node.outputs["Color"], nm.inputs["Color"])
                nodes.link(nm.outputs["Normal"], bsdf.inputs["Normal"])
            normal_set = True
        elif s.startswith("SPECULARMAP"):
            img.colorspace_settings.name = "Non-Color"
            if "Specular IOR Level" in bsdf.inputs:
                nodes.link(node.outputs["Color"], bsdf.inputs["Specular IOR Level"])
        elif s.startswith("TRANSPARENCYMAP"):
            img.colorspace_settings.name = "Non-Color"
            base_alpha = node.outputs["Color"]
        # REFLECTMAP / REFLECTIVITY stay in the tree, unlinked,
        # so they are available for hand-built shading.
    if color_layers:
        cm_alpha = _link_color_layers(nodes, bsdf, color_layers)
        if base_alpha is None:
            base_alpha = cm_alpha
    if base_alpha is not None:
        nodes.link(base_alpha, bsdf.inputs["Alpha"])
        try:
            mat.surface_render_method = "DITHERED"
        except AttributeError:
            mat.blend_method = "HASHED"
    session.materials[key] = mat
    return mat


_OPAQUE = 0.99


def _link_color_layers(nodes, bsdf, layers):
    """Wire COLORMAPn layers to Base Color; return the alpha socket to use, or None.

    One layer: its colour, plus its alpha when the format has one (cut-outs).
    Several layers (eyes): the game draws an opaque iris layer with translucent
    overlays on top (black eyelid shading in the alpha, highlights). The slot
    order of iris and overlay differs between characters, so the base is the
    first opaque layer and every translucent layer is alpha-blended over it in
    slot order. Other opaque layers (alternate faces, AO variants) are left
    unlinked.
    """
    def has_alpha_fmt(t):
        return t.fmt in (tx.FMT_DXT5, tx.FMT_DXT3)

    if len(layers) == 1:
        node, t, _ = layers[0]
        nodes.link(node.outputs["Color"], bsdf.inputs["Base Color"])
        return node.outputs["Alpha"] if has_alpha_fmt(t) else None

    cover = [tx.mean_alpha(t.fmt, t.width, t.height, owner.texture_bytes(t))
             for _, t, owner in layers]
    base = next((i for i, c in enumerate(cover) if c >= _OPAQUE), 0)
    node, t, _ = layers[base]
    col = node.outputs["Color"]
    x = -350
    for i, (onode, ot, _) in enumerate(layers):
        if i == base or cover[i] >= _OPAQUE or not has_alpha_fmt(ot):
            continue
        mix = nodes.new("ShaderNodeMix", x=x, y=onode.location.y, label="Overlay %d" % i)
        mix.data_type = "RGBA"
        mix.blend_type = "MIX"
        # ShaderNodeMix has float/vector/colour sockets sharing names: pick RGBA ones.
        a_in = next(s for s in mix.inputs if s.name == "A" and s.type == "RGBA")
        b_in = next(s for s in mix.inputs if s.name == "B" and s.type == "RGBA")
        fac = next(s for s in mix.inputs if s.name == "Factor" and s.type == "VALUE")
        nodes.link(onode.outputs["Alpha"], fac)
        nodes.link(col, a_in)
        nodes.link(onode.outputs["Color"], b_in)
        col = next(s for s in mix.outputs if s.name == "Result" and s.type == "RGBA")
        x += 120
    nodes.link(col, bsdf.inputs["Base Color"])
    return node.outputs["Alpha"] if cover[base] < _OPAQUE and has_alpha_fmt(t) else None


def _dxt5nm(nodes, tex_node, bsdf, uv, y):
    """X from alpha, Y from green, Z = sqrt(1 - x^2 - y^2), then Normal Map node."""
    tex_node.image.colorspace_settings.name = "Non-Color"
    sep = nodes.new("ShaderNodeSeparateColor", x=-450, y=y, label="DXT5nm")
    nodes.link(tex_node.outputs["Color"], sep.inputs["Color"])
    # x, y in [-1, 1]
    def remap(sock, yy):
        m = nodes.new("ShaderNodeMath", x=-300, y=yy)
        m.operation = "MULTIPLY_ADD"
        m.inputs[1].default_value = 2.0
        m.inputs[2].default_value = -1.0
        nodes.link(sock, m.inputs[0])
        return m.outputs[0]
    xs = remap(tex_node.outputs["Alpha"], y)
    ys = remap(sep.outputs["Green"], y - 60)
    dot = nodes.new("ShaderNodeVectorMath", x=-150, y=y - 120)
    dot.operation = "DOT_PRODUCT"
    comb_xy = nodes.new("ShaderNodeCombineXYZ", x=-300, y=y - 160)
    nodes.link(xs, comb_xy.inputs[0])
    nodes.link(ys, comb_xy.inputs[1])
    nodes.link(comb_xy.outputs[0], dot.inputs[0])
    nodes.link(comb_xy.outputs[0], dot.inputs[1])
    sub = nodes.new("ShaderNodeMath", x=0, y=y - 120)
    sub.operation = "SUBTRACT"
    sub.inputs[0].default_value = 1.0
    sub.use_clamp = True
    nodes.link(dot.outputs["Value"], sub.inputs[1])
    sq = nodes.new("ShaderNodeMath", x=120, y=y - 120)
    sq.operation = "SQRT"
    nodes.link(sub.outputs[0], sq.inputs[0])
    # back to [0,1] colour for the Normal Map node
    comb = nodes.new("ShaderNodeCombineColor", x=240, y=y)
    nodes.link(tex_node.outputs["Alpha"], comb.inputs[0])
    nodes.link(sep.outputs["Green"], comb.inputs[1])
    zc = nodes.new("ShaderNodeMath", x=120, y=y - 240)
    zc.operation = "MULTIPLY_ADD"
    zc.inputs[1].default_value = 0.5
    zc.inputs[2].default_value = 0.5
    nodes.link(sq.outputs[0], zc.inputs[0])
    nodes.link(zc.outputs[0], comb.inputs[2])
    nm = nodes.new("ShaderNodeNormalMap", x=400, y=y)
    if uv:
        nm.uv_map = uv
    nodes.link(comb.outputs["Color"], nm.inputs["Color"])
    nodes.link(nm.outputs["Normal"], bsdf.inputs["Normal"])
    # keep the principled node to the right of this little graph
    bsdf.location.x = max(bsdf.location.x, 650)
    out = bsdf.id_data.nodes.get("Material Output")
    if out:
        out.location.x = bsdf.location.x + 350
