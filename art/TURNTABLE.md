# BORG hero turntable

The hero on `collective.html` shows the ship turning slowly in the same quality as
its Blender poster. Earlier, the page swapped the poster for a live WebGL model a few
seconds after loading; the live model used simpler lighting, no glow and a different
size and angle, so the picture jumped. Now Blender renders the turn itself, and the
poster is the turn's first frame, so the hand-off is invisible.

`art/borg_ship_turntable.py` runs `art/borg_ship.py` without rendering, so the ship,
materials, lights, orthographic camera and bloom are exactly the poster's. It renders
one sweep of a gentle back-and-forth turn, 12 degrees each way and eased like a
pendulum, as transparent PNG frames. Played from the middle frame to the end, back to
the start and up to the middle again, the sweep loops without a seam and begins on the
poster pose.

## Rebuild

Use Blender 5.2.1 LTS, ffmpeg with libvpx-vp9, ImageMagick and libwebp's `cwebp`:

```sh
blender --background --python art/borg_ship_turntable.py -- \
  --output /tmp/borg-turn --frames 145 --resolution 1200 --samples 48 --gpu
```

Then build the loop order and encode (frame 72 is the poster pose):

```sh
cd /tmp/borg-turn && mkdir seq
python3 - <<'PY'
import os
order = list(range(72, 145)) + list(range(143, -1, -1)) + list(range(1, 72))
for i, k in enumerate(order):
    os.symlink(f'frames/f{k:04d}.png', f'seq/s{i:04d}.png')
PY
for size in 960 720; do
  ffmpeg -framerate 24 -i seq/s%04d.png -vf scale=$size:$size:flags=lanczos \
    -c:v libvpx-vp9 -pix_fmt yuva420p -b:v 0 -crf 34 -row-mt 1 -deadline good \
    -cpu-used 2 -an borg-ship-turn-$size.webm
done
magick frames/f0072.png -filter Lanczos -resize 960x960 poster-960.png
cwebp -q 88 -alpha_q 100 poster-960.png -o borg-ship-poster.webp
```

`--gpu` renders with Metal on Apple Silicon (about nine minutes for 145 frames);
without it Cycles uses the CPU. The output directory also receives the editable scene
and `BLENDER-TURNTABLE-BUILD.json`. Renders are reproducible from the fixed seed;
bytes can vary between Blender builds and denoisers, so published files are pinned by
hash.

## Published files

| File | Size | SHA256 |
| --- | --- | --- |
| `site/assets/borg-ship-poster.webp` (960 × 960) | 137,628 bytes | `c833f8af4807d8ad5e8016f999beea01f7107d60c4e28678d8c29233332775d7` |
| `site/assets/borg-ship-turn-960.webm` (12 s loop, 24 fps) | 3,103,530 bytes | `eebdc0a45d9557ea192564983121ae651188ed53ddb007e89d00935b436a9cab` |
| `site/assets/borg-ship-turn-720.webm` (12 s loop, 24 fps) | 1,935,893 bytes | `173caa9b11ccdd10032531ceeebb7a499d86ac310825e4296254598e5013e7d7` |

## How the page plays it

`site/scene.js` shows the poster first and downloads the turn only after the page has
loaded, while motion is on and the visitor is not saving data. Narrow screens get the
720 file. Before the turn takes over, the page reads two pixels of the first frame; if
the browser plays the video but drops its transparency, the poster stays, because a
black square behind the ship would be worse than a still ship. The shared motion
switch, reduced motion and offscreen pausing work as before.

Safari has not been tested with this file. If it drops the VP9 transparency, the
page keeps the poster there. A Safari-specific version would use HEVC with alpha; the
software encoder available when this was made was visibly softer than the poster, so
none is published yet.
