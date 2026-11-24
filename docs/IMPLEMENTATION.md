# How the importer works

The add-on (version 1.0.2, Blender 5.2) has two layers:

- `jstars_importer/formats/` and `catalog.py` read the disc. They use only the standard library and NumPy (no `bpy`), so the scan runs on a worker thread and the tests run outside Blender.
- `jstars_importer/build/` turns parsed data into Blender datablocks. `operators.py`, `ui.py` and `prefs.py` are the Blender front end.

| Module | Role |
|---|---|
| `formats/iso.py` | ISO 9660 / Joliet reader, or an extracted disc folder with the same API |
| `formats/cpk.py` | CRI CPK table of contents (`@UTF` unmasking, ITOC, CRILAYLA) |
| `formats/cmp.py` | `$CMP` decompressor (LZ, Huffman, stored) |
| `formats/stpk.py` | STPK pack walker (nested packs, `$CMP` entries) |
| `formats/srd.py` | SRD blocks: `$RSI`, `$TXR`, `$TXI`, `$MAT`, `$MSH`, `$VTX`, `$TRE`, `$SKL`, `$COL` |
| `formats/texture.py` | DXT / ARGB decoding, Morton unswizzle, DDS header writer |
| `formats/san.py` | `.san` motions (`$ANM`) |
| `formats/rsx.py` | `$PSD` / `$VSD` shader blocks, RSX fragment microcode decoder, `SameTable` |
| `catalog.py` | Disc scan, persistent index, pack cache |
| `build/model.py`, `meshes.py`, `materials.py`, `images.py`, `motion.py` | Characters, meshes, materials, textures, actions |
| `build/stage.py`, `shader_nodes.py` | Stages, locator props, water, shader-to-node rebuilding |
| `build/common.py` | Import session, special-geometry collections, axis conversion, name classifiers |

Format layouts are in [FORMATS.md](FORMATS.md).

## Disc scan and index

**Scan Disc** reads every CPK table of contents under `PS3_GAME/USRDIR/data/PS3/` and records the absolute disc offset and size of every pack. Character, stage, world-map and motion packs are then decompressed once to list their `.srd` / `.srdi` / `.srdv` / `.san` entries. The scan runs on a thread with a progress bar and can be cancelled with Esc. It takes about 20–25 s.

The result is written as gzip'd JSON (`disc_index.json.gz`, about 300 KB, `CACHE_VERSION = 3`) to the extension's user folder (`extension_path_user(..., "cache")`). It holds the ISO path, size and mtime, the pack offsets and the pack listings. The model, stage and motion lists and their labels are rebuilt from the listings each time the index loads, so naming fixes apply without a rescan. The index loads at startup and on every `load_post` (File › New, open, revert). If the ISO's size or mtime changes, the panel asks for a rescan. `settings.json` in the same folder remembers the disc path.

At import time `Catalog.pak_entries()` reads a pack straight from its disc offset, decompresses it and walks it into `{path: bytes}`. The last *Packs Kept in Memory* packs (default 12) stay in an LRU cache. `srd_triplet()` finds a header's `.srdi` / `.srdv` in the sibling `_i` / `_v` packs, matching by the 32-character truncated name if needed.

## Characters

- **Skeleton**: `$SKL` local matrices are chained into world matrices. Each becomes an edit bone with its scale stripped and its tail along local +Y. The length is the distance to the nearest child, or *Bone Display Length* for leaf bones. Bones are created in file order, so Blender's bone order matches the `$SKL` index. Pose bones use quaternion rotation.
- **Meshes**: one object per `$MSH`, named after the last path segment (`body16:OBI`). The mesh datablock is shared by every `$MSH` with the same vertex block and material. Skinned meshes get one vertex group per bone name in the `$VTX` bone list, plus an Armature modifier. Models without a skeleton get an empty as root.
- **Materials**: one per `$MAT`, with texture slots wired to a Principled BSDF. COLORMAP goes to Base Color, with the alpha of DXT5 cut-outs. NORMALMAP is either a DXT5nm reconstruction (X from alpha, Y from green, Z rebuilt) or a Normal Map node. SPECULARMAP goes to Specular IOR Level and TRANSPARENCYMAP to Alpha. REFLECTMAP / REFLECTIVITY are added to the tree but left unlinked. Eyes and other multi-layer COLORMAPs use the first opaque layer as the base, with translucent layers alpha-blended over it. Toon-shader parameters are stored as `srd_*` custom properties.
- **Special geometry**: `bind_equipment` parts go to *Move-Specific Props*, and shadow-named parts to *Shadow Meshes* (see the README).

## Meshes and textures

- **Bulk mesh building**: vertices, loops, UVs, colours and custom normals are set with `foreach_set` from NumPy arrays. Triangle strips with primitive restart become triangles in vectorised NumPy. Vertex order matches the `$VTX` buffer one-to-one, and triangles with out-of-range indices are dropped. `mesh.validate()` then removes duplicate faces, which Blender's custom-normal code would otherwise crash on.
- **UVs** are named after the game's sets (`map1`, `map2`, `lightmap`), with V flipped (`v' = 1 − v`). **Vertex colours** are point-domain `FLOAT_COLOR` attributes named after the game's colour set (`colorSet1`). **Normals** are set as custom per-vertex normals.
- **Direct texture import**: DXT1/DXT5 data is wrapped in a DDS header in memory and packed into the Image datablock unchanged, so Blender's own decoder handles it, mipmaps included. No temp files are written. The NumPy decoder (vectorised BC1/BC3, Morton unswizzle) is the fallback and the alternative mode; its output is packed as PNG. Images are created once per disc texture and tagged with `jstars_key` and `jstars_format`.

## Motions

- `.san` rotations are decompressed, normalised and hemisphere-fixed in vectorised NumPy. Each channel is an offset from the rest pose (`local = T(T0 + dT) · R0 · dR · S(1 + dS)`). Because the edit bones use the game's bind matrices exactly, the pose channels are `location = R0ᵀ·dT`, `rotation = dR`, `scale = 1 + dS`.
- Keys go into the 5.x layered Action (slots) through `fcurve_ensure_for_datablock` with bulk `foreach_set`, using linear interpolation. Each Action has a fake user and a `jstars_motion` property. Bones the motion does not key are reset to rest.
- With *Set Scene Frame Rate* on, the scene is set to the motion frame rate (30) and the motion's range. With it off, key times are rescaled to the scene's frame rate.

## Stages

- **Layout**: each stage collection holds the root empty and one child collection per kind: *Terrain*, *Sky*, *Props*, *Water*, *Breakable Debris* and any special categories turned on. Sky is anything under a `sky*`, `*_sky` or `sora` node.
- **Placement**: meshes referenced by a `$TRE` get the node's world matrix; meshes no tree references sit at the origin. Mesh data is shared per (file, vertex block, material).
- **Props**: placements come from `mapNN_param_locator.srd`, and model IDs are resolved through the library's `OpLocatorObjectIndex`. Each placed prop is an empty named after its locator (e.g. `ya_574_5940`) with its parts as child meshes. The parts are linked duplicates that share mesh data, so they stay light, and each part can be selected and shows its materials in the outliner. *Instance Repeated Stage Props* switches to collection instances of a hidden *_Prop Library*. Each placement's damaged and destroyed states go to *Breakable Debris › Broken States*. Placements flagged `OpEventNonDisplay` are skipped.
- **Water** (Planet Namek, Penguin Village) is rebuilt as a plane at the height and colour from the stage parameters, with the game's wave normal map. The game generates this plane at run time. Its extent is the ground's bounds and the wave-map tiling (`_WAVE_TILE = 20`) is a guess. *Stage Water* in the import options turns it off.
- **Shader rebuilding**: every stage material has its own RSX pixel program (`mapNN_shader.srd`). That program blends texture layers using splat textures, vertex-colour weights, tint constants and a slope term. `shader_nodes.py` runs the program symbolically into an expression graph, with runtime uniforms left symbolic and an identity tangent frame. It takes the largest light-independent factor of the output colour as the albedo and emits it as Mix / Math / Vector Math nodes, plus a tangent-space normal. Textures are read raw (Non-Color, channel-packed alpha) and the albedo goes through a 2.2 gamma node, because the game shades in gamma space. Unlit programs (sky, cards) drive Emission instead. Alpha-tested programs use `OpBinaryThreshold`. A program that cannot be rebuilt falls back to plain slot wiring with a warning. *Rebuild Stage Shaders* in the preferences switches to slot wiring for everything.
- **Classification by shader**: a mesh whose program outputs lighting alone (no texture, no vertex colour) counts as a Shadow Mesh. A mesh with a `UMIREF*` material counts as a Reflection Mesh.

## Axes and units

Mesh data, bone matrices and motion values stay in the game's own space (Y-up, game units). Only the root object (armature or empty) carries the conversion: a +90° X rotation (*Z-Up*) and *Scale*. Child objects use their `$TRE` matrices as `matrix_basis` relative to that root.

## Custom properties written on import

The exporter design ([ROUNDTRIP.md](ROUNDTRIP.md)) depends on these provenance tags.

| Where | Property | Value |
|---|---|---|
| Character collection, root | `jstars_model_key` | `<pak>\|<srd base>` index key |
| Character root | `jstars_id`, `jstars_kind` | character ID, *Battle* / *J-Adventure* / ... |
| Stage collection, root | `jstars_stage_key` | stage pack path |
| Mesh objects | `jstars_path` | full `$MSH` / `$COL` scene path (`\|model\|group4\|body16:OBI`) |
| Prop empties | `jstars_locator` | locator node name |
| Water object | `jstars_water_height` | height from `mapNN_param.dat` |
| Materials | `srd_material`, `srd_<Param>` | `$MAT` name; float-decodable RSI parameters (≤ 16 floats, no NaN) |
| Stage materials | `srd_shader`, `srd_unlit` | program the nodes were built from; unlit flag |
| Water material | `srd_water_color` | gamma-space colour |
| Images | `jstars_key`, `jstars_format` | `<srd file>\|<texture>`; `$TXR` format byte |
| Actions | `jstars_motion` | motion name |
| Sub-collections | `jstars_sub` | kind (*Terrain*, *Props*, ...) |
