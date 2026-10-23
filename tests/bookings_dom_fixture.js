import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const sourceRoot = new URL('../', import.meta.url);
const html = readFileSync(new URL('poolrotater_host/bookings/static/index.html', sourceRoot), 'utf8');

class MiniNode {
  constructor(tagName = 'div', ownerDocument = null) {
    this.tagName = tagName.toUpperCase();
    this.ownerDocument = ownerDocument;
    this.children = [];
    this.parentNode = null;
    this.listeners = new Map();
    this.dataset = {};
    this.attributes = new Map();
    this.className = '';
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.selected = false;
    this.value = '';
    this.text = '';
    this.id = '';
    this.type = '';
  }

  get classList() {
    return { add: (...values) => {
      const classes = new Set(this.className.split(/\s+/).filter(Boolean));
      values.forEach((value) => classes.add(value));
      this.className = [...classes].join(' ');
    } };
  }

  get textContent() {
    return this.text + this.children.map((child) => child.textContent).join('');
  }

  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children.at(-1) || null; }

  set textContent(value) {
    this.children = [];
    this.text = String(value ?? '');
  }

  append(...values) {
    for (let value of values) {
      if (typeof value === 'string' || typeof value === 'number') {
        value = this.ownerDocument.createTextNode(String(value));
      }
      if (!value) continue;
      if (value.parentNode) value.parentNode.removeChild(value);
      value.parentNode = this;
      this.children.push(value);
      if (value.tagName === 'OPTION' && (value.selected || (this.tagName === 'SELECT' && this.children.length === 1))) {
        this.value = value.value;
      }
    }
  }

  replaceChildren(...values) {
    this.children.forEach((child) => { child.parentNode = null; });
    this.children = [];
    this.text = '';
    this.append(...values);
  }

  removeChild(child) {
    this.children = this.children.filter((value) => value !== child);
    child.parentNode = null;
  }

  addEventListener(type, callback) {
    const callbacks = this.listeners.get(type) || [];
    callbacks.push(callback);
    this.listeners.set(type, callbacks);
  }

  dispatchEvent(event) {
    if (!event.target) event.target = this;
    event.currentTarget = this;
    event.preventDefault ||= function preventDefault() { this.defaultPrevented = true; };
    for (const callback of this.listeners.get(event.type) || []) callback(event);
    if (event.bubbles && this.parentNode) this.parentNode.dispatchEvent(event);
    return !event.defaultPrevented;
  }

  dispatch(type) { return this.dispatchEvent({ type, bubbles: true, target: this }); }
  click() { this.dispatch('click'); }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name === 'href') this.href = String(value);
    if (name === 'hidden') this.hidden = true;
  }

  removeAttribute(name) {
    this.attributes.delete(name);
    if (name === 'href') delete this.href;
    if (name === 'hidden') this.hidden = false;
  }

  matches(selector) {
    if (selector.startsWith('#')) return this.id === selector.slice(1);
    if (selector.startsWith('.')) return this.className.split(/\s+/).includes(selector.slice(1));
    const dataMatch = selector.match(/^\[data-([\w-]+)(?:="([^"]*)")?\]$/);
    if (dataMatch) {
      const key = dataMatch[1].replace(/-([a-z])/g, (_all, letter) => letter.toUpperCase());
      return Object.hasOwn(this.dataset, key)
        && (dataMatch[2] === undefined || String(this.dataset[key]) === dataMatch[2]);
    }
    const roleMatch = selector.match(/^(\w+)?\[data-([\w-]+)(?:="([^"]*)")?\]$/);
    if (roleMatch) {
      const key = roleMatch[2].replace(/-([a-z])/g, (_all, letter) => letter.toUpperCase());
      return (!roleMatch[1] || this.tagName === roleMatch[1].toUpperCase())
        && Object.hasOwn(this.dataset, key)
        && (roleMatch[3] === undefined || String(this.dataset[key]) === roleMatch[3]);
    }
    const checkedMatch = selector.match(/^(\w+)\[type=["']?([\w-]+)["']?\]$/);
    if (checkedMatch) return this.tagName === checkedMatch[1].toUpperCase() && this.type === checkedMatch[2];
    if (selector === 'input:checked') return this.tagName === 'INPUT' && this.checked;
    if (selector === 'select') return this.tagName === 'SELECT';
    if (selector === 'button') return this.tagName === 'BUTTON';
    if (selector === 'a') return this.tagName === 'A';
    if (selector === 'fieldset') return this.tagName === 'FIELDSET';
    return false;
  }

  querySelectorAll(selector) {
    const selectors = selector.split(',').map((value) => value.trim());
    const result = [];
    const walk = (node) => {
      for (const child of node.children) {
        if (selectors.some((item) => child.matches(item))) result.push(child);
        walk(child);
      }
    };
    walk(this);
    return result;
  }

  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }

  closest(selector) {
    let current = this;
    while (current) {
      if (selector.split(',').some((item) => current.matches(item.trim()))) return current;
      current = current.parentNode;
    }
    return null;
  }
}

class MiniHeaders {
  constructor(source = {}) {
    this.values = new Map(source instanceof MiniHeaders ? source.values : Object.entries(source));
  }
  set(name, value) { this.values.set(String(name).toLowerCase(), String(value)); }
  get(name) { return this.values.get(String(name).toLowerCase()) ?? null; }
}

function makeDocument() {
  const document = {
    ids: new Map(),
    createElement(tagName) { return new MiniNode(tagName, document); },
    createTextNode(text) {
      const node = new MiniNode('#text', document);
      node.text = String(text);
      return node;
    },
    getElementById(id) { return this.ids.get(id) || null; },
    querySelectorAll(selector) { return this.body.querySelectorAll(selector); },
  };
  document.body = document.createElement('body');
  const expression = /<([a-z][a-z0-9]*)\b([^>]*\bid="([^"]+)"[^>]*)>/gi;
  let match;
  while ((match = expression.exec(html))) {
    const [, tag, attrs, id] = match;
    const node = document.createElement(tag);
    node.id = id;
    const classMatch = attrs.match(/\bclass="([^"]*)"/);
    if (classMatch) node.className = classMatch[1];
    if (/\shidden(?:\s|>|$)/.test(attrs)) node.hidden = true;
    document.ids.set(id, node);
    document.body.append(node);
  }

  const append = (parentId, tag, text, dataset = {}) => {
    const node = document.createElement(tag);
    node.textContent = text;
    node.dataset = { ...dataset };
    document.getElementById(parentId).append(node);
    return node;
  };
  const reserveForm = document.getElementById('reserve-form');
  for (const id of ['reserve-slot', 'reserve-preset']) {
    const node = document.getElementById(id);
    reserveForm.append(node);
  }
  for (const id of ['friend-id', 'basic-settings', 'reserve-button', 'cancel-reserve']) {
    reserveForm.append(document.getElementById(id));
  }
  for (const id of ['settings-controls', 'settings-state', 'preview-settings',
    'apply-settings', 'preview-result']) document.getElementById('settings-block').append(document.getElementById(id));
  for (const action of ['start', 'abort', 'skip', 'end']) append('owner-controls', 'button', action, { action });
  document.getElementById('owner-controls').append(document.getElementById('invite-block'));
  document.getElementById('session-section').append(
    document.getElementById('private-details'), document.getElementById('activity-status'),
    document.getElementById('map-queue-block'),
    document.getElementById('owner-controls'),
    document.getElementById('roster-block'), document.getElementById('settings-block'),
  );
  return document;
}

const inventory = {
  fields: ['sr_min', 'sr_max', 'auto_difficulty', 'freestyle', 'voteskip_enabled',
    'user_commands_enabled', 'selection_styles', 'allowed_mods', 'required_mods', 'dt_mode'],
  limits: { sr_min: [0, 100], sr_max: [0, 100] },
  options: {
    selection_styles: ['variety', 'farm', 'stream', 'jump'],
    allowed_mods: ['NF', 'EZ', 'HD', 'HR'],
    required_mods: ['NF', 'EZ', 'HD', 'HR', 'DT'],
    dt_mode: ['auto', 'off', 'mixed', 'full'],
  },
  filters: {
    ar: { min: 0, max: 12, modes: ['hard', 'prefer'] },
    length: { min: 0, max: 3600, modes: ['hard', 'prefer'] },
  },
  defaults: {
    sr_min: 3.5, sr_max: 4.5, auto_difficulty: false, freestyle: false,
    voteskip_enabled: true, user_commands_enabled: true,
    selection_styles: ['variety'], allowed_mods: [], required_mods: [], dt_mode: 'auto',
    selection_filters: {
      ar: { min: 5, max: 9, mode: 'prefer' },
      length: { min: 0, max: 240, mode: 'prefer' },
    },
  },
};

function response(status, value) {
  return { status, ok: status >= 200 && status < 300, json: async () => value };
}

const waitTurn = () => new Promise((resolve) => setTimeout(resolve, 0));

test('booking portal widgets preserve drafts, roles, revisions, and uncertain request identity', async () => {
  const document = makeDocument();
  globalThis.document = document;
  globalThis.Headers = MiniHeaders;
  globalThis.window = { setInterval: () => 1, clearInterval: () => {}, location: { assign() {}, reload() {} } };
  globalThis.confirm = () => true;

  let booking = null;
  let revision = 1;
  let serverSettings = structuredClone(inventory.defaults);
  let friendRole = 'player';
  let phase = 'active';
  let failureCode = null;
  let cooldownUntil = 0;
  let failFirstConfigureResponse = true;
  let conflictNextConfigure = false;
  const requests = [];
  const operationIds = new Map();
  const makeBooking = () => ({
    id: 'booking-fixture', revision, role: 'owner', phase, failure_code: failureCode,
    slot_label: 'West room', preset: 'standard-fixed',
    settings: structuredClone(serverSettings), settings_inventory: inventory,
    players: [{ user_id: 222, username: '<img src=x onerror=alert(1)>', role: friendRole }],
    operations: [], cooldown_until: cooldownUntil,
    join_url: 'https://osu.ppy.sh/multiplayer/rooms/123', password: 'secret-room',
    activity: { condition: 'no_gameplay', closes_at: Date.now() / 1000 + 200 },
    activity_updated_at: Date.now() / 1000,
    current_map: { beatmap_id: 123, label: '<img src=x onerror=alert(1)>', stars: 4.25, hit_length: 95,
      url: 'javascript:alert(1)' },
    queue: Array.from({ length: 7 }, (_value, index) => ({
      beatmap_id: 200 + index, label: index === 0 ? '<svg onload=alert(1)>' : `Queue map ${index + 1}`,
      stars: 3.5, hit_length: 125, url: 'javascript:alert(1)',
    })),
  });

  globalThis.fetch = async (url, options = {}) => {
    const method = options.method || 'GET';
    const body = options.body ? JSON.parse(options.body) : null;
    requests.push({ url: String(url), method, headers: new Headers(options.headers || {}), body });
    if (url.endsWith('/me')) return response(200, {
      authenticated: true, enabled: true, user: { id: 111, username: 'Room owner' },
      csrf_token: 'fixture-csrf', booking_id: null,
    });
    if (url.endsWith('/slots')) return response(200, { enabled: true, slots: [{
      slot_id: 'west', label: 'West room', state: 'available', fresh: true,
      supported: true, supported_presets: ['standard-fixed'],
    }] });
    if (url.includes('/settings?')) return response(200, inventory);
    if (url.includes('/session?')) return response(200, { booking: makeBooking() });
    if (url.endsWith('/reserve')) {
      assert.equal(body.designated_friend_id, 222);
      assert.equal(body.settings.sr_min, 4.2);
      booking = makeBooking();
      return response(201, { booking });
    }
    if (url.endsWith('/action')) {
      assert.equal(requests.at(-1).headers.get('X-CSRF-Token'), 'fixture-csrf');
      if (body.action === 'grant' || body.action === 'revoke') {
        friendRole = body.action === 'grant' ? 'controller' : 'player';
        revision += 1;
        return response(202, { operation: { phase: 'applied', application: 'applied' }, booking: makeBooking() });
      }
      if (body.action === 'configure' && conflictNextConfigure) {
        conflictNextConfigure = false;
        revision += 1;
        serverSettings = { ...serverSettings, sr_max: 4.7 };
        cooldownUntil = 0;
        return response(409, { error: 'booking_revision_conflict' });
      }
      if (body.action === 'configure') {
        const prior = operationIds.get(body.request_id);
        if (prior) return response(202, prior);
        serverSettings = structuredClone(body.payload);
        revision += 1;
        cooldownUntil = Date.now() / 1000 + 15;
        const queued = { operation: { phase: 'queued', application: null }, booking: makeBooking() };
        operationIds.set(body.request_id, queued);
        if (failFirstConfigureResponse) {
          failFirstConfigureResponse = false;
          throw new TypeError('connection closed after dispatch');
        }
        return response(202, queued);
      }
    }
    return response(404, { error: 'not_found' });
  };

  await import(new URL('../poolrotater_host/bookings/static/app.js?dom-flow=1', import.meta.url));
  const portal = globalThis.__privateBookingPortal;
  await waitTurn(); await waitTurn();
  assert.equal(document.getElementById('availability-freshness').textContent, 'Availability report is current');
  document.getElementById('slot-list').querySelector('[data-reserve-slot]').click();
  await waitTurn(); await waitTurn();
  document.getElementById('friend-id').value = '222';
  const reserveSr = document.getElementById('basic-settings').querySelector('[data-setting-key="sr_min"]')
    .querySelector('input[type="number"]');
  reserveSr.value = '4.2'; reserveSr.dispatch('input');
  document.getElementById('reserve-form').dispatch('submit');
  await waitTurn(); await waitTurn(); await waitTurn();
  assert.equal(portal.booking.role, 'owner');
  assert.equal(portal.booking.players[0].role, 'player');
  assert.equal(document.getElementById('roster-list').querySelector('.roster-name').textContent,
    '<img src=x onerror=alert(1)>');
  assert.equal(document.getElementById('map-queue-block').hidden, false);
  assert.match(document.getElementById('activity-status').textContent,
    /30 minutes without meaningful gameplay/);
  assert.match(document.getElementById('activity-status').textContent,
    /Current host close estimate: \d+:\d{2}/);
  assert.equal(document.getElementById('activity-status').className, 'notice notice-warning');
  assert.equal(document.getElementById('current-map').textContent,
    '<img src=x onerror=alert(1)>4.25★ · 1:35');
  assert.equal(document.getElementById('current-map').querySelector('a').href,
    'https://osu.ppy.sh/beatmaps/123');
  const queuedMaps = document.getElementById('next-maps').querySelectorAll('.next-map');
  assert.equal(queuedMaps.length, 5);
  assert.equal(queuedMaps[0].textContent, '<svg onload=alert(1)>3.50★ · 2:05');
  assert.equal(queuedMaps[0].querySelector('a').href, 'https://osu.ppy.sh/beatmaps/200');

  const grant = document.getElementById('roster-list').querySelector('[data-role-action="grant"]');
  grant.click(); await waitTurn(); await waitTurn();
  assert.equal(requests.filter((entry) => entry.body?.action === 'grant').length, 1);
  assert.equal(portal.booking.players[0].role, 'controller');
  document.getElementById('roster-list').querySelector('[data-role-action="revoke"]').click();
  await waitTurn(); await waitTurn();
  assert.equal(requests.filter((entry) => entry.body?.action === 'revoke').length, 1);

  const srMaxRange = document.getElementById('settings-controls')
    .querySelector('[data-setting-key="sr_max"]').querySelector('input[type="range"]');
  srMaxRange.value = '4.6'; srMaxRange.dispatch('input');
  assert.equal(portal.draft.sr_max, 4.6);
  const dtSelect = document.getElementById('settings-controls')
    .querySelector('[data-setting-key="dt_mode"]').querySelector('select');
  dtSelect.value = 'full'; dtSelect.dispatch('change');
  const freestyle = document.getElementById('settings-controls')
    .querySelector('[data-setting-key="freestyle"]').querySelector('input[type="checkbox"]');
  freestyle.checked = true; freestyle.dispatch('change');
  const styles = document.getElementById('settings-controls')
    .querySelector('[data-setting-key="selection_styles"]');
  const farm = styles.querySelectorAll('input[type="checkbox"]')
    .find((input) => input.value === 'farm');
  farm.checked = true; farm.dispatch('change');
  const filterCards = document.getElementById('settings-controls').querySelectorAll('.filter-control');
  assert.equal(filterCards.length, 2);
  const arMin = filterCards[0].querySelectorAll('input[type="number"]')[0];
  arMin.value = '7'; arMin.dispatch('input');
  const lengthMax = filterCards[1].querySelectorAll('input[type="number"]')[1];
  lengthMax.value = '180'; lengthMax.dispatch('input');
  assert.equal(portal.draft.dt_mode, 'full');
  assert.equal(portal.draft.freestyle, true);
  assert.deepEqual(portal.draft.selection_styles, ['variety', 'farm']);
  assert.equal(portal.draft.selection_filters.ar.min, 7);
  assert.equal(portal.draft.selection_filters.length.max, 180);
  assert.equal(document.getElementById('apply-settings').disabled, false);

  document.getElementById('apply-settings').click();
  await waitTurn(); await waitTurn();
  const firstConfigure = requests.find((entry) => entry.body?.action === 'configure');
  assert.match(firstConfigure.body.request_id, /^[0-9a-f-]{36}$/);
  assert.equal(firstConfigure.body.payload.dt_mode, 'full');
  assert.equal(firstConfigure.body.payload.freestyle, true);
  assert.deepEqual(firstConfigure.body.payload.selection_styles, ['variety', 'farm']);
  assert.equal(firstConfigure.body.payload.selection_filters.length.max, 180);
  const retry = document.getElementById('page-message').querySelector('button');
  assert.match(document.getElementById('page-message').textContent, /Retry the same request/);
  retry.click(); await waitTurn(); await waitTurn();
  const configureRequests = requests.filter((entry) => entry.body?.action === 'configure');
  assert.equal(configureRequests.length, 2);
  assert.equal(configureRequests[0].body.request_id, configureRequests[1].body.request_id);
  assert.match(document.getElementById('page-message').textContent, /Request queued/);

  const numericRange = document.getElementById('settings-controls')
    .querySelector('[data-setting-key="sr_min"]').querySelector('input[type="number"]');
  numericRange.value = '4.3'; numericRange.dispatch('input');
  assert.equal(document.getElementById('apply-settings').disabled, true);
  assert.match(document.getElementById('settings-state').textContent, /cooldown/);
  cooldownUntil = 0;
  await portal.loadSession();
  assert.equal(document.getElementById('apply-settings').disabled, false);
  conflictNextConfigure = true;
  document.getElementById('apply-settings').click();
  await waitTurn(); await waitTurn(); await waitTurn();
  assert.equal(portal.conflict, true);
  assert.equal(portal.draft.sr_min, 4.3);
  assert.match(document.getElementById('settings-state').textContent, /draft is retained/i);
  assert.equal(requests.findLast((entry) => entry.body?.action === 'configure').body.expected_revision, revision - 1);

  phase = 'unavailable';
  failureCode = 'private_internal_failure_detail';
  await portal.loadSession();
  assert.match(document.getElementById('session-state').textContent, /temporarily unavailable|needs attention/i);
  assert.doesNotMatch(document.getElementById('session-state').textContent, /private_internal_failure_detail/);

  delete globalThis.__privateBookingPortal;
  delete globalThis.document;
  delete globalThis.window;
  delete globalThis.fetch;
  delete globalThis.confirm;
  delete globalThis.Headers;
});
