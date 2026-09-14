/* Controller: state, actions and the render loop.
 *
 * Plain JS, no framework. State is a single object; every action mutates it and
 * calls render(), which rebuilds the view tree with safe DOM nodes. Toasts render
 * into their own container so a background notice never wipes a half-typed form.
 */
(function (global) {
  'use strict';

  var NS = global.EcoInbox = global.EcoInbox || {};
  var core = NS.core, dom = NS.dom, views = NS.views, api = NS.api;

  var IDLE_LOCK_MS = 30 * 60 * 1000;
  var AUTO_REFRESH_MS = 30 * 1000;

  function initialState() {
    return {
      connected: false,
      busy: false,
      probing: false,
      localAvailable: false,     // the local launcher answered without a token
      localTransport: false,     // this session is running on it
      showCredentialForm: false,
      error: null,
      principal: null,
      realm: null,
      nowMs: Date.now(),
      view: 'overview',
      snapshot: { agents: [], messages: [], discoveries: [], grants: [], assignments: [], counts: {} },
      filters: {
        kind: null, state: null, scope: '', query: '', agent: null,
        unresolvedOnly: false, showRevoked: false, grantAgent: null
      },
      selectedMessageId: null,
      selectedMessage: null,
      selectedGrantId: null,
      delegationParentId: null,
      expandedAssignment: null,
      reassignTarget: null,
      assignmentDraft: null,
      composing: false,
      draft: null,
      discoveryResults: [],
      discoveryQuery: '',
      discoveryScope: '/',
      discoveryLimit: 20,
      probe: {},
      pending: [],
      toasts: []
    };
  }

  function App(config) {
    config = config || {};
    this.state = initialState();
    this.root = config.root || null;
    this.toastRoot = config.toastRoot || null;
    this.client = config.client;
    this.toastSeq = 0;
    this.idleTimer = null;
    this.autoTimer = null;
    this.actions = this.buildActions();
    var self = this;
    this.client.onUnauthorized = function () {
      if (!self.state.connected) return;
      self.lock('The hub rejected the credential. Enter it again.');
    };
  }

  /* ---------------------------------------------------------------- render */

  App.prototype.render = function () {
    this.state.nowMs = Date.now();
    this.state.pending = this.client.pending.slice();
    var focus = this.captureFocus();
    if (this.root) dom.mount(this.root, views.app(this.state, this.actions));
    if (this.toastRoot) dom.mount(this.toastRoot, views.toastArea(this.state, this.actions));
    this.restoreFocus(focus);
  };

  App.prototype.captureFocus = function () {
    var doc = global.document;
    if (!doc || !doc.activeElement || !doc.activeElement.id) return null;
    var a = doc.activeElement;
    var info = { id: a.id };
    try {
      info.start = a.selectionStart;
      info.end = a.selectionEnd;
    } catch (e) { /* not a text control */ }
    return info;
  };

  App.prototype.restoreFocus = function (info) {
    if (!info || !global.document) return;
    var node = global.document.getElementById(info.id);
    if (!node || !node.focus) return;
    node.focus();
    if (info.start != null && node.setSelectionRange) {
      try { node.setSelectionRange(info.start, info.end); } catch (e) { /* ignore */ }
    }
  };

  App.prototype.toast = function (text, kind) {
    var id = ++this.toastSeq;
    var self = this;
    this.state.toasts.push({ id: id, text: text, kind: kind || 'info' });
    if (this.toastRoot) dom.mount(this.toastRoot, views.toastArea(this.state, this.actions));
    if (global.setTimeout) {
      global.setTimeout(function () { self.dismissToast(id); }, kind === 'error' ? 12000 : 6000);
    }
    return id;
  };

  App.prototype.dismissToast = function (id) {
    this.state.toasts = this.state.toasts.filter(function (t) { return t.id !== id; });
    if (this.toastRoot) dom.mount(this.toastRoot, views.toastArea(this.state, this.actions));
  };

  App.prototype.fail = function (err) {
    var msg = core.describeError(err);
    this.toast(msg, 'error');
    return msg;
  };

  /* ------------------------------------------------------------- lifecycle */

  /**
   * Start-up. The owner should not have to handle a credential on his own
   * machine: if the local console launcher is serving this page it answers an
   * unauthenticated owner.snapshot, and we connect straight through it. A 401
   * means this is a plain hub, and the credential form is shown instead.
   */
  App.prototype.start = function () {
    var self = this;
    this.state.probing = true;
    this.render();
    return this.client.probeLocalTransport({ limit: core.LIMITS.MAX_LIMIT })
      .then(function (outcome) {
        self.state.probing = false;
        if (outcome.local) {
          self.state.localAvailable = true;
          self.state.localTransport = true;
          self.applySnapshot(outcome.snapshot);
          self.state.connected = true;
          self.render();
          self.startTimers();
          self.loadRealm();
          return true;
        }
        self.state.localAvailable = false;
        self.state.showCredentialForm = true;
        self.render();
        return false;
      });
  };

  /** Reconnect through the local transport after an explicit lock. */
  App.prototype.connectLocal = function () {
    this.state.error = null;
    return this.start();
  };

  App.prototype.connect = function (credential) {
    var self = this;
    if (!this.client.setCredential(credential)) {
      this.state.error = 'Enter the credential.';
      this.render();
      return Promise.resolve(false);
    }
    this.state.busy = true;
    this.state.error = null;
    this.render();
    return this.loadSnapshot().then(function () {
      self.state.connected = true;
      self.state.busy = false;
      self.state.error = null;
      self.render();
      self.startTimers();
      self.loadRealm();
      return true;
    }, function (err) {
      self.client.clearCredential();
      self.state.busy = false;
      self.state.connected = false;
      self.state.showCredentialForm = true;
      self.state.error = core.describeError(err);
      self.render();
      return false;
    });
  };

  App.prototype.loadRealm = function () {
    var self = this;
    return this.client.realm().then(function (realm) {
      if (realm) { self.state.realm = realm; self.render(); }
      return realm;
    });
  };

  App.prototype.lock = function (why) {
    var wasLocal = this.state.localAvailable;
    this.client.clearCredential();      // also drops the local transport flag
    this.stopTimers();
    var toasts = this.state.toasts;
    this.state = initialState();
    this.state.toasts = toasts;
    this.state.localAvailable = wasLocal;
    this.state.showCredentialForm = !wasLocal;
    this.state.error = why || null;
    this.render();
    if (why) this.toast(why, 'error');
  };

  App.prototype.startTimers = function () {
    var self = this;
    this.stopTimers();
    if (!global.setInterval) return;
    this.autoTimer = global.setInterval(function () {
      if (!self.state.connected) return;
      if (self.state.composing || self.formHasFocus()) return;   // never eat typing
      self.loadSnapshot().then(function () { self.render(); }, function () { /* toast already shown */ });
    }, AUTO_REFRESH_MS);
    this.resetIdle();
  };

  App.prototype.stopTimers = function () {
    if (this.autoTimer && global.clearInterval) global.clearInterval(this.autoTimer);
    if (this.idleTimer && global.clearTimeout) global.clearTimeout(this.idleTimer);
    this.autoTimer = null;
    this.idleTimer = null;
  };

  App.prototype.resetIdle = function () {
    var self = this;
    if (!global.setTimeout) return;
    if (this.idleTimer) global.clearTimeout(this.idleTimer);
    this.idleTimer = global.setTimeout(function () {
      if (self.state.connected) self.lock('Locked after 30 minutes idle. The credential was discarded.');
    }, IDLE_LOCK_MS);
  };

  App.prototype.formHasFocus = function () {
    var doc = global.document;
    if (!doc || !doc.activeElement) return false;
    var tag = (doc.activeElement.tagName || '').toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select';
  };

  /* ------------------------------------------------------------------ data */

  /**
   * owner.snapshot is the one read the console needs. If a hub build does not
   * expose it we fall back to the individual list operations rather than showing
   * an empty console.
   */
  App.prototype.loadSnapshot = function (params) {
    var self = this;
    var req = { limit: core.LIMITS.MAX_LIMIT };
    if (params && params.scope && params.scope !== '/') req.scope = params.scope;
    return this.client.snapshot(req).then(function (snap) {
      self.applySnapshot(snap);
      return snap;
    }, function (err) {
      if (err.code === 'unknown_operation' || err.code === 'unsupported_operation' || err.status === 404) {
        return self.loadSnapshotByParts();
      }
      self.fail(err);
      throw err;
    });
  };

  App.prototype.loadSnapshotByParts = function () {
    var self = this;
    var c = this.client;
    return Promise.all([
      c.agentsList().then(null, function () { return { agents: [] }; }),
      c.messagesList({ limit: core.LIMITS.MAX_LIMIT }).then(null, function () { return { messages: [] }; }),
      c.grantsList({}).then(null, function () { return { grants: [] }; }),
      c.assignmentsList({}).then(null, function () { return { assignments: [] }; })
    ]).then(function (parts) {
      self.applySnapshot({
        generated_at: core.toIsoUtc(Date.now()),
        agents: parts[0].agents || [],
        messages: parts[1].messages || [],
        grants: parts[2].grants || [],
        assignments: parts[3].assignments || [],
        discoveries: [],
        counts: {}
      });
      return self.state.snapshot;
    });
  };

  App.prototype.applySnapshot = function (snap) {
    snap = snap || {};
    this.state.snapshot = {
      generated_at: snap.generated_at || null,
      agents: snap.agents || [],
      messages: snap.messages || [],
      discoveries: snap.discoveries || [],
      grants: snap.grants || [],
      assignments: snap.assignments || [],
      counts: snap.counts || {}
    };
    var principal = snap.principal || snap.actor || snap.owner || null;
    if (principal) this.state.principal = principal;
    var wanted = this.state.selectedMessageId;
    if (wanted && !this.state.selectedMessage) {
      var found = this.state.snapshot.messages.filter(function (m) {
        return m.id === wanted;
      })[0];
      if (found) this.state.selectedMessage = found;
    }
  };

  App.prototype.refresh = function () {
    var self = this;
    this.state.busy = true;
    this.render();
    return this.loadSnapshot().then(function () {
      self.state.busy = false;
      self.render();
    }, function () {
      self.state.busy = false;
      self.render();
    });
  };

  /* --------------------------------------------------------------- actions */

  App.prototype.buildActions = function () {
    var self = this;
    var a = {};

    a.connect = function (cred) { return self.connect(cred); };
    a.connectLocal = function () { return self.connectLocal(); };
    a.showCredentialForm = function () {
      self.state.showCredentialForm = true;
      self.render();
    };
    a.lock = function () { self.lock(null); };
    a.refresh = function () { return self.refresh(); };
    a.dismissToast = function (id) { self.dismissToast(id); };

    a.setView = function (view) {
      self.state.view = view;
      if (global.location) { try { global.location.hash = '#' + view; } catch (e) { /* ignore */ } }
      self.render();
    };

    a.setFilter = function (key, value) {
      self.state.filters[key] = value;
      self.render();
    };

    a.filterByAgent = function (agentId) {
      self.state.filters.agent = agentId;
      self.state.view = 'inbox';
      self.render();
    };

    a.openMessage = function (id) {
      self.state.selectedMessageId = id;
      self.state.composing = false;
      var known = self.state.snapshot.messages.filter(function (m) { return m.id === id; })[0];
      self.state.selectedMessage = known || null;
      self.state.view = self.state.view === 'overview' ? 'inbox' : self.state.view;
      self.render();
      return self.client.messageGet(id).then(function (res) {
        var full = res.message || res;
        if (full && full.id === self.state.selectedMessageId) {
          self.state.selectedMessage = full;
          self.render();
        }
        return full;
      }, function (err) {
        // The list copy is still shown; say why the full record is unavailable.
        self.toast('Could not load the full message: ' + core.describeError(err), 'error');
      });
    };

    a.toggleComposer = function () {
      self.state.composing = !self.state.composing;
      if (!self.state.composing) self.state.draft = null;
      self.render();
    };

    a.composeTo = function (agentId) {
      self.state.view = 'inbox';
      self.state.composing = true;
      self.state.draft = { to: agentId, kind: 'information', scope: '/' };
      self.render();
    };

    a.replyTo = function (m) {
      self.state.composing = true;
      self.state.draft = {
        to: core.senderOf(m),
        kind: m.kind === 'request' ? 'result' : 'information',
        scope: m.scope || '/',
        subject: /^re:/i.test(m.subject || '') ? m.subject : 'Re: ' + (m.subject || ''),
        work_id: m.work_id || '',
        reply_to: m.id || ''
      };
      self.render();
    };

    a.sendMessage = function (payload, formNode) {
      return self.mutate('messages.send', payload, formNode, function (res) {
        var n = (res.deliveries || []).length;
        self.state.composing = false;
        self.state.draft = null;
        return 'Sent — ' + n + ' deliver' + (n === 1 ? 'y' : 'ies') + ' queued.';
      });
    };

    /**
     * A poll result marked as a replay is the answer to an earlier invocation.
     * Its leases and authority may already be stale, so it is never acted on:
     * the console asks again with a fresh request id instead.
     */
    a.poll = function (retried) {
      return self.client.messagesPoll({ limit: 20, lease_seconds: 60 }).then(function (res) {
        if ((res.replayed || res.stale) && !retried) {
          self.toast('That answered an earlier poll. Asking again for current work.', 'info');
          return a.poll(true);
        }
        if (res.replayed || res.stale) {
          self.toast('The hub is still replaying an earlier poll. Nothing was claimed.', 'error');
          return self.refresh();
        }
        var n = (res.messages || []).length;
        self.toast(n ? 'Claimed ' + n + ' message' + (n === 1 ? '' : 's') + '.' : 'Nothing waiting.',
          n ? 'ok' : 'info');
        return self.refresh();
      }, function (err) { self.fail(err); });
    };

    a.ack = function (m, state) {
      var params = { message_id: m.id, state: state };
      if (m.delivery && m.delivery.lease_id) params.lease_id = m.delivery.lease_id;
      return self.mutate('messages.ack', params, null, function () {
        return 'Marked ' + state + '.';
      });
    };

    a.searchDiscoveries = function (params) {
      self.state.discoveryQuery = params.query || '';
      self.state.discoveryScope = params.scope || '/';
      self.state.discoveryLimit = params.limit || 20;
      var clean = { scope: params.scope, limit: params.limit };
      if (params.query) clean.query = params.query;
      if (params.topics && params.topics.length) clean.topics = params.topics;
      return self.client.discoveriesSearch(clean).then(function (res) {
        self.state.discoveryResults = res.discoveries || [];
        self.render();
        return res;
      }, function (err) { self.fail(err); self.render(); });
    };

    a.publishDiscovery = function (payload, formNode) {
      return self.mutate('discoveries.publish', payload, formNode, function (res) {
        var d = res.discovery || {};
        self.state.discoveryResults = [d].concat(self.state.discoveryResults || []);
        return 'Published “' + core.truncate(d.title || payload.title, 40) + '”.';
      });
    };

    a.selectGrant = function (id) {
      self.state.selectedGrantId = self.state.selectedGrantId === id ? null : id;
      self.render();
    };

    a.startDelegation = function (grantId) {
      self.state.delegationParentId = grantId || null;
      self.state.view = 'grants';
      self.render();
    };

    a.grantTo = function (agentId) {
      self.state.view = 'grants';
      self.state.delegationParentId = null;
      self.render();
      var node = global.document && global.document.getElementById('f-grantee_other-grant');
      if (node) { node.value = agentId; node.focus(); }
    };

    a.issueGrant = function (payload, formNode) {
      return self.mutate('grants.issue', payload, formNode, function (res) {
        var g = res.grant || {};
        self.state.selectedGrantId = g.id || null;
        self.state.delegationParentId = null;
        return 'Grant issued to ' + (g.grantee || payload.grantee) + ' on ' + (g.scope || payload.scope) + '.';
      });
    };

    a.revokeGrant = function (grant) {
      var reason = null;
      if (global.prompt) {
        reason = global.prompt('Revoke ' + grant.id + '\nDescendant grants stop authorizing too.\nReason:');
        if (reason === null) return Promise.resolve(null);   // cancelled
      }
      var params = { grant_id: grant.id };
      if (reason) params.reason = reason;
      return self.mutate('grants.revoke', params, null, function () {
        return 'Revoked ' + core.truncate(grant.id, 14) + '. Descendants no longer authorize.';
      });
    };

    a.runProbe = function (params) {
      self.state.probe = { action: params.action, scope: params.scope, agent_id: params.agent_id || '' };
      return self.client.authorize(params).then(function (res) {
        self.state.probe.result = res;
        self.state.probe.asked = params;
        self.render();
        return res;
      }, function (err) {
        self.state.probe.result = { allowed: false, grant_ids: [] };
        self.state.probe.asked = params;
        self.fail(err);
        self.render();
      });
    };

    a.toggleAssignment = function (workId) {
      self.state.expandedAssignment = self.state.expandedAssignment === workId ? null : workId;
      self.render();
    };

    a.startReassign = function (assignment) {
      self.state.reassignTarget = assignment || null;
      self.state.view = 'assignments';
      self.render();
    };

    a.assignFromMessage = function (m) {
      self.state.view = 'assignments';
      self.state.reassignTarget = null;
      self.state.assignmentDraft = {
        work_id: m.work_id || '',
        scope: m.scope || '/',
        summary: core.truncate(m.subject || '', 120),
        assignee: core.recipientsOf(m)[0] || ''
      };
      self.render();
    };

    a.submitAssignment = function (payload, isReassign, formNode) {
      var op = isReassign ? 'assignments.reassign' : 'assignments.assign';
      return self.mutate(op, payload, formNode, function () {
        self.state.reassignTarget = null;
        self.state.assignmentDraft = null;
        return isReassign
          ? 'Reassigned ' + payload.work_id + ' to ' + payload.assignee + '.'
          : 'Assigned ' + payload.work_id + ' to ' + payload.assignee + '.';
      });
    };

    return a;
  };

  /**
   * One place for every mutation: a stable request_id per attempt-set, an honest
   * message when the outcome is unknown, and a refresh so the console always
   * shows what the hub actually recorded rather than what we hoped it recorded.
   */
  App.prototype.mutate = function (operation, params, formNode, describe) {
    var self = this;
    var requestId = core.uuid4();
    this.state.busy = true;
    this.render();
    return this.client.call(operation, params, { requestId: requestId }).then(function (res) {
      self.state.busy = false;
      if (formNode && formNode.showErrors) formNode.showErrors([]);
      var msg = describe ? describe(res) : 'Done.';
      self.toast(msg, 'ok');
      return self.refresh().then(function () { return res; });
    }, function (err) {
      self.state.busy = false;
      var text = core.describeError(err);
      if (err.uncertain) {
        text = 'Sent, but the outcome is unknown (' + text + '). ' +
          'Refresh before retrying — the hub deduplicates request ' + core.truncate(requestId, 8) + '.';
      }
      if (formNode && formNode.showErrors) formNode.showErrors([text]);
      self.toast(text, 'error');
      self.render();
      throw err;
    });
  };

  /* ----------------------------------------------------------------- start */

  function boot() {
    var doc = global.document;
    var root = doc.getElementById('root');
    var toastRoot = doc.getElementById('toasts');
    if (!root) return null;
    var endpoint;
    try {
      endpoint = api.resolveEndpoint(
        doc.baseURI || global.location.href,
        global.location ? global.location.origin : null,
        global.ECO_HUB_ENDPOINT || null
      );
    } catch (e) {
      dom.mount(root, dom.el('div', { class: 'banner banner-error' }, core.describeError(e)));
      return null;
    }
    var client = new api.HubClient({ endpoint: endpoint });
    var app = new App({ root: root, toastRoot: toastRoot, client: client });

    if (global.location && global.location.hash) {
      var want = global.location.hash.replace('#', '');
      if (views.NAV.some(function (n) { return n.id === want; })) app.state.view = want;
    }
    doc.addEventListener('click', function () { app.resetIdle(); });
    doc.addEventListener('keydown', function (ev) {
      app.resetIdle();
      if (ev.key === 'Escape' && app.state.composing) {
        app.state.composing = false;
        app.render();
      }
    });
    global.addEventListener('hashchange', function () {
      var v = global.location.hash.replace('#', '');
      if (v && v !== app.state.view && views.NAV.some(function (n) { return n.id === v; })) {
        app.state.view = v;
        app.render();
      }
    });
    app.render();
    app.start();
    global.EcoInboxApp = app;
    return app;
  }

  NS.App = App;
  NS.boot = boot;

  if (global.document && global.document.addEventListener) {
    if (global.document.readyState === 'loading') {
      global.document.addEventListener('DOMContentLoaded', boot);
    } else {
      boot();
    }
  }
})(typeof globalThis !== 'undefined' ? globalThis : this);
