"""Mesh construction with bulk NumPy -> foreach_set transfers."""
import bpy
import numpy as np

from ..formats import srd as S


def _fill_geometry(me, pos, tris):
    nv = len(pos)
    nt = len(tris)
    me.vertices.add(nv)
    me.vertices.foreach_set("co", np.ascontiguousarray(pos, np.float32).ravel())
    me.loops.add(nt * 3)
    me.loops.foreach_set("vertex_index", np.ascontiguousarray(tris, np.int32).ravel())
    me.polygons.add(nt)
    me.polygons.foreach_set("loop_start", np.arange(0, nt * 3, 3, dtype=np.int32))
    me.update(calc_edges=True)


def mesh_from_vertex_block(name, attrs, tris, vb, mesh_ref=None):
    """Create a bpy mesh from decoded attributes. Returns (mesh, used_vertex_mask)."""
    pos = attrs.get((S.SEM_POSITION, 0))
    if pos is None or len(tris) == 0:
        return None
    pos = pos[:, :3]
    me = bpy.data.meshes.new(name)
    _fill_geometry(me, pos, tris)

    loop_idx = tris.ravel()
    # UV sets, named as the game names them (map1, map2, lightmap)
    for (sem, usage), arr in sorted(attrs.items()):
        if sem != S.SEM_TEXCOORD:
            continue
        uvname = vb.set_names.get((sem, usage)) or ("map%d" % (usage + 1))
        layer = me.uv_layers.new(name=uvname)
        if layer is None:
            break
        uv = arr[loop_idx, :2].astype(np.float32)
        uv[:, 1] = 1.0 - uv[:, 1]
        layer.uv.foreach_set("vector", uv.ravel())
    # vertex colours (stage colour sets). Float storage: stage shaders read
    # them as raw blend weights, and byte colours are sRGB-decoded in shaders.
    for (sem, usage), arr in sorted(attrs.items()):
        if sem != S.SEM_COLOR:
            continue
        cname = vb.set_names.get((sem, usage)) or ("Col%d" % usage)
        ca = me.color_attributes.new(cname, "FLOAT_COLOR", "POINT")
        col = np.ones((len(pos), 4), np.float32)
        col[:, :arr.shape[1]] = arr[:, :4]
        ca.data.foreach_set("color", col.ravel())
    # Topology must be valid before custom normals: strips can yield duplicate
    # faces, and Blender's custom-normal code crashes on such meshes.
    me.validate(clean_customdata=False)
    nrm = attrs.get((S.SEM_NORMAL, 0))
    if nrm is not None and len(me.vertices) == len(nrm) and len(me.polygons):
        n = nrm[:, :3].astype(np.float64)
        ln = np.linalg.norm(n, axis=1, keepdims=True)
        n = np.where(ln > 1e-8, n / np.maximum(ln, 1e-8), (0.0, 0.0, 1.0))
        try:
            me.normals_split_custom_set_from_vertices(n.astype(np.float32).tolist())
        except (RuntimeError, ValueError):
            pass
    return me


def add_skin(obj, attrs, vb):
    """Create vertex groups from blend weights/indices (local bone list -> names)."""
    w = attrs.get((S.SEM_WEIGHTS, 0))
    ix = attrs.get((S.SEM_INDICES, 0))
    if w is None or ix is None or not vb.bones:
        return False
    ix = np.rint(ix).astype(np.int64)
    nb = len(vb.bones)
    groups = {}
    for k in range(min(4, w.shape[1])):
        wk = w[:, k]
        bk = ix[:, k]
        live = (wk > 1e-5) & (bk >= 0) & (bk < nb)
        for b in np.unique(bk[live]):
            sel = np.nonzero(live & (bk == b))[0]
            if not len(sel):
                continue
            name = vb.bones[int(b)]
            vg = groups.get(name)
            if vg is None:
                vg = obj.vertex_groups.get(name) or obj.vertex_groups.new(name=name)
                groups[name] = vg
            ws = wk[sel]
            # vg.add takes one weight per call: batch vertices sharing a weight
            order = np.argsort(ws, kind="stable")
            ws_sorted = ws[order]
            cuts = np.nonzero(np.diff(ws_sorted))[0] + 1
            for chunk_idx, chunk_w in zip(np.split(sel[order], cuts), np.split(ws_sorted, cuts)):
                vg.add(chunk_idx.tolist(), float(chunk_w[0]), "ADD")
    return True


def mesh_from_collision(col):
    if len(col.tris) == 0:
        return None
    me = bpy.data.meshes.new(col.name.rsplit("|", 1)[-1] or "collision")
    _fill_geometry(me, col.verts, col.tris)
    me.validate(clean_customdata=False)
    return me
