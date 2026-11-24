"""Add-on preferences and the persistent cache location."""
import json
import os

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty

ADDON_ID = __package__

SPECIAL_ITEMS = [
    ("OFF", "Off", "Do not import this geometry"),
    ("ON", "On", "Import it into its own visible collection"),
    ("HIDDEN", "Hidden", "Import it into its own collection with viewport and render visibility turned off"),
]

# key, label, description
SPECIAL_CATEGORIES = [
    ("geo_collision", "Collision", "Stage collision meshes ($COL blocks: ground, walls, breakable-object hulls)"),
    ("geo_camera_collision", "Camera Collision", "Collision volumes the camera is kept out of"),
    ("geo_shadow", "Shadow Meshes", "Blob/fake shadow geometry under props and trees"),
    ("geo_breakable", "Breakable Debris", "Stage 'knock' parts: the pieces that fly when scenery breaks"),
    ("geo_props", "Move-Specific Props", "Character 'bind_equipment' props that only appear during certain moves"),
    ("geo_reflection", "Reflection Meshes",
     "Copies of stage terrain the game draws only into water reflections (UMIREF materials); "
     "they sit on top of the real terrain"),
]


def cache_dir():
    """Folder that survives Blender restarts, factory resets of the scene and add-on updates."""
    try:
        path = bpy.utils.extension_path_user(ADDON_ID, path="cache", create=True)
        if path:
            return path
    except (ValueError, AttributeError, TypeError):
        pass
    path = os.path.join(bpy.utils.user_resource("CONFIG"), "jstars_importer_cache")
    os.makedirs(path, exist_ok=True)
    return path


def index_file():
    return os.path.join(cache_dir(), "disc_index.json.gz")


def settings_file():
    return os.path.join(cache_dir(), "settings.json")


def load_settings():
    try:
        with open(settings_file(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(**kw):
    s = load_settings()
    s.update(kw)
    try:
        with open(settings_file(), "w", encoding="utf-8") as f:
            json.dump(s, f, indent=1)
    except OSError:
        pass


def _iso_changed(self, context):
    if self.iso_path:
        save_settings(iso_path=bpy.path.abspath(self.iso_path))


class JSTARS_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_ID

    iso_path: StringProperty(
        name="Game Disc",
        description="J-Stars Victory VS+ disc image (.iso) or extracted disc folder (the one containing PS3_GAME)",
        subtype="FILE_PATH",
        update=_iso_changed,
    )

    geo_collision: EnumProperty(name="Collision", items=SPECIAL_ITEMS, default="OFF",
                                description=SPECIAL_CATEGORIES[0][2])
    geo_camera_collision: EnumProperty(name="Camera Collision", items=SPECIAL_ITEMS, default="OFF",
                                       description=SPECIAL_CATEGORIES[1][2])
    geo_shadow: EnumProperty(name="Shadow Meshes", items=SPECIAL_ITEMS, default="OFF",
                             description=SPECIAL_CATEGORIES[2][2])
    geo_breakable: EnumProperty(name="Breakable Debris", items=SPECIAL_ITEMS, default="OFF",
                                description=SPECIAL_CATEGORIES[3][2])
    geo_props: EnumProperty(name="Move-Specific Props", items=SPECIAL_ITEMS, default="OFF",
                            description=SPECIAL_CATEGORIES[4][2])
    geo_reflection: EnumProperty(name="Reflection Meshes", items=SPECIAL_ITEMS, default="OFF",
                                 description=SPECIAL_CATEGORIES[5][2])

    texture_mode: EnumProperty(
        name="Textures",
        items=[
            ("DDS", "Direct (DDS in memory)",
             "Hand the original compressed DXT data to Blender's own DDS loader, packed into the .blend. "
             "Fastest, keeps mipmaps, no files written"),
            ("PIXELS", "Decode with NumPy",
             "Decode to RGBA with NumPy and fill the image buffer directly, then pack as PNG"),
        ],
        default="DDS",
    )
    convert_axes: BoolProperty(
        name="Z-Up (Blender axes)",
        description="Rotate imports from the game's Y-up space to Blender's Z-up",
        default=True,
    )
    scale: FloatProperty(name="Scale", default=1.0, min=0.001, soft_max=100.0,
                         description="Uniform scale applied to the import root")
    fps: IntProperty(name="Motion Frame Rate", default=30, min=1, max=240,
                     description="Frames per second the game's motion key times are authored in")
    set_scene_fps: BoolProperty(name="Set Scene Frame Rate", default=True,
                                description="Set the scene frame rate and range when importing motions")
    bone_length: FloatProperty(name="Bone Display Length", default=0.05, min=0.001, max=1.0,
                               description="Length used for leaf bones (game skeletons have no bone lengths)")
    stage_instances: BoolProperty(
        name="Instance Repeated Stage Props",
        description="Place repeated props as collection instances (lighter, but the parts and their "
                    "materials are hidden inside the instance). Off: each prop is real objects sharing "
                    "mesh data, grouped under one empty per placement",
        default=False,
    )
    stage_shaders: BoolProperty(
        name="Rebuild Stage Shaders",
        description="Build stage materials from the game's compiled shaders (splat maps, vertex-colour "
                    "blends, tints). Off: wire the texture slots directly",
        default=True,
    )
    stage_water: BoolProperty(
        name="Stage Water",
        description="Add the water plane of stages with a sea (Planet Namek, Penguin Village). "
                    "The game generates it at run time from the stage parameters",
        default=True,
    )
    memory_packs: IntProperty(name="Packs Kept in Memory", default=12, min=1, max=200,
                              description="How many decompressed disc packs to keep in RAM between imports")

    def draw(self, context):
        lay = self.layout
        lay.prop(self, "iso_path")
        from .catalog import CATALOG
        row = lay.row()
        if CATALOG.ready:
            d = CATALOG.data
            row.label(text="Indexed: %d models, %d stages, %d motion sets (scanned %s)" % (
                len(d["models"]), len(d["stages"]), len(d["motion_sets"]), d.get("scanned", "?")), icon="CHECKMARK")
        else:
            row.label(text="Not scanned yet", icon="INFO")
        row.operator("jstars.scan", text="Scan Disc", icon="FILE_REFRESH")
        lay.label(text="Cache folder: " + cache_dir(), icon="FILE_FOLDER")

        box = lay.box()
        box.label(text="Special Geometry", icon="MOD_PHYSICS")
        col = box.column(align=True)
        for key, label, _desc in SPECIAL_CATEGORIES:
            col.row().prop(self, key, expand=True, text=label)

        box = lay.box()
        box.label(text="Import", icon="IMPORT")
        col = box.column()
        col.prop(self, "texture_mode")
        col.prop(self, "convert_axes")
        col.prop(self, "scale")
        col.prop(self, "bone_length")
        col.prop(self, "stage_instances")
        col.prop(self, "stage_shaders")
        col.prop(self, "stage_water")
        row = col.row()
        row.prop(self, "fps")
        row.prop(self, "set_scene_fps")
        col.prop(self, "memory_packs")


def get_prefs(context=None):
    context = context or bpy.context
    addon = context.preferences.addons.get(ADDON_ID)
    return addon.preferences if addon else None
