# Third-party notices

This file describes the public source distribution, not a prebuilt runtime.
The installer downloads dependencies from their publishers at setup time and
verifies the artifacts recorded in `installer/requirements.lock`,
`installer/npm/package-lock.json`, `installer/runtime-lock.json`, and
`installer/models.lock.json`, and `installer/browser-lock.json`. Optional MLX preparation uses
`installer/mlx-requirements.lock` and the full base-model/tokenizer pins in
`adapters/MANIFEST.json`. No proprietary provider runtime, provider
credential, owner account, private corpus, or installed service database is
bundled here.

The repository `LICENSE` applies to owner-authored BORG source and the three
BORG LoRA deltas. It does not replace the licenses of base models, downloaded
packages, binaries, embedded database engines, or source whose provenance is
called out below.

## Adapter and model notices

All three adapter deltas are MIT-licensed BORG artifacts. They require exact
MLX conversions of Qwen models and remain benched. The immutable revisions,
configuration checksums, model-file LFS SHA-256 values, and upstream model
lineage are in `adapters/MANIFEST.json`.

- `mlx-community/Qwen3-1.7B-4bit` and
  `mlx-community/Qwen3-4B-Instruct-2507-4bit`: Apache-2.0 model repositories,
  converted from the Qwen repositories identified in the manifest.
- `Qwen/Qwen3-1.7B` and `Qwen/Qwen3-4B-Instruct-2507`: Apache-2.0.
- `mlx-lm`, used optionally to load, train, and evaluate the adapters: MIT,
  <https://github.com/ml-explore/mlx-lm>.

The Qwen/MLX base weights are fetched separately and are not relicensed by
BORG. The adapter training pairs and learned historical memories are not part
of the distribution.

## Downloaded runtime components

The exact downloaded artifacts and integrity values are authoritative in the
installer locks. The principal runtime components are:

| Component | Pinned release | License | Official source |
|---|---:|---|---|
| Node.js | 24.21.0 | MIT | <https://github.com/nodejs/node> |
| OpenAI Codex CLI npm package | 0.146.0 | Apache-2.0 | <https://github.com/openai/codex> |
| uv | 0.12.13 | Apache-2.0 OR MIT | <https://github.com/astral-sh/uv> |
| Qdrant server | 1.19.1 | Apache-2.0 | <https://github.com/qdrant/qdrant> |
| Ollama | 0.34.0 | MIT | <https://github.com/ollama/ollama> |
| Beads (`bd`) | 1.2.2 | MIT | <https://github.com/gastownhall/beads> |
| cloudflared | 2026.9.1 | Apache-2.0 | <https://github.com/cloudflare/cloudflared> |

Ollama model blobs are separate artifacts governed by each model publisher's
terms. Their pinned registry manifests are recorded in
`installer/models.lock.json`.

Chromium 151.0.7922.34 (Playwright revision 1234) is fetched from the official
Playwright CDN, using Chrome for Testing for macOS and Linux x86-64 and the
Playwright Chromium build for Linux ARM64. The browser lock records exact URLs,
byte counts and SHA-256 values measured by this project from those archives.
These are project-maintained integrity pins, not publisher checksum claims.
The downloaded browser retains its bundled Chromium and third-party notices;
BORG does not redistribute the browser archives. Only full Chromium is installed.

### FalkorDB distinction

`falkordblite==0.10.0` is a New BSD/BSD-3-Clause Python wrapper
(<https://github.com/FalkorDB/falkordblite>). It packages or obtains the
FalkorDB database module; FalkorDB itself is licensed under the Server Side
Public License v1, not BSD or MIT
(<https://github.com/FalkorDB/FalkorDB>). The Python client
`falkordb==1.7.1` is MIT-licensed. Preserve the FalkorDB and FalkorDBLite
license files supplied by their installed distributions.

## Direct Python dependencies

The public bootstrap's direct pins are listed below. Transitive versions and
file hashes are in `installer/requirements.lock`; every installed wheel or
source distribution retains its own notices.

| Package | Pin | License reported by the official package metadata |
|---|---:|---|
| mem0ai | 2.0.18 | Apache-2.0 |
| graphiti-core | 0.29.3 | Apache-2.0 |
| fastmcp | 3.4.7 | Apache-2.0 |
| httpx | 0.28.1 | BSD-3-Clause |
| starlette | 1.6.0 | BSD-3-Clause |
| uvicorn | 0.52.4 | BSD-3-Clause |
| qdrant-client | 1.19.0 | Apache-2.0 |
| ollama (Python client) | 0.6.2 | MIT |
| openai (Python client) | 3.3.1 | Apache-2.0 |
| falkordblite | 0.10.0 | BSD-3-Clause wrapper; FalkorDB engine is SSPL-1.0 |
| websockets | 17.0.1 | BSD-3-Clause |
| requests | 2.34.2 | Apache-2.0 |
| zstandard | 0.25.0 | BSD-3-Clause |
| playwright | 1.62.0 | Apache-2.0 |

Optional training uses NumPy (BSD-3-Clause) and MLX/MLX-LM (MIT). Optional
fleet desktop integration uses `vncdotool==1.4.2` (MIT) and the fully pinned
closure in `coordination/requirements-optional-fleet.lock`; it is disabled by
default.

## Included source provenance boundaries

- `coordination/` carries its own `LICENSE`, `PROVENANCE.json`,
  `PROVENANCE.md`, and `THIRD_PARTY_NOTICES.md`. Its core is standard-library
  Python; Beads and the optional VNC integration are external.
- `conductor/THIRD_PARTY_NOTICES.md` describes the owner-developed conductor,
  router and provider orchestration source. Canonical commit and individual
  author metadata were not supplied; the notice does not assert a third-party
  origin. These BORG modules use the repository license. Separately installed
  provider runtimes retain their upstream licenses.
- No third-party source is intentionally vendored by the root installer.
  If a future release adds vendored code or binaries, add their exact notices
  before updating the publication inventory.

## Website fonts

The website includes Chakra Petch Regular and Bold from the
[Google Fonts Chakra Petch distribution](https://github.com/google/fonts/tree/main/ofl/chakrapetch).
Copyright 2018 The Chakra Petch Project Authors. Licensed under the SIL Open Font
License 1.1; the complete license is included at `site/assets/OFL.txt`.

## Website scene and provider identifiers

The cube ship is original procedural Blender artwork, distributed under BORG's
MIT license with its reproducible source in `art/`. The BORG mark is original
SVG artwork. No franchise mesh, image or texture is bundled.

The website vendors selected files from **Three.js 0.180.0 (r180)** under MIT,
including the upstream Meshopt decoder. Full notices are preserved in
`site/vendor/three/LICENSE` and `site/vendor/three/LICENSE.meshoptimizer`.
The local runtime's source URLs, version and integrity list are documented in
`site/vendor/three/README.md` and `SHA256SUMS` in that directory.

The OpenAI, Claude and Grok identifiers are sourced from their official public
websites to identify the integrations described beside them. They retain their
owners' trademark rights and are not relicensed under BORG's MIT grant.
Sources, original proportions and distributed hashes are recorded in
`site/assets/providers/README.md` and `SHA256SUMS` in that directory. Their use
does not imply a partnership or endorsement.
