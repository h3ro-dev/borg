"""Exploded view of the BORG cube: three layers a newcomer can name.

Blender 5.2: blender --background --threads 4 --python art/borg_layers.py -- --output DIR
See art/LAYERS.md for the trim and WebP steps and the label positions the site uses.
Same procedural plate language, materials, lights and camera angle as
art/borg_ship.py, so the stack reads as "the cube, opened up". Labels are
left to the web page (crisp, translatable, readable by screen readers).
No external textures, imports, private paths or third-party artwork.
"""
import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import random
import sys

import bpy
from mathutils import Vector

args = argparse.ArgumentParser()
args.add_argument('--output', type=Path, required=True)
args.add_argument('--samples', type=int, default=32)
args.add_argument('--width', type=int, default=1400)
args.add_argument('--height', type=int, default=1600)
args.add_argument('--no-render', action='store_true')
opt = args.parse_args(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else [])
out = opt.output.resolve()
out.mkdir(parents=True, exist_ok=True)
rng = random.Random(40261)
bpy.ops.wm.read_factory_settings(use_empty=True)
verts, faces = defaultdict(list), defaultdict(list)


def material(name, color, metallic=.7, roughness=.48, emission=0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    m.diffuse_color = (*color, 1)
    p = m.node_tree.nodes.get('Principled BSDF')
    p.inputs['Base Color'].default_value = (*color, 1)
    p.inputs['Metallic'].default_value = metallic
    p.inputs['Roughness'].default_value = roughness
    if emission:
        p.inputs['Emission Color'].default_value = (*color, 1)
        p.inputs['Emission Strength'].default_value = emission
    return m


materials = [
    material('Hull · carbon titanium', (.034, .043, .046), .8, .49),
    material('Panels · graphite', (.074, .088, .088), .7, .55),
    material('Panels · gunmetal', (.13, .153, .145), .76, .43),
    material('Machinery · aged alloy', (.22, .24, .20), .72, .53),
    material('Trenches · black ceramic', (.009, .015, .013), .32, .74),
    material('Conduits · green ion light', (.21, 1, .018), .15, .37, 4.5),
    material('Ports · pale green light', (.64, 1, .32), .05, .3, 3.5),
    material('Data beams · soft green light', (.42, 1, .16), 0, .4, 9),
]
corners = [(-1,-1,-1), (1,-1,-1), (1,1,-1), (-1,1,-1),
           (-1,-1,1), (1,-1,1), (1,1,1), (-1,1,1)]
quads = [(0,3,2,1), (4,5,6,7), (0,1,5,4), (1,2,6,5), (2,3,7,6), (3,0,4,7)]


def box(center, size, mat, u=(1,0,0), v=(0,1,0), n=(0,0,1)):
    c, a, b, z = map(Vector, (center, u, v, n))
    start = len(verts[mat])
    for x,y,w in corners:
        verts[mat].append(c + a*x*size[0]/2 + b*y*size[1]/2 + z*w*size[2]/2)
    faces[mat].extend(tuple(start+i for i in q) for q in quads)


S = 5.47           # slab width and depth, the ship's hull size
H = 1.22           # slab height
GAP = 1.5          # open space between layers
PITCH = 5.66 / 30  # the ship's plate pitch


def greeble_face(origin, U, V, N, half_u, half_v, density=1.0):
    """Plates, trenches, ridges and ports on one face, like the ship's hull."""
    def plate(x, y, z, sx, sy, sz, mat):
        box(origin + U*x + V*y + N*z, (sx, sy, sz), mat, U, V, N)

    nu, nv = int(2*half_u/PITCH), max(2, int(2*half_v/PITCH))
    for ix in range(nu):
        for iy in range(nv):
            x, y = (ix-(nu-1)/2)*PITCH, (iy-(nv-1)/2)*PITCH
            trench = ix in (nu//4, nu//4+1, (2*nu)//3) or (nv > 10 and iy in (nv//3, (2*nv)//3) and ix < nu-6)
            depth = rng.uniform(.055, .18)
            if trench:
                if rng.random() < .33:
                    plate(x, y, -.005, .013, PITCH*.65, .012, 5)
                if rng.random() < .2:
                    plate(x+.045, y, .05, .038, PITCH*.88, .05, 2)
                continue
            sx, sy = PITCH*rng.uniform(.7,.95), PITCH*rng.uniform(.68,.95)
            plate(x, y, depth/2, sx, sy, depth, rng.choices([1,2,3],[.58,.34,.08])[0])
            if rng.random() < .4*density:
                plate(x, y, .015+depth, sx*.65, sy*.28, .019, 0 if rng.random() < .6 else 3)
            if rng.random() < .21*density:
                for k in range(3):
                    plate(x+(k-1)*sx*.21, y, .04+depth, .012, sy*.56, .024, 4)
            if rng.random() < .1:
                plate(x+sx*.3, y, .04+depth, .009, sy*rng.uniform(.23,.7), .018, 5)
            if rng.random() < .06:
                plate(x, y-sy*.29, .043+depth, sx*.16, .012, .011, 6)
    islands = int(36 * (half_u*half_v) / (2.735*2.735))
    for _ in range(max(3, islands)):
        x, y = rng.uniform(-half_u+.2, half_u-.2), rng.uniform(-half_v+.2, half_v-.2)
        sx, sy = rng.uniform(.2,.43), min(rng.uniform(.2,.5), half_v*.8)
        z = rng.uniform(.16, .27)
        plate(x, y, z, sx, sy, .12, 0)
        for k in range(5):
            plate(x+(k-2)*sx*.17, y, z+.08, sx*.08, sy*.9, .055, 2)
        if rng.random() < .35:
            plate(x+sx*.38, y, z+.11, .012, sy*.7, .014, 5)


def slab(cz, glow_band):
    """One layer: a hull block with detailed top and sides and a lit seam."""
    box((0, 0, cz), (S, S, H), 0)
    top = Vector((0, 0, cz + H/2))
    greeble_face(top, Vector((1,0,0)), Vector((0,1,0)), Vector((0,0,1)), S/2-.1, S/2-.1)
    for U, N in (((1,0,0),(0,-1,0)), ((-1,0,0),(0,1,0)), ((0,1,0),(1,0,0)), ((0,-1,0),(-1,0,0))):
        U, N = Vector(U), Vector(N)
        greeble_face(Vector((0,0,cz)) + N*(S/2), U, Vector((0,0,1)), N, S/2-.12, H/2-.1, density=.8)
    # A thin lit seam around the base says "this part is alive" without text.
    for U, N in (((1,0,0),(0,-1,0)), ((-1,0,0),(0,1,0)), ((0,1,0),(1,0,0)), ((0,-1,0),(-1,0,0))):
        U, N = Vector(U), Vector(N)
        c = Vector((0, 0, cz - H/2 + .07)) + N*(S/2 + .012)
        for k in range(12):
            if rng.random() < glow_band:
                t = -S/2 + .3 + k*(S-.6)/11
                box(c + U*t, (.34, .02, .022), 5, U, Vector((0,0,1)), N)


levels = [GAP + H, 0.0, -(GAP + H)]   # memory on top, tools in the middle, teamwork below
for cz, band in zip(levels, (.75, .55, .55)):
    slab(cz, band)

# Data beams between the layers: work and memory flowing up and down.
beam_spots = [(-1.7, -1.3), (.4, -2.0), (1.9, .6), (-.6, 1.5), (1.1, -.4)]
for upper, lower in ((levels[0], levels[1]), (levels[1], levels[2])):
    z0, z1 = lower + H/2, upper - H/2
    for (x, y) in beam_spots:
        box((x, y, (z0+z1)/2), (.035, .035, z1-z0-.04), 7)
        box((x, y, z1-.03), (.16, .16, .05), 6)
        box((x, y, z0+.03), (.16, .16, .05), 6)

root = bpy.data.objects.new('BORG_Layers', None)
bpy.context.collection.objects.link(root)
parts = []
for mat in sorted(verts):
    mesh = bpy.data.meshes.new('LayerGeometry_'+str(mat))
    mesh.from_pydata(verts[mat], [], faces[mat]); mesh.update()
    obj = bpy.data.objects.new(materials[mat].name, mesh)
    bpy.context.collection.objects.link(obj); obj.data.materials.append(materials[mat])
    obj.parent = root; parts.append(obj)
    if mat in (1, 2, 3):
        bevel = obj.modifiers.new('Machined edge', 'BEVEL')
        bevel.width = .004; bevel.segments = 1; bevel.affect = 'EDGES'
        bevel.limit_method = 'ANGLE'

scene = bpy.context.scene
world = bpy.data.worlds.new('Interstellar void'); world.use_nodes = True
world.node_tree.nodes['Background'].inputs[0].default_value = (.002, .005, .007, 1)
world.node_tree.nodes['Background'].inputs[1].default_value = .16; scene.world = world


def area(name, position, power, color, size):
    data = bpy.data.lights.new(name, 'AREA'); data.energy = power; data.color = color
    data.shape = 'DISK'; data.size = size
    ob = bpy.data.objects.new(name, data); bpy.context.collection.objects.link(ob); ob.location = position
    ob.rotation_euler = (-ob.location).to_track_quat('-Z', 'Y').to_euler()


area('Cold stellar key', (4, -6, 12), 2300, (.74, .84, 1), 8)
area('Green ion rim', (-6, 2, 7), 2300, (.37, 1, .12), 6)
area('Soft fill', (5, 6, 0), 1400, (.46, .6, .52), 7)
area('Face detail', (2, -9, -1), 900, (.65, .8, .75), 6)
area('Gap light', (7, -7, 0), 600, (.5, 1, .3), 3)
camera_data = bpy.data.cameras.new('Exploded camera')
camera = bpy.data.objects.new('Exploded camera', camera_data)
bpy.context.collection.objects.link(camera); camera.location = (9.5, -12, 7.4)
camera.rotation_euler = (-camera.location).to_track_quat('-Z', 'Y').to_euler()
camera_data.type = 'ORTHO'; camera_data.ortho_scale = 14.6; scene.camera = camera
scene.render.engine = 'CYCLES'; scene.cycles.device = 'CPU'; scene.cycles.samples = min(opt.samples, 64)
scene.cycles.use_denoising = True
scene.render.threads_mode = 'FIXED'; scene.render.threads = 4
scene.render.resolution_x = opt.width; scene.render.resolution_y = opt.height
scene.render.resolution_percentage = 100; scene.render.film_transparent = True
scene.render.image_settings.file_format = 'PNG'; scene.render.image_settings.color_mode = 'RGBA'
scene.view_settings.view_transform = 'AgX'
tree = bpy.data.node_groups.new('Ion bloom', 'CompositorNodeTree')
scene.compositing_node_group = tree
tree.interface.new_socket(name='Image', in_out='OUTPUT', socket_type='NodeSocketColor')
render = tree.nodes.new('CompositorNodeRLayers')
glow = tree.nodes.new('CompositorNodeGlare')
glow.inputs['Type'].default_value = 'Fog Glow'
glow.inputs['Quality'].default_value = 'High'
if 'Threshold' in glow.inputs: glow.inputs['Threshold'].default_value = 1.4
output = tree.nodes.new('NodeGroupOutput')
tree.links.new(render.outputs['Image'], glow.inputs['Image'])
tree.links.new(glow.outputs['Image'], output.inputs['Image'])

# Where each layer's centre lands in the image, so the page can pin its labels.
from bpy_extras.object_utils import world_to_camera_view
bpy.context.view_layer.update()
anchors = {}
for name, cz in zip(('memory', 'tools', 'teamwork'), levels):
    p = world_to_camera_view(scene, camera, Vector((S/2, 0, cz)))   # right face, mid-height
    anchors[name] = {'x': round(p.x, 4), 'y': round(1 - p.y, 4)}
bpy.ops.wm.save_as_mainfile(filepath=str(out/'borg-layers.blend'))
scene.render.filepath = str(out/'borg-layers.png')
if not opt.no_render: bpy.ops.render.render(write_still=True)
receipt = {'blender': bpy.app.version_string, 'seed': 40261, 'original_geometry': True,
           'boxes': sum(len(v) for v in verts.values())//8, 'width': opt.width, 'height': opt.height,
           'samples': scene.cycles.samples, 'label_anchors': anchors}
(out/'BLENDER-LAYERS-BUILD.json').write_text(json.dumps(receipt, indent=2)+'\n')
print(json.dumps(receipt))
