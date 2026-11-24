# Accuracy checks done

- Bone chains rebuilt from local matrices match the files' inverse-bind matrices (max error 1.7e-7).
- Skinned meshes in rest pose reproduce the raw vertex data exactly.
- Imported motions were checked against an independent NumPy forward-kinematics pass: Blender's evaluated bone positions match at every sampled frame.
- Locator placement counts were checked offline: Planet Namek places 103 props (70 distinct models), not 13 copies of one model as before the per-node fix.
- Stage names were matched to packs by each pack's stage-select card and texture prefixes, not by the game's menu-ordered name table.
- Every stage (25 variants), every battle and J-Adventure model (122) and a sample of world-map models import without errors in background mode. All 25 stage variants take about 3 minutes.

## Running the tests

`tests/test_formats.py` checks the format layer outside Blender. The offline tests cover strip decoding, quaternion field order, u32 blend indices, a fragment program and DXT1 decoding. The disc tests (index, a character model, textures, stage shaders, motion quaternions) run only when `JSTARS_ISO` points at your disc image. Nothing from the disc is written.

```
set JSTARS_ISO=path\to\disc.iso
python -m pytest tests -q
```
