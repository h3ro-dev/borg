"""Author the BORG constellation in Blender; see FLEET.md for reproduction.

Original ship geometry is reused from borg-ship.glb. Provider SVG paths are
imported unchanged and uniformly scaled onto untextured identification plates.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import sys

import bpy
from mathutils import Vector


def material(name, color, emission=0, metallic=0, roughness=.5):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    shader = mat.node_tree.nodes.get('Principled BSDF')
    shader.inputs['Base Color'].default_value = (*color, 1)
    shader.inputs['Metallic'].default_value = metallic
    shader.inputs['Roughness'].default_value = roughness
    shader.inputs['Emission Color'].default_value = (*color, 1)
    shader.inputs['Emission Strength'].default_value = emission
    return mat


def flat_material(name, color, strength=1):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    nodes.clear()
    shader = nodes.new('ShaderNodeEmission')
    shader.inputs['Color'].default_value = (*color, 1)
    shader.inputs['Strength'].default_value = strength
    output = nodes.new('ShaderNodeOutputMaterial')
    mat.node_tree.links.new(shader.outputs[0], output.inputs['Surface'])
    return mat


def box(name, location, dimensions, mat):
    bpy.ops.mesh.primitive_cube_add(size=1, location=location)
    obj = bpy.context.object
    obj.name = name
    obj.dimensions = dimensions
    obj.data.materials.append(mat)
    return obj


def line(name, points, mat, width=.012):
    curve = bpy.data.curves.new(name, 'CURVE')
    curve.dimensions = '3D'
    curve.bevel_depth = width
    curve.bevel_resolution = 2
    spline = curve.splines.new('POLY')
    spline.points.add(len(points)-1)
    for point, xyz in zip(spline.points, points):
        point.co = (*xyz, 1)
    obj = bpy.data.objects.new(name, curve)
    bpy.context.collection.objects.link(obj)
    obj.data.materials.append(mat)
    return obj


def text(name, body, x, y, size, mat, z=4.15):
    curve = bpy.data.curves.new(name, 'FONT')
    curve.body = body
    curve.align_x = 'CENTER'
    curve.align_y = 'CENTER'
    curve.size = size
    curve.space_character = 1.2
    obj = bpy.data.objects.new(name, curve)
    bpy.context.collection.objects.link(obj)
    obj.location = (x, y, z)
    obj.data.materials.append(mat)
    return obj


def insignia(path, x, y, width, height, mat):
    before = set(bpy.data.objects)
    bpy.ops.import_curve.svg(filepath=str(path))
    objects = list(set(bpy.data.objects)-before)
    bpy.context.view_layer.update()
    points = [obj.matrix_world @ Vector(corner) for obj in objects for corner in obj.bound_box]
    low = Vector(tuple(min(p[i] for p in points) for i in range(3)))
    high = Vector(tuple(max(p[i] for p in points) for i in range(3)))
    center = (low+high)/2
    scale = min(width/(high.x-low.x), height/(high.y-low.y))
    for obj in objects:
        # Preserve every source spline and relative position; uniform scaling only.
        obj.location = Vector((x, y, 4.2)) + (obj.location-center)*scale
        obj.scale *= scale
        obj.name = path.stem + ' official identifier'
        obj.data.materials.clear()
        obj.data.materials.append(mat)
        obj.data.resolution_u = 24


def area(name, location, target, energy, color, size):
    data = bpy.data.lights.new(name, 'AREA')
    data.energy, data.color, data.size = energy, color, size
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    obj.location = location
    obj.rotation_euler = (Vector(target)-obj.location).to_track_quat('-Z', 'Y').to_euler()


def build(opt):
    repo = Path(__file__).resolve().parents[1]
    out = opt.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(repo/'site/assets/borg-ship.glb'))
    templates = [obj for obj in bpy.data.objects if obj.type == 'MESH']
    # Bake the glTF coordinate conversion before making linked geometry instances.
    for obj in templates:
        obj.data.transform(obj.matrix_world)
        obj.parent = None
        obj.matrix_world.identity()
    for mat in bpy.data.materials:
        if not mat.use_nodes:
            continue
        shader = mat.node_tree.nodes.get('Principled BSDF')
        if shader and 'Emission' not in mat.name and 'light' not in mat.name:
            shader.inputs['Metallic'].default_value = .42
            c = shader.inputs['Base Color'].default_value
            shader.inputs['Base Color'].default_value = tuple(min(v*1.5, 1) for v in c[:3])+(1,)
    white = flat_material('Identifier white', (1, 1, 1))
    muted = flat_material('Label cool white', (.56, .72, .69))
    green = material('BORG signal', (.17, 1, .06), 3)
    link = material('Conceptual links', (.075, .39, .15), .9)
    plaque = flat_material('Plain identification plate', (.006, .012, .016))
    # All coordinates are a designed conceptual composition, never host state.
    ships = [
        ('BORG memory core', 0, .15, .68, (24, -29, -13)),
        ('GPT Codex', -6.0, 2.65, .34, (24, -28, 8)),
        ('Claude', 5.9, 2.55, .34, (28, -30, -11)),
        ('Grok', 5.4, -2.65, .32, (19, -28, 10)),
        ('Native tools remote nodes', -5.75, -2.8, .30, (27, -24, -8)),
    ]
    for name, x, y, scale, angles in ships:
        parent = bpy.data.objects.new(name, None)
        bpy.context.collection.objects.link(parent)
        parent.location = (x, y, 0)
        parent.scale = (scale,)*3
        parent.rotation_euler = tuple(math.radians(a) for a in angles)
        for template in templates:
            obj = bpy.data.objects.new(name+' / '+template.name, template.data)
            bpy.context.collection.objects.link(obj)
            obj.parent = parent
            if name == 'BORG memory core':
                for slot in obj.material_slots:
                    mat = slot.material
                    if 'light' not in mat.name:
                        slot.link = 'OBJECT'
                        slot.material = mat.copy()
                        slot.material.name = 'Core green / '+mat.name
                        shader = slot.material.node_tree.nodes.get('Principled BSDF')
                        color = shader.inputs['Base Color'].default_value
                        shader.inputs['Base Color'].default_value = (color[0]*.38, color[1], color[2]*.43, 1)
        # A narrow engine rail gives each vessel a legible green silhouette.
        area(name+' local key', (x-2, y+3, 6), (x, y, 0), 350, (.7, .88, 1), 4)
        area(name+' ion rim', (x+2, y-1, -1), (x, y, 0), 180, (.22, 1, .08), 2)
    for obj in templates:
        bpy.data.objects.remove(obj, do_unlink=True)
    for obj in list(bpy.data.objects):
        if obj.name == 'BORG_Ship':
            bpy.data.objects.remove(obj, do_unlink=True)

    # Links sit behind the hulls and terminate beneath them, not across logos.
    for _, x, y, _, _ in ships[1:]:
        line('Conceptual connection', [(0, .15, -3), (x*.55, .15, -3), (x, y, -3)], link)
        box('Signal packet', (x*.68, .15+(y-.15)*.29, -2.98), (.075, .075, .025), green)

    # Floating ID plates keep official marks flat, bright and free of hull texture.
    for x, y, width in [(-6, 2.35, 2.15), (5.9, 2.25, 2.2), (5.4, -2.85, 2.1)]:
        box('Provider identification plate', (x, y, 4), (width, .95, .06), plaque)
    insignia(repo/'site/assets/providers/openai.svg', -6, 2.4, .65, .65, white)
    insignia(repo/'site/assets/providers/claude.svg', 5.9, 2.25, 1.70, .52, white)
    insignia(repo/'site/assets/providers/grok.svg', 5.4, -2.85, 1.5, .57, white)
    text('GPT role', 'GPT / CODEX', -6, .88, .27, white)
    text('GPT detail', 'AGENT RUNTIME', -6, .49, .15, muted)
    text('Claude role', 'CLAUDE', 5.9, .80, .27, white)
    text('Claude detail', 'AGENT RUNTIME', 5.9, .41, .15, muted)
    text('Grok role', 'GROK', 5.4, -4.22, .27, white)
    text('Grok detail', 'PROVIDER ADAPTER', 5.4, -4.61, .15, muted)
    box('Core identification plate', (0, -.1, 4), (3.2, 1.28, .06), plaque)
    text('Core name', 'B O R G', 0, .09, .48, white)
    text('Core role', 'MEMORY / CONTEXT', 0, -.38, .18, muted)
    text('Core caption', 'OWNER-CONTROLLED', 0, -2.7, .17, muted)
    line('Core plate rail', [(-1.6, -.76, 4.1), (1.6, -.76, 4.1)], green, .018)
    box('Tools identification plate', (-5.75, -2.97, 4), (2.25, .86, .06), plaque)
    text('Tools symbol', '>_', -5.75, -2.94, .47, white)
    text('Tools role', 'TOOLS / NODES', -5.75, -4.3, .25, white)
    text('Tools detail', 'EXPLICIT ENROLLMENT', -5.75, -4.68, .15, muted)

    # Batched deterministic star mesh: tiny points, no bulky particles or blur.
    rng = random.Random(20260916)
    vertices, faces = [], []
    for _ in range(520):
        x, y = rng.uniform(-11, 11), rng.uniform(-7, 7)
        radius = rng.choices([.006, .01, .018], [75, 22, 3])[0]
        start = len(vertices)
        vertices += [(x-radius, y, -8), (x, y+radius, -8),
                     (x+radius, y, -8), (x, y-radius, -8)]
        faces.append(tuple(range(start, start+4)))
    mesh = bpy.data.meshes.new('Fine starfield geometry')
    mesh.from_pydata(vertices, [], faces)
    stars = bpy.data.objects.new('Fine starfield', mesh)
    bpy.context.collection.objects.link(stars)
    stars.data.materials.append(material('Distant starlight', (.42, .56, .68), 1))
    scene = bpy.context.scene
    world = bpy.data.worlds.new('Deep space')
    world.use_nodes = True
    world.node_tree.nodes['Background'].inputs[0].default_value = (.005, .012, .02, 1)
    world.node_tree.nodes['Background'].inputs[1].default_value = .28
    scene.world = world
    area('Fleet softbox', (-4, 5, 12), (0, 0, 0), 1600, (.72, .83, 1), 12)
    camera_data = bpy.data.cameras.new('Fleet camera')
    camera = bpy.data.objects.new('Fleet camera', camera_data)
    bpy.context.collection.objects.link(camera)
    camera.location = (0, 0, 32)
    camera_data.type = 'ORTHO'
    camera_data.ortho_scale = 19.2
    scene.camera = camera
    scene.render.engine = 'CYCLES'
    scene.cycles.device = 'CPU'
    scene.cycles.samples = min(48, max(1, opt.samples))
    scene.cycles.use_denoising = True
    scene.render.threads_mode = 'FIXED'
    scene.render.threads = 4
    scene.render.resolution_x = opt.width
    scene.render.resolution_y = round(opt.width*10/16)
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGB'
    scene.render.filepath = str(out/'borg-fleet.png')
    scene.view_settings.view_transform = 'AgX'
    scene.view_settings.look = 'AgX - Medium High Contrast'
    scene.view_settings.exposure = .7
    tree = bpy.data.node_groups.new('Restrained ion bloom', 'CompositorNodeTree')
    scene.compositing_node_group = tree
    tree.interface.new_socket(name='Image', in_out='OUTPUT', socket_type='NodeSocketColor')
    render = tree.nodes.new('CompositorNodeRLayers')
    glow = tree.nodes.new('CompositorNodeGlare')
    glow.inputs['Type'].default_value = 'Fog Glow'
    glow.inputs['Quality'].default_value = 'High'
    glow.inputs['Threshold'].default_value = 2.5
    output = tree.nodes.new('NodeGroupOutput')
    tree.links.new(render.outputs['Image'], glow.inputs['Image'])
    tree.links.new(glow.outputs['Image'], output.inputs['Image'])
    bpy.ops.wm.save_as_mainfile(filepath=str(out/'borg-fleet.blend'))
    bpy.ops.render.render(write_still=True)
    inputs = ['site/assets/borg-ship.glb'] + [f'site/assets/providers/{p}.svg' for p in ('openai', 'claude', 'grok')]
    receipt = {
        'blender': bpy.app.version_string, 'seed': 20260916,
        'dimensions': [scene.render.resolution_x, scene.render.resolution_y],
        'engine': scene.render.engine, 'device': scene.cycles.device,
        'threads': scene.render.threads, 'samples': scene.cycles.samples,
        'ships': len(ships), 'objects': len(scene.objects),
        'mesh_objects': sum(o.type == 'MESH' for o in scene.objects),
        'mesh_polygons_instanced': sum(len(o.data.polygons) for o in scene.objects if o.type == 'MESH'),
        'inputs_sha256': {p: hashlib.sha256((repo/p).read_bytes()).hexdigest() for p in inputs},
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    (out/'BLENDER-FLEET-BUILD.json').write_text(json.dumps(receipt, indent=2)+'\n')
    print(json.dumps(receipt))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--width', type=int, default=2200)
    parser.add_argument('--samples', type=int, default=32)
    args = sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else []
    build(parser.parse_args(args))
