# Original BORG ship artwork

The website's industrial cube ship is original procedural geometry authored with
Blender 5.2.1 LTS. No franchise mesh, image, texture, or scene was imported.
The script and generated ship artwork use this repository's MIT license.
Provider symbols elsewhere on the website retain their owners' trademark rights.

## Rebuild

Run from the repository checkout with Blender 5.2 and libwebp's `cwebp` installed:

```sh
blender --background --threads 4 --python art/borg_ship.py -- \
  --output /tmp/borg-art --samples 48 --resolution 1200
cwebp -q 88 -alpha_q 100 /tmp/borg-art/borg-ship-poster.png \
  -o /tmp/borg-art/borg-ship-poster.webp
```

On macOS, the Blender executable inside the application bundle can be used instead
of `blender`. The output directory receives an editable `.blend` scene, compressed
GLB, transparent PNG render, and build metadata. The website uses only the GLB and
WebP. To intentionally refresh published artwork, copy those two reviewed outputs
to `site/assets/`, inspect them, update their exact pins in the release guard, and
regenerate the release inventory after the normal source scan passes.

## Asset design and performance

The seeded scene contains 12,121 original cuboid details across seven material
groups: layered alloy plates, recessed conduits, heat-exchanger ridges, equipment
islands, and green emissive ports. A 240-frame gentle rotation/float animation is
included in the working Blender scene. The browser controls its own animation so
pause, reduced motion, offscreen and visibility behavior can follow the page.

The web export omits the tiny render-only bevel modifier. Blender's native
`EXT_meshopt_compression` reduces this version to 1,709,776 bytes without external
textures. The browser uses a local Meshopt decoder. The poster is a 1,200-square
transparent Cycles render, 48 samples, with denoising and a subtle compositor glow.
The mechanical seed is fixed; exporter changes may change exact bytes. Published
artifacts remain pinned by byte length and SHA256 rather than assuming bitwise
reproducibility across Blender versions.

The page supplies its own space background, lighting, camera and starfield. Its
fallback does not need WebGL. Blender and its rendering machinery are tools used
to author these outputs; they are not bundled into the website.
