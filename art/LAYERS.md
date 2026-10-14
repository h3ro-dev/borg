# BORG layers artwork

An original exploded view of the BORG cube for the front page's "What you get"
section. The cube opens into three stacked layers joined by green light beams:
memory on top, tools in the middle and teamwork at the bottom. It uses the same
plate language, materials, lights and camera angle as `art/borg_ship.py`, so it
reads as the ship opened up. No franchise mesh, image, texture, scene or
generative image service is used.

Labels and numbers are not drawn into the picture. The page places them as HTML,
so they stay sharp, can be translated and can be read by screen readers.

## Rebuild

Use Blender 5.2.1 LTS, ImageMagick and libwebp's `cwebp`, from a repository
checkout:

```sh
blender --background --threads 4 --python art/borg_layers.py -- \
  --output /tmp/borg-layers --width 1400 --height 1600 --samples 32
magick /tmp/borg-layers/borg-layers.png -trim +repage \
  -bordercolor none -border 36 /tmp/borg-layers/borg-layers-trim.png
cwebp -q 86 -m 6 -alpha_q 100 /tmp/borg-layers/borg-layers-trim.png \
  -o /tmp/borg-layers/borg-layers.webp
```

The output directory also receives the editable `borg-layers.blend` and
`BLENDER-LAYERS-BUILD.json` with the seed, part count and the untrimmed label
anchors. The seeded geometry is reproducible; byte-for-byte renders can vary
with Blender builds and denoising, so the published file is pinned by its hash.

## Published file

- File: `site/assets/borg-layers.webp`
- Dimensions: 929 × 1101 pixels with alpha
- Length: 155,780 bytes
- SHA256: `271a72eb15dc2666f2474da12031cc578eb1711c306bda310aef7399f16b3745`
- Authoring: Blender 5.2.1 LTS, Cycles CPU, four threads, 32 samples with
  denoising; 8,342 original parts; render time 25 seconds.

The front page pins its numbers at these positions in the trimmed image, as
fractions of width and height: x 0.759 for all three layers, and y 0.331
(memory), 0.575 (tools) and 0.819 (teamwork). Re-measure them if the camera or
trim changes.
