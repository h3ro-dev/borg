"""Build an original industrial cube ship in Blender and export its web asset.

Blender 5.2: blender --background --threads 4 --python art/borg_ship.py -- --output DIR
The optional .blend and PNG are working deliverables; the website uses GLB/WebP.
No external textures, scene imports, private paths, or third-party artwork.
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
args.add_argument('--samples', type=int, default=48)
args.add_argument('--resolution', type=int, default=1200)
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


box((0,0,0), (5.47,5.47,5.47), 0)
# Each basis is right handed. Greebles form recessed streets between uneven plates.
bases = [((1,0,0),(0,1,0),(0,0,1)), ((-1,0,0),(0,1,0),(0,0,-1)),
         ((1,0,0),(0,0,1),(0,-1,0)), ((-1,0,0),(0,0,1),(0,1,0)),
         ((0,1,0),(0,0,1),(1,0,0)), ((0,-1,0),(0,0,1),(-1,0,0))]
for side, (u,v,n) in enumerate(bases):
    U,V,N = map(Vector,(u,v,n))

    def plate(x,y,z,sx,sy,sz,mat):
        box(U*x+V*y+N*z,(sx,sy,sz),mat,U,V,N)

    pitch = 5.66 / 30
    for ix in range(30):
        for iy in range(30):
            x,y = (ix-14.5)*pitch,(iy-14.5)*pitch
            trench = ix in (8,9,20) or (iy in (9,21) and ix<24)
            depth = rng.uniform(.055,.18)
            if trench:
                # Sparse illuminated cables in deep channels, not a glowing grid.
                if rng.random()<.33:
                    plate(x,y,2.755,.013,pitch*.65,.012,5)
                if rng.random()<.2:
                    plate(x+.045,y,2.81,.038,pitch*.88,.05,2)
                continue
            sx,sy = pitch*rng.uniform(.7,.95),pitch*rng.uniform(.68,.95)
            plate(x,y,2.76+depth/2,sx,sy,depth,rng.choices([1,2,3],[.58,.34,.08])[0])
            if rng.random()<.4:
                plate(x,y,2.775+depth,sx*.65,sy*.28,.019,0 if rng.random()<.6 else 3)
            if rng.random()<.21:
                for k in range(3):
                    plate(x+(k-1)*sx*.21,y,2.80+depth,.012,sy*.56,.024,4)
            if rng.random()<.1:
                plate(x+sx*.3,y,2.80+depth,.009,sy*rng.uniform(.23,.7),.018,5)
            if rng.random()<.055:
                plate(x,y-sy*.29,2.803+depth,sx*.16,.012,.011,6)

    # Asymmetric equipment islands and dense heat-exchanger ridges.
    for _ in range(36):
        x,y = rng.uniform(-2.55,2.55),rng.uniform(-2.55,2.55)
        sx,sy = rng.uniform(.2,.43),rng.uniform(.2,.5)
        z = rng.uniform(2.92,3.03)
        plate(x,y,z,sx,sy,.12,0)
        for k in range(5):
            plate(x+(k-2)*sx*.17,y,z+.08,sx*.08,sy*.9,.055,2)
        if rng.random()<.35:
            plate(x+sx*.38,y,z+.11,.012,sy*.7,.014,5)

    # Segmented edge frame: intact silhouette, intentionally irregular detail.
    for k in range(10):
        t=-2.6+k*.58
        for edge in (-2.82,2.82):
            plate(edge,t,2.94,.055,.43,.1,2)
            plate(t,edge,2.94,.43,.055,.1,2)
    for _ in range(20):
        x,y=rng.uniform(-2.65,2.65),rng.uniform(-2.65,2.65)
        plate(x,y,2.97,rng.uniform(.17,.6),.028,.045,3)

root = bpy.data.objects.new('BORG_Ship',None)
bpy.context.collection.objects.link(root)
ship=[]
for mat in sorted(verts):
    mesh=bpy.data.meshes.new('ShipGeometry_'+str(mat))
    mesh.from_pydata(verts[mat],[],faces[mat]); mesh.update()
    obj=bpy.data.objects.new(materials[mat].name,mesh)
    bpy.context.collection.objects.link(obj); obj.data.materials.append(materials[mat])
    obj.parent=root; ship.append(obj)
    # Fine mechanical edges catch grazing light while keeping a small GLB.
    if mat in (1,2,3):
        bevel=obj.modifiers.new('Machined edge', 'BEVEL')
        bevel.width=.004; bevel.segments=1; bevel.affect='EDGES'
        bevel.limit_method='ANGLE'

root.rotation_euler=(0,0,0)
root.keyframe_insert('rotation_euler',frame=1)
root.rotation_euler=(math.radians(3),math.radians(-4),math.radians(14))
root.keyframe_insert('rotation_euler',frame=240)
root.location=(0,0,.16);root.keyframe_insert('location',frame=120)
root.location=(0,0,0);root.keyframe_insert('location',frame=1)
root.keyframe_insert('location',frame=240)
scene=bpy.context.scene;scene.frame_start=1;scene.frame_end=240;scene.frame_set(1)

# Only the original ship is exported. Camera/lights/background are browser-owned.
bpy.ops.object.select_all(action='DESELECT')
for obj in [root]+ship: obj.select_set(True)
bpy.context.view_layer.objects.active=root
bpy.ops.export_scene.gltf(filepath=str(out/'borg-ship.glb'),export_format='GLB',
    use_selection=True,export_animations=False,export_apply=False,
    export_yup=True,export_cameras=False,export_lights=False,
    export_meshopt_compression_enable=True,export_meshopt_extension='EXT_meshopt_compression')

world=bpy.data.worlds.new('Interstellar void');world.use_nodes=True
world.node_tree.nodes['Background'].inputs[0].default_value=(.002,.005,.007,1)
world.node_tree.nodes['Background'].inputs[1].default_value=.16;scene.world=world

def area(name,position,power,color,size):
    data=bpy.data.lights.new(name,'AREA');data.energy=power;data.color=color;data.shape='DISK';data.size=size
    ob=bpy.data.objects.new(name,data);bpy.context.collection.objects.link(ob);ob.location=position
    ob.rotation_euler=(-ob.location).to_track_quat('-Z','Y').to_euler()

area('Cold stellar key',(4,-6,10),1900,(.74,.84,1),7)
area('Green ion rim',(-5,2,6),2000,(.37,1,.12),5)
area('Soft fill',(4,6,1),1300,(.46,.6,.52),6)
area('Face detail',(2,-8,1),750,(.65,.8,.75),5)
camera_data=bpy.data.cameras.new('Hero camera');camera=bpy.data.objects.new('Hero camera',camera_data)
bpy.context.collection.objects.link(camera);camera.location=(9.5,-12,8)
camera.rotation_euler=(-camera.location).to_track_quat('-Z','Y').to_euler()
camera_data.type='ORTHO';camera_data.ortho_scale=11.5;scene.camera=camera
scene.render.engine='CYCLES';scene.cycles.device='CPU';scene.cycles.samples=opt.samples
scene.cycles.use_denoising=True
scene.render.threads_mode='FIXED';scene.render.threads=4
scene.render.resolution_x=opt.resolution;scene.render.resolution_y=opt.resolution
scene.render.resolution_percentage=100;scene.render.film_transparent=True
scene.render.image_settings.file_format='PNG';scene.render.image_settings.color_mode='RGBA'
scene.view_settings.view_transform='AgX'
tree=bpy.data.node_groups.new('Ion bloom','CompositorNodeTree')
scene.compositing_node_group=tree
tree.interface.new_socket(name='Image',in_out='OUTPUT',socket_type='NodeSocketColor')
render=tree.nodes.new('CompositorNodeRLayers')
glow=tree.nodes.new('CompositorNodeGlare')
glow.inputs['Type'].default_value='Fog Glow'
glow.inputs['Quality'].default_value='High'
if 'Threshold' in glow.inputs:glow.inputs['Threshold'].default_value=1.4
output=tree.nodes.new('NodeGroupOutput')
tree.links.new(render.outputs['Image'],glow.inputs['Image']);tree.links.new(glow.outputs['Image'],output.inputs['Image'])
bpy.ops.wm.save_as_mainfile(filepath=str(out/'borg-ship.blend'))
scene.render.filepath=str(out/'borg-ship-poster.png')
if not opt.no_render:bpy.ops.render.render(write_still=True)
receipt={'blender':bpy.app.version_string,'seed':40261,'original_geometry':True,
    'mesh_groups':len(ship),'boxes':sum(len(v) for v in verts.values())//8,
    'source_vertices':sum(len(v) for v in verts.values()),'glb_bytes':(out/'borg-ship.glb').stat().st_size,
    'resolution':opt.resolution,'samples':opt.samples,'animation_frames':240}
(out/'BLENDER-BUILD.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt))
