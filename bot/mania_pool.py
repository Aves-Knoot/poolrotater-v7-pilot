"""Read-only native 4K/7K pool adapter for Mania Finder's maps schema.

No copy of the multi-million-score database is needed. Structural and empirical
tables are optional; an unclassified catalog remains playable in variety mode.
"""
from __future__ import annotations

import logging
import math
import sqlite3
from pathlib import Path

from .pool import MapPool, PoolMap
from .composition import COMPOSITIONS, composition_score

STYLES = {"jack": "jack_score", "chordjack": "chordjack_score",
          "stream": "stream_score", "ln": "ln_score", "technical": "technical_score"}
ALL_STYLES = (*STYLES, 'rice', 'noodle')
STYLE_EVIDENCE_COLUMNS = (
    "max_jack_run", "jack_ratio", "hold_ratio", "max_chordjack_run",
    "chordjack_event_ratio", "max_stream_run", "stream_event_ratio",
    "chord_event_ratio", "hold_coverage", "rhythm_cv",
)
log = logging.getLogger("mania_pool")

class ManiaPool(MapPool):
    mania_mode = True

    def __init__(self, db_path, *, mania_source_mode="variety", mania_min_length=45,
                 mania_style_min_score=0.5, mania_farm_enabled=False,
                 mania_min_rank_score=0.5, mania_min_confidence=0.9,
                 mania_min_appearances=100, mania_min_bands=3, mania_keys=4,
                 mania_farm_db="data/mania_farm.db",
                 mania_farm_bonus_max=0.30,
                 mania_farm_bonus_min_confidence=0.50,
                 mania_farm_high_score_threshold=70.0,
                 mania_farm_pick_multiplier=1.25,
                 mania_high_star_relax_style=False,
                 mania_style_fallback_enabled=False,
                 mania_convert_db="",
                 mania_convert_validation_db=None,
                 mania_convert_max_share=0.30,
                 mania_ln_pick_multiplier=0.10, **kwargs):
        try:
            self.mania_keys = int(mania_keys)
        except (TypeError, ValueError) as exc:
            raise ValueError("Mania key count must be native 4K or 7K") from exc
        if self.mania_keys not in (4, 7):
            raise ValueError("Only native 4K and 7K are supported")
        self._overrep_table = (
            "mania_overrep_report" if self.mania_keys == 4
            else "mania_7k_overrep_report"
        )
        if mania_source_mode not in {"variety", "farm", *ALL_STYLES}:
            raise ValueError("Unknown mania_source_mode")
        self.mania_source_mode = mania_source_mode
        if (isinstance(mania_ln_pick_multiplier, bool)
                or not isinstance(mania_ln_pick_multiplier, (int, float))
                or not math.isfinite(mania_ln_pick_multiplier)
                or not 0 < mania_ln_pick_multiplier <= 1):
            raise ValueError('mania_ln_pick_multiplier must be a finite number in (0, 1]')
        self.mania_ln_pick_multiplier = float(mania_ln_pick_multiplier)
        self.mania_min_length = int(mania_min_length)
        self.mania_style_min_score = float(mania_style_min_score)
        self.mania_farm_enabled = bool(mania_farm_enabled)
        self.mania_min_rank_score = float(mania_min_rank_score)
        self.mania_min_confidence = float(mania_min_confidence)
        self.mania_min_appearances = int(mania_min_appearances)
        self.mania_min_bands = int(mania_min_bands)
        self.mania_farm_db = str(mania_farm_db or "").strip()
        self.mania_farm_bonus_max = float(mania_farm_bonus_max)
        self.mania_farm_bonus_min_confidence = float(mania_farm_bonus_min_confidence)
        self.mania_farm_high_score_threshold = float(mania_farm_high_score_threshold)
        self.mania_farm_pick_multiplier = float(mania_farm_pick_multiplier)
        self.mania_high_star_relax_style = bool(mania_high_star_relax_style)
        self.mania_style_fallback_enabled = bool(mania_style_fallback_enabled)
        self._round_history = []
        self._playlist_reserved = set()
        self._style_context = None
        self._style_fallback_active = False
        self._noodle_ln_fallback_active = False
        self._style_fallback_notice = None
        self._notice_style_ids = set()
        self._style_repeat_unavoidable = False
        self.mania_convert_db = str(mania_convert_db or "").strip()
        self.mania_convert_validation_db = Path(mania_convert_validation_db) if mania_convert_validation_db else (
            Path(__file__).resolve().parent.parent / "catalog-data/mania_convert_validation.db")
        self.mania_convert_max_share = float(mania_convert_max_share)
        self._convert_pick_credit = 0.0
        if any(not math.isfinite(v) or v < 0 for v in (
            self.mania_min_length, self.mania_style_min_score, self.mania_min_rank_score,
            self.mania_min_confidence, self.mania_min_appearances, self.mania_min_bands,
            self.mania_farm_bonus_max, self.mania_farm_bonus_min_confidence,
            self.mania_farm_high_score_threshold, self.mania_farm_pick_multiplier,
            self.mania_convert_max_share,
        )) or self.mania_min_confidence > 1 or self.mania_farm_bonus_max > 1 \
                or self.mania_farm_bonus_min_confidence > 1 \
                or self.mania_farm_high_score_threshold > 100 \
                or not 1.0 <= self.mania_farm_pick_multiplier <= 3.0 \
                or self.mania_convert_max_share > 1:
            raise ValueError("Invalid mania filter thresholds")
        self._mania_farm_signals = self._load_farm_signals()
        kwargs.update(loved_mode=False, stream_enabled=False)
        super().__init__(db_path, **kwargs)

    def _load_farm_signals(self):
        """Load only farm signals matching this room's native key count."""
        if not self.mania_farm_db:
            return {}
        path = Path(self.mania_farm_db).resolve()
        if not path.is_file():
            log.warning("mania farm sidecar missing at %s; using neutral weights", path)
            return {}
        try:
            conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """
                    SELECT beatmap_id, farm_index, confidence
                    FROM mania_farm_efficiency
                    WHERE key_count=?
                    """, (self.mania_keys,)
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.warning("mania farm sidecar unavailable: %s; using neutral weights", exc)
            return {}

        signals = {}
        for row in rows:
            try:
                beatmap_id = int(row["beatmap_id"])
                farm_index = float(row["farm_index"])
                confidence = float(row["confidence"])
            except (TypeError, ValueError, KeyError, IndexError):
                continue
            if (
                beatmap_id > 0
                and math.isfinite(farm_index)
                and math.isfinite(confidence)
                and 0.0 <= farm_index <= 100.0
                and 0.0 <= confidence <= 1.0
            ):
                signals[beatmap_id] = (farm_index, confidence)
        log.info("loaded %d native-%dK farm-efficiency signals", len(signals), self.mania_keys)
        return signals

    def _connect_catalog(self):
        # mode=ro prevents silently creating an empty DB at a misspelled path.
        conn = sqlite3.connect(Path(self.db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _style_evidence(self, style):
        """Require sustained, prominent structure in legacy v1 catalog scores."""
        if style == "jack":
            return (("max_jack_run", ">=", 5),
                    ("jack_ratio", ">=", .35 if self.mania_keys == 4 else .12),
                    ("hold_ratio", "<=", .30))
        if style == "chordjack":
            return (("max_chordjack_run", ">=", 8),
                    ("chordjack_event_ratio", ">=", .40),
                    ("jack_ratio", ">=", .20 if self.mania_keys == 4 else .08),
                    ("hold_ratio", "<=", .30))
        if style == "stream":
            return (("max_stream_run", ">=", 12),
                    ("stream_event_ratio", ">=", .50),
                    ("chord_event_ratio", "<=", .45),
                    ("hold_ratio", "<=", .25))
        if style == "ln":
            return (("hold_ratio", ">=", .25),
                    ("hold_coverage", ">=", .06))
        if style == "technical":
            return (("rhythm_cv", ">=", .65),)
        return ()

    def _style_score_floor(self, style):
        return max(self.mania_style_min_score, .65 if style == "technical" else 0.0)

    def _style_matches_values(self, values, style, *, converted=False):
        if style in COMPOSITIONS:
            return not converted and composition_score(values, style) is not None
        score = values.get(STYLES[style])
        if score is None or float(score) < self._style_score_floor(style):
            return False
        evidence = self._style_evidence(style)
        # Older small catalogs and convert sidecars only contain the score
        # columns. Preserve their existing behavior until they are rebuilt.
        if converted or not all(col in values for col, _op, _min in evidence):
            return True
        for col, op, threshold in evidence:
            value = values.get(col)
            if value is None:
                return False
            if op == ">=" and float(value) < threshold:
                return False
            if op == "<=" and float(value) > threshold:
                return False
        return True

    def _convert_rows(self, beatmap_id=None):
        """Load only explicitly vetted converts matching this room's keys."""
        if not self.mania_convert_db:
            return []
        path = Path(self.mania_convert_db).resolve()
        if not path.is_file():
            return []
        try:
            conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            where = ["key_count=?", "status IN ('ranked','approved')"]
            params = [self.mania_keys]
            if beatmap_id is not None:
                where.append("beatmap_id=?")
                params.append(int(beatmap_id))
            else:
                where += ["hit_length BETWEEN ? AND ?", "passcount>=?"]
                params += [self.mania_min_length, self.max_length, self.min_passcount]
                cutoff = self.max_map_age_cutoff()
                if cutoff:
                    where.append("julianday(ranked_date)>=julianday(?)")
                    params.append(cutoff)
            rows = conn.execute(
                f"SELECT * FROM convert_maps WHERE {' AND '.join(where)} "
                "ORDER BY unique_players DESC, beatmap_id",
                params,
            ).fetchall()
            # Legacy sidecar key_count is NOT proof of actual conversion keys.
            # Require independently calculated, release-sealed evidence. The
            # catalog revision binding invalidates evidence after a refresh.
            if not self.mania_convert_validation_db.is_file():
                return []
            with sqlite3.connect(self.mania_convert_validation_db.resolve().as_uri() + "?mode=ro", uri=True) as evidence:
                evidence.row_factory = sqlite3.Row
                verified = {int(r["beatmap_id"]): dict(r) for r in evidence.execute("SELECT * FROM verified_converts")}
            admitted = []
            for row in rows:
                v = verified.get(int(row["beatmap_id"]))
                if not v or v["catalog_keys"] != row["key_count"] or v["actual_keys"] != self.mania_keys:
                    continue
                if v["catalog_sr"] != row["sr"] or v["catalog_checked_at"] != row["checked_at"]:
                    continue
                sr, source_sr = v["converted_sr"], v["source_sr"]
                if any(not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0 for x in (sr, source_sr)):
                    continue
                if not v["source_sha256"] or not v["backend"] or not v["verified_at"]:
                    continue
                # Lazer's online playlist can display the ORIGINAL rating.
                # Until upstream carries converted attributes, neither the
                # actual nor the displayed rating may escape this range.
                if beatmap_id is None and not (self.sr_min <= sr <= self.sr_max and self.sr_min <= source_sr <= self.sr_max):
                    continue
                admitted.append(dict(row) | {"sr": sr, "verified_source_sr": source_sr,
                                             "conversion_verified": True})
            return admitted
        except (sqlite3.Error, KeyError, TypeError, ValueError, IndexError) as exc:
            log.warning("mania convert sidecar unavailable: %s; using native maps only", exc)
            return []
        finally:
            if 'conn' in locals():
                conn.close()

    def _rows(self, beatmap_id=None, *, ignore_style=False):
        conn = self._connect_catalog()
        try:
            has_features = self._has_table(conn, "mania_features")
            has_report = self._has_table(conn, self._overrep_table)
            fields = ["m.*"]
            joins = []
            feature_columns = set()
            if has_features:
                joins.append("LEFT JOIN mania_features f USING(beatmap_id)")
                feature_columns = {row[1] for row in conn.execute("PRAGMA table_info(mania_features)")}
                fields.extend(f"f.{col}" for col in STYLES.values())
                fields.extend(f"f.{col}" for col in STYLE_EVIDENCE_COLUMNS
                              if col in feature_columns)
            if has_report:
                joins.append(f"LEFT JOIN {self._overrep_table} r USING(beatmap_id)")
                fields.extend(f"r.{col}" for col in ("rank_score", "confidence", "raw_appearances", "band_count"))
            where = ["m.mode_int=3", "m.key_count=?", "m.status IN ('ranked','approved')"]
            params = [self.mania_keys]
            if beatmap_id is not None:
                # Reconciliation may resolve maps outside the active SR/style
                # window, but must never resolve another ruleset/key count.
                where.append("m.beatmap_id=?")
                params.append(int(beatmap_id))
            else:
                where += ["m.sr BETWEEN ? AND ?", "m.hit_length BETWEEN ? AND ?", "m.passcount>=?"]
                params += [self.sr_min, self.sr_max, self.mania_min_length, self.max_length, self.min_passcount]
                cutoff = self.max_map_age_cutoff()
                if cutoff:
                    where.append("julianday(m.ranked_date)>=julianday(?)")
                    params.append(cutoff)
                source = self.mania_source_mode
                if source in STYLES and not ignore_style:
                    if not has_features:
                        return []
                    where.append(f"f.{STYLES[source]}>=?")
                    params.append(self._style_score_floor(source))
                    evidence = self._style_evidence(source)
                    if all(col in feature_columns for col, _op, _threshold in evidence):
                        for col, op, threshold in evidence:
                            where.append(f"f.{col}{op}?")
                            params.append(threshold)
                elif (self._multi_styles() and not ignore_style
                      and not COMPOSITIONS.intersection(self._multi_styles())
                      and "farm" not in self._multi_styles()):
                    if not has_features:
                        return []
                    alternatives = []
                    for style in self._multi_styles():
                        checks = [f"f.{STYLES[style]}>=?"]
                        params.append(self._style_score_floor(style))
                        evidence = self._style_evidence(style)
                        if all(col in feature_columns for col, _op, _threshold in evidence):
                            for col, op, threshold in evidence:
                                checks.append(f"f.{col}{op}?")
                                params.append(threshold)
                        alternatives.append("(" + " AND ".join(checks) + ")")
                    where.append("(" + " OR ".join(alternatives) + ")")
            order = "m.passcount DESC, m.beatmap_id"
            if beatmap_id is None and self.mania_source_mode in STYLES and has_features:
                order = f"f.{STYLES[self.mania_source_mode]} DESC, m.beatmap_id"
            # Sidecar membership is intentionally evaluated in Python so the
            # catalog stays read-only. Do not truncate candidates before that
            # filter, or a low-passcount high-farm chart could disappear.
            query_limit = 1 if beatmap_id is not None else (
                50000 if self.mania_source_mode in {"farm", *COMPOSITIONS} or self._multi_styles()
                else self.limit
            )
            rows = conn.execute(
                f"SELECT {', '.join(fields)} FROM maps m {' '.join(joins)} WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?",
                [*params, query_limit],
            ).fetchall()
            if beatmap_id is None and not ignore_style:
                if source in COMPOSITIONS:
                    rows = [r for r in rows if composition_score(dict(r), source) is not None]
                    rows.sort(key=lambda r: -composition_score(dict(r), source))
                    rows = rows[:self.limit]
                elif COMPOSITIONS.intersection(self._multi_styles()):
                    rows = [r for r in rows if self._composition_guard_values(dict(r), relaxed=True)]
            if (
                beatmap_id is None
                and source in STYLES
                and not ignore_style
                and self.mania_high_star_relax_style
                and not self.mania_style_fallback_enabled
                and rows
                and len(rows) < self.high_star_relax_min_maps
            ):
                log.info(
                    "sparse native-%dK %.2f-%.2f* %s pool (%d maps); "
                    "appending non-style candidates after preferred maps",
                    self.mania_keys, self.sr_min, self.sr_max, source, len(rows),
                )
                fallback = self._rows(beatmap_id, ignore_style=True)
                seen = {int(row["beatmap_id"]) for row in rows}
                return rows + [row for row in fallback
                               if int(row["beatmap_id"]) not in seen]
            return rows
        finally:
            conn.close()

    def _mania_map(self, row, *, converted=False):
        m = self._map_from_row(row)
        m.is_mania = True
        m.is_mania_convert = bool(converted)
        m.mania_keys = self.mania_keys
        m.map_origin = "standard_convert" if converted else "native_mania"
        values = dict(row)
        m.mania_composition = {k: values.get(k) for k in ('hold_ratio', 'hold_coverage', 'stream_event_ratio')}
        if converted:
            m.mania_conversion_verified = values.get("conversion_verified", False)
            m.mania_source_sr = values.get("verified_source_sr")
        m.mania_styles = tuple(style for style in ALL_STYLES
                               if self._style_matches_values(values, style, converted=converted))
        if not converted:
            m.mania_rank_score = values.get("rank_score")
            m.mania_confidence = values.get("confidence")
            signal = self._mania_farm_signals.get(int(m.beatmap_id))
            if signal is not None:
                m.mania_farm_index, m.mania_farm_confidence = signal
        m.quadrant = (
            f"{self.mania_keys}K convert: " + (" / ".join(m.mania_styles) or "unclassified")
            if converted else
            f"{self.mania_keys}K: " + (" / ".join(m.mania_styles) or "unclassified")
        )
        return m

    def _selection_weight(self, item, good, bad):
        """Make hold-heavy Variety picks rare; explicit sources stay unchanged."""
        base = super()._selection_weight(item, good, bad)
        if (self.mania_source_mode == 'variety'
                and {'ln', 'noodle'}.intersection(item.mania_styles)):
            # A relative weight, not an eligibility filter or claimed fixed
            # percentage. Apply once even when a chart has several styles.
            base *= self.mania_ln_pick_multiplier
        # This applies in Variety and every structural source. Farm mode itself
        # contains only high-farm maps, so the common multiplier is neutral to
        # its internal ordering while feedback and repeat-spacing still apply.
        if self._is_high_farm(item):
            return base * self.mania_farm_pick_multiplier
        return base

    def _is_high_farm(self, item):
        index = item.mania_farm_index
        confidence = item.mania_farm_confidence
        return (
            index is not None and confidence is not None
            and float(index) >= self.mania_farm_high_score_threshold
            and float(confidence) >= self.mania_farm_bonus_min_confidence
        )

    def _weighted_pick(self, candidates):
        """Reserve at most 30% of ordinary variety slots for vetted converts."""
        passcount_max = self._passcount_reference(candidates)
        composition = next(iter(COMPOSITIONS.intersection(self._multi_styles() or (self.mania_source_mode,))), None)
        if composition and not (composition == 'noodle' and self._noodle_ln_fallback_active):
            candidates = self._duration_candidates(candidates)
            strongest = max(composition_score(item.mania_composition, composition, relaxed=True) or 0.0
                            for item in candidates)
            candidates = [item for item in candidates
                          if (composition_score(item.mania_composition, composition, relaxed=True) or 0.0) == strongest]
            return super()._weighted_pick(candidates, passcount_max=passcount_max)
        if self.mania_source_mode == "farm":
            candidates = self._duration_candidates(candidates)
            # Sorting the catalog alone does not order random picks. Farm
            # players get the highest-index eligible fresh chart in the chosen
            # duration bucket first (the soft preference is opt-in);
            # human-feedback weighting breaks equal-score ties only.
            scored = [item for item in candidates
                      if item.mania_farm_index is not None]
            if scored:
                highest = max(item.mania_farm_index for item in scored)
                candidates = [item for item in scored
                              if item.mania_farm_index == highest]
            return super()._weighted_pick(candidates, passcount_max=passcount_max)
        if self.mania_source_mode != "variety" or self.mania_convert_max_share <= 0:
            return super()._weighted_pick(candidates, passcount_max=passcount_max)
        converts = [item for item in candidates if item.is_mania_convert]
        native = [item for item in candidates if not item.is_mania_convert]
        if not converts or not native:
            return super()._weighted_pick(candidates, passcount_max=passcount_max)

        # Preserve the fractional overflow (0.9 + 0.3 -> one convert plus 0.2
        # credit) so a 30% cap produces exactly three slots in ten. Bound
        # deferred credit so temporary candidate filtering cannot cause bursts.
        self._convert_pick_credit = min(
            1.0 + self.mania_convert_max_share,
            self._convert_pick_credit + self.mania_convert_max_share,
        )
        if self._convert_pick_credit + 1e-9 >= 1.0:
            self._convert_pick_credit = max(0.0, self._convert_pick_credit - 1.0)
            return super()._weighted_pick(converts, passcount_max=passcount_max)
        return super()._weighted_pick(native, passcount_max=passcount_max)

    def reload(self):
        maps = [self._mania_map(row) for row in self._rows()]
        multi = self._multi_styles()
        if multi:
            maps = [item for item in maps if self._multi_match_count(item, multi)]
        if self.mania_source_mode == "farm":
            if not self.mania_farm_enabled:
                maps = []
            else:
                # Farm mode is defined by the sidecar, not merely by the older
                # adoption report. This leaves only confidence-qualified,
                # high-index native charts in the candidate pool.
                maps = [item for item in maps if self._is_high_farm(item)]
                maps.sort(key=lambda item: (-float(item.mania_farm_index), item.beatmap_id))
        if (not maps and self.mania_style_fallback_enabled
                and 'noodle' in (multi or (self.mania_source_mode,))):
            rows = self._rows(ignore_style=True)
            maps = [self._mania_map(row) for row in rows
                    if composition_score(dict(row), 'noodle', relaxed=True) is not None]
            if not maps:
                maps = [self._mania_map(row) for row in rows
                        if self._style_matches_values(dict(row), 'ln')]
        if (not maps and self.mania_style_fallback_enabled
                and self.mania_source_mode in {*ALL_STYLES, "farm"}
                and (self.mania_source_mode != "farm" or self.mania_farm_enabled)):
            # Keep empty style requests when native alternatives satisfy all
            # hard filters; next_map applies and announces the relaxation.
            maps = [self._mania_map(row) for row in self._rows(ignore_style=True)
                    if self._composition_guard_values(dict(row), relaxed=True)]
        if self.mania_source_mode == "variety":
            maps.extend(self._mania_map(row, converted=True) for row in self._convert_rows())
        self.maps = maps
        if self._multi_styles() or (self.mania_style_fallback_enabled and self.mania_source_mode != "variety"):
            # This policy broadens features only, not length or ranked age.
            self.source_mode = self.mania_source_mode
            return len(maps)
        relaxed_count = self._maybe_relax_high_star_window(len(maps))
        if relaxed_count is not None:
            return relaxed_count
        self.source_mode = self.mania_source_mode
        return len(maps)

    def get_map(self, beatmap_id):
        for m in self.maps:
            if m.beatmap_id == int(beatmap_id):
                return m
        rows = self._rows(beatmap_id)
        if rows:
            return self._mania_map(rows[0])
        converts = self._convert_rows(beatmap_id)
        return self._mania_map(converts[0], converted=True) if converts else None

    def source_mode_label(self):
        if (self._noodle_ln_fallback_active
                and 'noodle' in (self._multi_styles() or (self.mania_source_mode,))):
            return f"mania {self.mania_keys}K {self.mania_source_mode} (LN fallback)"
        suffix = ' (relaxed)' if self._style_fallback_active and self.mania_source_mode in COMPOSITIONS else ''
        return f"mania {self.mania_keys}K {self.mania_source_mode}{suffix}"

    def _multi_styles(self):
        source = self.mania_source_mode
        return tuple(source.split("+")) if "+" in source else ()

    def _composition_guard_values(self, values, *, relaxed=False):
        requested = self._multi_styles() or (self.mania_source_mode,)
        return all(composition_score(values, style, relaxed=relaxed) is not None
                   for style in COMPOSITIONS.intersection(requested))

    def _multi_match_count(self, item, requested):
        return sum(self._is_high_farm(item) if style == "farm"
                   else style in item.mania_styles for style in requested)

    def _multi_style_rank(self, item, requested):
        matches = self._multi_match_count(item, requested)
        # Matching more requested styles wins first. Within that tier, prefer
        # a clean chart over one that also contains unrelated patterns.
        extras = len(set(item.mania_styles) - set(requested) - COMPOSITIONS)
        return matches, -extras

    def _style_window(self):
        return (self.mania_source_mode, self.sr_min, self.sr_max,
                self.mania_min_length, self.max_length, self.min_passcount,
                self.max_map_age_years, self.mania_style_min_score,
                self.mania_farm_high_score_threshold,
                self.mania_farm_bonus_min_confidence)

    def record_round(self, beatmap_id):
        """Record actual completions/skips, never speculative queue selections."""
        self._round_history = (self._round_history + [int(beatmap_id)])[-10:]

    def reserve_playlist_maps(self, beatmap_ids):
        self._playlist_reserved = {int(bid) for bid in beatmap_ids}

    def _strict_style_match(self, item):
        if self._multi_styles():
            return self._multi_match_count(item, self._multi_styles()) == len(self._multi_styles())
        if self.mania_source_mode == "farm":
            return self._is_high_farm(item)
        return self.mania_source_mode in item.mania_styles

    def strict_style_count(self):
        return sum(self._strict_style_match(item) and item.beatmap_id not in
                   self._runtime_excluded for item in self.maps)

    def take_style_fallback_notice(self):
        if self._style_context != self._style_window():
            return None
        # Predownloaded fallback maps are explained after the remaining strict
        # map has been played/skipped, not while it still awaits first play.
        if self._notice_style_ids and not (
                self._notice_style_ids & set(self._round_history)):
            return None
        notice, self._style_fallback_notice = self._style_fallback_notice, None
        return notice

    def _pick_without_recent_rounds(self, candidates):
        old_maps = self.maps
        self.maps = candidates
        try:
            # Song spacing/rating weights still apply, but the parent's small
            # pool relaxations cannot reintroduce the rounds removed here.
            return super().next_map()
        finally:
            self.maps = old_maps

    def next_map(self):
        requested = self._multi_styles()
        if (self.mania_style_fallback_enabled
                and 'noodle' in (requested or (self.mania_source_mode,))):
            return self._next_noodle_map()
        if requested:
            blocked = (set(self._round_history) | self._playlist_reserved
                       | self._runtime_excluded | set(self._recent))
            fresh = [item for item in self.maps if item.beatmap_id not in blocked]
            if fresh:
                fresh = self._space_song_candidates(fresh)
                best = max(self._multi_style_rank(item, requested) for item in fresh)
                return self._pick_without_recent_rounds(
                    [item for item in fresh if self._multi_style_rank(item, requested) == best]
                )
            available = [item for item in self.maps if item.beatmap_id not in
                         (self._playlist_reserved | self._runtime_excluded)]
            if not available:
                return None
            last_seen = {bid: index for index, bid in enumerate(self._round_history)}
            oldest = min(last_seen.get(item.beatmap_id, -1) for item in available)
            available = [item for item in available
                         if last_seen.get(item.beatmap_id, -1) == oldest]
            best = max(self._multi_style_rank(item, requested) for item in available)
            return self._pick_without_recent_rounds(
                [item for item in available if self._multi_style_rank(item, requested) == best]
            )
        if (not self.mania_style_fallback_enabled
                or self.mania_source_mode not in {*ALL_STYLES, "farm"}):
            return super().next_map()
        context = self._style_window()
        if context != self._style_context:
            self._style_context = context
            self._style_fallback_active = False
            self._style_fallback_notice = None
            self._notice_style_ids = set()
            self._style_repeat_unavoidable = False
        recent = set(self._round_history)
        blocked = recent | self._playlist_reserved | self._runtime_excluded
        strict = [m for m in self.maps if self._strict_style_match(m)
                  and m.beatmap_id not in self._runtime_excluded]
        fresh = [m for m in strict if m.beatmap_id not in blocked]
        if fresh:
            if len(fresh) >= 2:
                self._style_fallback_active = False
                self._style_fallback_notice = None
                self._style_repeat_unavoidable = False
            return self._pick_without_recent_rounds(fresh)

        # Query broader patterns only at exhaustion; SR/passcount/length/key
        # boundaries remain identical to the strict request.
        rows = self._rows(ignore_style=True)
        broader = [self._mania_map(row) for row in rows
                   if self._composition_guard_values(dict(row), relaxed=True)]
        fresh = [m for m in broader if m.beatmap_id not in blocked]
        if self.mania_source_mode in COMPOSITIONS:
            weaker = fresh
        elif self.mania_source_mode in STYLES:
            weak_ids = {
                int(row["beatmap_id"]) for row in rows
                if float(dict(row).get(STYLES[self.mania_source_mode]) or 0)
                >= max(0.0, self.mania_style_min_score - 0.15)
            }
            weaker = [m for m in fresh if m.beatmap_id in weak_ids]
        else:
            weaker = [m for m in fresh if m.mania_farm_index is not None
                      and m.mania_farm_confidence is not None
                      and m.mania_farm_index >= max(0.0, self.mania_farm_high_score_threshold - 10)
                      and m.mania_farm_confidence >= self.mania_farm_bonus_min_confidence]
        candidates = weaker or fresh
        unavoidable = False
        if not candidates:
            available = [m for m in broader if m.beatmap_id not in
                         (self._playlist_reserved | self._runtime_excluded)]
            if not available:
                return None
            last_seen = {bid: index for index, bid in enumerate(self._round_history)}
            oldest = min(last_seen.get(m.beatmap_id, -1) for m in available)
            candidates = [m for m in available if last_seen.get(m.beatmap_id, -1) == oldest]
            unavoidable = True
        if not self._style_fallback_active or (
                unavoidable and not self._style_repeat_unavoidable):
            style = self.mania_source_mode
            if unavoidable:
                self._style_fallback_notice = (
                    f"Even with relaxed {style} features, every available map "
                    "was played or skipped in the past 10 rounds. Using the "
                    "oldest available map; widen the range for more choices.")
            else:
                self._style_fallback_notice = (
                    f"No fresh {style} maps remain in {self.sr_min:.2f}-{self.sr_max:.2f}* "
                    f"with >= {self.min_passcount} passes. Relaxing feature checks "
                    "and including other styles to avoid repeats from the past "
                    "10 played/skipped rounds.")
            strict_ids = {m.beatmap_id for m in strict}
            self._notice_style_ids = strict_ids if not (strict_ids & recent) else set()
        self._style_fallback_active = True
        self._style_repeat_unavoidable = unavoidable
        return self._pick_without_recent_rounds(candidates)

    def _next_noodle_map(self):
        """Strict Noodle, bounded Noodle, then native LN before repeats."""
        context = self._style_window()
        if context != self._style_context:
            self._style_context = context
            self._style_fallback_active = False
            self._noodle_ln_fallback_active = False
            self._style_fallback_notice = None
            self._notice_style_ids = set()
        blocked = set(self._round_history) | self._playlist_reserved | self._runtime_excluded
        requested = self._multi_styles() or ('noodle',)
        others = tuple(style for style in requested if style != 'noodle')

        def pick(candidates, *, relaxed=False, ln=False, repeat=False):
            self._style_fallback_active = relaxed or ln
            self._noodle_ln_fallback_active = ln
            self._style_repeat_unavoidable = repeat
            if others:
                best = max(self._multi_style_rank(item, others) for item in candidates)
                candidates = [item for item in candidates
                              if self._multi_style_rank(item, others) == best]
            return self._pick_without_recent_rounds(candidates)

        preferred = [item for item in self.maps if item.beatmap_id not in blocked
                     and self._strict_style_match(item)]
        if preferred:
            return pick(preferred)

        # This query retains native keys/status and every active hard filter.
        # Only the final LN tier is exempt from Noodle composition limits.
        rows = self._rows(ignore_style=True)
        noodles = [self._mania_map(row) for row in rows
                   if composition_score(dict(row), 'noodle', relaxed=True) is not None]
        fresh = [item for item in noodles if item.beatmap_id not in blocked]
        strict = [item for item in fresh if 'noodle' in item.mania_styles]
        if strict:
            return pick(strict)
        if fresh:
            return pick(fresh, relaxed=True)

        long_notes = [self._mania_map(row) for row in rows
                      if self._style_matches_values(dict(row), 'ln')]
        fresh_ln = [item for item in long_notes if item.beatmap_id not in blocked]
        if fresh_ln:
            return pick(fresh_ln, ln=True)

        available = [item for item in noodles + long_notes if item.beatmap_id not in
                     (self._playlist_reserved | self._runtime_excluded)]
        if not available:
            return None
        last_seen = {bid: index for index, bid in enumerate(self._round_history)}
        oldest = min(last_seen.get(item.beatmap_id, -1) for item in available)
        available = [item for item in available
                     if last_seen.get(item.beatmap_id, -1) == oldest]
        noodle_ids = {item.beatmap_id for item in noodles}
        noodle_repeats = [item for item in available if item.beatmap_id in noodle_ids]
        if noodle_repeats:
            return pick(noodle_repeats, relaxed=True, repeat=True)
        return pick(available, ln=True, repeat=True)

    def set_mania_source(self, source):
        if isinstance(source, (tuple, list)):
            requested = tuple(dict.fromkeys(source))
            if (not requested or any(style not in {"variety", "farm", *ALL_STYLES} for style in requested)
                    or ("variety" in requested and len(requested) > 1)
                    or COMPOSITIONS <= set(requested)
                    or ("farm" in requested and not self.mania_farm_enabled)):
                return 0
            source = "+".join(requested) if len(requested) > 1 else requested[0]
        if source not in {"variety", "farm", *ALL_STYLES} and not (
                isinstance(source, str) and "+" in source
                and all(style in {"farm", *ALL_STYLES} for style in source.split("+"))
                and not COMPOSITIONS <= set(source.split("+"))
                and len(set(source.split("+"))) == len(source.split("+"))
                and ("farm" not in source or self.mania_farm_enabled)):
            return 0
        old, maps = self.mania_source_mode, self.maps
        self.mania_source_mode = source
        try:
            count = self.reload()
        except (sqlite3.Error, ValueError):
            count = 0
        if not count:
            self.mania_source_mode = old
            self.source_mode = old
            self.maps = maps
        elif source != old:
            self._style_context = None
            self._style_fallback_active = False
            self._noodle_ln_fallback_active = False
            self._style_fallback_notice = None
            self._notice_style_ids = set()
        return count

    def set_farm_mode(self):
        return self.set_mania_source("farm")

    def set_stream_mode(self):
        return self.set_mania_source("stream")

    def set_variety_mode(self, *args):
        return self.set_mania_source("variety")
