"""Character / prop model import: armature, skinned meshes, materials."""
import bpy
import numpy as np
from mathutils import Matrix

from ..formats import srd as S
from .common import (ImportSession, is_move_prop, is_shadow_mesh, np_to_matrix,
                     root_matrix, short_name)
from .materials import build_material
from .meshes import add_skin, mesh_from_vertex_block


def _world_matrices(bones):
    W = [None] * len(bones)
    for b in bones:
        W[b.index] = (W[b.parent] @ b.local) if b.parent >= 0 else b.local.copy()
    return W


def build_armature(session, name, bones, coll):
    arm = bpy.data.armatures.new(name)
    arm.display_type = "STICK"
    ob = bpy.data.objects.new(name, arm)
    coll.objects.link(ob)
    ob.show_in_front = True
    ctx = session.context
    prev_active = ctx.view_layer.objects.active
    ctx.view_layer.objects.active = ob
    prev_mode = ctx.object.mode if ctx.object else "OBJECT"
    if prev_mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.mode_set(mode="EDIT")
    W = _world_matrices(bones)
    heads = np.array([w[:3, 3] for w in W])
    children = {}
    for b in bones:
        children.setdefault(b.parent, []).append(b.index)
    leaf_len = session.pref("bone_length", 0.05)
    ebs = []
    for b in bones:
        eb = arm.edit_bones.new(b.name or ("bone_%d" % b.index))
        m = W[b.index].copy()
        # strip any scale so edit-bone axes are orthonormal
        for c in range(3):
            n = np.linalg.norm(m[:3, c])
            if n > 1e-8:
                m[:3, c] /= n
        kids = children.get(b.index, [])
        length = leaf_len
        if kids:
            d = np.linalg.norm(heads[kids] - heads[b.index], axis=1)
            d = d[d > 1e-4]
            if len(d):
                length = float(min(max(d.min(), leaf_len * 0.25), 1.0))
        eb.head = (0.0, 0.0, 0.0)
        eb.tail = (0.0, length, 0.0)
        eb.matrix = np_to_matrix(m)
        if b.parent >= 0:
            eb.parent = ebs[b.parent]
        ebs.append(eb)
    names = [eb.name for eb in ebs]
    bpy.ops.object.mode_set(mode="OBJECT")
    ctx.view_layer.objects.active = prev_active if prev_active else ob
    for pb in ob.pose.bones:
        pb.rotation_mode = "QUATERNION"
    return ob, names


def load_srd(catalog, pak, srd_name, label):
    srd, srdi, srdv = catalog.srd_triplet(pak, srd_name)
    if srd is None:
        raise ValueError("%s not found in %s" % (srd_name, pak))
    files = [S.SrdFile(srd, srdi, srdv, name=pak + "|" + srd_name)]
    # Texture packs: the model's own "<base>_texture.srd" first, then any other
    # in the same pak. Alternate forms (004_naruto_1/2/3) have none of their
    # own and use the base form's 004_naruto_01p_PS3_texture.srd.
    base = srd_name[:-4]
    own = base + "_texture.srd"
    others = sorted(n for n in catalog.pak_entries(pak)
                    if n.lower().endswith("_texture.srd") and "lightmap" not in n.lower()
                    and "/" not in n and n != own)
    for extra in [own] + others:
        t = catalog.entry(pak, extra)
        if t:
            ts, ti, tv = catalog.srd_triplet(pak, extra)
            files.append(S.SrdFile(ts, ti, tv, name=pak + "|" + extra))
    return files


def import_model(context, catalog, entry):
    label = entry["label"]
    session = ImportSession(context, label)
    files = load_srd(catalog, entry["pak"], entry["srd"], label)
    f = files[0]
    coll = session.make_root_collection(label.title())
    coll["jstars_model_key"] = entry["key"]

    if f.skeleton:
        root, bone_names = build_armature(session, label.title(), f.skeleton, coll)
    else:
        root = bpy.data.objects.new(label.title(), None)
        root.empty_display_type = "ARROWS"
        coll.objects.link(root)
        bone_names = []
    root.matrix_world = root_matrix(session)
    root["jstars_id"] = entry.get("id", -1)
    root["jstars_model_key"] = entry["key"]
    root["jstars_kind"] = entry.get("kind", "")

    objs = build_meshes(session, files, f, coll, root, skinned=bool(f.skeleton))
    return root, objs, session


def build_meshes(session, files, f, coll, parent, skinned, node_mats=None, mesh_filter=None):
    """Create one object per $MSH. node_mats: {mesh name: 4x4 numpy local matrix}."""
    objs = []
    data_cache = {}
    for mref in f.meshes:
        if mesh_filter is not None and not mesh_filter(mref):
            continue
        target = coll
        if is_move_prop(mref.name):
            target = session.special_collection("geo_props", "Move-Specific Props")
        elif is_shadow_mesh(mref.name, mref.mat):
            target = session.special_collection("geo_shadow", "Shadow Meshes")
        if target is None:
            continue
        vb = f.vertex_blocks.get(mref.vtx)
        if vb is None:
            session.warn("Vertex block %s missing" % mref.vtx)
            continue
        me = data_cache.get((mref.vtx, mref.mat))
        attrs = None
        if me is None:
            res = f.mesh_arrays(vb)
            if res is None:
                session.warn("No vertex data for %s" % mref.vtx)
                continue
            attrs, tris = res
            me = mesh_from_vertex_block(short_name(mref.name), attrs, tris, vb, mref)
            if me is None:
                continue
            mat = build_material(session, files, f.materials.get(mref.mat), mref, (f.name, mref.mat, tuple(sorted(mref.samplers.items()))))
            me.materials.append(mat)
            data_cache[(mref.vtx, mref.mat)] = me
        ob = bpy.data.objects.new(short_name(mref.name), me)
        target.objects.link(ob)
        ob.parent = parent
        ob["jstars_path"] = mref.name
        if node_mats and mref.name in node_mats:
            ob.matrix_basis = np_to_matrix(node_mats[mref.name])
        if skinned and vb.bones:
            if attrs is None:
                attrs = f.mesh_arrays(vb)[0]
            if add_skin(ob, attrs, vb):
                mod = ob.modifiers.new("Armature", "ARMATURE")
                mod.object = parent
        objs.append(ob)
    return objs
