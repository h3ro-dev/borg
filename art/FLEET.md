# BORG fleet artwork

An original, five-vessel Blender composition illustrates logical roles in BORG.
The large central cube represents owner-controlled memory and context. Three
provider vessels identify GPT/Codex, Claude and Grok. A fifth vessel represents
native tools and explicitly enrolled remote nodes. Green lines are conceptual
connections, not a diagram of a running installation or automatic memory sharing.
Each host's memory remains private to that host unless the owner explicitly
configures otherwise. Provider integrations require the owner's setup and
credentials. LoRAs remain inactive research and are not depicted as active ships.

## Rebuild

Use Blender 5.2.1 LTS and libwebp's `cwebp`, from a repository checkout:

```sh
blender --background --threads 4 --python art/borg_fleet.py -- \
  --output /tmp/borg-fleet-preview --width 960 --samples 16
blender --background --threads 4 --python art/borg_fleet.py -- \
  --output /tmp/borg-fleet-final --width 2200 --samples 32
cwebp -q 88 -m 6 /tmp/borg-fleet-final/borg-fleet.png \
  -o /tmp/borg-fleet-final/borg-fleet.webp
```

The script imports the existing original `site/assets/borg-ship.glb` and the
three existing `site/assets/providers/*.svg` files. It creates linked geometry
instances, lights, editable SVG curves, editable labels, fine stars, a camera
and a restrained bloom compositor. The output directory receives the editable
`borg-fleet.blend`, PNG and `BLENDER-FLEET-BUILD.json` input hashes and counts.
The camera is orthographic; dimensions are 2200 × 1375 (16:10). Cycles uses CPU,
four threads, 32 samples and denoising. The script caps samples at 48. No external
texture, generative image service, franchise mesh or private installation data
is used. Save working scenes and render logs outside the public artifact.

Review the final WebP before copying it to `site/assets/borg-fleet.webp`.
The integrating release must update its asset pins and inventory. The seeded
composition is reproducible; byte-for-byte renders can vary with Blender builds
and denoising implementations. Use the published binary's hash for integrity.

## Published still

- File: `site/assets/borg-fleet.webp`
- Dimensions: 2200 × 1375 pixels
- Length: 234,470 bytes
- SHA256: `f7d0fd992b3fa0eb8f5d7e30cc89d2644f38ae5f7e7a68fb1517686641351cca`
- Authoring: Blender 5.2.1 LTS, build `9e2066aef7ef`, Cycles CPU, four threads,
  32 samples with denoising; final render time 2 minutes 1.925 seconds.
- Scene: five vessels, 88 objects, 45 mesh objects, 727,834 mesh polygons counting
  instances. The reused ship contains 12,121 cuboid details before instancing.

The final compressed still and both small previews were visually inspected.
The editable scene was reopened and its dimensions, object count, CPU thread
limit and sample count verified. No external image dependencies are required.

## Layout and accessible role legend

Coordinates are percentages of the full image, measured from the top left,
and identify vessel centers. Keep the image contained at its full aspect ratio
on mobile; cropping loses roles. Supply the legend as real HTML text.

| Vessel | X | Y | Logical role |
| --- | ---: | ---: | --- |
| BORG memory core | 50.0% | 48.8% | Owner-controlled memory and context |
| OpenAI GPT/Codex | 18.8% | 27.9% | Agent runtime; owner authentication required |
| Claude | 80.7% | 28.8% | Agent runtime; owner authentication required |
| Grok | 78.1% | 72.1% | Provider adapter; owner configuration required |
| Tools / nodes | 20.1% | 73.3% | Native tools and explicitly enrolled remote nodes |

Suggested alt text: “Five illuminated cube ships in deep space: a large central
BORG memory and context core linked to smaller GPT/Codex, Claude, Grok, and
tools/remote-node vessels, with white provider identifiers on dark plates.”

## Provenance and license exception

The hull geometry was authored by `art/borg_ship.py`; its provenance is documented
in [README.md](README.md). The new layout, scene script and original geometry use
the repository's MIT license. Provider identifiers embedded in the rendered
image retain their owners' trademark rights and are **excluded from that MIT
grant**. Their inclusion identifies integrations and implies no partnership or
endorsement.

The existing approved SVGs are imported without editing source paths or changing
their proportions. Uniformly scaled white marks sit on plain dark identification
plates. The source assets and their hashes remain unchanged. Recorded provenance
is in [the provider asset notes](../site/assets/providers/README.md): OpenAI's
Blossom from its official developer website, Claude's official wordmark from
its website, and Grok's official mark and wordmark from its website. See those
notes and [the third-party notices](../THIRD_PARTY_NOTICES.md) for source links
and the provider license exception.
