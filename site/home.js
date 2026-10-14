(() => {
  // Toggletips and "Technical name" reveals: click to open, Escape or outside click to close.
  const triggers = [...document.querySelectorAll('.tip, .tip-more')];
  const closeAll = except => triggers.forEach(t => {
    if (t === except) return;
    t.setAttribute('aria-expanded', 'false');
    const pop = document.getElementById(t.getAttribute('aria-controls'));
    if (pop) pop.hidden = true;
  });
  triggers.forEach(t => t.addEventListener('click', e => {
    e.preventDefault(); e.stopPropagation();
    const pop = document.getElementById(t.getAttribute('aria-controls'));
    const open = t.getAttribute('aria-expanded') !== 'true';
    if (t.classList.contains('tip')) closeAll(t);
    t.setAttribute('aria-expanded', String(open));
    pop.hidden = !open;
  }));
  document.addEventListener('click', e => { if (!e.target.closest('.tip-wrap')) document.querySelectorAll('.tip').forEach(t => { t.setAttribute('aria-expanded', 'false'); document.getElementById(t.getAttribute('aria-controls')).hidden = true; }); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeAll(null); });

  // Tabs with arrow-key support.
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  const select = tab => tabs.forEach(t => {
    const on = t === tab;
    t.setAttribute('aria-selected', String(on));
    t.tabIndex = on ? 0 : -1;
    document.getElementById(t.getAttribute('aria-controls')).hidden = !on;
  });
  select(tabs.find(t => t.getAttribute('aria-selected') === 'true') || tabs[0]);
  tabs.forEach((t, i) => {
    t.addEventListener('click', () => select(t));
    t.addEventListener('keydown', e => {
      const k = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
      let next = null;
      if (k) next = tabs[(i + k + tabs.length) % tabs.length];
      if (e.key === 'Home') next = tabs[0];
      if (e.key === 'End') next = tabs[tabs.length - 1];
      if (next) { e.preventDefault(); select(next); next.focus(); }
    });
  });

  // Owner name fills the install command (the page does the typing, not the reader).
  const owner = document.getElementById('owner');
  const hint = document.getElementById('owner-hint');
  const hintText = hint.textContent;
  const slot = document.querySelector('.owner-slot');
  owner.addEventListener('input', () => {
    const v = owner.value.trim();
    const ok = v === '' || /^[a-z][a-z0-9_-]{0,47}$/.test(v);
    owner.setAttribute('aria-invalid', String(!ok));
    hint.classList.toggle('err', !ok);
    hint.textContent = ok ? hintText : 'Start with a lowercase letter. Then use lowercase letters, numbers, - or _.';
    slot.textContent = ok && v ? v : 'yourname';
  });

  // Copy buttons. The command copy leaves out the explanations so the lines paste cleanly.
  const copyText = id => id === 'cmds'
    ? [...document.querySelectorAll('#cmds code')].map(c => c.textContent).join('\n')
    : document.getElementById(id).textContent.trim();
  document.querySelectorAll('.copy').forEach(btn => {
    const label = btn.textContent;
    btn.addEventListener('click', async () => {
      const text = copyText(btn.dataset.copy);
      let ok = false;
      try { await navigator.clipboard.writeText(text); ok = true; } catch (_) {
        const ta = document.createElement('textarea');
        ta.value = text; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.select();
        try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
        ta.remove();
      }
      btn.textContent = ok ? 'Copied' : 'Select and copy';
      btn.classList.toggle('done', ok);
      setTimeout(() => { btn.textContent = label; btn.classList.remove('done'); }, 2200);
    });
  });

  // "Will it run?" answers come straight from the site's planning tiers
  // (16 GB: try it small, plan 50 GB; 32 GB+: daily use, plan 100 GB; 20 GB free to install).
  const form = document.getElementById('checker');
  const $ = id => document.getElementById(id);
  const guide = './guide.html';
  function verdict(kind, ram, disk) {
    if (kind === 'windows') return { s: 'no', pill: 'Not on Windows', title: "BORG doesn't run on Windows.", body: 'Today it is built and tested for Macs with M-series chips.', plan: null, cta: null };
    if (kind === 'intel' || kind === 'linux') return { s: 'maybe', pill: 'Not tested yet', title: 'Downloads exist, but full support is still being checked.', body: 'BORG is tested on Macs with M-series chips today. Read the install guide before you start, so you know which parts may not work yet.', plan: null, cta: ['Read the install guide', guide] };
    if (disk === 'lt20') return { s: 'no', pill: 'Free up space first', title: 'Setup needs at least 20 GB of free space.', body: 'BORG downloads its local AI models and tools during setup. Clear some space, then check again.', plan: null, cta: null };
    if (ram === '8') return { s: 'maybe', pill: 'Below our plans', title: '8 GB is below the smallest setup we plan for.', body: "BORG's local AI models need room, so it may run slowly. Our smallest plan is 16 GB, and we haven't measured 8 GB.", plan: ['16 GB', 'About 50 GB'], cta: null };
    if (ram === '16') return { s: 'small', pill: 'Good to try', title: 'Good for trying BORG with light use.', body: 'Start small and watch how your Mac feels before you rely on it.' + (disk === '20' ? ' You have enough space to install; plan for about 50 GB free as your memory grows.' : ''), plan: ['16 GB', 'About 50 GB'], cta: ['Get started', '#start'] };
    if (ram === '24') return { s: 'small', pill: 'Good to try', title: 'Comfortable for trying BORG.', body: 'For a full daily setup we plan for 32 GB or more.' + (disk === '20' ? ' You have enough space to install; plan for about 50 to 100 GB free over time.' : ''), plan: ['24 GB', '50 to 100 GB'], cta: ['Get started', '#start'] };
    return { s: 'yes', pill: 'Great fit', title: "Your Mac can run BORG's full memory every day.", body: 'This matches our plan for a daily setup with a modest amount of agent work.' + (disk === '100' ? '' : ' Plan for about 100 GB free as your memory grows.'), plan: ['32 GB or more', 'About 100 GB'], cta: ['Get started', '#start'] };
  }
  function update() {
    const d = new FormData(form);
    const v = verdict(d.get('kind'), d.get('ram'), d.get('disk'));
    $('result').dataset.state = v.s;
    $('r-pill').textContent = v.pill;
    $('r-title').textContent = v.title;
    $('r-body').textContent = v.body;
    $('r-plan').hidden = !v.plan;
    if (v.plan) { $('r-ram').textContent = v.plan[0]; $('r-disk').textContent = v.plan[1]; }
    const cta = $('r-cta');
    cta.hidden = !v.cta;
    if (v.cta) { cta.firstChild.textContent = v.cta[0] + ' '; cta.href = v.cta[1]; cta.lastElementChild.textContent = v.cta[1].startsWith('#') ? '↓' : '↗'; }
    const pcOnly = d.get('kind') !== 'm';
    form.querySelectorAll('input[name="ram"], input[name="disk"]').forEach(i => { i.disabled = pcOnly; i.closest('fieldset').style.opacity = pcOnly ? .45 : 1; });
  }

  // One motion switch for the page. Reduced-motion users start paused.
  const motion = document.getElementById('motion');
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)');
  const setMotion = paused => {
    document.documentElement.dataset.motion = paused ? 'paused' : 'running';
    motion.textContent = paused ? 'Play motion' : 'Pause motion';
    motion.setAttribute('aria-pressed', String(!paused));
  };
  setMotion(reduce.matches);
  motion.addEventListener('click', () => setMotion(document.documentElement.dataset.motion !== 'paused'));
  reduce.addEventListener('change', e => setMotion(e.matches));

  form.addEventListener('change', update);
  form.addEventListener('submit', e => e.preventDefault());
  update();
})();
