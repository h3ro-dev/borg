/** Pure, offline planning. These estimates never authorize runtime dispatch. */
export const BLUEPRINT_SCHEMA = 'borg-blueprint/v1';
const ID = /^[a-z][a-z0-9-]{0,31}$/;
const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const sameKeys = (value, keys) => object(value)
  && Object.keys(value).length === keys.length
  && keys.every((key) => Object.hasOwn(value, key));
const blueprintKeys = ['schema', 'catalog_version', 'goal', 'machines'];
const machineKeys = ['id', 'label', 'platform', 'profile', 'components', 'integrations', 'workload'];
const workloadKeys = ['agents', 'browsers', 'builds', 'memory_millions', 'project_gb', 'context_tokens', 'training'];

export function resolveComponents(catalog, ids) {
  const byId = new Map(catalog.items.map((item) => [item.id, item]));
  const resolved = new Set();
  const visiting = new Set();
  function visit(id) {
    if (resolved.has(id)) return;
    const item = byId.get(id);
    if (!item || item.type !== 'component') throw new Error(`Unknown component: ${id}`);
    if (visiting.has(id)) throw new Error(`Cyclic component dependency: ${id}`);
    visiting.add(id);
    for (const dependency of item.requires) visit(dependency);
    visiting.delete(id);
    resolved.add(id);
  }
  for (const id of ids) visit(id);
  return [...resolved];
}

export function createMachine(catalog, { id = 'node-1', goal = 'coding' } = {}) {
  const preset = catalog.goals.find((row) => row.id === goal);
  if (!preset) throw new Error('Unknown workload preset');
  return { id, label: id.replace(/^node-/, 'Node '), platform: 'macos-arm64',
    profile: preset.profile, components: resolveComponents(catalog, preset.components),
    integrations: [...preset.integrations], workload: { ...preset.workload } };
}

export function createBlueprint(catalog, goal = 'coding') {
  return { schema: BLUEPRINT_SCHEMA, catalog_version: catalog.version, goal,
    machines: [createMachine(catalog, { goal })] };
}

export function validateBlueprint(blueprint, catalog) {
  const errors = [];
  if (!sameKeys(blueprint, blueprintKeys)) {
    return { valid: false, errors: ['Blueprint must contain only schema, catalog_version, goal and machines.'] };
  }
  if (blueprint.schema !== BLUEPRINT_SCHEMA) errors.push('Unsupported blueprint version.');
  if (blueprint.catalog_version !== catalog.version) errors.push('Catalog version differs; regenerate this blueprint.');
  if (!catalog.goals.some((row) => row.id === blueprint.goal)) errors.push('Unknown workload preset.');
  if (!Array.isArray(blueprint.machines) || blueprint.machines.length < 1 || blueprint.machines.length > 100) {
    return { valid: false, errors: [...errors, 'Choose between 1 and 100 machines per blueprint.'] };
  }
  const ids = new Set();
  const items = new Map(catalog.items.map((item) => [item.id, item]));
  blueprint.machines.forEach((machine, index) => {
    const prefix = `Machine ${index + 1}: `;
    const fail = (message) => errors.push(prefix + message);
    if (!sameKeys(machine, machineKeys)) { fail('unexpected or missing machine fields.'); return; }
    if (typeof machine.id !== 'string' || !ID.test(machine.id)) fail('use a short lowercase machine ID.');
    if (ids.has(machine.id)) fail('machine ID must be unique.');
    ids.add(machine.id);
    if (typeof machine.label !== 'string' || !machine.label.trim() || machine.label.length > 60
        || /[\p{C}\u2028\u2029]/u.test(machine.label)) fail('use a plain label of 1–60 characters.');
    if (!catalog.platforms.some((row) => row.id === machine.platform)) fail('unknown operating system.');
    if (!catalog.profiles.some((row) => row.id === machine.profile)) fail('unknown machine role.');
    for (const [key, type] of [['components', 'component'], ['integrations', 'integration']]) {
      const selected = machine[key];
      if (!Array.isArray(selected) || selected.length > catalog.items.length) { fail(`invalid ${key} list.`); continue; }
      if (new Set(selected).size !== selected.length) fail(`duplicate ${key} choices.`);
      for (const id of selected) {
        const item = items.get(id);
        if (!item || item.type !== type) { fail(`unknown ${type} choice.`); continue; }
        if (!item.profiles.includes(machine.profile)) fail(`${item.name} needs a full BORG node.`);
        // Windows remains a planning choice; the native installer refuses it.
        if (machine.platform !== 'windows' && !item.platforms.includes(machine.platform)) fail(`${item.name} is unavailable on this platform.`);
        for (const required of item.requires) {
          if (!Array.isArray(machine.components) || !machine.components.includes(required)) fail(`${item.name} requires ${items.get(required)?.name ?? required}.`);
        }
      }
    }
    const w = machine.workload;
    if (!sameKeys(w, workloadKeys)) { fail('unexpected or missing workload fields.'); return; }
    for (const [key, maximum, integer] of [['agents',128,true],['browsers',32,true],['builds',32,true],['memory_millions',100,false],['project_gb',100000,true]]) {
      if (typeof w[key] !== 'number' || !Number.isFinite(w[key]) || w[key] < 0 || w[key] > maximum
          || (integer && !Number.isInteger(w[key]))) fail(`${key} must be ${integer ? 'an integer' : 'a number'} from 0 to ${maximum}.`);
    }
    if (![8192,16384,32768].includes(w.context_tokens)) fail('choose an 8K, 16K or 32K extraction context.');
    if (typeof w.training !== 'boolean') fail('training must be true or false.');
    if (w.training && (machine.profile !== 'full' || machine.platform !== 'macos-arm64'
        || !Array.isArray(machine.components) || !machine.components.includes('training'))) {
      fail('local training requires a full Apple Silicon node with Training and evaluation selected.');
    }
  });
  return { valid: errors.length === 0, errors };
}

const roundUp = (value, unit) => Math.ceil(value / unit) * unit;
function memoryTier(value) {
  return [8,16,24,32,48,64,96,128,192,256,384,512,768,1024].find((tier) => tier >= value)
    ?? roundUp(value, 256);
}

export function estimateMachine(machine, catalog) {
  const checked = validateBlueprint({schema:BLUEPRINT_SCHEMA,catalog_version:catalog.version,goal:'custom',machines:[machine]}, catalog);
  if (!checked.valid) throw new Error(checked.errors.join(' '));
  const w = machine.workload;
  const a = catalog.sizing.allowances;
  const full = machine.profile === 'full';
  const training = w.training || machine.components.includes('training');
  const adapters = machine.components.includes('adapters');
  const breakdown = [];
  const add = (label, ram_gb = 0, disk_gb = 0, cpu_cores = 0) => breakdown.push({label,ram_gb,disk_gb,cpu_cores});
  add('Operating system reserve', a.os_ram_gb, 0, 1);
  add(full ? 'Memory, graph and small local models' : 'Connector and selected work services',
    full ? a.full_services_ram_gb : a.tools_services_ram_gb,
    full ? a.full_runtime_disk_gb : a.tools_runtime_disk_gb, full ? 2 : 1);
  if (full) add('Local model context reserve', 2 * w.context_tokens / 16384);
  add('Active cloud-agent clients', w.agents * a.per_agent_ram_gb, 0, w.agents * a.per_agent_cpu_cores);
  add('Concurrent browser sessions', w.browsers * a.per_browser_ram_gb, 0, w.browsers * a.per_browser_cpu_cores);
  add('Concurrent builds and tests', w.builds * a.per_build_ram_gb, 0, w.builds * a.per_build_cpu_cores);
  if (full) add('Stored facts, graph and history', w.memory_millions * a.per_million_facts_ram_gb,
    w.memory_millions * a.per_million_facts_disk_gb);
  add('Projects, caches and working copies', 0, w.project_gb * a.project_disk_multiplier);
  if (adapters) add('Optional adapter bases and evaluation', 4, 10);
  if (training) add('Training experiment reserve', a.training_extra_ram_gb, a.training_extra_disk_gb, 2);
  const raw = breakdown.reduce((sum,row) => ({ram_gb:sum.ram_gb+row.ram_gb,disk_gb:sum.disk_gb+row.disk_gb,cpu_cores:sum.cpu_cores+row.cpu_cores}),{ram_gb:0,disk_gb:0,cpu_cores:0});
  const ram_gb = memoryTier(Math.max(training ? 64 : full ? 32 : 16, raw.ram_gb * a.headroom_multiplier));
  const cpu_cores = roundUp(Math.max(4, raw.cpu_cores * a.headroom_multiplier), 2);
  const free_disk_gb = roundUp(Math.max(training ? 200 : full ? 100 : 50, raw.disk_gb * a.headroom_multiplier), 25);
  add('30% headroom and hardware tier rounding', Math.round((ram_gb-raw.ram_gb)*100)/100,
    Math.round((free_disk_gb-raw.disk_gb)*100)/100, Math.round((cpu_cores-raw.cpu_cores)*100)/100);
  const platform = catalog.platforms.find((row) => row.id === machine.platform);
  const warnings = [];
  if (platform.status !== 'verified') warnings.push(platform.note);
  if (machine.integrations.length) warnings.push('External-service compute and storage are excluded. Add a separate budget if you host them on this machine.');
  if (!full) warnings.push('This node has no running local memory or graph. Fleet enrollment does not automatically connect it to another node’s brain.');
  if (w.agents > 8 || w.builds > 4 || w.browsers > 8) warnings.push('This is a large single-machine workload. Profile it and consider splitting work across nodes; estimates do not prove concurrency.');
  if (training) warnings.push('Training is not enabled by this plan. Budget the exact dataset/checkpoints and profile your model before buying hardware.');
  if (machine.components.includes('desktop')) warnings.push('Physical desktop focus is shared. Use separate browser sessions or machines for independent interactive work.');
  return {ram_gb,cpu_cores,free_disk_gb,gpu:full
    ? (machine.platform === 'macos-arm64' ? 'Apple Silicon unified GPU memory; included in the RAM estimate.' : 'Check Ollama support for your GPU and model. CPU inference may be slower; GPU memory is not certified by this estimate.')
    : 'No accelerator required by the connector. Your build, browser or external model workload may need one.',
    breakdown,warnings,assumptions:[...catalog.sizing.notes, 'A “build” is an ordinary project compile/test job. Containers, VMs, video rendering and unusually large builds need separate allowances.']};
}

export function planBlueprint(blueprint, catalog) {
  const checked = validateBlueprint(blueprint, catalog);
  if (!checked.valid) return {...checked,machines:[],totals:{ram_gb:0,cpu_cores:0,free_disk_gb:0},warnings:[]};
  const byId = new Map(catalog.items.map((item) => [item.id,item]));
  const machines = blueprint.machines.map((machine) => {
    const estimate = estimateMachine(machine,catalog);
    const setup = [
      `Copy borg-blueprint.json to ${machine.label}; clone the public repository there.`,
      `python3 borg.py blueprint inspect borg-blueprint.json --machine ${machine.id}`,
      `./install.sh --owner YOUR_OWNER_ID --blueprint borg-blueprint.json --machine ${machine.id}`,
      'Run the installed borg doctor and borg onboard commands, then verify your actual workflow.',
      ...[...machine.components,...machine.integrations].flatMap((id) => byId.get(id).setup.map((step) => `${byId.get(id).name}: ${step}`)),
    ];
    return {id:machine.id,label:machine.label,estimate,setup,warnings:estimate.warnings};
  });
  const totals = machines.reduce((sum,{estimate:e}) => ({ram_gb:sum.ram_gb+e.ram_gb,cpu_cores:sum.cpu_cores+e.cpu_cores,free_disk_gb:sum.free_disk_gb+e.free_disk_gb}),{ram_gb:0,cpu_cores:0,free_disk_gb:0});
  return {...checked,machines,totals,warnings:[
    'Selection records setup intent, not live readiness, authentication or a security permission grant.',
    'Each machine is installed independently. Fleet enrollment, provider sign-in and external services require your own configuration.',
  ]};
}
