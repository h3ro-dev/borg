/* In-memory UI only. The public planner owns validation and all sizing math. */
const host = document.querySelector('#configurator');
const status = document.querySelector('#configure-status');
const repository = 'https://github.com/h3ro-dev/borg/blob/master/';
const number = new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 });

function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key === 'class') node.className = value;
    else if (value !== false && value !== undefined) node.setAttribute(key, value === true ? '' : value);
  }
  node.append(...children.flat().filter(child => child !== undefined && child !== null));
  return node;
}
function say(message) { status.textContent = message; }
function publicLink(value) {
  if (!value) return null;
  if (value.startsWith('https://')) return value;
  if (/^[a-z][a-z0-9+.-]*:/i.test(value) || value.startsWith('/') || value.includes('..')) return null;
  return repository + value.replace(/^\.\//, '');
}
const list = values => el('ul', {}, values.map(value => el('li', {}, value)));

async function start() {
  const [response, planner] = await Promise.all([
    fetch(new URL('./platform/catalog.json', import.meta.url)),
    import('./platform/planner.mjs'),
  ]);
  if (!response.ok) throw new Error('Catalog unavailable');
  const catalog = await response.json();
  const { createBlueprint, createMachine, resolveComponents, validateBlueprint, planBlueprint } = planner;
  const blueprint = createBlueprint(catalog);
  let activeId = blueprint.machines[0].id;
  let sequence = 1;
  const presets = new Map([[activeId, blueprint.goal]]);
  const items = new Map(catalog.items.map(item => [item.id, item]));
  const active = () => blueprint.machines.find(machine => machine.id === activeId);
  const title = id => items.get(id)?.name || id;
  const selectionKey = item => item.type === 'integration' ? 'integrations' : 'components';
  const compatible = (item, machine, seen = new Set()) => {
    if (!item || seen.has(item.id)) return false;
    if (machine.platform !== 'windows' && !item.platforms.includes(machine.platform)) return false;
    if (item.id === 'training' && machine.platform !== 'macos-arm64') return false;
    if (!item.profiles.includes(machine.profile)) return false;
    return item.requires.every(id => compatible(items.get(id), machine, new Set([...seen, item.id])));
  };
  function reconcile(machine) {
    const removed = [];
    for (const key of ['components', 'integrations']) {
      machine[key] = machine[key].filter(id => {
        if (compatible(items.get(id), machine)) return true;
        removed.push(title(id));
        return false;
      });
    }
    if (!machine.components.includes('training')) machine.workload.training = false;
    return removed;
  }
  function choose(item, checked) {
    const machine = active();
    const key = selectionKey(item);
    if (checked) {
      machine[key] = [...new Set([...machine[key], item.id])];
      const previous = machine.components;
      machine.components = resolveComponents(catalog, machine.components);
      const added = machine.components.filter(id => !previous.includes(id));
      if (item.id === 'training') machine.workload.training = true;
      say(added.length ? `Also selected: ${added.map(title).join(', ')}. Required by your choices.` : `${item.name} selected. Setup requirements are shown in your plan.`);
    } else {
      const removed = new Set([item.id]);
      let changed;
      do {
        changed = false;
        for (const id of [...machine.components, ...machine.integrations]) {
          if (!removed.has(id) && items.get(id).requires.some(dep => removed.has(dep))) {
            removed.add(id);
            changed = true;
          }
        }
      } while (changed);
      machine.components = machine.components.filter(id => !removed.has(id));
      machine.integrations = machine.integrations.filter(id => !removed.has(id));
      if (removed.has('training')) machine.workload.training = false;
      say(`Removed ${[...removed].map(title).join(', ')}${removed.size > 1 ? '; dependent choices were removed too' : ''}.`);
    }
    render(`choice-${item.id}`);
  }
  function field(label, input, hint) {
    const id = input.id;
    if (hint) input.setAttribute('aria-describedby', `${id}-hint`);
    return el('div', { class: 'builder-field' }, el('label', { for: id }, label), input,
      hint ? el('p', { id: `${id}-hint`, class: 'field-hint' }, hint) : null);
  }
  function select(id, options, value, onchange) {
    const node = el('select', { id, onchange }, options.map(option => el('option', { value: option.id }, option.name)));
    node.value = value;
    return node;
  }
  const form = el('form', { class: 'builder', novalidate: true, onsubmit: event => event.preventDefault() });
  const toolbar = el('div', { class: 'builder-toolbar' });
  const nodes = el('div', { class: 'machine-list', role: 'group', 'aria-label': 'Your machines' });
  const editor = el('div', { class: 'machine-editor' });
  const summary = el('aside', { class: 'machine-summary', 'aria-label': 'Machine planning estimate' });
  const errors = el('div', { id: 'blueprint-errors', class: 'builder-errors', tabindex: '-1', hidden: true });
  const delivery = el('div', { class: 'blueprint-delivery' });
  form.append(toolbar, nodes, el('div', { class: 'builder-grid' }, editor, summary), errors, delivery);
  host.replaceChildren(form);

  function renderNodes() {
    nodes.replaceChildren(...blueprint.machines.map((machine, index) => el('button', {
      type: 'button', id: `select-${machine.id}`, class: 'machine-select', 'aria-pressed': String(machine.id === activeId),
      onclick: () => { activeId = machine.id; render(`select-${machine.id}`); },
    }, el('span', { class: 'node-index', 'aria-hidden': 'true' }, String(index + 1).padStart(2, '0')),
    el('span', {}, machine.label || machine.id), el('small', {}, machine.id))));
  }
  function render(focusId) {
    const openDetails = new Map([...form.querySelectorAll('details[id]')].map(node => [node.id, node.open]));
    const machine = active();
    toolbar.replaceChildren(
      el('div', {}, el('p', { class: 'builder-kicker' }, 'COLLECTIVE BLUEPRINT'),
        el('p', { class: 'builder-muted' }, 'Saved only when you download. No accounts connected here.')),
      el('button', { id: 'add-machine', type: 'button', class: 'builder-button', disabled: blueprint.machines.length >= 100,
        onclick: () => {
          do { sequence++; } while (blueprint.machines.some(node => node.id === `node-${sequence}`));
          const next = createMachine(catalog, { id: `node-${sequence}`, goal: blueprint.goal });
          blueprint.machines.push(next);
          presets.set(next.id, blueprint.goal);
          activeId = next.id;
          render('machine-label');
          say(`${next.label} added. Configure this machine independently.`);
        } }, '+ Add machine'),
    );
    renderNodes();
    const labelInput = el('input', { id: 'machine-label', type: 'text', required: true, maxlength: 60, value: machine.label,
      oninput: event => {
        machine.label = event.target.value;
        renderNodes();
        refresh();
      } });
    const goal = select('machine-preset', catalog.goals, presets.get(machine.id), event => {
      blueprint.goal = event.target.value;
      presets.set(machine.id, blueprint.goal);
      const replacement = createMachine(catalog, { id: machine.id, goal: blueprint.goal });
      Object.assign(machine, replacement, { label: machine.label, platform: machine.platform });
      const removed = reconcile(machine);
      render('machine-preset');
      say(`Preset applied to ${machine.label}. Other machines are unchanged.${removed.length ? ` Unavailable choices omitted: ${removed.join(', ')}.` : ''}`);
    });
    // A preset is a starting point; edited machines never claim to match it exactly.
    const identity = el('div', { class: 'builder-fields' },
      field('Machine name', labelInput, `Stable ID: ${machine.id}`),
      field('Use-case preset', goal, 'Changing this resets this machine’s role, workload, and selections.'),
      field('Platform', select('machine-platform', catalog.platforms, machine.platform, event => {
        machine.platform = event.target.value;
        const removed = reconcile(machine);
        render('machine-platform');
        say(removed.length ? `Platform changed. Removed unavailable choices: ${removed.join(', ')}.` : 'Platform changed. Review its acceptance status.');
      })),
      field('Machine role', select('machine-profile', catalog.profiles, machine.profile, event => {
        machine.profile = event.target.value;
        const removed = reconcile(machine);
        render('machine-profile');
        say(removed.length ? `Role changed. Removed incompatible choices: ${removed.join(', ')}.` : 'Role changed. Estimate updated.');
      })),
    );
    const profile = catalog.profiles.find(row => row.id === machine.profile);
    const platform = catalog.platforms.find(row => row.id === machine.platform);
    const workload = el('div', { class: 'workload-fields' });
    const numericFields = [
      ['agents', 'Concurrent agents', 128, 1], ['browsers', 'Browser sessions', 32, 1],
      ['builds', 'Concurrent builds', 32, 1], ['memory_millions', 'Stored memories (millions)', 100, 'any'],
      ['project_gb', 'Project space (GB)', 100000, 1],
    ];
    for (const [key, label, max, step] of numericFields) {
      const input = el('input', { id: `workload-${key}`, type: 'number', min: 0, max, step, required: true,
        value: machine.workload[key], disabled: machine.profile === 'tools' && key === 'memory_millions', oninput: event => {
          machine.workload[key] = event.target.value === '' ? null : event.target.valueAsNumber;
          refresh();
        } });
      workload.append(field(label, input));
    }
    const contextSelect = select('workload-context', [8192, 16384, 32768].map(value => ({ id: String(value), name: `${number.format(value)} tokens` })), String(machine.workload.context_tokens), event => {
      machine.workload.context_tokens = Number(event.target.value);
      refresh();
    });
    contextSelect.disabled = machine.profile === 'tools';
    workload.append(field('Local model context', contextSelect));
    editor.replaceChildren(
      el('div', { class: 'editor-heading' }, el('h3', {}, 'Configure this machine'),
        el('button', { id: 'remove-machine', type: 'button', class: 'builder-remove', disabled: blueprint.machines.length === 1,
          onclick: () => {
            const removed = machine.label;
            const index = blueprint.machines.indexOf(machine);
            blueprint.machines.splice(index, 1);
            presets.delete(machine.id);
            activeId = blueprint.machines[Math.min(index, blueprint.machines.length - 1)].id;
            render(`select-${activeId}`);
            say(`${removed} removed. Remaining machine IDs are unchanged.`);
          } }, 'Remove')),
      identity,
      el('div', { class: 'role-note' }, el('p', {}, profile.description), el('p', { class: 'platform-note' }, platform.note)),
      el('fieldset', { class: 'workload-section' }, el('legend', {}, 'Work in flight'), workload,
        el('p', { class: 'field-hint' }, 'Planning allowances for simultaneous work. Provider quotas and actual throughput are separate. Stored memory and local context apply only to full nodes.')),
      choices('component', 'Components', true), choices('integration', 'Integrations', false),
    );
    refresh();
    for (const [id, open] of openDetails) {
      const detail = document.getElementById(id);
      if (detail) detail.open = open;
    }
    if (focusId) document.getElementById(focusId)?.focus({ preventScroll: true });
  }
  function choices(type, heading, open) {
    const machine = active();
    const key = type === 'component' ? 'components' : 'integrations';
    const rows = catalog.items.filter(item => item.type === type);
    const groups = [...new Set(rows.map(item => item.category))];
    return el('details', { id: `choices-${type}`, class: 'choice-section', open },
      el('summary', {}, heading, el('span', {}, `${machine[key].length} selected`)),
      el('p', { class: 'choice-intro' }, type === 'component'
        ? 'Select what this machine will prepare. Required components are added automatically.'
        : 'Selections create your setup checklist. Services are not deployed or authenticated by a checkbox.'),
      groups.map(group => el('details', { id: `group-${type}-${groups.indexOf(group)}`, class: 'choice-category', open: group === 'Orchestration' },
        el('summary', {}, group, el('span', {}, `${rows.filter(item => item.category === group && machine[key].includes(item.id)).length} selected`)),
        el('fieldset', { class: 'choice-group' }, el('legend', { class: 'sr-only' }, group),
        rows.filter(item => item.category === group).map(item => {
          const available = compatible(item, machine);
          const input = el('input', { id: `choice-${item.id}`, type: 'checkbox', checked: machine[key].includes(item.id),
            disabled: !available, 'aria-describedby': `description-${item.id}`, onchange: event => choose(item, event.target.checked) });
          const docs = publicLink(item.docs);
          const source = publicLink(item.source);
          return el('div', { class: `component-choice${available ? '' : ' unavailable'}` },
            el('div', { class: 'choice-heading' }, input, el('label', { for: input.id }, item.name), el('span', { class: `component-status status-${item.status}` }, item.status_label)),
            el('p', { id: `description-${item.id}` }, item.summary),
            !available ? el('p', { class: 'compatibility-note' }, 'Unavailable for this role or platform, including required components.') : null,
            item.requires.length ? el('p', { class: 'dependency-note' }, `Requires: ${item.requires.map(title).join(', ')}.`) : null,
            el('details', { id: `details-${item.id}`, class: 'component-details' }, el('summary', {}, 'Setup & details'),
              list([...item.setup, ...item.limitations]),
              el('p', {}, `License: ${item.license}`),
              docs ? el('a', { href: docs }, 'Documentation ↗') : null,
              source && source !== docs ? el('a', { href: source }, 'Source ↗') : null),
          );
        })))),
    );
  }
  function refresh() {
    const expanded = [...summary.querySelectorAll('details[open]')].map(node => node.id);
    for (const input of editor.querySelectorAll('input')) {
      input.setAttribute('aria-invalid', String(!input.validity.valid));
    }
    const validation = validateBlueprint(blueprint, catalog);
    const nativeInvalid = [...form.querySelectorAll('input')].some(input => !input.validity.valid);
    const valid = validation.valid && !nativeInvalid;
    errors.hidden = valid;
    errors.replaceChildren(el('strong', {}, 'Review your configuration before downloading.'),
      list(validation.errors.length ? validation.errors : valid ? [] : ['Enter a value within the displayed input limits.']));
    if (!valid) {
      summary.replaceChildren(el('p', { class: 'builder-kicker' }, 'ESTIMATE PAUSED'), el('h3', {}, 'Check your inputs.'),
        el('p', {}, 'Fix the highlighted values to update the plan. Your other machine selections are preserved.'));
      renderDelivery(null);
      return;
    }
    const plan = planBlueprint(blueprint, catalog);
    const node = plan.machines.find(machine => machine.id === activeId);
    const estimate = node.estimate;
    const metrics = [['RAM', estimate.ram_gb, 'GB'], ['Free disk', estimate.free_disk_gb, 'GB'], ['CPU planning', estimate.cpu_cores, 'cores']];
    const warnings = [...new Set([...estimate.warnings, ...node.warnings, ...plan.warnings])];
    summary.replaceChildren(
      el('p', { class: 'builder-kicker' }, 'THIS MACHINE / PLANNING ESTIMATE'),
      el('h3', {}, active().label),
      el('dl', { class: 'estimate-metrics' }, metrics.map(([label, value, unit]) => el('div', {},
        el('dt', {}, label), el('dd', {}, el('strong', {}, number.format(value)), ` ${unit}`)))),
      el('p', { class: 'gpu-note' }, typeof estimate.gpu === 'string' ? estimate.gpu : JSON.stringify(estimate.gpu)),
      el('p', { class: 'estimate-boundary' }, 'Engineering allowances, not measured minimums or guaranteed capacity. Check actual memory pressure before adding work.'),
      warnings.length ? el('div', { class: 'estimate-warnings' }, list(warnings)) : null,
      el('details', { id: 'estimate-assumptions', class: 'estimate-details' }, el('summary', {}, 'What goes into this estimate'),
        el('div', { class: 'breakdown' }, estimate.breakdown.map(row => el('p', {}, el('strong', {}, row.label),
          el('span', {}, `${number.format(row.ram_gb)} GB RAM · ${number.format(row.disk_gb)} GB disk · ${number.format(row.cpu_cores)} CPU`)))),
        list(estimate.assumptions)),
      el('details', { id: 'estimate-setup', class: 'estimate-details' }, el('summary', {}, 'Selected setup requirements'),
        node.setup.length ? list(node.setup.filter(step => active().platform !== 'windows' || !step.startsWith('./install.sh'))) : el('p', {}, 'Review the role and component documentation before installation.')),
      el('a', { class: 'estimate-guide', href: repository + 'docs/SIZING.md' }, 'Sizing rationale & assumptions ↗'),
    );
    renderDelivery(plan);
    expanded.forEach(id => { const node = document.getElementById(id); if (node) node.open = true; });
  }
  function renderDelivery(plan) {
    const preview = el('div', { class: 'fleet-estimates' });
    if (plan) {
      preview.append(...plan.machines.map(machine => el('div', {}, el('strong', {}, machine.label),
        el('span', {}, `${number.format(machine.estimate.ram_gb)} GB RAM / ${number.format(machine.estimate.free_disk_gb)} GB disk / ${number.format(machine.estimate.cpu_cores)} cores`))));
    }
    const commands = ['python3 borg.py blueprint inspect borg-blueprint.json'];
    if (plan) for (const machine of blueprint.machines) {
      if (machine.platform !== 'windows') commands.push(`./install.sh --owner yourname --blueprint borg-blueprint.json --machine ${machine.id}`);
    }
    const commandBox = el('textarea', { id: 'blueprint-commands', readonly: true, rows: Math.min(commands.length + 1, 8), spellcheck: 'false', 'aria-label': 'Blueprint inspection and per-machine installation commands' });
    commandBox.value = commands.join('\n');
    delivery.replaceChildren(
      el('div', { class: 'delivery-heading' }, el('div', {}, el('p', { class: 'builder-kicker' }, 'TAKE THE NEXT STEP'),
        el('h3', {}, 'Your plan. Ready to review.'), el('p', { class: 'builder-muted' }, `${blueprint.machines.length} machine${blueprint.machines.length === 1 ? '' : 's'} · Each sized independently · Catalog ${catalog.version}`)),
        el('button', { type: 'button', id: 'download-blueprint', class: 'button primary', onclick: download }, 'Download blueprint ↓')),
      preview,
      el('p', { class: 'delivery-instructions' }, 'Save borg-blueprint.json in your BORG checkout. Inspect it first. Replace yourname with your owner ID, then run each installation command on its matching machine. Use a new owner home or an identical rerun.'),
      blueprint.machines.some(machine => machine.platform === 'windows') ? el('p', { class: 'compatibility-note' }, 'Windows is a planning-only choice. Native installation is unsupported; no Windows install command is generated.') : null,
      plan ? el('div', { class: 'blueprint-terminal' }, el('div', { class: 'blueprint-terminal-header' }, el('span', {}, 'FROM YOUR BORG CHECKOUT'),
        el('button', { type: 'button', id: 'copy-blueprint-commands', class: 'builder-button', onclick: async () => {
          try {
            if (!navigator.clipboard?.writeText) throw new Error('Unavailable');
            await navigator.clipboard.writeText(commandBox.value);
            say('Commands copied. Replace yourname and run each install on its matching machine.');
          } catch {
            commandBox.focus();
            commandBox.select();
            say('Clipboard unavailable. Commands selected — use your device’s copy action.');
          }
        } }, 'Copy commands')), commandBox) : el('p', {}, 'Commands will appear after all machines pass validation.'),
      el('p', { class: 'delivery-footnote' }, 'The download contains your choices, not estimates or credentials. Nothing is installed, connected, or started by this page. ',
        el('a', { href: repository + 'docs/BLUEPRINT.md' }, 'Blueprint setup guide ↗')),
    );
  }
  function download() {
    refresh();
    if (!errors.hidden) {
      errors.focus();
      say('Download paused. Fix the configuration errors first.');
      return;
    }
    const file = new Blob([JSON.stringify(blueprint, null, 2) + '\n'], { type: 'application/json' });
    const url = URL.createObjectURL(file);
    const link = el('a', { href: url, download: 'borg-blueprint.json' });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    document.getElementById('download-blueprint').focus({ preventScroll: true });
    say('Blueprint downloaded. Inspect it before installing on each machine.');
  }
  render();
}
start().catch(() => {
  host.replaceChildren(el('div', { class: 'builder-load-error', role: 'status' },
    el('p', {}, 'The configuration planner could not load. Reload the page to try again.'),
    el('a', { class: 'text-link', href: '#setup' }, 'Continue to the installation guide ↓')));
});
