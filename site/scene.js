/* Decorative hero. The Blender poster shows first. Where the browser draws transparent
   video, a Blender-rendered turn of the same ship takes over: its first frame is the
   poster, so nothing jumps and the ship keeps its rendered quality while it moves.
   Everywhere else, and for reduced motion or data saving, the poster simply stays. */
const hero = document.querySelector(".hero");
const stage = document.querySelector(".ship-stage");
const button = document.querySelector("#motion-toggle");
const fleet = document.querySelector("#fleet");
const fleetButton = document.querySelector("#fleet-motion-toggle");
const map = document.querySelector("#system");
const motionButtons = [button, fleetButton, document.querySelector("#profile-motion-toggle"), document.querySelector("#map-motion-toggle")].filter(Boolean);
const status = document.querySelector("#scene-status");
const motionPreference = window.matchMedia("(prefers-reduced-motion: reduce)");
const saveData = Boolean(navigator.connection && navigator.connection.saveData);
let paused = motionPreference.matches;
let visible = true;
let fleetVisible = false;
let mapVisible = false;
let pageLoaded = document.readyState === "complete";
let video = null;
let ready = false;
let failed = false;

function motionState() {
  const running = !paused && !document.hidden;
  document.documentElement.dataset.motion = running ? "running" : "paused";
  hero.dataset.motion = running && visible ? "running" : "paused";
  if (fleet) fleet.dataset.motion = running && fleetVisible ? "running" : "paused";
  if (map) map.dataset.motion = running && mapVisible ? "running" : "paused";
  status.textContent = paused
    ? (motionPreference.matches ? "Static view · reduced motion" : "Motion paused")
    : (failed ? "Static ship · stars in motion" : "Your collective. In orbit.");
  for (const control of motionButtons) {
    control.textContent = paused ? "Play motion" : "Pause motion";
    control.setAttribute("aria-pressed", String(!paused));
  }
  if (!video || !ready) return;
  if (running && visible) video.play().catch(() => {});
  else video.pause();
}

function fallback() {
  failed = true;
  ready = false;
  video?.remove();
  video = null;
  stage.dataset.state = "fallback";
  motionState();
}

// Some browsers play this video but drop its transparency, which would put a black square
// behind the ship. Read two pixels of the first frame: a clear corner and a solid centre.
function keepsTransparency(element) {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 32;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) return false;
  context.drawImage(element, 0, 0, 32, 32);
  const corner = context.getImageData(1, 1, 1, 1).data[3];
  const centre = context.getImageData(16, 16, 1, 1).data[3];
  return corner < 24 && centre > 200;
}

function initialize() {
  if (video || failed || saveData || !pageLoaded) return;
  const probe = document.createElement("video");
  if (!probe.canPlayType('video/webm; codecs="vp9"')) return fallback();
  video = probe;
  video.className = "ship-video";
  video.muted = true;
  video.loop = true;
  video.playsInline = true;
  video.preload = "auto";
  video.disablePictureInPicture = true;
  video.setAttribute("aria-hidden", "true");
  video.setAttribute("tabindex", "-1");
  const narrow = stage.getBoundingClientRect().width < 560;
  video.src = narrow ? "./assets/borg-ship-turn-720.webm" : "./assets/borg-ship-turn-960.webm";
  video.addEventListener("error", fallback, { once: true });
  video.addEventListener(
    "loadeddata",
    () => {
      try {
        if (!keepsTransparency(video)) return fallback();
      } catch {
        return fallback();
      }
      ready = true;
      stage.dataset.state = "ready";
      // Start turning once the identical first frame has fully faded in over the poster.
      setTimeout(motionState, 650);
    },
    { once: true },
  );
  stage.dataset.state = "loading";
  stage.append(video);
}

for (const control of motionButtons) {
  control.hidden = false;
  control.addEventListener("click", () => {
    paused = !paused;
    if (!paused && visible && !video) initialize();
    motionState();
  });
}
motionState();
motionPreference.addEventListener("change", (event) => {
  paused = event.matches;
  if (!paused && visible && !video) initialize();
  motionState();
});
document.addEventListener("visibilitychange", motionState);
const intersection = new IntersectionObserver(
  (entries) => {
    for (const entry of entries) {
      if (entry.target === hero) visible = entry.isIntersecting;
      else if (entry.target === map) mapVisible = entry.isIntersecting;
      else fleetVisible = entry.isIntersecting;
    }
    motionState();
    if (visible && !paused && !video) initialize();
  },
  { threshold: 0.05 },
);
intersection.observe(hero);
if (fleet) intersection.observe(fleet);
if (map) intersection.observe(map);
window.addEventListener("pagehide", () => {
  video?.pause();
  hero.dataset.motion = "paused";
  document.documentElement.dataset.motion = "paused";
  if (fleet) fleet.dataset.motion = "paused";
  if (map) map.dataset.motion = "paused";
});
window.addEventListener("pageshow", motionState);
// The poster and page come first; the video downloads after the page has loaded.
// Reduced-motion and data-saving visitors keep the poster and never download it.
window.addEventListener("load", () => {
  pageLoaded = true;
  if (!paused && visible) initialize();
});
if (pageLoaded && !paused) initialize();
