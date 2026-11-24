# Known limitations

## Shading

- Stage lightmaps are loaded as textures but not wired. The game assigns lightmap atlas pages at runtime (`OpbbLighMapData`), not by name. Rebuilt stage materials therefore show the albedo without baked shading, and the game's `× 2` lightmap scale is left out. The `lightmap` UV set is imported.
- Shader rebuilding covers stage `fu` programs only. Character materials are wired from their texture slots, and vertex programs are not decoded. Real-time lights, fog, specular and the shadow pass are not translated. Conditional instructions (dither fades) are skipped, and the normal-map Y direction follows the game's tangent frame, which has not been checked against a reference.
- Planet Namek's terrain renders a more saturated blue than in-game screenshots. This has not been investigated.
- The game shades in gamma space, so rebuilt materials read textures and vertex colours raw and convert the result with a 2.2 gamma node. Vertex colours are stored as float colour attributes for this reason.
- Toon-shader parameters (rim light, shadow pull-up and so on) are stored on each material as `srd_*` custom properties. The material itself is a Principled BSDF approximation.

## Geometry and classification

- Meshes whose shader outputs lighting alone, anything named `sdw*` (including the opaque leaf cards in Alabasta's palm `sdwl`/`sdwm` groups) and Namek's `sdo` objects count as Shadow Meshes.
- Vertex tangents and binormals are not imported; Blender recomputes tangents from the UVs. Skeleton rotation limits are not imported.
- Stage water is generated, as the game does at run time. Its height and colour come from inferred `mapNN_param.dat` entries, and the wave tiling and extent are guesses.
- Locator props hidden until an event (`OpEventNonDisplay`) are skipped. A few locator IDs are missing from the prop library (Namek's grass, most props of the map08 split-screen pack); they are listed in the console.
- A few debris templates reference textures that are not on the disc. They import untextured and a warning is printed.

## Not imported yet

- Facial (`.fei`, `_facial.srd`), effect (`.feff`) and stage object animations.
- Export back to the game format. See [ROUNDTRIP.md](ROUNDTRIP.md) for the design.
