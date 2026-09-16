# BORG character artwork

`borg_drone.py` authors an original biomechanical humanoid in Blender 5.2.1 LTS. The head is a unified voxel sculpt assembled from original anatomical volumes; layered armor, articulated limbs, finger links, conduits and service modules are procedural geometry. There are no downloaded meshes, external textures, franchise insignias, scene labels or generated-image assets.

The two transparent 1100 × 1600 portraits share the exact camera, body, materials and lighting. `borg-drone.webp` has a clean dark chest plate; `borg-drone-codex.webp` displays the official OpenAI Blossom imported from `site/assets/providers/openai.svg`. Source splines and proportions are preserved with uniform scale and a rigid plane rotation. The mark identifies the optional/native GPT/Codex integration; it does not imply endorsement. Provider marks retain their owners’ trademark rights and are excluded from the repository’s MIT license. See [provider provenance](../site/assets/providers/README.md). Original character geometry and authoring code use the repository MIT license.

## Reproduction

Run from the repository root with installed Blender and libwebp tools. No additional packages are required. Outputs under `evidence/drone/` are private build evidence; keep them outside published site artifacts.

```sh
/Applications/Blender.app/Contents/MacOS/Blender --background --threads 4 \
  --python art/borg_drone.py -- \
  --output evidence/drone/preview --width 550 --samples 12 --preview

/Applications/Blender.app/Contents/MacOS/Blender --background --threads 4 \
  --python art/borg_drone.py -- \
  --output evidence/drone/final --width 1100 --samples 32

cwebp -lossless -exact evidence/drone/final/borg-drone.png \
  -o site/assets/borg-drone.webp
cwebp -lossless -exact evidence/drone/final/borg-drone-codex.png \
  -o site/assets/borg-drone-codex.webp
```

Final rendering uses Cycles CPU, four threads, 32 samples, denoising, four area lights, AgX and transparent film. No compositor changes the alpha channel. The script caps samples at 32 and renders variants sequentially. `borg-drone.blend` retains editable scene geometry and the official spline objects. Its default scene has the insignia visible; hiding the objects named `Official OpenAI Blossom / source spline` produces the generic variant. `BUILD.json` records actual Blender version, geometry counts, settings, elapsed time, source/input hashes and PNG/blend output hashes. WebP hashes and visual inspection evidence are retained with the private release receipt.

## Image coordinate contract

Coordinates use the entire uncropped image, origin at top left, x increasing right and y increasing down. Apply the image’s rendered bounds to HTML/SVG overlays; preserve the 11:16 aspect ratio and use `object-fit: contain`, never cover/crop. Both variants use identical coordinates.

| Visible target | Normalized x | Normalized y |
|---|---:|---:|
| Cortex / temple implant | 0.570276 | 0.112360 |
| Capture optic | 0.534321 | 0.123596 |
| Chest plate center | 0.500000 | 0.292135 |
| Central spine / abdominal port | 0.500000 | 0.441573 |
| Tool forearm | 0.220531 | 0.484270 |
| Model cartridge bay / right image hip | 0.602962 | 0.515731 |

The frontal chest plate is parallel to the orthographic camera: normalized width **0.148723**, height **0.077528**. A centered provider logo should fit within **0.086619 × 0.059551**, preserving its own proportions. These dimensions are fractions of the image width and height, respectively, so the logo’s pixel box is square. Keep the surrounding plain plate visible.

Final nonzero-alpha bounding box `[left, top, right, bottom]`: **[0.160909, 0.055625, 0.818182, 0.956250]**, or pixel bounds **[177, 89, 900, 1530]** (right/bottom exclusive). Both variants have identical alpha. The script also writes `coordinates.json` directly from camera projection; its base-geometry bounds exclude the small edge bevel and antialiasing fringe. All anatomy fits inside the frame with room for external interface callouts.

The temple implant, optic, chest plate, abdominal port, forearm chassis and hip cartridges are visibly modeled parts. Their assignments to recall, capture, memory, tools and models/adapters are **schematic metaphors for software capabilities**, not physical components of a deployed computer or live system telemetry. Dorsal spine cabling is partly occluded in the frontal view; the abdominal spine is visible. Real accessible labels, selection states, motion and source-backed explanations belong in the website interface.
