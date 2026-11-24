"""Sidebar panel (3D View > Sidebar > J-Stars) and the lists it shows."""
import bpy
from bpy.app.handlers import persistent
from bpy.props import CollectionProperty, EnumProperty, IntProperty, StringProperty

from .catalog import CATALOG
from .prefs import SPECIAL_CATEGORIES, get_prefs


class JSTARS_Item(bpy.types.PropertyGroup):
    key: StringProperty()
    kind: StringProperty()
    cid: IntProperty(default=-1)


def _motion_short(name):
    s = name.rsplit("/", 1)[-1]
    if s.lower().endswith(".san"):
        s = s[:-4]
    parts = s.split("_", 2)
    # "028_gintoki_00_00_stand" -> "00_00_stand"
    return parts[2] if len(parts) == 3 and parts[0].isdigit() else s


def _fill_motions(wm):
    wm.jstars_motions.clear()
    idx = wm.jstars_motion_sets_index
    if not (0 <= idx < len(wm.jstars_motion_sets)):
        return
    key = wm.jstars_motion_sets[idx].key
    mset = next((m for m in CATALOG.motion_sets() if m["key"] == key), None)
    if not mset:
        return
    for n in mset["motions"]:
        it = wm.jstars_motions.add()
        it.name = _motion_short(n)
        it.key = n
    wm.jstars_motions_index = 0


def _on_set_changed(self, context):
    _fill_motions(context.window_manager)


def refresh_lists():
    wm = bpy.context.window_manager
    if wm is None or not hasattr(wm, "jstars_models"):
        return
    wm.jstars_models.clear()
    wm.jstars_stages.clear()
    wm.jstars_motion_sets.clear()
    wm.jstars_motions.clear()
    if not CATALOG.ready:
        return
    for m in sorted(CATALOG.models(), key=lambda m: (m["kind"] != "Battle", m["kind"], m["label"])):
        it = wm.jstars_models.add()
        it.name = m["label"].title()
        it.key = m["key"]
        it.kind = m["kind"]
        it.cid = m["id"]
    for s in CATALOG.stages():
        it = wm.jstars_stages.add()
        it.name = s["label"]
        it.key = s["key"]
        it.cid = s["id"]
    for ms in sorted(CATALOG.motion_sets(), key=lambda m: (m["kind"], m["id"], m["label"])):
        it = wm.jstars_motion_sets.add()
        it.name = "%s (%d)" % (ms["label"], len(ms["motions"]))
        it.key = ms["key"]
        it.kind = ms["kind"]
        it.cid = ms["id"]
    _fill_motions(wm)


class JSTARS_UL_models(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        ic = {"Battle": "OUTLINER_OB_ARMATURE", "J-Adventure": "ARMATURE_DATA",
              "Support": "COMMUNITY", "World Map": "WORLD"}.get(item.kind, "OBJECT_DATA")
        layout.label(text=item.name, icon=ic)

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        kind = context.window_manager.jstars_model_kind
        flt = self.filter_name.lower()
        flags = []
        for it in items:
            ok = (kind == "ALL" or it.kind == kind) and (not flt or flt in it.name.lower())
            flags.append(self.bitflag_filter_item if ok else 0)
        return flags, []


class JSTARS_UL_simple(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=item.name, icon="DOT")


class JSTARS_UL_sets(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=item.name, icon="ACTION")

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        wm = context.window_manager
        flt = self.filter_name.lower()
        cid = -2
        if wm.jstars_match_active:
            ob = context.object
            if ob is not None and ob.type != "ARMATURE" and ob.parent is not None:
                ob = ob.parent
            if ob is not None and "jstars_id" in ob:
                cid = ob["jstars_id"]
        flags = []
        for it in items:
            ok = (not flt or flt in it.name.lower()) and (cid == -2 or it.cid == cid)
            flags.append(self.bitflag_filter_item if ok else 0)
        return flags, []


class JSTARS_PT_main(bpy.types.Panel):
    bl_label = "J-Stars Victory VS+"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "J-Stars"

    def draw(self, context):
        from .operators import SCAN
        lay = self.layout
        prefs = get_prefs(context)
        wm = context.window_manager
        if prefs is None:
            lay.label(text="Add-on preferences unavailable", icon="ERROR")
            return
        col = lay.column(align=True)
        col.prop(prefs, "iso_path", text="")
        row = col.row(align=True)
        if SCAN["running"]:
            row.progress(factor=SCAN["fraction"], text=SCAN["message"] or "Scanning…")
            row.operator("jstars.cancel_scan", text="", icon="X")
        else:
            row.operator("jstars.scan", text="Rescan Disc" if CATALOG.ready else "Scan Disc", icon="FILE_REFRESH")
            row.operator("jstars.reload_cache", text="", icon="FILE_CACHE")
        if CATALOG.ready:
            d = CATALOG.data
            box = lay.box()
            box.scale_y = 0.8
            box.label(text="%d models · %d stages · %d motion sets" % (
                len(d["models"]), len(d["stages"]), len(d["motion_sets"])), icon="CHECKMARK")
            box.label(text="Scanned %s (cached)" % d.get("scanned", "?"), icon="TIME")
            if CATALOG.is_stale():
                box.label(text="Disc changed or missing: rescan", icon="ERROR")
        elif not SCAN["running"]:
            lay.label(text="Pick the disc image, then Scan", icon="INFO")
            return
        else:
            return

        lay.row().prop(wm, "jstars_tab", expand=True)
        tab = wm.jstars_tab
        if tab == "MODELS":
            lay.row().prop(wm, "jstars_model_kind", text="")
            lay.template_list("JSTARS_UL_models", "", wm, "jstars_models", wm, "jstars_models_index", rows=8)
            row = lay.row(align=True)
            row.scale_y = 1.4
            op = row.operator("jstars.import_model", icon="IMPORT")
            op.motions = wm.jstars_model_motions
            lay.row().prop(wm, "jstars_model_motions", expand=True)
        elif tab == "STAGES":
            lay.template_list("JSTARS_UL_simple", "stages", wm, "jstars_stages", wm, "jstars_stages_index", rows=8)
            row = lay.row()
            row.scale_y = 1.4
            row.operator("jstars.import_stage", icon="IMPORT")
        else:
            ob = context.object
            arm = ob if ob and ob.type == "ARMATURE" else (ob.parent if ob and ob.parent and ob.parent.type == "ARMATURE" else None)
            lay.label(text="Target: %s" % (arm.name if arm else "select an imported armature"),
                      icon="ARMATURE_DATA" if arm else "ERROR")
            lay.prop(wm, "jstars_match_active", text="Only sets for the active character")
            lay.template_list("JSTARS_UL_sets", "", wm, "jstars_motion_sets", wm, "jstars_motion_sets_index", rows=4)
            lay.template_list("JSTARS_UL_simple", "motions", wm, "jstars_motions", wm, "jstars_motions_index", rows=8)
            row = lay.row(align=True)
            row.scale_y = 1.3
            row.enabled = arm is not None
            row.operator("jstars.import_motion", text="Import Motion", icon="ACTION").all_in_set = False
            row.operator("jstars.import_motion", text="Whole Set", icon="NLA").all_in_set = True
            row = lay.row(align=True)
            row.prop(prefs, "fps")
            row.prop(prefs, "set_scene_fps", text="Set Scene")


class JSTARS_PT_special(bpy.types.Panel):
    bl_label = "Special Geometry"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "J-Stars"
    bl_parent_id = "JSTARS_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        prefs = get_prefs(context)
        if prefs is None:
            return
        col = self.layout.column(align=True)
        for key, label, _d in SPECIAL_CATEGORIES:
            col.label(text=label)
            col.row(align=True).prop(prefs, key, expand=True)


class JSTARS_PT_options(bpy.types.Panel):
    bl_label = "Import Options"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "J-Stars"
    bl_parent_id = "JSTARS_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        prefs = get_prefs(context)
        if prefs is None:
            return
        col = self.layout.column()
        col.prop(prefs, "texture_mode", text="Textures")
        col.prop(prefs, "convert_axes")
        col.prop(prefs, "scale")
        col.prop(prefs, "stage_instances")
        col.prop(prefs, "stage_shaders")
        col.prop(prefs, "stage_water")
        col.prop(prefs, "bone_length")
        row = self.layout.row(align=True)
        row.operator("jstars.open_cache", icon="FILE_FOLDER")
        row.operator("jstars.clear_cache", icon="TRASH")


@persistent
def _on_load_post(*_args):
    # bpy.data was replaced (File > New, open, revert, factory reset of the scene):
    # the disc index lives on disk and in this module, so just refill the lists.
    if not CATALOG.ready:
        from .operators import load_cached_index
        load_cached_index()
    else:
        refresh_lists()


classes = (
    JSTARS_Item,
    JSTARS_UL_models,
    JSTARS_UL_simple,
    JSTARS_UL_sets,
    JSTARS_PT_main,
    JSTARS_PT_special,
    JSTARS_PT_options,
)


def register_props():
    WM = bpy.types.WindowManager
    WM.jstars_models = CollectionProperty(type=JSTARS_Item)
    WM.jstars_models_index = IntProperty()
    WM.jstars_stages = CollectionProperty(type=JSTARS_Item)
    WM.jstars_stages_index = IntProperty()
    WM.jstars_motion_sets = CollectionProperty(type=JSTARS_Item)
    WM.jstars_motion_sets_index = IntProperty(update=_on_set_changed)
    WM.jstars_motions = CollectionProperty(type=JSTARS_Item)
    WM.jstars_motions_index = IntProperty()
    WM.jstars_tab = EnumProperty(items=[("MODELS", "Characters", "", "OUTLINER_OB_ARMATURE", 0),
                                        ("STAGES", "Stages", "", "WORLD", 1),
                                        ("MOTIONS", "Motions", "", "ACTION", 2)])
    WM.jstars_model_kind = EnumProperty(name="Kind", items=[
        ("ALL", "All Models", ""), ("Battle", "Battle", ""), ("J-Adventure", "J-Adventure", ""),
        ("Support", "Support", ""), ("World Map", "World Map", "")], default="ALL")
    WM.jstars_model_motions = EnumProperty(name="Motions", items=[
        ("NONE", "Model Only", ""), ("IDLE", "+ Idle", ""), ("ALL", "+ All Motions", "")], default="NONE")
    WM.jstars_match_active = bpy.props.BoolProperty(default=True)
    bpy.app.handlers.load_post.append(_on_load_post)


def unregister_props():
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    WM = bpy.types.WindowManager
    for p in ("jstars_models", "jstars_models_index", "jstars_stages", "jstars_stages_index",
              "jstars_motion_sets", "jstars_motion_sets_index", "jstars_motions", "jstars_motions_index",
              "jstars_tab", "jstars_model_kind", "jstars_model_motions", "jstars_match_active"):
        if hasattr(WM, p):
            delattr(WM, p)
