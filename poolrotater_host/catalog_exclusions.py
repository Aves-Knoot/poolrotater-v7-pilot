"""Signed bounded exclusion events and real-reader/playlist enforcement.

v1 catalogs omit beatmapset IDs. Set events therefore carry explicit signed
member lists bound to content IDs; an unknown version refuses future selection.
Runtime download failures remain independent of this policy.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import stat
import sqlite3

from .catalog_export import MODES, EXCLUDED, canonical, sha256
from .catalog_trust import bounded_file
from .config import ConfigError, fields
from .layout import unique_fields

MAX_ENTRIES = 256
REASONS = {'missing_audio', 'corrupt', 'unavailable', 'moderation'}


def validate_feed(feed):
    try: return _validate_feed(feed)
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise ConfigError('invalid closed exclusion feed fields') from exc


def _validate_feed(feed):
    fields(feed, ('format', 'revision', 'events'), 'exclusion feed')
    if (type(feed['format']) is not int or feed['format'] != 1
            or type(feed['revision']) is not int or not 1 <= feed['revision'] <= 2**53
            or not isinstance(feed['events'], list) or len(feed['events']) > MAX_ENTRIES):
        raise ConfigError('invalid bounded exclusion feed')
    seen = set()
    for event in feed['events']:
        fields(event, ('kind', 'id', 'modes', 'action', 'reason', 'memberships'), 'exclusion event')
        if (event['kind'] not in ('beatmap', 'beatmapset') or type(event['id']) is not int
                or not 0 < event['id'] <= 2**53 or event['action'] not in ('exclude', 'reverse')
                or event['reason'] not in (REASONS if event['action']=='exclude' else {'reversed'})
                or not isinstance(event['modes'], list) or not event['modes']
                or len(event['modes']) != len(set(event['modes'])) or not set(event['modes']) <= set(MODES)
                or not isinstance(event['memberships'], dict)):
            raise ConfigError('invalid exclusion event')
        if event['kind'] == 'beatmap' or event['action'] == 'reverse':
            if event['memberships']: raise ConfigError('unexpected exclusion member bindings')
        else:
            if set(event['memberships']) != set(event['modes']): raise ConfigError('set exclusion needs every scoped mode binding')
            for mode, bindings in event['memberships'].items():
                if not isinstance(bindings, list) or not 1 <= len(bindings) <= 8: raise ConfigError('invalid set binding budget')
                identities = set()
                for binding in bindings:
                    fields(binding, ('content_id', 'beatmap_ids'), 'set membership binding')
                    identity, ids = binding['content_id'], binding['beatmap_ids']
                    if (not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{64}', identity)
                            or identity in identities or not isinstance(ids, list) or len(ids) > 4096
                            or any(type(bid) is not int or not 0 < bid <= 2**53 for bid in ids)
                            or ids != sorted(set(ids))): raise ConfigError('invalid signed set membership')
                    identities.add(identity)
        for mode in event['modes']:
            key = (mode, event['kind'], event['id'])
            if key in seen: raise ConfigError('overlapping exclusion event scopes')
            seen.add(key)
    if len(canonical(feed)) > 65536: raise ConfigError('exclusion feed size budget exceeded')
    return feed


def initial_state(bootstrap):
    return {'format': 1, 'bootstrap_sha256': bootstrap, 'revision': 0, 'target_sha256': None, 'events': []}


def validate_state(state, bootstrap):
    fields(state, ('format', 'bootstrap_sha256', 'revision', 'target_sha256', 'events'), 'installed exclusions')
    if (type(state['format']) is not int or state['format'] != 1 or state['bootstrap_sha256'] != bootstrap
            or type(state['revision']) is not int or not 0 <= state['revision'] <= 2**53
            or (state['revision'] and (not isinstance(state['target_sha256'], str)
                                     or not re.fullmatch('[a-f0-9]{64}', state['target_sha256'])))
            or (not state['revision'] and (state['events'] or state['target_sha256'] is not None))):
        raise ConfigError('exclusion receipt identity changed')
    if state['revision']: validate_feed({'format': 1, 'revision': state['revision'], 'events': state['events']})
    if any(e['action'] != 'exclude' for e in state['events']): raise ConfigError('installed exclusion state contains reversals')
    return state


def apply_feed(state, feed, digest):
    validate_feed(feed)
    if feed['revision'] < state['revision'] or (feed['revision'] == state['revision'] and digest != state['target_sha256']):
        raise ConfigError('exclusion feed replay or changed same revision')
    if feed['revision'] == state['revision']: return state
    active = {(mode, e['kind'], e['id']): {**copy.deepcopy(e), 'modes': [mode],
               'memberships': {mode: e['memberships'][mode]} if e['memberships'] else {}}
              for e in state['events'] for mode in e['modes']}
    for event in feed['events']:
        for mode in event['modes']:
            key = (mode, event['kind'], event['id'])
            if event['action'] == 'reverse':
                if event['kind'] == 'beatmap' and event['id'] in EXCLUDED:
                    raise ConfigError('known missing-audio reversal requires separate explicit owner policy')
                active.pop(key, None)
            else:
                active[key] = {**copy.deepcopy(event), 'modes': [mode],
                               'memberships': {mode: event['memberships'][mode]} if event['memberships'] else {}}
    result = {**state, 'revision': feed['revision'], 'target_sha256': digest,
              'events': [active[key] for key in sorted(active)]}
    validate_state(result, state['bootstrap_sha256'])
    if len(canonical(result)) > 65536: raise ConfigError('installed exclusion state budget exceeded')
    return result


def membership_event(source, bundle, set_id, *, reason='missing_audio'):
    """Publisher expands one set using a matching isolated source, never an API."""
    from .catalog_export import inspect_bundle, source as source_snapshot
    manifest = inspect_bundle(bundle); mode = manifest['mode']
    with source_snapshot(source) as (db, identity):
        if identity != manifest['sources']['catalog']: raise ConfigError('set expansion source differs from catalog provenance')
        table = 'beatmaps' if mode in ('standard','loved') else 'maps'
        if 'beatmapset_id' not in {r[1] for r in db.execute(f'PRAGMA table_info({table})')}:
            raise ConfigError('publisher source lacks beatmapset identity')
        ids = sorted(r[0] for r in db.execute(f'SELECT beatmap_id FROM {table} WHERE beatmapset_id=?', (set_id,))
                     if r[0] not in EXCLUDED)
    return {'kind': 'beatmapset', 'id': set_id, 'modes': [mode], 'action': 'exclude', 'reason': reason,
            'memberships': {mode: [{'content_id': manifest['content_id'], 'beatmap_ids': ids}]}}


class ExclusionPolicy:
    def __init__(self, store, mode, content_id):
        self.store, self.mode, self.content_id = store, mode, content_id
        self.revision = None; self.blocked_ids = set(EXCLUDED); self.unknown_membership = False
        self._stamp = None; self._target = None

    def refresh(self):
        path = self.store.path/('exclusions-'+self.mode+'.json' if self.mode in ('catch','loved') else 'exclusions.json')
        try:
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise ConfigError('unsafe installed exclusion policy')
            stamp = (st.st_dev,st.st_ino,st.st_ctime_ns,st.st_mtime_ns,st.st_size,st.st_mode,st.st_uid)
        except FileNotFoundError: stamp = None
        if stamp == self._stamp and self.revision is not None: return False
        state = self.store.exclusions(mode=self.mode) if self.mode in ('catch','loved') else self.store.exclusions()
        if self.revision is not None and (state['revision'] < self.revision
                or (state['revision'] == self.revision and state['target_sha256'] != self._target)):
            raise ConfigError('installed exclusion state regressed during reader lifetime')
        blocked, unknown = set(EXCLUDED), False
        for event in state['events']:
            if self.mode not in event['modes']: continue
            if event['kind'] == 'beatmap': blocked.add(event['id'])
            else:
                binding = next((b for b in event['memberships'][self.mode] if b['content_id'] == self.content_id), None)
                if binding is None: unknown = True
                else: blocked.update(binding['beatmap_ids'])
        self.blocked_ids, self.unknown_membership, self.revision = blocked, unknown, state['revision']
        self._stamp, self._target = stamp, state['target_sha256']
        return True

    def allowed(self, item):
        self.refresh()
        return item is not None and not self.unknown_membership and int(item.beatmap_id) not in self.blocked_ids


class _ExcludedReader:
    """Mixin into existing readers; preserve their complete mutation interface."""
    def _filter_rows(self, rows):
        policy = self._host_exclusion_policy; policy.refresh()
        return [r for r in rows if not policy.unknown_membership and int(r['beatmap_id']) not in policy.blocked_ids]

    def _rows(self, *args, **kwargs): return self._filter_rows(super()._rows(*args, **kwargs))
    def _convert_rows(self, *args, **kwargs): return self._filter_rows(super()._convert_rows(*args, **kwargs))
    def _dt_rows(self, *args, **kwargs): return self._filter_rows(super()._dt_rows(*args, **kwargs))

    def reload(self):
        super().reload()
        self.maps = [m for m in self.maps if self._host_exclusion_policy.allowed(m)]
        self._host_applied_revision = self._host_exclusion_policy.revision
        return len(self.maps)

    def next_map(self, *args, **kwargs):
        self._host_exclusion_policy.refresh()
        if self._host_applied_revision != self._host_exclusion_policy.revision: self.reload()
        self.maps = [m for m in self.maps if self._host_exclusion_policy.allowed(m)]
        return super().next_map(*args, **kwargs)

    def get_map(self, *args, **kwargs):
        result = super().get_map(*args, **kwargs)
        return result if self._host_exclusion_policy.allowed(result) else None

    def get_variant_map(self, *args, **kwargs):
        result = super().get_variant_map(*args, **kwargs)
        return result if self._host_exclusion_policy.allowed(result) else None


_READERS = {}
def attach_policy(pool, policy):
    original = type(pool)
    if original not in _READERS: _READERS[original] = type('Excluded'+original.__name__, (_ExcludedReader, original), {})
    pool.__class__ = _READERS[original]; pool._host_exclusion_policy = policy
    policy.refresh(); pool.maps = [m for m in pool.maps if policy.allowed(m)]
    pool._host_applied_revision = policy.revision
    return pool


def reconcile_exclusions(bot):
    """Remove known blocked future entries; preserve an active match exactly."""
    policy = getattr(bot.pool, '_host_exclusion_policy', None)
    if policy is None: return
    policy.refresh()
    for item_id, item in list(bot.queued):
        if policy.allowed(item): continue
        if item_id is not None and item_id in (getattr(bot,'playing_item_id',None),getattr(bot,'starting_item_id',None)):
            continue  # Never remove the actual active/counting-down item as a future entry.
        if item_id is None:
            # Existing playlist audit resolves unknown IDs, never invent one.
            bot.reconcile_playlist(reason='exclusion queued-id resolution', fetch_remote=True,
                                   ensure_depth=False, trim_extras=False)
            return
        bot._referee_operation_with_rejoin('RemovePlaylistItem',
            lambda item_id=item_id: bot.hub.remove_playlist_item(bot.room_id, item_id))
        bot.queued.remove((item_id, item)); bot._playlist_maps.pop(item_id, None); bot._playlist_dirty = True
    from bot.bot import IDLE, STARTING
    if bot.state == STARTING and not policy.allowed(bot.starting_map or bot.current_map):
        bot._stop_match_countdown(); bot.starting_map = None; bot.starting_item_id = None
        bot._clear_start_recovery_tracking(); bot.set_state(IDLE)
    if bot.state == IDLE and bot.current_map is not None and not policy.allowed(bot.current_map):
        bot.skip_map()


class ExclusionLobbyMixin:
    def _map_is_downloadable(self, item, **kwargs):
        policy = getattr(self.pool, '_host_exclusion_policy', None)
        return bool((policy is None or policy.allowed(item)) and super()._map_is_downloadable(item, **kwargs))

    def apply_map(self, item, *args, **kwargs):
        policy = getattr(self.pool, '_host_exclusion_policy', None)
        if policy is not None and not policy.allowed(item): raise ConfigError('map is excluded from this managed catalog')
        return super().apply_map(item, *args, **kwargs)

    def _append_future_map(self, item):
        policy = getattr(self.pool, '_host_exclusion_policy', None)
        if policy is not None and not policy.allowed(item): raise ConfigError('future map is excluded')
        return super()._append_future_map(item)

    def start_match(self):
        reconcile_exclusions(self)
        policy = getattr(self.pool, '_host_exclusion_policy', None)
        if policy is not None and not policy.allowed(self.current_map): return
        return super().start_match()
