/* Decorative local scene. Page content and the Blender poster work independently. */
const hero = document.querySelector(".hero");
const stage = document.querySelector(".ship-stage");
const button = document.querySelector("#motion-toggle");
const fleet = document.querySelector("#fleet");
const fleetButton = document.querySelector("#fleet-motion-toggle");
const motionButtons = [button, fleetButton, document.querySelector("#profile-motion-toggle")].filter(Boolean);
const status = document.querySelector("#scene-status");
const motionPreference = window.matchMedia("(prefers-reduced-motion: reduce)");
let paused = motionPreference.matches;
let visible = true;
let fleetVisible = false;
let loading = false;
let failed = false;
let renderer, scene, camera, ship, stars, environment;
let frame = 0;
let previous = 0;
let elapsed = 0;
let lastRender = 0;
let THREE;

function motionState() {
  const running = !paused && !document.hidden;
  document.documentElement.dataset.motion = running ? "running" : "paused";
  hero.dataset.motion = running && visible ? "running" : "paused";
  if (fleet) fleet.dataset.motion = running && fleetVisible ? "running" : "paused";
  status.textContent = paused
    ? (motionPreference.matches ? "Static view · reduced motion" : "Motion paused")
    : (failed ? "Static ship · stars in motion" : "Your collective. In orbit.");
  for (const control of motionButtons) {
    control.textContent = paused ? "Play motion" : "Pause motion";
    control.setAttribute("aria-pressed", String(!paused));
  }
  cancelAnimationFrame(frame);
  frame = 0;
  previous = 0;
  if (running && visible && ship && !failed) frame = requestAnimationFrame(animate);
}

function render() {
  if (renderer && ship && !failed) renderer.render(scene, camera);
}

function animate(now) {
  if (paused || !visible || document.hidden || failed) return;
  frame = requestAnimationFrame(animate);
  if (now - lastRender < 1000 / 30) return;
  const delta = previous ? Math.min((now - previous) / 1000, 0.1) : 0;
  elapsed += delta;
  previous = now;
  lastRender = now;
  ship.rotation.y = -0.18 + elapsed * 0.055;
  ship.rotation.z = Math.sin(elapsed * 0.18) * 0.025;
  ship.position.y = Math.sin(elapsed * 0.55) * 0.16;
  stars.rotation.y = elapsed * 0.008;
  stars.rotation.z = elapsed * 0.002;
  render();
}

function resize() {
  if (!renderer || failed) return;
  const { width, height } = stage.getBoundingClientRect();
  if (!width || !height) return;
  renderer.setPixelRatio(
    Math.min(window.devicePixelRatio || 1, width < 600 ? 1.25 : 1.5),
  );
  renderer.setSize(width, height, false);
  camera.aspect = width / height;
  // Keep the entire rotating cube inside the canvas at narrow aspect ratios.
  const distance = 17 * Math.max(1, 0.95 / camera.aspect);
  camera.position.set(0.66, 0.57, 0.76).normalize().multiplyScalar(distance);
  camera.updateProjectionMatrix();
  camera.lookAt(0, 0, 0);
  render();
}

function disposeScene() {
  if (scene)
    scene.traverse((object) => {
      object.geometry?.dispose();
      const materials = object.material
        ? Array.isArray(object.material)
          ? object.material
          : [object.material]
        : [];
      for (const material of materials) {
        for (const value of Object.values(material))
          if (value?.isTexture) value.dispose();
        material.dispose();
      }
    });
  environment?.dispose();
  renderer?.dispose();
  renderer?.domElement.remove();
  renderer = undefined;
  ship = undefined;
}

function fallback() {
  failed = true;
  disposeScene();
  stage.dataset.state = "fallback";
  // Background stars and their controls work even without the 3D renderer.
  motionState();
}

async function initialize() {
  if (loading || ship || failed) return;
  loading = true;
  stage.dataset.state = "loading";
  try {
    const modules = await Promise.all([
      import("three"),
      import("./vendor/three/loaders/GLTFLoader.js"),
      import("./vendor/three/environments/RoomEnvironment.js"),
      import("./vendor/three/libs/meshopt_decoder.module.js"),
    ]);
    THREE = modules[0];
    const { GLTFLoader } = modules[1];
    const { RoomEnvironment } = modules[2];
    renderer = new THREE.WebGLRenderer({
      alpha: true,
      antialias: true,
      powerPreference: "low-power",
    });
    renderer.setClearColor(0x000000, 0);
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 0.95;
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    const canvas = renderer.domElement;
    canvas.setAttribute("aria-hidden", "true");
    canvas.className = "ship-canvas";
    canvas.addEventListener(
      "webglcontextlost",
      (event) => {
        event.preventDefault();
        fallback();
      },
      { once: true },
    );
    stage.append(canvas);
    scene = new THREE.Scene();
    camera = new THREE.PerspectiveCamera(35, 1, 0.1, 100);
    const pmrem = new THREE.PMREMGenerator(renderer);
    const room = new RoomEnvironment();
    environment = pmrem.fromScene(room, 0.04);
    scene.environment = environment.texture;
    scene.environmentIntensity = 0.23;
    room.dispose();
    pmrem.dispose();
    scene.add(new THREE.HemisphereLight(0xc5d5d0, 0x11190c, 0.85));
    const key = new THREE.DirectionalLight(0xe0eee9, 3.2);
    key.position.set(-7, 10, 9);
    key.castShadow = true;
    key.shadow.mapSize.set(1024, 1024);
    Object.assign(key.shadow.camera, {
      left: -6,
      right: 6,
      top: 6,
      bottom: -6,
      near: 0.5,
      far: 40,
    });
    key.shadow.normalBias = 0.012;
    key.shadow.bias = -0.00002;
    scene.add(key);
    const rim = new THREE.DirectionalLight(0xa8ff59, 1.7);
    rim.position.set(6, 2, -4);
    scene.add(rim);
    const fill = new THREE.DirectionalLight(0x88a5c5, 0.65);
    fill.position.set(2, -3, 8);
    scene.add(fill);

    // One small point cloud. No per-star objects, textures, or per-frame allocation.
    const positions = new Float32Array(180 * 3);
    let seed = 431;
    const random = () => {
      seed = (seed * 16807) % 2147483647;
      return seed / 2147483647;
    };
    for (let i = 0; i < positions.length; i += 3) {
      positions[i] = (random() - 0.5) * 40;
      positions[i + 1] = (random() - 0.5) * 32;
      positions[i + 2] = -10 - random() * 15;
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    stars = new THREE.Points(
      geometry,
      new THREE.PointsMaterial({
        color: 0xb1bdcb,
        size: 0.026,
        transparent: true,
        opacity: 0.65,
        depthWrite: false,
      }),
    );
    scene.add(stars);

    const loader = new GLTFLoader();
    loader.setMeshoptDecoder(modules[3].MeshoptDecoder);
    const model = await loader.loadAsync(
      new URL("./assets/borg-ship.glb", import.meta.url).href,
    );
    if (failed) return;
    model.scene.traverse((object) => {
      if (!object.isMesh) return;
      object.castShadow = true;
      object.receiveShadow = true;
      // Preserve Blender materials; adapt emissive exposure to the browser's tone mapper.
      const materials = Array.isArray(object.material)
        ? object.material
        : [object.material];
      for (const material of materials) {
        if (material.emissive?.getHex() > 0) {
          material.emissiveIntensity *= 0.45;
          material.toneMapped = false;
        }
      }
    });
    const bounds = new THREE.Box3().setFromObject(model.scene);
    const size = bounds.getSize(new THREE.Vector3());
    const center = bounds.getCenter(new THREE.Vector3());
    const maxSize = Math.max(size.x, size.y, size.z);
    if (!Number.isFinite(maxSize) || maxSize <= 0)
      throw new Error("Invalid ship geometry");
    model.scene.position.sub(center);
    const modelScale = new THREE.Group();
    modelScale.add(model.scene);
    modelScale.scale.setScalar(6 / maxSize);
    ship = new THREE.Group();
    ship.add(modelScale);
    ship.rotation.y = -0.18;
    scene.add(ship);
    resize();
    stage.dataset.state = "ready";
    status.textContent = "Your collective. In orbit.";
    motionState();
  } catch {
    fallback();
  } finally {
    loading = false;
  }
}

for (const control of motionButtons) {
  control.hidden = false;
  control.addEventListener("click", () => {
    paused = !paused;
    if (!paused && visible && !ship) initialize();
    motionState();
  });
}
motionState();
motionPreference.addEventListener("change", (event) => {
  paused = event.matches;
  if (!paused && visible && !ship) initialize();
  motionState();
});
document.addEventListener("visibilitychange", motionState);
const intersection = new IntersectionObserver(
  (entries) => {
    for (const entry of entries) {
      if (entry.target === hero) visible = entry.isIntersecting;
      else fleetVisible = entry.isIntersecting;
    }
    motionState();
    if (visible && !paused && !ship) initialize();
  },
  { threshold: 0.05 },
);
intersection.observe(hero);
if (fleet) intersection.observe(fleet);
const dimensions = new ResizeObserver(resize);
dimensions.observe(stage);
window.addEventListener("pagehide", () => {
  cancelAnimationFrame(frame);
  hero.dataset.motion = "paused";
  document.documentElement.dataset.motion = "paused";
  if (fleet) fleet.dataset.motion = "paused";
});
window.addEventListener("pageshow", motionState);
// Reduced-motion users start with the poster and do not download Three.js or GLB.
if (!paused) initialize();
