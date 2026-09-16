# Local Three.js runtime

Pinned upstream: `three@0.180.0` / Three.js r180.
Source package: https://registry.npmjs.org/three/-/three-0.180.0.tgz
Project: https://github.com/mrdoob/three.js/tree/r180
Documentation: https://threejs.org/docs/
License: MIT, preserved in `LICENSE`.

Unmodified package files:

- `build/three.module.min.js` → `three.module.min.js`
- `build/three.core.min.js` → `three.core.min.js`
- `examples/jsm/loaders/GLTFLoader.js` → `loaders/GLTFLoader.js`
- `examples/jsm/utils/BufferGeometryUtils.js` → `utils/BufferGeometryUtils.js`
- `examples/jsm/environments/RoomEnvironment.js` → `environments/RoomEnvironment.js`
- `examples/jsm/libs/meshopt_decoder.module.js` → `libs/meshopt_decoder.module.js`

An HTML import map resolves `three` to the local module. All module imports and the GLB are local. Meshopt decoding is bundled locally (inline WebAssembly; no decoder network requests). The room environment is generated once in memory, with no external texture. No CDN, Draco decoder, runtime package installation, or postprocessing pipeline is used. The bundled decoder identifies meshoptimizer 0.22 and retains its upstream MIT copyright header. Its full license is preserved in `LICENSE.meshoptimizer`, retrieved from https://raw.githubusercontent.com/zeux/meshoptimizer/v0.22/LICENSE.md . See https://github.com/zeux/meshoptimizer . A pinned known release is deliberate; this is not a claim to be the latest version.

`SHA256SUMS` records distributed upstream bytes. Package archive integrity and exact source URLs are in the frontend delivery evidence.

Archive SHA-256: `ad66d724565ee29a2467277fa84daa5ed0211d6b8d446e9ef29f6bae0cd14144`. All seven package files, including the Three.js license, were byte-compared with the official archive.
