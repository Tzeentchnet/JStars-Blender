"""Shared helpers for building Blender data."""
import math
import re

import bpy
from mathutils import Matrix

from ..prefs import get_prefs


class ImportSession:
    """State for one import: preferences snapshot, caches and messages."""

    def __init__(self, context, label):
        self.context = context
        self.prefs = get_prefs(context)
        self.label = label
        self.images = {}        # texture name -> bpy.types.Image
        self.materials = {}     # (srd id, material name) -> bpy.types.Material
        self.warnings = []
        self.root_collection = None
        self._special = {}

    def warn(self, msg):
        if msg not in self.warnings:
            self.warnings.append(msg)

    def pref(self, key, default=None):
        return getattr(self.prefs, key, default) if self.prefs else default

    # -------------------------------------------------------------- collections
    def make_root_collection(self, name):
        coll = bpy.data.collections.new(name)
        self.context.scene.collection.children.link(coll)
        self.root_collection = coll
        return coll

    def sub_collection(self, name, parent=None):
        parent = parent or self.root_collection
        for c in parent.children:
            if c.name.split(".")[0] == name or c.get("jstars_sub") == name:
                return c
        c = bpy.data.collections.new(name)
        c["jstars_sub"] = name
        parent.children.link(c)
        return c

    def special_mode(self, key):
        return self.pref(key, "OFF") or "OFF"

    def special_collection(self, key, label):
        """Collection for a special-geometry category, or None when it is OFF."""
        mode = self.special_mode(key)
        if mode == "OFF":
            return None
        if key in self._special:
            return self._special[key]
        coll = self.sub_collection(label)
        if mode == "HIDDEN":
            coll.hide_render = True
            lc = find_layer_collection(self.context.view_layer.layer_collection, coll)
            if lc is not None:
                lc.hide_viewport = True
        self._special[key] = coll
        return coll


def find_layer_collection(lc, coll):
    if lc.collection == coll:
        return lc
    for ch in lc.children:
        r = find_layer_collection(ch, coll)
        if r is not None:
            return r
    return None


def root_matrix(session):
    """Game space (Y-up, metres) -> Blender space."""
    s = session.pref("scale", 1.0) or 1.0
    m = Matrix.Scale(s, 4)
    if session.pref("convert_axes", True):
        m = m @ Matrix.Rotation(math.radians(90.0), 4, "X")
    return m


def np_to_matrix(a):
    return Matrix([list(map(float, row)) for row in a])


def short_name(path):
    """'|model|group4|body16:OBI' -> 'body16:OBI'"""
    return path.rsplit("|", 1)[-1] if "|" in path else path


# ------------------------------------------------------------------ classifiers
# sdoN: stage shadow-object meshes; their shaders output constant black.
_SHADOW_SEG = re.compile(r"^(shadow\w*|shadows?\d*|shd\d*|sdo\d*|kage)$")
_SHA_SUFFIX = re.compile(r"(^|_)sha(\d+)?$")
# sdw / sdw_l / sdwm / _hap_sdw3 / pill_02_sdw: shadow casters. Alabasta's palm
# sdwl/sdwm groups are opaque leaf cards (alpha threshold 0) plus a second trunk.
_SDW = re.compile(r"(^|_)sdw")
_SKY_SEG = re.compile(r"^(sky(?!_and)|sora)|_sky_?$")


def is_sky_mesh(path):
    """Sky domes / backdrops: a node or group named sky*, *_sky or sora (Japanese 'sky')."""
    segs = [s.split(":", 1)[0] for s in path.lower().split("|") if s]
    return any(_SKY_SEG.search(s) for s in segs)


def is_shadow_mesh(path, material):
    segs = [s for s in re.split(r"[|:]", path.lower()) if s]
    for s in segs:
        if _SHADOW_SEG.match(s) or _SHA_SUFFIX.search(s) or _SDW.search(s):
            return True
        if s.startswith("shadow") or s.endswith("_shadow"):
            return True
    mat = (material or "").lower()
    return "shadow" in mat


def is_move_prop(path):
    return "|bind_equipment" in path.lower() or "|bindequipment" in path.lower()


def is_camera_collision(name):
    return "camera" in name.lower()
