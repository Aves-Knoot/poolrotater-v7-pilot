"""Pure player selection filters for qualified booking catalogs.

Only metadata that exists in the full catalog is exposed here. In particular,
OD and BPM are not inferred from unrelated difficulty or stream metadata.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
import copy


STANDARD_PRESETS = frozenset(('standard-auto', 'standard-fixed', 'standard-loved'))
MANIA_PRESETS = frozenset(('mania-4k', 'mania-7k'))
CATCH_PRESETS = frozenset(('catch-fixed',))
TAIKO_PRESETS = frozenset(('taiko-fixed','taiko-auto'))
PRESETS = STANDARD_PRESETS | MANIA_PRESETS | CATCH_PRESETS | TAIKO_PRESETS
FILTER_LIMITS = {'ar': (0.0, 12.0), 'length': (0.0, 3600.0)}
UNSUPPORTED_FILTERS = {
    'od': 'OD filtering is unavailable because the full catalog does not qualify OD metadata.',
    'bpm': 'BPM filtering is unavailable because the full catalog does not provide global BPM metadata.',
}
MODS = frozenset(('HR', 'EZ', 'DT', 'NC', 'HT', 'DC', 'HD', 'FL', 'NF', 'SD', 'PF', 'FI', 'MR'))
SPEED_MODS = frozenset(('DT', 'NC'))


class SelectionError(ValueError):
    """Invalid or unsupported selection filter input."""


class UnsupportedFilterError(SelectionError):
    """A requested filter has no qualified metadata in current catalogs."""


def _get(candidate, name, default=None):
    if isinstance(candidate, Mapping):
        return candidate.get(name, default)
    try:
        return candidate[name]
    except (KeyError, IndexError, TypeError, AttributeError):
        pass
    return getattr(candidate, name, default)


def _finite_number(value):
    return (type(value) in (int, float) and math.isfinite(value))


def _normalize_mods(required_mods):
    if required_mods is None:
        return frozenset()
    if (isinstance(required_mods, (str, bytes))
            or not isinstance(required_mods, (tuple, list, set, frozenset)) or len(required_mods) > 16):
        raise SelectionError('required_mods must be a collection of mod acronyms')
    result = set()
    for item in required_mods:
        if isinstance(item, Mapping):
            item = item.get('acronym')
        elif not isinstance(item, str):
            item = getattr(item, 'acronym', None)
        if not isinstance(item, str) or not item:
            raise SelectionError('required_mods contains an invalid mod')
        mod = item.upper()
        if mod not in MODS:
            raise SelectionError('required_mods contains an unsupported mod')
        result.add(mod)
    return frozenset(result)


def validate_filters(filters, preset):
    """Return a checked copy; reject filters without qualified catalog data."""
    if not isinstance(preset, str) or preset not in PRESETS:
        raise SelectionError('unsupported preset')
    if not isinstance(filters, Mapping):
        raise SelectionError('filters must be an object')
    result = {}
    for key, value in filters.items():
        if key in UNSUPPORTED_FILTERS:
            raise UnsupportedFilterError(UNSUPPORTED_FILTERS[key])
        if key not in FILTER_LIMITS:
            raise UnsupportedFilterError('This filter is unavailable for the current catalog.')
        if key == 'ar' and preset in MANIA_PRESETS:
            raise UnsupportedFilterError('AR filtering is unavailable for Mania presets.')
        if preset in (CATCH_PRESETS|TAIKO_PRESETS) and key == 'ar':
            raise UnsupportedFilterError('AR filtering is unavailable for Catch in current qualified catalogs.')
        if not isinstance(value, Mapping) or set(value) != {'min', 'max', 'mode'}:
            raise SelectionError('filter requires min, max, and mode')
        low, high = FILTER_LIMITS[key]
        minimum, maximum = value['min'], value['max']
        if (not _finite_number(minimum) or not _finite_number(maximum)
                or not low <= minimum <= maximum <= high):
            raise SelectionError('filter range is invalid')
        mode = value['mode']
        if mode not in ('hard', 'prefer'):
            raise SelectionError('filter mode is invalid')
        result[key] = {'min': float(minimum), 'max': float(maximum), 'mode': mode}
    return result


def _effective_ar(candidate, preset, required_mods):
    if preset in MANIA_PRESETS:
        return None
    base = _get(candidate, 'approach_rate')
    if not _finite_number(base) or not 0 <= base <= 12:
        return None
    ar = float(base)
    if preset in CATCH_PRESETS:
        return ar
    if 'HR' in required_mods:
        ar = min(10.0, ar * 1.4)
    elif 'EZ' in required_mods:
        ar /= 2.0

    is_dt = _get(candidate, 'is_dt', False) is True
    clock_rate = _get(candidate, 'clock_rate', 1.0)
    if not _finite_number(clock_rate) or clock_rate <= 0:
        clock_rate = 1.0
    if clock_rate <= 1.0 and required_mods & SPEED_MODS:
        clock_rate = 1.5
    if is_dt and clock_rate <= 1.0:
        clock_rate = 1.5
    if clock_rate <= 1.0:
        return ar

    # Catalog DT rows carry the source NoMod AR and an already-adjusted length.
    # Transform AR from the source preempt once; never transform length here.
    preempt = 1200.0 + 120.0 * (5.0 - ar) if ar < 5.0 else 1200.0 - 150.0 * (ar - 5.0)
    preempt /= float(clock_rate)
    return 5.0 + (1200.0 - preempt) / (120.0 if preempt > 1200.0 else 150.0)


def _feature(candidate, key, preset, required_mods):
    if key == 'ar':
        return _effective_ar(candidate, preset, required_mods)
    if key == 'length':
        value = _get(candidate, 'hit_length')
        if not _finite_number(value) or value < 0:
            return None
        # PoolMap.hit_length for a DT catalog row is already clock-adjusted.
        return float(value)
    return None


def _matches(value, spec):
    return value is not None and spec['min'] <= value <= spec['max']


def apply_candidates(candidates, filters, preset, required_mods=()):
    """Apply hard filters without mutating the candidate list or pool."""
    checked = validate_filters(filters, preset)
    mods = _normalize_mods(required_mods)
    result = []
    for candidate in candidates:
        include = True
        for key, spec in checked.items():
            if spec['mode'] != 'hard':
                continue
            feature = _feature(candidate, key, preset, mods)
            if not _matches(feature, spec):
                include = False
                break
        if include:
            result.append(candidate)
    return result


def weights(candidates, filters, preset, required_mods=()):
    """Return multiplicative preference weights (2 in range, 1 outside).

    Missing preference metadata remains neutral. A preferred filter never
    removes a candidate; hard eligibility belongs to ``apply_candidates``.
    """
    checked = validate_filters(filters, preset)
    mods = _normalize_mods(required_mods)
    result = []
    for candidate in candidates:
        weight = 1.0
        for key, spec in checked.items():
            if spec['mode'] != 'prefer':
                continue
            if _matches(_feature(candidate, key, preset, mods), spec):
                weight *= 2.0
        result.append(weight)
    return result


def _candidate_list(pool):
    if isinstance(pool, (list, tuple)):
        return pool
    candidates = getattr(pool, 'maps', None)
    if isinstance(candidates, (list, tuple)):
        return candidates
    raise SelectionError('preview requires a read-only candidate pool')


def preview(pool, filters, preset, required_mods=()):
    """Pure preview with a bounded, title-free sample; the pool is untouched."""
    candidates = _candidate_list(pool)
    selected = apply_candidates(candidates, filters, preset, required_mods)
    samples = []
    for candidate in selected[:5]:
        beatmap_id = _get(candidate, 'beatmap_id')
        sr = _get(candidate, 'sr')
        hit_length = _get(candidate, 'hit_length')
        sample = {}
        if type(beatmap_id) is int and beatmap_id > 0:
            sample['beatmap_id'] = beatmap_id
        if _finite_number(sr):
            sample['sr'] = float(sr)
        if _finite_number(hit_length):
            sample['hit_length'] = int(hit_length)
        if sample:
            samples.append(sample)
    return {'count': len(selected), 'samples': samples}


class _BookingSelectionAdapter:
    """Instance-level guard that leaves native selection and cooldown policy intact."""

    def __init__(self, pool):
        self.pool = pool
        self.original_next_map = getattr(pool, 'next_map', None)
        self.original_weight = getattr(pool, '_selection_weight', None)
        self.original_rows = getattr(pool, '_rows', None)
        self.original_convert_rows = getattr(pool, '_convert_rows', None)
        self.original_base_random = getattr(pool, '_base_random', None)
        self.settings = {}
        self.filters = {}
        self.preset = None
        self.required_mods = ()
        self.dt_mode = None
        self.last_candidate_count = 0
        self.base_random_wrapper = None

    def configure(self, settings, preset):
        if not isinstance(settings, Mapping):
            raise SelectionError('booking settings must be an object')
        if not callable(self.original_next_map) or not isinstance(getattr(self.pool, 'maps', None), list):
            raise SelectionError('booking pool does not support native selection')
        self.settings = copy.deepcopy(dict(settings))
        self.preset = preset
        self.filters = validate_filters(self.settings.get('selection_filters', {}), preset)
        self.required_mods = _normalize_mods(self.settings.get('required_mods', ()))
        self.dt_mode = self.settings.get('dt_mode')
        if self.dt_mode is not None and self.dt_mode not in ('auto', 'off', 'mixed', 'full'):
            raise SelectionError('DT mode is invalid')
        self._configure_dt_mode()
        self._install_wrappers()
        self.last_candidate_count = self.candidate_count()
        setattr(self.pool, '_booking_hard_candidate_count', self.last_candidate_count)
        return self.last_candidate_count

    def _configure_dt_mode(self):
        if not getattr(self.pool, 'mixed_dt', False):
            if self.dt_mode in ('mixed', 'full'):
                raise UnsupportedFilterError('This room has no qualified DT candidate catalog.')
            return
        configure = getattr(self.pool, 'set_dt_override', None)
        if not callable(configure):
            if self.dt_mode in ('mixed', 'full'):
                raise SelectionError('This room cannot safely change its DT policy.')
            return
        target = {
            None: (None, False),
            'auto': (None, False),
            'off': (False, False),
            'mixed': (True, False),
            'full': (True, True),
        }[self.dt_mode]
        current = (getattr(self.pool, 'dt_override', None), bool(getattr(self.pool, 'dt_full', False)))
        if current != target:
            try:
                configure(target[0], full=target[1])
            except Exception:
                raise SelectionError('The room could not apply this DT mode.') from None

    def _eligible(self, candidates, *, native_only=False):
        selected = apply_candidates(candidates or (), self.filters, self.preset, self.required_mods)
        if not getattr(self.pool, 'mixed_dt', False):
            return selected
        mode = self.dt_mode
        dt_enabled = getattr(self.pool, 'dt_enabled', None)
        catalog_ready = getattr(self.pool, 'dt_catalog_ready', None)
        ready = True
        try:
            if callable(dt_enabled):
                ready = bool(dt_enabled())
            if callable(catalog_ready):
                ready = ready and bool(catalog_ready())
        except Exception:
            ready = False
        excluded = getattr(self.pool, '_dt_excluded', None) or set()
        compatible_mods = not (set(self.required_mods) - {'NF'})
        return [item for item in selected if not _get(item, 'is_dt', False) or
                (compatible_mods and mode != 'off' and not native_only and ready
                 and _get(item, 'beatmap_id') not in excluded)]

    def _install_wrappers(self):
        pool = self.pool

        def next_map(*args, **kwargs):
            original_maps = pool.maps
            candidates = self._eligible(original_maps, native_only=bool(kwargs.get('native_only', False)))
            self.last_candidate_count = len(candidates)
            if not candidates and not getattr(pool, 'mania_mode', False):
                return None
            pool.maps = candidates
            try:
                # Mania can query qualified fallback rows after strict styles
                # are exhausted; its query methods are wrapped below.
                return self.original_next_map(*args, **kwargs)
            finally:
                pool.maps = original_maps

        pool.next_map = next_map

        if callable(self.original_weight):
            def selection_weight(item, good, bad, *args, **kwargs):
                native_weight = self.original_weight(item, good, bad, *args, **kwargs)
                factor = weights([item], self.filters, self.preset, self.required_mods)[0]
                return native_weight * factor
            pool._selection_weight = selection_weight

        if getattr(pool, 'mania_mode', False) and callable(self.original_rows):
            def rows(*args, **kwargs):
                return self._eligible_rows(self.original_rows(*args, **kwargs))
            pool._rows = rows
            if callable(self.original_convert_rows):
                def convert_rows(*args, **kwargs):
                    return self._eligible_rows(self.original_convert_rows(*args, **kwargs))
                pool._convert_rows = convert_rows

        if (getattr(pool, 'catch_mode', False) and self.original_base_random is not None
                and self.base_random_wrapper is None):
            self.base_random_wrapper = _CatchPreferenceRandom(self, self.original_base_random)
            pool._base_random = self.base_random_wrapper

    def _eligible_rows(self, rows):
        return apply_candidates(rows or (), self.filters, self.preset, self.required_mods)

    def candidate_count(self):
        current = self._eligible(getattr(self.pool, 'maps', ()))
        if current:
            return len(current)
        if (getattr(self.pool, 'mania_mode', False)
                and getattr(self.pool, 'mania_style_fallback_enabled', False)
                and getattr(self.pool, 'mania_source_mode', '') != 'variety'
                and callable(getattr(self.pool, '_rows', None))):
            return len(self.pool._rows(ignore_style=True))
        return 0

    def restore(self):
        pool = self.pool
        if callable(self.original_next_map):
            pool.next_map = self.original_next_map
        if callable(self.original_weight):
            pool._selection_weight = self.original_weight
        if callable(self.original_rows):
            pool._rows = self.original_rows
        if callable(self.original_convert_rows):
            pool._convert_rows = self.original_convert_rows
        if self.original_base_random is not None:
            pool._base_random = self.original_base_random
        for name in ('_booking_selection_adapter', '_booking_hard_candidate_count'):
            if hasattr(pool, name):
                delattr(pool, name)


class _CatchPreferenceRandom:
    """Apply booking preference after Catch's native duration-bucket scaling."""

    def __init__(self, adapter, original):
        self.adapter = adapter
        self.original = original

    def __getattr__(self, name):
        # Keep the native RNG interface available to diagnostics/tests while
        # exposing only the narrow choices override used by Catch.
        return getattr(self.original, name)

    def choices(self, population, weights=None, *, cum_weights=None, k=1):
        if cum_weights is not None or weights is None:
            return self.original.choices(population, weights=weights, cum_weights=cum_weights, k=k)
        if len(population) != len(weights):
            return self.original.choices(population, weights=weights, k=k)
        adjusted = []
        maps = getattr(self.adapter.pool, '_all_maps', {})
        for value, native_weight in zip(population, weights):
            map_id = _get(value, 'map_id')
            item = maps.get(map_id)
            factor = (weights_fn(self.adapter, item)
                      if item is not None else 1.0)
            adjusted.append(native_weight * factor)
        return self.original.choices(population, weights=adjusted, k=k)


def weights_fn(adapter, item):
    return weights([item], adapter.filters, adapter.preset, adapter.required_mods)[0]


def install_selection(pool, settings, preset):
    """Install an idempotent booking-only filter around native pool selection.

    Hard filters are applied to each fresh selection candidate set and to Mania
    query-based fallback rows before native conversion/style logic runs. Native
    repeat cooldowns, feedback, source weights, and style selection stay active.
    """
    adapter = getattr(pool, '_booking_selection_adapter', None)
    if adapter is None:
        adapter = _BookingSelectionAdapter(pool)
        setattr(pool, '_booking_selection_adapter', adapter)
    return adapter.configure(settings, preset)


def clear_selection(pool):
    """Remove the booking adapter, preserving the pool's original methods."""
    adapter = getattr(pool, '_booking_selection_adapter', None)
    if adapter is not None:
        adapter.restore()


def _qualified_runtime_state(old_pool, new_pool):
    """Carry cooldowns and reservations across a fresh read-only pool build."""
    for name in ('_recent', '_recent_songs', '_song_rounds', '_round_history',
                 '_playlist_reserved', '_dt_excluded', '_runtime_excluded',
                 '_operator_excluded', '_download_excluded'):
        if hasattr(old_pool, name) and hasattr(new_pool, name):
            previous = copy.deepcopy(getattr(old_pool, name))
            current = getattr(new_pool, name)
            if name.endswith('_excluded') and isinstance(previous, set) and isinstance(current, set):
                previous |= current
            setattr(new_pool, name, previous)
    old_selector = getattr(old_pool, 'selector', None)
    new_selector = getattr(new_pool, 'selector', None)
    if old_selector is not None and new_selector is not None:
        new_selector.history = copy.deepcopy(getattr(old_selector, 'history', []))
        new_selector.now = getattr(old_selector, 'now', 0.0)


def apply_booking_settings(bot, settings, preset, *, materialize, before_commit=None):
    """Atomically apply session settings using a caller-qualified read-only pool.

    ``materialize(staged_cfg)`` must use the current managed catalog binding and
    must not perform provider operations or write shared configuration. When
    supplied, ``before_commit(effective_settings, application_receipt)`` runs
    after the candidate passes qualification and before worker state changes;
    returning ``False`` cancels the in-memory commit.
    """
    if not callable(materialize):
        raise SelectionError('a qualified pool materializer is required')
    if not isinstance(settings, Mapping) or not isinstance(getattr(bot, 'cfg', None), dict):
        raise SelectionError('booking settings cannot be applied to this worker')
    from .settings import FIELDS as BOOKING_FIELDS, inventory, validate_player_patch
    from ..config import PRESETS

    if preset not in PRESETS:
        raise SelectionError('unsupported booking preset')
    expected_ruleset = PRESETS[preset]['ruleset']
    if bot.cfg.get('ruleset_id', 0) != expected_ruleset:
        raise SelectionError('booking preset does not match this native room')

    prior_session = getattr(bot, '_booking_session_settings', None)
    if not isinstance(prior_session, dict):
        # Seed non-native controls (notably filters and DT mode) from the
        # public preset defaults, then prefer the worker's actual effective
        # native configuration. This also makes full settings snapshots safe
        # to submit without falsely reporting every default as a new change.
        prior_session = copy.deepcopy(inventory(preset)['defaults'])
        prior_session.update({key: copy.deepcopy(bot.cfg[key])
                              for key in BOOKING_FIELDS if key in bot.cfg})
    try:
        patch = validate_player_patch(dict(settings), current=prior_session, preset=preset)
    except Exception as exc:
        raise SelectionError('Booking settings are not valid for this preset.') from exc
    effective = copy.deepcopy(prior_session)
    effective.update(patch)
    staged_cfg = copy.deepcopy(bot.cfg)
    native_patch = {key: copy.deepcopy(value) for key, value in effective.items()
                    if key not in ('selection_filters', 'dt_mode')}
    staged_cfg.update(native_patch)
    overrides = copy.deepcopy(staged_cfg.get('_supervisor_overrides') or {})
    overrides.update(copy.deepcopy(effective))
    staged_cfg['_supervisor_overrides'] = overrides

    try:
        candidate = materialize(staged_cfg)
        if candidate is None:
            raise SelectionError('The room could not prepare this settings change.')
        if (expected_ruleset == 3 and (
                    not getattr(candidate, 'mania_mode', False)
                    or getattr(candidate, 'mania_keys', None) != PRESETS[preset]['keys'])
                or expected_ruleset == 2 and not getattr(candidate, 'catch_mode', False)
                or expected_ruleset == 0 and (
                    getattr(candidate, 'mania_mode', False)
                    or getattr(candidate, 'catch_mode', False)
                    or bool(getattr(candidate, 'loved_mode', False)) != (preset == 'standard-loved'))):
            raise SelectionError('The prepared pool does not match this room preset.')
        if callable(getattr(candidate, 'configure_duration_policy', None)):
            candidate.configure_duration_policy(staged_cfg)
        if callable(getattr(candidate, 'configure_jump_policy', None)):
            candidate.configure_jump_policy(staged_cfg)
        if (effective.get('selection_styles') is not None
                and prior_session.get('selection_styles') != effective.get('selection_styles')):
            from ..managed_host.native import apply_pool_styles
            apply_pool_styles(candidate, staged_cfg)
        _qualified_runtime_state(getattr(bot, 'pool', None), candidate)
        count = install_selection(candidate, effective, preset)
        if not count:
            raise SelectionError('No qualified maps match these room settings.')
    except SelectionError:
        raise
    except Exception as exc:
        raise SelectionError('The room could not apply these settings safely.') from exc

    changed = {key for key, value in effective.items()
               if key not in prior_session or prior_session[key] != value}
    application = 'next_map' if any(key in changed for key in ('selection_filters', 'dt_mode',
                      'selection_styles', 'sr_min', 'sr_max', 'auto_difficulty',
                      'allowed_mods', 'required_mods', 'freestyle', 'max_length', 'real_max_length',
                      'preferred_min_length', 'preferred_max_length', 'preferred_length_share')) else 'live'
    receipt = {'application': application, 'candidate_count': count}
    if before_commit is not None:
        if not callable(before_commit):
            raise SelectionError('invalid booking settings commit callback')
        if before_commit(copy.deepcopy(effective), copy.deepcopy(receipt)) is False:
            raise SelectionError('The settings request is no longer active.')
    bot.cfg.clear()
    bot.cfg.update(staged_cfg)
    bot.pool = candidate
    bot._booking_session_settings = effective
    bot._booking_preset = preset
    if hasattr(bot, 'manual_range'):
        bot.manual_range = not bool(effective.get('auto_difficulty', False))
    if effective.get('auto_difficulty') is True and hasattr(bot, '_auto_retune_pending'):
        bot._auto_retune_pending = True
    return receipt


class SelectionPreview:
    """Selection preview facade over an explicitly supplied read-only pool.

    ``catalogs`` binds known presets to a factory or a safe catalog reference.
    This layer never opens paths or mutates a pool; integration supplies a
    qualified pool after checking its catalog binding.
    """

    def __init__(self, catalogs):
        if not isinstance(catalogs, Mapping) or set(catalogs) - PRESETS:
            raise SelectionError('invalid preview catalog map')
        self.catalogs = dict(catalogs)

    def preview(self, pool, filters, preset, required_mods=()):
        if preset not in self.catalogs:
            raise SelectionError('preset has no preview catalog')
        return preview(pool, filters, preset, required_mods)
