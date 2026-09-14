/* Agent Inbox & Authority — pure logic.
 *
 * No DOM, no network, no globals beyond the EcoInbox namespace, so this file can be
 * loaded by the browser with a plain <script> tag and by the test engine with load().
 * Everything here is deterministic and side-effect free; rendering lives in views.js
 * and transport lives in api.js.
 */
(function (global) {
  'use strict';

  var NS = global.EcoInbox = global.EcoInbox || {};
  var core = {};

  /* ---------------------------------------------------------------- limits */

  core.LIMITS = {
    BODY_BYTES: 16 * 1024,       // contract: bodies <= 16 KiB
    REQUEST_BYTES: 128 * 1024,   // contract: requests <= 128 KiB
    MAX_LIMIT: 100,              // contract: bounded limits <= 100
    SCOPE_CHARS: 512,
    SUBJECT_CHARS: 300
  };

  core.DELIVERY_STATES = ['queued', 'leased', 'acknowledged', 'resolved'];
  core.MESSAGE_KINDS = ['information', 'instruction', 'request', 'result'];

  /* ------------------------------------------------------------- utilities */

  function isString(v) { return typeof v === 'string'; }
  core.isString = isString;

  /** UTF-8 byte length, so the client-side body limit matches the server's. */
  function byteLength(s) {
    if (!isString(s)) return 0;
    var n = 0;
    for (var i = 0; i < s.length; i++) {
      var c = s.charCodeAt(i);
      if (c < 0x80) n += 1;
      else if (c < 0x800) n += 2;
      else if (c >= 0xd800 && c <= 0xdbff && i + 1 < s.length) {
        var d = s.charCodeAt(i + 1);
        if (d >= 0xdc00 && d <= 0xdfff) { n += 4; i++; } else { n += 3; }
      } else n += 3;
    }
    return n;
  }
  core.byteLength = byteLength;

  function clampLimit(n, dflt) {
    var v = parseInt(n, 10);
    if (!isFinite(v) || v < 1) v = dflt || 20;
    return Math.min(v, core.LIMITS.MAX_LIMIT);
  }
  core.clampLimit = clampLimit;

  function truncate(s, n) {
    if (!isString(s)) return '';
    if (s.length <= n) return s;
    return s.slice(0, Math.max(0, n - 1)) + '…';
  }
  core.truncate = truncate;

  /** RFC4122 v4 id. Uses the platform CSPRNG when present. */
  function uuid4() {
    var c = global.crypto;
    if (c && typeof c.randomUUID === 'function') return c.randomUUID();
    var bytes = new Array(16), i;
    if (c && typeof c.getRandomValues === 'function') {
      var buf = new Uint8Array(16);
      c.getRandomValues(buf);
      for (i = 0; i < 16; i++) bytes[i] = buf[i];
    } else {
      for (i = 0; i < 16; i++) bytes[i] = Math.floor(Math.random() * 256);
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    var hex = bytes.map(function (b) { return (b + 0x100).toString(16).slice(1); });
    return hex.slice(0, 4).join('') + '-' + hex.slice(4, 6).join('') + '-' +
      hex.slice(6, 8).join('') + '-' + hex.slice(8, 10).join('') + '-' +
      hex.slice(10, 16).join('');
  }
  core.uuid4 = uuid4;

  /* ----------------------------------------------------------------- scope */

  /**
   * Canonical slash-separated scope. Rejects empty/dot segments, control
   * characters, backslashes and over-long paths. Returns the canonical string
   * or throws a plain Error whose message is safe to show the owner.
   */
  function canonicalScope(raw) {
    if (!isString(raw)) throw new Error('Scope must be text.');
    var s = raw.trim();
    if (s === '') throw new Error('Scope is required (use / for everything).');
    if (s.length > core.LIMITS.SCOPE_CHARS) throw new Error('Scope is too long.');
    if (s.indexOf('\\') !== -1) throw new Error('Scope may not contain a backslash.');
    for (var i = 0; i < s.length; i++) {
      var c = s.charCodeAt(i);
      if (c < 0x20 || c === 0x7f) throw new Error('Scope may not contain control characters.');
    }
    if (s.charAt(0) !== '/') throw new Error('Scope must start with / .');
    var parts = s.split('/');
    var out = [];
    for (var j = 1; j < parts.length; j++) {
      var p = parts[j];
      if (p === '') {
        // a trailing slash is tolerated, an interior empty segment is not
        if (j === parts.length - 1) continue;
        throw new Error('Scope may not contain an empty path segment.');
      }
      if (p === '.' || p === '..') throw new Error('Scope may not contain . or .. segments.');
      out.push(p);
    }
    return out.length ? '/' + out.join('/') : '/';
  }
  core.canonicalScope = canonicalScope;

  function tryCanonicalScope(raw) {
    try { return { ok: true, scope: canonicalScope(raw) }; }
    catch (e) { return { ok: false, error: e.message }; }
  }
  core.tryCanonicalScope = tryCanonicalScope;

  /**
   * True when `child` is `parent` or lies beneath it in the scope tree.
   * /project/foo contains /project/foo/bar but NOT /project/foobar.
   */
  function scopeContains(parent, child) {
    var p, c;
    try { p = canonicalScope(parent); c = canonicalScope(child); }
    catch (e) { return false; }
    if (p === '/') return true;
    if (p === c) return true;
    return c.indexOf(p + '/') === 0;
  }
  core.scopeContains = scopeContains;

  function scopeDepth(scope) {
    var s;
    try { s = canonicalScope(scope); } catch (e) { return 0; }
    return s === '/' ? 0 : s.split('/').length - 1;
  }
  core.scopeDepth = scopeDepth;

  /* --------------------------------------------------------------- actions */

  function normalizeActions(raw) {
    var list = Array.isArray(raw) ? raw : String(raw == null ? '' : raw).split(/[,\s]+/);
    var seen = {}, out = [];
    for (var i = 0; i < list.length; i++) {
      var a = String(list[i] == null ? '' : list[i]).trim();
      if (!a) continue;
      if (a.length > 120) continue;
      if (!seen[a]) { seen[a] = true; out.push(a); }
    }
    if (out.indexOf('*') !== -1) return ['*'];
    return out;
  }
  core.normalizeActions = normalizeActions;

  /**
   * Contract semantics only: '*' covers every action, otherwise the action must
   * match exactly. Deliberately no prefix wildcards — the client must never
   * promise authority the server would refuse.
   */
  function actionsCover(held, wanted) {
    if (!Array.isArray(held) || !Array.isArray(wanted) || wanted.length === 0) return false;
    if (held.indexOf('*') !== -1) return true;
    for (var i = 0; i < wanted.length; i++) {
      if (wanted[i] === '*') return false;
      if (held.indexOf(wanted[i]) === -1) return false;
    }
    return true;
  }
  core.actionsCover = actionsCover;

  function topicsFrom(raw) {
    var list = Array.isArray(raw) ? raw : String(raw == null ? '' : raw).split(/[,\n]+/);
    var seen = {}, out = [];
    for (var i = 0; i < list.length; i++) {
      var t = String(list[i] == null ? '' : list[i]).trim().toLowerCase();
      if (!t || t.length > 60) continue;
      if (!seen[t]) { seen[t] = true; out.push(t); }
    }
    return out.slice(0, 25);
  }
  core.topicsFrom = topicsFrom;

  function linesFrom(raw) {
    var list = Array.isArray(raw) ? raw : String(raw == null ? '' : raw).split(/[\n,]+/);
    var out = [];
    for (var i = 0; i < list.length; i++) {
      var v = String(list[i] == null ? '' : list[i]).trim();
      if (v) out.push(v);
    }
    return out.slice(0, 50);
  }
  core.linesFrom = linesFrom;

  /* ------------------------------------------------------------ timestamps */

  function parseTs(iso) {
    if (!isString(iso) || !iso) return null;
    var t = Date.parse(iso);
    if (isNaN(t)) {
      // tolerate "YYYY-MM-DD HH:MM:SS" and naive UTC forms
      t = Date.parse(iso.replace(' ', 'T') + (/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? '' : 'Z'));
    }
    return isNaN(t) ? null : t;
  }
  core.parseTs = parseTs;

  function toIsoUtc(ms) {
    return new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');
  }
  core.toIsoUtc = toIsoUtc;

  function formatTs(iso) {
    var t = parseTs(iso);
    if (t === null) return isString(iso) ? iso : '';
    return toIsoUtc(t).replace('T', ' ').replace('Z', ' UTC');
  }
  core.formatTs = formatTs;

  function relativeTime(iso, nowMs) {
    var t = parseTs(iso);
    if (t === null) return '';
    var now = typeof nowMs === 'number' ? nowMs : Date.now();
    var d = Math.round((now - t) / 1000);
    var future = d < 0;
    d = Math.abs(d);
    var s;
    if (d < 45) return future ? 'now' : 'just now';
    if (d < 3600) s = Math.round(d / 60) + 'm';
    else if (d < 86400) s = Math.round(d / 3600) + 'h';
    else s = Math.round(d / 86400) + 'd';
    return future ? 'in ' + s : s + ' ago';
  }
  core.relativeTime = relativeTime;

  function isExpired(iso, nowMs) {
    var t = parseTs(iso);
    if (t === null) return false;
    return t <= (typeof nowMs === 'number' ? nowMs : Date.now());
  }
  core.isExpired = isExpired;

  /** Accepts "2026-09-04T10:00" (datetime-local) and returns UTC ISO8601. */
  function isoFromLocalInput(v) {
    if (!isString(v) || !v.trim()) return null;
    var t = Date.parse(v);
    if (isNaN(t)) return null;
    return toIsoUtc(t);
  }
  core.isoFromLocalInput = isoFromLocalInput;

  /* ------------------------------------------------------------------ urls */

  var BAD_SCHEME = /^(javascript|data|vbscript|file|blob|about):/i;

  /**
   * Returns a safe href for an owner-visible link, or null when the value must
   * be shown as inert text. Untrusted artifact refs and message bodies flow
   * through here before ever becoming an <a href>.
   */
  function safeHref(raw) {
    if (!isString(raw)) return null;
    // strip the characters browsers ignore inside a scheme ("java\tscript:")
    var probe = raw.replace(/[\u0000-\u0020\u00a0\u1680\u2000-\u200f\u2028\u2029\u202f\u205f\u3000\ufeff]/g, '').toLowerCase();
    if (BAD_SCHEME.test(probe)) return null;
    var v = raw.trim();
    if (!v) return null;
    if (v.indexOf('//') === 0) return null;                      // protocol-relative
    if (v.charAt(0) === '/' || v.charAt(0) === '#') return v;    // same-origin
    if (/^https?:\/\/[^\s]+$/i.test(v)) return v;
    if (/^mailto:[^\s]+@[^\s]+$/i.test(v)) return v;
    return null;
  }
  core.safeHref = safeHref;

  /* -------------------------------------------------------------- messages */

  function deliveryState(message) {
    var d = message && message.delivery;
    var s = d && d.state ? d.state : (message && message.state) || 'queued';
    return isString(s) ? s : 'queued';
  }
  core.deliveryState = deliveryState;

  /**
   * Every delivery we actually know about for a message.
   *
   * A message to three agents has three deliveries and they can be in three
   * different states. When the hub sends the full `deliveries` array we count
   * each one; when it sends only the single `delivery` relevant to this caller
   * we count exactly that one, rather than assuming the other recipients are in
   * the same state.
   */
  function deliveriesOf(message) {
    if (!message) return [];
    if (Array.isArray(message.deliveries) && message.deliveries.length) {
      return message.deliveries;
    }
    if (message.delivery) return [message.delivery];
    return [];
  }
  core.deliveriesOf = deliveriesOf;

  function stateOf(delivery) {
    var s = delivery && delivery.state;
    return isString(s) ? s : 'queued';
  }
  core.stateOf = stateOf;

  function deliveryStates(message) {
    var seen = {}, out = [];
    deliveriesOf(message).forEach(function (d) {
      var s = stateOf(d);
      if (!seen[s]) { seen[s] = true; out.push(s); }
    });
    if (!out.length) out.push(deliveryState(message));
    return out;
  }
  core.deliveryStates = deliveryStates;

  function deliveryCounts(messages) {
    var counts = { queued: 0, leased: 0, acknowledged: 0, resolved: 0, other: 0, total: 0 };
    (messages || []).forEach(function (m) {
      var list = deliveriesOf(m);
      if (!list.length) list = [{ state: deliveryState(m) }];
      list.forEach(function (d) {
        var s = stateOf(d);
        counts.total++;
        if (s === 'queued' || s === 'leased' || s === 'acknowledged' || s === 'resolved') counts[s]++;
        else counts.other++;
      });
    });
    return counts;
  }
  core.deliveryCounts = deliveryCounts;

  function recipientsOf(m) {
    if (!m) return [];
    if (Array.isArray(m.to)) return m.to.filter(isString);
    if (isString(m.to)) return [m.to];
    if (m.delivery && isString(m.delivery.agent_id)) return [m.delivery.agent_id];
    return deliveriesOf(m).map(function (d) { return d.recipient || d.agent_id; })
      .filter(isString).filter(function (id, index, ids) { return ids.indexOf(id) === index; });
  }
  core.recipientsOf = recipientsOf;

  function senderOf(m) {
    if (!m) return '';
    return m.from || m.sender || m.actor || m.issuer || '';
  }
  core.senderOf = senderOf;

  /** Per-agent inbox tallies, keyed by agent_id, for the agents table. */
  function agentDeliveryCounts(messages, agents) {
    var byAgent = {};
    function fresh() {
      return { queued: 0, leased: 0, acknowledged: 0, resolved: 0, other: 0, total: 0, sent: 0 };
    }
    (agents || []).forEach(function (a) {
      if (a && a.agent_id) byAgent[a.agent_id] = fresh();
    });
    function bucket(id) {
      if (!id || !isString(id)) return null;
      if (!byAgent[id]) byAgent[id] = fresh();
      return byAgent[id];
    }
    (messages || []).forEach(function (m) {
      var from = bucket(senderOf(m));
      if (from) from.sent++;
      var explicit = {};
      deliveriesOf(m).forEach(function (d) {
        var id = d && (d.recipient || d.agent_id);
        if (isString(id)) explicit[id] = stateOf(d);
      });
      var fallback = deliveryState(m);
      recipientsOf(m).forEach(function (r) {
        var b = bucket(r);
        if (!b) return;
        var s = Object.prototype.hasOwnProperty.call(explicit, r) ? explicit[r] : fallback;
        b.total++;
        if (s === 'queued' || s === 'leased' || s === 'acknowledged' || s === 'resolved') b[s]++;
        else b.other++;
      });
    });
    return byAgent;
  }
  core.agentDeliveryCounts = agentDeliveryCounts;

  function matchesQuery(message, query) {
    if (!query) return true;
    var q = String(query).toLowerCase();
    var hay = [message.subject, message.body, message.scope, message.work_id,
      senderOf(message), recipientsOf(message).join(' '), message.kind]
      .filter(isString).join(' \n ').toLowerCase();
    return hay.indexOf(q) !== -1;
  }
  core.matchesQuery = matchesQuery;

  function filterMessages(messages, f) {
    f = f || {};
    return (messages || []).filter(function (m) {
      if (f.kind && m.kind !== f.kind) return false;
      if (f.state && deliveryStates(m).indexOf(f.state) === -1) return false;
      if (f.scope && f.scope !== '/' && !scopeContains(f.scope, m.scope || '/')) return false;
      if (f.agent) {
        if (recipientsOf(m).indexOf(f.agent) === -1 && senderOf(m) !== f.agent) return false;
      }
      if (f.unresolvedOnly && !unresolvedReason(m, f.nowMs)) return false;
      if (!matchesQuery(m, f.query)) return false;
      return true;
    });
  }
  core.filterMessages = filterMessages;

  function sortMessages(messages) {
    return (messages || []).slice().sort(function (a, b) {
      var ta = parseTs(a.created_at || a.sent_at) || 0;
      var tb = parseTs(b.created_at || b.sent_at) || 0;
      if (tb !== ta) return tb - ta;
      return String(b.id || '').localeCompare(String(a.id || ''));
    });
  }
  core.sortMessages = sortMessages;

  /**
   * Why an item may need human adjudication. Returns null when nothing is wrong.
   * This is a derived view only — the UI keeps no case records of its own; the
   * owner is sent to the existing /ops/#adjudication surface.
   */
  function unresolvedReason(message, nowMs) {
    if (!message) return null;
    var state = deliveryState(message);
    if (state === 'resolved') return null;
    var now = typeof nowMs === 'number' ? nowMs : Date.now();
    if (message.expires_at && isExpired(message.expires_at, now)) {
      return 'Expired before it was resolved.';
    }
    if (message.kind === 'instruction' && message.authority &&
      message.authority.valid === false) {
      return 'Instruction authority no longer verifies.';
    }
    var worst = 0;
    deliveriesOf(message).forEach(function (d) {
      if (d && typeof d.attempts === 'number' && stateOf(d) !== 'acknowledged' &&
        stateOf(d) !== 'resolved' && d.attempts > worst) {
        worst = d.attempts;
      }
    });
    if (worst >= 3) {
      return 'Redelivered ' + worst + ' times without acknowledgement.';
    }
    return null;
  }
  core.unresolvedReason = unresolvedReason;

  /* ---------------------------------------------------------------- grants */

  function isGrantActive(grant, nowMs) {
    if (!grant) return false;
    if (grant.revoked_at) return false;
    if (grant.expires_at && isExpired(grant.expires_at, nowMs)) return false;
    return true;
  }
  core.isGrantActive = isGrantActive;

  function indexGrants(grants) {
    var byId = {};
    (grants || []).forEach(function (g) { if (g && g.id) byId[g.id] = g; });
    return byId;
  }
  core.indexGrants = indexGrants;

  /**
   * Walk a grant to its root. Detects cycles and reports the first ancestor that
   * kills the chain, mirroring the server rule that descendants of a revoked or
   * expired grant stop authorizing.
   */
  function grantChain(grants, grantId, nowMs) {
    var byId = indexGrants(grants);
    var chain = [], seen = {}, cycle = false, broken = null, missing = null;
    var id = grantId;
    while (id) {
      if (seen[id]) { cycle = true; break; }
      seen[id] = true;
      var g = byId[id];
      if (!g) { missing = id; break; }
      chain.push(g);
      if (!isGrantActive(g, nowMs) && !broken) {
        broken = { id: g.id, reason: g.revoked_at ? 'revoked' : 'expired' };
      }
      id = g.parent_grant_id || null;
    }
    return {
      chain: chain,
      cycle: cycle,
      missing: missing,
      broken: broken,
      // "effective" is advisory. authorize() on the server stays the decider.
      effective: !cycle && !missing && !broken && chain.length > 0,
      root: chain.length ? chain[chain.length - 1] : null,
      depth: chain.length
    };
  }
  core.grantChain = grantChain;

  /**
   * A grant only authorizes while its whole chain does. `isGrantActive` looks at
   * one record; this looks at the record AND its ancestors, so a grant whose
   * parent was revoked is correctly reported as powerless even though its own
   * revoked_at is still null.
   */
  function isGrantEffective(grants, grantId, nowMs) {
    return grantChain(grants, grantId, nowMs).effective;
  }
  core.isGrantEffective = isGrantEffective;

  function effectiveGrants(grants, nowMs) {
    return (grants || []).filter(function (g) {
      return g && g.id && isGrantEffective(grants, g.id, nowMs);
    });
  }
  core.effectiveGrants = effectiveGrants;

  /** Grants held by an agent that actually authorize something right now. */
  function grantsFor(grants, agentId, nowMs) {
    return (grants || []).filter(function (g) {
      return g && g.grantee === agentId && isGrantEffective(grants, g.id, nowMs);
    });
  }
  core.grantsFor = grantsFor;

  /**
   * Grants a principal can actually cite for an action and scope. A grant whose
   * ancestor was revoked or expired is excluded, so the console never offers the
   * owner authority the hub would refuse.
   */
  function usableIssuerGrants(grants, principal, action, scope, nowMs) {
    return (grants || []).filter(function (g) {
      if (!g || !g.id) return false;
      if (!isGrantEffective(grants, g.id, nowMs)) return false;
      if (principal && g.grantee !== principal) return false;
      if (action && !actionsCover(g.actions || [], [action])) return false;
      if (scope && !scopeContains(g.scope, scope)) return false;
      return true;
    });
  }
  core.usableIssuerGrants = usableIssuerGrants;

  /**
   * Client-side pre-check for a delegation. The server re-derives all of this;
   * we run it first so the owner gets an instant, specific reason instead of a
   * round-trip error.
   */
  function validateDelegation(parent, draft, nowMs, grants) {
    var errors = [];
    var scope = null;
    draft = draft || {};
    if (!isString(draft.grantee) || !draft.grantee.trim()) {
      errors.push('Choose who receives the grant.');
    }
    var sc = tryCanonicalScope(draft.scope);
    if (!sc.ok) errors.push(sc.error); else scope = sc.scope;
    var actions = normalizeActions(draft.actions);
    if (!actions.length) errors.push('List at least one action (or * for all).');
    if (parent) {
      if (!isGrantActive(parent, nowMs)) {
        errors.push('The source grant is ' + (parent.revoked_at ? 'revoked' : 'expired') + '.');
      } else if (grants && parent.id && !isGrantEffective(grants, parent.id, nowMs)) {
        var info = grantChain(grants, parent.id, nowMs);
        errors.push('The source grant no longer authorizes: ' +
          (info.cycle ? 'its chain contains a cycle.'
            : info.missing ? 'ancestor ' + info.missing + ' is missing.'
              : 'ancestor ' + info.broken.id + ' is ' + info.broken.reason + '.'));
      }
      if (!parent.delegable) errors.push('The source grant is not delegable.');
      if (scope && !scopeContains(parent.scope, scope)) {
        errors.push('Scope ' + scope + ' is outside the source grant scope ' + parent.scope + '.');
      }
      if (actions.length && !actionsCover(parent.actions || [], actions)) {
        errors.push('A child grant cannot hold actions the source grant lacks.');
      }
      if (draft.delegable && !parent.delegable) {
        errors.push('Cannot pass on delegation the source grant does not have.');
      }
      if (draft.expires_at && parent.expires_at) {
        var d = parseTs(draft.expires_at), p = parseTs(parent.expires_at);
        if (d !== null && p !== null && d > p) {
          errors.push('A child grant cannot outlive the source grant.');
        }
      }
      if (draft.grantee && parent.grantee === draft.grantee && scope && parent.scope === scope &&
        actionsCover(actions, parent.actions || []) && actionsCover(parent.actions || [], actions)) {
        errors.push('That would re-grant the same authority to its current holder.');
      }
    }
    return { ok: errors.length === 0, errors: errors, scope: scope, actions: actions };
  }
  core.validateDelegation = validateDelegation;

  /* ----------------------------------------------------------- assignments */

  function assignmentVersion(a) {
    if (!a) return null;
    var v = a.version != null ? a.version : a.expected_version;
    if (typeof v === 'number') return v;
    if (v == null) return null;
    var n = parseInt(v, 10);
    return isNaN(n) ? null : n;
  }
  core.assignmentVersion = assignmentVersion;

  function priorOwners(a) {
    var h = (a && (a.history || a.prior_owners)) || [];
    if (!Array.isArray(h)) return [];
    return h.map(function (e) {
      if (isString(e)) return { assignee: e };
      return e || {};
    });
  }
  core.priorOwners = priorOwners;

  /* -------------------------------------------------------------- drafting */

  function validateMessageDraft(draft) {
    draft = draft || {};
    var errors = [];
    var to = Array.isArray(draft.to) ? draft.to.filter(Boolean) : linesFrom(draft.to);
    if (!to.length) errors.push('Pick at least one recipient.');
    if (core.MESSAGE_KINDS.indexOf(draft.kind) === -1) errors.push('Choose a message kind.');
    if (!isString(draft.subject) || !draft.subject.trim()) errors.push('Subject is required.');
    if (isString(draft.subject) && draft.subject.length > core.LIMITS.SUBJECT_CHARS) {
      errors.push('Subject is longer than ' + core.LIMITS.SUBJECT_CHARS + ' characters.');
    }
    if (!isString(draft.body) || !draft.body.trim()) errors.push('Body is required.');
    var bytes = byteLength(draft.body || '');
    if (bytes > core.LIMITS.BODY_BYTES) {
      errors.push('Body is ' + bytes + ' bytes; the limit is ' + core.LIMITS.BODY_BYTES + '.');
    }
    var sc = tryCanonicalScope(draft.scope);
    if (!sc.ok) errors.push(sc.error);
    if (draft.kind === 'instruction' && !draft.authority_grant_id) {
      errors.push('An instruction needs a grant that allows instructions.issue for this scope.');
    }
    return {
      ok: errors.length === 0, errors: errors, to: to,
      scope: sc.ok ? sc.scope : null, bytes: bytes,
      artifacts: linesFrom(draft.artifacts)
    };
  }
  core.validateMessageDraft = validateMessageDraft;

  function validateDiscoveryDraft(draft) {
    draft = draft || {};
    var errors = [];
    if (!isString(draft.title) || !draft.title.trim()) errors.push('Title is required.');
    if (!isString(draft.body) || !draft.body.trim()) errors.push('Body is required.');
    var bytes = byteLength(draft.body || '');
    if (bytes > core.LIMITS.BODY_BYTES) {
      errors.push('Body is ' + bytes + ' bytes; the limit is ' + core.LIMITS.BODY_BYTES + '.');
    }
    var sc = tryCanonicalScope(draft.scope);
    if (!sc.ok) errors.push(sc.error);
    var topics = topicsFrom(draft.topics);
    if (!topics.length) errors.push('Add at least one topic so this is findable.');
    return {
      ok: errors.length === 0, errors: errors, topics: topics,
      artifacts: linesFrom(draft.artifacts), scope: sc.ok ? sc.scope : null, bytes: bytes
    };
  }
  core.validateDiscoveryDraft = validateDiscoveryDraft;

  function validateAssignmentDraft(draft, forReassign) {
    draft = draft || {};
    var errors = [];
    if (!isString(draft.work_id) || !draft.work_id.trim()) errors.push('Work id is required.');
    if (!isString(draft.assignee) || !draft.assignee.trim()) errors.push('Assignee is required.');
    if (!isString(draft.summary) || !draft.summary.trim()) errors.push('Summary is required.');
    var sc = tryCanonicalScope(draft.scope);
    if (!sc.ok) errors.push(sc.error);
    if (forReassign) {
      var v = draft.expected_version;
      if (v == null || v === '' || isNaN(parseInt(v, 10))) {
        errors.push('Reassignment needs the version you are replacing.');
      }
    }
    return { ok: errors.length === 0, errors: errors, scope: sc.ok ? sc.scope : null };
  }
  core.validateAssignmentDraft = validateAssignmentDraft;

  /* ------------------------------------------------------------- envelopes */

  function buildEnvelope(operation, params, requestId) {
    var env = { operation: operation, params: params || {} };
    if (requestId) env.request_id = requestId;
    return env;
  }
  core.buildEnvelope = buildEnvelope;

  function envelopeTooLarge(envelope) {
    return byteLength(JSON.stringify(envelope)) > core.LIMITS.REQUEST_BYTES;
  }
  core.envelopeTooLarge = envelopeTooLarge;

  /** Human-readable, traceback-free error text from any failure shape. */
  function describeError(err) {
    if (!err) return 'Something went wrong.';
    if (isString(err)) return err;
    var code = err.code || (err.error && err.error.code);
    var msg = err.message || (err.error && err.error.message);
    if (code && msg) return code + ': ' + msg;
    return msg || code || 'Something went wrong.';
  }
  core.describeError = describeError;

  /** Counts for the overview tiles; recomputed locally when core omits them. */
  function snapshotCounts(snapshot) {
    var s = snapshot || {};
    var given = s.counts || {};
    var msgs = core.deliveryCounts(s.messages || []);
    var now = Date.now();
    var activeGrants = effectiveGrants(s.grants || [], now).length;
    var derived = {
      messages_queued: msgs.queued,
      messages_leased: msgs.leased,
      messages_acknowledged: msgs.acknowledged,
      messages_resolved: msgs.resolved,
      messages: msgs.total,
      agents: (s.agents || []).length,
      discoveries: (s.discoveries || []).length,
      grants_active: activeGrants,
      grants: (s.grants || []).length,
      assignments_open: (s.assignments || []).length
    };
    var out = {}, k;
    for (k in derived) if (Object.prototype.hasOwnProperty.call(derived, k)) out[k] = derived[k];
    for (k in given) {
      if (Object.prototype.hasOwnProperty.call(given, k) && typeof given[k] === 'number') out[k] = given[k];
    }
    return out;
  }
  core.snapshotCounts = snapshotCounts;

  NS.core = core;
})(typeof globalThis !== 'undefined' ? globalThis : this);
