"""Texture -> bpy.types.Image without temp files.

DDS mode wraps the disc's DXT blocks in a DDS header and packs those bytes
straight into the Image datablock, so Blender's own loader decodes them
(mipmaps included). PIXELS mode decodes with NumPy and writes the float
buffer through ``foreach_set``.
"""
import bpy
import numpy as np

from ..formats import texture as tx


def _find_existing(key):
    for img in bpy.data.images:
        if img.get("jstars_key") == key:
            return img
    return None


def _image_from_dds(name, dds):
    # Order matters: name the file, pack the bytes, then switch to FILE.
    # Blender then decodes the packed DDS lazily and logs nothing.
    img = bpy.data.images.new(name, 4, 4, alpha=True)
    img.filepath_raw = "//jstars/" + name + ".dds"
    img.pack(data=dds, data_len=len(dds))
    img.source = "FILE"
    if tuple(img.size) == (0, 0):
        bpy.data.images.remove(img)
        return None
    return img


def _image_from_pixels(name, rgba):
    h, w = rgba.shape[:2]
    alpha = tx.has_alpha(rgba)
    img = bpy.data.images.new(name, w, h, alpha=alpha)
    px = np.flipud(rgba).astype(np.float32).ravel() / 255.0
    img.pixels.foreach_set(px)
    img.update()
    img.pack()
    return img


def load_texture(session, srdfile, tex, key_prefix=""):
    """Return a bpy image for a parsed $TXR, creating it once per disc texture."""
    key = key_prefix + "|" + tex.name
    if key in session.images:
        return session.images[key]
    img = _find_existing(key)
    if img is None:
        data = srdfile.texture_bytes(tex)
        if data is None:
            session.warn("Texture data missing for %s" % tex.name)
            session.images[key] = None
            return None
        name = tex.name.rsplit(".", 1)[0]
        mode = session.pref("texture_mode", "DDS")
        if mode == "DDS" and tx.is_block_compressed(tex.fmt):
            dds = tx.make_dds(tex.fmt, tex.width, tex.height, data, tex.mips)
            img = _image_from_dds(name, dds) if dds else None
        if img is None:
            try:
                rgba = tx.decode_rgba(tex.fmt, tex.width, tex.height, data,
                                      swizzled=(tex.pitch == 0 and not tx.is_block_compressed(tex.fmt)))
            except ValueError as e:
                session.warn("Could not decode %s: %s" % (tex.name, e))
                session.images[key] = None
                return None
            img = _image_from_pixels(name, rgba)
        img["jstars_key"] = key
        img["jstars_format"] = tex.fmt
        if tex.fmt == tx.FMT_DXT5_N or tex.name.lower().endswith(("_n.tga", "_nrm.bmp", "_n.bmp")):
            img.colorspace_settings.name = "Non-Color"
    session.images[key] = img
    return img
