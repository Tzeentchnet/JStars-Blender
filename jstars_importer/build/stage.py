"""Stage (battle map) import.

A stage pack holds several .srd parts:
  mapNN.srd                 main geometry (meshes in world space) + $COL collision
  mapNN_locator_obj.srd     prop library; one $TRE per prop model
  mapNN_knock_*.srd,
  mapNN_scatter_*.srd       break-apart debris templates (shown only when scenery breaks)
  mapNN_sea00.srd           wave normal map of the generated water plane
  mapNN_shader.srd          compiled RSX programs, one per material
  mapNN_texture.srd(+v)     shared textures
and the param pack's mapNN_param_locator.srd places locator props: one
$TRE per placement group, one node with a 4x4 matrix per placement, and
per-node rows in its Op* parameters (see _locator_instances and
docs/FORMATS.md "Stages").
"""
import re
import struct

import bpy
import numpy as np

from ..formats import rsx
from ..formats import srd as S
from .common import (ImportSession, is_camera_collision, is_shadow_mesh, is_sky_mesh, np_to_matrix,
                     root_matrix, short_name, find_layer_collection)
from . import shader_nodes
from .materials import build_material
from .meshes import mesh_from_collision, mesh_from_vertex_block


def _node_worlds(nodes):
    W = {}
    for n in nodes:
        local = n.matrix if n.matrix is not None else np.eye(4)
        W[id(n)] = (W[id(n.parent)] @ local) if n.parent is not None else local
    return W


class _MeshFactory:
    """Creates bpy meshes once per (file, vertex block, material)."""

    def __init__(self, session, tex_files, programs=None):
        self.session = session
        self.tex_files = tex_files
        self.programs = programs or {}
        self.cache = {}

    def is_shadow(self, f, mref):
        """Shadow geometry by name, or by a shader that outputs lighting alone
        (no texture, no vertex colour), as the stages' sdw shadow meshes do."""
        if is_shadow_mesh(mref.name, mref.mat):
            return True
        md = f.materials.get(mref.mat)
        if md is None or not self.programs:
            return False
        names = [n for n in md.shaders if n.endswith("_fu.pcg")] + ["opbb_shader_%s_fu.pcg" % md.name]
        prog = next((self.programs[n] for n in names if n in self.programs), None)
        return prog is not None and shader_nodes.light_only(prog)

    @staticmethod
    def is_reflection(mref):
        """Terrain copies drawn only into the sea reflection (Namek ref_* groups).
        They duplicate the visible geometry in place, unmirrored."""
        return (mref.mat or "").upper().startswith("UMIREF")

    def category(self, f, mref):
        if self.is_reflection(mref):
            return "reflection"
        if self.is_shadow(f, mref):
            return "shadow"
        return "main"

    def get(self, f, mref):
        key = (f.name, mref.vtx, mref.mat)
        if key in self.cache:
            return self.cache[key]
        vb = f.vertex_blocks.get(mref.vtx)
        me = None
        if vb is not None:
            res = f.mesh_arrays(vb)
            if res is not None:
                attrs, tris = res
                me = mesh_from_vertex_block(short_name(mref.name), attrs, tris, vb, mref)
                if me is not None:
                    files = [f] + self.tex_files
                    uv_names = _uv_names(vb)
                    mat = build_material(self.session, files, f.materials.get(mref.mat), mref,
                                         (f.name, mref.mat, tuple(sorted(mref.samplers.items())),
                                          tuple(uv_names)),
                                         programs=self.programs, uv_names=uv_names)
                    me.materials.append(mat)
        self.cache[key] = me
        return me


def _uv_names(vb):
    """UV map names by texcoord usage (pixel-shader TEXn reads usage n),
    named as mesh_from_vertex_block names the layers."""
    usages = sorted(u for (sem, u) in ((a[2], a[3]) for a in vb.attrs) if sem == S.SEM_TEXCOORD)
    names = []
    for u in usages:
        while len(names) < u:
            names.append("")
        names.append(vb.set_names.get((S.SEM_TEXCOORD, u)) or "map%d" % (u + 1))
    return names


def _load_programs(catalog, pak):
    """Fragment programs from every *_shader.srd in the pack, by name."""
    programs = {}
    for name, data in catalog.pak_entries(pak).items():
        if name.lower().endswith("_shader.srd"):
            try:
                programs.update(rsx.load_programs(data))
            except (struct.error, IndexError, ValueError) as e:
                print("[J-Stars] could not read %s: %s" % (name, e))
    return programs


def _place_file(session, factory, f, coll_for, parent, tree_filter=None):
    """Instantiate every mesh of f. coll_for(mref, f) -> collection or None (skip).
    Meshes referenced by $TRE nodes get the node's world matrix; others identity."""
    objs = []
    placed = set()
    for nodes, _params, _name in f.trees:
        if tree_filter is not None and not tree_filter(nodes):
            continue
        W = _node_worlds(nodes)
        for n in nodes:
            for ref in n.refs:
                placed.add(ref)
    mrefs = {m.name: m for m in f.meshes}
    for nodes, _params, _name in f.trees:
        if tree_filter is not None and not tree_filter(nodes):
            continue
        W = _node_worlds(nodes)
        for n in nodes:
            for ref in n.refs:
                m = mrefs.get(ref)
                if m is None:
                    continue
                ob = _make_obj(session, factory, f, m, coll_for, parent, W[id(n)])
                if ob is not None:
                    objs.append(ob)
    if tree_filter is None:
        for m in f.meshes:
            if m.name in placed:
                continue
            ob = _make_obj(session, factory, f, m, coll_for, parent, None)
            if ob is not None:
                objs.append(ob)
    return objs


def _make_obj(session, factory, f, mref, coll_for, parent, mat):
    coll = coll_for(mref, f)
    if coll is None:
        return None
    me = factory.get(f, mref)
    if me is None:
        return None
    ob = bpy.data.objects.new(short_name(mref.name), me)
    coll.objects.link(ob)
    if parent is not None:
        ob.parent = parent
    if mat is not None:
        ob.matrix_basis = np_to_matrix(mat)
    ob["jstars_path"] = mref.name
    return ob


def _collisions(session, f, parent, base_coll_hint=""):
    objs = []
    for b in f.collisions:
        try:
            col = S.parse_col(b)
        except (struct.error, IndexError, ValueError):
            continue
        if is_camera_collision(col.name):
            coll = session.special_collection("geo_camera_collision", "Camera Collision")
        else:
            coll = session.special_collection("geo_collision", "Collision")
        if coll is None:
            continue
        me = mesh_from_collision(col)
        if me is None:
            continue
        ob = bpy.data.objects.new(short_name(col.name) or "collision", me)
        ob.display_type = "WIRE"
        ob["jstars_path"] = col.name
        coll.objects.link(ob)
        ob.parent = parent
        objs.append(ob)
    return objs


def _load(catalog, pak, name):
    srd, srdi, srdv = catalog.srd_triplet(pak, name)
    if not srd:
        return None
    return S.SrdFile(srd, srdi, srdv, name=pak + "|" + name)


def _node_rows(params, key, count):
    """Split a per-node RSI parameter into one tuple of u32 per $TRE node."""
    v = params.get(key)
    if not v or count == 0 or len(v) % count:
        return [()] * count
    stride = len(v) // count
    n = stride // 4
    return [struct.unpack_from(">%dI" % n, v, i * stride) for i in range(count)]


def _locator_instances(catalog, entry):
    """Return [(model ids, 4x4 world matrix, name)] from mapNN_param_locator.srd.

    Each $TRE is a placement group (e.g. ``|wood3`` with 24 trees); every
    ``Op*`` parameter holds one row per node. OpLocatorModelIndex rows are
    three u32 model IDs, the damage states intact -> damaged -> destroyed;
    IDs match the library's ``<root>:OpLocatorObjectIndex``. 0 is a valid ID
    (Alabasta's ``area_id0`` floor tile): a column is in use when its
    OpLocatorFileNameHash entry is non-zero. Unused columns come back as None,
    and group roots (all columns unused) are skipped.
    """
    param = entry.get("param")
    if not param:
        return []
    ents = catalog.pak_entries(param)
    name = next((k for k in ents if k.endswith("_param_locator.srd")), None)
    if not name:
        return []
    out = []
    for b in S.parse_blocks(ents[name]):
        if b.tag != "$TRE":
            continue
        nodes, params, _ = S.parse_tre(b)
        if not nodes:
            continue
        W = _node_worlds(nodes)
        ids = _node_rows(params, "OpLocatorModelIndex", len(nodes))
        hashes = _node_rows(params, "OpLocatorFileNameHash", len(nodes))
        hidden = _node_rows(params, "OpEventNonDisplay", len(nodes))
        for n, row, hs, nd in zip(nodes, ids, hashes, hidden):
            if len(hs) == len(row):
                row = tuple(i if h else None for i, h in zip(row, hs))
            else:   # no hash table: fall back to 0 = unused
                row = tuple(i or None for i in row)
            if not row or row[0] is None:
                continue
            if nd and nd[0]:
                continue
            out.append((row, W[id(n)], n.name))
    return out


def _library_ids(f):
    """Locator library: model ID -> $TRE index, from ``<root>:OpLocatorObjectIndex``."""
    by_id = {}
    for i, (_nodes, params, _name) in enumerate(f.trees):
        for k, v in params.items():
            if k.endswith(":OpLocatorObjectIndex") and len(v) >= 4:
                by_id.setdefault(struct.unpack_from(">I", v, 0)[0], i)
    return by_id


def import_stage(context, catalog, entry):
    label = entry["label"]
    session = ImportSession(context, label)
    coll = session.make_root_collection("Stage " + label)
    coll["jstars_stage_key"] = entry["key"]
    root = bpy.data.objects.new("Stage " + label, None)
    root.empty_display_type = "PLAIN_AXES"
    coll.objects.link(root)
    root.matrix_world = root_matrix(session)
    root["jstars_stage_key"] = entry["key"]

    pak = entry["pak"]
    parts = list(entry.get("parts", []))
    tex_files = []
    stem = re.sub(r"_m\.pak$", "", pak.rsplit("/", 1)[-1])
    # Main texture set first, then every other texture file in the pack
    # (lightmap and debris sets) as fallbacks for cross-file references.
    texture_names = [stem + "_texture.srd", stem.replace("_2p", "") + "_texture.srd"]
    texture_names += sorted(n for n in catalog.pak_entries(pak)
                            if n.lower().endswith("_texture.srd") and n not in texture_names)
    for extra in texture_names:
        if not catalog.entry(pak, extra):
            continue
        tf = _load(catalog, pak, extra)
        if tf is not None and tf.textures:
            tex_files.append(tf)
    programs = _load_programs(catalog, pak) if session.pref("stage_shaders", True) else {}
    factory = _MeshFactory(session, tex_files, programs)

    # Stage collection layout: the root empty, then one child collection per kind
    # (Terrain, Sky, Props, Water, and the optional special categories).
    terrain = session.sub_collection("Terrain")
    sky = []

    def sky_coll():
        if not sky:
            sky.append(session.sub_collection("Sky"))
        return sky[0]

    def routed(base, split_sky=False):
        def coll_for(mref, f):
            cat = factory.category(f, mref)
            if cat == "main":
                if split_sky and is_sky_mesh(mref.name):
                    return sky_coll()
                return base
            key, label = _SPECIAL[cat]
            return session.special_collection(key, label)
        return coll_for

    objs = []
    locator_file = None
    sea_files = []
    main_objs = []
    for p in parts:
        low = p.lower()
        if low.endswith("_locator_obj.srd"):
            locator_file = p
            continue
        f = _load(catalog, pak, p)
        if f is None:
            continue
        if re.search(r"_sea\d*\.srd$", low):
            sea_files.append(f)
            continue
        if "_knock_" in low or "_scatter_" in low:
            dcoll = session.special_collection("geo_breakable", "Breakable Debris")
            if dcoll is None:
                continue
            sub = session.sub_collection(p[:-4], dcoll)
            objs += _place_file(session, factory, f, routed(sub), root)
        else:
            placed = _place_file(session, factory, f, routed(terrain, split_sky=True), root)
            objs += placed
            main_objs += [o for o in placed if terrain in o.users_collection]
        objs += _collisions(session, f, root)

    if locator_file:
        objs += _import_locators(session, catalog, entry, factory, locator_file, root, coll)

    if sea_files and session.pref("stage_water", True):
        water = _add_water(session, catalog, entry, sea_files[0], main_objs, root,
                           session.sub_collection("Water"))
        if water is not None:
            objs.append(water)

    _order_children(session.context.view_layer, coll, ["Terrain", "Sky", "Props", "Water", "Breakable Debris"])
    session.context.view_layer.update()
    return root, objs, session


def _order_children(view_layer, coll, first):
    """Relink child collections so the named ones come first, in that order.
    Relinking rebuilds their view-layer entries, so exclude / hide flags are
    carried over."""
    kids = list(coll.children)
    rank = {n: i for i, n in enumerate(first)}
    want = sorted(kids, key=lambda c: (rank.get(c.get("jstars_sub", c.name), len(first)), kids.index(c)))
    if want == kids:
        return
    flags = {}

    def save(lc):
        flags[lc.collection.name] = (lc.exclude, lc.hide_viewport)
        for ch in lc.children:
            save(ch)

    top = find_layer_collection(view_layer.layer_collection, coll)
    if top is not None:
        for ch in top.children:
            save(ch)
    for c in kids:
        coll.children.unlink(c)
    for c in want:
        coll.children.link(c)

    def restore(lc):
        f = flags.get(lc.collection.name)
        if f is not None:
            lc.exclude, lc.hide_viewport = f
        for ch in lc.children:
            restore(ch)

    top = find_layer_collection(view_layer.layer_collection, coll)
    if top is not None:
        for ch in top.children:
            restore(ch)


_SPECIAL = {
    "shadow": ("geo_shadow", "Shadow Meshes"),
    "reflection": ("geo_reflection", "Reflection Meshes"),
    "collision": ("geo_collision", "Collision"),
    "camera": ("geo_camera_collision", "Camera Collision"),
}


def _import_locators(session, catalog, entry, factory, locator_file, root, coll):
    f = _load(catalog, entry["pak"], locator_file)
    if f is None:
        return []
    instances = _locator_instances(catalog, entry)
    if not instances:
        return []
    mrefs = {m.name: m for m in f.meshes}
    use_instances = session.pref("stage_instances", False)
    props_coll = session.sub_collection("Props")
    protos_parent = None
    if use_instances:
        protos_parent = session.sub_collection("_Prop Library")
        lc = find_layer_collection(session.context.view_layer.layer_collection, protos_parent)
        if lc is not None:
            lc.exclude = True
    # collisions that belong to locator models, keyed by model root name
    col_by_root = {}
    for b in f.collisions:
        try:
            c = S.parse_col(b)
        except (struct.error, IndexError, ValueError):
            continue
        rootname = c.name.split("|")[1] if c.name.startswith("|") and c.name.count("|") > 1 else ""
        col_by_root.setdefault(rootname, []).append(c)

    protos = {}     # (model, category) -> collection

    def proto(model, category):
        key = (model, category)
        if key in protos:
            return protos[key]
        if model >= len(f.trees):
            protos[key] = None
            return None
        nodes = f.trees[model][0]
        rootname = nodes[0].name.strip("|") if nodes else str(model)
        pc = bpy.data.collections.new("%s%s" % (rootname, "" if category == "main" else " [%s]" % category))
        protos_parent.children.link(pc) if protos_parent else None
        W = _node_worlds(nodes)
        made = 0
        for n in nodes:
            for ref in n.refs:
                m = mrefs.get(ref)
                if m is None:
                    continue
                if factory.category(f, m) != category:
                    continue
                me = factory.get(f, m)
                if me is None:
                    continue
                ob = bpy.data.objects.new(short_name(m.name), me)
                ob.matrix_basis = np_to_matrix(W[id(n)])
                ob["jstars_path"] = m.name
                pc.objects.link(ob)
                made += 1
        if category in ("collision", "camera"):
            for c in col_by_root.get(rootname, []):
                if is_camera_collision(c.name) != (category == "camera"):
                    continue
                me = mesh_from_collision(c)
                if me is None:
                    continue
                ob = bpy.data.objects.new(short_name(c.name), me)
                ob.display_type = "WIRE"
                pc.objects.link(ob)
                made += 1
        if not made:
            if pc.users:
                if protos_parent:
                    protos_parent.children.unlink(pc)
            bpy.data.collections.remove(pc)
            pc = None
        protos[key] = pc
        return pc

    categories = [("main", props_coll)]
    for cat in ("shadow", "reflection", "collision", "camera"):
        c = session.special_collection(*_SPECIAL[cat])
        if c is not None:
            categories.append((cat, c))

    debris = session.special_collection("geo_breakable", "Breakable Debris")
    alt_coll = session.sub_collection("Broken States", debris) if debris is not None else None

    # Placements name models by ID; libraries without ID tags fall back to tree order.
    by_id = _library_ids(f)
    resolve = by_id.get if by_id else (lambda i: i if i < len(f.trees) else None)
    unresolved = set()

    objs = []
    jobs = []
    for pi, (ids, mat, name) in enumerate(instances):
        model = resolve(ids[0])
        if model is None:
            unresolved.add(ids[0])
            continue
        for cat, target in categories:
            jobs.append((model, cat, target, mat, name, (pi, 0)))
        if alt_coll is None:
            continue
        # Later columns are the damaged / destroyed states of the same spot.
        seen = {ids[0]}
        for k, alt in enumerate(ids[1:], 1):
            if alt is None or alt in seen:
                continue
            seen.add(alt)
            m = resolve(alt)
            if m is None:
                unresolved.add(alt)
                continue
            jobs.append((m, "main", alt_coll, mat, "%s (broken %d)" % (name, k), (pi, k)))
    if unresolved:
        # e.g. Namek's grass IDs 5, 6, 20...; split-screen packs drop many props
        print("[J-Stars] %s: %d locator model IDs not in %s: %s" % (
            entry["label"], len(unresolved), locator_file,
            ", ".join(str(i) for i in sorted(unresolved)[:12]) + (" ..." if len(unresolved) > 12 else "")))
    holders = {}    # placement key -> empty that carries the placement transform

    def holder(key, name, mat, coll):
        h = holders.get(key)
        if h is None:
            h = bpy.data.objects.new(_prop_name(name), None)
            h.empty_display_type = "PLAIN_AXES"
            h.empty_display_size = 0.5
            h.parent = root
            h.matrix_basis = np_to_matrix(mat)
            h["jstars_locator"] = name
            coll.objects.link(h)
            holders[key] = h
            objs.append(h)
        return h

    for model, cat, target, mat, name, key in jobs:
        pc = proto(model, cat)
        if pc is None:
            continue
        if use_instances:
            e = bpy.data.objects.new(_prop_name(name), None)
            e.instance_type = "COLLECTION"
            e.instance_collection = pc
            e.empty_display_size = 0.5
            e.parent = root
            e.matrix_basis = np_to_matrix(mat)
            target.objects.link(e)
            objs.append(e)
        else:
            # Real objects sharing the prototype's mesh data (linked duplicates):
            # selectable in the viewport, materials listed in the outliner.
            # One empty per placement holds them, so a prop moves as one piece.
            h = holder(key, name, mat, target)
            for src in pc.objects:
                ob = bpy.data.objects.new(src.name, src.data)
                ob.display_type = src.display_type
                ob.parent = h
                ob.matrix_basis = src.matrix_basis
                if src.get("jstars_path"):
                    ob["jstars_path"] = src["jstars_path"]
                target.objects.link(ob)
                objs.append(ob)
    if not use_instances:
        for pc in list(protos.values()):
            if pc is not None:
                for o in list(pc.objects):
                    bpy.data.objects.remove(o)
                bpy.data.collections.remove(pc)
    return objs


def _prop_name(locator_name):
    """'_ya_locator|_ya_574_5940' -> 'ya_574_5940' (keeps any ' (broken n)' suffix)."""
    return short_name(locator_name.strip("|")).lstrip("_") or "prop"


# ---------------------------------------------------------------- water
# mapNN_param.dat is a float table. Stages with a sea (a mapNN_sea00.srd part:
# Namek, Penguin Village) differ from the rest at these entries; the meaning is
# inferred: Namek's basin floor sits just below the height, and the colours are
# Namek green / Penguin Village blue. Values are in the game's gamma space.
_WATER_HEIGHT = 32
_WATER_COLOR = 50          # 3 floats
_WAVE_TILE = 20.0          # game units per repeat of the wave normal map (a guess)


def _water_params(catalog, entry):
    height, color = 0.0, (0.58, 0.67, 1.0)
    param = entry.get("param")
    ents = catalog.pak_entries(param) if param else {}
    name = next((k for k in ents if k.endswith("_param.dat")), None)
    if name:
        vals = np.frombuffer(ents[name], ">f4", len(ents[name]) // 4)
        if len(vals) > _WATER_COLOR + 2:
            h = float(vals[_WATER_HEIGHT])
            c = tuple(float(x) for x in vals[_WATER_COLOR:_WATER_COLOR + 3])
            if np.isfinite(h):
                height = h
            if all(np.isfinite(c)) and all(0.0 <= x <= 1.0 for x in c):
                color = c
    return height, color


def _ground_bounds(objs):
    """XZ bounds (game space) of the stage's main meshes, sky domes left out."""
    lo, hi = None, None
    extents = {}
    for ob in objs:
        if ob.type != "MESH" or "sky" in ob.get("jstars_path", "").lower():
            continue
        me = ob.data
        if me.name not in extents:
            co = np.empty(len(me.vertices) * 3, np.float32)
            me.vertices.foreach_get("co", co)
            co = co.reshape(-1, 3)
            co = co[np.isfinite(co).all(1)]
            extents[me.name] = (co.min(0), co.max(0)) if len(co) else None
        ext = extents[me.name]
        if ext is None:
            continue
        a, b = ext
        corners = np.array([[x, y, z, 1.0] for x in (a[0], b[0]) for y in (a[1], b[1]) for z in (a[2], b[2])])
        M = np.array(ob.matrix_basis)
        w = (corners @ M.T)[:, :3]
        lo = w.min(0) if lo is None else np.minimum(lo, w.min(0))
        hi = w.max(0) if hi is None else np.maximum(hi, w.max(0))
    if lo is None:
        return (-150.0, -150.0), (150.0, 150.0)
    return (float(lo[0]), float(lo[2])), (float(hi[0]), float(hi[2]))


def _water_material(session, sea_file, color):
    from .images import load_texture
    mat = bpy.data.materials.new("Water")
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
    lin = tuple(c ** 2.2 for c in color)         # game colours are gamma-space
    bsdf.inputs["Base Color"].default_value = lin + (1.0,)
    bsdf.inputs["Roughness"].default_value = 0.05
    bsdf.inputs["IOR"].default_value = 1.333
    tex = next(iter(sea_file.textures.values()), None)
    img = load_texture(session, sea_file, tex, sea_file.name) if tex is not None else None
    if img is not None:
        img.colorspace_settings.name = "Non-Color"
        node = nt.nodes.new("ShaderNodeTexImage")
        node.image = img
        node.location = (-700, -250)
        nmap = nt.nodes.new("ShaderNodeNormalMap")
        nmap.inputs["Strength"].default_value = 0.4
        nmap.location = (-350, -250)
        nt.links.new(node.outputs["Color"], nmap.inputs["Color"])
        nt.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    mat["srd_water_color"] = list(color)
    return mat


def _add_water(session, catalog, entry, sea_file, main_objs, root, coll):
    """The sea is not a mesh on the disc: the game draws a plane at the
    stage's water height with the sea00 wave normal map (and renders the
    UMIREF meshes into its reflection). Rebuild that plane over the stage."""
    height, color = _water_params(catalog, entry)
    (x0, z0), (x1, z1) = _ground_bounds(main_objs)
    me = bpy.data.meshes.new("Water")
    # wound so the face normal is +Y (game up)
    verts = [(x0, height, z0), (x0, height, z1), (x1, height, z1), (x1, height, z0)]
    me.from_pydata(verts, [], [(0, 1, 2, 3)])
    uv = me.uv_layers.new(name="UVMap")
    for loop in me.loops:
        x, _y, z = verts[loop.vertex_index]
        uv.data[loop.index].uv = (x / _WAVE_TILE, -z / _WAVE_TILE)
    me.materials.append(_water_material(session, sea_file, color))
    ob = bpy.data.objects.new("Water", me)
    ob.parent = root
    ob["jstars_water_height"] = height
    coll.objects.link(ob)
    return ob
