# Known limitations

- Stage lightmaps are loaded as textures but not wired. The game assigns lightmap atlas pages at runtime (`OpbbLighMapData`), not by name. Rebuilt stage materials therefore show the albedo without baked shading, and the game's `× 2` lightmap scale is left out.
- Shader rebuilding covers stage `fu` programs. Real-time lights, fog, specular and the shadow pass are not translated. Conditional instructions (dither fades) are skipped, and the normal-map Y direction follows the game's tangent frame, which has not been checked against a reference.
- The game shades in gamma space, so rebuilt materials read textures and vertex colours raw and convert the result with a 2.2 gamma node. Vertex colours are stored as float colour attributes for this reason.
- Meshes whose shader outputs lighting alone, anything named `sdw*` (including the opaque leaf cards in Alabasta's palm `sdwl`/`sdwm` groups) and Namek's `sdo` objects count as Shadow Meshes.
- Toon-shader parameters (rim light, shadow pull-up and so on) are stored on each material as `srd_*` custom properties. The material itself is a Principled BSDF approximation.
- Facial (`.fei`), effect (`.feff`) and stage object animations are not imported yet.
- A few debris templates reference textures that are not on the disc. They import untextured and a warning is printed.
