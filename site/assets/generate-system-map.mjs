#!/usr/bin/env node
// Generates the isometric deck art for the home page system map (inline SVG in index.html,
// between the system-map:art markers) plus the hotspot positions. Deterministic, no dependencies.
//   node site/assets/generate-system-map.mjs          rewrite the block in index.html
//   node site/assets/generate-system-map.mjs --check  exit 1 if the block is out of date
import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const W = 1000;
const H = 800;
const C = Math.cos(Math.PI / 6);
const S = 0.5;
const r = value => Math.round(value * 10) / 10;
const pt = ([x, y]) => `${r(x)},${r(y)}`;
let seed = 7331;
const random = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);

// Parts: footprint centre on the deck (x, y), edge length e in px, hover height z.
export const NODES = [
  { id: 'fleet', x: 176, y: 440, e: 0, z: 0, cluster: true },
  { id: 'incoming', x: 332, y: 338, e: 60, z: 0 },
  { id: 'packets', x: 332, y: 566, e: 60, z: 0 },
  { id: 'jev', x: 500, y: 452, e: 96, z: 0 },
  { id: 'dreaming', x: 668, y: 338, e: 60, z: 0 },
  { id: 'recall', x: 668, y: 566, e: 60, z: 0 },
  { id: 'cache', x: 500, y: 662, e: 52, z: 0 },
  { id: 'memory', x: 812, y: 446, e: 138, z: 0 },
  { id: 'judge', x: 372, y: 214, e: 68, z: 46 },
  { id: 'deepdream', x: 800, y: 196, e: 68, z: 46 },
  { id: 'log', x: 880, y: 690, e: 40, z: 0 },
];
const byId = Object.fromEntries(NODES.map(node => [node.id, node]));

// Conduits follow "How it fits together": [id, from, to, steps, bend]
export const CONDUITS = [
  ['fleet-incoming', 'fleet', 'incoming', '1', 0.10],
  ['incoming-memory', 'incoming', 'memory', '1', -0.16],
  ['jev-packets', 'jev', 'packets', '2', 0.0],
  ['packets-fleet', 'packets', 'fleet', '2', 0.12],
  ['memory-recall', 'memory', 'recall', '2', 0.08],
  ['recall-fleet', 'recall', 'fleet', '2', 0.22],
  ['memory-dreaming', 'memory', 'dreaming', '3', 0.14],
  ['deepdream-memory', 'deepdream', 'memory', '3', 0.1],
  ['cache-fleet', 'cache', 'fleet', '4', -0.18],
  ['judge-fleet', 'judge', 'fleet', '5', 0.14],
  ['judge-jev', 'judge', 'jev', '5', -0.1],
  ['memory-log', 'memory', 'log', '6', -0.12],
];
// JEV's own spokes to each lane light up whenever that lane is part of a step.
const SPOKES = [['incoming', '1'], ['packets', '2'], ['recall', '2'], ['dreaming', '3'], ['cache', '4']];

function cubeFaces(cx, cy, e, z = 0) {
  const u = [e / 2 * C, e / 2 * S];
  const v = [-e / 2 * C, e / 2 * S];
  const at = (a, b, h) => [cx + a * u[0] + b * v[0], cy - z + a * u[1] + b * v[1] - h];
  const h = e;
  const T = { back: at(-1, -1, h), right: at(1, -1, h), front: at(1, 1, h), left: at(-1, 1, h) };
  const B = { back: at(-1, -1, 0), right: at(1, -1, 0), front: at(1, 1, 0), left: at(-1, 1, 0) };
  return {
    T, B,
    top: [T.back, T.right, T.front, T.left],
    left: [T.left, T.front, B.front, B.left],
    right: [T.front, T.right, B.right, B.front],
    footprint: [B.back, B.right, B.front, B.left],
  };
}

// Affine map of the unit square onto a face (origin, u-axis end, v-axis end) for greeble panels.
function faceMatrix(o, a, b) {
  return `matrix(${r(a[0] - o[0])} ${r(a[1] - o[1])} ${r(b[0] - o[0])} ${r(b[1] - o[1])} ${r(o[0])} ${r(o[1])})`;
}

function greebles(variant, rows = 5, cols = 5) {
  seed = 97 + variant * 131;
  const cells = [];
  const q = value => Math.round(value * 1000) / 1000;
  for (let row = 0; row < rows; row++) {
    for (let col = 0; col < cols; col++) {
      if (random() < 0.22) continue;
      const w = random() < 0.3 ? 2 : 1;
      if (col + w > cols) continue;
      const x = col / cols + 0.018;
      const y = row / rows + 0.018;
      const shade = ['#2c3925', '#1e2819', '#34432c', '#151c12'][Math.floor(random() * 4)];
      cells.push(`<rect x="${q(x)}" y="${q(y)}" width="${q(w / cols - 0.036)}" height="${q(1 / rows - 0.036)}" fill="${shade}"/>`);
      if (random() < 0.34) {
        const lx = x + 0.02 + random() * (w / cols - 0.12);
        cells.push(`<rect class="lit" x="${q(lx)}" y="${q(y + 0.06 + random() * 0.06)}" width="0.07" height="0.024"/>`);
      }
    }
  }
  return cells.join('');
}

function cube(node, { mini = false, held = false } = {}) {
  const f = cubeFaces(node.x, node.y, node.e, node.z || 0);
  const v = Math.round(node.x + node.y) % 3;
  const faces = [
    ['top', f.top, faceMatrix(f.T.left, f.T.back, f.T.front), `g-top-${v % 2}`],
    ['left', f.left, faceMatrix(f.T.left, f.T.front, f.B.left), `g-side-${v}`],
    ['right', f.right, faceMatrix(f.T.front, f.T.right, f.B.front), `g-side-${(v + 1) % 3}`],
  ];
  const body = faces.map(([name, poly, matrix, symbol]) =>
    `<polygon class="face ${name}" points="${poly.map(pt).join(' ')}"/>`
    + (mini ? '' : `<use href="#${symbol}" width="1" height="1" transform="${matrix}"/>`)).join('');
  const { T, B } = f;
  const edges = `<path class="edge" d="M${pt(T.back)}L${pt(T.right)}L${pt(T.front)}L${pt(T.left)}Z M${pt(T.front)}L${pt(B.front)} M${pt(T.right)}L${pt(B.right)}L${pt(B.front)}L${pt(B.left)}L${pt(T.left)}"/>`;
  const shadow = node.z ? `<polygon class="hover-shadow" points="${cubeFaces(node.x, node.y, node.e * 1.05).footprint.map(pt).join(' ')}"/>`
    + `<line class="tether" x1="${r(node.x)}" y1="${r(node.y)}" x2="${r(node.x)}" y2="${r(node.y - node.z)}"/>` : '';
  const mix = (a, b, t) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
  const trim = mini ? `<path class="mini-lit${held ? ' held' : ''}" d="M${pt(mix(mix(T.left, B.left, 0.45), mix(T.front, B.front, 0.45), 0.2))}L${pt(mix(mix(T.left, B.left, 0.45), mix(T.front, B.front, 0.45), 0.8))} M${pt(mix(mix(T.front, B.front, 0.62), mix(T.right, B.right, 0.62), 0.25))}L${pt(mix(mix(T.front, B.front, 0.62), mix(T.right, B.right, 0.62), 0.6))}"/>` : '';
  return `${shadow}<g class="cube-body">${body}${edges}${trim}</g>`;
}

function fleetCluster(node) {
  const d = 38;
  const u = [d * C, d * S];
  const v = [-d * C, d * S];
  // Six working studios in two rows, and the owner's studio set apart (it takes no agent lanes).
  const spots = [[-1, -0.55], [0, -0.55], [1, -0.55], [-1, 0.55], [0, 0.55], [1, 0.55]];
  const cubes = spots.map(([a, b]) => ({ x: node.x + a * u[0] + b * v[0], y: node.y + a * u[1] + b * v[1], e: 30, z: 0 }))
    .sort((p, q) => p.y - q.y)
    .map(spot => cube(spot, { mini: true }));
  const held = { x: node.x + 2.35 * u[0] + 1.35 * v[0], y: node.y + 2.35 * u[1] + 1.35 * v[1], e: 30, z: 0 };
  return cubes.join('') + `<g class="held-by-owner">${cube(held, { mini: true, held: true })}</g>`;
}

function anchor(id) {
  const node = byId[id];
  return [node.x, node.y - (node.z || 0) * 0.35];
}

function conduitPath(from, to, bend) {
  const a = anchor(from);
  const b = anchor(to);
  const mx = (a[0] + b[0]) / 2;
  const my = (a[1] + b[1]) / 2;
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const cx = mx - dy * bend;
  const cy = my + dx * bend;
  return `M${pt(a)} Q${pt([cx, cy])} ${pt(b)}`;
}

function deck() {
  const top = [500, 158];
  const right = [982, 436];
  const bottom = [500, 714];
  const left = [18, 436];
  const plate = [top, right, bottom, left];
  const drop = 16;
  const lines = [];
  for (let i = 1; i < 12; i++) {
    const t = i / 12;
    const a = [top[0] + (right[0] - top[0]) * t, top[1] + (right[1] - top[1]) * t];
    const b = [left[0] + (bottom[0] - left[0]) * t, left[1] + (bottom[1] - left[1]) * t];
    const c = [top[0] + (left[0] - top[0]) * t, top[1] + (left[1] - top[1]) * t];
    const d = [right[0] + (bottom[0] - right[0]) * t, right[1] + (bottom[1] - right[1]) * t];
    lines.push(`M${pt(a)}L${pt(b)}M${pt(c)}L${pt(d)}`);
  }
  const rules = ['REVERSIBLE BY DEFAULT', 'RECEIPTS WITHOUT TEXT', 'FAIL OPEN', 'A BUDGET AND A GRANT PER CALL', 'REVIEWED INSTALLS', 'AN EXACT UNDO'];
  const angle = Math.atan2(bottom[1] - left[1], bottom[0] - left[0]) * 180 / Math.PI;
  const angle2 = Math.atan2(right[1] - bottom[1], right[0] - bottom[0]) * 180 / Math.PI;
  const edgeText = (words, from, deg) => `<text class="hull-text" transform="translate(${pt(from)}) rotate(${r(deg)}) skewX(${r(deg)})" dy="${drop - 4}">${words}</text>`;
  return `<g class="deck">
<polygon class="deck-side" points="${[left, bottom, [bottom[0], bottom[1] + drop], [left[0], left[1] + drop]].map(pt).join(' ')}"/>
<polygon class="deck-side right" points="${[bottom, right, [right[0], right[1] + drop], [bottom[0], bottom[1] + drop]].map(pt).join(' ')}"/>
<polygon class="deck-plate" points="${plate.map(pt).join(' ')}"/>
<path class="deck-grid" d="${lines.join('')}"/>
<polygon class="deck-rim" points="${plate.map(pt).join(' ')}"/>
${edgeText(rules.slice(0, 3).join('  ·  '), [left[0] + 40, left[1] + 40 * Math.tan(angle * Math.PI / 180) + 2], angle)}
${edgeText(rules.slice(3).join('  ·  '), [bottom[0] + 34, bottom[1] + 34 * Math.tan(angle2 * Math.PI / 180) + 2], angle2)}
</g>`;
}

export function renderArt() {
  const defs = `<defs>
<linearGradient id="map-top" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#3a4a32"/><stop offset="1" stop-color="#26321f"/></linearGradient>
<linearGradient id="map-left" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1f291b"/><stop offset="1" stop-color="#121810"/></linearGradient>
<linearGradient id="map-right" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#151c13"/><stop offset="1" stop-color="#0b0f0a"/></linearGradient>
<radialGradient id="map-halo"><stop offset="0" stop-color="#94d93c" stop-opacity=".16"/><stop offset="1" stop-color="#94d93c" stop-opacity="0"/></radialGradient>
<symbol id="g-top-0" viewBox="0 0 1 1" preserveAspectRatio="none">${greebles(1)}</symbol>
<symbol id="g-top-1" viewBox="0 0 1 1" preserveAspectRatio="none">${greebles(2)}</symbol>
<symbol id="g-side-0" viewBox="0 0 1 1" preserveAspectRatio="none">${greebles(3)}</symbol>
<symbol id="g-side-1" viewBox="0 0 1 1" preserveAspectRatio="none">${greebles(4)}</symbol>
<symbol id="g-side-2" viewBox="0 0 1 1" preserveAspectRatio="none">${greebles(5)}</symbol>
</defs>`;
  const conduits = CONDUITS.map(([id, from, to, steps, bend]) => {
    const d = conduitPath(from, to, bend);
    return `<g class="conduit" data-steps="${steps}" data-link="${from} ${to}"><path class="conduit-bed" d="${d}"/><path class="conduit-line" d="${d}"/><path class="conduit-pulse" pathLength="100" d="${d}"/></g>`;
  }).join('\n');
  const spokes = SPOKES.map(([id, steps]) =>
    `<g class="conduit spoke" data-steps="${steps}" data-link="jev ${id}"><path class="conduit-line" d="M${pt(anchor('jev'))}L${pt(anchor(id))}"/><path class="conduit-pulse" pathLength="100" d="M${pt(anchor('jev'))}L${pt(anchor(id))}"/></g>`).join('\n');
  const order = [...NODES].sort((a, b) => (a.y - (a.z || 0) * 0.01) - (b.y - (b.z || 0) * 0.01));
  const cubes = order.map(node => {
    const inner = node.cluster ? fleetCluster(node) : node.id === 'log' ? logConsole(node) : cube(node);
    return `<g class="cube" data-part="${node.id}">${node.id === 'memory' ? `<ellipse class="core-halo" cx="${node.x}" cy="${node.y - node.e * 0.55}" rx="${node.e * 1.25}" ry="${node.e * 1.05}" fill="url(#map-halo)"/>` : ''}${inner}</g>`;
  }).join('\n');
  return `<svg class="map-art" viewBox="0 0 ${W} ${H}" aria-hidden="true" focusable="false" preserveAspectRatio="xMidYMid meet">
${defs}
${deck()}
<g class="conduits">
${spokes}
${conduits}
</g>
<g class="cubes">
${cubes}
</g>
</svg>`;
}

function logConsole(node) {
  const f = cubeFaces(node.x, node.y, node.e, 0);
  const slab = cubeFaces(node.x, node.y, node.e * 1.5, 0);
  const flat = [slab.footprint[0], slab.footprint[1], slab.footprint[2], slab.footprint[3]].map(([x, y]) => [x, y - 10]);
  return `<polygon class="face left" points="${[slab.footprint[3], slab.footprint[2], flat[2], flat[3]].map(pt).join(' ')}"/>`
    + `<polygon class="face right" points="${[slab.footprint[2], slab.footprint[1], flat[1], flat[2]].map(pt).join(' ')}"/>`
    + `<polygon class="face top log-top" points="${flat.map(pt).join(' ')}"/>`
    + [0.25, 0.45, 0.65].map(t => `<line class="log-line" x1="${r(flat[3][0] + (flat[0][0] - flat[3][0]) * 0.2 + (flat[2][0] - flat[3][0]) * t)}" y1="${r(flat[3][1] + (flat[0][1] - flat[3][1]) * 0.2 + (flat[2][1] - flat[3][1]) * t)}" x2="${r(flat[3][0] + (flat[0][0] - flat[3][0]) * 0.8 + (flat[2][0] - flat[3][0]) * t)}" y2="${r(flat[3][1] + (flat[0][1] - flat[3][1]) * 0.8 + (flat[2][1] - flat[3][1]) * t)}"/>`).join('')
    + `<polygon class="deck-rim" points="${flat.map(pt).join(' ')}"/>`
    + (f ? '' : '');
}

/** Hotspot centre and size for each part, as percentages of the art box. */
export function hotspots() {
  return Object.fromEntries(NODES.map(node => {
    if (node.cluster) return [node.id, { x: node.x / W * 100, y: (node.y - 30) / H * 100, w: 25, h: 16 }];
    const h = node.e * 2;
    const cy = node.y - (node.z || 0) - node.e * 0.5;
    return [node.id, { x: node.x / W * 100, y: cy / H * 100, w: Math.max(node.e * 2 * C, 70) / W * 100, h: Math.max(h, 60) / H * 100 }];
  }).map(([id, box]) => [id, Object.fromEntries(Object.entries(box).map(([k, value]) => [k, Math.round(value * 10) / 10]))]));
}

const START = '<!-- system-map:art -->';
const END = '<!-- /system-map:art -->';
if (import.meta.url === `file://${process.argv[1]}`) {
  const site = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
  const file = path.join(site, 'index.html');
  const html = await readFile(file, 'utf8');
  const start = html.indexOf(START);
  const end = html.indexOf(END);
  if (start < 0 || end < start) throw new Error('index.html is missing the system-map:art markers');
  const next = html.slice(0, start) + START + '\n' + renderArt() + '\n' + html.slice(end);
  if (process.argv.includes('--hotspots')) console.log(JSON.stringify(hotspots(), null, 2));
  else if (process.argv.includes('--check')) {
    if (next !== html) { console.error('System map art is out of date. Run: node site/assets/generate-system-map.mjs'); process.exit(1); }
    console.log('System map art is current.');
  } else {
    await writeFile(file, next);
    console.log(`System map art written (${renderArt().length} bytes).`);
  }
}
