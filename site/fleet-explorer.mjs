import { profiles } from './fleet-profiles.mjs';

const dialog = document.querySelector('#node-profile');
const select = (selector) => dialog.querySelector(selector);
const text = (selector, value) => { select(selector).textContent = value; };
const svgNS = 'http://www.w3.org/2000/svg';
const bodyLayout = {
  cortex: { x: 57.028, y: 11.236, labelX: 19, labelY: 15 },
  core: { x: 50, y: 29.214, labelX: 83, labelY: 27 },
  spine: { x: 50, y: 44.157, labelX: 83, labelY: 44 },
  tools: { x: 22.053, y: 48.427, labelX: 13, labelY: 56 },
  models: { x: 60.296, y: 51.573, labelX: 83, labelY: 68 },
};
const flowPositions = [[100, 50], [300, 50], [500, 50], [500, 180], [300, 180], [100, 180]];
let current;
let selectedStep = 0;
let opener;
let priorOverflow = '';
let timer;

function el(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}

function svg(tag, attributes) {
  const node = document.createElementNS(svgNS, tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
  return node;
}

function setStep(index) {
  selectedStep = index;
  select('.profile-flow-nodes').querySelectorAll('button').forEach((button, i) => {
    if (i === index) button.setAttribute('aria-current', 'step');
    else button.removeAttribute('aria-current');
  });
  text('#flow-detail', current.flow[index].detail);
}

function syncMotion() {
  clearInterval(timer);
  timer = undefined;
  if (dialog.open && !document.hidden && document.documentElement.dataset.motion === 'running') {
    timer = setInterval(() => setStep((selectedStep + 1) % current.flow.length), 4200);
  }
}

function setPart(index) {
  const part = current.parts[index];
  for (const button of dialog.querySelectorAll('[data-part]')) {
    button.setAttribute('aria-pressed', String(Number(button.dataset.part) === index));
  }
  for (const wire of dialog.querySelectorAll('[data-wire]')) {
    wire.dataset.active = String(Number(wire.dataset.wire) === index);
  }
  text('#component-coordinate', `${String(index + 1).padStart(2, '0')} / ${part.anatomy.toUpperCase()}`);
  text('#component-status', part.status);
  text('#component-name', part.name);
  text('#component-description', part.description);
  select('#component-stack').replaceChildren(...part.stack.map((item) => el('span', '', item)));
  select('#component-source').href = part.source;
  const step = current.flow.findIndex((item) => item.part === part.key);
  setStep(step >= 0 ? step : 0);
  syncMotion();
}

function renderBody() {
  const points = select('.profile-body-points');
  const parts = select('.profile-parts');
  const circuits = select('.profile-circuits');
  points.replaceChildren();
  parts.replaceChildren();
  circuits.replaceChildren();
  current.parts.forEach((part, index) => {
    const point = bodyLayout[part.key];
    const number = String(index + 1).padStart(2, '0');
    const hotspot = el('button', 'body-point', number);
    hotspot.type = 'button';
    hotspot.dataset.part = index;
    hotspot.style.left = `${point.labelX}%`;
    hotspot.style.top = `${point.labelY}%`;
    hotspot.setAttribute('aria-label', `Inspect ${part.anatomy}: ${part.name}`);
    hotspot.title = part.name;
    points.append(hotspot);
    const choice = el('button', '', part.anatomy);
    choice.type = 'button';
    choice.dataset.part = index;
    choice.prepend(el('span', '', number));
    parts.append(choice);
    const y = point.y * 16 / 11;
    const labelY = point.labelY * 16 / 11;
    const elbow = point.labelX < 50 ? point.x - 8 : point.x + 8;
    circuits.append(
      svg('path', { d: `M ${point.labelX} ${labelY} H ${elbow} L ${point.x} ${y}`, 'data-wire': index }),
      svg('circle', { cx: point.x, cy: y, r: .9, 'data-wire': index }),
    );
  });
}

function renderFlow() {
  const nodes = select('.profile-flow-nodes');
  nodes.replaceChildren();
  const positions = flowPositions.slice(0, current.flow.length);
  let rail = `M ${positions[0][0]} ${positions[0][1]}`;
  positions.slice(1).forEach(([x, y]) => { rail += ` L ${x} ${y}`; });
  for (const path of dialog.querySelectorAll('.profile-flow-map path')) path.setAttribute('d', rail);
  current.flow.forEach((step, index) => {
    const button = el('button', 'flow-node', step.label);
    button.type = 'button';
    button.dataset.step = index;
    button.style.setProperty('--x', `${positions[index][0] / 6}%`);
    button.style.setProperty('--y', `${positions[index][1] / 2.3}%`);
    button.prepend(el('span', '', String(index + 1).padStart(2, '0')));
    nodes.append(button);
  });
}

function renderProfile(id) {
  current = profiles.find((profile) => profile.id === id);
  dialog.dataset.unit = id;
  text('.profile-unit-code', `UNIT / ${current.number}`);
  text('#profile-role', current.role);
  text('#profile-name', current.name);
  text('#profile-summary', current.summary);
  select('.profile-tags').replaceChildren(...current.tags.map((tag) => el('span', '', tag)));
  for (const button of dialog.querySelectorAll('[data-unit]')) button.setAttribute('aria-pressed', String(button.dataset.unit === id));
  const image = select('#profile-character');
  image.src = id === 'codex' ? './assets/borg-drone-codex.webp' : './assets/borg-drone.webp';
  image.alt = id === 'codex'
    ? 'Original biomechanical BORG character with the OpenAI insignia on its chest; numbered callouts map its body to software components.'
    : `Original biomechanical BORG character representing ${current.name}, with numbered software component callouts.`;
  const mark = select('#profile-chest-mark');
  mark.hidden = id === 'codex';
  if (id !== 'codex') {
    mark.src = current.mark;
    Object.assign(mark.style, { left: '45.669%', top: '26.236%', width: '8.662%', height: '5.955%' });
  }
  select('.profile-boundaries').replaceChildren(...current.boundaries.map((item) => el('p', '', item)));
  select('#profile-setup').href = current.setup;
  renderBody();
  renderFlow();
  setPart(0);
}

function openProfile(id, trigger) {
  if (typeof dialog.showModal !== 'function') return false;
  renderProfile(id);
  if (!dialog.open) {
    opener = trigger;
    priorOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    dialog.showModal();
    select('.profile-close').focus();
  }
  dialog.scrollTop = 0;
  syncMotion();
  return true;
}

for (const link of document.querySelectorAll('a[data-profile]')) {
  link.setAttribute('aria-haspopup', 'dialog');
  link.addEventListener('click', (event) => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    if (openProfile(link.dataset.profile, link)) event.preventDefault();
  });
}

dialog.addEventListener('click', (event) => {
  const button = event.target.closest('button');
  if (button?.matches('.profile-close')) dialog.close();
  else if (button?.dataset.unit) renderProfile(button.dataset.unit);
  else if (button?.dataset.part !== undefined) setPart(Number(button.dataset.part));
  else if (button?.dataset.step !== undefined) {
    setStep(Number(button.dataset.step));
    syncMotion();
  } else if (event.target === dialog) {
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
  }
});
dialog.addEventListener('close', () => {
  clearInterval(timer);
  timer = undefined;
  document.body.style.overflow = priorOverflow;
  if (opener?.isConnected) opener.focus({ preventScroll: true });
});
new MutationObserver(syncMotion).observe(document.documentElement, { attributes: true, attributeFilter: ['data-motion'] });
document.addEventListener('visibilitychange', syncMotion);
window.addEventListener('pagehide', () => clearInterval(timer));
window.addEventListener('pageshow', syncMotion);
