/* Transport for the owner console.
 *
 * One entry point — POST {endpoint} {operation, params, request_id} with a Bearer
 * credential the human typed into the page. The credential lives in a closure
 * variable for the lifetime of the tab and is never written to localStorage,
 * sessionStorage, a cookie, the URL, the DOM or a log line.
 */
(function (global) {
  'use strict';

  var NS = global.EcoInbox = global.EcoInbox || {};
  var core = NS.core;
  var api = {};

  /* Operations that change state: these always carry a request_id, are retried
   * with the SAME id, and are never replayed with different content. */
  var MUTATIONS = {
    'agents.register': 1,
    'agents.heartbeat': 1,
    'messages.send': 1,
    'messages.poll': 1,      // claims leases, so it mutates
    'messages.ack': 1,
    'discoveries.publish': 1,
    'grants.issue': 1,
    'grants.revoke': 1,
    'assignments.assign': 1,
    'assignments.reassign': 1
  };
  api.MUTATIONS = MUTATIONS;

  var READS = {
    'agents.list': 1,
    'messages.list': 1,
    'messages.get': 1,
    'discoveries.search': 1,
    'grants.list': 1,
    'grants.get': 1,
    'assignments.list': 1,
    'authorize': 1,
    'owner.snapshot': 1
  };
  api.READS = READS;

  function isMutation(op) { return !!MUTATIONS[op]; }
  api.isMutation = isMutation;

  /** Error shape the whole UI understands. Never carries a traceback. */
  function HubClientError(code, message, opts) {
    opts = opts || {};
    this.name = 'HubClientError';
    this.code = code || 'error';
    this.message = message || 'Request failed.';
    this.status = opts.status || 0;
    this.retriable = !!opts.retriable;
    this.uncertain = !!opts.uncertain;   // sent, outcome unknown
    this.operation = opts.operation || null;
    this.request_id = opts.request_id || null;
  }
  HubClientError.prototype = Object.create(Error.prototype);
  HubClientError.prototype.constructor = HubClientError;
  api.HubClientError = HubClientError;

  /**
   * Resolve the API endpoint from the document that loaded this page, so the UI
   * works under any mount prefix without an edit, and refuse anything that is
   * not the page's own origin.
   */
  function resolveEndpoint(baseUri, origin, override) {
    var url;
    try {
      url = new global.URL(override || '../v1/call', baseUri);
    } catch (e) {
      throw new HubClientError('bad_endpoint', 'Could not work out the API endpoint.');
    }
    if (origin && url.origin !== origin) {
      throw new HubClientError('cross_origin',
        'Refusing to send the credential to a different origin.');
    }
    return url.toString();
  }
  api.resolveEndpoint = resolveEndpoint;

  function HubClient(config) {
    config = config || {};
    var credential = null;              // memory only, closure-scoped
    var self = this;

    this.endpoint = config.endpoint || null;
    this.fetchImpl = config.fetchImpl || (global.fetch ? global.fetch.bind(global) : null);
    this.timeoutMs = config.timeoutMs || 20000;
    this.maxAttempts = config.maxAttempts || 3;
    this.retryDelayMs = config.retryDelayMs == null ? 400 : config.retryDelayMs;
    this.sleep = config.sleep || function (ms) {
      return new Promise(function (r) { global.setTimeout(r, ms); });
    };
    this.uuid = config.uuid || core.uuid4;
    /* When the console is served by the local owner-console launcher, that
     * process holds the credential file and forwards authenticated calls. The
     * browser then sends NO Authorization header at all — there is no token in
     * the page to leak. Set only by a successful no-token probe. */
    this.localTransport = false;
    this.principal = null;              // agent_id the credential maps to, if known
    this.lastError = null;
    this.pending = [];                  // in-flight/uncertain mutations, for the UI
    this.onUnauthorized = config.onUnauthorized || null;
    this.onActivity = config.onActivity || null;

    this.setCredential = function (value) {
      credential = (typeof value === 'string' && value.trim()) ? value.trim() : null;
      if (credential !== null) self.localTransport = false;
      return credential !== null;
    };
    this.hasCredential = function () { return credential !== null; };
    this.clearCredential = function () {
      credential = null;
      self.localTransport = false;
      self.principal = null;
      self.pending = [];
    };
    this.authHeader = function () {
      return credential === null ? null : 'Bearer ' + credential;
    };
  }

  /** Build the wire envelope for an operation, with the contract's size guard. */
  HubClient.prototype.envelope = function (operation, params, requestId) {
    var env = core.buildEnvelope(operation, params, requestId);
    if (core.envelopeTooLarge(env)) {
      throw new HubClientError('too_large',
        'That request is larger than the ' + core.LIMITS.REQUEST_BYTES + ' byte limit.',
        { operation: operation });
    }
    return env;
  };

  function readErrorBody(body, status) {
    var code = null, message = null;
    if (body && typeof body === 'object') {
      if (body.error && typeof body.error === 'object') {
        code = body.error.code;
        message = body.error.message;
      }
      if (!code && typeof body.code === 'string') code = body.code;
      if (!message && typeof body.message === 'string') message = body.message;
    }
    if (!code) {
      code = status === 401 ? 'unauthorized' : status === 403 ? 'forbidden' : 'error';
    }
    if (!message) message = 'The hub refused that request (HTTP ' + status + ').';
    return { code: String(code), message: String(message) };
  }
  api.readErrorBody = readErrorBody;

  HubClient.prototype._once = function (envelope) {
    var self = this;
    var auth = this.authHeader();
    if (!auth && !this.localTransport) {
      return Promise.reject(new HubClientError('locked',
        'This console is not connected yet.', { operation: envelope.operation }));
    }
    if (!this.fetchImpl) {
      return Promise.reject(new HubClientError('no_transport', 'No HTTP transport available.'));
    }
    var controller = global.AbortController ? new global.AbortController() : null;
    var timer = null;
    if (controller && global.setTimeout) {
      timer = global.setTimeout(function () { controller.abort(); }, this.timeoutMs);
    }
    var headers = { 'Content-Type': 'application/json' };
    if (auth) headers.Authorization = auth;   // omitted entirely on local transport
    var init = {
      method: 'POST',
      headers: headers,
      body: JSON.stringify(envelope),
      mode: 'same-origin',
      credentials: 'omit',     // bearer only; no ambient cookie authority
      cache: 'no-store',
      redirect: 'error'
    };
    if (controller) init.signal = controller.signal;

    return this.fetchImpl(this.endpoint, init).then(function (res) {
      if (timer) global.clearTimeout(timer);
      return res.text().then(function (raw) {
        var body = null;
        if (raw) { try { body = JSON.parse(raw); } catch (e) { body = null; } }
        if (res.status >= 200 && res.status < 300) {
          if (body === null || typeof body !== 'object') {
            throw new HubClientError('bad_response', 'The hub returned a response we could not read.',
              { status: res.status, operation: envelope.operation });
          }
          return body;
        }
        var info = readErrorBody(body, res.status);
        var retriable = res.status === 429 || (res.status >= 500 && res.status < 600);
        throw new HubClientError(info.code, info.message, {
          status: res.status,
          retriable: retriable,
          operation: envelope.operation,
          request_id: envelope.request_id || null
        });
      });
    }, function (err) {
      if (timer) global.clearTimeout(timer);
      if (err instanceof HubClientError) throw err;
      var aborted = err && (err.name === 'AbortError');
      throw new HubClientError(aborted ? 'timeout' : 'network',
        aborted ? 'The hub did not answer in time.' : 'Could not reach the hub.', {
          retriable: true,
          uncertain: true,      // it may well have been applied
          operation: envelope.operation,
          request_id: envelope.request_id || null
        });
    });
  };

  /**
   * call(operation, params, opts)
   *
   * Mutations get a request_id (generated once, reused across retries so a retry
   * is a replay and not a second action). Retries are bounded and only happen for
   * transport-level failures; a 4xx answer is final. When the outcome is unknown
   * after the last attempt the request stays in `pending` so the owner sees an
   * honest "sent, outcome unknown" rather than a silent success or failure.
   */
  HubClient.prototype.call = function (operation, params, opts) {
    opts = opts || {};
    var self = this;
    var mutation = opts.mutation != null ? opts.mutation : isMutation(operation);
    var requestId = opts.requestId || (mutation ? this.uuid() : null);
    var envelope;
    try {
      envelope = this.envelope(operation, params, requestId);
    } catch (e) {
      return Promise.reject(e);
    }
    /* messages.poll is never retried. A retry would replay the request_id and
     * could hand back a cached claim whose authority has since gone stale; the
     * owner re-polls with a fresh id instead. */
    var maxAttempts = operation === 'messages.poll' ? 1
      : (mutation ? this.maxAttempts : Math.min(this.maxAttempts, 2));
    var record = null;
    if (mutation) {
      record = { operation: operation, request_id: requestId, state: 'in_flight' };
      this.pending.push(record);
    }

    function settle(state) {
      if (!record) return;
      record.state = state;
      if (state !== 'uncertain') {
        var i = self.pending.indexOf(record);
        if (i !== -1) self.pending.splice(i, 1);
      }
    }

    function attempt(n) {
      return self._once(envelope).then(function (result) {
        settle('done');
        if (self.onActivity) self.onActivity(operation);
        return result;
      }, function (err) {
        // 401 (or an explicit unauthorized code) means the credential itself is
        // no good, so the console must lock. A 403 means this principal may not
        // do this one thing — a normal, non-locking refusal.
        if (err.status === 401 || err.code === 'unauthorized') {
          settle('refused');
          self.lastError = err;
          if (self.onUnauthorized) self.onUnauthorized(err);
          throw err;
        }
        if (err.retriable && n < maxAttempts) {
          return self.sleep(self.retryDelayMs * n).then(function () { return attempt(n + 1); });
        }
        settle(err.uncertain ? 'uncertain' : 'failed');
        self.lastError = err;
        throw err;
      });
    }
    return attempt(1);
  };

  /**
   * Ask the local transport whether it will answer without a credential.
   *
   * Sends one no-token owner.snapshot. 200 means the console is being served by
   * the owner's own machine and the human never has to handle a credential;
   * 401/403 means this is a plain hub and the manual credential form is shown.
   * This deliberately bypasses call(), so a 401 here is an answer, not a lock.
   */
  HubClient.prototype.probeLocalTransport = function (params) {
    var self = this;
    var hadCredential = this.hasCredential();
    if (hadCredential) return Promise.resolve({ local: false, reason: 'credential_present' });
    this.localTransport = true;
    var envelope;
    try {
      envelope = this.envelope('owner.snapshot', params || { limit: core.LIMITS.MAX_LIMIT }, null);
    } catch (e) {
      this.localTransport = false;
      return Promise.resolve({ local: false, error: e });
    }
    return this._once(envelope).then(function (result) {
      return { local: true, snapshot: result };
    }, function (err) {
      self.localTransport = false;
      return { local: false, error: err };
    });
  };

  /* ------------------------------------------------------ operation helpers */

  HubClient.prototype.snapshot = function (params) {
    return this.call('owner.snapshot', params || {});
  };
  HubClient.prototype.agentsList = function () {
    return this.call('agents.list', {});
  };
  HubClient.prototype.messagesList = function (params) {
    return this.call('messages.list', params || {});
  };
  HubClient.prototype.messageGet = function (id) {
    return this.call('messages.get', { message_id: id });
  };
  HubClient.prototype.messagesSend = function (params, requestId) {
    return this.call('messages.send', params, { requestId: requestId });
  };
  HubClient.prototype.messagesPoll = function (params, requestId) {
    return this.call('messages.poll', params || {}, { requestId: requestId });
  };
  HubClient.prototype.messagesAck = function (params, requestId) {
    return this.call('messages.ack', params, { requestId: requestId });
  };
  HubClient.prototype.discoveriesSearch = function (params) {
    return this.call('discoveries.search', params || {});
  };
  HubClient.prototype.discoveriesPublish = function (params, requestId) {
    return this.call('discoveries.publish', params, { requestId: requestId });
  };
  HubClient.prototype.grantsList = function (params) {
    return this.call('grants.list', params || {});
  };
  HubClient.prototype.grantsIssue = function (params, requestId) {
    return this.call('grants.issue', params, { requestId: requestId });
  };
  HubClient.prototype.grantsRevoke = function (params, requestId) {
    return this.call('grants.revoke', params, { requestId: requestId });
  };
  HubClient.prototype.authorize = function (params) {
    return this.call('authorize', params);
  };
  HubClient.prototype.assignmentsList = function (params) {
    return this.call('assignments.list', params || {});
  };
  HubClient.prototype.assign = function (params, requestId) {
    return this.call('assignments.assign', params, { requestId: requestId });
  };
  HubClient.prototype.reassign = function (params, requestId) {
    return this.call('assignments.reassign', params, { requestId: requestId });
  };

  /** The realm manifest is public metadata: fetched with no credential. */
  HubClient.prototype.realm = function (url) {
    if (!this.fetchImpl) return Promise.resolve(null);
    var request;
    try {
      request = this.fetchImpl(url || '../realm.json', {
        method: 'GET', mode: 'same-origin', credentials: 'omit', cache: 'no-store'
      });
    } catch (e) {
      return Promise.resolve(null);   // the manifest is optional, never fatal
    }
    return request.then(function (res) {
      if (!res.ok) return null;
      return res.text().then(function (raw) {
        try { return JSON.parse(raw); } catch (e) { return null; }
      });
    }, function () { return null; });
  };

  api.HubClient = HubClient;
  NS.api = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
