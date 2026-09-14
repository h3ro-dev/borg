/* Views: pure state -> DOM functions.
 *
 * Every view takes (state, actions) and returns a node. `actions` is the callback
 * bag supplied by app.js, which keeps rendering free of transport concerns and
 * lets the test suite render any view against fabricated (including hostile)
 * server data and inspect the resulting node tree.
 */
(function (global) {
  'use strict';

  var NS = global.EcoInbox = global.EcoInbox || {};
  var core = NS.core, dom = NS.dom;
  var el = dom.el, chip = dom.chip, mono = dom.mono, field = dom.field, button = dom.button;
  var views = {};

  var KIND_META = {
    information: { label: 'information', kind: 'info', hint: 'Shared context. Carries no authority.' },
    instruction: { label: 'instruction', kind: 'instruct', hint: 'Binding. Requires a live grant chain.' },
    request: { label: 'request', kind: 'request', hint: 'Asks for work; the recipient decides.' },
    result: { label: 'result', kind: 'result', hint: 'Reports the outcome of earlier work.' }
  };
  views.KIND_META = KIND_META;

  var STATE_META = {
    queued: { kind: 'queued', hint: 'Delivered to the inbox, not yet claimed.' },
    leased: { kind: 'leased', hint: 'Claimed by a running agent; the lease can expire and redeliver.' },
    acknowledged: { kind: 'ack', hint: 'The recipient confirmed receipt.' },
    resolved: { kind: 'resolved', hint: 'The recipient reported the work finished.' }
  };
  views.STATE_META = STATE_META;

  function kindChip(kind) {
    var meta = KIND_META[kind];
    return chip(kind || 'unknown', meta ? meta.kind : 'neutral', meta ? meta.hint : null);
  }
  views.kindChip = kindChip;

  function stateChip(state) {
    var meta = STATE_META[state];
    return chip(state || 'unknown', meta ? meta.kind : 'neutral', meta ? meta.hint : null);
  }
  views.stateChip = stateChip;

  function idChip(id, label) {
    if (!id) return null;
    return chip((label ? label + ' ' : '') + core.truncate(String(id), 12), 'id', String(id));
  }
  views.idChip = idChip;

  function scopeChip(scope) {
    return chip(scope || '/', 'scope', 'Scope ' + (scope || '/'));
  }
  views.scopeChip = scopeChip;

  function actionChips(actions) {
    var list = Array.isArray(actions) ? actions : [];
    if (!list.length) return [chip('no actions', 'neutral')];
    return list.slice(0, 8).map(function (a) {
      return chip(a, a === '*' ? 'star' : 'action');
    }).concat(list.length > 8 ? [chip('+' + (list.length - 8) + ' more', 'neutral')] : []);
  }
  views.actionChips = actionChips;

  function whenText(iso, nowMs) {
    if (!iso) return '';
    var rel = core.relativeTime(iso, nowMs);
    return rel ? rel : core.formatTs(iso);
  }
  views.whenText = whenText;

  function timeEl(iso, nowMs) {
    if (!iso) return el('span', { class: 'muted' }, '—');
    return el('span', { class: 'when', title: core.formatTs(iso) }, whenText(iso, nowMs));
  }
  views.timeEl = timeEl;

  /* -------------------------------------------------------------- adjudication */

  function adjudicationHref(state) {
    var realm = state && state.realm;
    var links = (realm && realm.links) || [];
    for (var i = 0; i < links.length; i++) {
      if (links[i] && links[i].id === 'adjudication' && typeof links[i].href === 'string') {
        return links[i].href;
      }
    }
    return '/ops/#adjudication';
  }
  views.adjudicationHref = adjudicationHref;

  /**
   * Escalation is a link out to the existing Ops adjudication surface. This UI
   * records nothing about the case — there is exactly one case ledger and it is
   * not here.
   */
  function escalationBanner(state, reason, context) {
    return el('div', { class: 'banner banner-warn' }, [
      el('div', { class: 'banner-body' }, [
        el('strong', {}, 'Unresolved: '),
        el('span', {}, reason),
        context ? el('div', { class: 'muted small' }, context) : null
      ]),
      dom.link('Open Ops adjudication', adjudicationHref(state), { newTab: true, class: 'btn btn-ghost' })
    ]);
  }
  views.escalationBanner = escalationBanner;

  /* ---------------------------------------------------------------- forms */

  /**
   * Declarative form builder. Returns a node with a .collect() closure, so no
   * DOM query engine is needed to read the values back.
   */
  function form(spec, opts) {
    opts = opts || {};
    var inputs = {};
    var errorBox = el('div', { class: 'form-errors', hidden: true });

    function control(f) {
      var node, i;
      var common = { name: f.name, id: 'f-' + f.name + '-' + (opts.idSuffix || 'x') };
      if (f.placeholder) common.placeholder = f.placeholder;
      if (f.required) common['aria-required'] = 'true';
      if (f.maxlength) common.maxlength = String(f.maxlength);
      if (f.type === 'select') {
        node = el('select', common, (f.options || []).map(function (o) {
          var value = typeof o === 'string' ? o : o.value;
          var label = typeof o === 'string' ? o : o.label;
          return el('option', {
            value: value,
            selected: String(value) === String(f.value == null ? '' : f.value) ? true : null
          }, label);
        }));
      } else if (f.type === 'textarea') {
        common.rows = String(f.rows || 6);
        node = el('textarea', common, f.value == null ? '' : String(f.value));
      } else if (f.type === 'checkbox') {
        common.type = 'checkbox';
        if (f.value) common.checked = true;
        node = el('input', common);
      } else {
        common.type = f.type || 'text';
        if (f.value != null) common.value = String(f.value);
        node = el('input', common);
      }
      if (f.onchange) node.addEventListener('change', f.onchange);
      if (f.oninput) node.addEventListener('input', f.oninput);
      inputs[f.name] = { node: node, spec: f };
      return node;
    }

    var rows = (spec.fields || []).map(function (f) {
      var ctrl = control(f);
      if (f.type === 'checkbox') {
        return el('label', { class: 'form-row form-check' }, [ctrl, el('span', {}, f.label)]);
      }
      return el('label', { class: 'form-row' + (f.wide ? ' form-wide' : '') }, [
        el('span', { class: 'form-label' }, [
          f.label,
          f.required ? el('span', { class: 'req', title: 'required' }, ' *') : null
        ]),
        ctrl,
        f.hint ? el('span', { class: 'form-hint' }, f.hint) : null,
        f.counter ? el('span', { class: 'form-counter', id: 'c-' + f.name }, '') : null
      ]);
    });

    var node = el('form', {
      class: 'form' + (spec.class ? ' ' + spec.class : ''),
      onsubmit: function (ev) {
        if (ev && ev.preventDefault) ev.preventDefault();
        if (spec.onSubmit) spec.onSubmit(node.collect(), node);
        return false;
      }
    }, [
      spec.title ? el('h3', { class: 'form-title' }, spec.title) : null,
      spec.description ? el('p', { class: 'form-desc' }, spec.description) : null,
      errorBox,
      el('div', { class: 'form-grid' }, rows),
      el('div', { class: 'form-actions' }, [
        button(spec.submitLabel || 'Submit', { variant: 'primary', type: 'submit' }),
        spec.onCancel ? button('Cancel', { onclick: spec.onCancel }) : null,
        spec.extra || null
      ])
    ]);

    node.collect = function () {
      var out = {};
      Object.keys(inputs).forEach(function (k) {
        var it = inputs[k];
        if (it.spec.type === 'checkbox') out[k] = !!it.node.checked;
        else out[k] = it.node.value == null ? '' : String(it.node.value);
      });
      return out;
    };
    node.inputs = inputs;
    node.showErrors = function (errors) {
      dom.mount(errorBox, (errors || []).map(function (e) {
        return el('div', { class: 'form-error' }, e);
      }));
      if (errors && errors.length) errorBox.removeAttribute('hidden');
      else errorBox.setAttribute('hidden', '');
    };
    return node;
  }
  views.form = form;

  /* ------------------------------------------------------------- connect */

  /**
   * The front door.
   *
   * On the owner's own machine the local console launcher holds the credential
   * and this page never sees one: the console simply opens. The credential form
   * is the advanced fallback for a console served straight off a hub.
   */
  function connectPanel(state, actions) {
    var credentialForm = form({
      class: 'connect-form',
      title: state.localAvailable ? 'Connect with a credential instead' : 'Connect',
      submitLabel: state.busy ? 'Connecting…' : 'Connect',
      fields: [
        {
          name: 'credential', label: 'Service credential', type: 'password', required: true,
          placeholder: 'paste the credential from the hub',
          hint: 'Used for this tab only, then forgotten.'
        }
      ],
      onSubmit: function (values, node) {
        if (!values.credential || !values.credential.trim()) {
          node.showErrors(['Enter the credential.']);
          return;
        }
        node.showErrors([]);
        actions.connect(values.credential);
      }
    }, { idSuffix: 'connect' });

    var body;
    if (state.probing) {
      body = el('div', { class: 'connect-probing' }, [
        dom.spinner('Checking this machine’s connection…')
      ]);
    } else if (state.localAvailable && !state.showCredentialForm) {
      body = el('div', { class: 'connect-local' }, [
        el('p', {}, 'This machine is connected to the hub.'),
        button('Open the console', { variant: 'primary', onclick: actions.connectLocal }),
        el('button', {
          class: 'linklike small', onclick: actions.showCredentialForm
        }, 'Connect with a credential instead')
      ]);
    } else {
      body = credentialForm;
    }

    return el('div', { class: 'connect-screen' }, [
      el('div', { class: 'connect-card' }, [
        el('div', { class: 'brand' }, [
          el('div', { class: 'brand-mark', 'aria-hidden': 'true' }, '◇'),
          el('div', {}, [
            el('h1', {}, 'Agent Inbox & Authority'),
            el('p', { class: 'muted' }, 'Private owner console — tailnet only')
          ])
        ]),
        state.error ? el('div', { class: 'banner banner-error' }, state.error) : null,
        body,
        el('p', { class: 'connect-note muted small' },
          'Nothing is kept in this page: closing the tab ends the session.')
      ])
    ]);
  }
  views.connectPanel = connectPanel;

  /* ------------------------------------------------------------- overview */

  function tile(label, value, hint, kind) {
    return el('div', { class: 'tile' + (kind ? ' tile-' + kind : '') }, [
      el('div', { class: 'tile-value' }, String(value)),
      el('div', { class: 'tile-label' }, label),
      hint ? el('div', { class: 'tile-hint' }, hint) : null
    ]);
  }
  views.tile = tile;

  function overview(state, actions) {
    var snap = state.snapshot || {};
    var counts = core.snapshotCounts(snap);
    var now = state.nowMs || Date.now();
    var msgs = core.sortMessages(snap.messages || []).slice(0, 8);
    var unresolved = (snap.messages || []).filter(function (m) {
      return core.unresolvedReason(m, now);
    });

    return el('section', { class: 'view view-overview' }, [
      el('div', { class: 'view-head' }, [
        el('h2', {}, 'Overview'),
        el('div', { class: 'view-head-meta' }, [
          el('span', { class: 'muted' }, 'snapshot '),
          timeEl(snap.generated_at, now),
          button('Refresh', { onclick: actions.refresh, variant: 'ghost' })
        ])
      ]),
      el('div', { class: 'tiles' }, [
        tile('queued', counts.messages_queued, 'waiting to be claimed', 'queued'),
        tile('leased', counts.messages_leased, 'claimed, in progress', 'leased'),
        tile('acknowledged', counts.messages_acknowledged, 'receipt confirmed', 'ack'),
        tile('resolved', counts.messages_resolved, 'work reported done', 'resolved'),
        tile('agents', counts.agents, 'registered identities'),
        tile('active grants', counts.grants_active, 'authority in force'),
        tile('assignments', counts.assignments_open, 'work with an owner'),
        tile('discoveries', counts.discoveries, 'shared findings')
      ]),
      unresolved.length ? el('div', { class: 'panel' }, [
        el('h3', {}, 'Needs adjudication'),
        el('p', { class: 'muted small' },
          'Derived from delivery state only. Case records live in Ops, not here.'),
        el('ul', { class: 'plain-list' }, unresolved.slice(0, 6).map(function (m) {
          return el('li', { class: 'unresolved-row' }, [
            el('button', {
              class: 'linklike', onclick: function () { actions.openMessage(m.id); }
            }, core.truncate(m.subject || '(no subject)', 70)),
            el('span', { class: 'muted small' }, ' — ' + core.unresolvedReason(m, now))
          ]);
        })),
        dom.link('Open Ops adjudication', adjudicationHref(state), { newTab: true, class: 'btn btn-ghost' })
      ]) : null,
      el('div', { class: 'panel' }, [
        el('h3', {}, 'Latest traffic'),
        msgs.length ? el('ul', { class: 'msg-list compact' }, msgs.map(function (m) {
          return messageRow(m, state, actions);
        })) : dom.empty('No messages yet.')
      ])
    ]);
  }
  views.overview = overview;

  /* ---------------------------------------------------------------- inbox */

  function authorityChip(message, state) {
    var a = message.authority;
    if (message.kind !== 'instruction') return null;
    if (!a) return chip('no authority record', 'danger', 'An instruction without recorded authority.');
    var ids = a.grant_ids || (a.grant_id ? [a.grant_id] : []);
    if (a.allowed === false || a.valid === false) {
      return chip('authority invalid', 'danger', 'The recorded grant chain no longer authorizes this.');
    }
    return chip('authorized', 'ok',
      'Grant chain: ' + (ids.length ? ids.join(' <- ') : 'recorded on the message'));
  }
  views.authorityChip = authorityChip;

  function messageRow(m, state, actions) {
    var now = state.nowMs || Date.now();
    var st = core.deliveryState(m);
    var selected = state.selectedMessageId === m.id;
    var reason = core.unresolvedReason(m, now);
    var superseded = !!(m.superseded || m.superseded_at || core.deliveryStates(m).indexOf('superseded') !== -1 ||
      (m.delivery && (m.delivery.superseded || m.delivery.state === 'superseded')));
    return el('li', {
      class: 'msg-row' + (selected ? ' selected' : '') + (reason ? ' flagged' : '') +
        (superseded ? ' superseded' : ''),
      dataset: { id: m.id || '', kind: m.kind || '' }
    }, [
      el('button', {
        class: 'msg-hit',
        onclick: function () { actions.openMessage(m.id); }
      }, [
        el('div', { class: 'msg-line1' }, [
          kindChip(m.kind),
          el('span', { class: 'msg-subject' }, m.subject || '(no subject)'),
          // one chip per distinct delivery state: a message to three agents can
          // be resolved by one and still queued for another
          el('span', { class: 'chips' }, core.deliveryStates(m).map(stateChip)),
          superseded ? chip('superseded', 'neutral', 'Replaced by a later notice.') : null
        ]),
        el('div', { class: 'msg-line2' }, [
          el('span', { class: 'msg-from' }, core.senderOf(m) || 'unknown sender'),
          el('span', { class: 'arrow' }, '→'),
          el('span', { class: 'msg-to' }, core.recipientsOf(m).join(', ') || 'unknown recipient'),
          scopeChip(m.scope),
          m.work_id ? chip(m.work_id, 'work') : null,
          authorityChip(m, state),
          timeEl(m.created_at || m.sent_at, now)
        ]),
        el('div', { class: 'msg-preview' }, core.truncate(m.body || '', 160))
      ])
    ]);
  }
  views.messageRow = messageRow;

  function grantChainList(state, grantIds) {
    var grants = (state.snapshot && state.snapshot.grants) || [];
    var now = state.nowMs || Date.now();
    var ids = grantIds || [];
    if (!ids.length) return dom.empty('No grant recorded.');
    return el('div', { class: 'chains' }, ids.map(function (gid) {
      var info = core.grantChain(grants, gid, now);
      if (!info.chain.length) {
        return el('div', { class: 'chain chain-missing' }, [
          mono(gid), el('span', { class: 'muted' }, ' — not in this snapshot')
        ]);
      }
      return el('div', { class: 'chain' + (info.effective ? '' : ' chain-broken') },
        info.chain.map(function (g, i) {
          return el('div', { class: 'chain-node' }, [
            el('div', { class: 'chain-rank' }, i === info.chain.length - 1 ? 'root' : 'level ' + (i + 1)),
            el('div', { class: 'chain-body' }, [
              el('div', {}, [
                mono(g.issuer || 'unknown'),
                el('span', { class: 'arrow' }, '→'),
                mono(g.grantee || 'unknown')
              ]),
              el('div', { class: 'chips' }, [scopeChip(g.scope)].concat(actionChips(g.actions), [
                g.delegable ? chip('delegable', 'ok') : chip('terminal', 'neutral'),
                g.revoked_at ? chip('revoked', 'danger') : null,
                (g.expires_at && core.isExpired(g.expires_at, now)) ? chip('expired', 'danger') : null,
                idChip(g.id, 'id')
              ]))
            ])
          ]);
        }).concat([
          info.cycle ? el('div', { class: 'chain-note danger' }, 'Cycle detected — refused.') : null,
          info.missing ? el('div', { class: 'chain-note danger' },
            'Parent ' + info.missing + ' is not visible in this snapshot.') : null,
          info.broken ? el('div', { class: 'chain-note danger' },
            'Chain broken at ' + info.broken.id + ' (' + info.broken.reason + ').') : null
        ]));
    }));
  }
  views.grantChainList = grantChainList;

  function messageDetail(state, actions) {
    var m = state.selectedMessage;
    if (!m) return el('div', { class: 'detail detail-empty' }, dom.empty('Select a message.'));
    var now = state.nowMs || Date.now();
    var st = core.deliveryState(m);
    var reason = core.unresolvedReason(m, now);
    var auth = m.authority || {};
    var grantIds = auth.grant_ids || (auth.grant_id ? [auth.grant_id] : []);
    // A superseded ownership notice is history, not work to act on.
    var superseded = !!(m.superseded || m.superseded_at || core.deliveryStates(m).indexOf('superseded') !== -1 ||
      (m.delivery && (m.delivery.superseded || m.delivery.state === 'superseded')));
    // The hub requires the current lease to acknowledge a leased delivery.
    var leaseMissing = st === 'leased' && !(m.delivery && m.delivery.lease_id);
    var notRecipient = state.principal && core.recipientsOf(m).indexOf(state.principal) === -1;
    var authorityEnded = m.kind === 'instruction' && auth.allowed === false;

    return el('div', { class: 'detail' }, [
      el('div', { class: 'detail-head' }, [
        el('h3', {}, m.subject || '(no subject)'),
        el('div', { class: 'chips' }, [
          kindChip(m.kind), stateChip(st), scopeChip(m.scope),
          m.work_id ? chip(m.work_id, 'work') : null, idChip(m.id, 'msg')
        ])
      ]),
      superseded ? el('div', { class: 'banner banner-warn' }, [
        el('div', { class: 'banner-body' }, [
          el('strong', {}, 'Superseded. '),
          el('span', {}, 'A later notice replaced this one; it is no longer actionable.')
        ])
      ]) : null,
      reason ? escalationBanner(state, reason, 'Message ' + (m.id || '')) : null,
      el('div', { class: 'fields' }, [
        field('From', mono(core.senderOf(m) || '—')),
        field('To', core.recipientsOf(m).join(', ')),
        field('Sent', timeEl(m.created_at || m.sent_at, now)),
        field('Expires', m.expires_at ? timeEl(m.expires_at, now) : null),
        field('Lease', m.delivery && m.delivery.lease_id ? mono(m.delivery.lease_id) : null),
        field('Attempts', m.delivery && m.delivery.attempts != null ? String(m.delivery.attempts) : null),
        field('Reply to', m.reply_to ? mono(m.reply_to) : null),
        field('Receipt', m.delivery && m.delivery.receipt_ref ? mono(m.delivery.receipt_ref) : null)
      ]),
      m.kind === 'instruction' ? el('div', { class: 'panel authority-panel' }, [
        el('h4', {}, 'Authority evidence'),
        el('p', { class: 'muted small' },
          'A binding instruction is only binding while this chain authorizes it. ' +
          'Prose claiming authority is not authority.'),
        grantChainList(state, grantIds)
      ]) : null,
      el('div', { class: 'panel' }, [
        el('h4', {}, 'Body'),
        el('pre', { class: 'body-text' }, String(m.body == null ? '' : m.body))
      ]),
      (m.artifacts && m.artifacts.length) ? el('div', { class: 'panel' }, [
        el('h4', {}, 'Artifacts'),
        el('ul', { class: 'artifact-list' }, m.artifacts.map(function (a) {
          // dom.link degrades an unsafe ref to inert, struck-through text so the
          // owner still sees exactly what the sender wrote.
          return el('li', {}, dom.link(String(a), String(a), { newTab: true }));
        }))
      ]) : null,
      el('div', { class: 'detail-actions' }, [
        button('Acknowledge', {
          variant: 'primary',
          disabled: st === 'acknowledged' || st === 'resolved' || superseded || leaseMissing || notRecipient || authorityEnded,
          title: superseded ? 'Superseded: a later notice replaced this one.'
            : leaseMissing ? 'This delivery is leased by its recipient; only that lease may acknowledge it.'
              : 'Only the recipient may acknowledge; the hub enforces this.',
          onclick: function () { actions.ack(m, 'acknowledged'); }
        }),
        button('Mark resolved', {
          disabled: st === 'resolved' || superseded || leaseMissing || notRecipient || authorityEnded,
          title: superseded ? 'Superseded: a later notice replaced this one.' : null,
          onclick: function () { actions.ack(m, 'resolved'); }
        }),
        button('Reply', { onclick: function () { actions.replyTo(m); } }),
        button('Assign work', {
          onclick: function () { actions.assignFromMessage(m); },
          title: 'Open the assignment form pre-filled from this message.'
        })
      ])
    ]);
  }
  views.messageDetail = messageDetail;

  function composer(state, actions) {
    var agents = (state.snapshot && state.snapshot.agents) || [];
    var grants = (state.snapshot && state.snapshot.grants) || [];
    var now = state.nowMs || Date.now();
    var draft = state.draft || {};
    var issuerGrants = core.usableIssuerGrants(grants, state.principal, 'instructions.issue', null, now);

    var f = form({
      class: 'composer',
      title: 'Send a message',
      description: 'An instruction is binding on the recipient runtime and must cite a live grant.',
      submitLabel: 'Send',
      fields: [
        {
          name: 'to', label: 'To', type: 'select', required: true, value: draft.to,
          options: [{ value: '', label: '— choose an agent —' }].concat(agents.map(function (a) {
            return { value: a.agent_id, label: a.display_name ? (a.display_name + ' (' + a.agent_id + ')') : a.agent_id };
          }))
        },
        { name: 'to_extra', label: 'Additional recipients', placeholder: 'agent-b, agent-c', hint: 'Comma separated agent ids.' },
        { name: 'kind', label: 'Kind', type: 'select', required: true, value: draft.kind || 'information', options: core.MESSAGE_KINDS },
        { name: 'scope', label: 'Scope', required: true, value: draft.scope || '/', placeholder: '/project/foo' },
        { name: 'subject', label: 'Subject', required: true, wide: true, value: draft.subject || '', maxlength: core.LIMITS.SUBJECT_CHARS },
        { name: 'body', label: 'Body', type: 'textarea', required: true, wide: true, rows: 8, value: draft.body || '', counter: true },
        { name: 'work_id', label: 'Work id', value: draft.work_id || '' },
        { name: 'reply_to', label: 'Reply to message', value: draft.reply_to || '' },
        { name: 'artifacts', label: 'Artifacts', placeholder: 'path or URL per line', type: 'textarea', rows: 2 },
        { name: 'expires_at', label: 'Expires', type: 'datetime-local' },
        {
          name: 'authority_grant_id', label: 'Authority grant', type: 'select',
          value: draft.authority_grant_id || '',
          hint: issuerGrants.length
            ? 'Required for instructions. Only your live instructions.issue grants are listed.'
            : 'You hold no live instructions.issue grant. Issue or receive one first.',
          options: [{ value: '', label: '— none —' }].concat(issuerGrants.map(function (g) {
            return { value: g.id, label: g.scope + ' · ' + (g.actions || []).join(',') + ' · ' + core.truncate(g.id, 12) };
          }))
        }
      ],
      onSubmit: function (values, node) {
        var to = [values.to].concat(core.linesFrom(values.to_extra)).filter(Boolean);
        var payload = {
          to: to,
          kind: values.kind,
          subject: values.subject,
          body: values.body,
          scope: values.scope,
          work_id: values.work_id || undefined,
          reply_to: values.reply_to || undefined,
          artifacts: core.linesFrom(values.artifacts),
          expires_at: core.isoFromLocalInput(values.expires_at) || undefined,
          authority_grant_id: values.authority_grant_id || undefined
        };
        var check = core.validateMessageDraft(payload);
        if (!check.ok) { node.showErrors(check.errors); return; }
        node.showErrors([]);
        payload.to = check.to;
        payload.scope = check.scope;
        if (!payload.artifacts.length) delete payload.artifacts;
        actions.sendMessage(payload, node);
      }
    }, { idSuffix: 'compose' });

    var bodyInput = f.inputs.body.node;
    var counter = el('div', { class: 'byte-counter' }, '0 / ' + core.LIMITS.BODY_BYTES + ' bytes');
    function updateCounter() {
      var n = core.byteLength(bodyInput.value || '');
      dom.mount(counter, n + ' / ' + core.LIMITS.BODY_BYTES + ' bytes');
      counter.className = 'byte-counter' + (n > core.LIMITS.BODY_BYTES ? ' over' : '');
    }
    bodyInput.addEventListener('input', updateCounter);
    updateCounter();

    return el('div', { class: 'panel composer-panel' }, [f, counter]);
  }
  views.composer = composer;

  function inbox(state, actions) {
    var now = state.nowMs || Date.now();
    var all = (state.snapshot && state.snapshot.messages) || [];
    var filtered = core.sortMessages(core.filterMessages(all, {
      kind: state.filters.kind, state: state.filters.state,
      scope: state.filters.scope, query: state.filters.query,
      agent: state.filters.agent, unresolvedOnly: state.filters.unresolvedOnly, nowMs: now
    }));
    var counts = core.deliveryCounts(all);

    function filterButton(label, key, value) {
      var active = state.filters[key] === value || (!state.filters[key] && !value);
      return button(label, {
        variant: active ? 'chipactive' : 'chip',
        onclick: function () { actions.setFilter(key, value); }
      });
    }

    return el('section', { class: 'view view-inbox' }, [
      el('div', { class: 'view-head' }, [
        el('h2', {}, 'Inbox'),
        el('div', { class: 'view-head-meta' }, [
          chip(counts.queued + ' queued', 'queued'),
          chip(counts.leased + ' leased', 'leased'),
          chip(counts.acknowledged + ' acknowledged', 'ack'),
          chip(counts.resolved + ' resolved', 'resolved'),
          button('Poll my inbox', {
            onclick: actions.poll, variant: 'ghost',
            title: 'Claim messages addressed to this credential’s principal.'
          }),
          button('Refresh', { onclick: actions.refresh, variant: 'ghost' })
        ])
      ]),
      el('div', { class: 'filter-bar' }, [
        el('div', { class: 'filter-group' }, [el('span', { class: 'filter-label' }, 'kind')].concat(
          [filterButton('all', 'kind', null)],
          core.MESSAGE_KINDS.map(function (k) { return filterButton(k, 'kind', k); })
        )),
        el('div', { class: 'filter-group' }, [el('span', { class: 'filter-label' }, 'state')].concat(
          [filterButton('all', 'state', null)],
          core.DELIVERY_STATES.map(function (s) { return filterButton(s, 'state', s); })
        )),
        el('div', { class: 'filter-group' }, [
          el('span', { class: 'filter-label' }, 'scope'),
          el('input', {
            id: 'filter-scope',
            class: 'filter-input', type: 'text', value: state.filters.scope || '',
            placeholder: '/', 'aria-label': 'scope filter',
            oninput: function (ev) { actions.setFilter('scope', ev.target.value); }
          }),
          el('input', {
            id: 'filter-query',
            class: 'filter-input', type: 'search', value: state.filters.query || '',
            placeholder: 'search text', 'aria-label': 'text search',
            oninput: function (ev) { actions.setFilter('query', ev.target.value); }
          }),
          button(state.filters.unresolvedOnly ? 'unresolved only ✓' : 'unresolved only', {
            variant: state.filters.unresolvedOnly ? 'chipactive' : 'chip',
            onclick: function () { actions.setFilter('unresolvedOnly', !state.filters.unresolvedOnly); }
          })
        ])
      ]),
      el('div', { class: 'split' }, [
        el('div', { class: 'split-list' }, [
          el('div', { class: 'list-count muted small' },
            filtered.length + ' of ' + all.length + ' messages'),
          filtered.length
            ? el('ul', { class: 'msg-list' }, filtered.map(function (m) {
              return messageRow(m, state, actions);
            }))
            : dom.empty('Nothing matches these filters.')
        ]),
        el('div', { class: 'split-detail' }, [
          state.composing ? composer(state, actions) : messageDetail(state, actions)
        ])
      ]),
      el('div', { class: 'view-foot' }, [
        button(state.composing ? 'Close composer' : 'New message', {
          variant: 'primary', onclick: actions.toggleComposer
        })
      ])
    ]);
  }
  views.inbox = inbox;

  /* ------------------------------------------------------------ discovery */

  function discovery(state, actions) {
    var now = state.nowMs || Date.now();
    var results = state.discoveryResults || [];

    var searchForm = form({
      class: 'search-form',
      title: 'Search shared work',
      submitLabel: 'Search',
      fields: [
        { name: 'query', label: 'Text', value: state.discoveryQuery || '', placeholder: 'what are you looking for?' },
        { name: 'scope', label: 'Scope', value: state.discoveryScope || '/', placeholder: '/' },
        { name: 'topics', label: 'Topics', placeholder: 'comma separated' },
        { name: 'limit', label: 'Limit', type: 'number', value: String(state.discoveryLimit || 20) }
      ],
      onSubmit: function (values, node) {
        var sc = core.tryCanonicalScope(values.scope || '/');
        if (!sc.ok) { node.showErrors([sc.error]); return; }
        node.showErrors([]);
        actions.searchDiscoveries({
          query: values.query || undefined,
          scope: sc.scope,
          topics: core.topicsFrom(values.topics),
          limit: core.clampLimit(values.limit, 20)
        });
      }
    }, { idSuffix: 'search' });

    var publishForm = form({
      class: 'publish-form',
      title: 'Publish a discovery',
      description: 'Discoveries stay here as searchable shared work. Nothing is copied into another memory store.',
      submitLabel: 'Publish',
      fields: [
        { name: 'title', label: 'Title', required: true, wide: true },
        { name: 'body', label: 'What was found', type: 'textarea', rows: 6, required: true, wide: true },
        { name: 'scope', label: 'Scope', required: true, value: '/' },
        { name: 'topics', label: 'Topics', required: true, placeholder: 'sqlite, leases' },
        { name: 'artifacts', label: 'Artifacts', type: 'textarea', rows: 2, placeholder: 'one ref per line' },
        { name: 'work_id', label: 'Work id' },
        { name: 'expires_at', label: 'Expires', type: 'datetime-local' }
      ],
      onSubmit: function (values, node) {
        var draft = {
          title: values.title, body: values.body, scope: values.scope,
          topics: values.topics, artifacts: values.artifacts
        };
        var check = core.validateDiscoveryDraft(draft);
        if (!check.ok) { node.showErrors(check.errors); return; }
        node.showErrors([]);
        var payload = {
          title: values.title, body: values.body, scope: check.scope,
          topics: check.topics, artifacts: check.artifacts
        };
        if (values.work_id) payload.work_id = values.work_id;
        var exp = core.isoFromLocalInput(values.expires_at);
        if (exp) payload.expires_at = exp;
        actions.publishDiscovery(payload, node);
      }
    }, { idSuffix: 'publish' });

    return el('section', { class: 'view view-discovery' }, [
      el('div', { class: 'view-head' }, [el('h2', {}, 'Discovery')]),
      el('div', { class: 'two-col' }, [
        el('div', {}, [
          el('div', { class: 'panel' }, [searchForm]),
          el('div', { class: 'panel' }, [
            el('h3', {}, 'Results'),
            el('div', { class: 'muted small' },
              results.length + ' item' + (results.length === 1 ? '' : 's')),
            results.length ? el('ul', { class: 'discovery-list' }, results.map(function (d) {
              return el('li', { class: 'discovery-row' }, [
                el('div', { class: 'discovery-title' }, d.title || '(untitled)'),
                el('div', { class: 'chips' }, [scopeChip(d.scope)]
                  .concat((d.topics || []).slice(0, 8).map(function (t) { return chip(t, 'topic'); }))
                  .concat([timeEl(d.created_at, now), idChip(d.id, 'id')])),
                el('pre', { class: 'body-text small' }, String(d.body == null ? '' : d.body)),
                (d.artifacts && d.artifacts.length) ? el('ul', { class: 'artifact-list' },
                  d.artifacts.map(function (a) {
                    return el('li', {}, dom.link(String(a), String(a), { newTab: true }));
                  })) : null
              ]);
            })) : dom.empty('No discoveries for that search yet.')
          ])
        ]),
        el('div', {}, [el('div', { class: 'panel' }, [publishForm])])
      ])
    ]);
  }
  views.discovery = discovery;

  /* --------------------------------------------------------------- grants */

  function grantRow(g, state, actions) {
    var now = state.nowMs || Date.now();
    var grants = (state.snapshot && state.snapshot.grants) || [];
    var active = core.isGrantActive(g, now);
    var effective = core.isGrantEffective(grants, g.id, now);
    return el('tr', {
      class: 'grant-row' + (active ? '' : ' inactive') +
        (state.selectedGrantId === g.id ? ' selected' : ''),
      dataset: { id: g.id || '' }
    }, [
      el('td', {}, el('button', {
        class: 'linklike', onclick: function () { actions.selectGrant(g.id); }
      }, core.truncate(g.id || '', 14))),
      el('td', {}, mono(g.issuer || '—')),
      el('td', {}, mono(g.grantee || '—')),
      el('td', {}, scopeChip(g.scope)),
      el('td', {}, el('div', { class: 'chips' }, actionChips(g.actions))),
      el('td', {}, g.delegable ? chip('yes', 'ok') : chip('no', 'neutral')),
      el('td', {}, g.expires_at ? timeEl(g.expires_at, now) : el('span', { class: 'muted' }, 'never')),
      el('td', {}, g.revoked_at
        ? chip('revoked', 'danger', core.formatTs(g.revoked_at))
        : !active ? chip('expired', 'danger')
          : effective ? chip('active', 'ok')
            : chip('chain broken', 'danger',
              'This grant is not revoked, but an ancestor is revoked or expired, ' +
              'so it authorizes nothing.')),
      el('td', {}, el('div', { class: 'row-actions' }, [
        button('Delegate', {
          variant: 'ghost',
          disabled: !effective || !g.delegable,
          title: !g.delegable ? 'This grant is not delegable.'
            : !effective ? 'This grant no longer authorizes anything.'
              : 'Issue a child grant within this grant’s powers.',
          onclick: function () { actions.startDelegation(g.id); }
        }),
        button('Revoke', {
          variant: 'danger', disabled: !!g.revoked_at,
          onclick: function () { actions.revokeGrant(g); }
        })
      ]))
    ]);
  }

  function grantForm(state, actions) {
    var grants = (state.snapshot && state.snapshot.grants) || [];
    var agents = (state.snapshot && state.snapshot.agents) || [];
    var now = state.nowMs || Date.now();
    var parentId = state.delegationParentId || '';
    var parent = parentId ? core.indexGrants(grants)[parentId] : null;
    var mine = core.usableIssuerGrants(grants, state.principal, null, null, now)
      .filter(function (g) { return g.delegable; });

    var f = form({
      class: 'grant-form',
      title: parent ? 'Delegate from an existing grant' : 'Issue a grant',
      description: parent
        ? 'The child grant cannot exceed the source grant in scope, actions, delegability or lifetime.'
        : 'A grant issued from your own delegable authority. Authenticated grants change what the grantee may actually do.',
      submitLabel: parent ? 'Delegate' : 'Issue grant',
      fields: [
        {
          name: 'parent_grant_id', label: 'Source grant', type: 'select', value: parentId,
          hint: 'Leave empty to issue directly from your own seed authority.',
          options: [{ value: '', label: '— my own authority —' }].concat(mine.map(function (g) {
            return { value: g.id, label: g.scope + ' · ' + (g.actions || []).join(',') + ' · ' + core.truncate(g.id, 10) };
          })),
          onchange: function (ev) { actions.startDelegation(ev.target.value || null); }
        },
        {
          name: 'grantee', label: 'Grantee', required: true, type: 'select',
          options: [{ value: '', label: '— choose an agent —' }].concat(agents.map(function (a) {
            return { value: a.agent_id, label: a.agent_id + (a.display_name ? ' — ' + a.display_name : '') };
          }))
        },
        { name: 'grantee_other', label: 'Or type an agent id', placeholder: 'agent-id' },
        {
          name: 'scope', label: 'Scope', required: true,
          value: parent ? parent.scope : '/',
          hint: parent ? 'Must sit inside ' + parent.scope : 'Use / for everything.'
        },
        {
          name: 'actions', label: 'Actions', required: true,
          value: parent ? (parent.actions || []).join(', ') : 'messages.send, instructions.issue',
          hint: parent
            ? 'Must be covered by ' + ((parent.actions || []).join(', ') || 'nothing')
            : 'Comma separated. * means every action.'
        },
        { name: 'delegable', label: 'Grantee may delegate further', type: 'checkbox', value: parent ? !!parent.delegable : true },
        { name: 'expires_at', label: 'Expires', type: 'datetime-local' },
        { name: 'reason', label: 'Reason', wide: true, placeholder: 'why this authority is being granted' }
      ],
      onSubmit: function (values, node) {
        var grantee = (values.grantee_other || '').trim() || values.grantee;
        var draft = {
          grantee: grantee,
          scope: values.scope,
          actions: values.actions,
          delegable: !!values.delegable,
          expires_at: core.isoFromLocalInput(values.expires_at) || undefined
        };
        var check = core.validateDelegation(parent, draft, now, grants);
        if (!check.ok) { node.showErrors(check.errors); return; }
        node.showErrors([]);
        var payload = {
          grantee: grantee,
          scope: check.scope,
          actions: check.actions,
          delegable: !!values.delegable
        };
        if (values.parent_grant_id) payload.parent_grant_id = values.parent_grant_id;
        if (draft.expires_at) payload.expires_at = draft.expires_at;
        if (values.reason) payload.reason = values.reason;
        actions.issueGrant(payload, node);
      },
      onCancel: parent ? function () { actions.startDelegation(null); } : null
    }, { idSuffix: 'grant' });
    return el('div', { class: 'panel' }, [f]);
  }

  function authorityProbe(state, actions) {
    var agents = (state.snapshot && state.snapshot.agents) || [];
    var probe = state.probe || {};
    var f = form({
      class: 'probe-form',
      title: 'Authority probe',
      description: 'Asks the hub the same question it asks itself before allowing an action.',
      submitLabel: 'Check',
      fields: [
        {
          name: 'agent_id', label: 'Agent', type: 'select', value: probe.agent_id || '',
          options: [{ value: '', label: '— me —' }].concat(agents.map(function (a) {
            return { value: a.agent_id, label: a.agent_id };
          }))
        },
        { name: 'action', label: 'Action', required: true, value: probe.action || 'instructions.issue' },
        { name: 'scope', label: 'Scope', required: true, value: probe.scope || '/' }
      ],
      onSubmit: function (values, node) {
        var sc = core.tryCanonicalScope(values.scope);
        if (!sc.ok) { node.showErrors([sc.error]); return; }
        if (!values.action.trim()) { node.showErrors(['Name an action.']); return; }
        node.showErrors([]);
        var payload = { action: values.action.trim(), scope: sc.scope };
        if (values.agent_id) payload.agent_id = values.agent_id;
        actions.runProbe(payload);
      }
    }, { idSuffix: 'probe' });

    var result = null;
    if (probe.result) {
      result = el('div', {
        class: 'banner ' + (probe.result.allowed ? 'banner-ok' : 'banner-error')
      }, [
        el('strong', {}, probe.result.allowed ? 'Allowed' : 'Denied'),
        el('span', {}, ' — ' + (probe.asked ? probe.asked.action + ' on ' + probe.asked.scope : '')),
        (probe.result.grant_ids && probe.result.grant_ids.length)
          ? el('div', { class: 'chips' }, probe.result.grant_ids.map(function (id) {
            return chip(core.truncate(id, 14), 'id', id);
          }))
          : el('div', { class: 'muted small' }, 'No grant satisfied it.')
      ]);
    }
    return el('div', { class: 'panel' }, [f, result]);
  }
  views.authorityProbe = authorityProbe;

  function grantsView(state, actions) {
    var grants = (state.snapshot && state.snapshot.grants) || [];
    var now = state.nowMs || Date.now();
    var shown = grants.filter(function (g) {
      if (state.filters.grantAgent &&
        g.grantee !== state.filters.grantAgent && g.issuer !== state.filters.grantAgent) return false;
      if (!state.filters.showRevoked && !core.isGrantEffective(grants, g.id, now)) return false;
      return true;
    });
    var rows = shown.map(function (g) { return grantRow(g, state, actions); });

    return el('section', { class: 'view view-grants' }, [
      el('div', { class: 'view-head' }, [
        el('h2', {}, 'Grants & delegation'),
        el('div', { class: 'view-head-meta' }, [
          chip(core.effectiveGrants(grants, now).length + ' active', 'ok',
            'Grants whose whole chain still authorizes.'),
          chip(grants.filter(function (g) { return !!g.revoked_at; }).length + ' revoked', 'danger'),
          button(state.filters.showRevoked ? 'hide inactive' : 'show inactive', {
            variant: 'chip',
            onclick: function () { actions.setFilter('showRevoked', !state.filters.showRevoked); }
          }),
          button('Refresh', { onclick: actions.refresh, variant: 'ghost' })
        ])
      ]),
      el('div', { class: 'panel' }, [
        dom.table(
          ['id', 'issuer', 'grantee', 'scope', 'actions', 'delegable', 'expires', 'status', ''],
          rows
        )
      ]),
      state.selectedGrantId ? el('div', { class: 'panel' }, [
        el('h3', {}, 'Chain for ' + core.truncate(state.selectedGrantId, 18)),
        grantChainList(state, [state.selectedGrantId])
      ]) : null,
      el('div', { class: 'two-col' }, [
        grantForm(state, actions),
        authorityProbe(state, actions)
      ])
    ]);
  }
  views.grants = grantsView;

  /* ---------------------------------------------------------- assignments */

  function assignmentRow(a, state, actions) {
    var now = state.nowMs || Date.now();
    var version = core.assignmentVersion(a);
    var history = core.priorOwners(a);
    var open = state.expandedAssignment === a.work_id;
    return el('tbody', { class: 'assignment-group' }, [
      el('tr', { class: 'assignment-row' + (open ? ' open' : '') }, [
        el('td', {}, el('button', {
          class: 'linklike',
          onclick: function () { actions.toggleAssignment(a.work_id); }
        }, a.work_id || '(no work id)')),
        el('td', {}, mono(a.assignee || '—')),
        el('td', {}, scopeChip(a.scope)),
        el('td', {}, version == null ? el('span', { class: 'muted' }, '—') : chip('v' + version, 'version')),
        el('td', {}, core.truncate(a.summary || '', 80)),
        el('td', {}, timeEl(a.updated_at || a.created_at, now)),
        el('td', {}, button('Reassign', {
          variant: 'ghost', onclick: function () { actions.startReassign(a); }
        }))
      ]),
      open ? el('tr', { class: 'assignment-detail' }, [
        el('td', { colspan: '7' }, el('div', { class: 'assignment-detail-body' }, [
          el('div', { class: 'fields' }, [
            field('Summary', a.summary, { wide: true }),
            field('Scope', a.scope, { mono: true }),
            field('Grant', a.grant_id ? mono(a.grant_id) : null),
            field('Assigned by', a.assigned_by ? mono(a.assigned_by) : null),
            field('Created', timeEl(a.created_at, now))
          ]),
          el('h4', {}, 'Ownership history'),
          history.length ? el('ol', { class: 'history' }, history.map(function (h) {
            return el('li', {}, [
              mono(h.assignee || h.owner || 'unknown'),
              h.version != null ? chip('v' + h.version, 'version') : null,
              h.at || h.changed_at ? timeEl(h.at || h.changed_at, now) : null,
              h.reason ? el('span', { class: 'muted small' }, ' ' + h.reason) : null
            ]);
          })) : dom.empty('No prior owners recorded.'),
          a.grant_id ? el('div', {}, [
            el('h4', {}, 'Authority behind this assignment'),
            grantChainList(state, [a.grant_id])
          ]) : null
        ]))
      ]) : null
    ]);
  }

  function assignmentForm(state, actions) {
    var agents = (state.snapshot && state.snapshot.agents) || [];
    var grants = (state.snapshot && state.snapshot.grants) || [];
    var now = state.nowMs || Date.now();
    var target = state.reassignTarget || null;
    var prefill = state.assignmentDraft || {};
    var action = target ? 'reassign' : 'assign';
    var usable = core.usableIssuerGrants(grants, state.principal,
      target ? 'assignments.reassign' : 'assignments.assign', null, now);

    var fields = [
      { name: 'work_id', label: 'Work id', required: true, value: target ? target.work_id : (prefill.work_id || '') },
      {
        name: 'assignee', label: 'Assignee', required: true, type: 'select',
        value: prefill.assignee || '',
        options: [{ value: '', label: '— choose an agent —' }].concat(agents.map(function (a) {
          return { value: a.agent_id, label: a.agent_id + (a.display_name ? ' — ' + a.display_name : '') };
        }))
      },
      { name: 'assignee_other', label: 'Or type an agent id' },
      { name: 'scope', label: 'Scope', required: true, value: target ? target.scope : (prefill.scope || '/') },
      { name: 'summary', label: 'Summary', required: true, wide: true, value: target ? target.summary : (prefill.summary || '') },
      {
        name: 'grant_id', label: 'Authority grant', type: 'select',
        hint: usable.length ? 'Cited on the ownership-change notice.'
          : 'No live ' + (target ? 'assignments.reassign' : 'assignments.assign') + ' grant found for you.',
        options: [{ value: '', label: '— none —' }].concat(usable.map(function (g) {
          return { value: g.id, label: g.scope + ' · ' + core.truncate(g.id, 12) };
        }))
      }
    ];
    if (target) {
      fields.push({
        name: 'expected_version', label: 'Expected version', required: true,
        type: 'number', value: String(core.assignmentVersion(target) == null ? '' : core.assignmentVersion(target)),
        hint: 'The reassignment is refused if someone else moved it first.'
      });
    }

    var f = form({
      class: 'assignment-form',
      title: target ? 'Reassign ' + target.work_id : 'Assign work',
      description: target
        ? 'Ownership change is delivered durably to both the old and the new owner.'
        : 'Assignment delivers a binding ownership notice inside the same transaction.',
      submitLabel: target ? 'Reassign' : 'Assign',
      fields: fields,
      onCancel: target ? function () { actions.startReassign(null); } : null,
      onSubmit: function (values, node) {
        var assignee = (values.assignee_other || '').trim() || values.assignee;
        var draft = {
          work_id: values.work_id, assignee: assignee, scope: values.scope,
          summary: values.summary, expected_version: values.expected_version
        };
        var check = core.validateAssignmentDraft(draft, !!target);
        if (!check.ok) { node.showErrors(check.errors); return; }
        node.showErrors([]);
        var payload = {
          work_id: values.work_id, assignee: assignee,
          scope: check.scope, summary: values.summary
        };
        if (values.grant_id) payload.grant_id = values.grant_id;
        if (target) payload.expected_version = parseInt(values.expected_version, 10);
        actions.submitAssignment(payload, !!target, node);
      }
    }, { idSuffix: action });
    return el('div', { class: 'panel' }, [f]);
  }

  function assignments(state, actions) {
    var list = (state.snapshot && state.snapshot.assignments) || [];
    var filtered = list.filter(function (a) {
      if (!state.filters.scope || state.filters.scope === '/') return true;
      return core.scopeContains(state.filters.scope, a.scope || '/');
    });
    return el('section', { class: 'view view-assignments' }, [
      el('div', { class: 'view-head' }, [
        el('h2', {}, 'Assignments'),
        el('div', { class: 'view-head-meta' }, [
          chip(filtered.length + ' tracked', 'neutral'),
          button('Refresh', { onclick: actions.refresh, variant: 'ghost' })
        ])
      ]),
      el('div', { class: 'panel' }, [
        el('div', { class: 'table-wrap' }, [
          el('table', { class: 'table' }, [
            el('thead', {}, el('tr', {}, ['work', 'owner', 'scope', 'version', 'summary', 'updated', '']
              .map(function (h) { return el('th', {}, h); })))
          ].concat(filtered.length
            ? filtered.map(function (a) { return assignmentRow(a, state, actions); })
            : [el('tbody', {}, el('tr', {}, el('td', { colspan: '7' },
              dom.empty('No assignments yet.'))))]))
        ])
      ]),
      assignmentForm(state, actions)
    ]);
  }
  views.assignments = assignments;

  /* --------------------------------------------------------------- agents */

  function agents(state, actions) {
    var snap = state.snapshot || {};
    var list = snap.agents || [];
    var now = state.nowMs || Date.now();
    var counts = core.agentDeliveryCounts(snap.messages || [], list);
    var grants = snap.grants || [];

    var rows = list.map(function (a) {
      var c = counts[a.agent_id] || {};
      var held = core.grantsFor(grants, a.agent_id, now);
      return el('tr', { class: 'agent-row' }, [
        el('td', {}, [
          el('div', { class: 'agent-name' }, a.display_name || a.agent_id),
          el('div', { class: 'muted small mono' }, a.agent_id)
        ]),
        el('td', {}, chip(a.runtime || 'unknown', 'runtime')),
        el('td', {}, mono(a.machine || '—')),
        el('td', {}, timeEl(a.last_seen, now)),
        el('td', {}, chip(a.status || 'unknown', a.status === 'online' ? 'ok' : 'neutral')),
        el('td', {}, el('div', { class: 'chips' }, [
          chip((c.queued || 0) + ' queued', 'queued'),
          chip((c.leased || 0) + ' leased', 'leased'),
          chip((c.acknowledged || 0) + ' ack', 'ack'),
          chip((c.resolved || 0) + ' resolved', 'resolved'),
          chip((c.sent || 0) + ' sent', 'neutral')
        ])),
        el('td', {}, held.length
          ? el('div', { class: 'chips' }, held.slice(0, 4).map(function (g) {
            return chip(g.scope + ' · ' + (g.actions || []).join(','), 'action', g.id);
          }).concat(held.length > 4 ? [chip('+' + (held.length - 4), 'neutral')] : []))
          : el('span', { class: 'muted' }, 'none')),
        el('td', {}, el('div', { class: 'row-actions' }, [
          button('Message', { variant: 'ghost', onclick: function () { actions.composeTo(a.agent_id); } }),
          button('Grant', { variant: 'ghost', onclick: function () { actions.grantTo(a.agent_id); } }),
          button('Filter inbox', { variant: 'ghost', onclick: function () { actions.filterByAgent(a.agent_id); } })
        ]))
      ]);
    });

    return el('section', { class: 'view view-agents' }, [
      el('div', { class: 'view-head' }, [
        el('h2', {}, 'Agents'),
        el('div', { class: 'view-head-meta' }, [
          chip(list.length + ' registered', 'neutral'),
          button('Refresh', { onclick: actions.refresh, variant: 'ghost' })
        ])
      ]),
      el('div', { class: 'panel' }, [
        dom.table(['agent', 'runtime', 'machine', 'last seen', 'status', 'deliveries', 'active grants', ''], rows)
      ])
    ]);
  }
  views.agents = agents;

  /* ---------------------------------------------------------------- shell */

  var NAV = [
    { id: 'overview', label: 'Overview' },
    { id: 'inbox', label: 'Inbox' },
    { id: 'discovery', label: 'Discovery' },
    { id: 'grants', label: 'Grants' },
    { id: 'assignments', label: 'Assignments' },
    { id: 'agents', label: 'Agents' }
  ];
  views.NAV = NAV;

  function sidebar(state, actions) {
    var counts = core.snapshotCounts(state.snapshot || {});
    return el('nav', { class: 'sidebar', 'aria-label': 'sections' }, [
      el('div', { class: 'brand small' }, [
        el('div', { class: 'brand-mark', 'aria-hidden': 'true' }, '◇'),
        el('div', {}, [
          el('div', { class: 'brand-name' }, 'Agent Inbox'),
          el('div', { class: 'muted tiny' }, 'tailnet only')
        ])
      ]),
      el('ul', { class: 'nav' }, NAV.map(function (item) {
        var badgeValue = item.id === 'inbox' ? counts.messages_queued : null;
        return el('li', {}, el('button', {
          class: 'nav-item' + (state.view === item.id ? ' active' : ''),
          'aria-current': state.view === item.id ? 'page' : null,
          onclick: function () { actions.setView(item.id); }
        }, [
          el('span', {}, item.label),
          badgeValue ? dom.badge(String(badgeValue), 'queued') : null
        ]));
      })),
      el('div', { class: 'sidebar-foot' }, [
        dom.link('Ops adjudication', adjudicationHref(state), { newTab: true, class: 'link small' }),
        el('div', { class: 'muted tiny' }, 'Unresolved cases are adjudicated in Ops.')
      ])
    ]);
  }
  views.sidebar = sidebar;

  function header(state, actions) {
    return el('header', { class: 'topbar' }, [
      el('div', { class: 'topbar-left' }, [
        el('span', { class: 'principal-label muted' }, 'connected as'),
        mono(state.principal || 'owner'),
        state.snapshot && state.snapshot.generated_at
          ? el('span', { class: 'muted small' }, ['· snapshot ', timeEl(state.snapshot.generated_at, state.nowMs)])
          : null
      ]),
      el('div', { class: 'topbar-right' }, [
        state.pending && state.pending.length
          ? chip(state.pending.length + ' in flight', 'warn',
            'Mutations sent and awaiting a definite answer.')
          : null,
        state.busy ? dom.spinner('Working…') : null,
        button('Refresh', { onclick: actions.refresh, variant: 'ghost' }),
        button('Lock', {
          variant: 'danger', onclick: actions.lock,
          title: 'Discard the credential from memory.'
        })
      ])
    ]);
  }
  views.header = header;

  function toastArea(state, actions) {
    var toasts = state.toasts || [];
    return el('div', { class: 'toasts', role: 'status', 'aria-live': 'polite' },
      toasts.map(function (t) {
        return el('div', { class: 'toast toast-' + (t.kind || 'info') }, [
          el('div', { class: 'toast-body' }, t.text),
          button('×', { variant: 'ghost', onclick: function () { actions.dismissToast(t.id); } })
        ]);
      }));
  }
  views.toastArea = toastArea;

  function body(state, actions) {
    switch (state.view) {
      case 'inbox': return inbox(state, actions);
      case 'discovery': return discovery(state, actions);
      case 'grants': return grantsView(state, actions);
      case 'assignments': return assignments(state, actions);
      case 'agents': return agents(state, actions);
      default: return overview(state, actions);
    }
  }
  views.body = body;

  function app(state, actions) {
    if (!state.connected) {
      return el('div', { class: 'app app-locked' }, [
        connectPanel(state, actions),
        toastArea(state, actions)
      ]);
    }
    return el('div', { class: 'app' }, [
      sidebar(state, actions),
      el('div', { class: 'main' }, [
        header(state, actions),
        el('div', { class: 'content' }, body(state, actions))
      ]),
      toastArea(state, actions)
    ]);
  }
  views.app = app;

  NS.views = views;
})(typeof globalThis !== 'undefined' ? globalThis : this);
