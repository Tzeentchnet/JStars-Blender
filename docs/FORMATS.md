# J-Stars Victory VS+ (PS3) data formats

All values are big-endian unless noted. These layouts were recovered from the disc for this importer. Container notes ($CMP, STPK, CPK masking) come from the jsp-vs-ex project.

## Containers

- **ISO 9660** with Joliet. Data in `PS3_GAME/USRDIR/data/PS3/partition_op_*_ps3.cpk`.
- **CPK**: CRI archive. The `@UTF` tables are XOR-masked (m = 0x655F, m *= 0x4115 per byte). File offset = TOC offset + `FileOffset`. No CPK-level compression is used on this disc.
- **$CMP**: 0x20 header (+0x10 unpacked size), then chunks `tag, u32 unpacked, u32 chunk size, u32`:
  `$CL0` LZ, `$CLH` Huffman-coded LZ (inner `$CH0`), `$CH0` Huffman bytes, `$CR0` stored.
- **STPK**: `u32 count @+8`, `u32 alignment @+12`, 48-byte entries from +16: `u32 offset, u32 size, 8 unknown, char name[32]` (names longer than 32 characters are cut). Packs nest.
- Pack suffixes: `_m` headers (`.srd`, `.san`), `_i` vertex/index data (`.srdi`), `_v` textures and stage vertex data (`.srdv`).

## SRD blocks

`char tag[4], u32 data_len, u32 child_len, u32 unk`. Data is padded to 16 bytes, then the child blocks, also padded to 16.

**$RSI** (resource info, first child of most blocks): `u8 1, u8 5, u8 flag, u8 nres, u16 nparams, u16 nparams2, u16 params_off, u16 params2_off, u32 name_off`; resources at +16, 16 bytes each, `u32 location, u32 size, ...`. Location `0x2xxxxxxx` = `.srdi`, `0x4xxxxxxx` = `.srdv`, low 28 bits = offset. Params: 16 bytes each, `u32 name_off, u32 value_off, u32 size, u16 0xFFFF`.

**$TXR**: `u32 1, u16 flags, u16 width, u16 height, u16 pitch, u8 format, u8 mips`.
Formats: `0x0F` DXT1, `0x11` DXT5, `0x13` DXT5 normal map (X in alpha, Y in green), `0x01` A8R8G8B8, `0x04` 16-bit. Mips are concatenated in the resource.

**$TXI** (texture instance): `+0x0C u8 count`, `+0x10 u32[count]` texture file names. The RSI name is the instance name.

**$MAT**: `+0x14 u16 list_off, +0x16 u8 count`; entries `u16 instance_name, u16 slot_name` (COLORMAPn, NORMALMAPn, SPECULARMAPn, TRANSPARENCYMAPn, REFLECTMAPn). RSI params hold toon-shader values (`OpRimLightInfo`, `OpBinaryThreshold`, ...).
`OpShaderInfo` is 12 × `u32` offsets of strings inside the material's own RSI data: the names of its compiled programs, `opbb_shader_<material>_<pass>.vcg` / `.pcg` for the passes `dg, dc, fu, sd, us, cs` (vertex then pixel program for each). Stage programs are in `mapNN_shader.srd` (see [Shaders](#shaders-_shadersrd)). The slot list only says which textures are bound; how they combine is decided by the program.
Eye materials have two or three COLORMAPs: an opaque iris (DXT1, often a multi-frame `$TXI` for expressions) and black DXT5 overlays whose alpha is the eyelid shading. Slot order varies by character (Naruto: overlay in 0, iris in 1; Vegeta and Gon: the reverse), so the importer takes the opaque layer as the base.

**$MSH**: `+4 u16 vtx_name, +6 u16 mat_name, +0xA colour-set list, +0xC UV-set list, +0xE sampler list`; `+0x10 u8 n_colour_sets, n_uv_sets, n_samplers`. A sampler entry is `u16 slot, u16 uv_set_name`. The RSI name is the scene path, e.g. `|model|group4|body16:OBI`.

**$VTX**: `+4 u16 attr_off, +6 u8 attr_count, +7 u8 primitive (2 = strip), +8 u32 vertex_count, +0xF u8 stream_count, +0x12 u16 stream_off, +0x16 u16 bone_names_off, +0x18 u8 bone_count`.
- Attribute (8 bytes): `u16 set_name_off, u8 offset, u8 stream, u8 semantic, u8 usage, u8 format, u8 0`.
- Semantics: 1 position, 2 normal, 3 colour, 4 texcoord, 5 tangent, 6 binormal, 7 blend weights, 8 blend indices.
- Format: low nibble = component count, high nibble 0 = float32, 1 = u32 integer (blend indices use `0x14`). `0x39` = signed 11:11:10 packed normal, `0x3A` = RGBA8.
- Stream (8 bytes): `u32 offset, u8 stride`.
- Resources: [0] vertex buffer, [1] u16 indices as triangle strips with 0xFFFF restart.
- Blend indices index the VTX's own bone-name list.

**$TRE** (scene graph): `+4 u16 rec_off, +6 u16 count, +0xC u32 matrix_off`. Records are 16 bytes: `u32 name, u32 refs_off, u8 nrefs, u8 depth, u8 flag, u8 type, u32 matrix_index`. Nodes are in depth-first order (the parent is the previous node with a smaller depth). If `type & 1`, the node has a local 4×4 matrix (row-major, row vectors). Refs are 8 bytes, `u32 mesh_path`.

**$SKL**: `+4 u16 rec_off, +6 u16 count`. 128-byte records: `u16 name, u16, u16 depth, u8, u8`, then 12 floats for the local transform (X, Y, Z axes and translation as row vectors), 12 floats for the inverse bind matrix, and 6 floats for rotation limits. Depth-first order.

**$COL** (collision): `+4 u32 node_off, +8 u16 node_count`. Nodes are 32 bytes: `6 floats AABB, u32 leaf_off, u8 depth, u8 leaf (0xFF = inner node), u8, u8`. A leaf is `u32 verts_off, u32 index_off, u16 nverts, u16 nindices`; vertices are float4, indices are u16 strips with restart. The RSI name looks like `|collusion|<name>`; `camera` in the name marks camera collision.

## Shaders (`*_shader.srd`)

A stage pack's `mapNN_shader.srd` (and `*_gimmick_*_shader.srd`) holds one `$VSD` (vertex program) or `$PSD` (pixel program) block per program, each with an `$RSI` and an empty `$CT0` child. The RSI name is the program name from `OpShaderInfo`. Only the passes `fu` (full), `us` (with real-time shadows) and, for some materials, `sd` are stored.

**Microcode**: the RSI's single resource is an offset **into the RSI data itself** (no `.srdi`/`.srdv`): `code = rsi_data[loc : loc + size]`.

**$PSD / $VSD data** (the parameter table, strings at the end of the data):

| Offset | Type | Meaning |
|---|---|---|
| +0x00 | u32 | 4 |
| +0x04 | u16 | input table offset |
| +0x06 | u16 | output table offset |
| +0x08 | u16 | patch table offset |
| +0x0A | u16 | uniform table offset |
| +0x0C | u16 | sampler table offset |
| +0x0E | u8 | input count |
| +0x0F | u8 | output count |
| +0x10 | u8 | uniform count |
| +0x11 | u8 | sampler count |

- Input (2 bytes): `u8 semantic, u8 index` (3 = COLOR, 4 = TEXCOORD, 0x0D = face).
- Uniform (12 bytes): `u16 name, u16 semantic, u16 patch_entry, ...`. The patch entry is `u16 list_off, u8 count, u8`; the list is `count × u32` **byte offsets in the microcode** of the inline constants that the runtime overwrites with this uniform. Semantics seen in stages: `DIRECTIONALLIGHTDIRECTION/COLOR`, `AMBIENTLIGHTCOLOR`, `BACKDIFFUSECORRECTION`, `UPDIRVIEW` (world up in view space), `POINTLIGHT0*`, `BINARYTHRESHOLD` (alpha test), `UVOFFSETn` (texture scroll), `FOG_COLOR`/`SUN_*` (sky fog), `SPECULARPOWER/INTENSITY`, `RIMLIGHTCOLOR`.
- Sampler (10 bytes): `u16 name (sampNN, lightMap), u16 semantic (SPHINT_<slot>_TEXCOORDn, TEXUNIT6), u8 texture unit, u8 dimension, u16 slot name (COLORMAP0 ...; 0 = none), u16`. **Texture units do not follow slot order**: in `a_a_a_lambert108`, unit 1 is COLORMAP2 and unit 2 is COLORMAP1. Unit 6 (`TEXUNIT6`) is always the lightmap, unit 5 (`TEXUNIT5`) a screen-space dither texture.

**SameTable**: identical programs are stored once. The RSI of the file's first block has a single parameter `SameTable`: `u32 n`, then `n` NUL-terminated strings read as pairs (dropped name, kept name). A kept name can itself be in the table; follow the chain. Without it about 40% of stage materials would have no program (e.g. `a_lambert138_fu.pcg` → `a_a_lambert149_fu.pcg`).

### RSX fragment microcode

16-byte instructions, four `u32` words (destination, SRC0, SRC1, SRC2). Each word is stored with its 16-bit halves swapped: read big-endian, then `w = (w >> 16) | (w << 16)`.

| Word | Bits | Field |
|---|---|---|
| DST | 0 | end of program |
| | 1–6 | destination register |
| | 7 | destination is fp16 (H register) |
| | 9–12 | write mask x, y, z, w |
| | 13–16 | input register read by this instruction (0 WPOS, 1 COL0, 2 COL1, 3 FOGC, 4–13 TEX0–TEX9, 14 SSA / facing) |
| | 17–20 | texture unit |
| | 24–29 | opcode (low 6 bits) |
| | 30 | no destination (condition-code only) |
| | 31 | saturate |
| SRC0/1/2 | 0–1 | register type: 0 temp, 1 input, 2 inline constant |
| | 2–7 | temp register index |
| | 8 | fp16 |
| | 9–16 | swizzle x, y, z, w (2 bits each) |
| | 17 | negate |
| SRC0 | 18–20 | execute if lt / eq / gt (all set = unconditional) |
| | 29 | absolute value |
| SRC1 | 18 | absolute value |
| | 28–30 | destination scale (1 ×2, 2 ×4, 3 ×8, 5 ÷2, 6 ÷4, 7 ÷8) |
| | 31 | opcode bit 6 (flow control) |
| SRC2 | 18 | absolute value |

If any source is an inline constant, the next 16 bytes are four floats (also half-swapped) and are skipped as an instruction. Opcodes used by the stages: `01 MOV, 02 MUL, 03 ADD, 04 MAD, 05 DP3, 09 MAX, 08 MIN, 0A SLT, 0B SGE, 0F SEQ, 17 TEX, 1A RCP, 1B RSQ, 1C EX2, 1D LG2, 38 DP2, 3A DIV (÷ src1.x), 3B DIVSQ (÷ √src1.x), 3D/3E fences`. Scalar operations read the first swizzled component.

### How the stage programs are built

All 2,800-odd stage `fu` programs share one structure:

1. Surface colour from the material's textures. Terrain blends 2–4 COLORMAPs, weighted by a splat texture's channels (`nt_map.bmp`, `map02_gnd01_alp.tga`) or by vertex colour `COL0` (`blend_01` / `colorSet1`), with tint constants compiled in. Some add a slope term: `acos(dot(N, UPDIRVIEW))` mixes in grass on flat ground.
2. Tangent-space normal maps (DXT5nm: X in alpha, Y in green, `2x − 1`) blended with the same weights, then put into view space with `TEX6` (normal), `TEX7` (tangent), `cross(TEX6, TEX7)` and the facing sign `SSA`.
3. `colour × lightmap(unit 6, TEXCOORD2) × 2`, then directional + ambient + back-diffuse lighting (uniforms).
4. HDR packing: `out = c / max(max(r, g, b), 0.25)` (capped at 63.75) with `out.w = 0.25 / that scale`. Opaque programs set the pre-pack alpha to 0.25.

Alpha-tested programs compare a texture alpha against `BINARYTHRESHOLD` (the material's `OpBinaryThreshold`, usually 0.5) with `SGE`; there is no `KIL`. Unlit programs (sky, cards) skip the lighting in step 3. Materials without textures compile either to a constant (`MOV R0, (0, 0, 0, 1)`, e.g. Namek's `sdo` shadow objects and black pit fills) or to lighting alone (`sdw` shadow meshes).

Example, Namek `a_L141` (COLORMAP0 = `nt_map` on `t_map`, COLORMAP1/2 = `e_base_08c` / `n_grs2` on `map1`):

```
s   = nt_map.rgb
col = mix(e_base_08c, n_grs2, s.r)
col *= mix(1, (0.212, 0.129, 0.004), s.g)
col *= mix(1, (0.165, 0.471, 0.588), s.r * s.b)
out = pack(col * lightmap * 2 * lighting)
```

## Motions (.san)

An SRD file holding one `$ANM` block: `u32 4, f32 length (frames), u32 track_off, ..., +0x1C u16 track_count`.
Track (40 bytes): `u32 name, u32 nT, u32 nR, u32 nS, u32 timesT, timesR, timesS, u32 valuesT, valuesR, valuesS`.
- Times: float32 frames (shared tables).
- Translation and scale: float4.
- Rotation: u64 "smallest three". Bits 60–61 = index of the dropped component in (x, y, z, w) order; the remaining components follow in (x, y, z, w) order starting at the **low** bits (bits 0–19, 20–39, 40–59), each mapped to [-1/√2, 1/√2]. Reading the fields high-bits-first swaps X and Z, which makes knees and fingers twist instead of bend.
- Values are offsets from the bone's rest local transform (T0, R0): `local = T(T0 + dT) · R0 · dR · S(1 + dS)`. Bones a motion leaves alone are keyed with zero translation, identity rotation and zero scale.

## Stages

- `mapNN.srd`: world-space meshes and `$COL` blocks.
- `mapNN_locator_obj.srd`: prop library, one `$TRE` per prop.
- `mapNN_shader.srd`: the compiled shader of every material (see [Shaders](#shaders-_shadersrd)); `mapNN_texture.srd` / `mapNN_lightmap_texture.srd`: textures.
- `mapNN_param_locator.srd` (in the `_param_m` pack): one `$TRE` per **placement group** (`|wood3` holds 24 trees, `|l_0_20_40` a single floor tile). Every node has a matrix (world = parent chain, as for any `$TRE`), and every `Op*` RSI parameter holds **one row per node** (row size = parameter size / node count):
  - `OpLocatorModelIndex`: 3 × u32 model **IDs**, the damage states intact → damaged → destroyed (`ieA_1001` house → `1002` → `1003` rubble; `wood_212` tree → `213` stump). IDs are not `$TRE` indices: they match the library tree's `<root>:OpLocatorObjectIndex` parameter (`|wood_230:OpLocatorObjectIndex` = 230). ID 0 is valid (Alabasta's `area_id0` floor tile).
  - `OpLocatorFileNameHash`: 3 × u32, non-zero where the matching ID column is in use (one hash per stage, naming the locator library). Group roots have all-zero rows.
  - `OpEventNonDisplay`: u32 per node; `OpLocatorCount`: number of placements in the group.
  - A few IDs are absent from the library (Namek's grass `gu_5`, `gu_6`, ...; split-screen packs drop most props of map08).
- `mapNN_locator_obj.srd`: some stages are mostly locator props. Alabasta's `map02.srd` holds only the backdrop, sky and boundary lines; the palace, floor tiles (`area_id*`), houses and palms are all placed props.
- `_knock_*` / `_scatter_*`: break-apart debris templates.
- **Water** (Planet Namek, Penguin Village): there is no water mesh. `mapNN_sea00.srd` holds one 256² DXT5 tangent-space wave normal map, and the game draws a plane from `mapNN_param.dat` (big-endian float table; inferred: float 32 = water height, −2.0 on both stages; floats 50–52 = gamma-space colour, Namek `(0.58, 0.67, 0.32)`, Penguin Village `(0.58, 0.67, 1.0)`). Namek's `ref_mode_near` / `ref_model_fer` groups (materials `UMIREF_*`) are unmirrored copies of the terrain drawn only into the water reflection.
- Pack number → stage: 00 Planet Namek, 01 Hidden Leaf Village, 02 Alabasta, 03 Vegetable Sky, 04 Penguin Village, 05 Kameari Police Station, 06 Temple of Athena, 07 Soul Society, 08 Inferno Crags, 09 Dark Tournament Stadium, 10 Tenraikyousintojo, 11 Yoshiwara Paradise. The names are the game's (`game_resident_US`: `K_MapName_US/MapName_NNN_US.txt`), but that table is in menu order; the pairing comes from each pack's stage-select card (`progress/map_img/progress_map_img_NN`) and texture prefixes (`k_` Konoha, `ara_` Alabasta, `p_` Penguin, `s_`/`500_athena` Sanctuary, `y_` Yu Yu Hakusho, `g_` Gintama).
