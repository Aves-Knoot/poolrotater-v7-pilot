import test from 'node:test';
import assert from 'node:assert/strict';
import { friendlyError, makeRequestId, normalizeSchema } from '../poolrotater_host/bookings/static/app.js';

test('request IDs are UUIDs and distinct', () => {
  const first = makeRequestId();
  const second = makeRequestId();
  assert.match(first, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.notEqual(first, second);
});

test('settings inventory keeps only advertised safe fields', () => {
  const schema = normalizeSchema([
    'sr_min', 'sr_max', 'auto_difficulty', 'selection_styles', 'allowed_mods', 'dt_mode', 'room_name', 'password',
  ], { settings: ['sr_min', 'selection_styles', 'allowed_mods', 'dt_mode'] });
  assert.deepEqual(schema.map((field) => field.key), ['sr_min', 'selection_styles', 'allowed_mods', 'dt_mode']);
});

test('friendly errors never surface backend detail', () => {
  assert.equal(friendlyError('booking_settings_refused', 400), 'Those settings are not compatible with this room. Adjust the draft and try again.');
  assert.equal(friendlyError('arbitrary_internal_error', 500), 'We could not complete that request. Please try again.');
  assert.equal(friendlyError(undefined, 403), 'You do not have permission for that action.');
});
