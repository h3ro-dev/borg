"""Original procedural biomechanical character. Reproduce using DRONE.md."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time

import bpy
from mathutils import Vector, Matrix
from bpy_extras.object_utils import world_to_camera_view


def material(name, color, metal=0, rough=.4, emission=0):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    p = mat.node_tree.nodes.get('Principled BSDF')
    for key, value in [('Base Color', (*color, 1)), ('Metallic', metal),
                       ('Roughness', rough), ('Emission Color', (*color, 1)),
                       ('Emission Strength', emission)]:
        p.inputs[key].default_value = value
    if metal > .5 and not emission:
        noise = mat.node_tree.nodes.new('ShaderNodeTexNoise')
        noise.inputs['Scale'].default_value = 165
        noise.inputs['Detail'].default_value = 2
        bump = mat.node_tree.nodes.new('ShaderNodeBump')
        bump.inputs['Strength'].default_value = .13
        bump.inputs['Distance'].default_value = .016
        mat.node_tree.links.new(noise.outputs['Fac'], bump.inputs['Height'])
        mat.node_tree.links.new(bump.outputs['Normal'], p.inputs['Normal'])
    return mat


def finish(obj, name, mat, bevel=0):
    obj.name = name
    obj.data.materials.append(mat)
    if bevel:
        mod = obj.modifiers.new('Machined edge radius', 'BEVEL')
        mod.width, mod.segments = bevel, 3
    if obj.type == 'MESH':
        for p in obj.data.polygons:
            p.use_smooth = True
    return obj


def ellipsoid(name, loc, scale, mat):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=20, location=loc)
    obj = bpy.context.object
    obj.scale = scale
    return finish(obj, name, mat)


def box(name, loc, dims, mat, bevel=.035):
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc)
    obj = bpy.context.object
    obj.dimensions = dims
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    finish(obj, name, mat, bevel)
    for p in obj.data.polygons:
        p.use_smooth = False
    return obj


def rod(name, a, b, radius, mat, vertices=20):
    a, b = Vector(a), Vector(b)
    bpy.ops.mesh.primitive_cylinder_add(vertices=vertices, radius=radius,
                                      depth=(b-a).length, location=(a+b)/2)
    obj = bpy.context.object
    obj.rotation_euler = (b-a).to_track_quat('Z', 'Y').to_euler()
    return finish(obj, name, mat, .012)


def cable(name, points, radius, mat):
    curve = bpy.data.curves.new(name, 'CURVE')
    curve.dimensions = '3D'
    curve.resolution_u = 12
    curve.bevel_depth, curve.bevel_resolution = radius, 3
    s = curve.splines.new('BEZIER')
    s.bezier_points.add(len(points)-1)
    for p, co in zip(s.bezier_points, points):
        p.co = co
        p.handle_left_type = p.handle_right_type = 'AUTO'
    obj = bpy.data.objects.new(name, curve)
    bpy.context.collection.objects.link(obj)
    obj.data.materials.append(mat)
    return obj


def plate(name, outline, depth, mat, bevel=.035):
    # An outline in world X/Z, extruded along depth Y.
    vertices = [(x, y, z) for y in depth for x, z in outline]
    n = len(outline)
    faces = [tuple(reversed(range(n))), tuple(range(n, 2*n))]
    faces += [(i, (i+1)%n, (i+1)%n+n, i+n) for i in range(n)]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return finish(obj, name, mat, bevel)


def ring(name, center, major, minor, mat, axis='Y'):
    bpy.ops.mesh.primitive_torus_add(major_radius=major, minor_radius=minor,
                                   major_segments=32, minor_segments=8, location=center)
    obj = bpy.context.object
    if axis == 'Y':
        obj.rotation_euler.x = math.pi/2
    return finish(obj, name, mat)


def area(name, pos, energy, color, size, target=(0, 0, 4.4)):
    light = bpy.data.lights.new(name, 'AREA')
    light.energy, light.color, light.size = energy, color, size
    obj = bpy.data.objects.new(name, light)
    bpy.context.collection.objects.link(obj)
    obj.location = pos
    obj.rotation_euler = (Vector(target)-obj.location).to_track_quat('-Z', 'Y').to_euler()


def insignia(path, center, width, height, mat):
    before = set(bpy.data.objects)
    bpy.ops.import_curve.svg(filepath=str(path))
    objects = list(set(bpy.data.objects)-before)
    bpy.context.view_layer.update()
    points = [o.matrix_world @ Vector(c) for o in objects for c in o.bound_box]
    lo = Vector(tuple(min(p[i] for p in points) for i in range(3)))
    hi = Vector(tuple(max(p[i] for p in points) for i in range(3)))
    scale = min(width/(hi.x-lo.x), height/(hi.y-lo.y))
    transform = Matrix.Translation(Vector(center)) @ Matrix.Rotation(math.pi/2, 4, 'X') @ Matrix.Scale(scale, 4) @ Matrix.Translation(-(lo+hi)/2)
    for obj in objects:
        obj.matrix_world = transform @ obj.matrix_world
        obj.name = 'Official OpenAI Blossom / source spline'
        obj.data.materials.clear()
        obj.data.materials.append(mat)
        obj.data.resolution_u = 24
    return objects


def build(opt):
    started = time.monotonic()
    repo = Path(__file__).resolve().parents[1]
    out = opt.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    armor = material('Obsidian titanium', (.032, .046, .049), .78, .39)
    edge = material('Brushed pale titanium', (.19, .24, .25), .82, .40)
    dark = material('Carbon elastomer', (.012, .02, .022), .18, .5)
    skin = material('Synthetic graphite dermis', (.13, .155, .15), .22, .57)
    inset = material('Recesses', (.006, .012, .014), .2, .6)
    green = material('Emerald conductor', (.035, .8, .12), .3, .28, 2.6)
    white = material('Official insignia white', (.9, .96, .94), .05, .42, .7)

    # Continuous anatomical substrate with tapered silhouette.
    ellipsoid('Thoracic understructure', (0, .07, 5.60), (1.04, .48, 1.05), dark)
    ellipsoid('Pelvic understructure', (0, .04, 3.95), (.68, .4, .58), dark)
    for s in (-1, 1):
        ellipsoid('Latissimus woven actuator', (s*.76, .11, 5.4), (.34, .35, .73), dark)
        # Layered pectoral shells, sculpted six-sided planes rather than boxes.
        plate('Pectoral ceramic titanium shell', [(s*x, z) for x, z in [(.10,6.35),(.84,6.37),(1.04,6.12),(.89,5.66),(.24,5.73),(.10,5.93)]], (-.49,-.23), armor)
        cable('Clavicle polished lip', [(s*.12,-.49,6.37),(s*.65,-.50,6.44),(s*1.05,-.25,6.25)], .036, edge)
        cable('Pectoral signal rail', [(s*.31,-.54,5.77),(s*.74,-.55,5.76),(s*.91,-.48,5.99)], .016, green)
        for i in range(4):
            z = 5.57-i*.25
            plate('Overlapping lateral rib armor', [(s*.29,z+.10),(s*.85,z+.17),(s*(.80-i*.045),z-.06),(s*.26,z-.13)], (-.40,-.14), armor)
            cable('Intercostal cable', [(s*.44,-.28,z),(s*.88,.0,z-.12),(s*.78,.33,z-.27)], .025, edge)
        # Shoulder socket and floating pauldron.
        ellipsoid('Shoulder articulation', (s*1.12,.03,6.03), (.4,.39,.43), edge)
        shell = ellipsoid('Floating deltoid armor', (s*1.28,-.005,6.18), (.48,.43,.43), armor)
        cable('Shoulder perimeter', [(s*.98,-.35,6.35),(s*1.32,-.42,6.43),(s*1.65,-.22,6.17)], .031, edge)
        plate('Angular deltoid face', [(s*x,z) for x,z in [(1.00,6.36),(1.31,6.52),(1.63,6.29),(1.59,5.98),(1.32,5.87),(1.13,6.02)]],(-.41,-.27),armor,.045)
        cable('Deltoid stepped panel seam',[(s*1.07,-.46,6.30),(s*1.32,-.47,6.39),(s*1.53,-.43,6.23)],.018,edge)
        for k in range(3):
            box('Shoulder thermal vent', (s*(1.13+k*.14),-.389,6.24), (.045,.035,.14), inset, .008)
        a,b = (s*1.37,.04,5.9),(s*1.53,-.015,4.94)
        rod('Humeral load strut',a,b,.19,edge)
        muscle = ellipsoid('Upper arm flexor', (s*1.44,-.08,5.45), (.29,.30,.56), dark)
        muscle.rotation_euler.y = -s*.13
        plate('Biceps armor', [(s*1.25,5.89),(s*1.59,5.80),(s*1.72,5.22),(s*1.45,5.03),(s*1.25,5.39)],(-.34,-.17),armor)
        cable('Arm signal', [(s*1.30,-.36,5.72),(s*1.45,-.39,5.38),(s*1.48,-.34,5.17)],.012,green)
        rod('Elbow transverse axle',(s*1.30,0,4.89),(s*1.77,0,4.89),.20,edge)
        ellipsoid('Elbow seal',(s*1.56,-.02,4.86),(.24,.25,.25),dark)

    # Camera-facing central provider plate is a planar rectangle, no perspective skew.
    box('Chest plate frame',(0,-.625,6.03),(1.03,.16,.81),edge,.065)
    box('Flat provider identification plate',(0,-.727,6.03),(.91,.045,.69),inset,.045)
    for x in (-.46,.46):
        for z in (5.70,6.36):
            rod('Chest fastener',(x,-.734,z),(x,-.76,z),.022,edge,12)
    # Central abdomen module and serial vertebrae, with exposed conduit loops.
    for i in range(6):
        z=5.55-i*.205
        box('Ventral segmented spine',(0,-.44,z),(.37,.24,.155),edge,.035)
        box('Spine signal window',(0,-.577,z),(.13,.023,.068),green,.01)
    for s in (-1,1):
        cable('Abdominal power umbilical',[(s*.21,-.33,5.68),(s*.34,-.56,5.08),(s*.26,-.41,4.43),(s*.55,-.21,4.17)],.052,dark)
        cable('Abdominal braided conductor',[(s*.29,-.36,5.66),(s*.43,-.48,5.1),(s*.39,-.36,4.49)],.024,edge)
    box('Memory core casing',(0,-.44,4.69),(.49,.28,.45),armor,.07)
    ring('Memory core port',(0,-.607,4.7),.135,.025,edge)
    rod('Memory green crystal',(0,-.62,4.7),(0,-.65,4.7),.096,green)

    # Neck rings, four exposed tendons and spinal trunk.
    rod('Neck core',(0,.015,6.37),(0,.015,7.03),.24,dark)
    for i in range(5):
        ring('Cervical vertebra',(0,.015,6.53+i*.10),.235,.034,edge,'Z')
    for s in (-1,1):
        cable('Sternomastoid cable',[(s*.60,-.09,6.37),(s*.33,-.25,6.62),(s*.27,-.23,7.1)],.057,dark)
        cable('Neck inner conduit',[(s*.42,-.20,6.43),(s*.23,-.31,6.77),(s*.25,-.28,7.15)],.023,edge)
        cable('Dorsal spinal cable',[(s*.27,.41,7.45),(s*.42,.43,6.63),(s*.57,.49,5.4),(s*.38,.42,4.12)],.05,dark)

    # Sculpted original head: joined/remeshed cranial, cheek, jaw and nose volumes.
    face_parts = []
    for name, loc, scale in [
        ('Cranium',(0,.035,7.49),(.435,.37,.60)),
        ('Jaw',(0,-.045,7.12),(.32,.30,.31)),
        ('Chin',(0,-.24,6.98),(.205,.14,.12)),
        ('Cheek left',(-.255,-.24,7.35),(.17,.14,.18)),
        ('Cheek right',(.255,-.24,7.35),(.17,.14,.18)),
        ('Nasal bridge',(0,-.334,7.40),(.073,.115,.205)),
        ('Nasal tip',(0,-.437,7.28),(.098,.08,.070)),
        ('Brow left',(-.18,-.30,7.60),(.20,.11,.075)),
        ('Brow right',(.18,-.30,7.60),(.20,.11,.075)),
    ]:
        face_parts.append(ellipsoid(name,loc,scale,skin))
    bpy.ops.object.select_all(action='DESELECT')
    for o in face_parts:
        o.select_set(True)
    bpy.context.view_layer.objects.active=face_parts[0]
    bpy.ops.object.join()
    head=bpy.context.object
    head.name='Original sculpted synthetic face'
    bpy.ops.object.transform_apply(location=False,rotation=False,scale=True)
    remesh=head.modifiers.new('Unified anatomical sculpt','REMESH')
    remesh.mode='VOXEL'
    remesh.voxel_size=.019
    bpy.ops.object.modifier_apply(modifier=remesh.name)
    smooth=head.modifiers.new('Dermal smoothing','SMOOTH')
    smooth.factor=.65
    smooth.iterations=4
    for s in (-1,1):
        ellipsoid('Orbital socket',(s*.18,-.355,7.50),(.13,.055,.065),inset)
        ellipsoid('Recessed eye',(s*.18,-.397,7.50),(.070,.018,.029),edge)
        ellipsoid('Iris',(s*.18,-.416,7.50),(.018,.009,.024),green)
        cable('Upper eyelid',[(s*.08,-.39,7.53),(s*.18,-.417,7.55),(s*.29,-.37,7.53)],.017,skin)
    cable('Closed mouth seam',[(-.15,-.315,7.12),(0,-.359,7.105),(.15,-.315,7.12)],.011,inset)
    cable('Lower lip',[(-.12,-.32,7.083),(0,-.359,7.071),(.12,-.32,7.083)],.018,skin)
    # Asymmetric orbital prosthesis: lens, stepped rim, temple capture module.
    plate('Orbital prosthetic shield',[(.02,7.71),(.36,7.76),(.48,7.54),(.34,7.29),(.11,7.34)],(-.36,-.24),armor,.035)
    rod('Optic housing',(.21,-.31,7.53),(.21,-.49,7.53),.143,edge)
    ring('Optic focus rim',(.21,-.5,7.53),.117,.026,dark)
    rod('Optic emerald lens',(.21,-.51,7.53),(.21,-.526,7.53),.086,green)
    box('Temple cortex module',(.43,-.02,7.60),(.18,.46,.39),armor,.045)
    for i in range(4):
        box('Temple memory fins',(.49,-.15+i*.082,7.67),(.10,.038,.24),edge,.01)
    cable('Temple conduit',[(.44,-.12,7.76),(.32,.05,8.02),(.05,.12,8.08),(-.20,.22,7.79)],.035,dark)
    cable('Cheek implant seam',[(.34,-.32,7.39),(.30,-.33,7.17),(.17,-.31,7.0)],.019,edge)
    ellipsoid('Left auricle',(-.425,.02,7.42),(.075,.12,.17),skin)

    # Recessed asymmetric cranial service panels, no decorative symbols.
    cable('Cranial interface seam',[(-.27,-.25,7.86),(-.18,-.33,7.77),(-.15,-.36,7.64)],.010,inset)
    for z in (7.72,7.79,7.86):
        box('Temporal heat sink',(.385,-.20,z),(.052,.045,.025),edge,.007)

    # One tool gauntlet, one dexterous hand. All fingers have three visible links.
    for s in (-1,1):
        wrist=(s*1.73,-.16,3.83)
        rod('Forearm radius',(s*1.57,0,4.77),wrist,.14,edge)
        ellipsoid('Forearm actuator',(s*1.66,-.015,4.32),(.25,.25,.48),dark)
        plate('Forearm dorsal armor',[(s*1.4,4.71),(s*1.77,4.63),(s*1.91,3.98),(s*1.6,3.83)],(-.32,-.16),armor)
        ring('Wrist seal',(s*1.73,-.14,3.86),.18,.034,edge,'Z')
        if s == -1:
            box('Tool arm modular chassis',(-1.71,-.34,4.32),(.44,.27,.70),armor,.07)
            for x in (-1.86,-1.70,-1.54):
                rod('Tool interchangeable chuck',(x,-.36,4.08),(x,-.4,3.86),.068,edge)
                rod('Tool probe',(x,-.4,3.88),(x,-.43,3.65),.027,edge)
            box('Tool interface emerald rail',(-1.71,-.488,4.45),(.23,.035,.055),green,.01)
            cable('Tool hydraulic hose',[(-1.64,.1,4.83),(-1.99,-.05,4.55),(-1.97,-.16,4.05)],.046,dark)
        box('Metacarpal armor',(s*1.76,-.13,3.61),(.33,.25,.37),armor,.075)
        for j in range(4):
            x=s*(1.62+j*.091)
            z=3.48-abs(j-1.4)*.025
            for k in range(3):
                a=(x,-.18-k*.025,z-k*.125)
                b=(x,-.205-k*.025,z-(k+1)*.125)
                rod('Articulated finger phalanx',a,b,.039,edge,12)
                ellipsoid('Finger flexion joint',a,(.047,.047,.047),dark)
        cable('Opposable thumb',[(s*1.59,-.11,3.65),(s*1.49,-.20,3.48),(s*1.50,-.28,3.32)],.057,edge)

    for s in (-1,1):
        # Pelvic shells and replaceable hip model cartridges.
        plate('Iliac armor',[(s*.05,4.25),(s*.59,4.39),(s*.83,4.04),(s*.67,3.62),(s*.18,3.69)],(-.4,-.12),armor)
        box('Hip model bay',(s*.63,-.42,4.04),(.35,.18,.43),edge,.055)
        for j in range(3):
            box('Model cartridge',(s*.63,-.53,3.91+j*.13),(.23,.065,.087),dark,.012)
            box('Cartridge status',(s*.55,-.568,3.91+j*.13),(.034,.018,.046),green,.006)
        hip,knee,ankle=(s*.49,.02,3.77),(s*.65,.025,2.12),(s*.72,.03,.55)
        rod('Femoral strut',hip,knee,.20,edge)
        leg=ellipsoid('Quadriceps actuator',(s*.57,.065,2.99),(.39,.35,.84),dark)
        plate('Thigh armored shell',[(s*.25,3.62),(s*.72,3.70),(s*.92,3.20),(s*.79,2.48),(s*.50,2.37),(s*.29,2.78)],(-.36,-.17),armor,.055)
        plate('Thigh inset panel',[(s*.40,3.43),(s*.67,3.50),(s*.79,3.12),(s*.69,2.61),(s*.54,2.58)],(-.411,-.36),edge,.025)
        for j in range(3):
            cable('Thigh service panel grooves',[(s*.48,-.449,3.29-j*.11),(s*.69,-.449,3.32-j*.11)],.011,inset)
        cable('Femoral light circuit',[(s*.32,-.405,3.46),(s*.37,-.434,2.98),(s*.52,-.426,2.51)],.013,green)
        rod('Knee axle',(s*.43,0,2.12),(s*.86,0,2.12),.20,edge)
        plate('Patella shield',[(s*.47,2.32),(s*.76,2.32),(s*.85,2.11),(s*.67,1.91),(s*.46,2.08)],(-.39,-.15),armor)
        rod('Tibial piston',knee,ankle,.15,edge)
        ellipsoid('Calf actuator',(s*.69,.14,1.43),(.28,.31,.63),dark)
        plate('Shin tapered carapace',[(s*.43,1.97),(s*.85,1.91),(s*.92,1.27),(s*.81,.60),(s*.60,.56),(s*.49,1.15)],(-.31,-.10),armor)
        cable('Tibial polished ridge',[(s*.66,-.38,1.87),(s*.73,-.39,1.32),(s*.73,-.32,.65)],.041,edge)
        cable('Shin signal rail',[(s*.52,-.32,1.80),(s*.55,-.36,1.38),(s*.64,-.31,.8)],.013,green)
        rod('External ankle actuator',(s*.92,.045,1.78),(s*.93,.04,.60),.045,edge)
        for i in range(3):
            ring('Ankle collar',(s*.73,.03,.57+i*.08),.18,.025,dark,'Z')
        box('Armored boot sole',(s*.74,-.20,.22),(.56,1.0,.19),dark,.075)
        ellipsoid('Armored boot upper',(s*.74,-.13,.36),(.29,.48,.22),armor)
        for j in range(3):
            box('Toe segmented plate',(s*.74,-.52+j*.14,.34+j*.023),(.50,.13,.11),edge,.025)
        # Functional seam fasteners, deliberately sparse.
        for z in (3.4,2.7,1.73,.85):
            rod('Armor seam captive screw',(s*(.70 if z>2 else .79),-.42,z),(s*(.70 if z>2 else .79),-.44,z),.022,inset,12)
    plate('Pelvic central guard',[(-.22,4.08),(.22,4.08),(.25,3.73),(0,3.49),(-.25,3.73)],(-.45,-.22),armor)

    marks=insignia(repo/'site/assets/providers/openai.svg',(0,-.758,6.03),.53,.53,white)
    scene=bpy.context.scene
    world=bpy.data.worlds.new('Neutral studio environment')
    world.use_nodes=True
    world.node_tree.nodes['Background'].inputs[0].default_value=(.12,.16,.18,1)
    world.node_tree.nodes['Background'].inputs[1].default_value=.3
    scene.world=world
    area('Large cool key',(-4,-6,10),850,(.72,.85,1),5)
    area('Soft frontal fill',(3,-5,5),240,(.64,.83,.79),4)
    area('Emerald edge',(3,2,6.4),950,(.12,1,.30),3)
    area('Cold crown rim',(-3,2,8.6),1100,(.6,.78,1),3)
    cam_data=bpy.data.cameras.new('Portrait orthographic camera')
    cam=bpy.data.objects.new('Portrait orthographic camera',cam_data)
    bpy.context.collection.objects.link(cam)
    cam.location=(0,-24,4.18)
    cam.rotation_euler=(Vector((0,0,4.18))-cam.location).to_track_quat('-Z','Y').to_euler()
    cam_data.type='ORTHO'
    cam_data.ortho_scale=8.9
    scene.camera=cam
    scene.render.engine='CYCLES'
    scene.cycles.device='CPU'
    scene.cycles.samples=min(32,max(1,opt.samples))
    scene.cycles.seed=20260916
    scene.cycles.use_denoising=True
    scene.render.threads_mode='FIXED'
    scene.render.threads=4
    scene.render.resolution_x=opt.width
    scene.render.resolution_y=round(opt.width*1600/1100)
    scene.render.resolution_percentage=100
    scene.render.film_transparent=True
    scene.render.image_settings.file_format='PNG'
    scene.render.image_settings.color_mode='RGBA'
    scene.view_settings.view_transform='AgX'
    scene.view_settings.look='AgX - Medium High Contrast'
    scene.view_settings.exposure=.1
    # No compositor is needed: restrained emissive materials preserve native alpha.
    bpy.context.view_layer.update()
    def xy(point):
        p=world_to_camera_view(scene,cam,Vector(point))
        return [round(p.x,6),round(1-p.y,6)]
    bounds=[xy(o.matrix_world @ Vector(c)) for o in scene.objects if o.type in ('MESH','CURVE') for c in o.bound_box]
    contract={
        'origin':'top-left; x right, y down; full uncropped 1100x1600 image',
        'cortex_temple':xy((.43,-.2,7.63)),
        'capture_optic':xy((.21,-.53,7.53)),
        'chest_plate':{'center':xy((0,-.76,6.03)), 'width':round(.91/(8.9*1100/1600),6), 'height':round(.69/8.9,6)},
        'central_spine_abdomen':xy((0,-.61,4.70)),
        'tool_forearm':xy((-1.71,-.49,4.32)),
        'model_bay_hip':xy((.63,-.57,4.04)),
        'figure_bounds_geometry':[round(min(p[0] for p in bounds),6),round(min(p[1] for p in bounds),6),round(max(p[0] for p in bounds),6),round(max(p[1] for p in bounds),6)],
    }
    (out/'coordinates.json').write_text(json.dumps(contract,indent=2)+'\n')
    receipt={'blender':bpy.app.version_string,'engine':'CYCLES','device':'CPU','threads':4,'samples':scene.cycles.samples,'dimensions':[scene.render.resolution_x,scene.render.resolution_y], 'alpha':True,'lights':sum(o.type=='LIGHT' for o in scene.objects),'objects':len(scene.objects),'mesh_objects':sum(o.type=='MESH' for o in scene.objects),'mesh_polygons':sum(len(o.data.polygons) for o in scene.objects if o.type=='MESH'),'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'inputs_sha256':{'site/assets/providers/openai.svg':hashlib.sha256((repo/'site/assets/providers/openai.svg').read_bytes()).hexdigest()},'coordinates':contract,'outputs_sha256':{}}
    bpy.ops.wm.save_as_mainfile(filepath=str(out/'borg-drone.blend'))
    variants=['codex'] if opt.preview else ['generic','codex']
    for variant in variants:
        for obj in marks:
            obj.hide_render=variant=='generic'
        path=out/('borg-drone-codex.png' if variant=='codex' else 'borg-drone.png')
        scene.render.filepath=str(path)
        bpy.ops.render.render(write_still=True)
        receipt['outputs_sha256'][path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
    receipt['outputs_sha256']['borg-drone.blend']=hashlib.sha256((out/'borg-drone.blend').read_bytes()).hexdigest()
    receipt['elapsed_seconds']=round(time.monotonic()-started,2)
    (out/'BUILD.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--width',type=int,default=1100)
    parser.add_argument('--samples',type=int,default=32)
    parser.add_argument('--preview',action='store_true')
    build(parser.parse_args(sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else []))
