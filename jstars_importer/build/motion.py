"""Motion (.san) import onto an armature built by model.py.

Every channel in a .san is an offset from the bone's rest local transform,
not a replacement for it: bones the motion leaves alone are keyed with zero
translation, identity rotation and zero scale. The game composes::

    local = T(T0 + dT) @ R0 @ dR @ S(1 + dS)

The armature's edit bones use the game's bind matrices exactly, so the Blender
pose basis (``rest_local^-1 @ animated_local``) is simply::

    location = R0^T dT      rotation = dR      scale = 1 + dS
"""
import bpy
import numpy as np

from ..formats import san


def _hemisphere(q):
    if len(q) < 2:
        return q
    d = (q[1:] * q[:-1]).sum(1)
    sign = np.concatenate(([1.0], np.cumprod(np.where(d < 0, -1.0, 1.0))))
    return q * sign[:, None]


def _write(action, ob, path, group, frames, values):
    n = len(frames)
    if n == 0:
        return
    for i in range(values.shape[1]):
        fc = action.fcurve_ensure_for_datablock(ob, path, index=i, group_name=group)
        fc.keyframe_points.clear()
        fc.keyframe_points.add(n)
        co = np.empty(2 * n, np.float32)
        co[0::2] = frames
        co[1::2] = values[:, i]
        fc.keyframe_points.foreach_set("co", co)
        fc.keyframe_points.foreach_set("interpolation", np.ones(n, np.int32))  # LINEAR
        fc.update()


def _rest_locals(ob):
    out = {}
    for b in ob.data.bones:
        m = b.matrix_local if b.parent is None else b.parent.matrix_local.inverted() @ b.matrix_local
        r = np.array(m.to_3x3().normalized())
        out[b.name] = r
    return out


def apply_motion(context, ob, motion, action_name, fps_game=30, set_scene=True):
    if ob.animation_data is None:
        ob.animation_data_create()
    action = bpy.data.actions.new(action_name)
    action.use_fake_user = True
    action["jstars_motion"] = action_name
    ob.animation_data.action = action
    rest = _rest_locals(ob)
    for pb in ob.pose.bones:          # bones this motion does not key sit at rest
        pb.location = (0.0, 0.0, 0.0)
        pb.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        pb.scale = (1.0, 1.0, 1.0)
    scene_fps = context.scene.render.fps / context.scene.render.fps_base
    k = scene_fps / float(fps_game) if not set_scene else 1.0
    matched = 0
    for tr in motion.tracks:
        if tr.name not in rest or tr.name not in ob.pose.bones:
            continue
        matched += 1
        r0 = rest[tr.name]
        pb = ob.pose.bones[tr.name]
        pb.rotation_mode = "QUATERNION"
        base = 'pose.bones["%s"].' % bpy.utils.escape_identifier(tr.name)
        if len(tr.t_times):
            loc = tr.t_vals @ r0             # == (R0^T dT)^T row-wise
            _write(action, ob, base + "location", tr.name, tr.t_times * k, loc)
        if len(tr.r_times):
            q = tr.r_vals / np.linalg.norm(tr.r_vals, axis=1, keepdims=True)
            _write(action, ob, base + "rotation_quaternion", tr.name, tr.r_times * k, _hemisphere(q))
        if len(tr.s_times):
            _write(action, ob, base + "scale", tr.name, tr.s_times * k, 1.0 + tr.s_vals)
    length = max(1.0, motion.length * k)
    action.use_frame_range = True
    action.frame_start = 0
    action.frame_end = length
    if set_scene:
        sc = context.scene
        sc.render.fps = int(fps_game)
        sc.render.fps_base = 1.0
        sc.frame_start = 0
        sc.frame_end = int(round(length))
    return action, matched


def import_motions(context, ob, blobs, fps_game, set_scene):
    """blobs: list of (name, bytes). Returns list of (action, matched track count)."""
    out = []
    for name, data in blobs:
        for m in san.parse_san(data, name):
            out.append(apply_motion(context, ob, m, name, fps_game, set_scene))
    return out
