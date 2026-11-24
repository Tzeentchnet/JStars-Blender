# J-Stars Victory VS+ Importer for Blender 5.2

Imports characters, skeletons, motions, materials, textures and stages directly from **your own** J-Stars Victory VS+ (PS3) disc image. No game data is included in this repository.

## Examples

| Character import (Boa Hancock, idle motion) | Stage import (Soul Society) |
|---|---|
| <img src="docs/images/charimport_hancock.webp" alt="Boa Hancock imported with skeleton, materials and idle pose" height="420"> | <img src="docs/images/levelimport_soulsociety.png" alt="Soul Society stage imported with terrain, props and sky" height="420"> |

## Install

1. Build the package (or use `dist/jstars_importer-1.0.2.zip`):
   ```
   "D:\Blender Foundation\Blender 5.2\blender.exe" --command extension build --source-dir jstars_importer --output-dir dist
   ```
2. In Blender: **Edit › Preferences › Get Extensions › ▾ › Install from Disk…** and pick the zip.

## Use

1. **3D View › Sidebar (N) › J-Stars**: set the disc (`.iso`, or an extracted folder containing `PS3_GAME`) and press **Scan Disc**. The scan runs in the background with a progress bar (about 20–25 s) and writes a small index (~300 KB) to the extension's user folder. That index is reloaded automatically after Blender restarts, File › New, or opening another file, so you only scan once. Use **Rescan** if the disc moves or changes.
2. **Characters**: pick a model (Battle, J-Adventure, Support or World Map) and choose **Import Character**, optionally with the idle motion or all motions.
3. **Motions**: select an imported armature. The list filters to that character's motion sets. Import one motion or the whole set as Actions.
4. **Stages**: pick a stage (normal or split-screen version) and choose **Import Stage**.
5. **File › Import › J-Stars Victory VS+ (.srd/.san)** imports loose extracted files.

<p align="center"><img src="docs/images/menu.png" alt="J-Stars sidebar panel: disc scan, Characters / Stages / Motions tabs and Special Geometry options" width="300"><br><em>The J-Stars sidebar panel after a scan, with the Stages tab open.</em></p>

### Special geometry (Preferences, or the *Special Geometry* sub-panel)

Each category can be **Off** (default), **On**, or **Hidden**. *Hidden* imports the geometry into its own collection with viewport (eye) and render visibility turned off.

| Category | What it is |
|---|---|
| Collision | `$COL` collision hulls (ground, walls, breakable-object hulls) |
| Camera Collision | Volumes the camera is kept out of |
| Shadow Meshes | Fake/blob shadow geometry (`shadow`, `shd`, `*_sha` parts, shadow materials) |
| Breakable Debris | Stage `knock`/`scatter` break-apart templates and the broken state of placed props |
| Move-Specific Props | Character `bind_equipment` props shown only during certain moves |
| Reflection Meshes | Planet Namek's terrain copies (`UMIREF` materials) that the game draws only into the water reflection; they overlap the real terrain |

## More documentation

- [What uses Blender 5.2 and NumPy](docs/IMPLEMENTATION.md): texture, mesh, animation, stage and shader import details
- [Accuracy checks done](docs/ACCURACY.md)
- [Known limitations](docs/LIMITATIONS.md)

Format notes are in [docs/FORMATS.md](docs/FORMATS.md). Container formats ($CMP, STPK, CPK layout) build on the notes of the [jsp-vs-ex](https://github.com/Tsamiuy/jsp-vs-ex) project.
