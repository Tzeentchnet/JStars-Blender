# Round-tripping: a possible exporter

**Status: design proposal. Nothing here is implemented yet.**

This document describes an exporter that writes edited Blender data back into files the game can load: Blender → `.srd` / `.srdi` / `.srdv` / `.san` → STPK pack → `$CMP` → CPK. It covers what the importer keeps and loses, which edits can be supported and in what order, the writers needed at each layer, and what has to be measured on the disc first. Format layouts are in [FORMATS.md](FORMATS.md). Import conventions and the custom properties referred to below are in [IMPLEMENTATION.md](IMPLEMENTATION.md).

## Goals and non-goals

Goals:

- Edit an imported character, stage or motion in Blender and write it back so the game shows the change.
- Never modify the user's disc image. The exporter writes modified copies of the affected files into an output folder laid out like the disc (`PS3_GAME/USRDIR/data/PS3/...`). The importer already reads such folders, so its output can be checked by importing it again.
- Make a lossless pass-through possible: exporting an unedited import produces files equivalent to the originals.

Non-goals (for now):

- Authoring new shaders. Every material needs compiled RSX programs, and compiling Cg for RSX is out of scope. New or changed materials must reuse an existing program.
- Building ISO images. The output is files for an extracted disc folder.
- Writing data from scratch for a format whose fields are not fully understood (see the next section).

## Core principle: patch the original, do not regenerate

Many fields on the disc are read but not understood, or not read at all:

- the `u32 unk` word of every SRD block;
- the second `$RSI` parameter list and the last two words of each resource;
- the 8 unknown bytes of every STPK entry;
- `$MAT` +4 floats, `$VTX` +0x10 / +0x14, `$TRE` `flag`, `$SKL` padding and rotation limits;
- `$COL` inner-node bytes, the `$ANM` header between +0x0C and +0x1C;
- `OpLocatorFileNameHash` (the hash function is unknown);
- every RSI parameter the importer does not turn into a custom property.

A generator that rebuilt these files from Blender data would have to invent all of them. The exporter should instead **reload the original file from the disc and change only what the edit touched**, keeping every other byte. Every imported datablock already records where it came from (`jstars_model_key`, `jstars_stage_key`, `jstars_path`, `jstars_key`, `jstars_motion`), and the catalog can return the original bytes for any of them. A few more tags are needed (see [Importer changes needed first](#importer-changes-needed-first)).

The same rule applies to every layer: an unchanged pack entry, `$CMP` stream or CPK file is copied byte for byte.

## What the import keeps and what it loses

| Data | Import | Round-trip notes |
|---|---|---|
| Vertex positions | float32, buffer order kept one-to-one | Exact if the vertex count and order are unchanged |
| UVs | float32, V flipped (`1 − v`) | Exact; flip back |
| Vertex colours | RGBA8 → float colour attribute | Exact if rounded back (`round(c × 255)`) |
| Normals | packed 11:11:10 → custom per-vertex normals | Re-quantised; error ≤ 1/1023 per component. Blender's corner normals must be averaged back to one per vertex |
| Tangents, binormals | **not imported** | Must be regenerated (MikkTSpace via `calc_tangents`) or copied from the original when topology is unchanged |
| Triangles | strips → triangles; degenerate and duplicate faces dropped | Strips must be regenerated. Index buffers are u16 with `0xFFFF` as restart, so a vertex block holds at most 65,535 vertices |
| Skin weights | ≤ 4 per vertex, weights < 1e-5 dropped, groups by bone name | Rebuild the per-`$VTX` bone palette, keep 4 influences, normalise |
| Skeleton | edit bones from world matrices, scale stripped, invented lengths and tails | Not invertible in general. Copy the original `$SKL` unless bones are added (not supported) |
| Materials | node tree is an approximation; float params copied to `srd_*` | The node tree is **never** read back. Only texture assignments and `srd_*` values can be exported |
| Textures (DDS mode) | original DXT bytes packed unchanged | Lossless pass-through |
| Textures (NumPy mode or edited) | decoded RGBA | Need DXT re-compression and mips |
| Motions | 20-bit smallest-three quaternions, float times, linear keys | Re-quantisation error ≈ 1.4e-6 per component; times exact at 30 fps |
| Stage placement | `$TRE` world matrices on objects; props expanded into per-placement copies | Collapse back to `$TRE` matrices and locator rows |
| Collision | `$COL` tree flattened to one triangle mesh | Copy the original unless edited; rebuilding the tree needs the unknown node bytes |
| Generated content | water plane, sky / shadow / reflection sorting, prop holder empties | Importer-only. Ignore on export |

## Edits by tier

Each tier depends on the previous one.

**Tier 0: pass-through.** Re-encode unchanged data through every writer and compare against the original. This validates the container writers and needs no Blender code.

**Tier 1: in-place edits** (same vertex count and order, same texture size and format):

- move, sculpt or re-UV vertices; repaint vertex colours; reweight within the existing bone palette;
- replace a texture with one of the same size, format and mip count;
- change `srd_*` material values; values of the same byte size are patched in the RSI;
- move a stage object or locator prop (patch its `$TRE` matrix);
- replace a motion with an edited version (same name, same bones).

Every one of these is a fixed-size patch inside an existing file. No offsets move except in the rebuilt containers above it.

**Tier 2: size-changing edits:**

- change mesh topology (rebuild one vertex and index buffer; patch `$VTX` vertex count and the RSI resource location and size; relayout `.srdi` / `.srdv`);
- change texture size, format or mip count;
- add or remove locator placements (resize the `Op*` rows; copy `OpLocatorFileNameHash` from an existing row);
- change a motion's track or key count.

**Tier 3: structural edits** (research needed):

- add meshes (new `$MSH` and `$VTX` blocks; must reuse an existing `$MAT`, or a copy of one whose `OpShaderInfo` names existing programs);
- add materials by copying a `$MAT` and its programs' names (stages: through `SameTable`);
- edit collision (rebuild the `$COL` tree).

Adding bones or characters is out of scope.

## The write pipeline

The writers mirror the readers in `formats/`. Each needs a Tier 0 test: `read(write(read(x))) == read(x)`, and `write(read(x)) == x` where the format allows.

### 1. SRD (`formats/srd_write.py`)

- Keep the original block list. Re-serialise as `tag, data_len, child_len, unk` (unk copied), data padded to 16, children padded to 16.
- Patch fixed-size fields in place: `$VTX` count (+8), RSI resource `location` / `size`, `$TRE` matrices (row-major, row vectors: transpose the column-vector matrices back), RSI parameter values of unchanged size.
- When a string or parameter grows, rebuild that block's RSI string table and fix the offsets inside it. This is only needed in Tier 3.

### 2. `.srdi` / `.srdv`

- Resources are addressed by `location & 0x0FFFFFFF`, with the high nibble choosing the file (2 = `.srdi`, 4 = `.srdv`).
- Tier 1: write the new bytes over the old range.
- Tier 2: lay the file out again in original resource order and update every RSI that points into it. **Measure first:** the alignment of resources in the originals (16, 128 bytes or more) and whether texture data needs extra alignment for the RSX.

### 3. Vertex and index buffers

- Encode each attribute into its original stream, offset and format code: float32, `0x14` u32 indices, `0x39` signed 11:11:10 normals (x and y × 1023, z × 511, two's complement), `0x3A` RGBA8.
- Tier 1 keeps the original index buffer. Tier 2 generates strips with `0xFFFF` restarts. The simplest valid form is one restart-separated triangle per strip segment; a real stripifier makes smaller buffers. **Check:** whether primitive values other than 2 (strips) are accepted, which would allow plain triangle lists.
- Skinning: blend indices refer to the `$VTX` bone-name list. A Tier 1 reweight must stay within that list. **Measure:** the largest bone palette on the disc; it is probably limited by vertex-shader constants.

### 4. Textures

- A `$TXR` image that still holds its packed DDS from the import is copied byte for byte (strip the 128-byte DDS header).
- An edited image needs BC1/BC3 compression with the original mip count. Blender has no encoder. Options: a NumPy range-fit encoder (simple, lower quality), or accepting a DDS the user made with an external tool and only checking its format and size. The second is safer to start with.
- Normal maps (`0x13`) must be written as DXT5nm: X in alpha, Y in green.
- Uncompressed formats with `pitch = 0` must be Morton-swizzled again.

### 5. Motions (`.san`)

Invert the import (see [FORMATS.md](FORMATS.md#motions-san)):

```
dT = R0 · location          (row-wise: loc @ R0ᵀ)
dR = rotation_quaternion
dS = scale − 1
frame = Blender frame / k   (k = scene fps / 30 when Set Scene Frame Rate was off)
```

Smallest-three encoding: drop the component with the largest magnitude. Negate the whole quaternion so that component is positive (the decoder rebuilds it as `+√(1 − Σc²)`). Map each remaining component to `round((c·√2 + 1) / 2 · 0xFFFFF)`. Store the fields in x, y, z, w order from bit 0 and put the dropped index in bits 60–61.

Use the original `.san` as a template for the `$ANM` header, and bake Blender's interpolation to linear keys before writing. **Check:** whether the game expects every skeleton bone to have a track, as the originals do.

### 6. STPK

- Keep entry order, names (including the 32-character truncation) and the 8 unknown bytes. Recompute offsets with the pack's alignment (`+12`).
- Nested packs are rebuilt from the innermost pack outward. An entry that was `$CMP`-wrapped stays wrapped.

### 7. `$CMP`

- Unchanged streams are copied.
- Changed data can be written as a single `$CR0` (stored) chunk, or as `$CL0` LZ. The token limits are literal runs of 0–127 bytes, back-references of 0–31 bytes and distances of 1–1023 bytes.
- **Check:** whether the game accepts `$CR0`-only streams and very large chunks. Measure the largest chunk in the originals, and whether `pak_entries` of a plain (uncompressed) pack would also load in-game.

### 8. CPK

The CPK's `@UTF` table of contents has fixed-width rows, so it can be patched without rebuilding it:

- If the new pack fits in the old slot (up to the next file's aligned offset), write it there and patch `FileSize` / `ExtractSize`.
- Otherwise append it at the end of the CPK (aligned to `Align`, default 0x800), patch `FileOffset` and the sizes, and update the header's size fields.
- Re-apply the XOR mask after patching (the mask is its own inverse).
- **Check:** whether the TOC has CRC or checksum columns that must be recomputed, and which header fields (`ContentSize`, `EnabledDataSize`, ...) the game validates.

Alternative: the scan already finds loose `.pak` files under `data/PS3/`. If the game prefers loose files over CPK contents, the exporter could skip CPK patching entirely. This is unverified and worth testing first, because it would remove the riskiest writer.

## Converting Blender data

- **Find the original.** Use the collection's `jstars_model_key` / `jstars_stage_key` to reload the source pack through the catalog, and each object's `jstars_path` to find its `$MSH`. Images map back through `jstars_key`, actions through `jstars_motion`.
- **Undo the axes.** Mesh data is already in game space. Object transforms are relative to the root, which carries the Z-up rotation and scale, so take `root.matrix_world⁻¹ · object.matrix_world` and never apply the root's transform to the data.
- **Shared meshes.** One Blender mesh can stand for several `$MSH` blocks, and for every placement of a prop. Export the mesh once per `$VTX`.
- **Stage props.** A prop holder empty (`jstars_locator`) maps to one locator node. Its `matrix_basis` relative to the stage root is the node's world matrix, and that has to be turned back into a local matrix under the group's root. Children of the holder are views of the shared library model: editing their mesh edits the library model for every placement.
- **Skip importer-only content**: *Water*, prop holders as geometry, *_Prop Library*, the broken-state copies (export the library model instead) and anything in a *Hidden* special collection that was not edited.

## Importer changes needed first

To keep the exporter from guessing, the importer should record more provenance:

| Add to | Property | Why |
|---|---|---|
| Mesh data | `jstars_srd` (`<pak>\|<srd>`), `jstars_vtx`, `jstars_mat` | Find the exact `$VTX` / `$MAT` even when objects are renamed |
| Mesh data | integer point attribute `jstars_vid` (original vertex index) | Detect reordering, deletion and topology change, which decides Tier 1 or Tier 2 |
| Mesh data | `jstars_vcount` | Same, cheaply |
| Stage objects | `jstars_tre` (file, tree index, node index) | Patch the right `$TRE` matrix |
| Prop holders | locator file, group and row index | Patch the right `Op*` row |
| Actions | `jstars_set` (motion pack) | Write back into the right pack |

*Instance Repeated Stage Props* is the more faithful import mode for round-tripping, because each library model then exists once.

## Blender front end

- **File › Export › J-Stars Victory VS+** and an *Export* section in the J-Stars panel, with: output folder, target (*selected character* / *stage* / *actions*), and *Dry run* (report what would change without writing).
- Before writing, show a report per object: *unchanged*, *Tier 1 patch*, *Tier 2 rebuild* or *unsupported* (with the reason, such as "new material without a program" or "bone not in palette").
- The extension manifest's `[permissions] files` text must be updated, because the add-on would then write files as well as read them.

## Validation plan

1. **Writer unit tests** (`tests/`, with and without `JSTARS_ISO`): byte-identical STPK, SRD and CPK TOC re-serialisation for every pack on the disc; `$CMP` encode → decode identity; quaternion and normal encode → decode within the quantisation limits above.
2. **Round-trip import.** Export an unedited import, import the output folder with the importer and compare positions, UVs, weights, normals and motions numerically. This reuses the checks in [ACCURACY.md](ACCURACY.md).
3. **In-game.** Start with a visible Tier 1 change (one texture, one moved prop) in an emulator. Only then try topology changes.

## Milestones

| # | Deliverable | Depends on |
|---|---|---|
| 1 | STPK, SRD and `$CMP` (`$CR0`) writers with pass-through tests | — |
| 2 | Loose-pack vs CPK patching test in the game; CPK TOC patcher if needed | 1 |
| 3 | Importer provenance tags | — |
| 4 | Tier 1: texture replacement (DDS input), vertex edits, `srd_*` values | 1–3 |
| 5 | Tier 1: motion export | 1, 3 |
| 6 | Tier 1: stage object and prop moves | 1, 3 |
| 7 | Tier 2: topology and texture-size changes, `.srdi` / `.srdv` relayout | 4 |
| 8 | Tier 3 research: new meshes and materials, collision | 7 |

## Open questions to settle on the disc or in-game

- Alignment of `.srdi` / `.srdv` resources and of STPK entries in the originals.
- Whether the game loads `$CR0`-only `$CMP` streams, or uncompressed packs.
- Whether loose `.pak` files override CPK contents.
- CPK TOC checksum columns and validated header fields.
- Largest bone palette per `$VTX`; whether triangle lists (primitive ≠ 2) are supported.
- Whether every bone needs a motion track.
- The `OpLocatorFileNameHash` function (only needed for new locator libraries).
