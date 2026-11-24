"""J-Stars Victory VS+ importer for Blender 5.2.

Reads characters, motions, materials and stages straight from your own
J-Stars Victory VS+ (PS3) disc image. Nothing from the game is bundled.
"""
bl_info = {
    "name": "J-Stars Victory VS+ Importer",
    "author": "JStars-Blender contributors",
    "version": (1, 0, 2),
    "blender": (5, 2, 0),
    "location": "3D View > Sidebar > J-Stars, File > Import",
    "description": "Import characters, motions, materials and stages from a J-Stars Victory VS+ disc image",
    "category": "Import-Export",
}

try:
    import bpy
except ImportError:      # the formats/ and catalog modules also work outside Blender
    bpy = None

if bpy is not None:
    from . import operators, prefs, ui


def _deferred_load():
    try:
        operators.load_cached_index()
    except Exception as e:  # never break startup over a bad cache
        print("[J-Stars] could not load cached index:", e)
    return None


def register():
    bpy.utils.register_class(prefs.JSTARS_AddonPreferences)
    for c in ui.classes:
        bpy.utils.register_class(c)
    ui.register_props()
    for c in operators.classes:
        bpy.utils.register_class(c)
    bpy.types.TOPBAR_MT_file_import.append(operators.menu_import)
    # WindowManager is not ready during add-on registration at startup
    bpy.app.timers.register(_deferred_load, first_interval=0.2)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(operators.menu_import)
    for c in reversed(operators.classes):
        bpy.utils.unregister_class(c)
    ui.unregister_props()
    for c in reversed(ui.classes):
        bpy.utils.unregister_class(c)
    bpy.utils.unregister_class(prefs.JSTARS_AddonPreferences)
    from .catalog import CATALOG
    CATALOG.clear_memory()
