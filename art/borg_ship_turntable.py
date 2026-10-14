"""Render the BORG ship turning slowly, in the same quality as its poster.

Blender 5.2: blender --background --python art/borg_ship_turntable.py -- --output DIR
It runs art/borg_ship.py without rendering, so the ship, materials, lights, camera and
bloom are exactly the poster's. It then renders one sweep of a gentle back-and-forth turn
as transparent PNG frames. The middle frame is the poster pose, so the website can start on
the poster and play the turn without a visible jump. See art/TURNTABLE.md for encoding.
"""
import argparse
import json
import math
from pathlib import Path
import runpy
import sys

import bpy

args = argparse.ArgumentParser()
args.add_argument('--output', type=Path, required=True)
args.add_argument('--resolution', type=int, default=1200)
args.add_argument('--samples', type=int, default=64)
args.add_argument('--frames', type=int, default=145, help='frames in one sweep; odd, so the middle is the poster pose')
args.add_argument('--amplitude', type=float, default=12.0, help='degrees turned each way')
args.add_argument('--gpu', action='store_true', help='render with Metal on Apple Silicon')
opt = args.parse_args(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else [])
assert opt.frames % 2 == 1, '--frames must be odd'
out = opt.output.resolve()
(out / 'frames').mkdir(parents=True, exist_ok=True)

# Build the poster scene with the original script, without its still render.
ship_script = Path(__file__).resolve().parent / 'borg_ship.py'
sys.argv = [sys.argv[0], '--', '--output', str(out / 'scene'), '--no-render',
            '--samples', str(opt.samples), '--resolution', str(opt.resolution)]
runpy.run_path(str(ship_script), run_name='__main__')

scene = bpy.context.scene
root = bpy.data.objects['BORG_Ship']
root.animation_data_clear()
root.location = (0, 0, 0)

# One sweep from -amplitude to +amplitude, eased like a pendulum. Played forwards then
# backwards it loops without a seam; the middle frame (angle 0) is the poster pose.
angles = [opt.amplitude * math.sin(math.pi * (k / (opt.frames - 1) - 0.5)) for k in range(opt.frames)]
for frame, angle in enumerate(angles):
    root.rotation_euler = (0, 0, math.radians(angle))
    root.keyframe_insert('rotation_euler', frame=frame)
scene.frame_start, scene.frame_end = 0, opt.frames - 1

if opt.gpu:
    prefs = bpy.context.preferences.addons['cycles'].preferences
    prefs.compute_device_type = 'METAL'
    prefs.get_devices()
    for device in prefs.devices:
        device.use = device.type == 'METAL'
    scene.cycles.device = 'GPU'
scene.render.use_persistent_data = True
scene.render.filepath = str(out / 'frames' / 'f')
scene.render.image_settings.file_format = 'PNG'
scene.render.image_settings.color_mode = 'RGBA'
bpy.ops.render.render(animation=True)

receipt = {'blender': bpy.app.version_string, 'source': 'art/borg_ship.py', 'frames': opt.frames,
           'poster_frame': (opt.frames - 1) // 2, 'amplitude_degrees': opt.amplitude,
           'resolution': opt.resolution, 'samples': opt.samples, 'device': scene.cycles.device,
           'loop': 'middle frame to the end, back to the first, then up to the middle'}
(out / 'BLENDER-TURNTABLE-BUILD.json').write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps(receipt))
