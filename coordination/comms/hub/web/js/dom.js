/* Safe DOM construction.
 *
 * The single rule this file exists to enforce: server data becomes text nodes and
 * attribute values, never markup. Nothing in this codebase assigns innerHTML,
 * outerHTML or insertAdjacentHTML, and nothing calls eval/new Function. A message
 * body containing <img src=x onerror=...> is displayed as those literal characters.
 */
(function (global) {
  'use strict';

  var NS = global.EcoInbox = global.EcoInbox || {};
  var core = NS.core;
  var dom = {};

  var doc = global.document;

  /** Attributes that carry a URL and must be scheme-checked before use. */
  var URL_ATTRS = { href: 1, src: 1, action: 1, formaction: 1, poster: 1, cite: 1, data: 1 };

  function document_() {
    if (!doc) doc = global.document;
    if (!doc) throw new Error('No document available.');
    return doc;
  }

  function text(value) {
    return document_().createTextNode(value == null ? '' : String(value));
  }
  dom.text = text;

  function append(node, child) {
    if (child == null || child === false) return;
    if (Array.isArray(child)) {
      for (var i = 0; i < child.length; i++) append(node, child[i]);
      return;
    }
    if (typeof child === 'object' && child.nodeType) node.appendChild(child);
    else node.appendChild(text(child));
  }
  dom.append = append;

  /**
   * el('div', {class: 'x', onclick: fn, dataset: {id: '1'}}, ['text', childNode])
   * Strings in the children list always become text nodes.
   */
  function el(tag, attrs, children) {
    var node = document_().createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v == null || v === false) return;
        if (k === 'dataset') {
          Object.keys(v).forEach(function (d) {
            if (v[d] != null) node.dataset[d] = String(v[d]);
          });
          return;
        }
        if (k === 'class' || k === 'className') { node.className = String(v); return; }
        if (k === 'text') { append(node, String(v)); return; }
        if (k.indexOf('on') === 0 && typeof v === 'function') {
          node.addEventListener(k.slice(2), v);
          return;
        }
        if (URL_ATTRS[k.toLowerCase()]) {
          var safe = core.safeHref(String(v));
          if (safe === null) {
            // Refused link: keep the intent visible without a usable target.
            node.setAttribute('data-unsafe-' + k, String(v));
            node.setAttribute('title', 'Link refused: unsupported or unsafe URL scheme.');
            return;
          }
          node.setAttribute(k, safe);
          if (k === 'href' && /^https?:/i.test(safe)) {
            node.setAttribute('rel', 'noopener noreferrer');
          }
          return;
        }
        if (v === true) { node.setAttribute(k, ''); return; }
        node.setAttribute(k, String(v));
      });
    }
    append(node, children);
    return node;
  }
  dom.el = el;

  function frag(children) {
    var f = document_().createDocumentFragment();
    append(f, children);
    return f;
  }
  dom.frag = frag;

  /** Empty a node without touching innerHTML. */
  function clear(node) {
    if (!node) return node;
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  }
  dom.clear = clear;

  function mount(node, children) {
    clear(node);
    append(node, children);
    return node;
  }
  dom.mount = mount;

  function byId(id) { return document_().getElementById(id); }
  dom.byId = byId;

  function qs(sel, root) { return (root || document_()).querySelector(sel); }
  dom.qs = qs;

  function qsa(sel, root) {
    var list = (root || document_()).querySelectorAll(sel);
    return Array.prototype.slice.call(list);
  }
  dom.qsa = qsa;

  function on(node, event, handler) {
    if (node) node.addEventListener(event, handler);
    return node;
  }
  dom.on = on;

  /* ------------------------------------------------------- small components */

  function chip(label, kind, title) {
    return el('span', {
      class: 'chip' + (kind ? ' chip-' + kind : ''),
      title: title || null
    }, String(label == null ? '' : label));
  }
  dom.chip = chip;

  function badge(label, kind) {
    return el('span', { class: 'badge' + (kind ? ' badge-' + kind : '') },
      String(label == null ? '' : label));
  }
  dom.badge = badge;

  function mono(value, title) {
    return el('span', { class: 'mono', title: title || null },
      String(value == null ? '' : value));
  }
  dom.mono = mono;

  function field(label, value, opts) {
    opts = opts || {};
    return el('div', { class: 'field' + (opts.wide ? ' field-wide' : '') }, [
      el('div', { class: 'field-label' }, label),
      el('div', { class: 'field-value' + (opts.mono ? ' mono' : '') },
        value == null || value === '' ? el('span', { class: 'muted' }, '—') : value)
    ]);
  }
  dom.field = field;

  function button(label, opts) {
    opts = opts || {};
    var attrs = {
      class: 'btn' + (opts.variant ? ' btn-' + opts.variant : ''),
      type: opts.type || 'button',
      title: opts.title || null,
      disabled: opts.disabled ? true : null
    };
    if (opts.onclick) attrs.onclick = opts.onclick;
    if (opts.dataset) attrs.dataset = opts.dataset;
    return el('button', attrs, label);
  }
  dom.button = button;

  /** A link that degrades to inert text when the target is not safe. */
  function link(label, href, opts) {
    opts = opts || {};
    var safe = core.safeHref(href);
    if (safe === null) {
      return el('span', {
        class: 'link-refused',
        title: 'Link refused: unsupported or unsafe URL scheme.'
      }, label);
    }
    var attrs = { class: opts.class || 'link', href: safe };
    if (opts.newTab) { attrs.target = '_blank'; }
    return el('a', attrs, label);
  }
  dom.link = link;

  function empty(message) {
    return el('div', { class: 'empty' }, message);
  }
  dom.empty = empty;

  function spinner(label) {
    return el('div', { class: 'loading' }, [
      el('span', { class: 'spinner', 'aria-hidden': 'true' }),
      el('span', {}, label || 'Working…')
    ]);
  }
  dom.spinner = spinner;

  function table(headers, rows) {
    return el('div', { class: 'table-wrap' }, [
      el('table', { class: 'table' }, [
        el('thead', {}, el('tr', {}, headers.map(function (h) {
          return el('th', {}, h);
        }))),
        el('tbody', {}, rows.length ? rows : [
          el('tr', {}, el('td', { colspan: String(headers.length) },
            el('div', { class: 'empty' }, 'Nothing here yet.')))
        ])
      ])
    ]);
  }
  dom.table = table;

  NS.dom = dom;
})(typeof globalThis !== 'undefined' ? globalThis : this);
