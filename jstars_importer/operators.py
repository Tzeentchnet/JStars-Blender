"""Operators: scan, import model / stage / motions, cache maintenance."""
import os
import threading
import time
import traceback

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

from . import catalog as catalog_mod
from .catalog import CATALOG, Catalog
from .prefs import cache_dir, get_prefs, index_file, load_settings, save_settings

# ---------------------------------------------------------------------- scan state
SCAN = {"running": False, "fraction": 0.0, "message": "", "error": None, "result": None, "cancel": False}


def _redraw_ui(context=None):
    wm = bpy.context.window_manager
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type in ("VIEW_3D", "PREFERENCES"):
                area.tag_redraw()


def load_cached_index():
    """Load the persistent index from disk (startup, file load, after reset)."""
    path = index_file()
    if os.path.exists(path) and CATALOG.load(path):
        prefs = get_prefs()
        if prefs is not None:
            CATALOG.pak_cache_limit = prefs.memory_packs
            if not prefs.iso_path:
                prefs.iso_path = CATALOG.path
        from .ui import refresh_lists
        refresh_lists()
        return True
    s = load_settings()
    prefs = get_prefs()
    if prefs is not None and not prefs.iso_path and s.get("iso_path"):
        prefs.iso_path = s["iso_path"]
    return False


def _scan_worker(path):
    try:
        cat = Catalog()
        ok = cat.scan(path,
                      progress=lambda f, m: SCAN.update(fraction=f, message=m),
                      cancel=lambda: SCAN["cancel"])
        SCAN["result"] = cat if ok else None
        if not ok:
            SCAN["error"] = "Scan cancelled"
    except Exception as e:  # report anything to the UI thread
        SCAN["error"] = "%s: %s" % (type(e).__name__, e)
        traceback.print_exc()
    finally:
        SCAN["running"] = False


def _finish_scan():
    cat = SCAN.get("result")
    SCAN["result"] = None
    if cat is None:
        return False
    CATALOG.data = cat.data
    CATALOG.path = cat.path
    CATALOG._close()
    CATALOG.save(index_file())
    save_settings(iso_path=cat.path, scanned=cat.data.get("scanned"))
    from .ui import refresh_lists
    refresh_lists()
    return True


class JSTARS_OT_scan(bpy.types.Operator):
    """Index the disc: read every archive table and list character, stage and motion packs.
The result is cached on disk and reused after restarts"""
    bl_idname = "jstars.scan"
    bl_label = "Scan Disc"

    _timer = None

    def _path(self, context):
        prefs = get_prefs(context)
        return bpy.path.abspath(prefs.iso_path) if prefs and prefs.iso_path else ""

    def execute(self, context):
        # Blocking variant (background mode / scripts)
        path = self._path(context)
        if not path or not os.path.exists(path):
            self.report({"ERROR"}, "Set the game disc (.iso or disc folder) first")
            return {"CANCELLED"}
        SCAN.update(running=True, error=None, cancel=False)
        _scan_worker(path)
        if SCAN["error"]:
            self.report({"ERROR"}, SCAN["error"])
            return {"CANCELLED"}
        _finish_scan()
        self.report({"INFO"}, "Indexed %d models, %d stages" % (len(CATALOG.models()), len(CATALOG.stages())))
        return {"FINISHED"}

    def invoke(self, context, event):
        if SCAN["running"]:
            self.report({"WARNING"}, "A scan is already running")
            return {"CANCELLED"}
        path = self._path(context)
        if not path or not os.path.exists(path):
            self.report({"ERROR"}, "Set the game disc (.iso or disc folder) first")
            return {"CANCELLED"}
        save_settings(iso_path=path)
        SCAN.update(running=True, fraction=0.0, message="Starting", error=None, cancel=False, result=None)
        threading.Thread(target=_scan_worker, args=(path,), daemon=True).start()
        self._timer = context.window_manager.event_timer_add(0.25, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type == "ESC":
            SCAN["cancel"] = True
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        _redraw_ui(context)
        if SCAN["running"]:
            return {"PASS_THROUGH"}
        context.window_manager.event_timer_remove(self._timer)
        if SCAN["error"]:
            self.report({"WARNING"}, SCAN["error"])
            return {"CANCELLED"}
        _finish_scan()
        _redraw_ui(context)
        self.report({"INFO"}, "J-Stars disc indexed: %d models, %d stages, %d motion sets" % (
            len(CATALOG.models()), len(CATALOG.stages()), len(CATALOG.motion_sets())))
        return {"FINISHED"}


class JSTARS_OT_cancel_scan(bpy.types.Operator):
    """Stop the running disc scan"""
    bl_idname = "jstars.cancel_scan"
    bl_label = "Cancel Scan"

    def execute(self, context):
        SCAN["cancel"] = True
        return {"FINISHED"}


class JSTARS_OT_reload_cache(bpy.types.Operator):
    """Reload the disc index from the cache folder"""
    bl_idname = "jstars.reload_cache"
    bl_label = "Reload Cache"

    def execute(self, context):
        if load_cached_index():
            self.report({"INFO"}, "Loaded cached disc index")
            return {"FINISHED"}
        self.report({"WARNING"}, "No cached index yet: scan the disc")
        return {"CANCELLED"}


class JSTARS_OT_clear_cache(bpy.types.Operator):
    """Delete the cached disc index (the disc itself is never modified)"""
    bl_idname = "jstars.clear_cache"
    bl_label = "Clear Cache"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        try:
            os.remove(index_file())
        except OSError:
            pass
        CATALOG.data = None
        CATALOG._close()
        from .ui import refresh_lists
        refresh_lists()
        return {"FINISHED"}


class JSTARS_OT_open_cache(bpy.types.Operator):
    """Open the cache folder in the file browser"""
    bl_idname = "jstars.open_cache"
    bl_label = "Open Cache Folder"

    def execute(self, context):
        bpy.ops.wm.path_open(filepath=cache_dir())
        return {"FINISHED"}


# ---------------------------------------------------------------------- helpers
def _need_catalog(op):
    if not CATALOG.ready:
        op.report({"ERROR"}, "Scan the disc first (J-Stars panel > Scan Disc)")
        return False
    if CATALOG.is_stale():
        op.report({"WARNING"}, "The disc changed or moved since the scan; rescan if imports fail")
    prefs = get_prefs()
    if prefs:
        CATALOG.pak_cache_limit = prefs.memory_packs
    return True


def _report_session(op, session, what, t0):
    msg = "%s imported in %.1fs" % (what, time.time() - t0)
    if session.warnings:
        msg += " (%d warnings, see console)" % len(session.warnings)
        for w in session.warnings:
            print("[J-Stars]", w)
    op.report({"INFO"}, msg)


def _deselect_all(context):
    for o in context.selected_objects:
        o.select_set(False)


def _selected(collection, index):
    if 0 <= index < len(collection):
        return collection[index]
    return None


# ---------------------------------------------------------------------- import model
class JSTARS_OT_import_model(bpy.types.Operator):
    """Import the selected character / model with its skeleton, skinned meshes and materials"""
    bl_idname = "jstars.import_model"
    bl_label = "Import Character"
    bl_options = {"REGISTER", "UNDO"}

    key: StringProperty(name="Model Key", default="", options={"SKIP_SAVE"})
    motions: EnumProperty(
        name="Motions",
        items=[("NONE", "None", "Model only"),
               ("IDLE", "Idle", "Also import the stand/idle motion"),
               ("ALL", "All", "Import every motion of this character's fighter set (can take a while)")],
        default="NONE")

    def execute(self, context):
        if not _need_catalog(self):
            return {"CANCELLED"}
        wm = context.window_manager
        key = self.key
        if not key:
            it = _selected(wm.jstars_models, wm.jstars_models_index)
            key = it.key if it else ""
        entry = next((m for m in CATALOG.models() if m["key"] == key), None)
        if entry is None:
            self.report({"ERROR"}, "Pick a character in the list")
            return {"CANCELLED"}
        if context.object and context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        from .build.model import import_model
        t0 = time.time()
        try:
            root, objs, session = import_model(context, CATALOG, entry)
        except Exception as e:
            traceback.print_exc()
            self.report({"ERROR"}, "Import failed: %s" % e)
            return {"CANCELLED"}
        _deselect_all(context)
        root.select_set(True)
        context.view_layer.objects.active = root
        if self.motions != "NONE" and root.type == "ARMATURE":
            sets = CATALOG.motion_sets_for(entry["id"])
            fighter = [s for s in sets if s["kind"] == "Fighter"] or sets
            if fighter:
                names = fighter[0]["motions"]
                if self.motions == "IDLE":
                    names = [n for n in names if "_00_00_" in n or "stand" in n.lower()][:1] or names[:1]
                _import_motion_names(self, context, root, fighter[0], names)
        _report_session(self, session, entry["label"], t0)
        return {"FINISHED"}


# ---------------------------------------------------------------------- import stage
class JSTARS_OT_import_stage(bpy.types.Operator):
    """Import the selected stage: geometry, materials, placed props and (per preferences) collision, shadow meshes and debris"""
    bl_idname = "jstars.import_stage"
    bl_label = "Import Stage"
    bl_options = {"REGISTER", "UNDO"}

    key: StringProperty(name="Stage Key", default="", options={"SKIP_SAVE"})

    def execute(self, context):
        if not _need_catalog(self):
            return {"CANCELLED"}
        wm = context.window_manager
        key = self.key
        if not key:
            it = _selected(wm.jstars_stages, wm.jstars_stages_index)
            key = it.key if it else ""
        entry = next((s for s in CATALOG.stages() if s["key"] == key), None)
        if entry is None:
            self.report({"ERROR"}, "Pick a stage in the list")
            return {"CANCELLED"}
        if context.object and context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        from .build.stage import import_stage
        t0 = time.time()
        try:
            root, objs, session = import_stage(context, CATALOG, entry)
        except Exception as e:
            traceback.print_exc()
            self.report({"ERROR"}, "Import failed: %s" % e)
            return {"CANCELLED"}
        _deselect_all(context)
        root.select_set(True)
        context.view_layer.objects.active = root
        _report_session(self, session, entry["label"], t0)
        return {"FINISHED"}


# ---------------------------------------------------------------------- import motion
def _target_armature(context):
    ob = context.object
    if ob and ob.type == "ARMATURE":
        return ob
    if ob and ob.parent and ob.parent.type == "ARMATURE":
        return ob.parent
    return None


def _import_motion_names(op, context, arm, mset, names):
    from .build.motion import import_motions
    prefs = get_prefs(context)
    ents = CATALOG.pak_entries(mset["pak"])
    blobs = []
    for n in names:
        data = ents.get(n)
        if data:
            short = n.rsplit("/", 1)[-1][:-4]
            blobs.append((short, data))
    res = import_motions(context, arm, blobs,
                         prefs.fps if prefs else 30,
                         prefs.set_scene_fps if prefs else True)
    if res and arm.animation_data:
        arm.animation_data.action = res[-1][0] if len(res) == 1 else res[0][0]
        if prefs is None or prefs.set_scene_fps:
            a = arm.animation_data.action
            context.scene.frame_end = int(a.frame_end)
    weak = [a.name for a, matched in res if matched == 0]
    if weak:
        op.report({"WARNING"}, "%d motion(s) matched no bones of %s" % (len(weak), arm.name))
    return res


class JSTARS_OT_import_motion(bpy.types.Operator):
    """Import the selected motion(s) as Actions on the active J-Stars armature"""
    bl_idname = "jstars.import_motion"
    bl_label = "Import Motion"
    bl_options = {"REGISTER", "UNDO"}

    all_in_set: BoolProperty(name="Whole Set", default=False, options={"SKIP_SAVE"})

    def execute(self, context):
        if not _need_catalog(self):
            return {"CANCELLED"}
        arm = _target_armature(context)
        if arm is None:
            self.report({"ERROR"}, "Select an imported J-Stars armature first")
            return {"CANCELLED"}
        wm = context.window_manager
        mset_item = _selected(wm.jstars_motion_sets, wm.jstars_motion_sets_index)
        mset = next((m for m in CATALOG.motion_sets() if mset_item and m["key"] == mset_item.key), None)
        if mset is None:
            self.report({"ERROR"}, "Pick a motion set")
            return {"CANCELLED"}
        if self.all_in_set:
            names = list(mset["motions"])
        else:
            it = _selected(wm.jstars_motions, wm.jstars_motions_index)
            if it is None:
                self.report({"ERROR"}, "Pick a motion")
                return {"CANCELLED"}
            names = [it.key]
        t0 = time.time()
        if context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        res = _import_motion_names(self, context, arm, mset, names)
        self.report({"INFO"}, "%d action(s) imported in %.1fs" % (len(res), time.time() - t0))
        return {"FINISHED"}


# ---------------------------------------------------------------------- loose files
def _read_if_exists(p):
    if os.path.exists(p):
        with open(p, "rb") as fh:
            return fh.read()
    return None


class JSTARS_OT_import_file(bpy.types.Operator, ImportHelper):
    """Import an extracted J-Stars .srd model/stage part (with .srdi/.srdv beside it) or a .san motion"""
    bl_idname = "jstars.import_file"
    bl_label = "Import J-Stars SRD / SAN"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".srd"
    filter_glob: StringProperty(default="*.srd;*.san", options={"HIDDEN"})

    def execute(self, context):
        path = self.filepath
        base, ext = os.path.splitext(path)
        with open(path, "rb") as f:
            data = f.read()
        if ext.lower() == ".san":
            arm = _target_armature(context)
            if arm is None:
                self.report({"ERROR"}, "Select a J-Stars armature to receive the motion")
                return {"CANCELLED"}
            from .build.motion import import_motions
            prefs = get_prefs(context)
            import_motions(context, arm, [(os.path.basename(base), data)],
                           prefs.fps if prefs else 30, prefs.set_scene_fps if prefs else True)
            return {"FINISHED"}

        def side(e):
            return _read_if_exists(base + e)
        from .formats import srd as S
        from .build.common import ImportSession, root_matrix
        from .build.model import build_armature, build_meshes
        session = ImportSession(context, os.path.basename(base))
        f = S.SrdFile(data, side(".srdi"), side(".srdv"), name=path)
        files = [f]
        # own texture pack first, then others in the folder (alternate forms
        # such as 004_naruto_1 share the base form's _texture.srd)
        folder = os.path.dirname(path)
        own = os.path.basename(base) + "_texture.srd"
        others = sorted(n for n in os.listdir(folder)
                        if n.lower().endswith("_texture.srd") and "lightmap" not in n.lower() and n != own)
        for n in [own] + others:
            tbase = os.path.join(folder, n[:-4])
            tex = _read_if_exists(tbase + ".srd")
            if tex:
                files.append(S.SrdFile(tex, None, _read_if_exists(tbase + ".srdv"), name=tbase))
        coll = session.make_root_collection(os.path.basename(base))
        if f.skeleton:
            root, _ = build_armature(session, os.path.basename(base), f.skeleton, coll)
        else:
            root = bpy.data.objects.new(os.path.basename(base), None)
            coll.objects.link(root)
        root.matrix_world = root_matrix(session)
        build_meshes(session, files, f, coll, root, skinned=bool(f.skeleton))
        return {"FINISHED"}


def menu_import(self, context):
    self.layout.operator(JSTARS_OT_import_file.bl_idname, text="J-Stars Victory VS+ (.srd/.san)")


classes = (
    JSTARS_OT_scan,
    JSTARS_OT_cancel_scan,
    JSTARS_OT_reload_cache,
    JSTARS_OT_clear_cache,
    JSTARS_OT_open_cache,
    JSTARS_OT_import_model,
    JSTARS_OT_import_stage,
    JSTARS_OT_import_motion,
    JSTARS_OT_import_file,
)
