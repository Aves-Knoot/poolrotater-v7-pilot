const API = '/private-lobbies/api';
const POLL_MS = 5000;
const SAFE_SETTING_KEYS = new Set([
  'sr_min', 'sr_max', 'max_length', 'real_max_length',
  'countdown_seconds', 'auto_start_seconds', 'min_players', 'queue_ahead', 'stream_share',
  'preferred_min_length', 'preferred_max_length', 'preferred_length_share',
  'auto_difficulty', 'freestyle', 'voteskip_enabled', 'user_commands_enabled',
  'selection_styles', 'allowed_mods', 'required_mods', 'dt_mode',
]);
const BOOLEAN_SETTING_KEYS = new Set(['auto_difficulty', 'freestyle', 'voteskip_enabled', 'user_commands_enabled']);
const CHOICE_SETTING_KEYS = new Set(['selection_styles', 'allowed_mods', 'required_mods']);
const SINGLE_CHOICE_SETTING_KEYS = new Set(['dt_mode']);
const SAFE_FILTER_KEYS = new Set(['ar', 'length']);
const MANIA_PRESETS = new Set(['mania-4k', 'mania-7k']);
const ERROR_COPY = {
  authentication_required: 'Please sign in to continue.',
  forbidden: 'You do not have permission for that action.',
  not_found: 'That booking is no longer available.',
  slot_unavailable: 'That room is no longer available. Choose another room.',
  unsupported: 'This room does not support that option.',
  conflict: 'The lobby changed while you were editing. Review the current state and apply your draft again.',
  rate_limited: 'Please wait a moment before trying again.',
  csrf_failed: 'Your session needs refreshing. Reload the page and try again.',
  booking_settings_refused: 'Those settings are not compatible with this room. Adjust the draft and try again.',
  booking_invalid_setting: 'Those settings are outside the room’s supported range.',
  booking_filter_unsupported: 'That filter is not available for this preset.',
  booking_invalid_filters: 'Review the filter ranges and try again.',
};

export function makeRequestId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((v) => v.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function friendlyError(code, status) {
  if (ERROR_COPY[code]) return ERROR_COPY[code];
  if (status === 401) return ERROR_COPY.authentication_required;
  if (status === 403) return ERROR_COPY.forbidden;
  if (status === 404) return ERROR_COPY.not_found;
  if (status === 409) return ERROR_COPY.conflict;
  if (status === 429) return ERROR_COPY.rate_limited;
  return 'We could not complete that request. Please try again.';
}

export function normalizeSchema(schema, capabilities = {}) {
  const source = Array.isArray(schema)
    ? schema
    : Object.entries(schema || {}).map(([key, value]) => ({ key, ...(value || {}) }));
  const supported = capabilities.settings || capabilities.supported_settings || null;
  return source.map((item) => typeof item === 'string' ? { key: item } : item).filter((item) => {
    const key = item.key || item.name;
    const isSupported = !supported || (Array.isArray(supported)
      ? supported.includes(key)
      : Object.hasOwn(supported, key) && supported[key] !== false);
    return SAFE_SETTING_KEYS.has(key) && isSupported && item.available !== false;
  }).map((item) => ({ ...item, key: item.key || item.name }));
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function byId(id) { return document.getElementById(id); }
function show(node, visible = true) { node.hidden = !visible; }
function clear(node) { node.replaceChildren(); }
function number(value, fallback = null) {
  if (value === '' || value === null || value === undefined) return fallback;
  const result = Number(value);
  return Number.isFinite(result) ? result : fallback;
}

function positiveMapId(value) {
  const parsed = typeof value === 'string' && /^\d+$/.test(value) ? Number(value) : value;
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

function mapLength(value) {
  const seconds = number(value);
  if (seconds == null || seconds <= 0 || seconds > 3600) return null;
  return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
}

function settingLabel(item) {
  if (item.label) return item.label;
  return item.key.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatTime(value) {
  if (value == null) return 'Not started';
  const milliseconds = Number(value) < 10_000_000_000 ? Number(value) * 1000 : Number(value);
  const seconds = Math.max(0, Math.ceil((milliseconds - Date.now()) / 1000));
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, '0')}`;
}

class BookingPortal {
  constructor() {
    this.me = null;
    this.slots = [];
    this.booking = null;
    this.draft = null;
    this.appliedSettings = null;
    this.pendingMutation = null;
    this.pollTimer = null;
    this.clockTimer = null;
    this.conflict = false;
    this.reserveInventory = {};
    this.reserveDefaults = {};
    this.settingsRenderKey = null;
    this.slotRenderKey = null;
    this.polling = false;
    this.inviteUrl = null;
    this.reserveTrigger = null;
    this.bind();
  }

  bind() {
    byId('reserve-form').addEventListener('submit', (event) => { event.preventDefault(); this.reserve(); });
    byId('cancel-reserve').addEventListener('click', () => {
      show(byId('reserve-section'), false);
      this.reserveTrigger?.focus?.();
    });
    byId('slot-list').addEventListener('click', (event) => {
      const button = event.target.closest('[data-reserve-slot]');
      if (button) this.openReserve(button.dataset.reserveSlot);
    });
    byId('owner-controls').addEventListener('click', (event) => {
      const button = event.target.closest('[data-action]');
      if (button) this.submitAction(button.dataset.action, {});
    });
    byId('roster-list').addEventListener('click', (event) => {
      const button = event.target.closest('[data-role-action]');
      if (!button) return;
      const id = number(button.dataset.userId);
      if (id == null) return;
      this.submitAction(button.dataset.roleAction, { user_id: id });
    });
    byId('apply-settings').addEventListener('click', () => this.applySettings());
    byId('reload-settings').addEventListener('click', () => this.reloadCurrentSettings());
    byId('preview-settings').addEventListener('click', () => this.previewSettings());
    byId('reserve-preset').addEventListener('change', () => this.loadReserveSchema());
    byId('sign-out').addEventListener('click', () => this.logout());
    byId('create-invite').addEventListener('click', () => this.createInvite());
    byId('copy-invite').addEventListener('click', () => this.copyInvite());
  }

  async logout() {
    byId('sign-out').disabled = true;
    try {
      await this.request('/logout', { method: 'POST', body: '{}' });
      window.location.reload();
    } catch (error) {
      byId('sign-out').disabled = false;
      this.message(error.message || 'Sign out could not be completed. Please try again.', 'error');
    }
  }

  createInvite() {
    if (this.booking?.role !== 'owner' || this.pendingMutation) return;
    show(byId('invite-link'), false);
    show(byId('copy-invite'), false);
    this.mutate({ path: '/invite', method: 'POST', kind: 'invite', body: { booking_id: this.booking.id } });
  }

  safeInviteUrl(value) {
    try {
      const url = new URL(value);
      return url.origin === window.location.origin && url.pathname === '/private-lobbies/join' ? url.href : null;
    } catch { return null; }
  }

  async copyInvite() {
    if (!this.inviteUrl || !navigator.clipboard?.writeText) {
      this.message('This browser cannot copy the invite link. Open it and copy the address.', 'warning');
      return;
    }
    try {
      await navigator.clipboard.writeText(this.inviteUrl);
      this.message('Invite link copied.', 'success');
    } catch {
      this.message('The invite link could not be copied. Open it and copy the address.', 'warning');
    }
  }

  async request(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set('Accept', 'application/json');
    if (options.body) headers.set('Content-Type', 'application/json');
    if (options.method && options.method !== 'GET' && this.me?.csrf_token) {
      headers.set('X-CSRF-Token', this.me.csrf_token);
    }
    const response = await fetch(`${API}${path}`, { credentials: 'same-origin', ...options, headers });
    let data = {};
    try { data = await response.json(); } catch { /* friendly response below */ }
    if (!response.ok) {
      const code = data.code || data.error;
      const error = new Error(friendlyError(code, response.status));
      error.status = response.status;
      error.code = code;
      throw error;
    }
    return data;
  }

  message(text, kind = 'info', retry = false) {
    const target = byId('page-message');
    clear(target);
    target.className = `notice notice-${kind}`;
    target.append(document.createTextNode(text));
    if (retry && this.pendingMutation) {
      const button = el('button', 'inline-button', 'Retry the same request');
      button.type = 'button';
      button.addEventListener('click', () => this.retryPending());
      target.append(document.createTextNode(' '), button);
    }
    show(target, Boolean(text));
  }

  async init() {
    try {
      this.me = await this.request('/me');
      const signIn = byId('sign-in');
      if (this.me.enabled !== false && !this.me.authenticated) show(signIn);
      if (this.me.authenticated) {
        signIn.textContent = this.me.user?.username || 'Signed in';
        signIn.removeAttribute('href');
        signIn.classList.add('signed-in');
        show(signIn);
        show(byId('sign-out'));
      }
      await this.loadSlots();
      if (this.me.enabled === false) {
        this.message('Private bookings are not available yet.', 'info');
        return;
      }
      if (this.me.authenticated && this.me.booking_id) await this.loadSession();
      this.pollTimer = window.setInterval(() => this.poll(), POLL_MS);
      this.clockTimer = window.setInterval(() => {
        this.renderCountdown();
        if (this.booking) this.renderActivity(this.booking, ['owner', 'controller'].includes(this.booking.role));
        if (this.booking && !byId('settings-block').hidden) this.renderSettingsStatus();
      }, 1000);
    } catch {
      this.message('Availability is temporarily unavailable. Please reload in a moment.', 'error');
      byId('availability-freshness').textContent = 'Availability could not be checked';
    }
  }

  async loadSlots() {
    const data = await this.request('/slots');
    this.slots = Array.isArray(data.slots) ? data.slots : [];
    this.renderSlots();
  }

  renderSlots() {
    const renderKey = JSON.stringify({ slots: this.slots, authenticated: Boolean(this.me?.authenticated),
      enabled: this.me?.enabled !== false, booking: Boolean(this.booking) });
    if (renderKey === this.slotRenderKey) return;
    this.slotRenderKey = renderKey;
    const list = byId('slot-list');
    clear(list);
    if (!this.slots.length) {
      list.append(el('p', 'muted', 'No rooms are listed right now. Check back later.'));
      byId('availability-freshness').textContent = 'No availability report';
      return;
    }
    const seen = this.slots.some((slot) => slot.fresh === true);
    byId('availability-freshness').textContent = seen
      ? 'Availability report is current'
      : 'Availability report may be delayed';
    for (const slot of this.slots) {
      const card = el('article', 'slot-card');
      const info = el('div');
      info.append(el('h3', '', slot.label || 'Private room'));
      const state = slot.state || 'unavailable';
      info.append(el('p', 'slot-state', state.charAt(0).toUpperCase() + state.slice(1)));
      const freshness = slot.fresh === true ? 'Current report' : 'Delayed report';
      info.append(el('p', 'muted', freshness));
      const action = el('button', 'button button-secondary', 'Set up');
      action.type = 'button';
      action.dataset.reserveSlot = String(slot.slot_id || '');
      const reservable = slot.state === 'available' && slot.fresh === true && slot.supported !== false;
      action.disabled = !reservable || !this.me?.authenticated || this.me?.enabled === false || Boolean(this.booking);
      if (this.me?.enabled === false) action.textContent = 'Unavailable';
      else if (!this.me?.authenticated) action.textContent = 'Sign in to reserve';
      info.append(action);
      card.append(info);
      const presets = Array.isArray(slot.supported_presets) ? slot.supported_presets : [];
      if (presets.length) card.append(el('p', 'muted preset-summary', `Presets: ${presets.map((p) => typeof p === 'string' ? p : p.label || p.id).join(', ')}`));
      list.append(card);
    }
  }

  openReserve(slotId) {
    if (!this.me?.authenticated) { window.location.assign('/private-lobbies/oauth/start'); return; }
    const slot = this.slots.find((item) => String(item.slot_id) === String(slotId));
    if (!slot) return;
    byId('reserve-slot').replaceChildren();
    const option = el('option', '', slot.label || 'Private room');
    option.value = slot.slot_id;
    byId('reserve-slot').append(option);
    const presets = Array.isArray(slot.supported_presets) ? slot.supported_presets : [];
    const selector = byId('reserve-preset');
    clear(selector);
    for (const preset of presets) {
      const value = typeof preset === 'string' ? preset : preset.id || preset.name;
      if (!value) continue;
      const optionNode = el('option', '', typeof preset === 'string' ? preset : preset.label || value);
      optionNode.value = value;
      selector.append(optionNode);
    }
    this.reserveInventory = slot.basic_settings_schema || slot.settings_schema || {};
    this.reserveDefaults = slot.base_settings || {};
    this.reserveTrigger = document.activeElement;
    show(byId('reserve-section'));
    byId('reserve-title').focus?.();
    this.loadReserveSchema();
  }

  async loadReserveSchema() {
    const preset = byId('reserve-preset').value;
    if (!preset) return;
    clear(byId('basic-settings'));
    this.reserveInventory = {};
    this.reserveDefaults = {};
    try {
      const data = await this.request(`/settings?preset=${encodeURIComponent(preset)}`);
      if (preset !== byId('reserve-preset').value) return;
      this.reserveInventory = data.settings_schema || data.inventory || data;
      this.reserveDefaults = data.defaults || data.settings || this.reserveDefaults || {};
    } catch {
      // Unsupported options remain absent if the preset inventory is unavailable.
    }
    if (preset !== byId('reserve-preset').value) return;
    const inventory = this.reserveInventory || {};
    const schema = normalizeSchema(inventory.fields || inventory, inventory.capabilities || {});
    const limits = inventory.limits || {};
    for (const field of schema) {
      const bounds = limits[field.key];
      const descriptor = { ...field, ...(inventory.options?.[field.key] ? { options: inventory.options[field.key] } : {}),
        ...(bounds ? { min: bounds[0], max: bounds[1] } : {}) };
      this.addSettingControl(byId('basic-settings'), descriptor, this.reserveDefaults, false);
    }
  }

  addSettingControl(container, field, values, draftMode) {
    const key = field.key;
    const value = values?.[key] ?? field.value ?? field.default ?? (CHOICE_SETTING_KEYS.has(key) ? [] : undefined);
    if (BOOLEAN_SETTING_KEYS.has(key)) {
      const label = el('label', 'setting-check');
      label.dataset.settingKey = key;
      const checkbox = el('input'); checkbox.type = 'checkbox'; checkbox.checked = value === true;
      checkbox.addEventListener('change', () => { if (draftMode) this.updateDraft(key, checkbox.checked); });
      label.append(checkbox, el('span', '', settingLabel(field)));
      if (key === 'freestyle') label.append(el('span', 'field-help', 'Freestyle cannot be combined with mod filters. The room checks preset-specific compatibility before saving.'));
      container.append(label);
      return;
    }
    if (SINGLE_CHOICE_SETTING_KEYS.has(key)) {
      const options = Array.isArray(field.options) ? field.options : [];
      if (!options.length) return;
      const label = el('label', 'setting-control');
      label.dataset.settingKey = key;
      label.append(el('span', '', settingLabel(field)));
      const select = el('select', 'setting-select');
      for (const item of options) {
        const option = typeof item === 'string' ? { value: item, label: item } : item;
        if (typeof option?.value !== 'string') continue;
        const node = el('option', '', option.label || option.value);
        node.value = option.value;
        node.selected = option.value === value;
        select.append(node);
      }
      select.addEventListener('change', () => { if (draftMode) this.updateDraft(key, select.value); });
      label.append(select);
      if (key === 'dt_mode') {
        label.append(el('span', 'field-help', 'Full uses the qualified DT pool whenever it is eligible. Mixed preserves the room’s native balance; off excludes DT maps.'));
      }
      container.append(label);
      return;
    }
    if (CHOICE_SETTING_KEYS.has(key)) {
      const options = Array.isArray(field.options) ? field.options : field.options && typeof field.options === 'object'
        ? Object.entries(field.options).map(([option, label]) => ({ value: option, label })) : [];
      if (!options.length) return;
      const group = el('fieldset', 'choice-control'); group.dataset.settingKey = key;
      group.append(el('legend', '', settingLabel(field)));
      for (const item of options) {
        const option = typeof item === 'string' ? { value: item, label: item } : item;
        if (typeof option?.value !== 'string') continue;
        const label = el('label', 'choice-option');
        const checkbox = el('input'); checkbox.type = 'checkbox'; checkbox.value = option.value;
        checkbox.checked = Array.isArray(value) && value.includes(option.value);
        checkbox.addEventListener('change', () => {
          if (!draftMode) return;
          const selected = [...group.querySelectorAll('input:checked')].map((input) => input.value);
          this.updateDraft(key, selected);
        });
        label.append(checkbox, el('span', '', option.label || option.value)); group.append(label);
      }
      if (key === 'allowed_mods' || key === 'required_mods') {
        group.append(el('p', 'field-help', 'The room validates conflicting mod combinations before applying this selection.'));
      }
      container.append(group);
      return;
    }
    const label = el('label', 'setting-control');
    label.dataset.settingKey = key;
    label.append(el('span', '', settingLabel(field)));
    const min = number(field.min, 0);
    const max = number(field.max, 10);
    const step = number(field.step, 0.1);
    const inputId = `${draftMode ? 'session' : 'reserve'}-setting-${key}`;
    const range = el('input', 'setting-range');
    range.type = 'range'; range.id = `${inputId}-range`; range.min = min; range.max = max; range.step = step;
    if (value != null) range.value = value;
    const numberInput = el('input', 'setting-number');
    numberInput.type = 'number'; numberInput.id = inputId; numberInput.min = min; numberInput.max = max; numberInput.step = step;
    numberInput.inputMode = 'decimal';
    if (value != null) numberInput.value = value;
    range.addEventListener('input', () => { numberInput.value = range.value; if (draftMode) this.updateDraft(key, numberInput.value); });
    numberInput.addEventListener('input', () => {
      const parsed = number(numberInput.value);
      if (parsed != null) range.value = String(Math.min(max, Math.max(min, parsed)));
      if (draftMode) this.updateDraft(key, numberInput.value);
    });
    const pair = el('div', 'range-pair'); pair.append(range, numberInput);
    label.append(pair);
    if (field.description) label.append(el('span', 'field-help', field.description));
    container.append(label);
  }

  async reserve() {
    const slotId = byId('reserve-slot').value;
    const friendRaw = byId('friend-id').value.trim();
    const friendId = friendRaw ? Number(friendRaw) : null;
    if (friendRaw && (!Number.isSafeInteger(friendId) || friendId < 1)) {
      this.message('Enter a valid numeric player ID or leave it blank.', 'error'); return;
    }
    const settings = {};
    for (const field of this.reserveSchema()) {
      const value = this.readSettingControl(byId('basic-settings'), field);
      if (value !== undefined) settings[field.key] = value;
    }
    const operation = {
      path: '/reserve', method: 'POST',
      body: { request_id: makeRequestId(), slot_id: slotId, preset: byId('reserve-preset').value,
        settings, designated_friend_id: friendId },
      kind: 'reserve',
    };
    await this.mutate(operation);
  }

  reserveSchema() {
    const inventory = this.reserveInventory || {};
    const limits = inventory.limits || {};
    return normalizeSchema(inventory.fields || inventory, inventory.capabilities || {}).map((field) => ({
      ...field,
      ...(inventory.options?.[field.key] ? { options: inventory.options[field.key] } : {}),
      ...(limits[field.key] ? { min: limits[field.key][0], max: limits[field.key][1] } : {}),
    }));
  }

  readSettingControl(container, field) {
    const group = container.querySelector(`[data-setting-key="${field.key}"]`);
    if (!group) return undefined;
    if (BOOLEAN_SETTING_KEYS.has(field.key)) return group.querySelector('input[type="checkbox"]')?.checked;
    if (SINGLE_CHOICE_SETTING_KEYS.has(field.key)) return group.querySelector('select')?.value;
    if (CHOICE_SETTING_KEYS.has(field.key)) return [...group.querySelectorAll('input:checked')].map((input) => input.value);
    return number(group.querySelector('input[type="number"]')?.value);
  }

  submitAction(action, payload) {
    if (!this.booking || !this.can(action)) return;
    if (action === 'end' && !window.confirm('End this private booking? The room will close after cleanup.')) return;
    this.mutate({
      path: '/action', method: 'POST', kind: action,
      body: { request_id: makeRequestId(), booking_id: this.booking.id,
        expected_revision: this.booking.revision, action, payload },
    });
  }

  async mutate(operation) {
    if (this.pendingMutation) return;
    this.pendingMutation = operation;
    this.setMutationsDisabled(true);
    await this.sendPending();
  }

  async sendPending() {
    if (!this.pendingMutation) return;
    const operation = this.pendingMutation;
    try {
      const result = await this.request(operation.path, { method: operation.method, body: JSON.stringify(operation.body) });
      this.pendingMutation = null;
      if (operation.kind === 'reserve') {
        this.booking = result.booking || result;
        this.draft = null;
        this.conflict = false;
        show(byId('reserve-section'), false);
        await this.loadSession();
        byId('session-title').focus?.();
        this.setMutationsDisabled(false);
      } else if (operation.kind === 'invite') {
        this.inviteUrl = this.safeInviteUrl(result.invite_url);
        if (this.inviteUrl) {
          byId('invite-link').href = this.inviteUrl;
          show(byId('invite-link'));
          show(byId('copy-invite'));
          const expiry = number(result.expires_in, 1800);
          byId('invite-expiry').textContent = `Invite expires in ${Math.max(1, Math.ceil(expiry / 60))} minutes. Creating another link revokes this one.`;
          this.message('Invite link created.', 'success');
        } else this.message('The server returned an invalid invite link. Please try again.', 'error');
        this.setMutationsDisabled(false);
      } else if (operation.kind === 'preview') {
        const preview = result.preview || result;
        const count = number(preview.count ?? preview.available_count);
        byId('preview-result').textContent = count == null
          ? 'Preview is ready.'
          : `${count} eligible map${count === 1 ? '' : 's'} match this draft.`;
        show(byId('preview-result'));
        this.message('Preview completed. Your draft is unchanged.', 'success');
        this.setMutationsDisabled(false);
      } else {
        if (operation.kind === 'configure') {
          this.appliedSettings = structuredClone(operation.body.payload);
          this.draft = structuredClone(operation.body.payload);
          this.conflict = false;
        }
        this.message(this.receiptMessage(result.operation || result), 'success');
        await this.loadSession();
        this.setMutationsDisabled(false);
      }
      this.renderSlots();
    } catch (error) {
      if (!this.pendingMutation) {
        this.setMutationsDisabled(false);
        this.message('The request was received, but the latest lobby status could not be refreshed. It will update automatically.', 'warning');
        return;
      }
      if (error instanceof TypeError || !error.status) {
        this.message('We could not confirm whether the request completed. Retry the same request to check safely.', 'warning', true);
      } else {
        this.pendingMutation = null;
        this.setMutationsDisabled(false);
        if (error.status === 409 && operation.kind !== 'reserve' && operation.kind !== 'preview') this.conflict = true;
        this.message(error.message || friendlyError(error.code, error.status), 'error');
        if (error.status === 409) await this.loadSession().catch(() => {});
      }
    }
  }

  async retryPending() { await this.sendPending(); }

  setMutationsDisabled(disabled) {
    for (const button of document.querySelectorAll('button[data-action], button[data-role-action], #apply-settings, #preview-settings, #reserve-button, #create-invite')) {
      if (disabled) button.disabled = true;
      else if (button.dataset.action) button.disabled = !this.can(button.dataset.action) || !this.actionAvailable(button.dataset.action);
      else if (button.dataset.roleAction) button.disabled = !this.can(button.dataset.roleAction);
      else if (button.id === 'preview-settings') button.disabled = !this.can('configure');
      else if (button.id === 'create-invite') button.disabled = this.booking?.role !== 'owner';
      else if (button.id === 'reserve-button') button.disabled = !this.me?.authenticated || this.me?.enabled === false || Boolean(this.booking);
    }
    if (disabled) byId('settings-state').textContent = 'Waiting for server receipt…';
    else if (this.booking && !byId('settings-block').hidden) this.renderSettingsStatus();
  }

  receiptMessage(operation) {
    if (operation?.action === 'configure') return this.settingsOutcomeMessage(operation);
    const phase = operation?.phase;
    if (phase === 'queued') return 'Request queued. The room will update when the host confirms it.';
    if (phase === 'applied') return 'Change applied by the room.';
    if (phase === 'refused' || operation?.error) return 'The room could not apply that change. Review the current settings.';
    return 'Request received. Waiting for the room to confirm the result.';
  }

  settingsOutcomeMessage(operation) {
    if (!operation) return 'Draft matches server settings.';
    if (operation.phase === 'failed' || operation.phase === 'refused' || operation.error) {
      return 'Settings were not confirmed. Review the room’s current settings before trying again.';
    }
    if (operation.phase === 'cancelled') return 'The settings update was cancelled.';
    if (['queued', 'pending'].includes(operation.phase)) return 'Settings queued for the room.';
    if (operation.phase === 'dispatched') return 'Settings are applying in the room.';
    if (operation.phase === 'applied') {
      if (operation.application === 'live') return 'Settings applied live in the room.';
      if (operation.application === 'next_map') return 'Settings applied for the next map selection.';
      if (operation.application === 'recreate') return 'The room reports that a new session is needed for these settings.';
      return 'The room confirmed the settings but did not report when they take effect.';
    }
    return 'Draft matches server settings.';
  }

  settingsEffectMessage(operation, dirty = false) {
    if (this.conflict) {
      return 'This draft has not been applied. Reload current settings to discard it; the current map and visible queue stay as they are.';
    }
    if (dirty) {
      return 'These edits are still a draft. Apply them before the room changes; the current map and visible queue will stay as they are.';
    }
    if (operation?.phase === 'failed' || operation?.phase === 'refused' || operation?.error) {
      return 'The room did not confirm this update. Review its current settings before applying again.';
    }
    if (operation?.phase === 'cancelled') return 'The update was cancelled before the room confirmed it.';
    if (['queued', 'pending', 'dispatched'].includes(operation?.phase)) {
      return 'The current map and maps already in the visible queue stay as they are while the room processes this update.';
    }
    if (operation?.phase === 'applied' && operation.application === 'live') {
      return 'Room controls covered by this update use the new settings now. The current map and visible queue stay unchanged.';
    }
    if (operation?.phase === 'applied' && operation.application === 'next_map') {
      return 'Map selection settings apply to new selections. The current map and maps already in the visible queue stay unchanged.';
    }
    if (operation?.phase === 'applied' && operation.application === 'recreate') {
      return 'The room reports that a new session is required before these settings take effect. The current map and visible queue stay unchanged while this session continues.';
    }
    return 'Settings guide room controls and future map choices. They do not change the current map or remove maps already in the visible queue.';
  }

  settingsOperationLabel(operation) {
    if (operation.phase === 'failed' || operation.phase === 'refused' || operation.error) return 'Not applied';
    if (operation.phase === 'cancelled') return 'Cancelled';
    if (['queued', 'pending'].includes(operation.phase)) return 'Queued';
    if (operation.phase === 'dispatched') return 'Applying';
    if (operation.phase === 'applied') {
      if (operation.application === 'live') return 'Applied live';
      if (operation.application === 'next_map') return 'Applied for next map';
      if (operation.application === 'recreate') return 'New session needed';
      return 'Applied';
    }
    return operation.phase || 'Received';
  }

  async poll() {
    if (this.polling) return;
    this.polling = true;
    try {
      await this.loadSlots();
      if (this.me?.authenticated && (this.booking || this.me.booking_id)) await this.loadSession();
    } catch { /* transient read failures do not change local drafts or permissions */ }
    finally { this.polling = false; }
  }

  async loadSession() {
    const id = this.booking?.id || this.me?.booking_id;
    if (!id) return;
    const data = await this.request(`/session?id=${encodeURIComponent(id)}`);
    const booking = data.booking || data;
    if (this.booking?.id === booking.id) {
      const previousRevision = this.booking.revision;
      const hasLocalEdits = JSON.stringify(this.draft) !== JSON.stringify(this.appliedSettings);
      this.booking = booking;
      if (previousRevision !== booking.revision) {
        if (hasLocalEdits) this.conflict = true;
        else {
          this.draft = structuredClone(booking.settings || {});
          this.appliedSettings = structuredClone(booking.settings || {});
          this.conflict = false;
          this.settingsRenderKey = null;
        }
      }
    } else {
      this.booking = booking;
      this.draft = structuredClone(booking.settings || {});
      this.appliedSettings = structuredClone(booking.settings || {});
      this.settingsRenderKey = null;
    }
    if (!this.appliedSettings) this.appliedSettings = structuredClone(booking.settings || {});
    if (this.draft == null) this.draft = structuredClone(booking.settings || {});
    this.renderSession();
  }

  capabilities() { return this.booking?.capabilities || this.booking?.settings_capabilities || {}; }
  inventory() { return this.booking?.settings_inventory || this.booking?.settings_schema || this.booking?.inventory || {}; }
  schema() {
    const inventory = this.inventory();
    const limits = inventory.limits || {};
    const schemaValue = this.booking?.settings_schema || this.booking?.schema || inventory;
    const declared = schemaValue.fields || schemaValue;
    const fields = normalizeSchema(declared, this.capabilities());
    return fields.map((field) => {
      const bounds = limits[field.key];
      return {
        ...field,
        ...(inventory.options?.[field.key] ? { options: inventory.options[field.key] } : {}),
        ...(bounds ? { min: field.min ?? bounds[0], max: field.max ?? bounds[1] } : {}),
      };
    });
  }

  can(action) {
    const role = this.booking?.role;
    if (action === 'end' || action === 'grant' || action === 'revoke') return role === 'owner';
    if (['configure', 'start', 'abort', 'skip'].includes(action)) return role === 'owner' || role === 'controller';
    return false;
  }

  renderSession() {
    const booking = this.booking;
    if (!booking) return;
    show(byId('session-section'));
    show(byId('public-section'), true);
    byId('session-title').textContent = booking.slot_label || 'Your private lobby';
    byId('session-eyebrow').textContent = `Booking ${String(booking.id).slice(0, 8)}`;
    byId('role-badge').textContent = booking.role || 'Player';
    byId('session-state').textContent = this.phaseCopy(booking);
    const privateAllowed = ['owner', 'controller'].includes(booking.role);
    const hasJoinDetails = Boolean(booking.join_url || booking.password);
    show(byId('private-details'), Boolean(booking.role) && hasJoinDetails);
    if (booking.role) {
      const link = byId('join-link');
      if (this.safeJoinUrl(booking.join_url)) { link.href = this.safeJoinUrl(booking.join_url); link.textContent = 'Open lobby'; }
      else { link.removeAttribute('href'); link.textContent = 'Not ready yet'; }
      byId('lobby-password').textContent = booking.password || 'Not ready yet';
    }
    const controls = byId('owner-controls');
    show(controls, this.can('start') && !['ended', 'closing'].includes(booking.phase));
    for (const button of controls.querySelectorAll('[data-action]')) {
      button.hidden = !this.can(button.dataset.action) || !this.actionAvailable(button.dataset.action);
    }
    const roster = Array.isArray(booking.players) ? booking.players : [];
    show(byId('roster-block'), privateAllowed);
    this.renderMapQueue(booking, privateAllowed);
    show(byId('invite-block'), booking.role === 'owner');
    byId('roster-caption').textContent = roster.length ? `${roster.length} player${roster.length === 1 ? '' : 's'}` : 'Roster unavailable';
    this.renderRoster(roster, privateAllowed);
    const canConfigure = this.can('configure') && this.schema().length > 0;
    show(byId('settings-block'), canConfigure);
    if (canConfigure) this.renderSettings();
    const operations = Array.isArray(booking.operations) ? booking.operations : [];
    show(byId('operation-block'), privateAllowed && operations.length > 0);
    this.renderOperations(operations);
    this.renderCountdown();
    this.renderActivity(booking, privateAllowed);
  }

  phaseCopy(booking) {
    const messages = {
      reserved: 'Reserved. The room is preparing.', preparing: 'The room is preparing.',
      ready: 'Room is ready. The owner or designated friend must arrive before the timer ends.',
      awaiting_claim: 'Room is ready. The owner or designated friend must arrive before the timer ends.',
      active: 'The private lobby is active.', closing: 'The room is closing and cleaning up.', ended: 'This booking has ended.',
      unavailable: 'The room is temporarily unavailable. Please wait for an updated status.',
    };
    if (booking.failure_code) return 'The room needs attention. Please wait while its status is checked.';
    return messages[booking.phase] || 'Booking status is being updated.';
  }

  renderMapQueue(booking, privateAllowed) {
    const hasCurrent = Object.hasOwn(booking, 'current_map');
    const hasQueue = Array.isArray(booking.queue);
    show(byId('map-queue-block'), privateAllowed && (hasCurrent || hasQueue));
    if (!privateAllowed || (!hasCurrent && !hasQueue)) return;

    const current = byId('current-map');
    current.replaceChildren();
    current.className = 'map-card';
    if (booking.current_map && typeof booking.current_map === 'object' && !Array.isArray(booking.current_map)) {
      this.renderMap(current, booking.current_map);
    } else {
      current.textContent = 'No map is currently reported.';
      current.className = 'map-card muted';
    }

    const list = byId('next-maps');
    clear(list);
    const maps = hasQueue ? booking.queue.slice(0, 5) : [];
    if (!maps.length) {
      list.append(el('li', 'muted', hasQueue ? 'No maps are waiting in the queue.' : 'Queue details are not available.'));
      return;
    }
    for (const map of maps) {
      const item = el('li', 'next-map');
      if (map && typeof map === 'object' && !Array.isArray(map)) this.renderMap(item, map);
      else item.textContent = 'Map details are not available.';
      list.append(item);
    }
  }

  renderMap(container, map) {
    const beatmapId = positiveMapId(map.beatmap_id);
    const label = typeof map.label === 'string' && map.label.trim() ? map.label : null;
    const title = label || (beatmapId ? `Beatmap ${beatmapId}` : 'Map details');
    if (beatmapId) {
      const link = el('a', 'map-title', title);
      link.href = `https://osu.ppy.sh/beatmaps/${beatmapId}`;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      container.append(link);
    } else {
      container.append(el('span', 'map-title', title));
    }
    const details = [];
    const stars = number(map.stars);
    if (stars != null && stars >= 0 && stars <= 20) details.push(`${stars.toFixed(2)}★`);
    const length = mapLength(map.hit_length);
    if (length) details.push(length);
    if (details.length) container.append(el('span', 'map-meta', details.join(' · ')));
  }

  safeJoinUrl(value) {
    try {
      const url = new URL(value);
      return url.protocol === 'https:' ? url.href : null;
    } catch { return null; }
  }

  safeInviteUrl(value) {
    try {
      const url = new URL(value);
      return url.origin === window.location.origin && url.pathname === '/private-lobbies/join' ? url.href : null;
    } catch { return null; }
  }

  actionAvailable(action) {
    const phase = this.booking?.phase;
    if (action === 'start') return ['ready', 'active'].includes(phase);
    if (['abort', 'skip'].includes(action)) return phase === 'active';
    if (action === 'end') return !['ended', 'closing'].includes(phase);
    return true;
  }

  renderCountdown() {
    const deadline = this.booking?.claim_deadline;
    if (!deadline) { byId('claim-countdown').textContent = 'Arrival window starts when the room is ready'; return; }
    byId('claim-countdown').textContent = formatTime(deadline);
  }

  renderActivity(booking, privateAllowed) {
    const status = byId('activity-status');
    const visible = privateAllowed && booking.phase === 'active';
    show(status, visible);
    if (!visible) return;
    status.className = 'field-help';
    const activity = booking.activity;
    const observedAt = Number(booking.activity_updated_at);
    const reportAge = Date.now() / 1000 - observedAt;
    const fresh = Number.isFinite(observedAt) && reportAge >= 0 && reportAge <= 30;
    if (!fresh || !activity || activity.condition === 'unknown' || activity.closes_at == null) {
      status.textContent = 'Activity timing is delayed or unavailable. The countdown stays hidden until a fresh, verified host report includes it.';
      return;
    }
    const description = activity.condition === 'empty'
      ? 'An empty lobby may close after 30 continuous minutes empty, or earlier if 30 minutes pass without meaningful gameplay.'
      : activity.condition === 'no_gameplay'
        ? 'The host closes a lobby after 30 minutes without meaningful gameplay.'
        : 'A verified map is in progress. The host uses its expected end plus a short grace when estimating inactivity closure.';
    const remaining = Math.max(0, (Number(activity.closes_at) - Date.now() / 1000));
    const countdown = remaining === 0
      ? 'The reported timer has elapsed; waiting for a fresh host report to confirm status.'
      : `Current host close estimate: ${formatTime(activity.closes_at)}. Fresh host reports can change the estimate.`;
    status.textContent = `${description} ${countdown}`;
    if (remaining > 0 && remaining <= 300) status.className = 'notice notice-warning';
  }

  renderRoster(players, privateAllowed) {
    const list = byId('roster-list'); clear(list);
    for (const player of players) {
      const row = el('li', 'roster-row');
      const identity = el('span', 'roster-name', player.username || `Player ${player.user_id}`);
      const role = el('span', 'roster-role', player.role || 'player');
      row.append(identity, role);
      if (privateAllowed && this.booking.role === 'owner' && number(player.user_id) != null && Number(player.user_id) !== Number(this.me?.user?.id)) {
        const action = player.role === 'controller' ? 'revoke' : 'grant';
        const button = el('button', 'button button-small button-secondary', action === 'grant' ? 'Make controller' : 'Remove controller');
        button.type = 'button'; button.dataset.roleAction = action; button.dataset.userId = player.user_id;
        row.append(button);
      }
      list.append(row);
    }
    if (!players.length) list.append(el('li', 'muted', 'Player list will appear when the host reports it.'));
  }

  renderSettings() {
    const container = byId('settings-controls');
    const schema = this.schema();
    const filters = this.inventory().filters || {};
    const enabledFilters = this.capabilities().filters;
    const renderKey = JSON.stringify({ schema, filters, enabledFilters, preset: this.booking.preset, role: this.booking.role });
    if (renderKey !== this.settingsRenderKey) {
      clear(container);
      for (const field of schema) this.addSettingControl(container, field,
        { ...(this.inventory().defaults || {}), ...(this.draft || {}) }, true);
      for (const [key, descriptor] of Object.entries(filters)) {
        if (!SAFE_FILTER_KEYS.has(key)) continue;
        if (Array.isArray(enabledFilters) && !enabledFilters.includes(key)) continue;
        if (enabledFilters && !Array.isArray(enabledFilters) && enabledFilters[key] !== true) continue;
        const current = this.draft?.selection_filters?.[key] || this.inventory().defaults?.selection_filters?.[key] || {
          min: descriptor.default_min ?? descriptor.min ?? 0,
          max: descriptor.default_max ?? descriptor.max ?? 10,
          mode: 'prefer',
        };
        this.addFilterControl(container, key, descriptor, current);
      }
      this.renderUnsupportedFilters(container);
      this.settingsRenderKey = renderKey;
    }
    this.renderSettingsStatus();
  }

  addFilterControl(container, key, descriptor, current) {
    if (!descriptor) return;
    const card = el('fieldset', 'filter-control');
    card.append(el('legend', '', `${key.toUpperCase()} range`));
    const bounds = { min: descriptor.min ?? 0, max: descriptor.max ?? 10 };
    const modeLabel = el('label', 'setting-control'); modeLabel.append(el('span', '', 'Filter style'));
    const mode = el('select', 'filter-mode');
    const modes = Array.isArray(descriptor.modes) ? descriptor.modes : ['hard', 'prefer'];
    for (const value of modes) {
      const option = el('option', '', value === 'hard' ? 'Hard filter' : 'Preferred range');
      option.value = value; option.selected = current.mode === value; mode.append(option);
    }
    mode.addEventListener('change', () => this.updateFilterDraft(key, 'mode', mode.value));
    modeLabel.append(mode); card.append(modeLabel);
    const rangeName = key === 'length' ? 'map length' : key.toUpperCase();
    for (const side of ['min', 'max']) {
      const label = el('label', 'setting-control'); label.append(el('span', '', `${side === 'min' ? 'Minimum' : 'Maximum'} ${rangeName}`));
      const min = bounds.min; const max = bounds.max;
      const range = el('input', 'setting-range'); range.type = 'range'; range.min = min; range.max = max;
      range.step = key === 'length' ? 1 : 0.1; range.value = current[side] ?? min;
      const numeric = el('input', 'setting-number'); numeric.type = 'number'; numeric.min = min; numeric.max = max;
      numeric.step = range.step; numeric.inputMode = 'decimal'; numeric.value = current[side] ?? min;
      range.addEventListener('input', () => { numeric.value = range.value; this.updateFilterDraft(key, side, number(range.value)); });
      numeric.addEventListener('input', () => {
        const parsed = number(numeric.value);
        if (parsed != null) range.value = String(Math.min(max, Math.max(min, parsed)));
        this.updateFilterDraft(key, side, parsed);
      });
      label.append(el('div', 'range-pair')); label.lastChild.append(range, numeric); card.append(label);
    }
    container.append(card);
  }

  renderUnsupportedFilters(container) {
    const inventory = this.inventory();
    const unsupported = inventory.unsupported_filters || inventory.unavailable_filters || {};
    const keys = new Set([...(Array.isArray(unsupported) ? unsupported : Object.keys(unsupported)),
      ...Object.keys(inventory.filters || {}).filter((key) => !SAFE_FILTER_KEYS.has(key)),
      ...Object.keys(inventory.filters || {}).filter((key) => SAFE_FILTER_KEYS.has(key)
        && this.capabilities().filters && (Array.isArray(this.capabilities().filters)
          ? !this.capabilities().filters.includes(key) : this.capabilities().filters[key] !== true))]);
    if (MANIA_PRESETS.has(this.booking.preset) && !inventory.filters?.ar) keys.add('ar');
    for (const key of keys) {
      if (SAFE_FILTER_KEYS.has(key) && inventory.filters?.[key]) continue;
      const label = key === 'length' ? 'Map length' : String(key).toUpperCase();
      container.append(el('p', 'field-help filter-unavailable', `${label} filtering is unavailable for this preset.`));
    }
  }

  updateFilterDraft(key, field, value) {
    if (!this.draft || value == null) return;
    if (!this.draft.selection_filters) this.draft.selection_filters = {};
    if (!this.draft.selection_filters[key]) {
      const bounds = this.inventory().filters?.[key] || {};
      this.draft.selection_filters[key] = { min: bounds.min ?? 0, max: bounds.max ?? 10, mode: 'prefer' };
    }
    if (value != null) this.draft.selection_filters[key][field] = value;
    show(byId('preview-result'), false);
    this.renderSettingsStatus();
  }

  updateDraft(key, rawValue) {
    const parsed = BOOLEAN_SETTING_KEYS.has(key) || CHOICE_SETTING_KEYS.has(key)
      || SINGLE_CHOICE_SETTING_KEYS.has(key) ? rawValue : number(rawValue);
    if (parsed == null || !this.draft) return;
    this.draft[key] = parsed;
    show(byId('preview-result'), false);
    this.renderSettingsStatus();
  }

  renderSettingsStatus() {
    const dirty = JSON.stringify(this.draft) !== JSON.stringify(this.appliedSettings);
    const cooldownUntil = number(this.booking?.cooldown_until, 0);
    const cooldown = Math.max(0, cooldownUntil - Date.now() / 1000);
    const latest = [...(this.booking?.operations || [])].reverse().find((item) => item.action === 'configure');
    byId('apply-settings').disabled = !dirty || Boolean(this.pendingMutation) || cooldown > 0 || this.conflict;
    show(byId('reload-settings'), this.conflict);
    byId('settings-state').textContent = this.conflict
      ? 'The server changed. Your draft is retained; review the values before applying.'
      : dirty && cooldown > 0
        ? `Draft has unapplied changes. Shared apply cooldown: ${Math.ceil(cooldown)} seconds`
        : dirty ? 'Draft has unapplied changes'
        : cooldown > 0 && ['queued', 'pending', 'dispatched', 'applied', 'failed', 'cancelled', 'refused'].includes(latest?.phase)
          ? `${this.settingsOutcomeMessage(latest)} Shared apply cooldown: ${Math.ceil(cooldown)} seconds`
          : cooldown > 0 ? `Shared apply cooldown: ${Math.ceil(cooldown)} seconds`
            : this.latestConfigureState();
    byId('settings-effects').textContent = this.settingsEffectMessage(latest, dirty);
  }

  latestConfigureState() {
    const latest = [...(this.booking?.operations || [])].reverse().find((item) => item.action === 'configure');
    return this.settingsOutcomeMessage(latest);
  }

  applySettings() {
    if (!this.can('configure') || !this.draft || !this.schema().length || this.pendingMutation || this.conflict) return;
    const payload = this.settingsPayload();
    this.mutate({ path: '/action', method: 'POST', kind: 'configure', body: {
      request_id: makeRequestId(), booking_id: this.booking.id,
      expected_revision: this.booking.revision, action: 'configure', payload,
    } });
  }

  reloadCurrentSettings() {
    if (!this.conflict || !this.booking || !this.can('configure')) return;
    this.draft = structuredClone(this.booking.settings || {});
    this.appliedSettings = structuredClone(this.booking.settings || {});
    this.conflict = false;
    this.settingsRenderKey = null;
    this.renderSession();
    byId('settings-controls').querySelector('input, select, button')?.focus?.();
  }

  previewSettings() {
    if (!this.booking || !this.can('configure') || this.pendingMutation) return;
    const payload = this.settingsPayload();
    this.mutate({ path: '/preview', method: 'POST', kind: 'preview', body: {
      request_id: makeRequestId(), booking_id: this.booking.id,
      preset: this.booking.preset, settings: payload,
    } });
  }

  settingsPayload() {
    const payload = {};
    const values = { ...(this.inventory().defaults || {}), ...(this.draft || {}) };
    for (const field of this.schema()) {
      const value = values[field.key];
      if (value !== undefined && value !== null) payload[field.key] = structuredClone(value);
    }
    if (values.selection_filters) {
      const availableFilters = this.inventory().filters || {};
      const selected = Object.fromEntries(Object.entries(values.selection_filters)
        .filter(([key]) => SAFE_FILTER_KEYS.has(key) && Object.hasOwn(availableFilters, key))
        .map(([key, value]) => [key, structuredClone(value)]));
      if (Object.keys(selected).length) payload.selection_filters = selected;
    }
    return payload;
  }

  renderOperations(operations) {
    const list = byId('operation-list'); clear(list);
    for (const operation of operations.slice(-8).reverse()) {
      const row = el('li', 'operation-row');
      const name = String(operation.action || 'Action').replaceAll('_', ' ');
      const state = operation.action === 'configure'
        ? this.settingsOperationLabel(operation)
        : operation.application === 'applied' || operation.phase === 'applied'
          ? 'Applied' : operation.phase === 'queued' ? 'Queued for host' : operation.phase || 'Received';
      row.append(el('span', '', name), el('span', 'operation-state', state));
      list.append(row);
    }
  }
}

if (typeof document !== 'undefined') {
  const portal = new BookingPortal();
  portal.init();
  globalThis.__privateBookingPortal = portal;
}
