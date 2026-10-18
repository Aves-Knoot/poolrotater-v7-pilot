"""Lobby orchestration.

State machine, deliberately small:

    IDLE     -> map set, waiting for players / countdown to start
    STARTING -> StartMatch sent, waiting for MatchStarted
    PLAYING  -> match in progress, do not touch the playlist
    SCORING  -> match ended, fetching scores before rotating

The important invariant: never call EditCurrentPlaylistItem while PLAYING.
The hub rejects playlist edits during a match, and doing it anyway is how you
end up in a state the bot can't reason about.
"""
from __future__ import annotations

import json
import logging
import math
import os
import queue
import re
import sqlite3
import threading
import time
import textwrap
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from .exclusions import ExclusionList
from .moderation import BanStore
from statistics import median

from .pool import (AUTO_SR_AUTO_MIN_CAP, AUTO_SR_FLOOR, AUTO_SR_HALF_WIDTH,
                   MapPool, PoolMap, auto_range_for_pp, target_sr_for_pp)
from .ppmath import gain_from
from .ppstats import PPStatsStore
from .ratings import RatingStore
from .player_preferences import DifficultyRecall, map_type_label
from .languages import PlayerLanguages, player_language_command
from .hub import (HubTransportError, HubDisconnected, HubInvocationUncertain,
                  validate_mods)
from .ai import AIService
from .pp_projection import MapPPProjection, PPArgumentError, parse_pp_arguments
from .mode import MANIA_KEYS, merge_room_config, ruleset_id, validate_mania_mods
from .discord_modlog import DiscordModLog
from .discord_control import DiscordControl
from .config_lock import serialized_config
from .discord_reports import DiscordReports
from .lazer_coordinator import LazerCoordinator
from .process_control import SharedProcessControl
from .custom_greetings import JOIN_GREETING_COOLDOWN, configured_greeting
from .community import DEFAULT_DISCORD_INVITE
from .api import (ApiAdmissionDeferred, ApiKeepaliveUnavailable,
                  ApiChatReadUnavailable, ApiChatDeliveryUncertain,
                  ApiScoreReadUnavailable, ApiProfileReadUnavailable)

log = logging.getLogger("bot")
_UNIVERSAL_CONFIG_WRITE_LOCK = threading.Lock()

IDLE, STARTING, PLAYING, SCORING = "IDLE", "STARTING", "PLAYING", "SCORING"

# Map feedback deliberately has a neutral value. It is retained in
# ``map_ratings`` now, but current pool weighting only uses positive/negative
# ratings so neutral feedback has no selection effect yet.
RATING_COMMANDS: dict[str, tuple[int, str]] = {
    "good": (1, "good"),
    "g": (1, "good"),
    "bad": (-1, "bad"),
    "b": (-1, "bad"),
    "ok": (0, "neutral"),
    "okay": (0, "neutral"),
    "meh": (0, "neutral"),
    "mid": (0, "neutral"),
    "neutral": (0, "neutral"),
    "fine": (0, "neutral"),
    "average": (0, "neutral"),
    "alright": (0, "neutral"),
}

# A short, automatic grace period is useful when a lobby is actively talking
# about the map or getting ready.  Explicit start-pressure/frustration is
# deliberately excluded so an annoyed lobby never gets slowed down further.
_COUNTDOWN_FRUSTRATION_PHRASES = (
    "start already", "just start", "start pls", "start please",
    "start now", "go already", "when start", "can we start",
    "why no start", "taking too long", "too slow", "so slow",
    "hurry up", "hurry", "come on", "cmon", "waiting forever",
    "still waiting", "stuck", "delayed", "delay", "annoying",
    "frustrat", "impatient",
)

# Public commands can create osu! API traffic simply by causing the bot to send
# a reply.  Keep a per-user burst guard in front of known public commands so one
# person cannot burn the shared API budget by hammering command responses.
_PUBLIC_COMMANDS = frozenset({
    "help", "help2", "commands", "discord", "ai", "queue", "farm", "stream",
    "variety", "pool", "start", "skip", "s", "harder", "easier", "hidden", "show",
    "stats", "lobby", "pp", "map", "style", "jack", "chordjack", "ln", "technical",
    "wait", "time", "dt", "nm", "lang", "language", "jump", "aim", "rice", "noodle", "noodles", "redo", "replay", "repeat", "again", "freedomdive",
}) | frozenset(RATING_COMMANDS)

_REPLAY_COMMANDS = frozenset({"redo", "replay", "repeat", "again"})

# Keep one-line command help short enough for osu! chat. Keys are canonical
# names; the lookup below accepts the same aliases as the command handlers.
_COMMAND_HELP = {
    "help": ("help [command]", "Show common commands and the full reference link, or explain one command."),
    "help2": ("help2", "Show the full command list for your permission level."),
    "queue": ("queue", "Show the current map and upcoming picks."),
    "pool": ("pool", "Show the active map pool, range and filters."),
    "start": ("start", "Request the countdown; with 2+ players, 1 ready player is enough. Nobody ready means waiting."),
    "wait": ("wait", "Add 10 seconds before the map starts, or cancel the countdown."),
    "time": ("time", "Show approximately how much time is left in the running map."),
    "lang": ("lang en|zh|ko|es|ru|ja|pl|id|fr|de", "Set your personal reply language; English always stays. en/off stops language offers."),
    "skip": ("skip", "Vote to skip the current map."),
    "replay": ("replay", "Vote to replay the last completed map."),
    "harder": ("harder", "Vote for harder future picks; the visible queue stays put."),
    "easier": ("easier", "Vote for easier future picks; the visible queue stays put."),
    "good": ("good", "Give positive feedback on the last completed map."),
    "bad": ("bad", "Give negative feedback on the last completed map."),
    "ok": ("ok", "Give neutral feedback on the last completed map."),
    "pp": ("pp [mods] [accuracy%...]", "Estimate PP for the current map; e.g. .pp HDHR 98.5."),
    "stats": ("stats [username]", "Show recorded player statistics."),
    "lobby": ("lobby", "Show this lobby's recorded activity."),
    "hidden": ("hidden", "Hide your statistics from public lookups."),
    "show": ("show", "Make your statistics visible in public lookups again."),
    "discord": ("discord", "Get the community Discord invite."),
    "ai": ("ai <question>", "Ask the optional bot chat feature when enabled."),
    "farm": ("farm", "Prefer farm maps for future picks."),
    "dt": ("dt [on|off|100]", "DT is separate from styles. on allows mixed DT; 100 prefers DT with NoMod fallback. off/.nm disables it."),
    "nm": ("nm", "No DT picks; replace unplayed DT maps safely. A running match finishes."),
    "jump": ("jump", "Snap-aim Standard maps, including slider jumps; prefers short maps and AR 9–10."),
    "rice": ("rice", "Prefer tap-dominant Mania maps with few holds and streams; tap chords allowed."),
    "noodle": ("noodle", "Prefer hold-dominant native Mania maps; stricter than LN."),
    "stream": ("stream", "Prefer stream maps for future picks."),
    "variety": ("variety [farm% stream%]", "Return to a broad selection; Standard can set a farm/stream mix."),
    "style": ("style <style[,style...]>", "Choose one or more Mania styles. Spaces, commas and | work; e.g. .style jack chordjack ln."),
    "map": ("map", "Show details and a link for the current Mania map."),
    "jack": ("jack", "Prefer repeated-note Mania charts for future picks."),
    "chordjack": ("chordjack", "Prefer repeated-chord Mania charts for future picks."),
    "ln": ("ln", "Prefer long-note Mania charts for future picks."),
    "technical": ("technical", "Prefer technical Mania charts for future picks."),
}
_COMMAND_HELP_ALIASES = {
    "commands": "help", "language": "lang", "s": "skip", "t": "time", "aim": "jump", "redo": "replay", "repeat": "replay",
    "again": "replay", "noodles": "noodle", "g": "good", "b": "bad", "okay": "ok",
    "meh": "ok", "mid": "ok", "neutral": "ok", "fine": "ok",
    "average": "ok", "alright": "ok",
}
_MOD_COMMAND_HELP = {
    "kick": ("kick <username|id>", "Remove a player from this room; they may rejoin."),
    "ban": ("ban <username|id> [30m|24h|7d] [| reason]", "Exclude a player from all four rooms; permanent unless a duration is supplied."),
    "unban": ("unban <username|id>", "Remove a persistent project ban."),
    "bans": ("bans [page]", "List persistent project bans."),
    "settings": ("settings", "Show current room settings."),
    "abort": ("abort", "Stop the current match."),
    "forceskip": ("forceskip", "Skip the current map without a vote."),
    "softrestart": ("softrestart", "Restart this room when safe."),
    "usercommands": ("usercommands on|off", "Enable or disable public commands."),
    "silence": ("silence on|off", "Enable or disable bot chat replies."),
    "voteskip": ("voteskip on|off", "Enable or disable player skip voting."),
    "ppannounce": ("ppannounce on|off", "Enable or disable PP announcements."),
    "range": ("range <min> <max>", "Set the room's star rating range."),
    "auto": ("auto", "Switch Standard automatic difficulty on or off."),
    "length": ("length <seconds>", "Set the maximum map length; alias of maxlength."),
    "maxlength": ("maxlength <seconds>", "Set the maximum map length."),
    "maxage": ("maxage <years|off>", "Limit the age of selected maps."),
    "countdown": ("countdown <seconds>", "Set the start countdown."),
    "autostart": ("autostart <seconds>", "Set the automatic start delay."),
    "minplayers": ("minplayers <number>", "Set the player count required to start."),
    "queueahead": ("queueahead <number>", "Set how many maps to queue ahead."),
    "difficultyvote": ("difficultyvote <25-100%>", "Set the difficulty vote threshold."),
}
_MOD_COMMAND_HELP_ALIASES = {"diffvote": "difficultyvote"}
_ADMIN_COMMAND_HELP = {
    "admin": ("admin add|remove <user>", "Manage bot admins."),
    "admins": ("admins", "List bot admins."),
    "mod": ("mod add|remove <user>", "Manage room moderators."),
    "mods": ("mods", "List room moderators."),
    "ratingignore": ("ratingignore add|remove|list|purge", "Manage ignored ratings."),
    "set": ("set <name> <value>", "Change an approved room setting."),
    "hardrestart": ("hardrestart", "Restart the bot process."),
    "close": ("close", "Close the room."),
    "shutdown": ("shutdown", "Shut down the bot."),
}

# Accept familiar spelling variants without inventing new chart categories.
_MANIA_STYLE_ALIASES = {
    "jack": "jack", "jacks": "jack", "jacking": "jack",
    "chordjack": "chordjack", "chordjacks": "chordjack",
    "stream": "stream", "streams": "stream", "streaming": "stream",
    "ln": "ln", "lns": "ln", "longnote": "ln", "longnotes": "ln",
    "technical": "technical", "tech": "technical", "technicals": "technical",
    "variety": "variety", "all": "variety", "any": "variety", "mixed": "variety",
    "farm": "farm", "farming": "farm",
    "rice": "rice", "noodle": "noodle", "noodles": "noodle",
}


def _mania_style_name(value: str) -> str:
    normalized = "".join(value.lower().split()).replace("-", "").replace("_", "")
    return _MANIA_STYLE_ALIASES.get(normalized, value.lower())


def _parse_mania_style_request(value: str) -> tuple[str, ...] | None:
    """Accept natural separators and aliases without guessing unknown styles."""
    value = re.sub(r"\bchord[\s_-]+jacks?\b", "chordjack", value.lower())
    value = re.sub(r"\blong[\s_-]+notes?\b", "longnotes", value)
    words = re.split(r"[\s,|/+&;]+", value.strip())
    requested = []
    for word in words:
        if not word or word in {"and", "or", "with"}:
            continue
        style = _mania_style_name(word.removeprefix("."))
        if style not in {"variety", "farm", "jack", "chordjack", "stream", "ln", "technical", "rice", "noodle"}:
            return None
        if style not in requested:
            requested.append(style)
    if len(requested) > 1 and "variety" in requested:
        requested.remove("variety")
    if {'rice', 'noodle'} <= set(requested):
        return None
    return tuple(requested) if requested else None


# A deliberately hidden novelty command.  This is the Standard approved
# Another difficulty requested by the operator, not the separate 4K convert.
# It is built in rather than looked up through the active pool so it remains
# available even when the lobby's ordinary SR/source filters exclude it.
_FREEDOM_DIVE = PoolMap(
    beatmap_id=126645,
    artist="xi",
    title="FREEDOM DiVE",
    version="Another",
    sr=7.01799,
    hit_length=226,
    farm_score=0.0,
    passcount=0,
)

class LobbyBot(PlayerLanguages, DifficultyRecall):
    def __init__(self, hub, api, pool: MapPool, cfg: dict, events: queue.Queue,
                 *, discord_control=None, chat_websocket=None, discord_modlog=None,
                 lifecycle=None):
        self.hub, self.api, self.pool, self.cfg = hub, api, pool, cfg
        mode = ruleset_id(cfg)
        if mode == 3:
            if not getattr(pool, "mania_mode", False):
                raise ValueError("Mania room requires ManiaPool")
            validate_mania_mods(cfg.get("allowed_mods", []), cfg.get("required_mods", []), cfg.get("freestyle", False))
            cfg["auto_difficulty"] = False
            cfg["loved_mode"] = False
        self.events = events
        self.chat_websocket = chat_websocket
        self._lifecycle = lifecycle

        # Live admin tuning writes only approved settings back to the same
        # room JSON. Prefer an explicitly supplied path, otherwise use the
        # normal project layout (poolrotater/instances/room1.json).
        self._config_path = self._resolve_config_path()
        self._universal_config_path = self._resolve_universal_config_path()

        self.room_id: int | None = None
        self.channel_id: int | None = None
        # Transport/liveness recovery. A SignalR reconnect loses the referee
        # room membership even when the room itself survives, while the public
        # room endpoint returning a real 404 means the server-side room is gone.
        self._hub_rejoin_pending = False
        self._room_missing_confirmations = 0
        self._last_transport_recovery_at = 0.0
        # Keep the configured room name as the human-authored template. The
        # live title renderer owns the dynamic difficulty-mode label, SR range,
        # and shared version suffix, so old per-room strings such as
        # "(Auto Difficulty)" and "[v6.4.6]" are harmless migration input.
        self._room_name_template = str(cfg.get("room_name", "Farm lobby"))
        self._shutdown_started = False
        self._current_room_name = self._room_name_template
        self.state = IDLE
        self.current_map = None
        self.current_item_id: int | None = None

        # Referee events are the primary playlist truth, with GET /rooms/{id}
        # used as a periodic/just-before-start audit. Keep a normalized mirror
        # so one delayed event can never shift map feedback by an index.
        self._server_playlist_items: dict[int, dict] = {}
        self._standard_playlist_edits: dict[int, dict] = {}
        self._server_current_item_id: int | None = None
        self._last_playlist_audit = 0.0
        self._playlist_dirty = True

        # Durable item -> map lookup. The server can advance the visible
        # playlist before MatchCompleted reaches us, so .good/.bad must be tied
        # to the playlist item that actually finished rather than whichever map
        # happens to be current when the completion event is processed.
        self._playlist_maps: dict[int, object] = {}
        # Keep the map/item that actually entered gameplay separate from the
        # playlist's newly-active item. lazer can advance the playlist as soon
        # as a match finishes, before score reporting is done.
        # Freeze the intended map/item as soon as StartMatch is sent. Playlist
        # events can arrive out of order around countdown completion, so this
        # is the authoritative "what did we ask lazer to play?" snapshot.
        self.starting_map = None
        self.starting_item_id: int | None = None
        # StartMatch can be accepted by osu! while the referee subscription is
        # silently evicted (for example when the same-account lazer client is
        # booted). In that case the command completion can be successful even
        # though CountdownStarted/MatchStarted never reach this process. Keep
        # enough state to avoid a second StartMatch until server truth proves
        # the first attempt did not actually run.
        self._start_command_acknowledged = False
        self._start_event_gap = False
        self._start_rejoin_last_attempt = 0.0
        self._start_snapshot_players: set[int] = set()
        self._start_countdown_seen = False
        self._start_countdown_stopped_at = 0.0

        self.playing_map = None
        self.playing_item_id: int | None = None
        self.playing_player_ids: set[int] = set()
        self.scoring_item_id: int | None = None

        # Protect a just-started match from one stale countdown-abort event
        # arriving out of order. Early aborts are now held briefly until the
        # server playlist confirms whether the match really ended; this avoids
        # both false aborts and the old "stuck locally PLAYING" corner case.
        self._match_started_at = 0.0
        self._explicit_abort_pending = False
        self._force_skip_abort_pending = False
        self._early_abort_pending: dict | None = None
        # Durable operating-history row for the server-confirmed match that is
        # currently PLAYING/SCORING. The row itself lives in ratings.db.
        self._activity_match_key: str | None = None

        self.players: dict[int, dict] = {}      # gameplay/spectator users only
        # Referees are visible in lazer rooms, but they are not gameplay
        # participants for auto-start / ready-count purposes.
        self.referee_ids: set[int] = set()
        # Admin-requested graceful shutdown. This is deliberately runtime-only:
        # a restart must never inherit an old request to close a new lobby.
        self._close_when_empty = False
        # Set only when .closewhenempty itself decides the room is empty.  The
        # multi-room supervisor uses this marker, together with confirmed loss
        # of RoomHub ownership, to decide whether this worker may safely be
        # recreated later by the Discord-only .open/.reopen command.
        self._supervisor_reopenable_stop = False
        self._start_immediate_retry = False
        # An explicit .start is a deliberate request to begin the countdown,
        # including from a lone player who has not toggled READY. Keep that
        # distinction only for the active countdown; automatic starts retain
        # the stricter one-player READY safety gate.
        self._manual_start_bypass_ready = False
        self._unready_start_grace_users: set[int] = set()

        # If a one-player countdown is consumed by osu! without a matching
        # MatchStarted event, do not immediately launch another countdown for
        # that same lone player. The server can mark each current playlist item
        # played even though nobody entered gameplay; blindly retrying would
        # burn through the pool. The hold clears when that player readies again
        # after the failure, leaves/rejoins, another gameplay player appears, or
        # an admin explicitly uses .start.
        self._single_player_start_hold_uid: int | None = None

        # If lazer cancels a countdown because all readiness disappeared while
        # players were still downloading/importing, it also advances the
        # playlist item. Restore the attempted beatmap and require a fresh
        # READY signal before another automatic countdown. The referee API has
        # no direct per-player download flag, so READY is the only reliable,
        # non-invasive availability signal we can use.
        self._readiness_retry_beatmap_id: int | None = None

        # When the room has no gameplay players and the first player arrives,
        # do not auto-start just because the room has already been IDLE longer
        # than auto_start_seconds. The referee API does not expose a direct
        # "beatmap downloaded" flag, so the safest observable signal is that
        # the player reaches READY. Until then, keep the room IDLE so a slow
        # download cannot make osu! consume playlist items without MatchStarted.
        # A second gameplay player or an admin .start clears this gate.
        self._first_player_download_wait_uid: int | None = None

        # Optional pre-start/countdown grace. Requests are tracked by both playlist
        # item and beatmap because lazer keeps the same current item id when an
        # idle skip replaces its beatmap. This gives every actual map a clean
        # allowance while still preventing one player from repeatedly adding
        # time. A countdown may be cancelled by a new requester until the map
        # starts, unless a player explicitly asks to start or expresses one of
        # the recognized start-pressure/frustration phrases.
        self._countdown_wait_item_id: int | None = None
        self._countdown_wait_beatmap_id: int | None = None
        self._countdown_wait_requesters: set[int] = set()
        self._countdown_wait_delay_seconds = 0.0
        self._countdown_wait_locked_item_id: int | None = None
        self._countdown_wait_locked_beatmap_id: int | None = None
        self._countdown_wait_force_item_id: int | None = None
        self._countdown_wait_force_beatmap_id: int | None = None

        # The countdown watchdog must use the actual duration sent to osu!,
        # including any quiet activity-based extension.
        self._active_countdown_seconds = 0.0

        # A joining player receives at most one mid-match orientation message
        # for each match.  This stays runtime-only and is cleared at the next
        # MatchStarted event.
        self._mid_match_intro_sent: set[int] = set()
        # Bounded, runtime-only familiarity survives ordinary room relists.
        self._introduced_players: deque[int] = deque(maxlen=2048)
        self._problem_notices: dict[str, tuple[object, float]] = {}
        self._last_non_hint_sent_at = 0.0
        self._sending_feedback_hint = False

        # Joining/leaving changes the desired auto-difficulty, but we do NOT
        # retune immediately. Pool range + room name are updated only after the
        # current map has concluded, before the queue is refilled.
        self._auto_retune_pending = False

        # Room settings cannot be changed while a match/countdown is active.
        # A manual .range issued during STARTING/PLAYING is still accepted for
        # future map selection, but its title change is deferred until the
        # next safe between-map window (SCORING/IDLE), before another
        # countdown can begin.
        self._pending_room_name_range: tuple[float, float] | None = None

        # Admin-triggered playlist rebuild. During PLAYING/SCORING it waits
        # for the safe between-map window; during STARTING the countdown is
        # cancelled first so the rebuild can happen immediately.
        self._pending_soft_restart = False

        # A valid hidden-map request made during an active match is retained
        # until rotate() reaches the next safe between-map boundary.
        self._pending_freedom_dive_user_id: int | None = None

        # A room can disappear from lazer's discovery listing after it has been
        # open for a while. Recreating it restores visibility, but must wait
        # until every player and spectator has left. Manual `.hardrestart N`
        # remains an explicit completed-map countdown.
        self._room_opened_at = 0.0
        self._automatic_hard_restart_due_at = 0.0
        self._automatic_hard_restart_waiting_for_empty = False
        self._pending_hard_restart_maps: int | None = None
        self._pending_hard_restart_reason = ""

        self.last_message_id = 0
        # Socket delivery, reconnect reconciliation, and the HTTP fallback can
        # overlap. Track actual ids rather than assuming batches always arrive
        # in order, so a command is never executed twice.
        self._seen_chat_message_ids: set[int] = set()
        self._seen_chat_message_order: deque[int] = deque()
        # Ids of messages WE posted. The bot runs as the operator's own
        # account, so sender_id cannot tell the two apart -- without this the
        # bot reads its own output back and executes it as commands.
        self._own_messages: set[int] = set()
        # Set by the range command. Stops auto_difficulty from silently
        # clobbering a manual choice on the next join. If auto difficulty is
        # disabled in the persisted config, treat the configured SR range as
        # manual immediately after restart too.
        self.manual_range = not bool(cfg.get("auto_difficulty", True))
        self._room_ready = False
        self._autodiff_lobby_lookup = None
        self._empty_since = None
        self._empty_reset_done = False
        self._empty_reset_retry_at = 0.0
        # osu! chat swallows messages beginning with "!" -- that is the
        # BanchoBot command prefix, so such messages reach the API but never
        # render in the channel. Players cannot see each other's commands,
        # which makes the lobby look broken. Any other prefix displays fine.
        self.prefix = str(cfg.get("command_prefix", ".")) or "."

        # Moderation controls are persisted into the room JSON whenever an
        # admin changes them.  Missing keys intentionally default to the
        # permissive/current behavior so older configs upgrade cleanly.
        normalized_admins: list[int] = []
        for raw_admin in cfg.get("admins", []):
            try:
                normalized_admins.append(int(raw_admin))
            except (TypeError, ValueError):
                log.warning("ignoring invalid admin id in config: %r", raw_admin)
        self.cfg["admins"] = sorted(set(normalized_admins))

        normalized_moderators: list[int] = []
        for raw_moderator in cfg.get("moderators", []):
            try:
                normalized_moderators.append(int(raw_moderator))
            except (TypeError, ValueError):
                log.warning(
                    "ignoring invalid moderator id in config: %r",
                    raw_moderator,
                )
        # Admins already inherit every moderator permission, so keeping them
        # out of the explicit moderator list makes role listings unambiguous.
        self.cfg["moderators"] = sorted(
            set(normalized_moderators) - set(self.cfg["admins"])
        )

        self.cfg.setdefault("user_commands_enabled", True)
        self.cfg.setdefault("command_spam_burst", 5)
        self.cfg.setdefault("command_spam_window_seconds", 10.0)
        self.cfg.setdefault("command_spam_block_seconds", 20.0)
        self.cfg.setdefault("bot_chat_silenced", False)
        self.cfg.setdefault("voteskip_enabled", True)
        self.cfg.setdefault("difficulty_vote_ratio", 0.50)
        self.cfg.setdefault("difficulty_vote_step", 0.20)
        self.cfg.setdefault("difficulty_vote_max_steps", 3)
        self.cfg.setdefault("chat_poll_seconds", 5.0)
        self.cfg.setdefault("adaptive_chat_polling", True)
        self.cfg.setdefault("auto_empty_reset_seconds", 300.0)
        self.cfg.setdefault("chat_poll_playing_seconds", 10.0)
        self.cfg.setdefault("chat_websocket_enabled", True)
        self.cfg.setdefault("chat_websocket_reconcile_seconds", 300.0)
        self.cfg.setdefault("player_profile_cache_seconds", 21600.0)
        self.cfg.setdefault("download_availability_cache_days", 90.0)
        self.cfg.setdefault("active_chat_countdown_extension_seconds", 5.0)
        self.cfg.setdefault("active_chat_countdown_window_seconds", 45.0)
        self.cfg.setdefault("active_chat_countdown_min_messages", 4)
        self.cfg.setdefault("active_chat_countdown_min_speakers", 2)
        self.cfg.setdefault("start_wait_seconds", 10.0)
        self.cfg.setdefault("freedom_dive_cooldown_seconds", 14400.0)
        # AutoDiff pool-density guard. Keep the requested difficulty band first:
        # allow longer maps before lowering the SR floor. These are runtime
        # fallbacks only; the admin-set max_length/bias/half-width stay intact.
        self.cfg.setdefault("max_length", int(self.pool.max_length))
        if callable(getattr(self.pool, 'configure_jump_policy', None)):
            self.pool.configure_jump_policy(self.cfg)
        if "real_max_length" in self.cfg:
            self.pool.configure_duration_policy(self.cfg)
            self.cfg.update(self.pool.duration_policy_from_config(self.cfg))
            self.cfg["max_length"] = self.pool.real_max_length
        self.cfg.setdefault("loved_mode", bool(getattr(self.pool, "loved_mode", False)))
        for key in ('sparse_passcount_enabled', 'sparse_passcount_min_maps', 'sparse_passcount_floor'):
            self.cfg.setdefault(key, getattr(self.pool, key, None))
        self.cfg.setdefault(
            "max_map_age_years",
            float(getattr(self.pool, "max_map_age_years", 0.0) or 0.0),
        )
        self.cfg.setdefault("auto_pool_min_maps", 30)
        self.cfg.setdefault("auto_pool_length_step", 30)
        self.cfg.setdefault("auto_pool_max_length", 300)
        self.cfg.setdefault("auto_pool_widen_step", 0.25)
        self.cfg.setdefault("auto_pool_max_widen_down", 1.50)

        # Optional conversational LLM.  The worker is isolated from the
        # referee/state-machine thread and can only return text.  Every output
        # line is prefixed by Python with the literal "~AI * ".
        self.cfg.setdefault("ai_enabled", True)
        self.cfg.setdefault("ai_model", "gpt-5-mini")
        self.cfg.setdefault("ai_base_url", "https://api.openai.com/v1")
        self.cfg.setdefault("ai_endpoint", "")
        self.cfg.setdefault("ai_api_key_env", "POOLROTATER_LLM_API_KEY")
        self.cfg.setdefault("ai_user_cooldown_seconds", 10.0)
        self.cfg.setdefault("ai_global_cooldown_seconds", 3.0)
        self.cfg.setdefault("ai_max_pending", 4)
        self.cfg.setdefault("ai_max_output_chars", 320)
        self.cfg.setdefault("ai_max_output_tokens", 120)
        self.cfg.setdefault("ai_max_prompt_chars", 700)
        self.cfg.setdefault("ai_recent_chat_messages", 10)
        self.cfg.setdefault("ai_timeout_seconds", 30.0)
        self.cfg.setdefault("ai_temperature", -1.0)
        self.cfg.setdefault("ai_context_file", "ai_context.md")
        self.cfg.setdefault("ai_config_reload_seconds", 2.0)
        # Keep the former AI-only reload interval as the compatibility default
        # for the broader live-config watcher.
        self.cfg.setdefault(
            "config_reload_seconds",
            float(self.cfg.get("ai_config_reload_seconds", 2.0) or 2.0),
        )
        self.cfg.setdefault("hard_restart_interval_seconds", 21600.0)
        self.cfg.setdefault("hard_restart_warning_maps", 3)
        self.cfg.setdefault("hard_restart_close_delay_seconds", 5.0)
        self.cfg.setdefault("lazer_rejoin_delay_seconds", 5.0)
        self.cfg.setdefault(
            "lazer_join_delay_seconds",
            float(self.cfg.get("lazer_rejoin_delay_seconds", 5.0) or 5.0),
        )
        self.cfg.setdefault("lazer_leave_delay_seconds", 1.0)
        self.cfg.setdefault("lazer_leave_confirm_delay_seconds", 0.5)
        self.cfg.setdefault("lazer_minimum_room_dwell_seconds", 2.0)
        self.cfg.setdefault("lazer_visit_max_seconds", 10.0)
        self.cfg.setdefault("lazer_visit_max_actions", 8)
        self.cfg.setdefault("lazer_chat_patrol_enabled", False)
        self.cfg.setdefault("lazer_chat_patrol_interval_seconds", 2.0)
        self.cfg.setdefault("lazer_switch_timeout_seconds", 45.0)
        self.cfg.setdefault("lazer_queue_poll_seconds", 0.10)
        self.cfg.setdefault("lazer_coordinator_db", "state/lazer_coordinator.db")
        self.cfg.setdefault("lazer_coordinator_enabled", os.name == "nt")
        # Existing Windows room configs predate this option. Default to local
        # auto-join there, while a headless Linux/VPS process defaults off.
        self.cfg.setdefault("lazer_rejoin_on_hard_restart", os.name == "nt")
        self.lazer_coordinator = LazerCoordinator(self.cfg)
        self.cfg.setdefault("process_control_db", "state/process_control.db")
        self.process_control = SharedProcessControl(self.cfg)
        log.info(
            "local lazer coordination: %s (join=%ss, dwell=%ss, host=%s)",
            "enabled" if (
                self.cfg["lazer_rejoin_on_hard_restart"]
                and self.cfg["lazer_coordinator_enabled"]
            ) else "disabled",
            self.cfg["lazer_join_delay_seconds"],
            self.cfg["lazer_minimum_room_dwell_seconds"],
            os.name,
        )

        # Discord transcript notifications are intentionally best-effort and use an
        # environment variable for the webhook URL.  The worker never blocks
        # referee processing or the osu! lobby chat command handler.
        self.cfg.setdefault("discord_modlog_enabled", True)
        self.cfg.setdefault(
            "discord_modlog_webhook_env", "POOLROTATER_DISCORD_MODLOG_WEBHOOK"
        )
        self.cfg.setdefault("discord_modlog_timeout_seconds", 5.0)
        self.cfg.setdefault("discord_invite_url", DEFAULT_DISCORD_INVITE)
        self.cfg.setdefault("discord_invite_reminder_seconds", 14400)
        # Current NoMod PP can always be requested with `.pp` while the PP
        # display service is enabled. Automatic PP lines are a separate knob so
        # operators can keep the command without adding a chat line after every
        # completed map.
        self.cfg.setdefault("map_pp_display_enabled", True)
        self.cfg.setdefault("map_pp_auto_announce_enabled", True)
        # Start the timer at process launch so a restart cannot immediately
        # add a community-link line to an already busy lobby. Website and
        # Discord share this single timer; they never get separate reminders.
        self._last_website_announcement = time.time()
        self._website_notified_players: set[int] = set()
        self._discord_notified_players: set[int] = set()
        self._last_discord_reminder_text = ""
        self._next_community_link_is_discord = False
        self.discord_modlog = discord_modlog if discord_modlog is not None else DiscordModLog(self.cfg)
        self.cfg.setdefault("discord_control_enabled", True)
        self.cfg.setdefault(
            "discord_control_bot_token_env", "POOLROTATER_DISCORD_BOT_TOKEN"
        )
        self.cfg.setdefault("discord_control_guild_id", 0)
        self.cfg.setdefault("discord_control_channel_id", 0)
        self.cfg.setdefault("discord_control_channel_ids_by_room", {})
        self.cfg.setdefault("discord_control_admin_role_names", ["Top dev"])
        self.cfg.setdefault("discord_control_moderator_role_names", ["mods"])
        self.cfg.setdefault("discord_public_status_channel_id", 0)
        self.cfg.setdefault("discord_public_status_channel_name", "")
        self.cfg.setdefault("discord_public_status_label", "")
        self.cfg.setdefault("discord_public_join_base_url", "")
        self.cfg.setdefault("discord_status_rename_spacing_seconds", 5.0)
        self.cfg.setdefault(
            "discord_public_status_state_file",
            "state/discord_public_status.json",
        )
        self._last_discord_public_status: tuple | None = None
        # Discord identity values are kept separate from osu! user IDs. The
        # temporary negative ids below can never collide with a real osu! id.
        self._discord_control_actors: dict[int, dict[str, str]] = {}
        # Set only while executing an inbound Discord command.  say() uses it
        # to reply in Discord rather than leaking that reply into osu! chat.
        self._discord_command_reply_channel_id: int | None = None
        self._command_spam_history: dict[int, deque[float]] = {}
        self._command_spam_block_until: dict[int, float] = {}
        self.discord_control = (
            discord_control if discord_control is not None else DiscordControl(self.cfg)
        )
        self.discord_reports = DiscordReports(cfg.get("ratings_db", "state/ratings.db"), ruleset_id=mode)

        self.ready: set[int] = set()          # user ids currently readied
        from poolrotater_host.ratings.reader import for_settings as rating_reader
        self.rating_reader=rating_reader(cfg)
        self.pool.ratings_reader=self.rating_reader
        self.ratings = RatingStore(cfg.get("ratings_db", "ratings.db"),
                                   initialize=not cfg.get("ratings_schema_prepared", False),reader=self.rating_reader)
        self._init_player_languages()
        # Keep persistent per-player PP gains in the same SQLite file as
        # ratings/performance evidence, but in a separate table. This starts
        # tracking from the first run of this version and is deduplicated by
        # room + playlist item + user so a score report can never count twice.
        from poolrotater_host.activity.reader import for_settings as activity_reader
        self.activity_reader = activity_reader(cfg)
        self.pp_stats = PPStatsStore(cfg.get("ratings_db", "ratings.db"), ruleset_id=mode,
                                     reader=self.activity_reader)
        self.last_played_map = None           # what map feedback refers to
        self._rating_context = {}
        self._rating_window_until = 0.0
        self._rating_prompt_pending = False
        # MatchCompleted and playlist promotion are separate referee events and
        # can arrive in either order. Arm this at match end, then announce as
        # soon as the promoted next map is known. The id of the completed map
        # prevents accidentally reporting the just-finished map as "next".
        self._post_match_pp_announce_pending = False
        self._post_match_pp_completed_beatmap_id: int | None = None
        # Rating commands remain available after every completed map, but the
        # At least six completed maps and ten minutes between optional hints.
        self._rating_prompt_map_count = 0
        self._last_rating_prompt_at = 0.0
        # Keep the recently completed feedback targets in play order. This is
        # intentionally runtime-only: a restarted room should reintroduce its
        # brief feedback reminder until it sees a current regular rater again.
        self._recent_rating_map_ids: deque[int] = deque(maxlen=3)

        # Public vote-skip state. The first .skip opens an 8-second voting
        # window and, if a match-start countdown is already running, stops it
        # so the room has time to vote without racing MatchStarted. Only
        # non-spectating gameplay players count/vote, and the threshold is
        # frozen when the window opens. Chat is polled in batches, so skip
        # commands also carry the item id + message timestamp they were read
        # under; stale votes can never spill onto the replacement map.
        self._skip_votes: set[int] = set()
        self._skip_vote_until = 0.0
        self._skip_vote_item_id: int | None = None
        self._skip_vote_required = 0
        self._skip_vote_player_count = 0
        self._last_voteskip_disabled_notice = 0.0

        # Replay votes deliberately mirror the player-only, eight-second skip
        # quorum, but target the *previous completed* map.  They are kept
        # separate so a stale replay command can never become a skip vote.
        self._replay_votes: set[int] = set()
        self._replay_vote_until = 0.0
        self._replay_vote_item_id: int | None = None
        self._replay_vote_last_beatmap_id: int | None = None
        self._replay_vote_required = 0
        self._replay_vote_player_count = 0

        # Community difficulty preference is a temporary layer on top of the
        # admin-set auto_sr_bias. Public .harder/.easier votes live for two
        # completed maps. A successful vote adds one configurable temporary
        # star-rating step.
        # Each layer remembers the voters who created it; once all of those
        # voters leave, that layer decays by 0.05* per completed map until the
        # admin baseline is restored.
        self._completed_map_count = 0
        self._difficulty_vote_harder: set[int] = set()
        self._difficulty_vote_easier: set[int] = set()
        self._difficulty_vote_required = 0
        self._difficulty_vote_player_count = 0
        self._difficulty_vote_expires_after_map = 0
        # Three successful community difficulty changes before the next match
        # mean visible queue entries no longer reflect the advertised range.
        # Rebuild once at the safe between-map boundary instead of letting
        # players work through stale picks.
        self._difficulty_vote_changes_since_match = 0
        self._difficulty_vote_rebuild_pending = False
        self._difficulty_bias_layers: list[dict] = []
        # Fixed-difficulty rooms use the same public vote/threshold UI, but a
        # passed vote shifts the configured JSON SR window directly instead of
        # feeding the PP-based AutoDiff calculation. The JSON values remain
        # the permanent baseline. A quiet soft rebuild is queued when a voter
        # responsible for an active shift leaves.
        self._fixed_difficulty_dirty = False
        self._fixed_range_rebuild_pending = False

        # Download availability is checked through osu!web before a new map is
        # committed to the playlist. Explicit results are shared persistently;
        # incomplete fail-open results are cached only briefly.
        self._download_availability_cache: dict[
            int, tuple[bool, float, bool]
        ] = {}
        self._download_set_cache: dict[int, tuple[bool, float, bool]] = {}

        self._last_info = time.time()
        self._info_idx = 0
        self.queued: list[tuple[int, object]] = []   # [(playlist_item_id, PoolMap)]
        self._last_chat_poll = 0.0
        self._last_ack = 0.0
        self._last_sent = 0.0
        self._chat_websocket_inbox: queue.Queue = queue.Queue()
        self._chat_websocket_channel_id: int | None = None
        self._chat_websocket_generation = 0
        self._chat_websocket_needs_reconcile = False
        self._last_chat_websocket_reconcile = 0.0
        # Chat membership follows the one visible lazer client. In multi-room
        # mode a background worker therefore takes short, fair room visits and
        # retrieves that room's backlog without blocking referee event logic.
        self._chat_patrol_stop = threading.Event()
        self._chat_patrol_wake = threading.Event()
        self._chat_patrol_fetch_lock = threading.Lock()
        self._chat_patrol_thread: threading.Thread | None = None
        self._chat_patrol_inbox: queue.Queue[list[dict]] = queue.Queue(maxsize=50)
        self._chat_patrol_since = 0

        # Human-only recent chat is optional context for `.ai`; bot output and
        # commands are deliberately excluded.  AI results are returned to this
        # main thread before any osu! chat send, so the worker never touches the
        # multiplayer API or bot state directly.
        self._recent_human_chat = deque(maxlen=50)
        self._ai_outbox = deque()
        self._last_ai_sent = 0.0
        self._last_ai_unavailable_notice = 0.0
        self._last_config_check = 0.0
        # Effective JSON values that were noticed but cannot be safely swapped
        # into already-constructed/shared runtime objects.  Keeping them here
        # lets us report restart-required changes explicitly instead of
        # silently ignoring arbitrary JSON edits.
        self._pending_restart_config: dict[str, object] = {}
        try:
            self._config_mtime_ns = self._config_path.stat().st_mtime_ns
        except OSError:
            self._config_mtime_ns = None
        try:
            self._universal_config_mtime_ns = (
                self._universal_config_path.stat().st_mtime_ns
            )
        except OSError:
            self._universal_config_mtime_ns = None
        self.ai = AIService(
            self.cfg, project_root=Path(__file__).resolve().parents[1]
        )
        # Player-facing map PP comes from the official osu!lazer calculator
        # embedded by osu-tools-py.  It is informational only and runs
        # off-thread; map rotation never waits indefinitely for the calculator.
        self.pp_projection = MapPPProjection(self.cfg)

        self._state_since = time.time()
        self._idle_since = self._state_since
        self._match_end_at = 0.0
        self.running = True

    # ------------------------------------------------------ live config

    def _resolve_config_path(self) -> Path:
        """Locate the room JSON used for persistent live admin tuning.

        run_bot.py may provide an explicit path in newer installs. Older
        installs do not, so fall back to the normal project layout next to
        this package. Keeping the fallback inside the project makes it work
        even when Python is launched from a different working directory.
        """
        explicit = self.cfg.get("_config_path") or self.cfg.get("config_path")
        if explicit:
            p = Path(str(explicit)).expanduser()
            if not p.is_absolute():
                p = (Path.cwd() / p).resolve()
            return p

        project_root = Path(__file__).resolve().parents[1]
        default = project_root / "instances" / "room1.json"
        if default.exists():
            return default

        # If the filename was changed but there is only one room config, use
        # that instead of silently writing a new room1.json.
        instances = project_root / "instances"
        try:
            candidates = sorted(instances.glob("*.json"))
        except OSError:
            candidates = []
        if len(candidates) == 1:
            return candidates[0]

        return default

    def _resolve_universal_config_path(self) -> Path:
        """Locate the optional highest-priority shared config overlay."""
        explicit = self.cfg.get("_universal_config_path")
        if explicit:
            path = Path(str(explicit)).expanduser()
            if not path.is_absolute():
                path = (Path.cwd() / path).resolve()
            return path
        return self._config_path.parent / "universal.json"

    @serialized_config("_config_path")
    def _persist_config_updates(self, updates: dict) -> bool:
        """Atomically merge approved live settings into the room JSON.

        The file is read fresh before every write so unrelated keys are
        preserved. Runtime-only keys such as _authenticated_user_id are never
        dumped back to disk.
        """
        path = self._config_path
        try:
            if self.cfg.get('_host_root'):
                from poolrotater_host.runtime_config import render_room
                from poolrotater_host.layout import read_json
                native_room = render_room(Path(self.cfg['_host_root']), self.cfg['_instance_name'],
                    account_id=self.cfg.get('_authenticated_user_id'),
                    catalog_reference=self.cfg.get('_host_catalog_reference'),
                    catalog_set=self.cfg.get('_host_catalog_set'),
                    private_session=self.cfg.get('_host_private_pilot_session'))
                if path != Path(native_room['config_path']):
                    raise ValueError('unexpected host-managed configuration path')
            if path.exists():
                data = (read_json(path) if self.cfg.get('_host_root') else
                        json.loads(path.read_text(encoding="utf-8")))
                if not isinstance(data, dict):
                    raise ValueError("room config root is not a JSON object")
            else:
                if self.cfg.get('_host_root'):
                    raise ValueError('host-managed room configuration is missing')
                # Defensive fallback for a nonstandard install. Build from the
                # loaded config but exclude runtime-only/private keys.
                path.parent.mkdir(parents=True, exist_ok=True)
                data = {
                    k: v for k, v in self.cfg.items()
                    if not str(k).startswith("_")
                }

            if self.cfg.get('_host_root'):
                data.update(updates)
            elif self.cfg.get("ruleset_id", 0) == 3:
                local = data.setdefault("mania_settings", {})
                local.update({k: v for k, v in updates.items() if k in MANIA_KEYS})
                data.update({k: v for k, v in updates.items() if k not in MANIA_KEYS})
            else:
                data.update(updates)
            if self.cfg.get('_host_root'):
                from poolrotater_host.runtime_config import validate_room_data
                from poolrotater_host.layout import atomic_json
                base = native_room['settings']
                validate_room_data(data, base)
                atomic_json(path, data)
            else:
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                os.replace(tmp, path)
            try:
                self._config_mtime_ns = path.stat().st_mtime_ns
            except OSError:
                pass
            log.info(
                "persisted live config update to %s: %s",
                path, ", ".join(f"{k}={v!r}" for k, v in updates.items()),
            )
            return True
        except Exception as e:
            log.error("could not persist live config update to %s: %s", path, e)
            return False

    @serialized_config("_universal_config_path")
    def _persist_universal_config_updates(self, updates: dict) -> bool:
        """Atomically merge shared live settings into universal.json.

        Shared Discord tuning must affect every supervised room. The other
        controllers observe this write through their normal config watcher.
        """
        path = self._universal_config_path
        try:
            if self.cfg.get('_host_root'):
                raise ValueError('shared room overrides are unavailable in the offline host')
            with _UNIVERSAL_CONFIG_WRITE_LOCK:
                if path.exists():
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError("universal config root is not a JSON object")
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    data = {}
                data.update(updates)
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                os.replace(tmp, path)
                try:
                    self._universal_config_mtime_ns = path.stat().st_mtime_ns
                except OSError:
                    pass
            self.cfg.update(updates)
            log.info(
                "persisted shared config update to %s: %s",
                path, ", ".join(f"{k}={v!r}" for k, v in updates.items()),
            )
            return True
        except Exception as e:
            log.error("could not persist shared config update to %s: %s", path, e)
            return False

    @staticmethod
    def _parse_admin_int(raw: str, minimum: int, maximum: int) -> int:
        value = int(raw)
        if not minimum <= value <= maximum:
            raise ValueError(f"must be {minimum}-{maximum}")
        return value

    @staticmethod
    def _parse_admin_float(raw: str, minimum: float, maximum: float) -> float:
        value = float(raw)
        if not minimum <= value <= maximum:
            raise ValueError(f"must be {minimum:g}-{maximum:g}")
        return value

    def _persist_suffix(self, ok: bool) -> str:
        return "" if ok else " (live only; JSON save failed)"

    def _admin_ids(self) -> list[int]:
        out: list[int] = []
        for raw in self.cfg.get("admins", []):
            try:
                out.append(int(raw))
            except (TypeError, ValueError):
                continue
        return sorted(set(out))

    def _is_admin(self, user_id: int | None) -> bool:
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return False
        actor = getattr(self, "_discord_control_actors", {}).get(uid)
        return uid in set(self._admin_ids()) or bool(
            actor and actor.get("role") == "ADMIN"
        )

    def _moderator_ids(self) -> list[int]:
        out: list[int] = []
        for raw in self.cfg.get("moderators", []):
            try:
                out.append(int(raw))
            except (TypeError, ValueError):
                continue
        # Admins implicitly have every moderator permission.
        return sorted(set(out) - set(self._admin_ids()))

    def _is_moderator(self, user_id: int | None) -> bool:
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return False
        actor = getattr(self, "_discord_control_actors", {}).get(uid)
        return self._is_admin(uid) or uid in set(self._moderator_ids()) or bool(
            actor and actor.get("role") == "MOD"
        )

    def _allow_public_command_burst(self, user_id: int, cmd: str) -> bool:
        """Silently suppress sustained public command floods per user."""
        if cmd not in _PUBLIC_COMMANDS:
            return True
        try:
            burst = max(1, int(self.cfg.get("command_spam_burst", 5) or 5))
        except (TypeError, ValueError):
            burst = 5
        try:
            window = max(
                1.0,
                float(self.cfg.get("command_spam_window_seconds", 10.0) or 10.0),
            )
        except (TypeError, ValueError):
            window = 10.0
        try:
            block = max(
                1.0,
                float(self.cfg.get("command_spam_block_seconds", 20.0) or 20.0),
            )
        except (TypeError, ValueError):
            block = 20.0

        uid = int(user_id)
        if not hasattr(self, "_command_spam_history"):
            self._command_spam_history = {}
        if not hasattr(self, "_command_spam_block_until"):
            self._command_spam_block_until = {}
        now = time.monotonic()
        blocked_until = float(self._command_spam_block_until.get(uid, 0.0) or 0.0)
        if blocked_until > now:
            return False
        if blocked_until:
            self._command_spam_block_until.pop(uid, None)

        history = self._command_spam_history.setdefault(uid, deque())
        cutoff = now - window
        while history and history[0] < cutoff:
            history.popleft()
        if len(history) >= burst:
            self._command_spam_block_until[uid] = now + block
            history.clear()
            log.warning(
                "temporarily suppressing command flood from user %s after %s commands in %.1fs",
                uid,
                burst,
                window,
            )
            return False
        history.append(now)
        return True

    def _effective_auto_bias(self) -> float:
        """Admin-set baseline plus temporary community preference layers."""
        base = float(self.cfg.get("auto_sr_bias", 0.30))
        overlay = sum(float(layer.get("amount") or 0.0)
                      for layer in self._difficulty_bias_layers)
        return base + overlay

    def _community_bias_overlay(self) -> float:
        return self._effective_auto_bias() - float(
            self.cfg.get("auto_sr_bias", 0.30)
        )

    @staticmethod
    def _parse_on_off(raw: str) -> bool:
        value = str(raw).strip().casefold()
        if value in ("on", "true", "yes", "1", "enable", "enabled"):
            return True
        if value in ("off", "false", "no", "0", "disable", "disabled"):
            return False
        raise ValueError("use on or off")

    def _known_username(self, user_id: int) -> str | None:
        external = getattr(self, "_discord_control_actors", {}).get(int(user_id))
        if external and external.get("name"):
            return str(external["name"])
        info = self.players.get(int(user_id)) or {}
        live = str(info.get("name") or "").strip()
        if live:
            return live
        try:
            stored = self.pp_stats.username_for_user_id(int(user_id))
        except Exception:
            stored = None
        return str(stored).strip() if stored else None

    def _discord_lobby_role(self, user_id: int) -> str:
        if self._is_admin(user_id):
            return "ADMIN"
        if self._is_moderator(user_id):
            return "MOD"
        return "PLAYER"

    @staticmethod
    def _discord_map_key(map_obj: object | None) -> str:
        """Stable Discord reaction key for one selected beatmap."""
        try:
            return str(int(getattr(map_obj, "beatmap_id")))
        except (TypeError, ValueError):
            return ""

    def _notify_discord_lobby_chat(
        self,
        *,
        user_id: int,
        text: str,
    ) -> None:
        """Mirror one incoming human-chat line without affecting chat flow."""
        notifier = getattr(self, "discord_modlog", None)
        control = getattr(self, "discord_control", None)
        if notifier is None:
            return
        try:
            content = notifier.format_chat(
                role=self._discord_lobby_role(int(user_id)),
                username=self._known_username(int(user_id)) or str(user_id),
                user_id=int(user_id),
                text=text,
                is_command=text.startswith(self.prefix),
                room_id=self.room_id,
                room_name=str(getattr(self, "_current_room_name", "") or ""),
            )
            # Prefer the connected Discord bot: it writes directly into the
            # configured control/log channel. A webhook remains a fallback for
            # installations that want logs without inbound Discord control.
            if control is not None and control.send_to_channel(content):
                return
            notifier._submit_content(content)
        except Exception:
            # Webhook configuration/transport must never break lobby chat.
            log.exception("could not queue Discord lobby-chat notification")

    def _discord_invite_text(self, *, fallback: bool = False) -> str:
        url = str(self.cfg.get(
            "discord_invite_url", DEFAULT_DISCORD_INVITE
        ) or "").strip()
        if not url and fallback:
            url = DEFAULT_DISCORD_INVITE
        return f"Discord: {url}" if url else ""

    def _maybe_announce_website(self) -> None:
        """Alternate community links on the existing post-match reminder timer."""
        try:
            interval = max(
                0.0,
                float(self.cfg.get("discord_invite_reminder_seconds", 14400)),
            )
        except (TypeError, ValueError):
            interval = 14400.0
        if interval <= 0 or self.cfg.get("bot_chat_silenced", False):
            return
        if self.gameplay_player_count() <= 0:
            return
        now = time.time()
        if now - self._last_website_announcement < interval:
            return
        active = set(self.gameplay_player_ids())
        # Keep the existing cadence even when an unchanged group needs no
        # repeat. A newcomer becomes eligible at the next scheduled interval.
        self._last_website_announcement = now
        discord = self._discord_invite_text()
        discord_notified = (
            getattr(self, "_discord_notified_players", set())
            if discord == getattr(self, "_last_discord_reminder_text", "")
            else set()
        )
        links = [
            (False, "Website: https://poolrotater.com/",
             getattr(self, "_website_notified_players", set())),
            (True, discord, discord_notified),
        ]
        if getattr(self, "_next_community_link_is_discord", False):
            links.reverse()
        for is_discord, message, notified in links:
            if not message or (active and active.issubset(notified)):
                continue
            self.say(message, automatic=True)
            if is_discord:
                self._discord_notified_players = active
                self._last_discord_reminder_text = message
            else:
                self._website_notified_players = active
            self._next_community_link_is_discord = not is_discord
            break

    def _join_help_tip(self, user_id: int) -> str:
        """Introduce controls once to a familiar player, not on every rejoin."""
        if self.cfg.get("bot_chat_silenced", False):
            return ""
        introduced = getattr(self, "_introduced_players", None)
        if introduced is None:
            introduced = self._introduced_players = deque(maxlen=2048)
        uid = int(user_id)
        if uid in introduced:
            return ""
        introduced.append(uid)
        return f" {self.prefix}help for commands."

    def _first_player_welcome(self, user_id: int, player_name: str, recalled=None) -> str:
        if recalled:
            text = f"Welcome back, {player_name}! Restored {recalled[0]:.2f}-{recalled[1]:.2f}★. Ready up."
        else:
            text = f"Welcome, {player_name}—ready up to start."
        return self._language_personal(user_id, text + self._join_help_tip(user_id), "welcome")

    def _say_problem_once(self, key: str, message: str, *, context=None) -> bool:
        """Deduplicate recurring problems, not essential one-off warnings.

        A different map/roster/reason gets a fresh explanation immediately.
        An unchanged problem can receive an actionable reminder after 5 min.
        """
        if self.cfg.get("bot_chat_silenced", False):
            return False
        if context is None:
            current = getattr(self, "current_map", None)
            context = (getattr(current, "beatmap_id", None),
                       tuple(sorted(self.gameplay_player_ids())))
        notices = getattr(self, "_problem_notices", None)
        if notices is None:
            notices = self._problem_notices = {}
        now = time.monotonic()
        previous = notices.get(key)
        if previous and previous[0] == context and now - previous[1] < 300:
            return False
        self.say(message, automatic=True)
        notices[key] = (context, now)
        return True

    def _acknowledge_saved_feedback(self, rating_label: str, user_id: int) -> None:
        # The network agent publishes durable votes independently of gameplay.
        # Retain the original map/title while waiting for its verified total.
        from .feedback import FeedbackReplies
        if not hasattr(self, "_feedback_replies"):
            self._feedback_replies = FeedbackReplies(self.ratings)
        for message in self._feedback_replies.enqueue(
            self.last_played_map.beatmap_id, int(user_id),
            self.last_played_map.title, rating_label,
        ):
            self.say(message, automatic=True)

    def _drain_feedback_replies(self) -> None:
        replies = getattr(self, "_feedback_replies", None)
        if replies is not None:
            for message in replies.drain():
                self.say(message, automatic=True)

    def _maybe_prompt_for_rating(self) -> bool:
        """Optional feedback never crowds out useful replies/achievements."""
        if not self._rating_prompt_pending:
            return False
        now = time.time()
        if self.last_played_map is None or now > self._rating_window_until:
            return False
        if self._has_recent_lobby_rater():
            self._rating_prompt_map_count = 0
            self._rating_prompt_pending = False
            return False
        if self.cfg.get("bot_chat_silenced", False):
            return False
        if now - getattr(self, "_last_rating_prompt_at", 0.0) < 600:
            return False
        if now - getattr(self, "_last_non_hint_sent_at", 0.0) < 20:
            return False
        self._sending_feedback_hint = True
        try:
            self.say(
                f"Rate the last map: {self.prefix}good / {self.prefix}bad / {self.prefix}ok (neutral).",
                automatic=True,
            )
        finally:
            self._sending_feedback_hint = False
        self._last_rating_prompt_at = time.time()
        self._rating_prompt_map_count = 0
        self._rating_prompt_pending = False
        return True

    def _arm_rating_prompt_after_map(self) -> None:
        if self.last_played_map is None:
            self._rating_prompt_pending = False
            return
        self._rating_prompt_map_count += 1
        self._rating_prompt_pending = self._rating_prompt_map_count >= 6

    def _notify_discord_lobby_event(
        self,
        label: str,
        text: str,
        *,
        automatic: bool = False,
        map_key: str = "",
    ) -> None:
        """Best-effort event record; never affect lobby state or chat sends."""
        notifier = getattr(self, "discord_modlog", None)
        control = getattr(self, "discord_control", None)
        if notifier is None:
            return
        event_label = str(label or "").upper()
        try:
            content = notifier.format_event(
                label=event_label,
                text=text,
                room_id=self.room_id,
                room_name=str(getattr(self, "_current_room_name", "") or ""),
            )
            if control is not None:
                map_reactions = {
                    "MATCH START": "▶️",
                    "MATCH END": "🔚",
                    "MATCH ABORTED": "🆎",
                }
                if event_label in map_reactions:
                    # Keep an explicit lifecycle line even when Discord lost
                    # its in-memory map-message reference after a reconnect.
                    # The reaction is extra context on the map record, not the
                    # only indication that the match changed state.
                    posted = control.send_to_channel(content)
                    reacted = control.react_to_map(
                        map_reactions[event_label], map_key
                    )
                    if posted or reacted:
                        return
                elif control.send_to_channel(
                    content,
                    map_message=(event_label == "MAP"),
                    map_key=map_key,
                ):
                    return
            notifier._submit_content(content)
        except Exception:
            log.exception("could not queue Discord lobby-event notification")

    def _update_discord_public_status(
        self,
        *,
        force: bool = False,
        is_open: bool | None = None,
        lifecycle: str | None = None,
        rename_channel: bool = True,
    ) -> bool:
        """Publish a compact public room card when its visible state changes."""
        control = getattr(self, "discord_control", None)
        if control is None or not hasattr(control, "update_public_status"):
            return False

        if is_open is None:
            is_open = bool(self.room_id and self._room_ready and self.running)
        if lifecycle is None:
            lifecycle = {
                IDLE: "Waiting for players",
                STARTING: "Starting",
                PLAYING: "Playing",
                SCORING: "Between maps",
            }.get(self.state, str(self.state or "Online").title())
            if not is_open:
                lifecycle = "Offline"

        active = len(self.gameplay_player_ids())
        excluded = self.non_gameplay_user_ids()
        spectators = sum(
            1
            for uid, info in self.players.items()
            if int(uid) not in excluded
            and str((info or {}).get("status") or "").casefold()
            == "spectating"
        )
        if self.state == PLAYING:
            shown_map = self.playing_map or self.current_map
        elif self.state == STARTING:
            shown_map = self.starting_map or self.current_map
        else:
            shown_map = self.current_map

        room_id = int(self.room_id or 0)
        room_name = str(getattr(self, "_current_room_name", "") or "")
        map_label = str(getattr(shown_map, "label", "") or "")
        map_url = str(getattr(shown_map, "url", "") or "")
        snapshot = (
            bool(is_open),
            str(lifecycle),
            room_id,
            room_name,
            active,
            spectators,
            round(float(self.pool.sr_min), 3),
            round(float(self.pool.sr_max), 3),
            map_label,
            int(self.cfg.get("discord_public_status_channel_id", 0) or 0),
            str(self.cfg.get("discord_public_status_channel_name", "") or ""),
            str(self.cfg.get("discord_public_status_label", "") or ""),
            str(self.cfg.get("discord_public_join_base_url", "") or ""),
            bool(rename_channel),
        )
        if not force and snapshot == self._last_discord_public_status:
            return False
        queued = control.update_public_status(
            is_open=bool(is_open),
            lifecycle=str(lifecycle),
            room_id=room_id,
            room_name=room_name,
            player_count=active,
            spectator_count=spectators,
            sr_min=float(self.pool.sr_min),
            sr_max=float(self.pool.sr_max),
            map_label=map_label,
            map_url=map_url,
            rename_channel=bool(rename_channel),
        )
        if queued:
            self._last_discord_public_status = snapshot
        return queued

    def _drain_discord_control_commands(self, *, recovering=False) -> None:
        """Run authorized Discord commands on the normal lobby thread."""
        control = getattr(self, "discord_control", None)
        if control is None:
            return
        while True:
            try:
                incoming = control.get_command_nowait()
            except queue.Empty:
                return
            # Negative IDs keep this separate from osu! account IDs.  Preserve
            # the display name solely for logs and visible command responses.
            actor_id = -abs(int(incoming.discord_user_id))
            self._discord_control_actors[actor_id] = {
                "name": str(incoming.username),
                "role": str(incoming.role).upper(),
            }
            # The command is already visible as the member's original message
            # in this same Discord channel, so do not create a second transcript
            # line just to acknowledge its receipt.
            previous_reply_channel = self._discord_command_reply_channel_id
            self._discord_command_reply_channel_id = int(incoming.channel_id)
            try:
                if recovering:
                    prefix = str(self.cfg.get("command_prefix", "."))
                    command = incoming.content.strip().split()[0].removeprefix(prefix).casefold()
                    if command not in {"close", "shutdown", "settings"}:
                        self.say("Referee connection is recovering. Please retry this command when recovery completes; it has not been executed.")
                        continue
                self.handle_command(actor_id, incoming.content)
            except Exception:
                # One malformed remote command must never kill the main loop.
                log.exception(
                    "Discord command failed from %s:%s",
                    incoming.username,
                    incoming.discord_user_id,
                )
            finally:
                self._discord_command_reply_channel_id = previous_reply_channel

    def _resolve_user_reference(self, raw: str) -> tuple[int, str] | None:
        """Resolve a live/stored username or explicit numeric osu! user id.

        Exact usernames are checked before interpreting an all-digit token as an
        id because osu! usernames themselves can be numeric-looking.
        """
        wanted = str(raw or "").strip()
        if not wanted:
            return None

        folded = wanted.casefold()
        for uid, info in self.players.items():
            name = str(info.get("name") or uid)
            if name.casefold() == folded:
                return int(uid), name

        try:
            resolved = self.pp_stats.find_user_by_username(wanted)
        except Exception:
            log.exception("could not resolve username %r for admin control", wanted)
            resolved = None
        if resolved is not None:
            return int(resolved[0]), str(resolved[1])

        if wanted.isdigit():
            uid = int(wanted)
            return uid, (self._known_username(uid) or wanted)

        return None

    def _set_persistent_toggle(self, key: str, enabled: bool) -> bool:
        self._toggle_was_unchanged = self.cfg.get(key) == bool(enabled)
        self.cfg[key] = bool(enabled)
        return self._persist_config_updates({key: bool(enabled)})

    def _toggle_reply(self, label: str, state: str, saved: bool) -> str:
        already = " already" if getattr(self, "_toggle_was_unchanged", False) else ""
        return f"{label}{already} {state}.{self._persist_suffix(saved)}"

    def _reload_pool_filter(self, attr: str, value, json_key: str) -> tuple[bool, int]:
        """Apply a pool filter for future picks without touching queued maps."""
        old_value = getattr(self.pool, attr)
        old_maps = self.pool.maps
        setattr(self.pool, attr, value)
        try:
            n = self.pool.reload()
        except Exception as e:
            setattr(self.pool, attr, old_value)
            self.pool.maps = old_maps
            log.warning(
                "live pool setting %s=%r failed; restored %r: %s",
                json_key, value, old_value, e,
            )
            return False, len(old_maps)

        if n <= 0:
            setattr(self.pool, attr, old_value)
            self.pool.maps = old_maps
            log.warning(
                "live pool setting %s=%r produced an empty pool; restored %r",
                json_key, value, old_value,
            )
            return False, len(old_maps)

        self.cfg[json_key] = value
        return True, n

    def _set_manual_range_operator(self, lo: float, hi: float, user_id=None) -> None:
        """Apply and persist a manual SR range for an admin/mod operator."""
        p = self.prefix
        n = self.pool.set_range(float(lo), float(hi))
        if n == 0:
            # MapPool restored the previous known-good range.
            self.say(
                f"No eligible maps in {lo:.1f}-{hi:.1f}*; "
                f"keeping {self.pool.sr_min:.1f}-{self.pool.sr_max:.1f}*."
            )
            return

        self._recalled_window = None
        if user_id is not None:
            self._note_solo_difficulty_choice(user_id)
        self._auto_retune_pending = False
        self._clear_community_difficulty(
            "operator selected a manual SR range"
        )
        self._fixed_difficulty_dirty = False
        self._fixed_range_rebuild_pending = False
        # Lock it in until .auto is used again, including after restart.
        self.manual_range = True
        self.cfg["sr_min"] = self.pool.sr_min
        self.cfg["sr_max"] = self.pool.sr_max
        self.cfg["auto_difficulty"] = False
        saved = self._persist_config_updates({
            "sr_min": self.pool.sr_min,
            "sr_max": self.pool.sr_max,
            "auto_difficulty": False,
        })

        renamed_now = self.update_room_name_for_range(
            self.pool.sr_min, self.pool.sr_max
        )
        if self.cfg.get("ruleset_id", 0) == 3:
            self.say(f"Mania fixed range {self.pool.sr_min:.2f}-{self.pool.sr_max:.2f}* ({n} maps). Future picks use it; title updates between maps. {p}harder/{p}easier remain temporary votes.{self._persist_suffix(saved)}")
            return
        if renamed_now:
            self.say(
                f"Pool set to {self.pool.sr_min:.1f}-{self.pool.sr_max:.1f}* "
                f"({n} maps). Auto-difficulty off; {p}auto to re-enable."
                f"{self._persist_suffix(saved)}"
            )
        else:
            self.say(
                f"Pool set to {self.pool.sr_min:.1f}-{self.pool.sr_max:.1f}* "
                f"({n} maps). Room title updates between maps; "
                f"auto-difficulty off; {p}auto to re-enable."
                f"{self._persist_suffix(saved)}"
            )

    def _enable_auto_difficulty_operator(self) -> None:
        """Enable AutoDiff and persist it for an admin/mod operator."""
        if self.cfg.get("ruleset_id", 0) == 3:
            self.say("Mania AutoDiff is not calibrated. Use .range <min> <max>; players can vote .harder/.easier.")
            return
        self._recalled_window = None
        self._preference_choice = None
        self._preference_played = None
        self._preference_started = None
        self._clear_community_difficulty("operator enabled AutoDiff")
        self._fixed_difficulty_dirty = False
        self._fixed_range_rebuild_pending = False
        self.cfg["auto_difficulty"] = True
        saved = self._persist_config_updates({"auto_difficulty": True})
        self.manual_range = False
        self._auto_retune_pending = True
        # A deferred manual title is stale once AutoDiff is requested again.
        self._pending_room_name_range = None
        # Switch the dynamic title label immediately when safe. During a
        # countdown/match this queues the rename for the next safe boundary.
        self.update_room_name_for_range()

        if self.state == IDLE:
            self.apply_pending_auto_retune(force=True)
            self.say(
                "Auto-difficulty enabled."
                f"{self._persist_suffix(saved)}"
            )
        else:
            self.say(
                "Auto-difficulty enabled; range will update after this map."
                f"{self._persist_suffix(saved)}"
            )

    def _mania_key_label(self) -> str:
        """Return the validated native Mania key-mode label for player UI."""
        return f"{int(self.cfg.get('mania_keys', 4))}K"

    def _preferred_length_text(self):
        if getattr(self.pool, 'source_mode', '') == 'jump':
            return f'{self.pool.jump_preferred_min_length}–{self.pool.jump_preferred_max_length}s'
        return f'{self.pool.preferred_min_length}–{self.pool.preferred_max_length}s'

    def show_admin_settings(self) -> None:
        """Show operational knobs only to an authenticated lobby admin."""
        if self.cfg.get("ruleset_id", 0) == 3:
            self.say(f"Mania: {self._mania_key_label()} | {self.pool.source_mode_label()} | vetted converts up to {self.pool.mania_convert_max_share:.0%} in variety | AutoDiff unavailable | farm {'enabled (experimental)' if self.pool.mania_farm_enabled else 'disabled pending validation'} | mode/schema settings require restart.")
        mode = "manual" if self.manual_range else (
            "auto" if self.cfg.get("auto_difficulty", True) else "off"
        )
        half = float(self.cfg.get("auto_sr_half_width", AUTO_SR_HALF_WIDTH))
        bias = float(self.cfg.get("auto_sr_bias", 0.30))
        community = self._community_bias_overlay()
        effective_bias = self._effective_auto_bias()
        ahead = int(self.cfg.get("queue_ahead", 2))
        base_length = int(self.cfg.get("max_length", self.pool.max_length))
        max_age_text = self.pool.max_map_age_label()
        if getattr(self.pool, "loved_mode", False) and base_length <= 0:
            length_text = "length unlimited"
        elif int(self.pool.max_length) > base_length:
            length_text = (
                f"length {int(self.pool.max_length)}s active/{base_length}s base"
            )
        else:
            length_text = f"length {base_length}s"
        if getattr(self.pool, "real_max_length", None) is not None or getattr(self.pool, 'source_mode', '') == 'jump':
            share = self.pool.jump_preferred_length_share if self.pool.source_mode == 'jump' else self.pool.preferred_length_share
            hard = self.pool.real_max_length if self.pool.real_max_length is not None else self.pool.max_length
            length_text = (f"prefer {self._preferred_length_text()} "
                           f"({share:.0%}); hard max {hard}s")
        if self._fixed_difficulty_mode():
            base_lo, base_hi, _base_length = self._fixed_baseline_window()
            difficulty_settings = (
                f"Settings: {mode} active "
                f"{self.pool.sr_min:.2f}-{self.pool.sr_max:.2f}* | "
                f"set {base_lo:.2f}-{base_hi:.2f}* + temp "
                f"{community:+.2f}*"
            )
        else:
            difficulty_settings = (
                f"Settings: {mode} {self.pool.sr_min:.2f}-{self.pool.sr_max:.2f}* | "
                f"bias {bias:+.2f} + community {community:+.2f} "
                f"= {effective_bias:+.2f} | halfwidth {half:.2f}"
            )
        self.say(
            f"{difficulty_settings} | "
            f"queue {ahead} future/{ahead + 1} visible | "
            f"{length_text} | poollimit {self.pool.limit} | "
            f"minpasses {self.pool.min_passcount} | maxage {max_age_text}"
        )
        self.say(
            f"Timing: countdown {int(self.cfg.get('countdown_seconds', 15))}s | "
            f"autostart {int(self.cfg.get('auto_start_seconds', 45))}s | "
            f"minplayers {int(self.cfg.get('min_players', 1))} | "
            f"ratingwindow {int(self.cfg.get('rating_window_seconds', 90))}s | "
            f"scoredelay {float(self.cfg.get('score_delay_seconds', 4)):g}s | "
            f"infointerval {int(self.cfg.get('info_interval_seconds', 900))}s | "
            f"roomrefresh {self._hard_restart_interval_seconds() / 3600:g}h "
            f"({self._hard_restart_warning_maps()} map warning, "
            f"{self._hard_restart_close_delay_seconds():g}s close delay)"
        )
        self.say(
            f"API/chat: {int(self.cfg.get('requests_per_minute', 60))}/min "
            f"burst {int(self.cfg.get('requests_burst_capacity', 90))} | "
            f"realtime "
            f"{'connected' if self._chat_websocket_enabled() and getattr(self.chat_websocket, 'connected', False) else 'HTTP fallback'} "
            f"(reconcile {float(self.cfg.get('chat_websocket_reconcile_seconds', 300)):g}s) | "
            f"fallback poll {float(self.cfg.get('chat_poll_seconds', 5)):g}s, "
            f"playing {float(self.cfg.get('chat_poll_playing_seconds', 10)):g}s "
            f"({'adaptive' if self.cfg.get('adaptive_chat_polling', True) else 'fixed'}) | "
            f"profile cache {float(self.cfg.get('player_profile_cache_seconds', 21600)):g}s | "
            f"download cache {float(self.cfg.get('download_availability_cache_days', 90)):g}d"
        )
        self.say(
            f"Moderation: usercommands "
            f"{'on' if self.cfg.get('user_commands_enabled', True) else 'off'} | "
            f"botchat "
            f"{'silenced' if self.cfg.get('bot_chat_silenced', False) else 'normal'} | "
            f"voteskip "
            f"{'on' if self.cfg.get('voteskip_enabled', True) else 'off'} | "
            f"ppannounce "
            f"{'on' if self.cfg.get('map_pp_auto_announce_enabled', True) else 'off'} | "
            f"difficultyvote {float(self.cfg.get('difficulty_vote_ratio', 0.50)):.0%} "
            f"step {self._difficulty_vote_step():.2f}* "
            f"cap {int(self.cfg.get('difficulty_vote_max_steps', 3))} steps "
            f"(±{self._difficulty_vote_max_offset():.2f}*) | "
            f"mods {len(self._moderator_ids())} | admins {len(self._admin_ids())}"
        )
        ai_state = self.ai.status()
        self.say(
            f"AI: {'on' if self.cfg.get('ai_enabled', True) else 'off'} | "
            f"model {self.cfg.get('ai_model', 'gpt-5-mini')} | "
            f"usercd {float(self.cfg.get('ai_user_cooldown_seconds', 10)):g}s | "
            f"global {float(self.cfg.get('ai_global_cooldown_seconds', 3)):g}s | "
            f"pending {ai_state['pending']}/{int(self.cfg.get('ai_max_pending', 4))} | "
            f"chars {int(self.cfg.get('ai_max_output_chars', 320))} | "
            f"chatctx {int(self.cfg.get('ai_recent_chat_messages', 10))}"
        )

    def handle_admin_set(self, parts: list[str]) -> None:
        """Handle `.set <name> <value>` and persist successful changes."""
        p = self.prefix
        if len(parts) != 3:
            self.say(
                f"Usage: {p}set <name> <value> | names: bias halfwidth queue "
                "length poollimit minpasses countdown autostart minplayers "
                "ratingwindow scoredelay infointerval difficultyvote ai aimodel "
                "difficultyvotecap difficultyvotestep "
                "apilimit apiburst chatpoll adaptivepoll playingchatpoll "
                "profilecache availabilitycache "
                "aiusercooldown aiglobalcooldown aimaxpending aimaxchars "
                "aimaxtokens aimaxprompt aichatcontext aitimeout aitemperature"
            )
            return

        name = parts[1].lower().replace("_", "").replace("-", "")
        raw = parts[2]
        aliases = {
            "offset": "bias",
            "srbias": "bias",
            "autosrbias": "bias",
            "half": "halfwidth",
            "width": "halfwidth",
            "srhalfwidth": "halfwidth",
            "queueahead": "queue",
            "maxlength": "length",
            "maxseconds": "length",
            "limit": "poollimit",
            "passes": "minpasses",
            "minpass": "minpasses",
            "countdownseconds": "countdown",
            "autostartseconds": "autostart",
            "players": "minplayers",
            "rating": "ratingwindow",
            "ratingseconds": "ratingwindow",
            "score": "scoredelay",
            "infoseconds": "infointerval",
            "difficultyratio": "difficultyvote",
            "diffvote": "difficultyvote",
            "difficultyvoteratio": "difficultyvote",
            "difficultycap": "difficultyvotecap",
            "difficultyvotemax": "difficultyvotecap",
            "difficultyvotemaxsteps": "difficultyvotecap",
            "diffcap": "difficultyvotecap",
            "difficultystep": "difficultyvotestep",
            "difficultyvotestars": "difficultyvotestep",
            "diffstep": "difficultyvotestep",
            "aienabled": "ai",
            "aion": "ai",
            "usercooldown": "aiusercooldown",
            "aiusercd": "aiusercooldown",
            "globalcooldown": "aiglobalcooldown",
            "aiglobalcd": "aiglobalcooldown",
            "aipending": "aimaxpending",
            "aichars": "aimaxchars",
            "aitokens": "aimaxtokens",
            "aiprompt": "aimaxprompt",
            "maxprompt": "aimaxprompt",
            "airecentchat": "aichatcontext",
            "aichat": "aichatcontext",
            "aitimeoutseconds": "aitimeout",
            "temp": "aitemperature",
            "apirate": "apilimit",
            "requestsperminute": "apilimit",
            "burst": "apiburst",
            "apiburstcapacity": "apiburst",
            "chatpollseconds": "chatpoll",
            "adaptivechatpolling": "adaptivepoll",
            "playingpoll": "playingchatpoll",
            "chatpollplaying": "playingchatpoll",
            "chatpollplayingseconds": "playingchatpoll",
            "playerprofilecache": "profilecache",
            "playerprofilecacheseconds": "profilecache",
            "downloadcache": "availabilitycache",
            "downloadavailabilitycache": "availabilitycache",
            "downloadavailabilitycachedays": "availabilitycache",
        }
        name = aliases.get(name, name)
        if self.cfg.get("ruleset_id", 0) == 3 and name in ("bias", "halfwidth"):
            self.say("Standard AutoDiff tuning does not apply to mania. Use .range or .set difficultyvotestep instead.")
            return

        try:
            if name == "adaptivepoll":
                value = self._parse_on_off(raw)
                saved = self._persist_universal_config_updates({
                    "adaptive_chat_polling": value
                })
                self.say(
                    f"Adaptive chat polling {'enabled' if value else 'disabled'} "
                    "for all rooms."
                    f"{self._persist_suffix(saved)}"
                )
                return

            shared_int = {
                "apilimit": ("requests_per_minute", 1, 60, "API limit", "/min"),
                "apiburst": ("requests_burst_capacity", 1, 90, "API burst", " requests"),
            }
            if name in shared_int:
                key, lo, hi, label, suffix = shared_int[name]
                value = self._parse_admin_int(raw, lo, hi)
                saved = self._persist_universal_config_updates({key: value})
                configure = getattr(self.api, "configure_rate_limit", None)
                if callable(configure):
                    configure(
                        int(self.cfg.get("requests_per_minute", 60)),
                        int(self.cfg.get("requests_burst_capacity", 90)),
                    )
                self.say(
                    f"{label} set to {value}{suffix} for all rooms."
                    f"{self._persist_suffix(saved)}"
                )
                return

            shared_float = {
                "chatpoll": ("chat_poll_seconds", 0.5, 5.0,
                             "Active/waiting chat poll", "s"),
                "playingchatpoll": ("chat_poll_playing_seconds", 0.5, 60.0,
                                    "Playing chat poll", "s"),
                "profilecache": ("player_profile_cache_seconds", 0.0, 2_592_000.0,
                                 "Player profile cache", "s"),
                "availabilitycache": ("download_availability_cache_days", 0.0, 365.0,
                                      "Confirmed download cache", " days"),
            }
            if name in shared_float:
                key, lo, hi, label, suffix = shared_float[name]
                value = self._parse_admin_float(raw, lo, hi)
                saved = self._persist_universal_config_updates({key: value})
                if key == "player_profile_cache_seconds":
                    configure_cache = getattr(self.api, "configure_profile_cache", None)
                    if callable(configure_cache):
                        configure_cache(value)
                self.say(
                    f"{label} set to {value:g}{suffix} for all rooms."
                    f"{self._persist_suffix(saved)}"
                )
                return

            if name == "bias":
                value = self._parse_admin_float(raw, -1.50, 1.50)
                self.cfg["auto_sr_bias"] = value
                self._clear_community_difficulty(
                    "admin changed the baseline SR bias"
                )
                saved = self._persist_config_updates({"auto_sr_bias": value})
                if self.cfg.get("auto_difficulty", True) and not self.manual_range:
                    self._auto_retune_pending = True
                    apply_text = " applies after the next completed map"
                else:
                    apply_text = " saved for the next time auto difficulty is enabled"
                self.say(
                    f"Auto SR bias set to {value:+.2f}*;{apply_text}."
                    f"{self._persist_suffix(saved)}"
                )
                return

            if name == "halfwidth":
                value = self._parse_admin_float(raw, 0.10, 1.50)
                self.cfg["auto_sr_half_width"] = value
                saved = self._persist_config_updates({"auto_sr_half_width": value})
                if self.cfg.get("auto_difficulty", True) and not self.manual_range:
                    self._auto_retune_pending = True
                    apply_text = "applies after the next completed map"
                else:
                    apply_text = "saved for the next time auto difficulty is enabled"
                self.say(
                    f"Auto half-width set to {value:.2f}*; {apply_text}."
                    f"{self._persist_suffix(saved)}"
                )
                return

            if name == "queue":
                value = self._parse_admin_int(raw, 0, 8)
                self.cfg["queue_ahead"] = value
                saved = self._persist_config_updates({"queue_ahead": value})
                if self.state in (IDLE, SCORING):
                    self.reconcile_playlist(
                        reason="admin queue setting", fetch_remote=True,
                        ensure_depth=True, trim_extras=True,
                    )
                    apply_text = "playlist depth updated"
                else:
                    apply_text = "playlist depth updates after the match"
                self.say(
                    f"Queue set to {value} future map(s) "
                    f"({value + 1} visible); {apply_text}."
                    f"{self._persist_suffix(saved)}"
                )
                return

            if name in ("length", "poollimit", "minpasses"):
                if name == "length":
                    minimum = 0 if getattr(self.pool, "loved_mode", False) else 30
                    value = self._parse_admin_int(raw, minimum, 600)
                    attr, key, label = "max_length", "max_length", "Max map length"
                    suffix = "s" if value else " (unlimited)"
                elif name == "poollimit":
                    value = self._parse_admin_int(raw, 25, 5000)
                    attr, key, label = "limit", "pool_limit", "Pool limit"
                    suffix = ""
                else:
                    value = self._parse_admin_int(raw, 0, 1000000)
                    attr, key, label = "min_passcount", "min_passcount", "Min passes"
                    suffix = ""

                ok, n = self._reload_pool_filter(attr, value, key)
                if not ok:
                    self.say(
                        f"Rejected {name}={value}: it failed or left no eligible maps; "
                        "previous setting kept."
                    )
                    return
                saved = self._persist_config_updates({key: value})
                if (
                    name == "length"
                    and self.cfg.get("auto_difficulty", True)
                    and not self.manual_range
                ):
                    self._auto_retune_pending = True
                self.say(
                    f"{label} set to {value}{suffix}; {n} eligible maps for future "
                    f"picks. Existing queued maps untouched."
                    f"{self._persist_suffix(saved)}"
                )
                return

            timing = {
                "countdown": ("countdown_seconds", 1, 120, "Countdown", "s"),
                "autostart": ("auto_start_seconds", 0, 600, "Auto-start", "s"),
                "minplayers": ("min_players", 1, 32, "Minimum players", ""),
                "ratingwindow": ("rating_window_seconds", 10, 600, "Rating window", "s"),
                "infointerval": ("info_interval_seconds", 60, 7200, "Info interval", "s"),
            }
            if name in timing:
                key, lo, hi, label, suffix = timing[name]
                value = self._parse_admin_int(raw, lo, hi)
                self.cfg[key] = value
                saved = self._persist_config_updates({key: value})
                if name == "minplayers":
                    self.cancel_start_if_too_few_players()
                self.say(
                    f"{label} set to {value}{suffix}."
                    f"{self._persist_suffix(saved)}"
                )
                return

            if name == "difficultyvote":
                value = self._parse_admin_float(raw, 0.25, 1.0)
                self.cfg["difficulty_vote_ratio"] = value
                saved = self._persist_config_updates({
                    "difficulty_vote_ratio": value
                })
                self._clear_difficulty_vote("threshold changed by admin")
                self.say(
                    f"Difficulty vote threshold set to {value:.0%} of active "
                    f"players (minimum 2 when more than one player)."
                    f"{self._persist_suffix(saved)}"
                )
                return

            if name == "difficultyvotecap":
                value = self._parse_admin_int(raw, 0, 20)
                self.cfg["difficulty_vote_max_steps"] = value
                fixed_shift_active = bool(
                    self._fixed_difficulty_mode()
                    and self._difficulty_bias_layers
                )
                self._clear_community_difficulty(
                    "admin changed the difficulty-vote cap"
                )
                if self.cfg.get("auto_difficulty", True) and not self.manual_range:
                    self._auto_retune_pending = True
                elif fixed_shift_active:
                    self._fixed_difficulty_dirty = True
                    self._apply_fixed_community_range(
                        "admin changed the difficulty-vote cap"
                    )
                    if self.players:
                        self._fixed_range_rebuild_pending = True
                saved = self._persist_config_updates({
                    "difficulty_vote_max_steps": value
                })
                self.say(
                    f"Difficulty vote cap set to {value} step(s) "
                    f"(±{self._difficulty_vote_max_offset():.2f}*); "
                    "temporary community preference reset."
                    f"{self._persist_suffix(saved)}"
                )
                return

            if name == "difficultyvotestep":
                value = self._parse_admin_float(raw, 0.05, 0.50)
                self.cfg["difficulty_vote_step"] = value
                fixed_shift_active = bool(
                    self._fixed_difficulty_mode()
                    and self._difficulty_bias_layers
                )
                self._clear_community_difficulty(
                    "admin changed the difficulty-vote step"
                )
                if self.cfg.get("auto_difficulty", True) and not self.manual_range:
                    self._auto_retune_pending = True
                elif fixed_shift_active:
                    self._fixed_difficulty_dirty = True
                    self._apply_fixed_community_range(
                        "admin changed the difficulty-vote step"
                    )
                    if self.players:
                        self._fixed_range_rebuild_pending = True
                saved = self._persist_config_updates({
                    "difficulty_vote_step": value
                })
                self.say(
                    f"Difficulty vote step set to {value:.2f}*; "
                    f"cap is now ±{self._difficulty_vote_max_offset():.2f}*. "
                    "Temporary community preference reset."
                    f"{self._persist_suffix(saved)}"
                )
                return

            if name == "ai":
                value = self._parse_on_off(raw)
                self.cfg["ai_enabled"] = value
                saved = self._persist_config_updates({"ai_enabled": value})
                self.say(
                    f"AI chat {'enabled' if value else 'disabled'}."
                    f"{self._persist_suffix(saved)}"
                )
                return

            if name == "aimodel":
                value = str(raw).strip()
                if not value or len(value) > 120:
                    raise ValueError("model must be 1-120 characters")
                self.cfg["ai_model"] = value
                saved = self._persist_config_updates({"ai_model": value})
                self.say(
                    f"AI model set to {value}."
                    f"{self._persist_suffix(saved)}"
                )
                return

            ai_numeric = {
                "aiusercooldown": ("ai_user_cooldown_seconds", 0.0, 600.0, "AI per-user cooldown", "s", float),
                "aiglobalcooldown": ("ai_global_cooldown_seconds", 1.5, 60.0, "AI global reply spacing", "s", float),
                "aimaxpending": ("ai_max_pending", 1, 20, "AI max pending requests", "", int),
                "aimaxchars": ("ai_max_output_chars", 80, 1200, "AI max output characters", "", int),
                "aimaxtokens": ("ai_max_output_tokens", 32, 1000, "AI max completion tokens", "", int),
                "aimaxprompt": ("ai_max_prompt_chars", 100, 2000, "AI max prompt characters", "", int),
                "aichatcontext": ("ai_recent_chat_messages", 0, 30, "AI recent-chat context", " messages", int),
                "aitimeout": ("ai_timeout_seconds", 5.0, 120.0, "AI request timeout", "s", float),
                "aitemperature": ("ai_temperature", -1.0, 2.0, "AI temperature (-1 omits it)", "", float),
            }
            if name in ai_numeric:
                key, lo, hi, label, suffix, kind = ai_numeric[name]
                if kind is int:
                    value = self._parse_admin_int(raw, int(lo), int(hi))
                else:
                    value = self._parse_admin_float(raw, float(lo), float(hi))
                self.cfg[key] = value
                saved = self._persist_config_updates({key: value})
                self.say(
                    f"{label} set to {value:g}{suffix}."
                    f"{self._persist_suffix(saved)}"
                )
                return

            if name == "scoredelay":
                value = self._parse_admin_float(raw, 0.0, 30.0)
                self.cfg["score_delay_seconds"] = value
                saved = self._persist_config_updates({"score_delay_seconds": value})
                self.say(
                    f"Score delay set to {value:g}s."
                    f"{self._persist_suffix(saved)}"
                )
                return

            self.say(
                f"Unknown setting '{parts[1]}'. Use {p}settings or {p}help."
            )

        except ValueError as e:
            self.say(f"Invalid value for {parts[1]}: {e}")

    # -------------------------------------------------------------- AI chat

    _AI_PREFIX = "~AI * "
    _AI_EXTERNAL_KEYS = {
        "ai_enabled", "ai_model", "ai_base_url", "ai_endpoint",
        "ai_api_key_env", "ai_user_cooldown_seconds",
        "ai_global_cooldown_seconds", "ai_max_pending",
        "ai_max_output_chars", "ai_max_output_tokens", "ai_max_prompt_chars",
        "ai_recent_chat_messages", "ai_timeout_seconds",
        "ai_temperature", "ai_context_file", "ai_config_reload_seconds",
    }

    def _apply_ai_config_from_disk(self, data: dict) -> list[str]:
        """Apply validated AI-only settings from an already-read room JSON."""
        changed = []
        for key in self._AI_EXTERNAL_KEYS:
            if key not in data:
                continue
            value = data[key]
            try:
                if key == "ai_enabled":
                    if not isinstance(value, bool):
                        raise ValueError("must be true/false")
                elif key in ("ai_max_pending", "ai_max_output_chars",
                             "ai_max_output_tokens", "ai_max_prompt_chars",
                             "ai_recent_chat_messages"):
                    value = int(value)
                    bounds = {
                        "ai_max_pending": (1, 20),
                        "ai_max_output_chars": (80, 1200),
                        "ai_max_output_tokens": (32, 1000),
                        "ai_max_prompt_chars": (100, 2000),
                        "ai_recent_chat_messages": (0, 30),
                    }[key]
                    if not bounds[0] <= value <= bounds[1]:
                        raise ValueError(f"must be {bounds[0]}-{bounds[1]}")
                elif key in ("ai_user_cooldown_seconds",
                             "ai_global_cooldown_seconds",
                             "ai_timeout_seconds", "ai_temperature",
                             "ai_config_reload_seconds"):
                    value = float(value)
                    bounds = {
                        "ai_user_cooldown_seconds": (0.0, 600.0),
                        "ai_global_cooldown_seconds": (1.5, 60.0),
                        "ai_timeout_seconds": (5.0, 120.0),
                        "ai_temperature": (-1.0, 2.0),
                        "ai_config_reload_seconds": (0.5, 60.0),
                    }[key]
                    if not bounds[0] <= value <= bounds[1]:
                        raise ValueError(f"must be {bounds[0]}-{bounds[1]}")
                else:
                    value = str(value).strip()
                    if key != "ai_endpoint" and not value:
                        raise ValueError("may not be blank")
                if self.cfg.get(key) != value:
                    self.cfg[key] = value
                    changed.append(f"{key}={value!r}")
            except (TypeError, ValueError) as e:
                log.warning("ignored invalid hot-reloaded %s=%r: %s", key, value, e)
        return changed

    @staticmethod
    def _external_int(value, minimum: int, maximum: int) -> int:
        parsed = int(value)
        if not minimum <= parsed <= maximum:
            raise ValueError(f"must be {minimum}-{maximum}")
        return parsed

    @staticmethod
    def _external_float(value, minimum: float, maximum: float) -> float:
        parsed = float(value)
        if not minimum <= parsed <= maximum:
            raise ValueError(f"must be {minimum:g}-{maximum:g}")
        return parsed

    def _hard_restart_interval_seconds(self) -> float:
        """Configured lifetime of one discoverable room; zero disables it."""
        try:
            return self._external_float(
                self.cfg.get("hard_restart_interval_seconds", 21600.0),
                0.0,
                2_592_000.0,
            )
        except (TypeError, ValueError):
            return 21600.0

    def _hard_restart_warning_maps(self) -> int:
        try:
            return self._external_int(
                self.cfg.get("hard_restart_warning_maps", 3), 0, 20
            )
        except (TypeError, ValueError):
            return 3

    def _hard_restart_close_delay_seconds(self) -> float:
        try:
            return self._external_float(
                self.cfg.get("hard_restart_close_delay_seconds", 5.0),
                0.0,
                30.0,
            )
        except (TypeError, ValueError):
            return 5.0

    def _start_hard_restart_cycle(self) -> None:
        """Start a fresh discovery-refresh timer for a newly created room."""
        now = time.time()
        self._room_opened_at = now
        interval = self._hard_restart_interval_seconds()
        self._automatic_hard_restart_due_at = (
            now + interval if interval > 0 else 0.0
        )
        self._pending_hard_restart_maps = None
        self._pending_hard_restart_reason = ""
        self._automatic_hard_restart_waiting_for_empty = False
        if interval > 0:
            log.info(
                "automatic hard restart scheduled for room %s in %.0fs",
                self.room_id,
                interval,
            )
        else:
            log.info("automatic hard restart disabled for room %s", self.room_id)

    def _reschedule_hard_restart_cycle_from_config(self) -> None:
        """Apply a live interval edit without disturbing a manual countdown."""
        opened_at = float(getattr(self, "_room_opened_at", 0.0) or 0.0)
        if opened_at <= 0:
            return
        interval = self._hard_restart_interval_seconds()
        self._automatic_hard_restart_due_at = (
            opened_at + interval if interval > 0 else 0.0
        )
        if interval <= 0 and (
            getattr(self, "_pending_hard_restart_reason", "") == "automatic"
        ):
            self._pending_hard_restart_maps = None
            self._pending_hard_restart_reason = ""
            log.info("cancelled pending automatic hard restart after config disabled it")

    def queue_hard_restart(self, maps_ahead: int, *, reason: str) -> bool:
        """Queue a room recreation after a number of *completed* maps."""
        try:
            maps = self._external_int(maps_ahead, 1, 100)
        except (TypeError, ValueError):
            return False

        previous = getattr(self, "_pending_hard_restart_maps", None)
        self._pending_hard_restart_maps = maps
        self._pending_hard_restart_reason = reason
        suffix = "" if previous is None else " (replaced previous restart request)"
        self.say(
            f"Room restarting in {maps} {'map' if maps == 1 else 'maps'}."
            f"{suffix}"
        )
        log.info("queued %s hard restart in %d completed map(s)", reason, maps)
        return True

    def _run_queued_hard_restart_after_map(self) -> bool:
        """Tick the warning countdown after a match; restart at its end."""
        remaining = getattr(self, "_pending_hard_restart_maps", None)
        if remaining is None:
            return False
        try:
            remaining = int(remaining)
        except (TypeError, ValueError):
            self._pending_hard_restart_maps = None
            self._pending_hard_restart_reason = ""
            return False

        if remaining > 1:
            remaining -= 1
            self._pending_hard_restart_maps = remaining
            self.say(
                f"Room restarting in {remaining} "
                f"{'map' if remaining == 1 else 'maps'}."
            )
            return False

        reason = getattr(self, "_pending_hard_restart_reason", "") or "manual"
        self._pending_hard_restart_maps = None
        self._pending_hard_restart_reason = ""
        self.say("Restarting lobby — be right back.")
        return self.hard_restart_room(
            announce=False,
            close_delay_seconds=self._hard_restart_close_delay_seconds(),
            close_reason=(
                "automatic_hard_restart"
                if reason == "automatic" else "manual_hard_restart"
            ),
        )

    def _maybe_queue_automatic_hard_restart(self) -> bool:
        """Arm or safely execute the periodic discovery-listing refresh.

        Returns true only when this call actually recreated the room. Occupied
        rooms remain untouched until every player and spectator has left. This
        scheduled maintenance is deliberately silent: unlike an explicit
        moderator restart, it must not distract active players with countdowns
        or restart notices.
        """
        if getattr(self, "_pending_hard_restart_maps", None) is not None:
            # Explicit map-counted restarts always take precedence over the
            # scheduled empty-room refresh.
            return False
        due_at = float(getattr(self, "_automatic_hard_restart_due_at", 0.0) or 0.0)
        if due_at <= 0 or time.time() < due_at:
            return False

        # ``players`` includes spectators but excludes the authenticated
        # operator/referees.  A scheduled refresh must therefore wait for this
        # exact collection to empty, not merely for gameplay_player_count().
        if self.players:
            if not getattr(self, "_automatic_hard_restart_waiting_for_empty", False):
                self._automatic_hard_restart_waiting_for_empty = True
                log.info(
                    "automatic hard restart due for room %s; waiting for an empty roster",
                    self.room_id,
                )
            return False

        self._automatic_hard_restart_waiting_for_empty = False
        log.info("automatic hard restart proceeding for empty room %s", self.room_id)
        return self.hard_restart_room(
            announce=False,
            close_delay_seconds=self._hard_restart_close_delay_seconds(),
            close_reason="automatic_hard_restart",
        )

    def _difficulty_vote_max_offset(self) -> float:
        """Return the temporary AutoDiff ceiling for community votes."""
        try:
            steps = self._external_int(
                self.cfg.get("difficulty_vote_max_steps", 3), 0, 20
            )
        except (TypeError, ValueError):
            steps = 3
        return steps * self._difficulty_vote_step()

    def _difficulty_vote_step(self) -> float:
        """Return one passed .harder/.easier increment in star rating."""
        try:
            return self._external_float(
                self.cfg.get("difficulty_vote_step", 0.20), 0.05, 0.50
            )
        except (TypeError, ValueError):
            return 0.20

    _DURATION_KEYS = ('real_max_length', 'preferred_min_length',
                      'preferred_max_length', 'preferred_length_share')
    _JUMP_KEYS = ('jump_preferred_min_length', 'jump_preferred_max_length',
                  'jump_preferred_length_share', 'jump_preferred_ar_min',
                  'jump_preferred_ar_max', 'jump_preferred_ar_boost')

    def _apply_hot_jump_policy(self, data):
        if (getattr(self, 'pool', None) is None
                or not callable(getattr(self.pool, 'configure_jump_policy', None))):
            return []
        previous = {key: getattr(self.pool, key) for key in self._JUMP_KEYS}
        try:
            self.pool.configure_jump_policy(data)
        except ValueError as exc:
            log.warning('ignored invalid Jump preferences: %s', exc)
            return []
        changed = [f'{key}={getattr(self.pool, key)!r}' for key in self._JUMP_KEYS
                   if previous[key] != getattr(self.pool, key)]
        for key in self._JUMP_KEYS:
            if key in data:
                self.cfg[key] = data[key]
            else:
                self.cfg.pop(key, None)
        return changed

    def _apply_hot_duration_policy(self, data: dict) -> list[str]:
        if getattr(self, 'pool', None) is None or not hasattr(self.pool, 'configure_duration_policy'):
            return []
        try:
            policy = self.pool.duration_policy_from_config(data)
            if policy is None:
                if self.pool.real_max_length is None:
                    return []
                previous = dict(self.pool.__dict__)
                try:
                    self.pool.real_max_length = None
                    self.pool.max_length = int(data.get('max_length', self.cfg['max_length']))
                    if self.pool.reload() <= 0:
                        raise ValueError('removing duration policy left no eligible maps')
                except Exception:
                    self.pool.__dict__.clear()
                    self.pool.__dict__.update(previous)
                    raise
                for key in self._DURATION_KEYS:
                    self.cfg.pop(key, None)
                self.cfg['max_length'] = self.pool.max_length
                return ['duration policy disabled']
            if all(getattr(self.pool, key) == value for key, value in policy.items()):
                return []
            self.pool.configure_duration_policy(data)
        except Exception as exc:
            log.warning('ignored duration policy; previous policy kept: %s', exc)
            return []
        self.cfg.update(policy)
        self.cfg['max_length'] = policy['real_max_length']
        self._auto_retune_pending = True
        return [f'{key}={value!r}' for key, value in policy.items()]

    def _apply_hot_pool_filters(self, data: dict) -> list[str]:
        """Safely reload pool/filter/source settings for future selections."""
        # Unit/smoke callers may exercise config parsing without constructing a
        # live MapPool. A real LobbyBot always has one, but treating that case
        # as "nothing to rebuild yet" keeps the reload path side-effect safe.
        if getattr(self, "pool", None) is None:
            return []
        duration_changed = self._apply_hot_duration_policy(data)+self._apply_hot_jump_policy(data)
        if getattr(self.pool, 'real_max_length', None) is not None:
            # Legacy room max_length remains available to old workers during a
            # rolling update; new workers exclusively use the explicit hard cap.
            data = dict(data, max_length=self.pool.real_max_length)
        fields = {
            "max_length": (
                "max_length",
                0 if getattr(getattr(self, "pool", None), "loved_mode", False)
                else 30,
                600,
                int,
            ),
            "min_passcount": ("min_passcount", 0, 1_000_000, int),
            "pool_limit": ("limit", 25, 5_000, int),
            "max_map_age_years": ("max_map_age_years", 0.0, 100.0, float),
            "stream_min_score": ("stream_min_score", 0.0, 10.0, float),
            "stream_min_bpm": ("stream_min_bpm", 0.0, 1000.0, float),
            "stream_min_length": ("stream_min_length", 0, 600, int),
        }
        desired: dict[str, object] = {}
        for key, (attr, minimum, maximum, kind) in fields.items():
            if key not in data:
                continue
            try:
                value = (
                    self._external_int(data[key], int(minimum), int(maximum))
                    if kind is int else
                    self._external_float(data[key], float(minimum), float(maximum))
                )
            except (TypeError, ValueError) as e:
                log.warning("ignored invalid hot-reloaded %s=%r: %s", key, data[key], e)
                continue
            # Compare the configured baseline, not the active pool attribute:
            # AutoDiff may temporarily widen max_length to keep a thin pool
            # healthy, and an unchanged JSON save must not undo that runtime
            # guard on every watcher pass.
            if self.cfg.get(key) != value:
                desired[key] = value

        source_requested = False
        stream_enabled = bool(data.get(
            "stream_enabled", self.cfg.get("stream_enabled", True)
        ))
        try:
            stream_share = self._external_float(
                data.get("stream_share", self.cfg.get("stream_share", 0.5)),
                0.0,
                1.0,
            )
        except (TypeError, ValueError) as e:
            log.warning(
                "ignored invalid hot-reloaded stream_share=%r: %s",
                data.get("stream_share"), e,
            )
            stream_share = float(self.cfg.get("stream_share", 0.5) or 0.5)
        if "stream_enabled" in data and not isinstance(data["stream_enabled"], bool):
            log.warning(
                "ignored invalid hot-reloaded stream_enabled=%r: must be true/false",
                data["stream_enabled"],
            )
            stream_enabled = bool(self.cfg.get("stream_enabled", True))
        if (
            ("stream_enabled" in data and self.cfg.get("stream_enabled") != stream_enabled)
            or (
                "stream_share" in data
                and float(self.cfg.get("stream_share", 0.5) or 0.0) != stream_share
            )
        ):
            source_requested = True

        if not desired and not source_requested:
            return duration_changed

        old_attrs = {
            attr: getattr(self.pool, attr)
            for attr, _minimum, _maximum, _kind in fields.values()
        }
        old_maps = self.pool.maps
        old_source = (
            getattr(self.pool, "source_mode", None),
            getattr(self.pool, "farm_share", None),
            getattr(self.pool, "stream_share", None),
            getattr(self.pool, "stream_enabled", None),
        )
        try:
            for key, value in desired.items():
                attr = fields[key][0]
                setattr(self.pool, attr, value)
            if source_requested and not getattr(self.pool, "loved_mode", False) and self.cfg.get("ruleset_id", 0) != 3:
                if not stream_enabled or stream_share <= 0.0:
                    self.pool.source_mode = "farm"
                    self.pool.farm_share = 1.0
                    self.pool.stream_share = 0.0
                    self.pool.stream_enabled = False
                elif stream_share >= 1.0:
                    self.pool.source_mode = "stream"
                    self.pool.farm_share = 0.0
                    self.pool.stream_share = 1.0
                    self.pool.stream_enabled = True
                else:
                    self.pool.source_mode = "variety"
                    self.pool.farm_share = 1.0 - stream_share
                    self.pool.stream_share = stream_share
                    self.pool.stream_enabled = True
            count = self.pool.reload()
        except Exception as e:
            count = 0
            log.warning("hot-reloaded pool filters failed: %s", e)

        if count <= 0:
            for attr, value in old_attrs.items():
                setattr(self.pool, attr, value)
            self.pool.maps = old_maps
            (
                self.pool.source_mode,
                self.pool.farm_share,
                self.pool.stream_share,
                self.pool.stream_enabled,
            ) = old_source
            log.warning(
                "hot-reloaded pool filters left no eligible maps; keeping previous filters"
            )
            return duration_changed

        changed = []
        for key, value in desired.items():
            self.cfg[key] = value
            changed.append(f"{key}={value!r}")
        if source_requested:
            self.cfg["stream_enabled"] = stream_enabled
            self.cfg["stream_share"] = stream_share
            changed.extend([
                f"stream_enabled={stream_enabled!r}",
                f"stream_share={stream_share!r}",
            ])
        if "max_length" in desired and self.cfg.get("auto_difficulty", True) and not self.manual_range:
            self._auto_retune_pending = True
        return duration_changed + changed

    _CONFIG_RESTART_REQUIRED_KEYS = frozenset({
        "mania_ln_pick_multiplier",
        "standard_dt_db", "dt_auto_threshold", "dt_pick_share",
        "sparse_passcount_enabled", "sparse_passcount_min_maps", "sparse_passcount_floor",
        "ruleset_id", "mania_db", "mania_keys", "mania_source_mode",
        "mania_min_length", "mania_style_min_score", "mania_farm_enabled",
        "mania_min_rank_score", "mania_min_confidence", "mania_min_appearances", "mania_min_bands",
        "mania_farm_db", "mania_farm_bonus_max", "mania_farm_bonus_min_confidence",
        "mania_convert_db", "mania_convert_max_share", "mania_style_fallback_enabled",
        # Account/referee transport identity.
        "spectator_url", "token_file", "oauth_client_id_env",
        "oauth_client_secret_env",
        # Already-open SQLite/shared-process resources.
        "api_rate_db", "process_control_db", "ratings_db",
        "lazer_coordinator_db", "log_file",
        # One shared chat WebSocket is owned by the supervisor, not a room.
        "chat_websocket_enabled", "chat_websocket_endpoint",
        "chat_websocket_reconnect_max_seconds",
        # One shared Discord gateway client/routing map is built at startup.
        "discord_control_enabled", "discord_control_bot_token_env",
        "discord_control_guild_id", "discord_control_channel_id",
        "discord_control_channel_ids_by_room",
        "discord_control_primary_instance",
        "discord_control_known_room_aliases",
        "discord_public_status_channel_id",
        # Changing the room alias changes supervisor ownership/thread routing.
        "discord_control_room_alias",
        "discord_public_status_state_file",
        # Supervisor topology.
        "room_count",
    })

    _GENERIC_CONFIG_SKIP_KEYS = frozenset({
        "mania_settings",
        # Dedicated handlers below / in the existing validated reload path.
        "sr_min", "sr_max", "room_name", "version", "auto_difficulty",
        "command_prefix", "allowed_mods", "required_mods", "freestyle",
        "farm_db", "loved_db", "loved_mode",
        "high_star_relax_enabled", "high_star_relax_threshold",
        "high_star_relax_min_maps", "high_star_relax_length_step",
        "high_star_relax_age_step_years", "high_star_relax_max_steps",
        "max_length", "min_passcount", "pool_limit", "max_map_age_years",
        "real_max_length", "preferred_min_length", "preferred_max_length",
        "preferred_length_share",
        "jump_preferred_min_length", "jump_preferred_max_length", "jump_preferred_length_share",
        "jump_preferred_ar_min", "jump_preferred_ar_max", "jump_preferred_ar_boost",
        "stream_enabled", "stream_share", "stream_min_score",
        "stream_min_bpm", "stream_min_length",
        "user_commands_enabled", "bot_chat_silenced", "voteskip_enabled",
        "map_pp_display_enabled", "map_pp_auto_announce_enabled",
        "admins", "moderators",
        "lazer_rejoin_on_hard_restart", "lazer_coordinator_enabled",
        "lazer_chat_patrol_enabled", "adaptive_chat_polling",
        "countdown_seconds", "auto_start_seconds", "min_players",
        "queue_ahead", "rating_window_seconds", "info_interval_seconds", "default_response_language",
        "difficulty_vote_max_steps", "hard_restart_warning_maps",
        "requests_per_minute", "requests_burst_capacity",
        "lazer_visit_max_actions", "command_spam_burst",
        "auto_sr_bias", "auto_sr_half_width", "difficulty_vote_ratio",
        "difficulty_vote_step", "score_delay_seconds",
        "config_reload_seconds", "discord_invite_reminder_seconds",
        "hard_restart_interval_seconds", "hard_restart_close_delay_seconds",
        "lazer_rejoin_delay_seconds", "lazer_join_delay_seconds",
        "lazer_leave_delay_seconds", "lazer_leave_confirm_delay_seconds",
        "lazer_minimum_room_dwell_seconds", "lazer_visit_max_seconds",
        "lazer_chat_patrol_interval_seconds", "lazer_switch_timeout_seconds",
        "lazer_queue_poll_seconds", "api_usage_log_seconds",
        "chat_poll_seconds", "chat_poll_playing_seconds",
        "chat_websocket_reconcile_seconds", "playlist_audit_seconds",
        "command_spam_window_seconds", "command_spam_block_seconds",
        "discord_status_rename_spacing_seconds", "auto_empty_reset_seconds",
        "player_profile_cache_seconds", "download_availability_cache_days",
        "freedom_dive_cooldown_seconds",
    }) | _AI_EXTERNAL_KEYS | _CONFIG_RESTART_REQUIRED_KEYS

    def _note_restart_required_config(self, data: dict) -> None:
        """Report effective JSON edits that cannot be swapped into live owners."""
        if not hasattr(self, "_pending_restart_config"):
            self._pending_restart_config = {}
        for key in self._CONFIG_RESTART_REQUIRED_KEYS:
            if key not in data:
                continue
            desired = data[key]
            current = self.cfg.get(key)
            if desired == current:
                self._pending_restart_config.pop(key, None)
                continue
            if self._pending_restart_config.get(key) == desired:
                continue
            self._pending_restart_config[key] = desired
            log.warning(
                "config change detected for %s=%r; this setting belongs to a "
                "constructed/shared runtime object and requires a supervisor "
                "restart. Current live value remains %r",
                key, desired, current,
            )

    def _hot_rebuild_pool_source(self, data: dict) -> list[str]:
        """Rebuild MapPool when its database/ruleset source changes.

        This is safe because the replacement is built completely before
        ``self.pool`` is swapped. The visible osu! playlist is then rebuilt at
        the next safe between-map boundary.
        """
        if self.cfg.get("ruleset_id", 0) == 3:
            return []  # Mania source/schema settings are restart-required.
        keys = ("farm_db", "loved_db", "loved_mode")
        if not any(key in data and self.cfg.get(key) != data[key] for key in keys):
            return []
        if getattr(self, "pool", None) is None:
            return []

        desired = dict(self.cfg)
        desired.update({key: data[key] for key in keys if key in data})
        loved_mode = bool(desired.get("loved_mode", False))
        db_key = "loved_db" if loved_mode else "farm_db"
        db_path = Path(str(desired.get(db_key, "")))
        if not db_path.is_file():
            log.warning(
                "hot pool rebuild rejected: %s does not exist at %s",
                db_key, db_path,
            )
            return []

        try:
            dt_options = {}
            constructor = MapPool
            if not loved_mode and desired.get('standard_dt_db') and self._dt_fixed_mods_supported():
                from .standard_pool import StandardPool
                constructor = StandardPool
                dt_options = {key: desired[key] for key in
                              ('standard_dt_db', 'dt_auto_threshold', 'dt_pick_share') if key in desired}
            new_pool = constructor(
                str(db_path),
                **dt_options,
                # Preserve the active runtime window. sr_min/sr_max in JSON are
                # still the empty/fixed baseline and are handled separately.
                sr_min=float(self.pool.sr_min),
                sr_max=float(self.pool.sr_max),
                max_length=int(desired.get("max_length", self.pool.max_length)),
                min_passcount=int(desired.get("min_passcount", self.pool.min_passcount)),
                limit=int(desired.get("pool_limit", self.pool.limit)),
                ratings_db=str(self.cfg.get("ratings_db", "state/ratings.db")),
                ratings_reader=getattr(self,'rating_reader',None),
                stream_enabled=bool(desired.get("stream_enabled", True)),
                stream_share=float(desired.get("stream_share", 0.5)),
                stream_min_score=float(desired.get("stream_min_score", 0.5)),
                stream_min_bpm=float(desired.get("stream_min_bpm", 140.0)),
                stream_min_length=int(desired.get("stream_min_length", 45)),
                max_map_age_years=float(desired.get("max_map_age_years", 0.0)),
                sparse_passcount_enabled=getattr(self.pool, 'sparse_passcount_enabled', False),
                sparse_passcount_min_maps=getattr(self.pool, 'sparse_passcount_min_maps', 50),
                sparse_passcount_floor=getattr(self.pool, 'sparse_passcount_floor', 1000),
                loved_mode=loved_mode,
            )
            if 'real_max_length' in desired:
                new_pool.configure_duration_policy(desired)
            new_pool.configure_jump_policy(desired)
            if getattr(new_pool, 'mixed_dt', False):
                new_pool.set_dt_override(getattr(self.pool, 'dt_override', None),
                                         full=getattr(self.pool, 'dt_full', False))
                if (callable(getattr(type(self.pool), 'active_styles', None))
                        and all(desired.get(key) == self.cfg.get(key)
                                for key in ('stream_enabled', 'stream_share'))):
                    styles = self.pool.active_styles()
                    new_pool._last_variety_mix = self.pool._last_variety_mix
                    if styles and not new_pool.set_style_selection(styles):
                        raise ValueError('active styles do not fit the rebuilt catalog')
        except Exception as exc:
            log.warning("hot pool rebuild rejected; keeping current pool: %s", exc)
            return []
        if not new_pool.maps:
            log.warning("hot pool rebuild produced no eligible maps; keeping current pool")
            return []

        # Hot rebuilding a catalog must not reset the song cooldown.
        new_pool._recent_songs = list(getattr(self.pool, '_recent_songs', []))
        new_pool._song_rounds = list(getattr(self.pool, '_song_rounds', []))
        self.pool = new_pool
        changed: list[str] = []
        for key in keys:
            if key in data and self.cfg.get(key) != data[key]:
                self.cfg[key] = data[key]
                changed.append(f"{key}={data[key]!r}")
        if changed:
            self._pending_soft_restart = True
            log.info(
                "hot-rebuilt map pool source; playlist will refresh at the next safe boundary"
            )
        return changed

    def _apply_hot_playlist_rules(self, data: dict) -> list[str]:
        """Hot-load allowed/required mods and freestyle for future playlist items."""
        if not any(key in data for key in ("allowed_mods", "required_mods", "freestyle")):
            return []
        allowed = data.get("allowed_mods", self.cfg.get("allowed_mods", []))
        required = data.get("required_mods", self.cfg.get("required_mods", []))
        freestyle = data.get("freestyle", self.cfg.get("freestyle", True))
        if not isinstance(freestyle, bool):
            log.warning("ignored invalid hot-reloaded freestyle=%r: must be true/false", freestyle)
            return []
        try:
            if self.cfg.get("ruleset_id", 0) == 3:
                validate_mania_mods(allowed, required, freestyle)
            validate_mods(allowed, required)
            if freestyle and (allowed or required):
                raise ValueError("freestyle=true needs empty allowed_mods and required_mods")
        except (Exception, SystemExit) as exc:
            log.warning("ignored invalid hot-reloaded playlist mod settings: %s", exc)
            return []

        changed: list[str] = []
        for key, value in (
            ("allowed_mods", list(allowed)),
            ("required_mods", list(required)),
            ("freestyle", freestyle),
        ):
            if self.cfg.get(key) != value:
                self.cfg[key] = value
                changed.append(f"{key}={value!r}")
        if changed:
            if self.state in (IDLE, SCORING):
                self.perform_soft_restart(announce=False)
            else:
                self._pending_soft_restart = True
        return changed

    def _apply_hot_staff_lists(self, data: dict) -> list[str]:
        """Normalize JSON admin/moderator IDs without requiring a restart."""
        if "admins" not in data and "moderators" not in data:
            return []

        def normalize(name: str, fallback: object) -> list[int] | None:
            raw = data.get(name, fallback)
            if not isinstance(raw, (list, tuple, set)):
                log.warning(
                    "ignored invalid hot-reloaded %s=%r: use a list of osu! user IDs",
                    name,
                    raw,
                )
                return None
            out: list[int] = []
            try:
                for value in raw:
                    uid = int(value)
                    if uid > 0:
                        out.append(uid)
            except (TypeError, ValueError):
                log.warning(
                    "ignored invalid hot-reloaded %s=%r: use numeric osu! user IDs",
                    name,
                    raw,
                )
                return None
            return sorted(set(out))

        current_admins = list(self.cfg.get("admins", []))
        current_mods = list(self.cfg.get("moderators", []))
        admins = normalize("admins", current_admins)
        moderators = normalize("moderators", current_mods)
        if admins is None or moderators is None:
            return []
        moderators = sorted(set(moderators) - set(admins))

        changed: list[str] = []
        if current_admins != admins:
            self.cfg["admins"] = admins
            changed.append(f"admins={admins!r}")
        if current_mods != moderators:
            self.cfg["moderators"] = moderators
            changed.append(f"moderators={moderators!r}")
        return changed

    def _apply_generic_runtime_config(self, data: dict) -> list[str]:
        """Apply every remaining JSON option that is safe as a live cfg value.

        Most PoolRotater helpers read ``self.cfg`` at the moment they act. Once
        constructor-owned settings and settings with dedicated side effects are
        removed, copying the remaining effective JSON values is the correct
        hot-reload behavior and avoids maintaining another fragile whitelist.
        """
        changed: list[str] = []
        for key, value in data.items():
            if str(key).startswith("_"):
                continue
            if key in self._GENERIC_CONFIG_SKIP_KEYS:
                continue
            if self.cfg.get(key) != value:
                self.cfg[key] = value
                changed.append(f"{key}={value!r}")
        return changed

    def _apply_live_config_from_disk(self, data: dict) -> list[str]:
        """Apply safe operational settings from an externally edited room JSON.

        Constructor/shared-owner settings are detected separately and reported
        as restart-required. Runtime-safe settings are applied here, including
        source database rebuilds and Discord routing/view values that are read
        dynamically from the shared config dictionaries.
        """
        if data.get("ruleset_id", self.cfg.get("ruleset_id", 0)) != self.cfg.get("ruleset_id", 0):
            log.warning("Ruleset identity changed; restart supervisor before applying this room config")
            return []
        if self.cfg.get("ruleset_id", 0) == 3:
            data = dict(data)
            data.update(auto_difficulty=False, loved_mode=False, stream_enabled=False)
        # ``ai_config_reload_seconds`` was the old AI-only watcher setting.
        # Let existing configs keep controlling the new unified watcher until
        # they opt into the clearer ``config_reload_seconds`` name.
        if (
            "config_reload_seconds" not in data
            and "ai_config_reload_seconds" in data
        ):
            data = dict(data)
            data["config_reload_seconds"] = data["ai_config_reload_seconds"]
        if (
            "lazer_join_delay_seconds" not in data
            and "lazer_rejoin_delay_seconds" in data
        ):
            data = dict(data)
            data["lazer_join_delay_seconds"] = data["lazer_rejoin_delay_seconds"]

        changed: list[str] = []

        # Source DB / Loved-vs-ranked changes require rebuilding MapPool, but
        # not the referee/API/Discord transports. Build-and-swap atomically.
        changed.extend(self._hot_rebuild_pool_source(data))

        if 'default_response_language' in data:
            from .languages import LANGUAGES
            value = data['default_response_language']
            if isinstance(value, str) and value in LANGUAGES:
                if value != self.cfg.get('default_response_language'):
                    self.cfg['default_response_language'] = value
                    changed.append(f'default_response_language={value!r}')
            else:
                log.warning('ignored invalid room response language')

        # Keep the JSON startup band separate from the AutoDiff runtime pool.
        # In AutoDiff this remains the empty-reset baseline. In fixed/manual
        # difficulty it is also the actual live target and can be applied now.
        if "sr_min" in data and "sr_max" in data:
            try:
                lo = self._external_float(data["sr_min"], 0.0, 20.0)
                hi = self._external_float(data["sr_max"], 0.0, 20.0)
                if lo >= hi:
                    raise ValueError("sr_min must be below sr_max")
                if (self.cfg.get("sr_min"), self.cfg.get("sr_max")) != (lo, hi):
                    self.cfg.update(sr_min=lo, sr_max=hi)
                    changed.append(f"empty_reset_range={lo:g}-{hi:g}")
                    self._empty_reset_done = False
                    if self.manual_range or not self.cfg.get("auto_difficulty", True):
                        count = self.pool.set_range(lo, hi)
                        if count > 0:
                            self.update_room_name_for_range()
                            if self.state in (IDLE, SCORING):
                                self.perform_soft_restart(announce=False)
                            else:
                                self._pending_soft_restart = True
            except (ValueError, TypeError) as exc:
                log.warning("ignored invalid JSON baseline range: %s", exc)

        if "auto_difficulty" in data:
            value = data["auto_difficulty"]
            if not isinstance(value, bool):
                log.warning(
                    "ignored invalid hot-reloaded auto_difficulty=%r: must be true/false",
                    value,
                )
            elif self.cfg.get("auto_difficulty", True) != value:
                self.cfg["auto_difficulty"] = value
                self.manual_range = not value
                changed.append(f"auto_difficulty={value!r}")
                if value:
                    self._auto_retune_pending = True
                else:
                    try:
                        lo = float(self.cfg.get("sr_min", self.pool.sr_min))
                        hi = float(self.cfg.get("sr_max", self.pool.sr_max))
                        if self.pool.set_range(lo, hi) > 0:
                            self.update_room_name_for_range()
                            if self.state in (IDLE, SCORING):
                                self.perform_soft_restart(announce=False)
                            else:
                                self._pending_soft_restart = True
                    except Exception as exc:
                        log.warning(
                            "could not apply fixed JSON SR range after disabling AutoDiff: %s",
                            exc,
                        )
                # The difficulty-mode phrase is part of the dynamic title, so
                # a JSON toggle should behave exactly like .auto/.range. This
                # safely defers when the room is currently starting/playing.
                self.update_room_name_for_range()

        if "command_prefix" in data:
            prefix = str(data["command_prefix"] or "").strip()
            if not prefix or len(prefix) > 8 or any(ch.isspace() for ch in prefix):
                log.warning(
                    "ignored invalid hot-reloaded command_prefix=%r: use 1-8 non-space characters",
                    data["command_prefix"],
                )
            elif self.prefix != prefix:
                self.prefix = prefix
                self.cfg["command_prefix"] = prefix
                changed.append(f"command_prefix={prefix!r}")

        bool_keys = (
            "user_commands_enabled",
            "bot_chat_silenced",
            "voteskip_enabled",
            "map_pp_display_enabled",
            "map_pp_auto_announce_enabled",
            "lazer_rejoin_on_hard_restart",
            "lazer_coordinator_enabled",
            "lazer_chat_patrol_enabled",
            "adaptive_chat_polling",
        )
        for key in bool_keys:
            if key not in data:
                continue
            value = data[key]
            if not isinstance(value, bool):
                log.warning("ignored invalid hot-reloaded %s=%r: must be true/false", key, value)
                continue
            if self.cfg.get(key) != value:
                self.cfg[key] = value
                changed.append(f"{key}={value!r}")
                if key == "user_commands_enabled" and not value:
                    self._clear_skip_vote("public commands disabled by config reload")
                    self._clear_replay_vote("public commands disabled by config reload")
                    self._clear_difficulty_vote("public commands disabled by config reload")

        int_keys = {
            "countdown_seconds": (1, 120),
            "auto_start_seconds": (0, 600),
            "min_players": (1, 32),
            "queue_ahead": (0, 8),
            "rating_window_seconds": (10, 600),
            "info_interval_seconds": (60, 7200),
            "difficulty_vote_max_steps": (0, 20),
            "hard_restart_warning_maps": (0, 20),
            "requests_per_minute": (1, 60),
            "requests_burst_capacity": (1, 90),
            "lazer_visit_max_actions": (1, 50),
            "command_spam_burst": (1, 20),
            "active_chat_countdown_min_messages": (2, 30),
            "active_chat_countdown_min_speakers": (1, 20),
        }
        changed_cap = False
        changed_queue = False
        changed_min_players = False
        changed_api_limit = False
        for key, (minimum, maximum) in int_keys.items():
            if key not in data:
                continue
            try:
                value = self._external_int(data[key], minimum, maximum)
            except (TypeError, ValueError) as e:
                log.warning("ignored invalid hot-reloaded %s=%r: %s", key, data[key], e)
                continue
            if self.cfg.get(key) != value:
                self.cfg[key] = value
                changed.append(f"{key}={value!r}")
                changed_cap = changed_cap or key == "difficulty_vote_max_steps"
                changed_queue = changed_queue or key == "queue_ahead"
                changed_min_players = changed_min_players or key == "min_players"
                changed_api_limit = changed_api_limit or key in {
                    "requests_per_minute", "requests_burst_capacity"
                }

        float_keys = {
            "auto_sr_bias": (-1.50, 1.50),
            "auto_sr_half_width": (0.10, 1.50),
            "difficulty_vote_ratio": (0.25, 1.0),
            "difficulty_vote_step": (0.05, 0.50),
            "score_delay_seconds": (0.0, 30.0),
            "config_reload_seconds": (0.5, 60.0),
            "discord_invite_reminder_seconds": (0.0, 604800.0),
            "hard_restart_interval_seconds": (0.0, 2_592_000.0),
            "hard_restart_close_delay_seconds": (0.0, 30.0),
            "lazer_rejoin_delay_seconds": (0.0, 30.0),
            "lazer_join_delay_seconds": (0.0, 30.0),
            "lazer_leave_delay_seconds": (0.0, 10.0),
            "lazer_leave_confirm_delay_seconds": (0.0, 5.0),
            "lazer_minimum_room_dwell_seconds": (0.0, 60.0),
            "lazer_visit_max_seconds": (1.0, 60.0),
            "lazer_chat_patrol_interval_seconds": (0.5, 60.0),
            "lazer_switch_timeout_seconds": (5.0, 300.0),
            "lazer_queue_poll_seconds": (0.05, 1.0),
            "api_usage_log_seconds": (5.0, 3600.0),
            "chat_poll_seconds": (0.5, 5.0),
            "chat_poll_playing_seconds": (0.5, 60.0),
            "chat_websocket_reconcile_seconds": (30.0, 3600.0),
            "playlist_audit_seconds": (3.0, 3600.0),
            "command_spam_window_seconds": (1.0, 120.0),
            "command_spam_block_seconds": (1.0, 600.0),
            "discord_status_rename_spacing_seconds": (0.5, 60.0),
            "auto_empty_reset_seconds": (0.0, 86400.0),
            "player_profile_cache_seconds": (0.0, 2_592_000.0),
            "download_availability_cache_days": (0.0, 365.0),
            "active_chat_countdown_extension_seconds": (0.0, 30.0),
            "active_chat_countdown_window_seconds": (10.0, 300.0),
            "start_wait_seconds": (1.0, 60.0),
            "freedom_dive_cooldown_seconds": (60.0, 604800.0),
        }
        changed_auto_tuning = False
        changed_ratio = False
        changed_step = False
        changed_hard_restart_interval = False
        for key, (minimum, maximum) in float_keys.items():
            if key not in data:
                continue
            try:
                value = self._external_float(data[key], minimum, maximum)
            except (TypeError, ValueError) as e:
                log.warning("ignored invalid hot-reloaded %s=%r: %s", key, data[key], e)
                continue
            if self.cfg.get(key) != value:
                self.cfg[key] = value
                changed.append(f"{key}={value!r}")
                changed_auto_tuning = changed_auto_tuning or key in {
                    "auto_sr_bias", "auto_sr_half_width"
                }
                changed_ratio = changed_ratio or key == "difficulty_vote_ratio"
                changed_step = changed_step or key == "difficulty_vote_step"
                changed_hard_restart_interval = (
                    changed_hard_restart_interval
                    or key == "hard_restart_interval_seconds"
                )

        changed.extend(self._apply_hot_pool_filters(data))
        changed.extend(self._apply_hot_playlist_rules(data))
        changed.extend(self._apply_hot_staff_lists(data))

        title_dirty = False
        if "room_name" in data:
            title = data["room_name"]
            if not isinstance(title, str) or not title.strip() or len(title) > 100:
                log.warning("ignored invalid room_name: use 1-100 characters")
            elif title.strip() != self._room_name_template:
                self._room_name_template = title.strip()
                self.cfg["room_name"] = title.strip()
                changed.append("room_name")
                title_dirty = True

        if "version" in data:
            raw_version = data["version"]
            if not isinstance(raw_version, str):
                log.warning(
                    "ignored invalid hot-reloaded version=%r: use a string such as "
                    "\"v6.4.8\"",
                    raw_version,
                )
            else:
                version = raw_version.strip()
                if len(version) > 32:
                    log.warning(
                        "ignored invalid hot-reloaded version=%r: use at most 32 characters",
                        raw_version,
                    )
                elif str(self.cfg.get("version", "")) != version:
                    self.cfg["version"] = version
                    changed.append(f"version={version!r}")
                    title_dirty = True

        if title_dirty:
            self.update_room_name_for_range()

        fixed_vote_settings_reset = bool(
            (changed_cap or changed_step)
            and self._fixed_difficulty_mode()
            and getattr(self, "_difficulty_bias_layers", [])
        )
        if changed_cap or changed_step:
            # Existing preference layers may be beyond a newly lowered cap.
            # Resetting them is clearer and safer than keeping an invalid bias.
            self._clear_community_difficulty(
                "difficulty-vote cap or step changed by config reload"
            )
            changed_auto_tuning = True
            if fixed_vote_settings_reset:
                self._fixed_difficulty_dirty = True
                self._apply_fixed_community_range(
                    "difficulty-vote settings changed by config reload"
                )
                if self.players:
                    self._fixed_range_rebuild_pending = True
        elif changed_ratio:
            self._clear_difficulty_vote("threshold changed by config reload")

        if changed_auto_tuning and self.cfg.get("auto_difficulty", True) and not self.manual_range:
            self._auto_retune_pending = True
        if changed_min_players:
            self.cancel_start_if_too_few_players()
        if changed_queue and self.state in (IDLE, SCORING):
            self.reconcile_playlist(
                reason="config reload queue setting", fetch_remote=True,
                ensure_depth=True, trim_extras=True,
            )
        if changed_hard_restart_interval:
            self._reschedule_hard_restart_cycle_from_config()
        if changed_api_limit:
            configure = getattr(self.api, "configure_rate_limit", None)
            if configure is not None:
                configure(
                    int(self.cfg.get("requests_per_minute", 60)),
                    int(self.cfg.get("requests_burst_capacity", 90)),
                )
        if any(item.startswith("api_usage_log_seconds=") for item in changed):
            limiter = getattr(self.api, "limiter", None)
            if limiter is not None:
                limiter.usage_log_seconds = float(
                    self.cfg.get("api_usage_log_seconds", 60.0)
                )
        if any(item.startswith("player_profile_cache_seconds=") for item in changed):
            configure_cache = getattr(self.api, "configure_profile_cache", None)
            if callable(configure_cache):
                configure_cache(float(
                    self.cfg.get("player_profile_cache_seconds", 21600.0)
                ))

        # Every remaining effective JSON option is copied live unless it is a
        # constructor/shared-owner setting. This is the catch-all that turns
        # hot reload from a brittle whitelist into the default behavior.
        changed.extend(self._apply_generic_runtime_config(data))
        return changed

    def _reload_config_from_disk(self) -> None:
        """Hot-reload room JSON plus its higher-priority universal overlay."""
        now = time.time()
        try:
            interval = self._external_float(
                self.cfg.get("config_reload_seconds", 2.0), 0.5, 60.0
            )
        except (TypeError, ValueError):
            interval = 2.0
        if now - self._last_config_check < interval:
            return
        self._last_config_check = now

        try:
            room_mtime = self._config_path.stat().st_mtime_ns
        except OSError:
            return
        universal_path = getattr(
            self,
            "_universal_config_path",
            self._config_path.parent / "universal.json",
        )
        try:
            universal_mtime = universal_path.stat().st_mtime_ns
        except OSError:
            universal_mtime = None

        if (
            self._config_mtime_ns == room_mtime
            and getattr(self, "_universal_config_mtime_ns", None) == universal_mtime
        ):
            return

        try:
            if self.cfg.get('_host_root'):
                from poolrotater_host.layout import read_json
                from poolrotater_host.runtime_config import render_room, validate_room_data
                room = render_room(Path(self.cfg['_host_root']), self.cfg['_instance_name'],
                    account_id=self.cfg.get('_authenticated_user_id'),
                    catalog_reference=self.cfg.get('_host_catalog_reference'),
                    catalog_set=self.cfg.get('_host_catalog_set'),
                    private_session=self.cfg.get('_host_private_pilot_session'))
                if self._config_path != Path(room['config_path']):
                    raise ValueError('unexpected host-managed configuration path')
                room_data = read_json(self._config_path)
                validate_room_data(room_data, room['settings'])
                if read_json(universal_path) != {}:
                    raise ValueError('shared room overrides are unavailable in the offline host')
            else:
                room_data = json.loads(self._config_path.read_text(encoding="utf-8"))
            if not isinstance(room_data, dict):
                raise ValueError("room config root is not an object")
            universal_data: dict = {}
            if universal_mtime is not None:
                universal_data = json.loads(
                    universal_path.read_text(encoding="utf-8")
                )
                if not isinstance(universal_data, dict):
                    raise ValueError("universal config root is not an object")
                universal_data = {
                    key: value
                    for key, value in universal_data.items()
                    if not str(key).startswith("_")
                }
        except Exception as e:
            log.warning("config hot-reload skipped: %s", e)
            return

        # Rebuild the effective view so deleting a universal override restores
        # the room value (or the program default) on the next reload.
        requested_data = dict(self.cfg.get("_default_config", {}))
        requested_data.update({
            key: value for key, value in room_data.items()
            if not str(key).startswith("_")
        })
        try:
            requested_data = merge_room_config(self.cfg.get("_default_config", {}), room_data, universal_data)
        except (ValueError, TypeError) as exc:
            log.warning("config hot-reload rejected: %s", exc)
            return
        # Notice constructor/shared-owner edits before supervisor safety
        # overrides mask them. This makes restart-required JSON changes visible
        # in logs instead of silently disappearing.
        self._note_restart_required_config(requested_data)
        if requested_data.get("ruleset_id", 0) != self.cfg.get("ruleset_id", 0):
            log.warning("Ruleset change requires restart; keeping all live room settings")
            return
        data = dict(requested_data)
        # Supervisor-owned transport settings cannot be switched back to GUI
        # automation or separate account budgets by an old universal.json.
        data.update(self.cfg.get("_supervisor_overrides", {}))
        self._config_mtime_ns = room_mtime
        self._universal_config_mtime_ns = universal_mtime
        self.cfg["_universal_config_keys"] = sorted(universal_data)

        changed = self._apply_live_config_from_disk(data)
        changed.extend(self._apply_ai_config_from_disk(data))
        if changed:
            log.info("hot-reloaded effective config: %s", ", ".join(changed))

    def _ai_recent_chat_context(self) -> str:
        count = max(0, min(30, int(
            self.cfg.get("ai_recent_chat_messages", 10) or 0
        )))
        if count <= 0:
            return ""
        rows = list(self._recent_human_chat)[-count:]
        return "\n".join(f"{name}: {content}" for _at, name, content in rows)

    def _build_ai_live_context(self, user_id: int) -> str:
        lines = [
            f"Ruleset: {'native ' + self._mania_key_label() + ' mania (fixed difficulty; no standard calibration)' if self.cfg.get('ruleset_id', 0) == 3 else 'osu!standard'}",
            f"Room id: {self.room_id or 'unknown'}",
            f"State: {self.state}",
        ]
        lines.append(f"Pool source mode: {self.pool.source_mode_label()}")
        active = self.gameplay_player_ids()
        lines.append(
            f"Gameplay players: {len(active)}; ready: {len(self.ready & active)}"
        )

        if self.current_map is not None:
            m = self.current_map
            lines.extend([
                f"Current map: {m.label}",
                f"Current beatmap id: {m.beatmap_id}",
                f"Current map length: {m.hit_length}s",
            ])
            is_farm = bool(getattr(m, "is_farm", False))
            is_stream = bool(getattr(m, "is_stream", False))
            is_loved = bool(getattr(m, "is_loved", False))
            if is_loved:
                lines.append("Current map category: Loved")
            elif is_farm and is_stream:
                lines.append("Current map category: Farm + Stream")
            elif is_stream:
                lines.append("Current map category: Stream")
            elif is_farm:
                lines.append("Current map category: Farm")
            if is_farm:
                lines.append(f"Current map Farm Score: {m.farm_score:.3f}")
            if is_stream:
                lines.append(
                    f"Current map Stream Score: {getattr(m, 'stream_score', 0.0):.3f}"
                )
                stream_bpm = float(getattr(m, "stream_bpm", 0.0) or 0.0)
                if stream_bpm > 0:
                    lines.append(f"Current map stream BPM: {stream_bpm:.0f}")
        else:
            lines.append("Current map: unresolved")

        if self.manual_range:
            lines.append(
                f"Difficulty: manual {self.pool.sr_min:.2f}-{self.pool.sr_max:.2f}*"
            )
        elif self.cfg.get("auto_difficulty", True):
            pps, dropped, known_count = self.auto_difficulty_pp_values(active)
            if pps:
                med = float(median(pps))
                half = float(self.cfg.get("auto_sr_half_width", AUTO_SR_HALF_WIDTH))
                bias = self._effective_auto_bias()
                target = target_sr_for_pp(med, half_width=half, bias=bias)
                line = (
                    f"Difficulty: AutoDiff active {self.pool.sr_min:.2f}-"
                    f"{self.pool.sr_max:.2f}*, target {target:.2f}*, "
                    f"median {med:.0f}pp from {len(pps)}/{known_count} PP-known"
                )
                if dropped:
                    dropped_name = str(
                        (self.players.get(dropped[0]) or {}).get("name") or dropped[0]
                    )
                    line += f"; lowest excluded: {dropped_name} ({dropped[1]:.0f}pp)"
                lines.append(line)
            else:
                lines.append(
                    f"Difficulty: AutoDiff active {self.pool.sr_min:.2f}-"
                    f"{self.pool.sr_max:.2f}*, no usable player PP yet"
                )
        else:
            lines.append(
                f"Difficulty: auto off {self.pool.sr_min:.2f}-{self.pool.sr_max:.2f}*"
            )

        base_length = int(self.cfg.get("max_length", self.pool.max_length))
        active_length = self.pool.max_length_label()
        base_length_text = (
            "unlimited" if getattr(self.pool, "loved_mode", False)
            and base_length <= 0 else f"{base_length}s"
        )
        lines.append(
            f"Pool: {len(self.pool.maps)} candidates; active max length "
            f"{active_length}; configured base {base_length_text}; "
            f"minimum passes {self.pool.min_passcount}; map-age limit "
            f"{self.pool.max_map_age_label()}"
        )
        if getattr(self.pool, 'sparse_passcount_enabled', False) and not getattr(self.pool, 'mania_mode', False) and not getattr(self.pool, 'loved_mode', False):
            lines.append(f"Sparse-pool pass fallback: target {self.pool.sparse_passcount_min_maps}; floor {self.pool.sparse_passcount_floor}; current minimum {self.pool.effective_min_passcount()}; original {self.pool.min_passcount} preferred")
        if self.cfg.get("ruleset_id", 0) == 3:
            lines.append(f"Mania fixed-range community overlay: {self._community_bias_overlay():+.2f}*; standard AutoDiff does not apply")
        else:
            lines.append(
                f"AutoDiff baseline bias: {float(self.cfg.get('auto_sr_bias', 0.30)):+.2f}*; "
                f"community overlay: {self._community_bias_overlay():+.2f}*"
            )

        if self.queued:
            queue_text = " | ".join(
                f"{m.artist} - {m.title} [{m.version}] ({m.sr:.2f}*)"
                for _item, m in self.queued[:4]
            )
            lines.append(f"Queued future maps: {queue_text}")
        else:
            lines.append("Queued future maps: none")

        if self.last_played_map is not None:
            try:
                good, bad = self.ratings.tally(self.last_played_map.beatmap_id)
                rating_text = f"; real public tally {good} good / {bad} bad"
            except Exception:
                rating_text = ""
            lines.append(f"Last completed map: {self.last_played_map.label}{rating_text}")

        requester = self.players.get(int(user_id)) or {}
        requester_name = str(requester.get("name") or user_id)
        requester_pp = requester.get("pp")
        if requester_pp not in (None, 0, 0.0):
            lines.append(f"Requester: {requester_name}, {float(requester_pp):.0f}pp")
        else:
            lines.append(f"Requester: {requester_name}")
        return "\n".join(lines)

    @staticmethod
    def _clean_ai_text(text: str) -> str:
        # One physical osu! chat line per generated segment.  Remove control
        # characters/newlines and collapse whitespace before prefixing.
        clean = "".join(
            ch if (ch >= " " and ch != "\x7f") else " " for ch in str(text)
        )
        clean = re.sub(r"\s+", " ", clean).strip()
        # The model is instructed not to add the prefix, but strip one anyway so
        # Python remains the sole owner of the visible trust marker.
        while clean.lower().startswith("~ai *"):
            clean = clean[5:].lstrip()
        return clean

    def _queue_ai_output(self, text: str) -> None:
        clean = self._clean_ai_text(text)
        if not clean:
            return

        # Keep the visible answer short even though GPT-5 may need a larger
        # completion-token budget for reasoning before it emits visible text.
        total_limit = max(80, min(1200, int(
            self.cfg.get("ai_max_output_chars", 200) or 200
        )))
        if len(clean) > total_limit:
            clean = clean[:max(1, total_limit - 1)].rstrip() + "…"

        # Use several short osu! chat messages instead of one large message that
        # wraps across multiple visual lines in the client.  The prefix is added
        # later by _drain_ai_results(), so body_limit applies to text only.
        body_limit = 145
        remaining = clean

        while remaining:
            if len(remaining) <= body_limit:
                part = remaining
                remaining = ""
            else:
                # Prefer sentence/clause boundaries near the target length.
                # Avoid tiny first fragments by only considering boundaries in
                # the latter half of the candidate chunk.
                search_start = max(0, body_limit // 2)
                chunk = remaining[:body_limit + 1]

                cut = -1
                for marker in (". ", "? ", "! ", "; ", ", "):
                    pos = chunk.rfind(marker, search_start)
                    if pos >= 0:
                        candidate = pos + len(marker) - 1
                        if candidate > cut:
                            cut = candidate

                # If there is no useful punctuation boundary, split on the last
                # whitespace that still keeps the message within the target.
                if cut < search_start:
                    cut = remaining.rfind(" ", search_start, body_limit + 1)

                # Last resort for a long unbroken token/URL.
                if cut < search_start:
                    cut = body_limit

                part = remaining[:cut].rstrip()
                remaining = remaining[cut:].lstrip()

            if part:
                self._ai_outbox.append(part)

    def _send_ai_status(self, text: str) -> None:
        self._queue_ai_output(text)

    def _drain_ai_results(self) -> None:
        # Turning AI off is immediate from the room's perspective: already
        # completed/queued text is discarded rather than leaking out afterward.
        if not self.cfg.get("ai_enabled", True):
            self._ai_outbox.clear()
            while True:
                try:
                    self.ai.get_result_nowait()
                except queue.Empty:
                    break
            return

        # Move completed worker results into a main-thread chat outbox.
        while True:
            try:
                result = self.ai.get_result_nowait()
            except queue.Empty:
                break
            if result.error:
                log.warning(
                    "AI response suppressed after provider error for %s:%s: %s",
                    result.username, result.user_id, result.error,
                )
                self._queue_ai_output("AI is temporarily unavailable. Try again shortly.")
            else:
                log.info(
                    "AI RESPONSE <%s:%s> %s",
                    result.username,
                    result.user_id,
                    self._clean_ai_text(result.text)[:500],
                )
                self._queue_ai_output(result.text)

        if not self._ai_outbox or not self.channel_id:
            return
        spacing = max(1.5, float(
            self.cfg.get("ai_global_cooldown_seconds", 3.0) or 3.0
        ))
        if time.time() - self._last_ai_sent < spacing:
            return
        body = self._ai_outbox.popleft()
        # This concatenation is the only code path that emits model text.  The
        # model never controls the prefix, even if it tries to output a command.
        self.say(self._AI_PREFIX + body)
        self._last_ai_sent = time.time()

    def _handle_ai_command(self, user_id: int, text: str) -> None:
        command_token = f"{self.prefix}ai"
        message = text[len(command_token):].strip()
        if len(message) >= 2 and message[0] == message[-1] and message[0] in "\"'":
            message = message[1:-1].strip()
        if not message:
            self.say(f"Usage: {self.prefix}ai <message>")
            return
        if not self.cfg.get('ai_enabled', True) or getattr(self, 'ai', None) is None:
            now = time.time()
            if now - getattr(self, '_last_ai_unavailable_notice', 0.0) >= 15.0:
                # Disabled AI drains its model outbox without publishing it.
                # This is a normal requested command reply, not model output.
                self.say('AI is unavailable right now.')
                self._last_ai_unavailable_notice = now
            return
        max_prompt = max(100, min(2000, int(
            self.cfg.get("ai_max_prompt_chars", 700) or 700
        )))
        if len(message) > max_prompt:
            message = message[:max_prompt]

        info = self.players.get(int(user_id)) or {}
        username = str(info.get("name") or self._known_username(user_id) or user_id)
        accepted, reason, retry_after = self.ai.submit(
            user_id=int(user_id),
            username=username,
            message=message,
            live_context=self._build_ai_live_context(int(user_id)),
            recent_chat=self._ai_recent_chat_context(),
        )
        if accepted:
            log.info("AI request queued from %s:%s: %s", username, user_id, message)
            return
        if reason == "cooldown":
            log.debug(
                "AI request cooldown from %s:%s (%.1fs remaining)",
                username, user_id, retry_after,
            )
            return
        if reason == "busy":
            log.debug("AI request queue full from %s:%s", username, user_id)
            return
        # Disabled/not-configured should not expose API configuration details to
        # ordinary players. Rate-limit the hardcoded status itself.
        now = time.time()
        if now - self._last_ai_unavailable_notice >= 15.0:
            self._send_ai_status("AI is unavailable right now.")
            self._last_ai_unavailable_notice = now

    # ------------------------------------------------------------ chat

    def _join_room_chat(self) -> bool:
        """Ensure this OAuth user is a member of the multiplayer chat channel."""
        if not self.channel_id:
            return False
        user_id = self.cfg.get("_authenticated_user_id")
        if not user_id:
            log.warning("cannot join room chat: authenticated user id is unknown")
            return False
        joined = self.api.chat_join(self.channel_id, int(user_id))
        if joined is None:
            return False
        log.info("joined multiplayer chat channel %s", self.channel_id)
        return True

    def say(
        self,
        msg: str,
        *,
        automatic: bool = False,
        lobby_only: bool = False,
    ) -> None:
        """Send one room-chat line.

        automatic=True marks unsolicited/noisy chatter that may be suppressed
        with `.silence on`. Functional command replies and vote-skip status stay
        available so silencing the bot never makes moderation controls unusable.
        """
        if isinstance(msg, str) and not msg.strip():
            return
        if automatic and self.cfg.get("bot_chat_silenced", False):
            log.debug("bot chat silenced; suppressed automatic message: %s", msg[:120])
            return
        try:
            msg = self._localize_command_reply(msg, automatic=automatic, lobby_only=lobby_only)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            # Language UX must never break a control or remove English.
            log.warning("language reply unavailable; using English")
        text = msg[:400]
        if not text.strip():
            return
        discord_reply_channel = getattr(
            self, "_discord_command_reply_channel_id", None
        )
        if discord_reply_channel and not lobby_only and not automatic:
            control = getattr(self, "discord_control", None)
            if control is not None and control.send_to_channel(
                text, channel_id=int(discord_reply_channel)
            ):
                return
            # Do not silently leak a Discord-only command response into osu!
            # if Discord went away mid-command.
            log.warning("could not send Discord command reply; osu! reply suppressed")
            return
        if not self.channel_id:
            return

        entry = {'room_id': self.room_id, 'channel_id': self.channel_id,
                 'text': text, 'automatic': automatic, 'lobby_only': lobby_only,
                 'discord_reply_channel': discord_reply_channel,
                 'expires_at': time.monotonic() + 120.0}
        # Rate spacing belongs to the bounded outgoing queue. Never sleep the
        # room thread while processing a batch of commands or join messages.
        if (getattr(self, '_pending_room_chat', None)
                or time.time() - getattr(self, '_last_sent', 0.0) < 1.5):
            self._queue_room_chat(entry)
            return
        try:
            self._deliver_room_chat(text, automatic=automatic, lobby_only=lobby_only,
                                    discord_reply_channel=discord_reply_channel)
        except ApiAdmissionDeferred:
            # This typed refusal proves that no message was dispatched.
            self._queue_room_chat(entry)
            self._room_chat_retry_at = time.monotonic() + 1.0
        except ApiChatDeliveryUncertain:
            # Losing a cosmetic/command reply is preferable to aborting all
            # occupied rooms. Its dispatch outcome remains uncertain in the
            # shared provider ledger; the message is never queued or replayed.
            log.warning('room chat delivery uncertain; message will not be resent')

    def _queue_room_chat(self, entry):
        pending = getattr(self, '_pending_room_chat', None)
        if pending is None:
            pending = self._pending_room_chat = deque()
        now = time.monotonic()
        kept = [item for item in pending if item['expires_at'] > now
                and item['room_id'] == self.room_id and item['channel_id'] == self.channel_id]
        pending.clear(); pending.extend(kept)
        if len(pending) >= 32:
            disposable = next((item for item in pending if item['automatic']), None)
            if disposable is not None:
                pending.remove(disposable)
            elif entry['automatic']:
                log.warning('automatic room chat omitted while delivery queue is full')
                return
            else:
                pending.popleft()
                log.warning('oldest room chat expired while delivery queue is full')
        if entry['automatic']:
            pending.append(entry)
        else:
            # Replies retain their order, but don't wait behind an unsolicited
            # welcome/reminder backlog. All expiry/room/uncertain-send guards stay.
            position = next((i for i, item in enumerate(pending) if item['automatic']), len(pending))
            pending.insert(position, entry)

    def _flush_room_chat(self):
        """Bounded, ordered delivery without blocking gameplay on pacing waits.

        Only positively undispatched messages enter this queue. Expiry, changed
        room/channel and newly silenced automatic messages are discarded.
        """
        pending = getattr(self, '_pending_room_chat', None)
        if not pending:
            return
        now = time.monotonic()
        while pending and (pending[0]['expires_at'] <= now
                or pending[0]['room_id'] != self.room_id
                or pending[0]['channel_id'] != self.channel_id
                or pending[0]['automatic'] and self.cfg.get('bot_chat_silenced', False)):
            pending.popleft()
        if not pending or now < getattr(self, '_room_chat_retry_at', 0.0):
            return
        # Do not sleep the loop just to satisfy the ordinary inter-message gap.
        if time.time() - getattr(self, '_last_sent', 0.0) < 1.5:
            return
        entry = pending[0]
        try:
            self._deliver_room_chat(entry['text'], automatic=entry['automatic'],
                lobby_only=entry['lobby_only'], discord_reply_channel=entry['discord_reply_channel'])
        except ApiAdmissionDeferred:
            failures = min(5, getattr(self, '_room_chat_deferred_count', 0) + 1)
            self._room_chat_deferred_count = failures
            # This typed refusal is local and positively undispatched. The
            # provider gate still owns spacing and Retry-After; a second
            # exponential client delay only postpones an otherwise ready reply.
            self._room_chat_retry_at = now + 1.0
            return
        except ApiChatDeliveryUncertain:
            log.warning('queued room chat delivery uncertain; message will not be resent')
        pending.popleft()
        self._room_chat_deferred_count = 0
        self._room_chat_retry_at = 0.0

    def _deliver_room_chat(self, text, *, automatic, lobby_only, discord_reply_channel):
        """One delivery; dispatched uncertain sends keep their existing fence."""
        def stopping():
            return getattr(self, '_shutdown_started', False) or not getattr(self, 'running', True)
        if stopping():
            return
        coordinator = getattr(self, "lazer_coordinator", None)

        if time.time() - self._last_sent < 1.5:
            raise ApiAdmissionDeferred('room_chat_spacing')
        if stopping():
            return

        def prepare_chat_send(*, force: bool = False) -> bool:
            if coordinator is None:
                return False
            return coordinator.ensure_room(
                int(self.room_id or 0),
                reason="chat-send recovery" if force else "outgoing lobby chat",
                force=force,
            )

        # Run room ownership after the API limiter wait and immediately before
        # HTTP. A long API queue can otherwise outlast the dwell lease and let
        # the other room move lazer before this request is sent.
        try:
            sent = self.api.chat_send(
                self.channel_id,
                text,
                before_request=prepare_chat_send,
            )
        except Exception:
            if not stopping():
                raise
            # A clean stop can retire shared API authority while a late help
            # reply waits in RPC. Ownership/uncertainty remain the parent's
            # responsibility; don't label that cancelled reply a worker crash.
            log.debug('room chat delivery interrupted during requested shutdown', exc_info=True)
            return

        # If the coordinator database was stale because lazer was moved by
        # hand, force a leave/rejoin sequence before the one recovery attempt.
        if sent is None and not self.cfg.get("_headless_supervised", False):
            if coordinator is not None and coordinator.enabled:
                sent = self.api.chat_send(
                    self.channel_id,
                    text,
                    before_request=lambda: prepare_chat_send(force=True),
                )
            elif self._join_room_chat():
                sent = self.api.chat_send(self.channel_id, text)

        if sent is None and self.cfg.get("_headless_supervised", False):
            log.warning("chat delivery unconfirmed in room %s; not replaying an ambiguous send", self.room_id)
            if lobby_only and discord_reply_channel:
                self.discord_control.send_to_channel(
                    "Lobby message delivery could not be confirmed. Check the lobby before retrying.",
                    channel_id=int(discord_reply_channel),
                )

        mid = (sent or {}).get("message_id") or (sent or {}).get("id")
        if mid:
            log.info("CHAT BOT %s", text.replace("\r", " ").replace("\n", " "))
            self._own_messages.add(int(mid))
            if len(self._own_messages) > 200:
                self._own_messages = set(sorted(self._own_messages)[-100:])
        self._last_sent = time.time()
        if sent is not None:
            if not getattr(self, "_sending_feedback_hint", False):
                self._last_non_hint_sent_at = time.time()
            self._notify_discord_lobby_event("BOT", text, automatic=automatic)

    @staticmethod
    def _chat_message_time(message: dict, fallback: float | None = None) -> float:
        """Best-effort epoch timestamp for an osu! chat message.

        Chat is polled every few seconds. Using the message's own timestamp for
        vote-skip eligibility means a vote typed inside the visible eight-second
        window still counts even if the next API poll processes it just after the
        local deadline. Older/alternate payloads fall back to receipt time.
        """
        fallback = time.time() if fallback is None else float(fallback)
        if not isinstance(message, dict):
            return fallback

        raw = None
        for key in ("timestamp", "created_at", "sent_at"):
            if message.get(key) not in (None, ""):
                raw = message.get(key)
                break

        if isinstance(raw, (int, float)):
            try:
                return float(raw)
            except (TypeError, ValueError):
                return fallback

        if isinstance(raw, str):
            text = raw.strip()
            if text:
                try:
                    # osu! API timestamps are UTC/offset-aware ISO-8601.
                    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    return parsed.timestamp()
                except (TypeError, ValueError, OverflowError):
                    pass

        return fallback

    def _chat_patrol_enabled(self) -> bool:
        return bool(
            self.cfg.get("lazer_chat_patrol_enabled", False)
            and getattr(self, "lazer_coordinator", None) is not None
            and self.lazer_coordinator.enabled
        )

    def _chat_websocket_enabled(self) -> bool:
        return bool(
            self.cfg.get("chat_websocket_enabled", True)
            and self.chat_websocket is not None
            and getattr(self.chat_websocket, "enabled", False)
        )

    def _attach_chat_websocket(self) -> None:
        """Route the current room channel into this lobby's main thread."""
        self._detach_chat_websocket()
        if not self._chat_websocket_enabled() or not self.channel_id:
            return
        channel_id = int(self.channel_id)
        self.chat_websocket.subscribe(channel_id, self._chat_websocket_inbox)
        self._chat_websocket_channel_id = channel_id
        self._chat_websocket_needs_reconcile = True
        self._last_chat_websocket_reconcile = 0.0

    def _detach_chat_websocket(self) -> None:
        channel_id = self._chat_websocket_channel_id
        if channel_id is not None and self.chat_websocket is not None:
            try:
                self.chat_websocket.unsubscribe(
                    int(channel_id), self._chat_websocket_inbox
                )
            except Exception:
                log.debug("could not remove real-time chat route", exc_info=True)
        self._chat_websocket_channel_id = None
        self._chat_websocket_needs_reconcile = False
        while True:
            try:
                self._chat_websocket_inbox.get_nowait()
            except queue.Empty:
                break

    def _reconcile_chat_history(self, *, reason: str) -> None:
        """Fetch once after a socket gap (or periodically as a safety net)."""
        if time.monotonic() < getattr(self, "_chat_reconcile_retry_at", 0.0):
            return
        if not self.channel_id:
            return
        room_id = self.room_id
        channel_id = int(self.channel_id)
        completed, messages = self._chat_api_call('chat_messages',
            channel_id,
            since=self.last_message_id or None,
        )
        if not completed or messages is None:
            # A 404/timeout is NOT an empty history. Keep the recovery pending
            # and the cursor unchanged, without hammering an unavailable API.
            self._chat_websocket_needs_reconcile = True
            self._chat_reconcile_retry_at = time.monotonic() + 5.0
            return
        # A queued HTTP request may outlive a hard restart. Never apply an old
        # channel's messages to its replacement room.
        if self.room_id != room_id or self.channel_id != channel_id:
            return
        self._process_chat_messages(messages)
        self._last_chat_websocket_reconcile = time.time()
        self._chat_websocket_needs_reconcile = False
        log.debug("chat history reconciled after %s", reason)

    def _drain_chat_websocket_inbox(self) -> None:
        """Consume socket events only on the lobby state-machine thread."""
        while True:
            try:
                incoming = self._chat_websocket_inbox.get_nowait()
            except queue.Empty:
                return

            generation = int(getattr(incoming, "generation", 0) or 0)
            kind = str(getattr(incoming, "kind", ""))
            event_channel_id = getattr(incoming, "channel_id", None)
            if (
                event_channel_id is not None
                and int(event_channel_id) != int(self.channel_id or 0)
            ):
                # unsubscribe() and a socket callback can cross during a hard
                # restart. Never deliver the destroyed room's final event to
                # the replacement room reusing this inbox.
                continue
            if kind in ("connected", "channel_join"):
                if generation != self._chat_websocket_generation:
                    self._chat_websocket_generation = generation
                self._chat_websocket_needs_reconcile = True
                continue
            if kind == "disconnected":
                continue
            if kind == "channel_part":
                log.debug(
                    "osu! chat channel %s was parted on the notification socket",
                    self.channel_id,
                )
                continue
            if kind != "messages":
                continue

            # The connected marker is enqueued before any messages from that
            # socket generation. Reconcile the outage first, then deduplicate
            # the queued live batch by message_id.
            if self._chat_websocket_needs_reconcile:
                self._reconcile_chat_history(reason="WebSocket connect/reconnect")
                if self._chat_websocket_needs_reconcile:
                    self._chat_websocket_inbox.put_nowait(incoming)
                    return
            self._process_chat_messages(list(getattr(incoming, "messages", ())))

    def _start_chat_patrol(self) -> None:
        if self._chat_patrol_thread and self._chat_patrol_thread.is_alive():
            return
        self._chat_patrol_stop.clear()
        self._chat_patrol_thread = threading.Thread(
            target=self._chat_patrol_loop,
            name=f"poolrotater-chat-patrol-{self.cfg.get('_instance_name', 'room')}",
            daemon=True,
        )
        self._chat_patrol_thread.start()

    def _stop_chat_patrol(self, timeout: float = 2.0) -> None:
        self._chat_patrol_stop.set()
        self._chat_patrol_wake.set()
        thread = self._chat_patrol_thread
        if thread and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout)))

    def _fetch_chat_patrol_batch(self, *, reason: str) -> list[dict]:
        """Acquire this room in lazer, then retrieve its available backlog."""
        if not self.room_id or not self.channel_id:
            return []
        with self._chat_patrol_fetch_lock:
            room_id = int(self.room_id)
            channel_id = int(self.channel_id)
            coordinator = self.lazer_coordinator

            def prepare() -> bool:
                return coordinator.ensure_room(room_id, reason=reason)

            now = time.time()
            if now - self._last_ack > 30:
                completed, _ = self._chat_api_call('chat_ack')
                if completed: self._last_ack = now
            completed, msgs = self._chat_api_call('chat_messages',
                channel_id,
                since=self._chat_patrol_since or None,
                before_request=prepare,
            )
            # A hard restart may replace both ids while this worker was waiting
            # in the cross-process queue. Never feed old-room chat to the new one.
            if self.room_id != room_id or self.channel_id != channel_id:
                return []
            for message in msgs or []:
                try:
                    self._chat_patrol_since = max(
                        self._chat_patrol_since,
                        int(message.get("message_id", 0) or 0),
                    )
                except (TypeError, ValueError):
                    continue
            return list(msgs or [])

    def _chat_patrol_loop(self) -> None:
        """Visit this room frequently without blocking referee processing."""
        while not self._chat_patrol_stop.is_set():
            if self._chat_patrol_enabled() and self.room_id and self.channel_id:
                try:
                    batch = self._fetch_chat_patrol_batch(reason="chat patrol")
                    if batch:
                        try:
                            self._chat_patrol_inbox.put_nowait(batch)
                        except queue.Full:
                            log.warning("chat patrol inbox full; deferred one batch")
                except Exception:
                    if not self._chat_patrol_stop.is_set():
                        log.exception("chat patrol visit failed")
            try:
                interval = min(60.0, max(0.5, float(
                    self.cfg.get("lazer_chat_patrol_interval_seconds", 2.0)
                )))
            except (TypeError, ValueError):
                interval = 2.0
            self._chat_patrol_wake.wait(interval)
            self._chat_patrol_wake.clear()

    def _drain_chat_patrol_inbox(self) -> None:
        while True:
            try:
                messages = self._chat_patrol_inbox.get_nowait()
            except queue.Empty:
                return
            self._process_chat_messages(messages)

    def _defer_chat_patrol_batch(self, messages: list[dict] | None) -> None:
        """Hand a fetched batch back to the main loop for normal processing.

        Discord relay commands fetch the selected room's backlog while their
        Discord reply route is active. Processing that backlog immediately
        would make replies to osu! commands go to Discord. Deferring it by one
        main-loop tick restores the normal osu! reply destination while keeping
        the fetch and relay send in the same lazer visit.
        """
        batch = list(messages or [])
        if not batch:
            return
        try:
            self._chat_patrol_inbox.put_nowait(batch)
        except queue.Full:
            log.warning("chat patrol inbox full; processing deferred batch now")
            # Losing chat commands is worse than processing synchronously. Clear
            # the Discord reply route temporarily so osu! replies stay in osu!.
            previous_reply_channel = self._discord_command_reply_channel_id
            self._discord_command_reply_channel_id = None
            try:
                self._process_chat_messages(batch)
            finally:
                self._discord_command_reply_channel_id = previous_reply_channel

    def _chat_poll_interval(self) -> float:
        """Current hot-reloadable poll interval for this room state."""
        key = (
            "chat_poll_playing_seconds"
            if self.cfg.get("adaptive_chat_polling", True) and self.state == PLAYING
            else "chat_poll_seconds"
        )
        fallback = 10.0 if key == "chat_poll_playing_seconds" else 5.0
        maximum = 60.0 if key == "chat_poll_playing_seconds" else 5.0
        try:
            return self._external_float(self.cfg.get(key, fallback), 0.5, maximum)
        except (TypeError, ValueError):
            return fallback

    def _chat_api_call(self, method, *args, **kwargs):
        """Bounded retries for positively identified pre-dispatch gate refusal.

        Only normal chat maintenance uses this path. Gameplay and real-time
        inbox processing continue; unsuccessful reads/acks never advance cursors.
        Typed keepalive transport failures also defer; authentication, unsafe
        child cleanup, message/room mutation uncertainty and malformed state raise.
        """
        if method not in ('chat_ack', 'chat_messages'):
            raise ValueError('unsupported chat maintenance operation')
        retries = getattr(self, '_chat_maintenance_retries', None)
        if retries is None:
            retries = self._chat_maintenance_retries = {}
        previous = retries.get(method, {'at': 0.0, 'failures': 0})
        if time.monotonic() < previous['at']:
            return False, None
        try:
            result = getattr(self.api, method)(*args, **kwargs)
        except (ApiAdmissionDeferred, ApiKeepaliveUnavailable, ApiChatReadUnavailable) as error:
            failures = min(5, previous['failures'] + 1)
            delay = 1.0 if isinstance(error, ApiAdmissionDeferred) else min(15.0, float(2 ** (failures - 1)))
            self._chat_api_deferred_count = failures
            self._chat_api_retry_at = time.monotonic() + delay
            retries[method] = {'at': self._chat_api_retry_at, 'failures': failures}
            log.warning('chat maintenance unavailable for %s; retrying in %.1fs', method, delay)
            return False, None
        self._chat_api_deferred_count = 0
        self._chat_api_retry_at = 0.0
        retries.pop(method, None)
        return True, result

    def _maintain_chat_keepalive(self, now: float) -> None:
        """Keep account chat alive after delivering already-received commands."""
        if now - self._last_ack > 30:
            completed, _ = self._chat_api_call('chat_ack')
            if completed: self._last_ack = now

    def poll_chat(self, force: bool = False) -> None:
        now = time.time()

        if self._chat_websocket_enabled():
            # The socket and broker only enqueue events. Apply them on this
            # room thread before any potentially paced HTTP maintenance.
            # Reconnect reconciliation still runs before its live batch in
            # _drain_chat_websocket_inbox; cursor/deduplication rules stay intact.
            self._drain_chat_websocket_inbox()
            connected = bool(getattr(self.chat_websocket, "connected", False))
            try:
                reconcile_seconds = self._external_float(
                    self.cfg.get("chat_websocket_reconcile_seconds", 300.0),
                    30.0,
                    3600.0,
                )
            except (TypeError, ValueError):
                reconcile_seconds = 300.0

            if force:
                self._reconcile_chat_history(reason="forced chat check")
                self._drain_chat_websocket_inbox()
                self._maintain_chat_keepalive(now)
                return
            if connected:
                if (
                    self._chat_websocket_needs_reconcile
                    or now - self._last_chat_websocket_reconcile
                    >= reconcile_seconds
                ):
                    self._reconcile_chat_history(
                        reason=(
                            "WebSocket connect/reconnect"
                            if self._chat_websocket_needs_reconcile
                            else "periodic WebSocket safety check"
                        )
                    )
                    self._drain_chat_websocket_inbox()
                self._maintain_chat_keepalive(now)
                return
            # The account socket is offline or still connecting. Fall through
            # to the existing adaptive HTTP poll until it recovers.

        self._maintain_chat_keepalive(now)
        if self._chat_patrol_enabled():
            self._drain_chat_patrol_inbox()
            if force:
                messages = self._fetch_chat_patrol_batch(
                    reason="urgent chat patrol"
                )
                self._process_chat_messages(messages)
                self._drain_chat_patrol_inbox()
            return

        if (
            not force
            and now - self._last_chat_poll
            < self._chat_poll_interval()
        ):
            return
        self._last_chat_poll = now

        completed, msgs = self._chat_api_call('chat_messages', self.channel_id, since=self.last_message_id or None)
        if completed: self._process_chat_messages(msgs)

    def _process_chat_messages(self, msgs: list[dict] | None) -> None:
        """Apply one fetched chat batch on the lobby's main state thread."""
        now = time.time()
        # Freeze the map context for this API batch. A successful vote can change
        # current_item_id while there are still older .skip messages left in the
        # same response; those old messages must not immediately vote-skip the
        # replacement map.
        batch_item_id = self.current_item_id
        try:
            batch_beatmap_id = int(self.current_map.beatmap_id)
        except (AttributeError, TypeError, ValueError):
            batch_beatmap_id = None
        def message_order(item: dict) -> int:
            try:
                return int(item.get("message_id", 0) or 0)
            except (TypeError, ValueError):
                return 0

        ordered = sorted(
            (m for m in (msgs or []) if isinstance(m, dict)),
            key=message_order,
        )
        for m in ordered:
            try:
                mid = int(m.get("message_id", 0) or 0)
            except (TypeError, ValueError):
                mid = 0
            if mid and mid in self._seen_chat_message_ids:
                continue
            if mid:
                self._seen_chat_message_ids.add(mid)
                self._seen_chat_message_order.append(mid)
                while len(self._seen_chat_message_order) > 2000:
                    expired = self._seen_chat_message_order.popleft()
                    self._seen_chat_message_ids.discard(expired)
            if mid > self.last_message_id:
                self.last_message_id = mid
            if mid in self._own_messages:
                continue
            # bot_user_id is a fallback for a DEDICATED bot account. When the
            # bot runs as the operator (the normal case) it must stay unset,
            # or the operator's own commands get ignored too.
            content = (m.get("content") or "").strip()
            sender_id = m.get("sender_id")
            is_configured_bot_sender = bool(
                self.cfg.get("bot_user_id")
                and sender_id == self.cfg["bot_user_id"]
            )

            # Keep a searchable transcript of all incoming human chat in the
            # normal session log. Bot-authored messages were already filtered
            # above via _own_messages, so this captures player conversation,
            # complaints, feedback, and commands without echoing our own chat.
            if content:
                sender = self.players.get(sender_id, {}) if sender_id else {}
                sender_name = (
                    m.get("_sender_username")
                    or sender.get("name")
                    or str(sender_id or "?")
                )
                # Keep one physical log line per chat message even if the API
                # returns embedded newlines.
                log_content = content.replace("\r", " ").replace("\n", " ")
                log.info("CHAT <%s:%s> %s",
                         sender_name, sender_id or "?", log_content)
                if sender_id is not None:
                    if is_configured_bot_sender:
                        # A dedicated bot account is never allowed to execute
                        # its own text, but its visible lobby messages still
                        # belong in the Discord transcript.
                        self._notify_discord_lobby_event(
                            "OSU BOT", f"{sender_name}: {content}"
                        )
                    else:
                        self._notify_discord_lobby_chat(
                            user_id=int(sender_id),
                            text=content,
                        )
                if is_configured_bot_sender:
                    continue
                if self.room_id is not None and sender_id is not None:
                    try:
                        self.ratings.record_interaction(
                            room_id=int(self.room_id),
                            user_id=int(sender_id),
                            username=sender_name,
                            is_command=content.startswith(self.prefix),
                        )
                    except Exception:
                        # Usage evidence must never interfere with chat/commands.
                        log.exception(
                            "could not record chat interaction for user %s",
                            sender_id,
                        )

                # Recent context is human conversation only.  Commands can carry
                # admin/private operational details and the current `.ai` prompt
                # is supplied separately, so exclude all prefixed commands here.
                if not content.startswith(self.prefix):
                    self._recent_human_chat.append(
                        (now, str(sender_name), log_content[:500])
                    )
                    self._observe_player_language(
                        sender_id, log_content, self._chat_message_time(m, now)
                    )
                    # Start-pressure from an actual player silently disables
                    # optional waits for this map. Spectators cannot take this
                    # control away from the people currently playing.
                    try:
                        active_player = int(sender_id) in self.gameplay_player_ids()
                    except (TypeError, ValueError):
                        active_player = False
                    if (
                        active_player
                        and self.state in (IDLE, STARTING)
                        and self._chat_is_countdown_frustration(log_content)
                    ):
                        self._suppress_countdown_wait(
                            f"start-pressure from player {sender_id}"
                        )

            if content.startswith(self.prefix):
                started = time.monotonic()
                try:
                    self.handle_command(
                        sender_id,
                        content,
                        command_time=self._chat_message_time(m, now),
                        command_item_id=batch_item_id,
                        command_beatmap_id=batch_beatmap_id,
                    )
                finally:
                    self._record_chat_command_duration(started)

    def _record_chat_command_duration(self, started: float) -> None:
        """Bounded, sampled handler timings without player or command content.

        These measure processing, not socket transit or deferred reply delivery.
        Recording failure durations must not turn an uncertain command into a
        retry; the original exception still leaves the room thread normally.
        """
        now = time.monotonic()
        samples = getattr(self, '_chat_command_durations', None)
        if samples is None:
            samples = self._chat_command_durations = deque(maxlen=64)
            self._chat_command_timing_at = now
        samples.append(max(0.0, now - started))
        if now - self._chat_command_timing_at < 60.0:
            return
        self._chat_command_timing_at = now
        ordered = sorted(samples)
        log.info(
            "chat command handler timing: samples=%d p50=%.3fs p95=%.3fs max=%.3fs "
            "(processing only; excludes socket transit and deferred delivery)",
            len(ordered), ordered[(len(ordered) - 1) // 2],
            ordered[min(len(ordered) - 1, math.ceil(len(ordered) * .95) - 1)],
            ordered[-1],
        )

    def _handle_mania_command(self, user_id, cmd, parts):
        """Mania-only player UI; common moderation/voting remains shared."""
        p = self.prefix
        sources = {"farm", "stream", "variety", "jack", "chordjack", "ln", "technical", "rice", "noodle"}
        if cmd in ("map", "style") and len(parts) == 1:
            m = self._pp_command_map()
            if m is None:
                self.say("No current map yet.")
            else:
                score = getattr(m, "mania_rank_score", None)
                evidence = f" | experimental overrep {score:.3f}" if score is not None else ""
                origin = "vetted standard convert" if getattr(m, "is_mania_convert", False) else "native"
                self.say(f"{m.label} | {origin} {self._mania_key_label()} | {m.hit_length}s | {m.quadrant}{evidence} — {m.url}")
            if cmd == "style":
                self.say(f"{p}style <styles>: jack, chordjack, stream, ln, technical, rice, noodle or farm. Spaces, commas or | combine styles; e.g. {p}style rice jack. Rice and Noodle cannot be combined.")
            return True
        requested = None
        if cmd == "style":
            requested = _parse_mania_style_request(" ".join(parts[1:]))
            if requested is None:
                self.say(f"Usage: {p}style <jack chordjack stream ln technical rice noodle farm|variety>; Rice and Noodle cannot be combined.")
                return True
            cmd = requested[0]
        if cmd not in sources:
            if cmd == "pool":
                convert_count = sum(bool(getattr(m, "is_mania_convert", False)) for m in self.pool.maps)
                preference = (f"; prefer {self._preferred_length_text()}"
                              if getattr(self.pool, 'real_max_length', None) is not None else '')
                if self.pool.mania_source_mode == 'variety':
                    preference += f"; LN weight {self.pool.mania_ln_pick_multiplier:.0%}"
                self.say(f"{self._mania_key_label()} {self.pool.source_mode_label()}: {len(self.pool.maps)} maps ({convert_count} vetted converts) | {self.pool.sr_min:.2f}-{self.pool.sr_max:.2f}* NM | {self.pool.mania_min_length}-{self.pool.max_length}s{preference} | >= {self.pool.min_passcount} passes. .harder/.easier changes future picks.")
                return True
            return False
        if requested is None:
            requested = _parse_mania_style_request(" ".join(parts))
            if requested is None or requested[0] != cmd:
                self.say(f"Usage: {p}style <styles>; combine styles with spaces, commas or |.")
                return True
        # Do not let a Discord outsider or spectator change a room's style.
        if not self._is_moderator(user_id) and user_id not in self.gameplay_player_ids():
            self.say("Join as a player to change this lobby's style.")
            return True
        if "farm" in requested and not self.pool.mania_farm_enabled:
            self.say("Mania farm selection is not enabled yet: the age-adjusted report needs validation. Variety and structural styles remain available.")
            return True
        label = "+".join(requested)
        display_label = " + ".join(style.upper() if style == "ln" else style.title() for style in requested)
        if label == self.pool.mania_source_mode:
            count = len(self.pool.maps)
            self.say(f"{display_label} already active—{count} eligible map{'s' if count != 1 else ''}.")
            return True
        n = self.pool.set_mania_source(requested)
        if not n:
            self.say("No eligible maps or missing analysis for that style. Previous pool kept; ask a mod to widen the range or run Mania Finder analysis.")
        else:
            self.say(f"{display_label} enabled—{n} eligible map{'s' if n != 1 else ''}. Future picks only.")
        return True

    def _handle_standard_style_toggle(self, user_id, cmd, parts, is_privileged):
        # Legacy/Mania/Loved pools keep their existing source handlers. Native
        # Standard pools expose the independent mix without new shared state.
        if cmd not in {'farm', 'stream', 'jump', 'variety'} or not callable(getattr(type(self.pool), 'set_style_toggle', None)):
            return False
        if cmd == 'variety':
            if not is_privileged and user_id not in self.gameplay_player_ids():
                self.say("Join as a player to change this lobby's style.")
                return True
            return False  # Retain validated legacy percentage arguments.
        if len(parts) > 2 or len(parts) == 2 and parts[1].lower() not in {'on', 'off'}:
            self.say(f"Usage: {self.prefix}{cmd} [on|off]")
            return True
        if not is_privileged and user_id not in self.gameplay_player_ids():
            self.say("Join as a player to change this lobby's style.")
            return True
        enabled = None if len(parts) == 1 else parts[1].lower() == 'on'
        if not self.pool.set_style_toggle(cmd, enabled):
            self.say("No eligible maps for that style with the current filters. Previous pool kept.")
        else:
            self.say(f"{self.pool.source_mode_label().title()} enabled—{len(self.pool.maps)} eligible maps. Future picks only.")
        return True

    @player_language_command
    def handle_command(
        self,
        user_id: int,
        text: str,
        command_time: float | None = None,
        command_item_id: int | None = None,
        command_beatmap_id: int | None = None,
    ) -> None:
        parts = text.split()
        if not parts:
            return
        cmd = parts[0].lower()
        if cmd.startswith(self.prefix):
            cmd = cmd[len(self.prefix):]
        if cmd == "language":
            cmd = "lang"
        if cmd == "t":
            cmd = "time"
        if cmd == "aim":
            cmd = "jump"
        log.info("command from %s: %s", user_id, text)
        p = self.prefix
        is_admin = self._is_admin(user_id)
        is_moderator = self._is_moderator(user_id)
        is_privileged = is_moderator  # admins inherit moderator permissions

        if self._is_project_banned(user_id) and not is_privileged:
            return
        if cmd in {'kick','ban','unban','bans'}:
            if not is_moderator:
                self.say('Moderator permission required.')
                return
            self._moderation_command(cmd,user_id,text[len(parts[0]):].strip())
            return

        # Normalize Mania aliases before the public flood guard, so aliases
        # retain the same permissions and rate limits as canonical commands.
        if self.cfg.get("ruleset_id", 0) == 3:
            cmd = _mania_style_name(cmd)

        # During very busy periods the operator can suppress all public
        # commands without affecting chat itself or admin controls. Privacy
        # opt-in/out and personal language choice/reset remain available.
        if (
            not is_privileged
            and not self.cfg.get("user_commands_enabled", True)
            and cmd not in ("hidden", "show", "lang")
        ):
            log.debug("public command suppressed from user %s: %s", user_id, text)
            return

        if not is_privileged and not self._allow_public_command_burst(user_id, cmd):
            # Stay silent while throttled: replying to the flood would consume
            # the same shared osu! API budget this guard is protecting.
            return

        if cmd == "lang":
            self._handle_language_command(user_id, parts)
            return

        mania = self.cfg.get("ruleset_id", 0) == 3
        if mania and self._handle_mania_command(user_id, cmd, parts):
            return
        if not mania and self._handle_standard_style_toggle(user_id, cmd, parts, is_privileged):
            return

        if cmd in ("help", "commands"):
            if len(parts) > 1:
                requested = parts[1].lower().removeprefix(p)
                canonical = _COMMAND_HELP_ALIASES.get(requested, requested)
                if mania:
                    canonical = _MANIA_STYLE_ALIASES.get(canonical, canonical)
                if canonical in {"style", "map", "jack", "chordjack", "ln", "technical", "rice", "noodle"} and not mania:
                    self.say(f"{p}{canonical} is available in Mania lobbies. Type {p}help for commands here.")
                elif canonical in {'jump', 'dt', 'nm'} and (mania or getattr(self.pool, 'loved_mode', False)):
                    self.say(f"{p}{canonical} is available in Standard Ranked lobbies.")
                elif canonical in _COMMAND_HELP:
                    usage, description = _COMMAND_HELP[canonical]
                    if mania and canonical == "variety":
                        usage, description = "variety", "Return to a mix of Mania styles for future picks."
                    elif canonical == "time":
                        description += f" Alias: {p}t."
                    elif not mania and canonical in {"farm", "stream", "jump"} and callable(getattr(type(self.pool), 'set_style_toggle', None)):
                        usage = f"{canonical} [on|off]"
                        description = ("Toggle this style independently; enabled styles mix. DT stays separate. "
                                       f"Reset styles with {p}variety." + (f" Alias: {p}aim." if canonical == 'jump' else ''))
                    elif getattr(self.pool, "loved_mode", False) and canonical in {"farm", "stream", "variety"}:
                        description = "Map source commands are unavailable in this Loved lobby."
                    elif canonical == "farm" and mania and not self.pool.mania_farm_enabled:
                        description = "Mania farm selection is not enabled yet."
                    self.say(f"{p}{usage} — {description}")
                elif is_moderator and _MOD_COMMAND_HELP_ALIASES.get(canonical, canonical) in _MOD_COMMAND_HELP:
                    usage, description = _MOD_COMMAND_HELP[_MOD_COMMAND_HELP_ALIASES.get(canonical, canonical)]
                    self.say(f"Mod: {p}{usage} — {description}")
                elif is_admin and canonical in _ADMIN_COMMAND_HELP:
                    usage, description = _ADMIN_COMMAND_HELP[canonical]
                    self.say(f"Admin: {p}{usage} — {description}")
                else:
                    self.say(f"Unknown command. Type {p}help or {p}help2 for available commands.")
            else:
                self.say(
                    f"{p}queue · {p}skip · {p}start · {p}harder/{p}easier · "
                    f"{p}good/{p}bad rate last map | All commands: https://poolrotater.com/commands"
                )

        elif cmd == "help2":
            # Regular players only see regular-player commands. Admin controls
            # are both hidden from and inaccessible to non-admin users.
            source_commands = (
                f"{p}map {p}style {p}jack {p}chordjack {p}stream {p}ln {p}technical {p}rice {p}noodle {p}variety {p}farm " if mania else
                "" if getattr(self.pool, "loved_mode", False) else
                f"{p}farm {p}stream {p}jump/{p}aim {p}variety [farm% stream%] {p}dt [on|off|100] {p}nm "
            )
            if mania:
                self.say(f"{self._mania_key_label()} Ranked/Approved; variety may include vetted converts. Fixed difficulty; {p}harder/{p}easier vote in {self._difficulty_vote_step():.2f}* steps. Styles are heuristic, not guaranteed PP efficiency.")
            auto_help = "" if mania else f"{p}auto "
            public_commands = (
                f"Commands: {p}queue {p}pool {source_commands}{p}stats [username] {p}lobby "
                f"{p}start {p}pp [mods] [acc%...] "
                f"{p}wait {p}time/{p}t "
                f"{p}redo/{p}replay/{p}repeat "
                f"{p}good/{p}g {p}bad/{p}b {p}ok {p}skip/{p}s "
                f"{p}harder {p}easier "
                f"{p}hidden {p}show {p}lang en|zh|ko|es|ru|ja|pl|id|fr|de {p}discord"
            )
            for line in textwrap.wrap(public_commands, width=380):
                self.say(line)
            if is_moderator:
                self.say(
                    f"Mod: {p}settings {p}start {p}abort {p}forceskip "
                    f"{p}softrestart | {p}usercommands on/off "
                    f"{p}silence on/off {p}voteskip on/off "
                    f"{p}ppannounce on/off"
                )
                self.say(f"Moderation: {p}kick <user> | {p}ban <user> [30m|24h|7d] [| reason] | {p}unban <user> | {p}bans [page]")
                self.say(
                    f"Mod tune: {p}range <min> <max> {auto_help}{p}length/<maxlength> <s> "
                    f"{p}maxage <years|off> {p}countdown <s> "
                    f"{p}autostart <s> {p}minplayers <n> {p}queueahead <n> "
                    f"{p}difficultyvote <25-100%>"
                )
                if self._discord_command_reply_channel_id is not None:
                    self.say(
                        f"Discord reports: {p}overview | {p}activity/{p}players/"
                        f"{p}ratings/{p}retention/{p}mapstats [24h|7d|30d|all] "
                        f"| {p}who | {p}apiusage | {p}lazerstatus"
                    )
            if is_admin:
                self.say(
                    f"Admin: {p}admin add/remove <user> {p}admins | "
                    f"{p}mod add/remove <user> {p}mods | "
                    f"{p}ratingignore add/remove/list/purge | {p}set <name> <value>"
                )
                self.say(
                    f"Admin room: {p}range <min> <max> {auto_help}"
                    f"{p}title <name> {p}hardrestart [maps] {p}close "
                    f"{p}closewhenempty [cancel]"
                )
                if self._discord_command_reply_channel_id is not None:
                    self.say(
                        f"Discord shutdown: {p}shutdown all | "
                        f"{p}shutdown <room> | {p}<room> shutdown | "
                        f"{p}say <message>"
                    )
                self.say(
                    "Tune names: bias halfwidth queue length poollimit minpasses "
                    "countdown autostart minplayers ratingwindow scoredelay "
                    "infointerval difficultyvote ai aimodel aiusercooldown "
                    "aiglobalcooldown aimaxpending aimaxchars aimaxtokens "
                    "aimaxprompt aichatcontext aitimeout aitemperature"
                )

        elif cmd == "discord":
            self.say(self._discord_invite_text(fallback=True))

        elif cmd == "ai":
            self._handle_ai_command(user_id, text)

        elif cmd == "time":
            if self.state != PLAYING:
                self.say("No map running.")
            else:
                remaining = self._match_remaining_seconds()
                if remaining is None:
                    self.say("Time remaining unavailable—you're in next.")
                elif remaining <= 0:
                    self.say("Finishing—you're in next.")
                else:
                    timing = self._format_match_remaining(remaining)
                    self.say(f"{timing.capitalize()} left—you're in next.")

        elif cmd == "queue":
            if self.current_map:
                self.say(f"Now: {self.current_map.label} — {self.current_map.url}")
            if self.queued:
                up_next = " | ".join(m.title[:30] for _i, m in self.queued)
                self.say(f"Up next: {up_next}")

        elif cmd == "pp":
            try:
                request = parse_pp_arguments(parts[1:])
                if mania:
                    from .mode import MANIA_FREE_MODS, MANIA_RATE_MODS
                    if set(request.mods) - (MANIA_FREE_MODS | MANIA_RATE_MODS):
                        raise PPArgumentError("Unsupported mania mods. Try .pp MR 99 or .pp DT 98; no HR, key changes or automation.")
                    if {"HD", "FI"} <= set(request.mods):
                        raise PPArgumentError("HD and FI cannot be combined.")
                    # Mania players typically compare high-accuracy runs.
                    if not parts[1:] or all(not any(c.isdigit() for c in token) for token in parts[1:]):
                        from .pp_projection import PPRequest
                        request = PPRequest(request.mods, (100.0, 99.0, 98.0, 95.0))
            except PPArgumentError as exc:
                if mania:
                    self.say(f"Usage: {p}pp [mods] [accuracy% ...] — {exc}. Examples: {p}pp | {p}pp 98.5 | {p}pp MR 99 | {p}pp DT 98")
                    return
                self.say(
                    f"Usage: {p}pp [mods] [accuracy% ...] — {exc}. "
                    f"Examples: {p}pp | {p}pp 98.5 | {p}pp HR | "
                    f"{p}pp HDHR | {p}pp HD HR 99.2"
                )
                return
            self._show_current_map_pp(
                mods=request.mods,
                accuracies=request.accuracies,
            )

        elif cmd == 'jump':
            if mania or getattr(self.pool, 'loved_mode', False):
                self.say(f"{p}jump is available in Standard Ranked lobbies.")
            elif len(parts) != 1:
                self.say(f"Usage: {p}jump")
            elif not is_privileged and user_id not in self.gameplay_player_ids():
                self.say("Join as a player to change this lobby's style.")
            elif self.pool.set_jump_mode():
                self.say(f"Jump enabled—{len(self.pool.maps)} eligible maps. Future picks only.")
            else:
                self.say("No verified jump maps fit the current filters. Previous pool kept.")

        elif cmd in ('rice', 'noodle', 'noodles'):
            self.say(f"{p}{cmd} is available in Mania lobbies.")

        elif cmd in ("farm", "stream", "variety"):
            if getattr(self.pool, "loved_mode", False):
                self.say(
                    "This is a Loved-only lobby. Farm/Stream source commands "
                    "cannot mix Ranked maps into it."
                )
                return
            if cmd in ("farm", "stream") and len(parts) != 1:
                self.say(f"Usage: {p}{cmd}")
                return

            previous_mode = self.pool.source_mode_label()
            if cmd in ("farm", "stream") and previous_mode == cmd:
                self.say(f"{cmd.title()} already active—{len(self.pool.maps)} eligible maps.")
                return
            if cmd == "farm":
                n = self.pool.set_farm_mode()
                requested = "farm"
            elif cmd == "stream":
                n = self.pool.set_stream_mode()
                requested = "stream"
            else:
                if len(parts) == 1:
                    farm_pct, stream_pct = 50.0, 50.0
                elif len(parts) == 3:
                    try:
                        farm_pct = float(parts[1])
                        stream_pct = float(parts[2])
                    except ValueError:
                        self.say(
                            f"Usage: {p}variety [farm% stream%] "
                            f"(example: {p}variety 70 30)"
                        )
                        return
                    if (
                        not math.isfinite(farm_pct) or not math.isfinite(stream_pct)
                        or farm_pct < 0 or stream_pct < 0
                        or farm_pct > 100 or stream_pct > 100
                        or abs((farm_pct + stream_pct) - 100.0) > 0.01
                    ):
                        self.say(
                            "Variety percentages must each be 0-100 and total 100 "
                            "(example: .variety 70 30)."
                        )
                        return
                else:
                    self.say(
                        f"Usage: {p}variety [farm% stream%] "
                        f"(example: {p}variety 70 30)"
                    )
                    return
                requested = f"variety {farm_pct:.0f}/{stream_pct:.0f}"
                desired_label = ("farm" if stream_pct == 0 else "stream" if farm_pct == 0 else requested)
                # Compare exact shares, not the rounded display label.
                if (self.pool.source_mode == ("variety" if farm_pct and stream_pct else desired_label)
                        and abs(self.pool.farm_share * 100 - farm_pct) < 0.0001
                        and abs(self.pool.stream_share * 100 - stream_pct) < 0.0001):
                    self.say(f"{desired_label.title()} already active—{len(self.pool.maps)} eligible maps.")
                    return
                n = self.pool.set_variety_mode(farm_pct, stream_pct)

            if n <= 0:
                self.say(
                    f"No eligible maps for {requested} with the current SR/length/"
                    "pass filters; keeping the previous pool mode."
                )
                return

            self.say(
                f"{self.pool.source_mode_label().title()} enabled—{n} eligible maps. Future picks only."
            )

        elif cmd in {"dt", "nm"}:
            if mania or not getattr(self.pool, 'mixed_dt', False):
                self.say("DT mixing is available in Standard Ranked lobbies with normal fixed mods.")
                return
            full_dt = cmd == 'dt' and len(parts) == 2 and parts[1] in ('100', '100%')
            toggle_dt = cmd == 'dt' and len(parts) == 2 and parts[1].lower() in ('on', 'off')
            if len(parts) != 1 and not full_dt and not toggle_dt:
                self.say(f"Usage: {p}dt [on|off|100] | Disable with {p}nm" if cmd == 'dt'
                         else f"Usage: {p}nm")
                return
            if not is_privileged and user_id not in self.gameplay_player_ids():
                return
            enable_dt = cmd == 'dt' and (not toggle_dt or parts[1].lower() == 'on')
            if enable_dt and not self._dt_fixed_mods_supported():
                self.say("DT mixing is unavailable with this lobby's fixed mods.")
                return
            if full_dt:
                self.pool.set_dt_override(True, full=True)
            else:
                self.pool.set_dt_override(enable_dt)
            self._dt_full_refresh_pending = full_dt
            if enable_dt:
                self.say("DT 100% enabled; NoMod fallback | Mixed with .dt | Disable with .nm"
                         if full_dt else "dt enabled, buckle up gamers | Disable with .nm")
                if full_dt and self.state in (IDLE, SCORING):
                    self.reconcile_playlist('DT 100 opt-in', fetch_remote=True)
            else:
                self.say("DT disabled; NoMod picks only. A running DT map will finish.")
                if self.state == STARTING and getattr(getattr(self, 'starting_map', None), 'is_dt', False):
                    if not self._stop_match_countdown():
                        return  # Unknown outcome: keep the frozen countdown identity.
                    self.ready.clear()
                    self.starting_map = None
                    self.starting_item_id = None
                    self._clear_start_recovery_tracking()
                    self.set_state(IDLE)
                if self.state in (IDLE, SCORING):
                    self.reconcile_playlist('DT opt-out', fetch_remote=True)

        elif cmd == "pool":
            farm_count = sum(
                1 for m in self.pool.maps if bool(getattr(m, "is_farm", False))
            )
            stream_count = sum(
                1 for m in self.pool.maps if bool(getattr(m, "is_stream", False))
            )
            both_count = sum(
                1 for m in self.pool.maps
                if bool(getattr(m, "is_farm", False))
                and bool(getattr(m, "is_stream", False))
            )
            if getattr(self.pool, "loved_mode", False):
                src = f"Loved {len(self.pool.maps)}"
            elif self.pool.source_mode == 'jump':
                src = f"Jump {len(self.pool.maps)} ({sum(m.jump_score > 0 for m in self.pool.maps)} strict)"
            elif farm_count or stream_count:
                src = f"F{farm_count}/S{stream_count}/both{both_count}"
            else:
                src = "fallback"

            active = self.gameplay_player_ids()
            pps, dropped_auto_player, auto_pp_count = (
                self.auto_difficulty_pp_values(active)
            )
            range_text = f"{self.pool.sr_min:.2f}-{self.pool.sr_max:.2f}*"

            if self.manual_range:
                community_overlay = self._community_bias_overlay()
                base_lo = float(self.cfg.get("sr_min", self.pool.sr_min))
                base_hi = float(self.cfg.get("sr_max", self.pool.sr_max))
                difficulty_text = f"manual {range_text}"
                if abs(community_overlay) >= 0.001:
                    difficulty_text += (
                        f", set {base_lo:.2f}-{base_hi:.2f}*, "
                        f"temp {community_overlay:+.2f}*"
                    )

            elif self.cfg.get("auto_difficulty", True):
                if pps:
                    med = float(median(pps))
                    half = float(self.cfg.get(
                        "auto_sr_half_width", AUTO_SR_HALF_WIDTH))
                    bias = self._effective_auto_bias()
                    target = target_sr_for_pp(
                        med, half_width=half, bias=bias)
                    next_lo, next_hi = auto_range_for_pp(
                        med, half_width=half, bias=bias)

                    pending_change = (
                        self._auto_retune_pending
                        and (
                            abs(next_lo - self.pool.sr_min) >= 0.001
                            or abs(next_hi - self.pool.sr_max) >= 0.001
                        )
                    )

                    if pending_change:
                        difficulty_text = (
                            f"auto current {range_text}; next "
                            f"{next_lo:.2f}-{next_hi:.2f}* after map "
                            f"(target {target:.2f}*, median {med:.0f}pp/"
                            f"{auto_pp_count} player"
                            f"{'s' if auto_pp_count != 1 else ''}"
                            f"{' — lowest excluded' if dropped_auto_player else ''})"
                        )
                    else:
                        difficulty_text = (
                            f"auto {range_text}, target {target:.2f}* "
                            f"from median {med:.0f}pp/{auto_pp_count} player"
                            f"{'s' if auto_pp_count != 1 else ''}"
                            f"{' — lowest excluded' if dropped_auto_player else ''}"
                        )
                else:
                    difficulty_text = (
                        f"auto on, current/default {range_text}, "
                        f"waiting for a player"
                    )

            else:
                difficulty_text = f"auto off, {range_text}"

            community_overlay = self._community_bias_overlay()
            if (
                abs(community_overlay) >= 0.001
                and not self.manual_range
                and self.cfg.get("auto_difficulty", True)
            ):
                difficulty_text += (
                    f", temp community {community_overlay:+.2f}*"
                )

            self.say(
                f"Pool: {len(self.pool.maps)} maps ({src}), "
                f"mode {self.pool.source_mode_label()}, {difficulty_text}, "
                f"length {self.pool.max_length_label()}, "
                + (f"prefer {self._preferred_length_text()}, "
                   if getattr(self.pool, 'real_max_length', None) is not None or self.pool.source_mode == 'jump' else '') +
                f"map max age {self.pool.max_map_age_label()}"
                + (f", minpasses {self.pool.effective_min_passcount()} "
                   f"({self.pool.min_passcount} preferred)"
                   if getattr(self.pool, 'sparse_passcount_enabled', False)
                   and self.pool._passcount_fallback_enabled() else '')
                + (f", {self.pool.dt_policy_label()}" if getattr(self.pool, 'mixed_dt', False) else '')
            )

        elif cmd in RATING_COMMANDS:
            if not self.last_played_map:
                return
            if time.time() > self._rating_window_until:
                self.say(
                    f"Rating window closed. Rate right after a map ends.",
                    automatic=True,
                )
                return
            rating, rating_label = RATING_COMMANDS[cmd]
            try:
                shadow_ignored = self.ratings.is_rating_ignored(int(user_id))
            except Exception:
                # If the policy lookup itself fails we cannot know that this user
                # is filtered, so retain normal feedback behavior.
                shadow_ignored = False
                log.exception(
                    "could not read rating-ignore policy for user %s", user_id
                )

            if shadow_ignored:
                # Shadow-ignore is intentionally invisible to the player.
                # Their command gets normal-looking ACK/tally behavior, but
                # the vote can never affect the real tally or map weighting.
                try:
                    shadow_first = self.ratings.shadow_rate(
                        self.last_played_map.beatmap_id,
                        user_id,
                        rating,
                    )
                    log.info(
                        "shadow-only map feedback from rating-filtered user %s: %s",
                        user_id, cmd,
                    )
                    if shadow_first:
                        if rating == 0:
                            self.say(
                                f"Thanks for the feedback! "
                                f"{self.last_played_map.title[:30]} "
                                f"— noted as {rating_label}.",
                                automatic=True,
                            )
                        else:
                            up, down = self.ratings.shadow_visible_tally(
                                self.last_played_map.beatmap_id,
                                int(user_id),
                            )
                            self.say(
                                f"Thanks for the feedback! "
                                f"{self.last_played_map.title[:30]} "
                                f"— {up} good / {down} bad.",
                                automatic=True,
                            )
                except Exception:
                    # Never fail open into map_ratings after we have positively
                    # identified a shadow-ignored user.
                    log.exception(
                        "could not save shadow-only rating for user %s", user_id
                    )
                return
            first = self.ratings.rate(
                self.last_played_map.beatmap_id,
                user_id,
                rating,
                map_origin=('standard-dt' if getattr(self.last_played_map, 'is_dt', False)
                            else getattr(self.last_played_map, "map_origin", "unknown")),
                key_count=getattr(self.last_played_map, "mania_keys", None),
                context=getattr(self, '_rating_context', {}),
            )
            if first:
                if self.ratings.reader is not None and rating != 0:
                    self._acknowledge_saved_feedback(rating_label, user_id)
                elif rating == 0:
                    self.say(
                        f"Thanks for the feedback! {self.last_played_map.title[:30]} "
                        f"— noted as {rating_label}.",
                        automatic=True,
                    )
                else:
                    up, down = self.ratings.tally(self.last_played_map.beatmap_id)
                    self.say(
                        f"Thanks for the feedback! {self.last_played_map.title[:30]} "
                        f"— {up} good / {down} bad.",
                        automatic=True,
                    )

        elif cmd == "wait":
            # .wait is player-scoped and remains available until MatchStarted.
            # During STARTING it cancels the current countdown first. Duplicate
            # requests from the same player are ignored for this map.
            self._request_countdown_wait(user_id)

        elif cmd in _REPLAY_COMMANDS:
            if not self.cfg.get("voteskip_enabled", True):
                now = time.time()
                if now - self._last_voteskip_disabled_notice >= 15.0:
                    self.say("Player votes are currently disabled.")
                    self._last_voteskip_disabled_notice = now
                return
            self.cast_replay_vote(
                user_id,
                vote_time=command_time,
                command_item_id=command_item_id,
                command_beatmap_id=command_beatmap_id,
            )

        elif cmd == "freedomdive":
            # This novelty command is deliberately absent from public help.
            self.queue_freedom_dive(user_id)

        elif cmd == "start" and not is_privileged:
            # Public .start is intentionally limited to an active gameplay
            # participant in this room. It starts the normal configured
            # countdown immediately instead of waiting for autostart/READY
            # thresholds. The normal pre-start reconciliation, availability,
            # minimum-player, and referee-safety checks still run.
            active = self.gameplay_player_ids()
            if int(user_id) not in active:
                return
            self._start_from_command(user_id, privileged=False)

        elif cmd in ("skip", "s"):
            if not self.cfg.get("voteskip_enabled", True):
                now = time.time()
                # Avoid creating a new spam source when vote-skip was disabled
                # specifically because the room is hectic.
                if now - self._last_voteskip_disabled_notice >= 15.0:
                    self.say("Vote-skip is currently disabled.")
                    self._last_voteskip_disabled_notice = now
                return
            self.cast_skip_vote(
                user_id,
                vote_time=command_time,
                command_item_id=command_item_id,
                command_beatmap_id=command_beatmap_id,
            )

        elif cmd in ("harder", "easier"):
            self.cast_difficulty_vote(user_id, cmd)

        elif cmd in ("hidden", "show"):
            requester_name = str(
                (self.players.get(user_id) or {}).get("name")
                or user_id
            )
            hidden = cmd == "hidden"
            try:
                requester_name = str(
                    (self.players.get(user_id) or {}).get("name")
                    or self.pp_stats.username_for_user_id(user_id)
                    or user_id
                )
                self.pp_stats.set_stats_hidden(
                    user_id=int(user_id),
                    username=requester_name,
                    hidden=hidden,
                )
            except Exception:
                log.exception(
                    "could not update AutoHost stats visibility for user %s",
                    user_id,
                )
                self.say(
                    f"{requester_name} — I couldn't update your stats "
                    "visibility right now."
                )
                return

            if hidden:
                self.say(
                    f"{requester_name} — your AutoHost stats are now hidden "
                    "from other players. Type "
                    f"{self.prefix}show to make them public again."
                )
            else:
                self.say(
                    f"{requester_name} — your AutoHost stats are public again."
                )

        elif cmd == "stats":
            self._show_player_stats(user_id, parts)

        elif cmd == "lobby":
            active = self.gameplay_player_ids()

            if active:
                raw_pps = []
                for uid in active:
                    raw_pp = (self.players.get(uid) or {}).get("pp")
                    try:
                        pp = float(raw_pp)
                    except (TypeError, ValueError):
                        continue
                    if pp > 0:
                        raw_pps.append(pp)

                if raw_pps:
                    average_pp = sum(raw_pps) / len(raw_pps)
                    pp_text = (
                        f"avg {average_pp:.0f}pp from {len(raw_pps)}/"
                        f"{len(active)} PP-known"
                    )
                else:
                    pp_text = "no PP data yet"

                player_text = (
                    f"{len(active)} player"
                    f"{'s' if len(active) != 1 else ''}, {pp_text}"
                )
            else:
                player_text = "waiting for players"

            range_text = f"{self.pool.sr_min:.2f}-{self.pool.sr_max:.2f}*"
            if self.manual_range:
                difficulty_text = f"manual {range_text}"
            elif not self.cfg.get("auto_difficulty", True):
                difficulty_text = f"auto off, {range_text}"
            elif active:
                auto_pps, dropped_auto_player, auto_pp_count = (
                    self.auto_difficulty_pp_values(active)
                )
                if auto_pps:
                    med = float(median(auto_pps))
                    half = float(self.cfg.get(
                        "auto_sr_half_width", AUTO_SR_HALF_WIDTH
                    ))
                    bias = self._effective_auto_bias()
                    target = target_sr_for_pp(
                        med, half_width=half, bias=bias
                    )
                    next_lo, next_hi = auto_range_for_pp(
                        med, half_width=half, bias=bias
                    )
                    next_differs = (
                        abs(next_lo - self.pool.sr_min) >= 0.001
                        or abs(next_hi - self.pool.sr_max) >= 0.001
                    )
                    excluded = (
                        ", lowest excluded" if dropped_auto_player else ""
                    )
                    if self._auto_retune_pending and next_differs:
                        difficulty_text = (
                            f"auto target {target:.2f}*, current {range_text}, "
                            f"next {next_lo:.2f}-{next_hi:.2f}* after map"
                            f"{excluded}"
                        )
                    else:
                        difficulty_text = (
                            f"auto target {target:.2f}*, active {range_text}"
                            f"{excluded}"
                        )
                else:
                    difficulty_text = f"auto on, active {range_text}"
            else:
                difficulty_text = f"auto on, active {range_text}"

            community_overlay = self._community_bias_overlay()
            if (
                abs(community_overlay) >= 0.001
                and not self.manual_range
                and self.cfg.get("auto_difficulty", True)
            ):
                difficulty_text += (
                    f", temp community {community_overlay:+.2f}*"
                )

            self.say(
                f"Lobby: {player_text} | Difficulty: {difficulty_text}."
            )

            # Lifetime usage evidence. Each autohost_match_players row is one
            # player entering one actual MatchStarted map, so two people
            # playing the same map count as two map plays. Include current
            # unflushed lobby-time slices so this is accurate during long runs.
            now = time.time()
            live_seconds: dict[int, float] = {}
            live_names: dict[int, str] = {}
            for uid, info in self.players.items():
                checkpoint = info.get("lobby_time_checkpoint_at")
                if checkpoint is not None:
                    live_seconds[int(uid)] = max(
                        0.0, now - float(checkpoint)
                    )
                live_names[int(uid)] = str(info.get("name") or uid)

            try:
                lifetime = self.pp_stats.lifetime_lobby_summary(
                    live_seconds_by_user=live_seconds,
                    live_names_by_user=live_names,
                    leaderboard_limit=3,
                )
            except Exception:
                log.exception("could not read lifetime AutoHost lobby stats")
                self.say("Lifetime stats are temporarily unavailable.")
                return

            total_lobby_time = self._format_lobby_time(
                float(lifetime.get("total_lobby_seconds") or 0.0)
            )
            unique_players = int(lifetime.get("unique_players") or 0)
            map_plays = int(lifetime.get("map_plays") or 0)
            total_pp = float(lifetime.get("total_pp_gain") or 0.0)

            self.say(
                f"Lifetime: {total_lobby_time} player-hours across "
                f"{unique_players:,} player"
                f"{'s' if unique_players != 1 else ''} | "
                f"{map_plays:,} recorded map play"
                f"{'s' if map_plays != 1 else ''} | "
                f"+{total_pp:.1f}pp total gained."
            )

            leaders = list(lifetime.get("top_playtime") or [])
            if leaders:
                leader_text = " | ".join(
                    f"{name} {self._format_lobby_time(seconds)}"
                    for name, seconds in leaders[:3]
                )
                self.say(f"Top playtime: {leader_text}.")

        # ---- moderator controls (admins inherit these)
        elif is_moderator and cmd in {
            "settings", "start", "abort", "forceskip", "softrestart",
            "usercommands", "silence", "voteskip", "ppannounce", "aichat",
            "range", "auto", "length", "maxlength", "maxage", "countdown", "autostart",
            "minplayers", "queueahead", "difficultyvote", "diffvote",
            "overview", "activity", "players", "ratings", "retention", "mapstats",
            "who", "apiusage", "lazerstatus",
        }:
            if cmd == "who":
                if user_id >= 0 or self._discord_command_reply_channel_id is None:
                    return
                if len(parts) != 1:
                    self.say(f"Usage: {p}who")
                    return

                excluded = self.non_gameplay_user_ids()
                playing: list[str] = []
                spectating: list[str] = []
                for uid, info in self.players.items():
                    if int(uid) in excluded:
                        continue
                    name = str(info.get("name") or uid).strip()
                    if info.get("status", "idle") == "spectating":
                        spectating.append(name)
                    else:
                        playing.append(name)
                playing.sort(key=str.casefold)
                spectating.sort(key=str.casefold)

                instance = str(self.cfg.get("_instance_name", "lobby"))
                if instance.casefold().startswith("room"):
                    suffix = instance[4:].strip()
                    lobby_name = f"Lobby {suffix}" if suffix else "Lobby"
                else:
                    lobby_name = instance
                lines = [
                    f"**{lobby_name} · {len(playing)} player"
                    f"{'s' if len(playing) != 1 else ''}**",
                    ", ".join(playing) if playing else "No active players.",
                ]
                if spectating:
                    lines.extend([
                        f"**Spectating · {len(spectating)}**",
                        ", ".join(spectating),
                    ])
                self.say("\n".join(lines))
                return
            if cmd == "apiusage":
                if user_id >= 0 or self._discord_command_reply_channel_id is None:
                    return
                if len(parts) != 1:
                    self.say(f"Usage: {p}apiusage")
                    return
                try:
                    self.say(self.api.usage_report())
                except Exception:
                    log.exception("Discord API usage report failed")
                    self.say("API usage report is unavailable right now.")
                return
            if cmd == "lazerstatus":
                if user_id >= 0 or self._discord_command_reply_channel_id is None:
                    return
                if len(parts) != 1:
                    self.say(f"Usage: {p}lazerstatus")
                    return
                try:
                    coordinator = self.lazer_coordinator
                    state = coordinator.snapshot()
                    now = time.time()
                    assigned = int(state.get("room_id", 0) or 0)
                    switch_in = max(
                        0.0, float(state.get("lease_until", 0.0) or 0.0) - now
                    )
                    visit_in = max(
                        0.0, float(state.get("visit_until", 0.0) or 0.0) - now
                    )
                    ready_in = max(
                        0.0, float(state.get("ready_at", 0.0) or 0.0) - now
                    )
                    try:
                        max_actions = max(
                            1, int(self.cfg.get("lazer_visit_max_actions", 8))
                        )
                    except (TypeError, ValueError):
                        max_actions = 8
                    instance = str(self.cfg.get("_instance_name", "room"))
                    self.say(
                        f"**lazer coordinator · {instance}**\n"
                        f"Enabled: {'yes' if coordinator.enabled else 'no'} · "
                        f"assigned room: {assigned or 'none'} · "
                        f"this room: {self.room_id or 'none'}\n"
                        f"Queued switches: {int(state.get('queued', 0) or 0)} · "
                        f"join ready in {ready_in:.1f}s · switchable in {switch_in:.1f}s\n"
                        f"Current visit: {int(state.get('action_count', 0) or 0)}"
                        f"/{max_actions} actions · batch window {visit_in:.1f}s left"
                    )
                except Exception:
                    log.exception("Discord lazer status report failed")
                    self.say("Lazer coordinator status is unavailable right now.")
                return
            if cmd in {"overview", "activity", "players", "ratings", "retention", "mapstats"}:
                if user_id >= 0 or self._discord_command_reply_channel_id is None:
                    return
                try:
                    report = "activity" if cmd == "overview" else cmd
                    default_window = "24h" if cmd == "overview" else "7d"
                    if len(parts) > 2:
                        raise ValueError(
                            "usage: .%s [24h|7d|30d|all]" % cmd
                        )
                    window = parts[1] if len(parts) == 2 else default_window
                    self.say(self.discord_reports.report(report, window))
                except ValueError as e:
                    self.say(f"Usage: .{cmd} [24h|7d|30d|all] ({e})")
                except Exception:
                    log.exception("Discord report failed: %s", cmd); self.say("Report is unavailable right now.")
                return
            if cmd == "settings":
                self.show_admin_settings()

            elif cmd == "usercommands":
                if len(parts) != 2:
                    self.say(
                        f"Public user commands are "
                        f"{'enabled' if self.cfg.get('user_commands_enabled', True) else 'disabled'}. "
                        f"Usage: {p}usercommands on/off"
                    )
                else:
                    try:
                        enabled = self._parse_on_off(parts[1])
                    except ValueError as e:
                        self.say(f"Usage: {p}usercommands on/off ({e})")
                    else:
                        saved = self._set_persistent_toggle(
                            "user_commands_enabled", enabled
                        )
                        if not enabled:
                            self._clear_skip_vote("public commands disabled")
                            self._clear_difficulty_vote(
                                "public commands disabled"
                            )
                        self.say(
                            self._toggle_reply("Public user commands", "enabled" if enabled else "disabled", saved)
                        )

            elif cmd == "silence":
                if len(parts) != 2:
                    self.say(
                        f"Bot chat is "
                        f"{'silenced' if self.cfg.get('bot_chat_silenced', False) else 'normal'}. "
                        f"Usage: {p}silence on/off"
                    )
                else:
                    try:
                        silenced = self._parse_on_off(parts[1])
                    except ValueError as e:
                        self.say(f"Usage: {p}silence on/off ({e})")
                    else:
                        saved = self._set_persistent_toggle(
                            "bot_chat_silenced", silenced
                        )
                        self.say(
                            self._toggle_reply("Bot automatic chat", "silenced" if silenced else "restored", saved)
                        )

            elif cmd == "voteskip":
                if len(parts) != 2:
                    self.say(
                        f"Vote-skip is "
                        f"{'enabled' if self.cfg.get('voteskip_enabled', True) else 'disabled'}. "
                        f"Usage: {p}voteskip on/off"
                    )
                else:
                    try:
                        enabled = self._parse_on_off(parts[1])
                    except ValueError as e:
                        self.say(f"Usage: {p}voteskip on/off ({e})")
                    else:
                        saved = self._set_persistent_toggle(
                            "voteskip_enabled", enabled
                        )
                        if not enabled:
                            self._clear_skip_vote(
                                "vote-skip disabled by moderator"
                            )
                        self.say(
                            self._toggle_reply("Vote-skip", "enabled" if enabled else "disabled", saved)
                        )

            elif cmd == "ppannounce":
                if len(parts) != 2:
                    self.say(
                        f"Automatic next-map PP is "
                        f"{'enabled' if self.cfg.get('map_pp_auto_announce_enabled', True) else 'disabled'}. "
                        f"Usage: {p}ppannounce on/off"
                    )
                else:
                    try:
                        enabled = self._parse_on_off(parts[1])
                    except ValueError as e:
                        self.say(f"Usage: {p}ppannounce on/off ({e})")
                    else:
                        key = "map_pp_auto_announce_enabled"
                        unchanged = self.cfg.get(key, True) == enabled
                        self.cfg[key] = enabled
                        universal_keys = {
                            str(value)
                            for value in self.cfg.get("_universal_config_keys", [])
                        }
                        if key in universal_keys:
                            saved = self._persist_universal_config_updates(
                                {key: enabled}
                            )
                            scope = " globally"
                        else:
                            saved = self._persist_config_updates({key: enabled})
                            scope = ""
                        if not enabled:
                            self._post_match_pp_announce_pending = False
                            self._post_match_pp_completed_beatmap_id = None
                        self.say(
                            f"Automatic next-map PP announcements "
                            f"{'already ' if unchanged else ''}"
                            f"{'enabled' if enabled else 'disabled'}{scope}."
                            f"{self._persist_suffix(saved)}"
                        )

            elif cmd == "aichat":
                if len(parts) != 2:
                    self.say(
                        f"AI chat is "
                        f"{'enabled' if self.cfg.get('ai_enabled', True) else 'disabled'}. "
                        f"Usage: {p}aichat on/off"
                    )
                else:
                    try:
                        enabled = self._parse_on_off(parts[1])
                    except ValueError as e:
                        self.say(f"Usage: {p}aichat on/off ({e})")
                    else:
                        saved = self._set_persistent_toggle("ai_enabled", enabled)
                        self.say(
                            self._toggle_reply("AI chat", "enabled" if enabled else "disabled", saved)
                        )

            elif cmd == "range":
                if len(parts) != 3:
                    self.say(f"Usage: {p}range 4.5 5.5")
                else:
                    try:
                        lo, hi = float(parts[1]), float(parts[2])
                        if not math.isfinite(lo) or not math.isfinite(hi) or not 0 <= lo < hi:
                            raise ValueError('star range must be finite, nonnegative and increasing')
                        self._set_manual_range_operator(lo, hi, user_id=user_id)
                    except ValueError:
                        self.say(f"Usage: {p}range 4.5 5.5")

            elif cmd == "auto":
                if len(parts) != 1:
                    self.say(f"Usage: {p}auto")
                else:
                    self._enable_auto_difficulty_operator()

            elif cmd in ("length", "maxlength"):
                length_minimum = (
                    0 if getattr(self.pool, "loved_mode", False) else 30
                )
                usage_range = "0-600 (0=unlimited)" if length_minimum == 0 else "30-600"
                if len(parts) != 2:
                    self.say(
                        f"Max map length is {self.pool.max_length_label()}. "
                        f"Usage: {p}maxlength <{usage_range}> (or {p}length)"
                    )
                else:
                    try:
                        raw_length = str(parts[1]).strip().casefold()
                        if length_minimum == 0 and raw_length in {
                            "off", "none", "unlimited"
                        }:
                            value = 0
                        else:
                            value = self._parse_admin_int(
                                parts[1], length_minimum, 600
                            )
                    except ValueError as e:
                        self.say(f"Usage: {p}maxlength <{usage_range}> ({e})")
                    else:
                        updates = {"max_length": value}
                        if getattr(self.pool, 'real_max_length', None) is not None:
                            updates['real_max_length'] = value
                            try:
                                n = self.pool.configure_duration_policy(dict(self.cfg, **updates))
                                self.cfg.update(updates)
                                ok = True
                            except (ValueError, TypeError, sqlite3.Error):
                                ok, n = False, 0
                        else:
                            ok, n = self._reload_pool_filter("max_length", value, "max_length")
                        if not ok:
                            self.say(
                                f"Rejected length={value}: it failed or left no eligible maps; previous setting kept."
                            )
                        else:
                            saved = self._persist_config_updates(updates)
                            if self.cfg.get("auto_difficulty", True) and not self.manual_range:
                                self._auto_retune_pending = True
                            self.say(
                                f"Max map length set to "
                                f"{'unlimited' if value == 0 else f'{value}s'}; "
                                f"{n} eligible maps for future picks. "
                                f"Existing queued maps untouched.{self._persist_suffix(saved)}"
                            )

            elif cmd == "maxage":
                age_kind = (
                    "Loved" if getattr(self.pool, "loved_mode", False)
                    else "Ranked"
                )
                if len(parts) == 1:
                    self.say(
                        f"{age_kind} map-age limit is {self.pool.max_map_age_label()}. "
                        f"Usage: {p}maxage <0.1-100|off>"
                    )
                elif len(parts) != 2:
                    self.say(f"Usage: {p}maxage <0.1-100|off>")
                else:
                    raw = str(parts[1]).strip().casefold()
                    try:
                        if raw in ("off", "none", "disable", "disabled"):
                            value = 0.0
                        else:
                            value = float(raw)
                            if value == 0.0:
                                value = 0.0
                            elif not 0.1 <= value <= 100.0:
                                raise ValueError("must be 0.1-100 years or off")
                    except (TypeError, ValueError) as e:
                        self.say(f"Usage: {p}maxage <0.1-100|off> ({e})")
                    else:
                        n = self.pool.set_max_map_age_years(value)
                        if n <= 0:
                            self.say(
                                "Rejected max-age setting: it failed or left no "
                                "eligible maps; previous setting kept."
                            )
                        else:
                            self.cfg["max_map_age_years"] = value
                            saved = self._persist_config_updates({
                                "max_map_age_years": value
                            })
                            if value <= 0.0:
                                setting_text = f"{age_kind} map-age limit disabled"
                            else:
                                unit = "year" if value == 1.0 else "years"
                                setting_text = (
                                    f"Max {age_kind.casefold()} map age set to "
                                    f"{value:g} {unit}"
                                )
                            self.say(
                                f"{setting_text}; {n} eligible maps for future "
                                "picks. Existing queued maps untouched."
                                f"{self._persist_suffix(saved)}"
                            )

            elif cmd == "countdown":
                if len(parts) != 2:
                    self.say(
                        f"Countdown is {int(self.cfg.get('countdown_seconds', 15))}s. "
                        f"Usage: {p}countdown <1-120>"
                    )
                else:
                    try:
                        value = self._parse_admin_int(parts[1], 1, 120)
                    except ValueError as e:
                        self.say(f"Usage: {p}countdown <1-120> ({e})")
                    else:
                        self.cfg["countdown_seconds"] = value
                        saved = self._persist_config_updates({"countdown_seconds": value})
                        self.say(
                            f"Countdown set to {value}s.{self._persist_suffix(saved)}"
                        )

            elif cmd == "autostart":
                if len(parts) != 2:
                    self.say(
                        f"Auto-start is {int(self.cfg.get('auto_start_seconds', 45))}s. "
                        f"Usage: {p}autostart <0-600>"
                    )
                else:
                    try:
                        value = self._parse_admin_int(parts[1], 0, 600)
                    except ValueError as e:
                        self.say(f"Usage: {p}autostart <0-600> ({e})")
                    else:
                        self.cfg["auto_start_seconds"] = value
                        saved = self._persist_config_updates({"auto_start_seconds": value})
                        self.say(
                            f"Auto-start set to {value}s.{self._persist_suffix(saved)}"
                        )

            elif cmd == "minplayers":
                if len(parts) != 2:
                    self.say(
                        f"Minimum players is {int(self.cfg.get('min_players', 1))}. "
                        f"Usage: {p}minplayers <1-32>"
                    )
                else:
                    try:
                        value = self._parse_admin_int(parts[1], 1, 32)
                    except ValueError as e:
                        self.say(f"Usage: {p}minplayers <1-32> ({e})")
                    else:
                        self.cfg["min_players"] = value
                        saved = self._persist_config_updates({"min_players": value})
                        self.cancel_start_if_too_few_players()
                        self.say(
                            f"Minimum players set to {value}.{self._persist_suffix(saved)}"
                        )

            elif cmd == "queueahead":
                if len(parts) != 2:
                    ahead = int(self.cfg.get("queue_ahead", 2))
                    self.say(
                        f"Queue is {ahead} future map(s). Usage: {p}queueahead <0-8>"
                    )
                else:
                    try:
                        value = self._parse_admin_int(parts[1], 0, 8)
                    except ValueError as e:
                        self.say(f"Usage: {p}queueahead <0-8> ({e})")
                    else:
                        self.cfg["queue_ahead"] = value
                        saved = self._persist_config_updates({"queue_ahead": value})
                        if self.state in (IDLE, SCORING):
                            self.reconcile_playlist(
                                reason="moderator queue setting",
                                fetch_remote=True,
                                ensure_depth=True,
                                trim_extras=True,
                            )
                            apply_text = "playlist depth updated"
                        else:
                            apply_text = "playlist depth updates after the match"
                        self.say(
                            f"Queue set to {value} future map(s) ({value + 1} visible); "
                            f"{apply_text}.{self._persist_suffix(saved)}"
                        )

            elif cmd in ("difficultyvote", "diffvote"):
                if len(parts) != 2:
                    ratio = float(self.cfg.get("difficulty_vote_ratio", 0.50))
                    self.say(
                        f"Difficulty-vote threshold is {ratio:.0%}. "
                        f"Usage: {p}difficultyvote <25-100%>"
                    )
                else:
                    try:
                        raw = str(parts[1]).strip()
                        if raw.endswith("%"):
                            value = float(raw[:-1]) / 100.0
                        else:
                            value = float(raw)
                            if value > 1.0:
                                value /= 100.0
                        if not 0.25 <= value <= 1.0:
                            raise ValueError("must be 25-100%")
                    except (TypeError, ValueError) as e:
                        self.say(
                            f"Usage: {p}difficultyvote <25-100%> ({e})"
                        )
                    else:
                        self.cfg["difficulty_vote_ratio"] = value
                        saved = self._persist_config_updates({
                            "difficulty_vote_ratio": value
                        })
                        self._clear_difficulty_vote(
                            "threshold changed by moderator"
                        )
                        self.say(
                            f"Difficulty-vote threshold set to {value:.0%} of active players "
                            f"(minimum 2 when more than one player)."
                            f"{self._persist_suffix(saved)}"
                        )

            elif cmd == "start":
                self._start_from_command(user_id, privileged=True)

            elif cmd == "abort":
                self._explicit_abort_pending = True
                self.hub.abort_match(self.room_id)

            elif cmd == "forceskip":
                self.force_skip(user_id)

            elif cmd == "softrestart":
                self.request_soft_restart()

        # ---- admin only
        elif is_admin:
            if cmd in {"say", "chat"}:
                if user_id >= 0 or self._discord_command_reply_channel_id is None:
                    return
                message = text.partition(" ")[2].strip()
                if not message:
                    self.say(f"Usage: {p}say <message>")
                    return
                if self.cfg.get("_headless_supervised", False):
                    # Normal polling already owns this room's cursor. Fetching
                    # through the disabled patrol cursor would replay old votes.
                    self.say(message, lobby_only=True)
                    return
                # Join the selected lobby and import its recent chat before the
                # operator's line is posted. Process that backlog on the next
                # main-loop tick, after Discord reply routing has been cleared,
                # so any osu! command replies still go to the osu! lobby.
                backlog = self._fetch_chat_patrol_batch(
                    reason="Discord lobby relay"
                )
                self._defer_chat_patrol_batch(backlog)
                self.say(message, lobby_only=True)
                return

            if cmd == "settings":
                self.show_admin_settings()

            elif cmd == "admins":
                admin_parts = []
                for admin_id in self._admin_ids():
                    name = self._known_username(admin_id)
                    admin_parts.append(
                        f"{name} ({admin_id})" if name else str(admin_id)
                    )
                self.say(
                    "Admins: " + (", ".join(admin_parts) if admin_parts else "none")
                )

            elif cmd == "admin":
                if len(parts) < 3 or parts[1].casefold() not in ("add", "remove"):
                    self.say(
                        f"Usage: {p}admin add <username|id> or "
                        f"{p}admin remove <username|id>"
                    )
                else:
                    action = parts[1].casefold()
                    raw_target = " ".join(parts[2:]).strip()
                    resolved = self._resolve_user_reference(raw_target)
                    if resolved is None:
                        self.say(
                            f"I don't know {raw_target!r} yet. Use their numeric "
                            "osu! user id, or wait until they've joined once."
                        )
                    else:
                        target_id, target_name = resolved
                        admins = self._admin_ids()
                        if action == "add":
                            if target_id in admins:
                                self.say(f"{target_name} is already an admin.")
                            else:
                                admins.append(target_id)
                                admins = sorted(set(admins))
                                moderators = [
                                    m for m in self._moderator_ids()
                                    if m != target_id
                                ]
                                self.cfg["admins"] = admins
                                self.cfg["moderators"] = moderators
                                saved = self._persist_config_updates({
                                    "admins": admins,
                                    "moderators": moderators,
                                })
                                self.say(
                                    f"Added {target_name} ({target_id}) as an admin."
                                    f"{self._persist_suffix(saved)}"
                                )
                        else:
                            try:
                                authenticated_owner = int(
                                    self.cfg.get("_authenticated_user_id")
                                )
                            except (TypeError, ValueError):
                                authenticated_owner = None

                            if (
                                authenticated_owner is not None
                                and target_id == authenticated_owner
                            ):
                                self.say(
                                    "The authenticated room owner can't be removed "
                                    "from admins while the bot is running."
                                )
                            elif target_id == int(user_id):
                                self.say(
                                    "You can't remove your own admin access from chat; "
                                    "ask another admin to remove you."
                                )
                            elif target_id not in admins:
                                self.say(f"{target_name} is not an admin.")
                            elif len(admins) <= 1:
                                self.say("The final admin can't be removed.")
                            else:
                                admins = [a for a in admins if a != target_id]
                                self.cfg["admins"] = admins
                                saved = self._persist_config_updates({"admins": admins})
                                self.say(
                                    f"Removed {target_name} ({target_id}) from admins."
                                    f"{self._persist_suffix(saved)}"
                                )

            elif cmd == "mods":
                mod_parts = []
                for moderator_id in self._moderator_ids():
                    name = self._known_username(moderator_id)
                    mod_parts.append(
                        f"{name} ({moderator_id})"
                        if name else str(moderator_id)
                    )
                self.say(
                    "Moderators: "
                    + (", ".join(mod_parts) if mod_parts else "none")
                )

            elif cmd == "mod":
                if len(parts) < 3 or parts[1].casefold() not in ("add", "remove"):
                    self.say(
                        f"Usage: {p}mod add <username|id> or "
                        f"{p}mod remove <username|id>"
                    )
                else:
                    action = parts[1].casefold()
                    raw_target = " ".join(parts[2:]).strip()
                    resolved = self._resolve_user_reference(raw_target)
                    if resolved is None:
                        self.say(
                            f"I don't know {raw_target!r} yet. Use their numeric "
                            "osu! user id, or wait until they've joined once."
                        )
                    else:
                        target_id, target_name = resolved
                        moderators = self._moderator_ids()
                        admins = self._admin_ids()

                        if action == "add":
                            if target_id in admins:
                                self.say(
                                    f"{target_name} is an admin and already has "
                                    "all moderator permissions."
                                )
                            elif target_id in moderators:
                                self.say(
                                    f"{target_name} is already a moderator."
                                )
                            else:
                                moderators.append(target_id)
                                moderators = sorted(set(moderators))
                                self.cfg["moderators"] = moderators
                                saved = self._persist_config_updates({
                                    "moderators": moderators
                                })
                                self.say(
                                    f"Added {target_name} ({target_id}) as a "
                                    f"moderator.{self._persist_suffix(saved)}"
                                )
                        else:
                            if target_id not in moderators:
                                self.say(
                                    f"{target_name} is not an explicit moderator."
                                )
                            else:
                                moderators = [
                                    m for m in moderators if m != target_id
                                ]
                                self.cfg["moderators"] = moderators
                                saved = self._persist_config_updates({
                                    "moderators": moderators
                                })
                                self.say(
                                    f"Removed {target_name} ({target_id}) from "
                                    f"moderators.{self._persist_suffix(saved)}"
                                )

            elif cmd in ("ratingignore", "feedbackignore", "ratingpurge"):
                pfx = self.prefix
                # .ratingpurge is a convenience alias for
                # .ratingignore purge <user> confirm
                if cmd == "ratingpurge":
                    action = "purge"
                    action_parts = [parts[0], "purge", *parts[1:]]
                else:
                    action = parts[1].casefold() if len(parts) >= 2 else ""
                    action_parts = parts

                if action in ("list", "show") and len(action_parts) == 2:
                    try:
                        ignored_rows = self.ratings.rating_ignored_users()
                    except Exception:
                        log.exception("could not list rating-ignored users")
                        self.say("Couldn't read the rating-ignore list right now.")
                        return

                    if not ignored_rows:
                        self.say("Rating-ignore list is empty.")
                    else:
                        labels = []
                        for row in ignored_rows:
                            uid = int(row.get("user_id"))
                            name = (
                                self._known_username(uid)
                                or str(row.get("username") or "").strip()
                            )
                            labels.append(
                                f"{name} ({uid})" if name else str(uid)
                            )

                        shown = []
                        length = len("Rating-ignored: ")
                        for label in labels:
                            extra = len(label) + (2 if shown else 0)
                            if length + extra > 340:
                                break
                            shown.append(label)
                            length += extra
                        remaining = len(labels) - len(shown)
                        suffix = f" | +{remaining} more" if remaining else ""
                        self.say(
                            "Rating-ignored: " + ", ".join(shown) + suffix
                        )

                elif action in ("add", "ignore", "remove", "allow") and len(action_parts) >= 3:
                    raw_target = " ".join(action_parts[2:]).strip()
                    resolved = self._resolve_user_reference(raw_target)
                    if resolved is None:
                        self.say(
                            f"I don't know {raw_target!r} yet. Use their numeric "
                            "osu! user id, or wait until they've joined once."
                        )
                    else:
                        target_id, target_name = resolved
                        actor_name = (
                            self._known_username(int(user_id)) or str(user_id)
                        )
                        make_ignored = action in ("add", "ignore")
                        try:
                            existing_real = self.ratings.rating_count_for_user(
                                target_id
                            )
                            existing_shadow = (
                                self.ratings.shadow_rating_count_for_user(target_id)
                            )
                            changed = self.ratings.set_rating_ignored(
                                user_id=target_id,
                                username=target_name,
                                ignored=make_ignored,
                                actor_user_id=int(user_id),
                                actor_username=actor_name,
                            )
                        except Exception:
                            log.exception(
                                "could not update rating policy for user %s",
                                target_id,
                            )
                            self.say("Couldn't update the rating filter right now.")
                            return

                        if make_ignored:
                            if changed:
                                self.say(
                                    f"Rating filter updated; {existing_real} existing "
                                    "real vote(s) retired. New feedback is shadow-only."
                                )
                                log.warning(
                                    "ADMIN %s:%s shadow-rating-ignore added %s:%s "
                                    "(%d real rows retired; %d prior shadow rows)",
                                    actor_name, user_id, target_name, target_id,
                                    existing_real, existing_shadow,
                                )
                            else:
                                self.say("That user is already rating-ignored.")
                        else:
                            if changed:
                                self.say(
                                    "Feedback allowed going forward; all older "
                                    "real/shadow ratings stay excluded."
                                )
                                log.warning(
                                    "ADMIN %s:%s shadow-rating-ignore allowed %s:%s; "
                                    "old ratings remain retired",
                                    actor_name, user_id, target_name, target_id,
                                )
                            else:
                                self.say(
                                    "That user is not currently rating-ignored."
                                )

                elif action == "purge":
                    # Irreversible chat-side purge requires an explicit final
                    # CONFIRM token. For invisible live moderation, use the
                    # standalone rating_control.py utility instead.
                    if len(action_parts) < 4 or action_parts[-1].casefold() != "confirm":
                        self.say(
                            f"Permanent purge: {pfx}ratingignore purge "
                            f"<username|id> confirm"
                        )
                        return

                    raw_target = " ".join(action_parts[2:-1]).strip()
                    resolved = self._resolve_user_reference(raw_target)
                    if resolved is None:
                        self.say(
                            f"I don't know {raw_target!r} yet. Use their numeric "
                            "osu! user id, or wait until they've joined once."
                        )
                        return

                    target_id, target_name = resolved
                    actor_name = (
                        self._known_username(int(user_id)) or str(user_id)
                    )
                    try:
                        deleted = self.ratings.purge_ratings_for_user(target_id)
                    except Exception:
                        log.exception(
                            "could not permanently purge ratings for user %s",
                            target_id,
                        )
                        self.say("Couldn't purge those ratings right now.")
                        return

                    self.say(
                        f"Purged {target_name} ({target_id}): "
                        f"{deleted['real']} real + {deleted['shadow']} shadow "
                        "rating(s) permanently deleted. Rating policy unchanged."
                    )
                    log.warning(
                        "ADMIN %s:%s permanently purged map ratings for %s:%s "
                        "(real=%d shadow=%d)",
                        actor_name, user_id, target_name, target_id,
                        deleted["real"], deleted["shadow"],
                    )

                else:
                    self.say(
                        f"Usage: {pfx}ratingignore add <username|id> | "
                        f"{pfx}ratingignore remove <username|id> | "
                        f"{pfx}ratingignore list | "
                        f"{pfx}ratingignore purge <username|id> confirm"
                    )

            elif cmd == "usercommands":
                if len(parts) != 2:
                    self.say(
                        f"Public user commands are "
                        f"{'enabled' if self.cfg.get('user_commands_enabled', True) else 'disabled'}. "
                        f"Usage: {p}usercommands on/off"
                    )
                else:
                    try:
                        enabled = self._parse_on_off(parts[1])
                    except ValueError as e:
                        self.say(f"Usage: {p}usercommands on/off ({e})")
                    else:
                        saved = self._set_persistent_toggle(
                            "user_commands_enabled", enabled
                        )
                        if not enabled:
                            self._clear_skip_vote("public commands disabled")
                        self.say(
                            self._toggle_reply("Public user commands", "enabled" if enabled else "disabled", saved)
                        )

            elif cmd == "silence":
                if len(parts) != 2:
                    self.say(
                        f"Bot chat is "
                        f"{'silenced' if self.cfg.get('bot_chat_silenced', False) else 'normal'}. "
                        f"Usage: {p}silence on/off"
                    )
                else:
                    try:
                        silenced = self._parse_on_off(parts[1])
                    except ValueError as e:
                        self.say(f"Usage: {p}silence on/off ({e})")
                    else:
                        saved = self._set_persistent_toggle(
                            "bot_chat_silenced", silenced
                        )
                        self.say(
                            self._toggle_reply("Bot automatic chat", "silenced" if silenced else "restored", saved)
                        )

            elif cmd == "voteskip":
                if len(parts) != 2:
                    self.say(
                        f"Vote-skip is "
                        f"{'enabled' if self.cfg.get('voteskip_enabled', True) else 'disabled'}. "
                        f"Usage: {p}voteskip on/off"
                    )
                else:
                    try:
                        enabled = self._parse_on_off(parts[1])
                    except ValueError as e:
                        self.say(f"Usage: {p}voteskip on/off ({e})")
                    else:
                        saved = self._set_persistent_toggle(
                            "voteskip_enabled", enabled
                        )
                        if not enabled:
                            self._clear_skip_vote("vote-skip disabled by admin")
                        self.say(
                            self._toggle_reply("Vote-skip", "enabled" if enabled else "disabled", saved)
                        )

            elif cmd == "set":
                self.handle_admin_set(parts)
            elif cmd == "start":
                if self._single_player_start_hold_uid is not None:
                    log.info(
                        "admin .start cleared single-player start hold for user %s",
                        self._single_player_start_hold_uid,
                    )
                    self._single_player_start_hold_uid = None
                if self._first_player_download_wait_uid is not None:
                    log.info(
                        "admin .start cleared first-player download wait for user %s",
                        self._first_player_download_wait_uid,
                    )
                    self._first_player_download_wait_uid = None
                self.start_match()
            elif cmd == "abort":
                self._explicit_abort_pending = True
                self.hub.abort_match(self.room_id)
            elif cmd == "title":
                if len(parts) < 2:
                    self.say(
                        f"Usage: {p}title <new lobby title> "
                        f"(difficulty mode, {self.pool.sr_min:.2f}-"
                        f"{self.pool.sr_max:.2f}* range, and version are added "
                        "automatically)"
                    )
                else:
                    # Keep the command responsible only for the human-readable
                    # title/template. _room_name_for_range() owns the dynamic
                    # difficulty-mode, star-rating, and version portions.
                    title = " ".join(parts[1:]).strip()
                    self._room_name_template = title
                    self.cfg["room_name"] = title
                    saved = self._persist_config_updates({"room_name": title})
                    renamed_now = self.update_room_name_for_range(
                        self.pool.sr_min, self.pool.sr_max
                    )
                    if renamed_now:
                        self.say(
                            f"Lobby title changed to: {self._current_room_name}."
                            f"{self._persist_suffix(saved)}"
                        )
                    else:
                        self.say(
                            "Lobby title saved; it will update between maps."
                            f"{self._persist_suffix(saved)}"
                        )
            elif cmd == "softrestart":
                self.request_soft_restart()
            elif cmd == "hardrestart":
                if len(parts) > 2:
                    self.say(f"Usage: {p}hardrestart [maps]")
                elif len(parts) == 1:
                    self.hard_restart_room()
                else:
                    try:
                        maps_ahead = self._parse_admin_int(parts[1], 0, 100)
                    except ValueError as e:
                        self.say(f"Usage: {p}hardrestart [maps] ({e})")
                    else:
                        if maps_ahead == 0:
                            self.hard_restart_room()
                        else:
                            self.queue_hard_restart(
                                maps_ahead, reason="manual"
                            )
            elif cmd == "close":
                self.say("Closing lobby.")
                self.running = False
            elif cmd == "shutdown":
                if self._discord_command_reply_channel_id is None:
                    self.say(
                        f"Use {p}close for this lobby. System shutdown is "
                        "Discord-only."
                    )
                elif len(parts) != 2:
                    self.say(
                        f"Usage: {p}shutdown all or {p}shutdown <room>"
                    )
                else:
                    requested = parts[1].strip().casefold()
                    current = str(
                        self.cfg.get("discord_control_room_alias")
                        or self.cfg.get("_instance_name")
                        or "room1"
                    ).strip().casefold()
                    known = {
                        str(value).strip().casefold()
                        for value in self.cfg.get(
                            "discord_control_known_room_aliases",
                            ["room1", "room2"],
                        ) or []
                        if str(value).strip()
                    }
                    known.add(current)
                    target = current if requested in {"this", "room"} else requested
                    if target != "all" and target not in known:
                        self.say(
                            "Unknown room. Use: "
                            + ", ".join(sorted(known))
                            + ", or all."
                        )
                    else:
                        actor = self._known_username(int(user_id)) or str(user_id)
                        self.process_control.request_shutdown(
                            target, requested_by=actor
                        )
                        if target == "all":
                            self.say("Shutting down all lobby rooms.")
                        else:
                            self.say(f"Shutting down {target}.")
                        if target in {"all", current}:
                            self.running = False
            elif cmd in ("closewhenempty", "closeempty"):
                if len(parts) > 2 or (
                    len(parts) == 2
                    and parts[1].casefold() not in {"cancel", "off", "stop"}
                ):
                    self.say(
                        f"Usage: {p}closewhenempty "
                        f"or {p}closewhenempty cancel"
                    )
                elif len(parts) == 2:
                    if self._close_when_empty:
                        self._close_when_empty = False
                        self.say("Close-when-empty cancelled.")
                    else:
                        self.say("Close-when-empty is not armed.")
                else:
                    self._close_when_empty = True
                    if not self._close_if_armed_and_empty():
                        self.say(
                            "Lobby will close once all non-referee players leave. "
                            f"Use {p}closewhenempty cancel to stop it."
                        )
            elif cmd == "range":
                if len(parts) != 3:
                    self.say(f"Usage: {p}range 4.5 5.5")
                else:
                    try:
                        lo, hi = float(parts[1]), float(parts[2])
                        if not math.isfinite(lo) or not math.isfinite(hi) or not 0 <= lo < hi:
                            raise ValueError('star range must be finite, nonnegative and increasing')
                        self._set_manual_range_operator(lo, hi, user_id=user_id)
                    except ValueError:
                        self.say(f"Usage: {p}range 4.5 5.5")
            elif cmd == "auto":
                self._enable_auto_difficulty_operator()

    # ------------------------------------------------------ admin restarts

    def _clear_match_tracking(self) -> None:
        """Forget transient match/rating state without touching the room."""
        self.ready.clear()
        self.starting_map = None
        self.starting_item_id = None
        self._clear_start_recovery_tracking()
        self.playing_map = None
        self.playing_item_id = None
        self.playing_player_ids.clear()
        self.scoring_item_id = None
        self._match_started_at = 0.0
        self._explicit_abort_pending = False
        self._early_abort_pending = None
        self._start_immediate_retry = False
        self.last_played_map = None
        self._rating_context = {}
        self._rating_window_until = 0.0
        self._rating_prompt_pending = False
        self._recent_rating_map_ids.clear()
        self._activity_match_key = None
        self._clear_skip_vote("match tracking reset")
        self._clear_replay_vote("match tracking reset")
        self._clear_difficulty_vote("match tracking reset")

    def request_soft_restart(self) -> None:
        """Rebuild the current room's playlist without closing the lobby."""
        self._clear_skip_vote("soft restart")
        self._clear_replay_vote("soft restart")
        if self.state == STARTING:
            # Playlist edits are illegal during a match-start countdown. Stop
            # it first, then rebuild immediately in the same room.
            self.say("Soft restart requested — cancelling countdown.")
            self._stop_match_countdown()
            self.starting_map = None
            self.starting_item_id = None
            self._clear_start_recovery_tracking()
            self.ready.clear()
            self.set_state(IDLE)
            self.perform_soft_restart()
            return

        if self.state in (PLAYING, SCORING):
            self._pending_soft_restart = True
            self.say(
                "Soft restart queued — playlist will be rebuilt "
                "after this map."
            )
            return

        self.perform_soft_restart()

    def perform_soft_restart(self, announce: bool = True) -> bool:
        """Clear future unplayed entries and seed a fresh 3-map window.

        Played/history items are deliberately left alone because osu! rejects
        attempts to remove them. The public room snapshot is consulted first so
        a stale local index cannot make us edit/remove the wrong playlist item.
        """
        if not self.room_id:
            return False
        if self.state in (STARTING, PLAYING):
            self._pending_soft_restart = True
            return False

        self._pending_soft_restart = False
        self._difficulty_vote_changes_since_match = 0
        self._difficulty_vote_rebuild_pending = False
        self.set_state(IDLE)

        # Rebuild our local mirror from osu!'s room state before touching the
        # playlist. This fixes the old failure mode where current_item_id still
        # pointed at a played item and .softrestart tried to remove it.
        self.reconcile_playlist(
            reason="pre-softrestart", fetch_remote=True,
            ensure_depth=False, trim_extras=False,
        )
        if self.state in (STARTING, PLAYING):
            self._pending_soft_restart = True
            return False

        # Difficulty/title updates accumulated during the previous map should
        # take effect before choosing the fresh playlist.
        self.apply_pending_auto_retune()
        self.apply_pending_room_name_update()

        keep_id = self.current_item_id
        remove_ids = []
        for item_id, _m in list(self.queued):
            if item_id is not None and item_id != keep_id:
                remove_ids.append(int(item_id))

        removed = 0
        for item_id in remove_ids:
            try:
                self._referee_operation_with_rejoin(
                    "RemovePlaylistItem",
                    lambda item_id=item_id: self.hub.remove_playlist_item(
                        self.room_id, item_id),
                )
                removed += 1
                self._server_playlist_items.pop(item_id, None)
                self._playlist_maps.pop(item_id, None)
            except HubTransportError:
                raise
            except Exception as e:
                log.debug(
                    "soft restart could not remove future playlist item %s: %s",
                    item_id, e,
                )

        self.queued.clear()

        fresh = self._next_downloadable_map()
        if not fresh:
            log.error("soft restart failed: map pool is empty")
            if announce:
                self.say("Soft restart failed — map pool is empty.")
            return False

        try:
            self.apply_map(fresh, announce=announce)
            self.fill_queue()
        except HubTransportError:
            raise
        except Exception as e:
            log.exception("soft restart failed while rebuilding playlist")
            if announce:
                self.say(f"Soft restart failed: {e}")
            return False

        self._clear_match_tracking()
        self._playlist_dirty = True
        self._last_playlist_audit = 0.0
        self.set_state(IDLE)
        log.info(
            "soft restart complete in room %s: removed %d future item(s), "
            "target window current + %d queued",
            self.room_id, removed, int(self.cfg.get("queue_ahead", 2)),
        )
        if announce:
            self.say(
                f"Soft restart complete — fresh current map + "
                f"{int(self.cfg.get('queue_ahead', 2))} queued."
            )
        return True

    def _reset_room_runtime_state_for_reopen(self) -> None:
        """Reset per-room state after CloseRoom, preserving config/pool/title."""
        self._room_ready = False
        self._empty_since = None
        self._empty_reset_done = False
        self._empty_reset_retry_at = 0.0
        self._last_countdown_ready_notice = None
        self._flush_all_lobby_time()
        self._detach_chat_websocket()
        self.room_id = None
        self.channel_id = None
        self._hub_rejoin_pending = False
        self._room_missing_confirmations = 0
        self.current_map = None
        self.current_item_id = None
        self._countdown_wait_item_id = None
        self._countdown_wait_beatmap_id = None
        self._countdown_wait_requesters.clear()
        self._countdown_wait_delay_seconds = 0.0
        self._countdown_wait_locked_item_id = None
        self._countdown_wait_locked_beatmap_id = None
        self._countdown_wait_force_item_id = None
        self._countdown_wait_force_beatmap_id = None
        self._mid_match_intro_sent.clear()
        self._server_playlist_items.clear()
        self._standard_playlist_edits = {}
        self._server_current_item_id = None
        self._last_playlist_audit = 0.0
        self._playlist_dirty = True
        self._playlist_maps.clear()
        self.queued.clear()

        self.players.clear()
        getattr(self, "_pending_player_profiles", {}).clear()
        getattr(self, "_unready_start_grace_users", set()).clear()
        self._recalled_window = None
        self._preference_choice = None
        self._preference_played = None
        self._preference_started = None
        self.referee_ids.clear()
        self._clear_match_tracking()
        self._clear_community_difficulty("hard restart/new room")
        if self._fixed_difficulty_mode():
            lo, hi, length = self._fixed_baseline_window()
            if self.pool.set_window(lo, hi, length) <= 0:
                log.warning(
                    "could not restore fixed JSON range %.2f-%.2f* while "
                    "reopening room; previous known-good pool kept", lo, hi,
                )
        self._fixed_difficulty_dirty = False
        self._fixed_range_rebuild_pending = False
        self._completed_map_count = 0
        self._rating_prompt_map_count = 0
        self._post_match_pp_announce_pending = False
        self._post_match_pp_completed_beatmap_id = None

        self._pending_room_name_range = None
        self._pending_soft_restart = False
        self._auto_retune_pending = False
        self._room_opened_at = 0.0
        self._automatic_hard_restart_due_at = 0.0
        self._pending_hard_restart_maps = None
        self._pending_hard_restart_reason = ""

        self.last_message_id = 0
        self._seen_chat_message_ids.clear()
        self._seen_chat_message_order.clear()
        self._chat_patrol_since = 0
        while True:
            try:
                self._chat_patrol_inbox.get_nowait()
            except queue.Empty:
                break
        self._chat_patrol_wake.set()
        self._own_messages.clear()
        self._last_chat_poll = 0.0
        self._last_ack = 0.0
        self._last_sent = 0.0
        self.set_state(IDLE)

    def hard_restart_room(
        self,
        *,
        announce: bool = True,
        close_delay_seconds: float = 0.0,
        close_reason: str = "hard_restart",
    ) -> bool:
        """Close the existing room and create a completely new one.

        ``close_delay_seconds`` intentionally happens *after* the player-facing
        final warning but before CloseRoom, so players have time to read it.
        """
        old_room_id = self.room_id
        if not old_room_id:
            return False

        if close_reason == 'automatic_hard_restart':
            now = time.monotonic()
            if now < getattr(self, '_hard_restart_admission_retry_at', 0.):
                return False
            ready = getattr(self.hub, 'refresh_ready', None)
            if callable(ready) and ready() is not True:
                self._hard_restart_admission_retry_at = now + 5.
                log.info('automatic hard restart postponed before close: ownership read admission unavailable')
                return False

        # This intentionally works even during PLAYING: unlike a soft restart,
        # a hard restart destroys the room, so no playlist/settings mutation is
        # attempted in the active room state.
        if announce:
            try:
                self.say("Hard restarting lobby — this room will close now.")
            except Exception:
                log.debug("could not announce hard restart", exc_info=True)

        # Planned automatic refreshes only last a few seconds. Keep the
        # channel-name indicator green so Discord's per-channel rename cooldown
        # cannot leave a healthy replacement room falsely marked red for
        # several minutes. The status card still shows Restarting/Closed during
        # the brief handoff. Manual hard restarts retain the normal red marker.
        self._update_discord_public_status(
            force=True,
            is_open=False,
            lifecycle="Restarting",
            rename_channel=(close_reason != "automatic_hard_restart"),
        )

        try:
            delay = self._external_float(close_delay_seconds, 0.0, 30.0)
        except (TypeError, ValueError):
            delay = 0.0
        if delay > 0:
            time.sleep(delay)

        try:
            self.hub.close_room(old_room_id)
            log.info("hard restart closed room %s", old_room_id)
            try:
                self.ratings.record_match_end(
                    match_key=self._activity_match_key,
                    room_id=old_room_id,
                    playlist_item_id=self.playing_item_id,
                    result="room_closed",
                )
                self.ratings.record_room_close(old_room_id, close_reason)
            except Exception:
                log.exception("could not finalize hard-restart activity history")
        except HubTransportError:
            # CloseRoom may already have reached osu! before the transport
            # failed. Do not create a second room blindly; recovery probes the
            # old room first and recreates only after confirmed 404s.
            raise
        except Exception as e:
            log.error(
                "hard restart refused because old room %s could not be "
                "closed: %s",
                old_room_id, e,
            )
            try:
                self.say("Hard restart failed — old lobby was left open.")
            except Exception:
                pass
            return False

        # Ignore late events from the destroyed room once the new room exists.
        self._reset_room_runtime_state_for_reopen()
        time.sleep(1.0)

        try:
            self.create_room()
            self.set_state(IDLE)
            log.info(
                "hard restart complete: old room %s -> new room %s",
                old_room_id, self.room_id,
            )
            return True
        except Exception:
            # create_room closes a half-created room on failure. Let the
            # process/service fail rather than silently run with no lobby;
            # systemd's Restart=on-failure can then recover on the VPS.
            log.exception(
                "hard restart could not create replacement for room %s",
                old_room_id,
            )
            raise

    def _open_current_room_in_lazer(self) -> None:
        """Queue a forced local lazer join for a newly-created room."""
        if not self.cfg.get("lazer_rejoin_on_hard_restart", False):
            return
        if not self.room_id:
            return
        coordinator = getattr(self, "lazer_coordinator", None)
        if coordinator is None:
            coordinator = LazerCoordinator(self.cfg)
            self.lazer_coordinator = coordinator
        coordinator.ensure_room(
            int(self.room_id),
            reason="new room startup",
            force=True,
        )

    # ------------------------------------------------------------ room

    @staticmethod
    def _strip_dynamic_room_title_parts(name: str) -> str:
        """Remove title fragments now owned by the live renderer.

        Older room JSONs commonly embed ``(Auto Difficulty)``,
        ``(Set Difficulty)``, an SR range, and ``[vX.Y.Z]`` directly in
        ``room_name``. Keeping those JSONs valid makes this migration painless:
        the renderer strips stale dynamic fragments and writes the current
        values back into the *live* title without touching the room file.
        """
        text = str(name or "").strip()
        text = re.sub(
            r"\(\s*(?:auto|set)\s+difficulty\s*\)",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\(\s*\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?\s*\*\s*\)",
            "",
            text,
            count=1,
        )
        text = re.sub(
            r"\[\s*v\s*\d+(?:\.\d+)+(?:[-+][^\]]+)?\s*\]",
            "",
            text,
            flags=re.IGNORECASE,
        )
        # Dynamic-fragment removal can leave doubled spaces around separators.
        text = re.sub(r"[ \t]+", " ", text).strip()
        return text

    def _room_difficulty_mode_label(self) -> str:
        if bool(self.cfg.get("auto_difficulty", True)) and not self.manual_range:
            return "Auto Difficulty"
        return "Set Difficulty"

    def _room_version_suffix(self) -> str:
        raw = str(self.cfg.get("version", "") or "").strip()
        if not raw:
            return ""
        # Be forgiving when universal.json contains "6.4.8", "v6.4.8", or
        # even the already-formatted "[v6.4.8]".
        if raw.startswith("[") and raw.endswith("]"):
            raw = raw[1:-1].strip()
        if raw[:1].casefold() == "v":
            raw = raw[1:].strip()
        if not raw:
            return ""
        return f"[v{raw}]"

    def _room_name_for_range(self, lo: float, hi: float) -> str:
        """Render mode + active SR range + one shared version suffix.

        ``room_name`` is now a stable human-authored base. The current mode is
        derived from ``.auto``/``.range`` state, ``version`` normally comes
        from universal.json, and both are rendered dynamically. Legacy
        per-room mode/range/version text is stripped before rendering.
        """
        label = f"({lo:.2f}-{hi:.2f}*)"
        mode = f"({self._room_difficulty_mode_label()})"
        version = self._room_version_suffix()
        base = self._strip_dynamic_room_title_parts(self._room_name_template)
        if not base:
            base = "AutoHost"

        def compose(base_text: str) -> str:
            name = base_text
            # Put the mode label before the first pipe, matching the established
            # "AutoHost (Auto Difficulty) |Type .help|" title style. Custom
            # titles without a pipe simply receive the mode label at the end.
            pipe = name.find("|")
            if pipe >= 0:
                left = name[:pipe].rstrip()
                right = name[pipe:].lstrip()
                name = f"{left} {mode} {right}".strip()
            else:
                name = f"{name.rstrip()} {mode}".strip()

            # Preserve non-version bracketed suffixes (for example [ALPHA]) by
            # keeping the SR range before them. The version itself is appended
            # only after the full rendered title, guaranteeing it is always last.
            bracket = name.find("[")
            if bracket >= 0:
                left = name[:bracket].rstrip()
                right = name[bracket:].lstrip()
                name = f"{left} {label} {right}".strip()
            else:
                name = f"{name.rstrip()} {label}".strip()

            suffix = f" {version}" if version else ""
            return f"{name}{suffix}".strip()

        rendered = compose(base)
        # osu! room names are limited to 100 characters. If a custom base is
        # unusually long, trim only that base and re-render so mode/range and
        # the version-at-end invariant survive intact.
        while len(rendered) > 100 and len(base) > 1:
            excess = max(1, len(rendered) - 100)
            base = base[:-excess].rstrip() or base[:1]
            rendered = compose(base)
        return rendered[:100]

    def update_room_name_for_range(
            self, lo: float | None = None,
            hi: float | None = None) -> bool:
        """Synchronize the room title, deferring when room state is unsafe.

        osu!'s referee server rejects ChangeRoomSettings while a match or
        match-start countdown is active. Range changes are therefore allowed
        immediately for future map selection, but title changes are queued
        until SCORING/IDLE.

        Returns True when the live title is already synchronized or was
        changed now. Returns False when the update is deferred/retry-pending.
        """
        if not self.room_id:
            return False

        lo = self.pool.sr_min if lo is None else float(lo)
        hi = self.pool.sr_max if hi is None else float(hi)
        new_name = self._room_name_for_range(lo, hi)

        if new_name == self._current_room_name:
            self._pending_room_name_range = None
            return True

        # Do not even ask the server in these states. Error 9 is expected if
        # ChangeRoomSettings is attempted during gameplay/countdown.
        if self.state in (STARTING, PLAYING):
            self._pending_room_name_range = (lo, hi)
            log.info(
                "deferring room rename to %.2f-%.2f* until between maps "
                "(state=%s)",
                lo, hi, self.state,
            )
            return False

        try:
            self.hub.change_room_settings(self.room_id, name=new_name)
            self._current_room_name = new_name
            self._pending_room_name_range = None
            try:
                self.ratings.update_room_name(self.room_id, new_name)
            except Exception:
                log.exception("could not update room title in activity history")
            log.info("room renamed for active range: %s", new_name)
            return True
        except HubTransportError:
            self._pending_room_name_range = (lo, hi)
            raise
        except Exception as e:
            # Keep the desired title queued. A server-side settings race should
            # not lose the requested name. Transport failures are handled by
            # the room recovery path above, not swallowed here.
            self._pending_room_name_range = (lo, hi)
            log.warning(
                "could not update room name for %.2f-%.2f*: %s; "
                "will retry at the next safe between-map boundary",
                lo, hi, e,
            )
            return False

    def apply_pending_room_name_update(self) -> bool:
        """Apply a queued room title only during a safe between-map state."""
        if self._pending_room_name_range is None:
            return True

        if self.state not in (SCORING, IDLE):
            return False

        lo, hi = self._pending_room_name_range
        return self.update_room_name_for_range(lo, hi)

    def _map_is_downloadable(self, m, *, force: bool = False) -> bool:
        """Return False only when osu! explicitly reports downloads disabled.

        Unknown/API-failure results fail open so a transient REST problem never
        empties or stalls the lobby. Explicit available/disabled results are
        retained in ratings.db for the configured number of days and shared by
        both rooms. Incomplete metadata is only assumed available for five
        minutes. Disabled sets are excluded from runtime selection immediately.
        """
        if m is None:
            return False

        beatmap_id = int(m.beatmap_id)
        now = time.time()
        try:
            persistent_seconds = self._external_float(
                self.cfg.get("download_availability_cache_days", 90.0),
                0.0, 365.0,
            ) * 86400.0
        except (TypeError, ValueError):
            persistent_seconds = 90.0 * 86400.0

        # A force check still trusts a recent *explicit* osu! availability
        # result. It only bypasses short-lived fail-open assumptions.
        try:
            persistent = self.ratings.cached_download_availability(
                beatmap_id, persistent_seconds
            )
        except sqlite3.Error:
            log.exception("download cache unavailable for beatmap %s; using memory/API", beatmap_id)
            persistent = None
        if persistent is not None:
            available, checked_at = persistent
            self._download_availability_cache[beatmap_id] = (
                bool(available), checked_at, True
            )
            if not available:
                self.pool.exclude_beatmaps([beatmap_id])
            return bool(available)

        cached = self._download_availability_cache.get(beatmap_id)
        if cached:
            available, checked_at, confirmed = cached
            ttl = persistent_seconds if confirmed else 300.0
            if (not available) or ((confirmed or not force) and (now - checked_at) <= ttl):
                return bool(available)

        try:
            beatmap = self.api.beatmap(beatmap_id)
        except Exception:
            log.exception(
                "download-availability lookup failed for beatmap %s; "
                "allowing it because availability is unknown",
                beatmap_id,
            )
            return True

        if not isinstance(beatmap, dict):
            log.warning(
                "download-availability lookup returned no metadata for "
                "beatmap %s; allowing it because availability is unknown",
                beatmap_id,
            )
            return True

        beatmapset = beatmap.get("beatmapset") or {}
        set_id = (
            beatmap.get("beatmapset_id")
            or beatmapset.get("id")
        )
        try:
            set_id = int(set_id) if set_id is not None else None
        except (TypeError, ValueError):
            set_id = None

        set_data = beatmapset if isinstance(beatmapset, dict) else {}
        availability = set_data.get("availability") or {}

        if set_id is not None:
            set_cached = self._download_set_cache.get(set_id)
            if set_cached:
                available, checked_at, confirmed = set_cached
                ttl = persistent_seconds if confirmed else 300.0
                if (not available) or ((confirmed or not force) and (now - checked_at) <= ttl):
                    self._download_availability_cache[beatmap_id] = (
                        bool(available), checked_at, confirmed
                    )
                    return bool(available)

        # GET /beatmaps/{id} does not always include all BeatmapsetExtended
        # attributes in every API context. Fall back to the explicit set
        # endpoint when the availability object is missing.
        if (
            set_id is not None
            and "download_disabled" not in availability
        ):
            try:
                full_set = self.api.beatmapset(set_id)
            except Exception:
                full_set = None
                log.exception(
                    "beatmapset availability lookup failed for set %s",
                    set_id,
                )
            if isinstance(full_set, dict):
                set_data = full_set
                availability = full_set.get("availability") or {}

        disabled = availability.get("download_disabled")
        if disabled is None:
            # Availability was not explicitly reported. Do not blacklist a
            # playable map because of incomplete metadata.
            self._download_availability_cache[beatmap_id] = (True, now, False)
            return True

        available = not bool(disabled)
        self._download_availability_cache[beatmap_id] = (available, now, True)
        if set_id is not None:
            self._download_set_cache[set_id] = (available, now, True)

        set_beatmap_ids: list[int] = []
        for row in (set_data.get("beatmaps") or []):
            try:
                set_beatmap_ids.append(int(row.get("id")))
            except (AttributeError, TypeError, ValueError):
                continue
        if beatmap_id not in set_beatmap_ids:
            set_beatmap_ids.append(beatmap_id)

        try:
            self.ratings.remember_download_availability(
                set_beatmap_ids,
                beatmapset_id=set_id,
                available=available,
                checked_at=now,
            )
        except Exception:
            log.exception("could not persist download availability for beatmap %s", beatmap_id)

        if available:
            return True

        self.pool.exclude_beatmaps(set_beatmap_ids)
        for bid in set_beatmap_ids:
            self._download_availability_cache[int(bid)] = (False, now, True)

        if bool(getattr(self.pool, "loved_mode", False)):
            pool_source = "loved"
        elif int(self.cfg.get("ruleset_id", 0) or 0) == 3:
            pool_source = "mania"
        else:
            pool_source = str(
                getattr(self.pool, "source_mode", "standard") or "standard"
            )
        try:
            self.ratings.record_download_quarantine(
                beatmap_id=beatmap_id,
                beatmapset_id=set_id,
                room_alias=str(
                    self.cfg.get("_instance_name")
                    or self.cfg.get("discord_control_room_alias")
                    or "unknown"
                ),
                pool_source=pool_source,
                excluded_count=len(set_beatmap_ids),
            )
        except Exception:
            log.exception(
                "could not record download quarantine event for beatmap %s",
                beatmap_id,
            )

        more_info = str(
            (availability or {}).get("more_information") or ""
        ).strip()
        log.warning(
            "beatmap %s/set %s is download-disabled%s; excluding %d "
            "difficulty/difficulties from runtime selection",
            beatmap_id,
            set_id,
            f" ({more_info})" if more_info else "",
            len(set_beatmap_ids),
        )
        return False

    def _record_mania_round(self, item):
        if item is not None and callable(getattr(getattr(self, 'pool', None), 'record_round', None)):
            self.pool.record_round(item.beatmap_id)

    def _next_downloadable_map(self, max_attempts: int = 30, *, native_only=False):
        """Pick until a map is either confirmed/assumed downloadable."""
        if not self._refresh_operator_exclusions():
            return None
        for _ in range(max(1, int(max_attempts))):
            if callable(getattr(self.pool, 'reserve_playlist_maps', None)):
                reserved = [m.beatmap_id for _item_id, m in getattr(self, "queued", [])]
                current = getattr(self, "current_map", None)
                if current is not None:
                    reserved.append(current.beatmap_id)
                for name in ("playing_map", "starting_map"):
                    active = getattr(self, name, None)
                    if active is not None:
                        reserved.append(active.beatmap_id)
                self.pool.reserve_playlist_maps(reserved)
            m = (self.pool.next_map(native_only=True) if native_only and getattr(self.pool, 'mixed_dt', False)
                 else self.pool.next_map())
            if m is None:
                return None
            if getattr(m, 'is_dt', False):
                # MakeRoom has no atomic required-mod setting. Always seed NM.
                if native_only:
                    continue
                try:
                    self._playlist_rules_for_map(m)
                except ValueError as exc:
                    log.warning('rejecting DT candidate %s: %s', m.beatmap_id, exc)
                    self.pool.exclude_dt_beatmap(m.beatmap_id)
                    continue
                if not self._check_dt_revision(m):
                    if (getattr(self.pool, 'dt_full', False)
                            and time.time() < getattr(self, '_dt_revision_retry_at', 0)):
                        return self._next_downloadable_map(max_attempts, native_only=True)
                    continue
            if getattr(m, "is_mania_convert", False):
                if native_only:
                    # MakeRoom cannot atomically set required key mods. Seed
                    # it with native Mania; verified converts can follow via
                    # Add/EditPlaylistItem with explicit conversion settings.
                    continue
                try:
                    self._playlist_rules_for_map(m)
                except ValueError as exc:
                    log.warning("rejecting Mania convert %s before queueing: %s", m.beatmap_id, exc)
                    self.pool.exclude_beatmap(m.beatmap_id)
                    continue
            if self._map_is_downloadable(m):
                return m
        log.error(
            "could not find a downloadable map after %d candidate picks",
            max_attempts,
        )
        if getattr(self.pool, 'dt_full', False) and not native_only:
            return self._next_downloadable_map(max_attempts, native_only=True)
        return None

    def _refresh_operator_exclusions(self):
        path = str(self.cfg.get('beatmap_exclusions_file') or '').strip()
        if not path:
            return True
        policy = getattr(self, '_operator_exclusion_policy', None)
        if policy is None or str(policy.path) != path:
            policy = self._operator_exclusion_policy = ExclusionList(path)
        policy.refresh()
        if not policy.valid:
            return False  # A configured policy must load before any new map.
        apply = getattr(self.pool, 'apply_operator_exclusions', None)
        if not callable(apply):
            return False
        changed = apply(policy.ids)
        if changed:
            self._operator_exclusion_audit_pending = True
            log.info('Hot-loaded %d operator chart exclusions', len(policy.ids))
        return True

    def _operator_map_allowed(self, m):
        if not self.cfg.get('beatmap_exclusions_file'):
            return True
        return (self._refresh_operator_exclusions()
                and m.beatmap_id not in self._operator_exclusion_policy.ids)

    def _cleanup_operator_exclusions(self):
        """Only an audited idle playlist can be edited; never touch gameplay."""
        if not self.cfg.get('beatmap_exclusions_file'):
            return True
        if self.state not in (IDLE, SCORING):
            return True
        blocked = getattr(self.pool, '_operator_excluded', set())
        current = self._server_playlist_items.get(self.current_item_id, {})
        if getattr(self.current_map, 'beatmap_id', None) in blocked:
            if not current or current.get('played') or current.get('expired'):
                return False
            replacement = self._next_downloadable_map()
            if replacement is None:
                log.warning('Excluded current chart has no eligible replacement; holding start')
                return False
            self.apply_map(replacement, announce=True)
        for item_id, m in list(self.queued):
            if m.beatmap_id not in blocked:
                continue
            known = self._server_playlist_items.get(item_id, {})
            if item_id is None or not known or known.get('played') or known.get('expired'):
                return False
            self._referee_operation_with_rejoin('RemovePlaylistItem',
                lambda item_id=item_id: self.hub.remove_playlist_item(self.room_id, item_id))
            self._server_playlist_items.pop(item_id, None)
            self._playlist_maps.pop(item_id, None)
            self.queued = [pair for pair in self.queued if pair[0] != item_id]
        self._operator_exclusion_audit_pending = False
        return True

    def _dt_fixed_mods_supported(self):
        return not (set(self.cfg.get('required_mods', [])) - {'NF'})

    def _dt_selection_enabled(self):
        return (getattr(self.pool, 'mixed_dt', False) and self.pool.dt_enabled()
                and self.pool.dt_catalog_ready()
                and self._dt_fixed_mods_supported())

    def _check_dt_revision(self, m, *, force=False):
        """Fail closed on revision/ruleset uncertainty; never trust a stale .osu."""
        if not getattr(m, 'is_dt', False):
            return True
        if not force and time.time() < getattr(self, '_dt_revision_retry_at', 0):
            return False
        cache = getattr(self, '_dt_revision_cache', {})
        self._dt_revision_cache = cache
        cached = cache.get(m.beatmap_id)
        if not force and cached and time.time()-cached[0] < 300 and cached[1] == m.dt_checksum:
            return True
        try:
            info = self.api.beatmap(int(m.beatmap_id))
            valid = (isinstance(info, dict) and info.get('checksum') == m.dt_checksum
                     and info.get('mode_int') == 0 and info.get('status') == 'ranked')
        except Exception as exc:
            log.warning('DT revision check unavailable for %s: %s', m.beatmap_id, exc)
            self._dt_revision_retry_at = time.time()+30
            return False
        if valid:
            cache[m.beatmap_id] = (time.time(), m.dt_checksum)
            return True
        cache.pop(m.beatmap_id, None)
        self.pool.exclude_dt_beatmap(m.beatmap_id)
        log.error('DT revision/ruleset mismatch for %s; excluding only its DT variant', m.beatmap_id)
        return False

    @staticmethod
    def _dt_pp_kwargs(m):
        return {'mods': ('DT',)} if getattr(m, 'is_dt', False) else {}

    def _prefetch_map_pp(self, m):
        self.pp_projection.prefetch(m.beatmap_id, **self._dt_pp_kwargs(m))

    def _cleanup_disabled_dt(self):
        """Remove unplayed DT only, never mutate a running/uncertain match."""
        if not getattr(self.pool, 'mixed_dt', False) or self._dt_selection_enabled():
            return True
        if self.state not in (IDLE, SCORING):
            return True
        active = [item for item in self._server_playlist_items.values()
                  if not item.get('played') and not item.get('expired')]
        dt_ids = {int(item['id']) for item in active
                  if 'DT' in item.get('required_mods', [])
                  or getattr(self._playlist_maps.get(int(item['id'])), 'is_dt', False)}
        dt_ids.update(int(item_id) for item_id, m in self.queued
                      if item_id is not None and getattr(m, 'is_dt', False))
        current_dt = getattr(self.current_map, 'is_dt', False)
        if current_dt and self.current_item_id is not None:
            entry = self._server_playlist_items.get(int(self.current_item_id), {})
            if not entry or (not entry.get('played') and not entry.get('expired')):
                dt_ids.add(int(self.current_item_id))
        for item_id in sorted(dt_ids - {self.current_item_id}):
            # A completed history row is never a removal target.
            known = self._server_playlist_items.get(item_id, {})
            if known.get('played') or known.get('expired'):
                continue
            try:
                self._referee_operation_with_rejoin('RemovePlaylistItem',
                    lambda item_id=item_id: self.hub.remove_playlist_item(self.room_id, item_id))
            except HubTransportError:
                raise
            except Exception as exc:
                log.warning('DT opt-out cleanup held on item %s: %s', item_id, exc)
                return False
            self._server_playlist_items.pop(item_id, None)
            self._playlist_maps.pop(item_id, None)
            self.queued = [pair for pair in self.queued if pair[0] != item_id]
        if self.current_item_id in dt_ids:
            replacement = self._next_downloadable_map()
            if replacement is None or getattr(replacement, 'is_dt', False):
                log.warning('DT opt-out: no eligible NM replacement; holding start')
                return False
            self.apply_map(replacement, announce=True)
        return not any(getattr(m, 'is_dt', False) for _, m in self.queued)

    def _refresh_full_dt_playlist(self):
        """Replace unplayed NM only after an authoritative, safe room audit.

        Stage a verified DT candidate before removing anything. Existing DT,
        history and NM fallback entries are retained. Future replacements are
        appended (the API cannot edit a future entry), never blindly retried.
        """
        if not getattr(self, '_dt_full_refresh_pending', False):
            return True
        if not getattr(self.pool, 'dt_full', False) or not self._dt_selection_enabled():
            self._dt_full_refresh_pending = False
            return True
        if self.state not in (IDLE, SCORING):
            return True
        if any(item_id is None for item_id, _m in self.queued):
            return False  # Wait for server identities, never guess a removal.
        current = self._server_playlist_items.get(self.current_item_id, {})
        if current.get('played') or current.get('expired'):
            return False  # Scoring has not promoted the next item yet.
        try:
            if not getattr(self.current_map, 'is_dt', False):
                candidate = self._next_downloadable_map()
                if candidate is not None and candidate.is_dt:
                    self.apply_map(candidate, announce=True)
            for item_id, m in list(self.queued):
                known = self._server_playlist_items.get(item_id, {})
                if m.is_dt or known.get('played') or known.get('expired'):
                    continue
                candidate = self._next_downloadable_map()
                if candidate is None or not candidate.is_dt:
                    continue  # Preserve the existing NoMod fallback.
                self._referee_operation_with_rejoin('RemovePlaylistItem',
                    lambda item_id=item_id: self.hub.remove_playlist_item(self.room_id, item_id))
                self._server_playlist_items.pop(item_id, None)
                self._playlist_maps.pop(item_id, None)
                self.queued = [pair for pair in self.queued if pair[0] != item_id]
                self._append_future_map(candidate)
        except HubTransportError:
            raise
        except Exception as exc:
            log.warning('DT 100 playlist refresh held for audit: %s', exc)
            return False
        self._dt_full_refresh_pending = False
        return True

    def create_room(self) -> None:
        first = self._next_downloadable_map(native_only=True)
        if not first:
            raise SystemExit("Pool is empty -- widen the SR range or lower "
                             "min_passcount in config.")

        log.info("creating room")
        initial_name = self._room_name_for_range(
            self.pool.sr_min, self.pool.sr_max)
        resp = self.hub.make_room(initial_name, first.beatmap_id, **({"ruleset_id": 3} if self.cfg.get("ruleset_id", 0) == 3 else {}))
        self._current_room_name = initial_name
        self.room_id = resp["room_id"]
        self.channel_id = resp["chat_channel_id"]
        self.current_map = first
        try:
            self.pp_stats.record_room_mode(int(self.room_id))
            room_url = self.ratings.record_room_open(
                room_id=int(self.room_id),
                room_name=initial_name,
            )
            log.info("room history URL: %s", room_url)
        except Exception:
            # History collection must never block room creation.
            log.exception("could not open room activity-history row")

        # Referees and players are separate concepts in the referee API. Keep
        # referee accounts out of player counts even if they are visible in the
        # lazer room or emit UserJoined/UserStatusChanged events.
        self.referee_ids = {
            int(r["user_id"]) for r in (resp.get("referees") or [])
            if r.get("user_id") is not None
        }
        if self.referee_ids:
            log.info("referee ids (excluded from player counts): %s",
                     ", ".join(map(str, sorted(self.referee_ids))))

        self._sync_playlist(resp)
        if self.current_item_id is not None:
            self._playlist_maps[self.current_item_id] = first
        log.info("room %s created (channel %s)", self.room_id, self.channel_id)

        # Rooms are created with a RANDOM PASSWORD. Empty string clears it;
        # null would keep it. Until this lands nobody can join.
        self.hub.change_room_settings(self.room_id, password="")
        log.info("room publication delegated to supervisor" if self.cfg.get("_rolling_worker")
                 else "password cleared, room is public")

        # Register the room's new multiplayer chat channel with the shared
        # account-level notification socket. Initial history reconciliation
        # covers messages around this short creation handoff.
        self._attach_chat_websocket()

        # Do this for every newly created room: the initial startup room,
        # explicit/periodic hard restarts, and automatic recovery replacements.
        # The URI handler is local to the Windows machine running lazer.
        self._open_current_room_in_lazer()

        # Room creation gives us the multiplayer chat channel id, but chat API
        # writes require explicit channel membership. Join proactively; say()
        # can also rejoin later if membership is lost.
        if not self._join_room_chat():
            if self.cfg.get("_headless_supervised", False):
                raise RuntimeError("Headless chat membership did not appear; stopping this room safely")
            log.warning(
                "could not join multiplayer chat channel %s; outgoing chat "
                "will retry membership on the next send",
                self.channel_id,
            )

        self.say(
            f"{self._current_room_name} — auto-rotating {self._mania_key_label() + ' mania' if self.cfg.get('ruleset_id', 0) == 3 else 'farm'} pool. "
            f"Type {self.prefix}help for commands.",
            automatic=True,
        )

        # From here on the room EXISTS on osu!'s side. Any exception must not
        # escape without closing it -- non-bot accounts cap at 4 open rooms,
        # so a few crashed starts will lock you out of making more.
        try:
            self.apply_map(first, announce=True)
            self.fill_queue()
            # Force the first idle/pre-start watchdog to compare our local
            # current+2 mirror against GET /rooms/{id}.
            self._playlist_dirty = True
            self._last_playlist_audit = 0.0
            self._start_hard_restart_cycle()
            self._room_ready = True
            self._update_discord_public_status(force=True)
        except Exception:
            log.exception("failed to set the first map; closing room %s",
                          self.room_id)
            try:
                self.hub.close_room(self.room_id)
                log.info("room %s closed", self.room_id)
                try:
                    self.ratings.record_room_close(
                        int(self.room_id), "setup_failure"
                    )
                except Exception:
                    log.exception("could not finalize failed room history")
            except Exception:
                log.error("could not close room %s -- close it manually, or "
                          "it will occupy one of your 4 room slots until it "
                          "expires", self.room_id)
            raise

    @staticmethod
    def _event_playlist_item(payload: dict) -> dict:
        """Extract the playlist item from real or legacy/fake event shapes."""
        if not isinstance(payload, dict):
            return {}
        item = payload.get("playlist_item") or payload.get("item")
        if isinstance(item, dict):
            return item
        return payload if "id" in payload or "beatmap_id" in payload else {}

    @staticmethod
    def _playlist_item_played(item: dict) -> bool:
        return bool(item.get("was_played") or item.get("played_at"))

    @staticmethod
    def _playlist_item_order(item: dict) -> int:
        raw = item.get("order")
        if raw is None:
            raw = item.get("playlist_order")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 65535

    @staticmethod
    def _standard_edit_signature(item):
        """Full identity evidence; never infer mods for a local edit revision."""
        try:
            if (not isinstance(item.get('required_mods'), (list, tuple))
                    or not isinstance(item.get('freestyle'), bool)
                    or item.get('ruleset_id', 0) != 0):
                return None
            mods = []
            for mod in item['required_mods']:
                acronym = str(mod.get('acronym', '') if isinstance(mod, dict) else mod).upper()
                if not acronym:
                    return None
                if isinstance(mod, dict) and mod.get('settings'):
                    settings = mod['settings']
                    if (acronym != 'DT' or set(settings) - {'speed_change'}
                            or float(settings.get('speed_change', 1.5)) != 1.5):
                        return None
                mods.append(acronym)
            return int(item['beatmap_id']), tuple(sorted(mods)), item['freestyle']
        except (KeyError, TypeError, ValueError, OverflowError):
            return None

    def _record_standard_edit(self, item_id, previous, previous_map, confirmed):
        """Bounded local revision history; uncertain attempts require a full audit."""
        if not hasattr(self, '_standard_playlist_edits'):
            self._standard_playlist_edits = {}
        history = self._standard_playlist_edits.setdefault(item_id, {'retired': {}})
        signature = self._standard_edit_signature(confirmed)
        old = self._standard_edit_signature(previous)
        if (old is not None and old != signature and not previous.get('standard_invalid')
                and previous_map is not None and old[0] == previous_map.beatmap_id
                and ('DT' in old[1]) == bool(getattr(previous_map, 'is_dt', False))):
            history['retired'][old] = previous_map.sr if getattr(previous_map, 'is_dt', False) else None
        history['retired'].pop(signature, None)
        while len(history['retired']) > 16:
            history['retired'].pop(next(iter(history['retired'])))
        history.update(expected=signature, pending_audit=False)
        history.pop('unconfirmed', None)
        while len(self._standard_playlist_edits) > 128:
            self._standard_playlist_edits.pop(next(iter(self._standard_playlist_edits)))

    def _superseded_standard_edit(self, item, history):
        """Recognize old revisions only from fields explicitly supplied by an event."""
        expected = history.get('expected')
        if expected is None or 'beatmap_id' not in item or item.get('ruleset_id', 0) != 0:
            return False
        for signature, dt_sr in history['retired'].items():
            # Fill only omitted identity fields for comparison, NEVER for adoption.
            evidence = dict(item)
            evidence.setdefault('required_mods', list(signature[1]))
            evidence.setdefault('freestyle', signature[2])
            if self._standard_edit_signature(evidence) != signature:
                continue
            distinguished = (signature[0] != expected[0]
                             or ('required_mods' in item and signature[1] != expected[1])
                             or ('freestyle' in item and signature[2] != expected[2]))
            if not distinguished:
                continue
            if dt_sr is not None and 'star_rating' in item:
                try:
                    reported = float(item['star_rating'])
                    if not math.isfinite(reported) or abs(reported - dt_sr) > .01:
                        continue
                except (ValueError, TypeError):
                    continue
            return True
        return False

    def _standard_edit_audit_pending(self):
        active = {self.current_item_id, self._server_current_item_id}
        active.update(qid for qid, _map in self.queued)
        return any(h.get('pending_audit') and item_id in active
                   for item_id, h in getattr(self, '_standard_playlist_edits', {}).items())

    def _resolve_standard_playlist_map(self, beatmap_id, mods, expected=None):
        resolved = self.pool.get_variant_map(beatmap_id, mods)
        if resolved is not None:
            return resolved
        # The explicit novelty request owns its built-in Approved NM chart.
        # General Ranked catalog lookup intentionally excludes Approved rows.
        # Do not extend that exception to unrequested server items, DT/NC/HT,
        # arbitrary maps or unrecognized fixed mods.
        if (expected is _FREEDOM_DIVE and beatmap_id == _FREEDOM_DIVE.beatmap_id
                and set(mods) <= {'NF', 'HD', 'HR'}):
            return expected
        return None

    def _remember_playlist_item(self, item: dict, *, authoritative=False):
        """Normalize one server item and bind its real id to our PoolMap."""
        if not isinstance(item, dict):
            return None
        raw_id = item.get("id")
        if raw_id is None:
            return None
        try:
            item_id = int(raw_id)
        except (TypeError, ValueError):
            return None

        beatmap_id = item.get("beatmap_id")
        try:
            beatmap_id = int(beatmap_id) if beatmap_id is not None else None
        except (TypeError, ValueError):
            beatmap_id = None

        from poolrotater_host.network.display import clock_rate as display_clock_rate
        normalized = {
            "display_clock_rate": display_clock_rate(item),
            "id": item_id,
            "beatmap_id": beatmap_id,
            "played": self._playlist_item_played(item),
            "expired": bool(item.get("expired", False)),
            "order": self._playlist_item_order(item),
        }
        if getattr(self.pool, 'mixed_dt', False):
            edit = getattr(self, '_standard_playlist_edits', {}).get(item_id)
            if edit is not None and (self._superseded_standard_edit(item, edit)
                    or not {'beatmap_id', 'required_mods', 'freestyle'} <= item.keys()):
                # A delayed/underspecified revision cannot poison the confirmed
                # replacement, clear an invalidation, or mark it played/expired.
                # A fresh FULL snapshot must confirm the replacement before start.
                edit['pending_audit'] = True
                self._playlist_dirty = True
                log.debug('Deferring stale/partial Standard edit event for item %s; awaiting server audit', item_id)
                return None
            previous = self._server_playlist_items.get(item_id, {})
            if 'beatmap_id' not in item:
                beatmap_id = previous.get('beatmap_id')
                normalized['beatmap_id'] = beatmap_id
            if not any(key in item for key in ('was_played', 'played_at')):
                normalized['played'] = previous.get('played', False)
            if 'expired' not in item:
                normalized['expired'] = previous.get('expired', False)
            if 'order' not in item and 'playlist_order' not in item:
                normalized['order'] = previous.get('order', normalized['order'])
            expected = self._playlist_maps.get(item_id)
            if expected is None and self.current_item_id == item_id:
                expected = self.current_map
            if expected is None:
                expected = next((m for qid, m in self.queued if qid == item_id
                    or qid is None and m.beatmap_id == beatmap_id), None)
            raw_mods = item.get('required_mods', previous.get('required_mods'))
            mods = None if not isinstance(raw_mods, (list, tuple)) else [
                str(mod.get('acronym', '') if isinstance(mod, dict) else mod).upper()
                for mod in raw_mods]
            normalized['required_mods'] = mods or []
            invalid = (beatmap_id is None or item.get('ruleset_id', 0) != 0 or mods is None
                       or ('required_mods' not in item and previous.get('standard_invalid', False)))
            resolved = None if invalid else self._resolve_standard_playlist_map(beatmap_id, mods, expected)
            invalid = invalid or resolved is None
            if resolved is not None:
                dt = getattr(resolved, 'is_dt', False)
                confirmed_uncertain_edit = (authoritative and edit is not None
                    and edit.get('unconfirmed')
                    and self._standard_edit_signature(item) == edit.get('expected'))
                if expected is not None and not confirmed_uncertain_edit:
                    invalid = invalid or (int(expected.beatmap_id) != beatmap_id
                                          or getattr(expected, 'is_dt', False) != dt)
                if dt:
                    invalid = invalid or bool(set(mods) - {'DT', 'NF'}) or item.get('freestyle', previous.get('freestyle', True))
                    for mod in raw_mods:
                        if isinstance(mod, dict) and mod.get('acronym') == 'DT':
                            speed = (mod.get('settings') or {}).get('speed_change', 1.5)
                            try:
                                invalid = invalid or float(speed) != 1.5
                            except (ValueError, TypeError):
                                invalid = True
                    reported = item.get('star_rating')
                    if reported is not None:
                        try:
                            invalid = invalid or not math.isfinite(float(reported)) or abs(float(reported)-resolved.sr) > .01
                        except (ValueError, TypeError):
                            invalid = True
            normalized['freestyle'] = item.get('freestyle', previous.get('freestyle', False))
            normalized['standard_invalid'] = invalid
            self._server_playlist_items[item_id] = normalized
            if edit is not None:
                if invalid:
                    edit['pending_audit'] = True
                elif authoritative and self._standard_edit_signature(item) == edit['expected']:
                    edit['pending_audit'] = False
                    edit.pop('unconfirmed', None)
            if invalid:
                self._playlist_maps.pop(item_id, None)
                log.error('Standard playlist item %s has unknown/mismatched mods or DT rating; holding start', item_id)
            else:
                self._playlist_maps[item_id] = resolved
                self.queued = [(qid, resolved if qid == item_id else m) for qid, m in self.queued]
                # An AddPlaylistItem response may omit its id. Bind the first
                # matching pending request only after this real event/snapshot
                # has passed all variant checks; never guess a server id.
                for index, (qid, pending) in enumerate(self.queued):
                    if qid is None and pending is expected:
                        self.queued[index] = (item_id, resolved)
                        break
            self._playlist_dirty = True
            return normalized
        if self.cfg.get("ruleset_id", 0) == 3:
            invalid = item.get("ruleset_id", 3) != 3 or beatmap_id is None
            resolved = None
            if not invalid:
                resolved = self.pool.get_map(beatmap_id)
                invalid = resolved is None
            if resolved is not None and getattr(resolved, "is_mania_convert", False):
                mods = {m.get("acronym") if isinstance(m, dict) else m
                        for m in item.get("required_mods", [])}
                reported_sr = item.get("star_rating")
                invalid = invalid or (item.get("freestyle", True)
                                      or f"{self.cfg.get('mania_keys', 4)}K" not in mods
                                      or not getattr(resolved, "mania_conversion_verified", False))
                if reported_sr is not None:
                    try:
                        invalid = invalid or not math.isfinite(float(reported_sr)) or abs(float(reported_sr) - resolved.sr) > .05
                    except (TypeError, ValueError):
                        invalid = True
                if invalid and not normalized["played"] and not normalized["expired"]:
                    self.pool.exclude_beatmap(beatmap_id)
                    self._pending_soft_restart = True
                    log.error("conversion playlist mismatch for %s: verified %.5f*, reported %r; quarantining and rebuilding safely", beatmap_id, resolved.sr, reported_sr)
            normalized["mania_invalid"] = invalid
            if invalid:
                self._playlist_maps.pop(item_id, None)
                self._server_playlist_items[item_id] = normalized
                log.error("Refusing unvetted/wrong-key %sK or wrong-ruleset playlist item %s", self.cfg.get("mania_keys", 4), item_id)
                return normalized
        self._server_playlist_items[item_id] = normalized

        m = self._playlist_maps.get(item_id)
        if m is None and beatmap_id is not None:
            if self.current_map is not None and int(self.current_map.beatmap_id) == beatmap_id:
                m = self.current_map
            else:
                for i, (queued_id, queued_map) in enumerate(self.queued):
                    if (queued_id == item_id or
                            (queued_id is None and int(queued_map.beatmap_id) == beatmap_id)):
                        m = queued_map
                        self.queued[i] = (item_id, queued_map)
                        break
            if m is None:
                m = self.pool.get_map(beatmap_id)

        if m is not None:
            self._playlist_maps[item_id] = m
        self._playlist_dirty = True
        return normalized

    def _map_for_item_id(self, item_id: int | None):
        if item_id is None:
            return None
        item_id = int(item_id)
        if (self._server_playlist_items.get(item_id, {}).get("mania_invalid")
                or self._server_playlist_items.get(item_id, {}).get('standard_invalid')):
            return None
        m = self._playlist_maps.get(item_id)
        if m is not None:
            return m
        item = self._server_playlist_items.get(item_id)
        if item and item.get("beatmap_id") is not None:
            m = (self.pool.get_variant_map(int(item['beatmap_id']), item.get('required_mods', []))
                 if getattr(self.pool, 'mixed_dt', False) else self.pool.get_map(int(item['beatmap_id'])))
            if m is not None:
                self._playlist_maps[item_id] = m
            return m
        return None

    def _adopt_current_item_id(self, item_id: int | None, reason: str) -> bool:
        if item_id is None:
            return False
        item_id = int(item_id)
        m = self._map_for_item_id(item_id)
        if m is None:
            return False

        old_id = self.current_item_id
        self.current_item_id = item_id
        self.current_map = m
        self._server_current_item_id = item_id
        self._sync_countdown_wait_item()

        # Everything before/through the promoted current item is no longer a
        # future queued entry. Keep later entries in their existing order.
        match_idx = None
        for i, (queued_id, queued_map) in enumerate(self.queued):
            if queued_id == item_id or (queued_id is None and int(queued_map.beatmap_id) == int(m.beatmap_id)):
                match_idx = i
                break
        if match_idx is not None:
            del self.queued[:match_idx + 1]

        if old_id != item_id:
            if (
                self._skip_vote_item_id is not None
                and old_id is not None
                and int(self._skip_vote_item_id) == int(old_id)
            ):
                self._clear_skip_vote("playlist current changed")
            self._clear_replay_vote("playlist current changed")
            log.info(
                "playlist current corrected%s: item %s -> %s (%s)",
                f" [{reason}]" if reason else "",
                old_id, item_id, m.label,
            )
            # Normal post-match rotation promotes an existing queued item
            # instead of calling apply_map(). Mirror that new current map too;
            # otherwise Discord only sees the very first map of the room.
            self._notify_discord_lobby_event(
                "MAP",
                m.label,
                map_key=self._discord_map_key(m),
            )
            self._maybe_announce_post_match_pp()
        return True

    def _sync_playlist(self, resp: dict) -> None:
        """Seed local playlist truth from MakeRoom/GET room responses."""
        items = resp.get("playlist") or []
        for item in items:
            self._remember_playlist_item(item, authoritative=True)

        active = [
            item for item in items
            if not self._playlist_item_played(item)
            and not bool(item.get("expired", False))
        ]
        active.sort(key=self._playlist_item_order)
        if active:
            current_id = active[0].get("id")
            if current_id is not None:
                self.current_item_id = int(current_id)
                self._server_current_item_id = int(current_id)

    def _sync_from_room_snapshot(self, resp: dict, reason: str) -> bool:
        """Rebuild current+future state from GET /rooms/{room_id}."""
        items = (resp or {}).get("playlist") or []
        if not items:
            return False

        active_norm = []
        for raw in items:
            norm = self._remember_playlist_item(raw, authoritative=True)
            if norm is None and getattr(self.pool, 'mixed_dt', False) and self._standard_edit_audit_pending():
                # Do not promote a future item when a lagging snapshot omitted
                # the just-edited current revision. Keep current/queue intact.
                return False
            if norm and not norm["played"] and not norm["expired"]:
                active_norm.append(norm)

        active_norm.sort(key=lambda x: (x["order"], x["id"]))
        if not active_norm:
            return False

        # RoomSettingsChanged is a real-time referee event and is more current
        # than the web snapshot if the latter is briefly catching up. Prefer its
        # selected id when that id is present in the active snapshot.
        active_ids = [int(item["id"]) for item in active_norm]
        if (self._server_current_item_id is not None
                and int(self._server_current_item_id) in active_ids):
            current_id = int(self._server_current_item_id)
            current_idx = active_ids.index(current_id)
        else:
            current_id = int(active_norm[0]["id"])
            current_idx = 0

        old_id = self.current_item_id
        self._server_current_item_id = current_id
        current_map = self._map_for_item_id(current_id)
        if current_map is not None:
            self.current_item_id = current_id
            self.current_map = current_map
            self._sync_countdown_wait_item()

        # Ignore any stale active-looking entries that sort before the referee's
        # selected current item; they are not future queue entries.
        rebuilt = []
        for item in active_norm[current_idx + 1:]:
            m = self._map_for_item_id(item["id"])
            if m is not None:
                rebuilt.append((int(item["id"]), m))
            else:
                log.warning(
                    "playlist audit could not resolve item %s beatmap %s; "
                    "counting it but not attaching feedback metadata",
                    item["id"], item.get("beatmap_id"),
                )
        self.queued = rebuilt
        self._playlist_dirty = False

        if old_id != self.current_item_id:
            log.warning(
                "playlist audit repaired current item [%s]: %s -> %s (%s)",
                reason, old_id, self.current_item_id,
                self.current_map.label if self.current_map else "unknown map",
            )
            self._maybe_announce_post_match_pp()
        return True

    def reconcile_playlist(
            self, reason: str, fetch_remote: bool = False,
            ensure_depth: bool = True, trim_extras: bool = True) -> bool:
        """Keep exactly current + queue_ahead unplayed maps while safe.

        This is the anti-index-drift guard. Referee events update state in real
        time; a public GET /rooms/{id} snapshot periodically audits that mirror
        and is forced immediately before every StartMatch. Playlist mutation is
        only performed while IDLE/SCORING.
        """
        if not self.room_id:
            return False

        if not self._refresh_operator_exclusions():
            return False
        exclusion_audit = getattr(self, '_operator_exclusion_audit_pending', False)
        if exclusion_audit and not fetch_remote and self.state in (IDLE, SCORING):
            return self.reconcile_playlist(reason, fetch_remote=True,
                                           ensure_depth=ensure_depth, trim_extras=trim_extras)
        if (getattr(self, '_dt_full_refresh_pending', False) and not fetch_remote
                and ensure_depth and self.state in (IDLE, SCORING)):
            return self.reconcile_playlist(reason, fetch_remote=True,
                                           ensure_depth=ensure_depth, trim_extras=trim_extras)
        if fetch_remote:
            # Ready players can request pre-start reconciliation every loop.
            # Do not let inconclusive probes turn that into an API-budget storm.
            # A backoff is a hold, never cached permission to mutate or start.
            if (getattr(self, '_playlist_probe_retry_room', None) == self.room_id
                    and time.monotonic() < getattr(self, '_playlist_probe_retry_at', 0.0)):
                return False
            room = None
            probe = getattr(self.api, "room_probe", None)
            if callable(probe):
                try:
                    status, room = probe(self.room_id)
                except Exception as e:
                    status, room = "unknown", None
                    log.warning("room liveness probe failed [%s]: %s", reason, e)

                if status == "missing":
                    self._room_missing_confirmations += 1
                    needed = max(2, int(self.cfg.get(
                        "room_missing_confirmations", 2)))
                    log.warning(
                        "room %s returned 404 during %s (%d/%d confirmations)",
                        self.room_id, reason, self._room_missing_confirmations, needed,
                    )
                    self._last_playlist_audit = time.time()
                    if self._room_missing_confirmations >= needed:
                        self._recreate_missing_room(reason)
                        return True
                    return False
                elif status == "ok":
                    self._room_missing_confirmations = 0
                    self._playlist_probe_retry_at = 0.0
                    self._playlist_probe_failures = 0
                else:
                    # Network/5xx/rate-limit uncertainty must never be treated
                    # as proof the room disappeared. For a pre-start audit, be
                    # conservative and wait for the next successful snapshot.
                    log.warning("room liveness unknown during %s; postponing mutation", reason)
                    failures = (getattr(self, '_playlist_probe_failures', 0)
                        if getattr(self, '_playlist_probe_retry_room', None) == self.room_id else 0) + 1
                    self._playlist_probe_retry_room = self.room_id
                    self._playlist_probe_failures = min(failures, 4)
                    self._playlist_probe_retry_at = time.monotonic() + min(15.0, 2.0 ** min(failures, 4))
                    self._last_playlist_audit = time.time()
                    return False
            else:
                try:
                    room = self.api.room(self.room_id)
                except Exception as e:
                    room = None
                    log.warning("playlist audit failed [%s]: %s", reason, e)

            if room:
                self._sync_from_room_snapshot(room, reason)
            elif getattr(self, '_dt_full_refresh_pending', False) or exclusion_audit:
                return False
            self._last_playlist_audit = time.time()

        if self.cfg.get("ruleset_id", 0) == 3 and any(
            item.get("mania_invalid") and not item.get("played") and not item.get("expired")
            for item in self._server_playlist_items.values()
        ):
            log.error("Mania playlist contains an ineligible item; refusing start until removed or soft-restarted")
            return False

        if self._standard_edit_audit_pending():
            log.debug('Standard edit awaits a full matching server snapshot; postponing start')
            return False
        try:
            if not self._cleanup_operator_exclusions():
                return False
        except HubTransportError:
            raise
        except Exception as exc:
            self._operator_exclusion_audit_pending = True
            self._playlist_dirty = True
            log.warning('Exclusion cleanup held for fresh audit: %s', exc)
            return False
        if self._standard_edit_audit_pending():
            return False
        if not self._cleanup_disabled_dt():
            return False
        if any(item.get('standard_invalid') and not item.get('played') and not item.get('expired')
               for item in self._server_playlist_items.values()):
            log.error('Standard playlist contains an unverifiable variant; refusing start')
            return False

        if self.current_item_id is None and self._server_current_item_id is not None:
            self._adopt_current_item_id(self._server_current_item_id, reason)

        if self.current_item_id is None or self.current_map is None:
            log.warning(
                "playlist not start-safe [%s]: current item/map unresolved", reason
            )
            return False

        if not ensure_depth or self.state not in (IDLE, SCORING):
            return True

        if not self._refresh_full_dt_playlist():
            return False
        target_ahead = max(0, int(self.cfg.get("queue_ahead", 2)))

        # If a bug/old restart left too many future UNPLAYED entries, trim only
        # the tail. Never touch the active current item or played history.
        if trim_extras and len(self.queued) > target_ahead:
            extras = list(self.queued[target_ahead:])
            for item_id, m in reversed(extras):
                if item_id is None:
                    continue
                try:
                    self._referee_operation_with_rejoin(
                        "RemovePlaylistItem",
                        lambda item_id=int(item_id): self.hub.remove_playlist_item(
                            self.room_id, item_id),
                    )
                    self._server_playlist_items.pop(int(item_id), None)
                    self._playlist_maps.pop(int(item_id), None)
                    self.queued = [
                        pair for pair in self.queued
                        if pair[0] != item_id
                    ]
                    log.warning(
                        "playlist audit removed extra future item %s (%s)",
                        item_id, m.label,
                    )
                except HubTransportError:
                    raise
                except Exception as e:
                    log.warning(
                        "playlist audit could not remove extra item %s: %s",
                        item_id, e,
                    )

        self.fill_queue()
        total = 1 + len(self.queued)
        expected = 1 + target_ahead
        if total != expected:
            log.warning(
                "playlist depth [%s]: have %d/%d unplayed maps "
                "(current + %d queued)",
                reason, total, expected, len(self.queued),
            )
            return False

        log.debug(
            "playlist verified [%s]: current item=%s beatmap=%s + %d queued",
            reason, self.current_item_id, self.current_map.beatmap_id,
            len(self.queued),
        )
        return True

    def _playlist_rules_for_map(self, m) -> dict:
        rules = dict(allowed_mods=self.cfg.get("allowed_mods", ["HD", "HR", "NF"]),
                     required_mods=self.cfg.get("required_mods", []),
                     freestyle=self.cfg.get("freestyle", False))
        if getattr(m, 'is_dt', False):
            from .standard_pool import DT_CALCULATOR
            if (self.cfg.get('ruleset_id', 0) != 0 or not self._dt_selection_enabled()
                    or getattr(m, 'dt_calculator', None) != DT_CALCULATOR
                    or getattr(m, 'clock_rate', None) != 1.5
                    or not isinstance(getattr(m, 'dt_checksum', None), str)
                    or len(m.dt_checksum) != 32
                    or any(c not in '0123456789abcdef' for c in m.dt_checksum)
                    or not math.isfinite(m.sr) or not self.pool.sr_min <= m.sr <= self.pool.sr_max
                    or m.hit_length > self.pool.max_length):
                raise ValueError('disabled/unverified/out-of-range DT variant')
            # Each item owns its speed. Never change global lobby mods, and
            # never permit difficulty/ruleset substitution on a verified DT pick.
            rules['required_mods'] = [*rules['required_mods'], 'DT']
            rules['freestyle'] = False
        if self.cfg.get("ruleset_id", 0) == 3:
            rules["ruleset_id"] = 3
            if getattr(m, "is_mania_convert", False):
                keys = self.cfg.get("mania_keys", 4)
                source_sr = getattr(m, "mania_source_sr", None)
                if (not getattr(m, "mania_conversion_verified", False)
                        or getattr(m, "mania_keys", None) != keys
                        or source_sr is None or not math.isfinite(source_sr)
                        or not self.pool.sr_min <= source_sr <= self.pool.sr_max
                        or not self.pool.sr_min <= m.sr <= self.pool.sr_max):
                    raise ValueError("unverified/wrong-key/out-of-range Mania conversion")
                # FreeSTYLE permits choosing another difficulty/ruleset. It
                # cannot be enabled for a specifically verified conversion.
                rules["freestyle"] = False
                rules["required_mods"] = [*rules["required_mods"], f"{keys}K"]
        return rules

    def apply_map(self, m, announce: bool = False) -> None:
        if not self._operator_map_allowed(m):
            raise ValueError('Chart is excluded by operator policy or quarantine')
        rules = self._playlist_rules_for_map(m)
        previous_id = self.current_item_id
        previous = dict(self._server_playlist_items.get(previous_id, {}))
        previous_map = self._playlist_maps.get(previous_id, self.current_map)
        self._prefetch_map_pp(m)
        try:
            resp = self._referee_operation_with_rejoin(
                "EditCurrentPlaylistItem",
                lambda: self.hub.edit_current_item(
                    self.room_id, m.beatmap_id,
                    **rules,
                ),
            )
        except HubInvocationUncertain:
            if previous_id is not None and getattr(self.pool, 'mixed_dt', False):
                # A lost completion does not prove failure or permit replay.
                # Retain the exact requested revision without adopting it. Only
                # a full matching server audit plus catalog verification can
                # resolve the existing item's old local map binding.
                self._record_standard_edit(previous_id, previous, previous_map, {
                    'beatmap_id': m.beatmap_id,
                    'required_mods': rules['required_mods'],
                    'freestyle': rules['freestyle'],
                })
                pending = self._standard_playlist_edits[previous_id]
                pending.update(unconfirmed=True, pending_audit=True)
                self._playlist_dirty = True
            raise
        if isinstance(resp, dict) and resp.get("id") is not None:
            self.current_item_id = int(resp["id"])
        # EditCurrentPlaylistItem normally retains the playlist item id. Set
        # the beatmap before syncing map-scoped state so a same-item skip still
        # resets .wait requesters and locks for the replacement beatmap.
        self.current_map = m
        self._sync_countdown_wait_item()
        if self.current_item_id is not None:
            self._playlist_maps[self.current_item_id] = m
            if getattr(self.pool, 'mixed_dt', False):
                self._server_playlist_items[self.current_item_id] = {
                    'id': self.current_item_id, 'beatmap_id': m.beatmap_id,
                    'played': False, 'expired': False, 'order': 0,
                    'required_mods': rules['required_mods'], 'freestyle': rules['freestyle'],
                    'standard_invalid': False}
                self._record_standard_edit(
                    self.current_item_id,
                    previous if previous_id == self.current_item_id else {},
                    previous_map,
                    self._server_playlist_items[self.current_item_id])
        self._notify_discord_lobby_event(
            "MAP",
            m.label,
            map_key=self._discord_map_key(m),
        )
        if announce:
            self._announce_map(m)

    def _map_pp_wait_seconds(self) -> float:
        try:
            return self._external_float(
                self.cfg.get("map_pp_announce_wait_seconds", 1.5), 0.0, 10.0
            )
        except (TypeError, ValueError):
            return 1.5

    def _map_pp_command_wait_seconds(self) -> float:
        try:
            return self._external_float(
                self.cfg.get("map_pp_command_wait_seconds", 5.0), 0.0, 15.0
            )
        except (TypeError, ValueError):
            return 5.0

    def _pp_command_map(self):
        """Return the map a human means by `.pp` right now.

        During gameplay, ``current_map`` can already be advanced by a fast
        playlist event, so the frozen MatchStarted map is authoritative. At all
        other times the playlist current item is the upcoming map users see.
        """
        if self.state == PLAYING and self.playing_map is not None:
            return self.playing_map
        if self.state == STARTING and self.starting_map is not None:
            return self.starting_map
        if self.state == SCORING and self.current_map is not None:
            try:
                current_bid = int(self.current_map.beatmap_id)
                completed_bid = int(
                    self.last_played_map.beatmap_id
                    if self.last_played_map is not None else 0
                )
            except (TypeError, ValueError):
                current_bid = completed_bid = 0
            if current_bid == completed_bid and self.queued:
                # MatchCompleted can beat the playlist-promotion event by a
                # moment. The first queued item is already the known upcoming
                # map, so `.pp` should not report the map that just finished.
                return self.queued[0][1]
        return self.current_map

    def _show_current_map_pp(
        self,
        *,
        mods=(),
        accuracies=(100.0, 99.0, 95.0),
    ) -> None:
        if not self.cfg.get("map_pp_display_enabled", True):
            self.say("Map PP lookup is disabled for this lobby.")
            return
        m = self._pp_command_map()
        if m is None:
            self.say("There is no current/upcoming map to calculate PP for.")
            return
        if getattr(m, 'is_dt', False):
            if any(mod in mods for mod in ('HT', 'DC', 'NC')):
                self.say('This map uses DT; omit other speed mods from .pp.')
                return
            mods = tuple(dict.fromkeys((*mods, 'DT')))
        projector = getattr(self, 'pp_projection', None)
        if projector is None:
            self.say('PP calculation is unavailable for this lobby.')
            return
        projection = projector.get(
            m.beatmap_id,
            wait_seconds=self._map_pp_command_wait_seconds(),
            mods=mods,
            accuracies=accuracies,
        )
        if projection is None:
            self.say("PP is still calculating or unavailable; try .pp again shortly.")
            return
        suffix = " | mania estimate; judgement distribution can change PP" if self.cfg.get("ruleset_id", 0) == 3 else ""
        self.say(f"{projection.display()} — {m.label}{suffix}")

    def _maybe_announce_post_match_pp(self) -> bool:
        """Announce PP once for the promoted map after MatchCompleted.

        Playlist promotion can happen either before or after MatchCompleted.
        This method is therefore safe to call from both paths. Once a real next
        map is known, the pending flag is consumed even if the calculator is
        temporarily unavailable so a delayed retry cannot spam the room later.
        """
        if not self._post_match_pp_announce_pending:
            return False
        if not self.cfg.get("map_pp_auto_announce_enabled", True):
            self._post_match_pp_announce_pending = False
            self._post_match_pp_completed_beatmap_id = None
            return False
        if not self.cfg.get("map_pp_display_enabled", True):
            self._post_match_pp_announce_pending = False
            self._post_match_pp_completed_beatmap_id = None
            return False

        m = self.current_map
        if m is None:
            return False
        try:
            current_bid = int(m.beatmap_id)
            completed_bid = int(self._post_match_pp_completed_beatmap_id or 0)
        except (TypeError, ValueError):
            return False
        if current_bid <= 0 or current_bid == completed_bid:
            return False

        # A promoted queue entry was prefetched when it was first added, so in
        # normal operation this is an immediate cache hit. Keep the same small
        # bounded wait as ordinary map announcements for cold-start cases.
        projection = self.pp_projection.get(
            current_bid, wait_seconds=self._map_pp_wait_seconds(), **self._dt_pp_kwargs(m)
        )
        self._post_match_pp_announce_pending = False
        self._post_match_pp_completed_beatmap_id = None
        if projection is None:
            return False
        self.say(
            f"({map_type_label(m)}) PP — {projection.display()}",
            automatic=True,
        )
        return True

    def _announce_map(self, m) -> None:
        extra = ""
        if self.cfg.get("map_pp_auto_announce_enabled", True):
            projection = self.pp_projection.get(
                m.beatmap_id, wait_seconds=self._map_pp_wait_seconds(), **self._dt_pp_kwargs(m)
            )
            if projection is not None:
                extra += f" PP {projection.display()}"
        self.say(
            f"Next ({map_type_label(m)}): {m.label} [{m.hit_length}s]{extra} — {m.url}",
            automatic=True,
        )

    def _adopt_playlist_item(self, item: dict) -> bool:
        """Consume a PlaylistItemChanged event without guessing by index."""
        norm = self._remember_playlist_item(item)
        if norm is None:
            return False

        # Match completion first reports the just-played item with order=0.
        # That is history, not the new current map. Only an unplayed order-zero
        # item (or the item named by RoomSettingsChanged) may become current.
        if norm["played"] or norm["expired"]:
            return False

        if norm["order"] == 0 or self._server_current_item_id == norm["id"]:
            return self._adopt_current_item_id(norm["id"], "playlist event")
        return False

    def rotate(self) -> None:
        """Finish scoring, then reconcile the server-owned playlist."""
        if self._run_pending_fixed_range_rebuild():
            return
        if self._run_pending_difficulty_vote_rebuild():
            return
        if self._pending_soft_restart:
            self.perform_soft_restart()
            return
        if self._run_pending_freedom_dive():
            return

        # If the six-hour discovery refresh became due during the last match,
        # arm its warnings now. The map that just ended predates the first
        # warning, so it deliberately does not count toward the 3 → 2 → 1
        # countdown. A previously armed/manual restart does tick here.
        had_queued_hard_restart = self._pending_hard_restart_maps is not None
        if self._maybe_queue_automatic_hard_restart():
            return
        if had_queued_hard_restart and self._run_queued_hard_restart_after_map():
            return

        # Referee playlist events normally arrive before scoring finishes. If
        # one was delayed/missed, ask the public room endpoint rather than
        # popping an index and hoping it matches lazer.
        self.reconcile_playlist(
            reason="post-match", fetch_remote=True,
            ensure_depth=False, trim_extras=False,
        )

        # The lazer playlist already shows the newly-promoted current map.
        # Keep normal post-match rotation silent instead of repeating it in chat.

        # Roster/status churn is collapsed into ONE automatic difficulty
        # decision per completed-map cycle.  Do not retune while people are
        # sitting in IDLE downloading/readying: that would spam chat/title
        # updates and can invalidate maps they already downloaded.
        old_range = (self.pool.sr_min, self.pool.sr_max)
        self.apply_pending_auto_retune()
        new_range = (self.pool.sr_min, self.pool.sr_max)
        self.apply_pending_room_name_update()

        # Auto difficulty must never replace a map that is already visible in
        # the playlist.  If the tier changes, keep current + all queued maps
        # exactly as they are.  The post-match refill below will append the
        # first map from the new SR pool only when a new queue slot naturally
        # becomes available.
        if new_range != old_range:
            log.info(
                "auto range changed after completed map %.2f-%.2f* -> "
                "%.2f-%.2f*; keeping existing playlist, new range applies "
                "only to maps added from now on",
                old_range[0], old_range[1], new_range[0], new_range[1],
            )

        self.reconcile_playlist(
            reason="post-match refill", fetch_remote=False,
            ensure_depth=True, trim_extras=True,
        )
        # Fallback for a missed/delayed promotion event. Reconciliation above
        # may be the first point where the next current map becomes known.
        self._maybe_announce_post_match_pp()

        self.starting_map = None
        self.starting_item_id = None
        self._clear_start_recovery_tracking()
        self.playing_map = None
        self.playing_item_id = None
        self.playing_player_ids.clear()
        self.scoring_item_id = None
        self._activity_match_key = None
        self._match_started_at = 0.0
        self._explicit_abort_pending = False
        self.set_state(IDLE)

    # ------------------------------------------------ community difficulty

    def _clear_difficulty_vote(self, reason: str = "") -> None:
        if self._difficulty_vote_required and reason == "two-map window expired":
            self.say("Difficulty vote expired—range unchanged.")
        if self._difficulty_vote_required and reason:
            log.debug("clearing difficulty vote (%s)", reason)
        self._difficulty_vote_harder.clear()
        self._difficulty_vote_easier.clear()
        self._difficulty_vote_required = 0
        self._difficulty_vote_player_count = 0
        self._difficulty_vote_expires_after_map = 0
        self._difficulty_vote_announced = set()

    def _clear_community_difficulty(self, reason: str = "") -> None:
        """Drop all temporary player preference and active difficulty votes."""
        self._clear_difficulty_vote(reason)
        if self._difficulty_bias_layers:
            log.info(
                "clearing temporary community difficulty offset %+.2f* (%s)",
                self._community_bias_overlay(),
                reason or "reset",
            )
        self._difficulty_bias_layers.clear()

    def _fixed_difficulty_mode(self) -> bool:
        """Return whether this room uses a configured or recalled fixed range."""
        if getattr(self, "_recalled_window", None) is not None:
            return True
        return bool(
            getattr(self, "manual_range", False)
            or not self.cfg.get("auto_difficulty", True)
        )

    def _fixed_baseline_window(self) -> tuple[float, float, int]:
        """Return the permanent JSON window underneath temporary votes."""
        if getattr(self, "_recalled_window", None) is not None:
            return (*self._recalled_window, int(self.cfg.get("max_length", self.pool.max_length)))
        lo = float(self.cfg.get("sr_min", self.pool.sr_min))
        hi = float(self.cfg.get("sr_max", self.pool.sr_max))
        length = int(self.cfg.get("max_length", self.pool.max_length))
        return lo, hi, length

    def _floor_fixed_community_layers(self, base_lo: float) -> None:
        """Discard unapplied easier bias while retaining each voter's attribution."""
        offset = self._community_bias_overlay()
        if offset >= -base_lo:
            return
        negative = sum(min(0.0, float(layer['amount']))
                       for layer in self._difficulty_bias_layers)
        if negative >= 0:
            return
        scale = max(0.0, min(1.0, (negative - base_lo - offset) / negative))
        self._difficulty_bias_layers = [
            dict(layer, amount=(float(layer['amount']) * scale
                                if layer['amount'] < 0 else layer['amount']))
            for layer in self._difficulty_bias_layers
            if layer['amount'] >= 0 or scale > 0
        ]

    def _apply_fixed_community_range(self, reason: str = "") -> bool:
        """Apply the JSON range plus the current temporary fixed-room shift."""
        if not self._fixed_difficulty_mode():
            return False
        base_lo, base_hi, length = self._fixed_baseline_window()
        # Keep the window's width while flooring the whole temporary shift.
        # Also covers a remaining negative layer after an opposite voter leaves.
        offset = max(-base_lo, self._community_bias_overlay())
        lo, hi = base_lo + offset, base_hi + offset
        if self.pool.set_window(lo, hi, length) <= 0:
            log.warning(
                "fixed temporary range %.2f-%.2f* had no eligible maps; "
                "keeping %.2f-%.2f* (%s)",
                lo, hi, self.pool.sr_min, self.pool.sr_max,
                reason or "difficulty update",
            )
            return False
        self._floor_fixed_community_layers(base_lo)
        self.update_room_name_for_range(lo, hi)
        log.info(
            "fixed difficulty range set to %.2f-%.2f* from JSON baseline "
            "%.2f-%.2f* with temporary offset %+.2f* (%s)",
            lo, hi, base_lo, base_hi, offset,
            reason or "difficulty update",
        )
        return True

    def _run_pending_fixed_range_rebuild(self) -> bool:
        """Quietly rebuild a fixed room after a temporary owner departs."""
        if not getattr(self, "_fixed_range_rebuild_pending", False):
            return False
        # Empty rooms use auto_empty_reset_seconds so a departure does not
        # immediately churn the playlist. If somebody joins before that timer
        # expires, this pending rebuild runs on the next safe loop instead.
        if not self.players:
            return False
        if self.state not in (SCORING, IDLE):
            return False
        if not self._apply_fixed_community_range("temporary voter left"):
            return False
        if not self.perform_soft_restart(announce=False):
            return False
        self._fixed_range_rebuild_pending = False
        if abs(self._community_bias_overlay()) < 0.001:
            self._fixed_difficulty_dirty = False
        log.info("fixed difficulty playlist rebuilt after temporary voter left")
        return True

    def _run_pending_difficulty_vote_rebuild(self) -> bool:
        """Refresh stale queued maps after several between-match vote changes."""
        if not getattr(self, "_difficulty_vote_rebuild_pending", False):
            return False
        if self.state not in (SCORING, IDLE):
            return False
        if not self.perform_soft_restart(announce=False):
            return False
        self._difficulty_vote_rebuild_pending = False
        self._difficulty_vote_changes_since_match = 0
        log.info("playlist rebuilt after repeated community difficulty votes")
        return True

    def difficulty_votes_required(self, player_count: int) -> int:
        n = max(0, int(player_count or 0))
        if n <= 1:
            return 1
        ratio = float(self.cfg.get("difficulty_vote_ratio", 0.50))
        ratio = min(1.0, max(0.25, ratio))
        return max(2, int(math.ceil(n * ratio)))

    def _difficulty_voter_ids(self) -> set[int]:
        # Same population semantics as vote-skip: spectators do not influence
        # active gameplay preferences when playable users are present.
        return self.skip_voter_ids()

    def _refresh_difficulty_vote_quorum(self) -> bool:
        """Recalculate an open difficulty vote from current playable users.

        Returns True if a reduced quorum immediately passes a vote. Departed
        users and spectators can never keep an old quorum artificially high.
        """
        if self._difficulty_vote_required <= 0:
            return False
        eligible = self._difficulty_voter_ids()
        if not eligible:
            self._clear_difficulty_vote("no eligible voters remain")
            return False
        self._difficulty_vote_harder.intersection_update(eligible)
        self._difficulty_vote_easier.intersection_update(eligible)
        self._difficulty_vote_player_count = len(eligible)
        self._difficulty_vote_required = self.difficulty_votes_required(
            len(eligible)
        )
        required = self._difficulty_vote_required
        for direction, votes in (
            ("harder", self._difficulty_vote_harder),
            ("easier", self._difficulty_vote_easier),
        ):
            if len(votes) >= required:
                self._apply_passed_difficulty_vote(
                    direction, set(votes), len(votes), required
                )
                return True
        return False

    def _apply_passed_difficulty_vote(
            self, direction: str, supporters: set[int], vote_count: int,
            votes_required: int) -> None:
        """Apply one already-approved public difficulty vote."""
        step_size = self._difficulty_vote_step()
        step = step_size if direction == "harder" else -step_size
        cap = self._difficulty_vote_max_offset()
        fixed_mode = self._fixed_difficulty_mode()
        previous_layers = list(self._difficulty_bias_layers)
        if fixed_mode:
            base_lo, _base_hi, _length = self._fixed_baseline_window()
            self._floor_fixed_community_layers(base_lo)
        old_overlay = self._community_bias_overlay()
        new_overlay = max(-cap, min(cap, old_overlay + step))
        if fixed_mode:
            # Record only the shift that can actually apply. Extra easier votes
            # at zero must not build debt that later harder votes have to undo.
            new_overlay = max(-base_lo, new_overlay)
        actual_delta = new_overlay - old_overlay
        self._clear_difficulty_vote("vote passed")

        if abs(actual_delta) < 0.001:
            self.say(
                f"Temporary community difficulty is already at the "
                f"{new_overlay:+.2f}* cap."
            )
            return

        self._difficulty_bias_layers.append({
            "amount": float(actual_delta),
            "supporters": set(supporters),
        })
        self._difficulty_vote_changes_since_match = (
            int(getattr(self, "_difficulty_vote_changes_since_match", 0)) + 1
        )

        if self._fixed_difficulty_mode():
            if not self._apply_fixed_community_range(
                f"{direction} vote passed"
            ):
                # Also restore any unapplied floor normalization when neither
                # the proposed window nor the prior floor is available.
                self._difficulty_bias_layers = previous_layers
                self._difficulty_vote_changes_since_match -= 1
                self._apply_fixed_community_range(
                    "rejected temporary vote restored previous range"
                )
                self.say(
                    f"{direction.title()} vote passed, but that range has no "
                    "eligible maps; previous difficulty kept."
                )
                return
            self._fixed_difficulty_dirty = True
            self._note_solo_difficulty_choice(next(iter(supporters)))
            base_lo, base_hi, _length = self._fixed_baseline_window()
            self.say(
                f"{direction.title()} vote passed ({vote_count}/{votes_required}) "
                f"— fixed range {self.pool.sr_min:.2f}-{self.pool.sr_max:.2f}* "
                f"({self._community_bias_overlay():+.2f}* temporary from "
                f"{base_lo:.2f}-{base_hi:.2f}*). Future picks use it; it "
                "resets when its voters leave or the room empties."
            )
        else:
            self._note_solo_difficulty_choice(next(iter(supporters)))
            self.mark_auto_retune_pending()
            if self.state == IDLE:
                self.apply_pending_auto_retune()
            base = float(self.cfg.get("auto_sr_bias", 0.30))
            self.say(
                f"{direction.title()} vote passed ({vote_count}/{votes_required}) "
                f"— future picks trend "
                f"{'harder' if actual_delta > 0 else 'easier'} "
                f"({actual_delta:+.2f}* temporary; community "
                f"{self._community_bias_overlay():+.2f}*, baseline {base:+.2f}*)."
            )

        if self._difficulty_vote_changes_since_match > 2:
            self._difficulty_vote_rebuild_pending = True
            self.say(
                "Several difficulty changes were made — refreshing the "
                "upcoming map queue before the next countdown."
            )

    def cast_difficulty_vote(self, user_id: int, direction: str) -> None:
        """Vote for one temporary AutoDiff bias or fixed-range step."""
        direction = str(direction).casefold()
        if direction not in ("harder", "easier"):
            return

        eligible = self._difficulty_voter_ids()
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return
        if uid not in eligible:
            return

        # Expire a stale two-map voting window before interpreting this command
        # as the first vote in a new one.
        if (
            self._difficulty_vote_required
            and self._completed_map_count
            >= self._difficulty_vote_expires_after_map
        ):
            self._clear_difficulty_vote("two-map window expired")

        opened = self._difficulty_vote_required <= 0
        if opened:
            self._difficulty_vote_player_count = len(eligible)
            self._difficulty_vote_required = self.difficulty_votes_required(
                len(eligible)
            )
            self._difficulty_vote_expires_after_map = (
                self._completed_map_count + 2
            )

        # A player has one preference vote at a time and may change their mind
        # during the two-map window. Prompts are tracked per direction below.
        self._difficulty_vote_harder.discard(uid)
        self._difficulty_vote_easier.discard(uid)
        votes = (
            self._difficulty_vote_harder
            if direction == "harder"
            else self._difficulty_vote_easier
        )
        votes.add(uid)

        vote_count = len(votes)
        votes_required = self._difficulty_vote_required
        if vote_count < votes_required:
            # One prompt per direction, then the outcome. Changing preference
            # may introduce the other direction, but never intermediate counts.
            announced = getattr(self, "_difficulty_vote_announced", set())
            if direction not in announced:
                self.say(
                    f"{direction.title()} vote: {vote_count}/{votes_required} "
                    f"— type {self.prefix}{direction}"
                )
                announced.add(direction)
                self._difficulty_vote_announced = announced
            return

        supporters = set(votes)
        self._apply_passed_difficulty_vote(
            direction, supporters, vote_count, votes_required
        )

    def _on_difficulty_voter_left(self, user_id: int) -> None:
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return
        self._difficulty_vote_harder.discard(uid)
        self._difficulty_vote_easier.discard(uid)
        self._refresh_difficulty_vote_quorum()
        fixed_mode = self._fixed_difficulty_mode()
        fixed_layer_removed = False
        kept_layers: list[dict] = []
        for layer in self._difficulty_bias_layers:
            supporters = layer.get("supporters")
            if not isinstance(supporters, set):
                supporters = set()
            supporters.discard(uid)
            if fixed_mode and not supporters:
                # A fixed-room step lasts while at least one of the players
                # who passed it is still present. In a one-player room this
                # naturally resets when that player leaves; group votes do not
                # disappear merely because one supporter disconnected.
                fixed_layer_removed = True
                continue
            layer["supporters"] = supporters
            kept_layers.append(layer)
        self._difficulty_bias_layers = kept_layers

        if fixed_layer_removed:
            self._fixed_difficulty_dirty = True
            applied = self._apply_fixed_community_range(
                f"temporary voter {uid} left"
            )
            # The caller has already removed the departing player. If anyone
            # remains, rebuild visible maps at the next safe boundary. If the
            # room is empty, the same pending flag is held until
            # auto_empty_reset_seconds expires (or somebody joins first).
            self._fixed_range_rebuild_pending = True

    def _advance_community_difficulty_after_map(self) -> None:
        """Expire public votes and decay unsupported temporary bias layers."""
        self._completed_map_count += 1

        if (
            self._difficulty_vote_required
            and self._completed_map_count
            >= self._difficulty_vote_expires_after_map
        ):
            self._clear_difficulty_vote("two-map window expired")

        # Fixed-room shifts do not decay by map count. They stay exact until
        # one of their voters leaves or the empty-room reset runs.
        if self._fixed_difficulty_mode():
            return

        supported_layers: list[dict] = []
        unsupported_net = 0.0

        for layer in self._difficulty_bias_layers:
            amount = float(layer.get("amount") or 0.0)
            supporters = layer.get("supporters")
            if not isinstance(supporters, set):
                supporters = set()

            if supporters:
                if abs(amount) > 0.001:
                    supported_layers.append({
                        "amount": amount,
                        "supporters": supporters,
                    })
            else:
                unsupported_net += amount

        changed = False
        if abs(unsupported_net) > 0.001:
            old_unsupported = unsupported_net
            unsupported_net -= math.copysign(
                min(0.05, abs(unsupported_net)),
                unsupported_net,
            )
            changed = abs(unsupported_net - old_unsupported) > 0.0001

        self._difficulty_bias_layers = supported_layers
        if abs(unsupported_net) > 0.001:
            self._difficulty_bias_layers.append({
                "amount": unsupported_net,
                "supporters": set(),
            })

        if changed:
            self.mark_auto_retune_pending()
            log.info(
                "community difficulty decayed toward admin baseline; "
                "temporary offset now %+.2f*",
                self._community_bias_overlay(),
            )

    @staticmethod
    def skip_votes_required(player_count: int) -> int:
        """Return the public vote threshold for the current lobby size."""
        n = max(0, int(player_count or 0))
        if n <= 1:
            return 1
        if n == 2:
            return 2
        if n <= 4:
            return 2
        if n <= 6:
            return 3
        return 4

    def skip_voter_ids(self) -> set[int]:
        """Players eligible to participate in a vote-skip.

        Only actual gameplay players count. Spectators, referees, and players
        who left must never inflate a quorum or keep an in-progress vote from
        completing after the lobby population changes.
        """
        return {int(uid) for uid in self.gameplay_player_ids()}

    def _refresh_skip_vote_quorum(self) -> bool:
        """Recalculate an open skip vote from current playable users."""
        if self._skip_vote_until <= 0:
            return False
        eligible = self.skip_voter_ids()
        if not eligible:
            self._clear_skip_vote("no eligible voters remain")
            return False
        self._skip_votes.intersection_update(eligible)
        self._skip_vote_player_count = len(eligible)
        self._skip_vote_required = self.skip_votes_required(len(eligible))
        if len(self._skip_votes) >= self._skip_vote_required:
            self._complete_vote_skip()
            return True
        return False

    def _clear_skip_vote(self, reason: str = "") -> None:
        if self._skip_vote_until > 0 and reason == "vote window expired":
            self.say(
                f"Skip vote expired—{len(self._skip_votes)}/{self._skip_vote_required} "
                "votes; map kept."
            )
        if self._skip_vote_until > 0 and reason:
            log.debug(
                "clearing vote-skip for item %s (%s)",
                self._skip_vote_item_id, reason,
            )
        self._skip_votes.clear()
        self._skip_vote_until = 0.0
        self._skip_vote_item_id = None
        self._skip_vote_required = 0
        self._skip_vote_player_count = 0

    def _refresh_replay_vote_quorum(self) -> bool:
        """Recalculate an open replay vote from the current player roster."""
        if self._replay_vote_until <= 0:
            return False
        if (
            self.last_played_map is None
            or self.current_item_id != self._replay_vote_item_id
            or int(self.last_played_map.beatmap_id)
            != self._replay_vote_last_beatmap_id
        ):
            self._clear_replay_vote("map context changed")
            return False
        eligible = self.skip_voter_ids()
        if not eligible:
            self._clear_replay_vote("no eligible voters remain")
            return False
        self._replay_votes.intersection_update(eligible)
        self._replay_vote_player_count = len(eligible)
        self._replay_vote_required = self.skip_votes_required(len(eligible))
        if len(self._replay_votes) >= self._replay_vote_required:
            self._complete_replay_vote()
            return True
        return False

    def _clear_replay_vote(self, reason: str = "") -> None:
        if self._replay_vote_until > 0 and reason == "vote window expired":
            self.say("Replay vote expired—next map kept.")
        if self._replay_vote_until > 0 and reason:
            log.debug(
                "clearing replay vote for item %s (%s)",
                self._replay_vote_item_id, reason,
            )
        self._replay_votes.clear()
        self._replay_vote_until = 0.0
        self._replay_vote_item_id = None
        self._replay_vote_last_beatmap_id = None
        self._replay_vote_required = 0
        self._replay_vote_player_count = 0

    def _complete_replay_vote(self) -> None:
        replay_map = self.last_played_map
        votes_cast = len(self._replay_votes)
        votes_required = int(self._replay_vote_required or 1)
        self._clear_replay_vote("vote passed")
        if replay_map is None:
            return
        self.ready.clear()
        if self.replay_last_map(replay_map):
            self.say(
                f"Replay vote passed ({votes_cast}/{votes_required}) — "
                f"{replay_map.title[:45]} is next."
            )
        else:
            self.say("Replay vote passed, but the playlist is still syncing. Try again in a moment.")

    def cast_replay_vote(
        self,
        user_id: int,
        vote_time: float | None = None,
        command_item_id: int | None = None,
        command_beatmap_id: int | None = None,
    ) -> None:
        """Vote to play the immediately preceding completed map again."""
        if self.state == PLAYING:
            self.say("Can't replay mid-match.")
            return
        if self.state == SCORING:
            return
        if self.current_map is None or self.current_item_id is None:
            return
        if self.last_played_map is None:
            self.say("No completed map is available to replay yet.")
            return
        if (
            command_item_id is not None
            and int(command_item_id) != int(self.current_item_id)
        ):
            return
        if command_beatmap_id is not None and int(command_beatmap_id) != int(self.current_map.beatmap_id):
            return

        eligible = self.skip_voter_ids()
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return
        if uid not in eligible:
            return

        now = time.time()
        try:
            sent_at = float(vote_time) if vote_time is not None else now
        except (TypeError, ValueError):
            sent_at = now
        same_vote = (
            self._replay_vote_until > 0
            and self._replay_vote_item_id == int(self.current_item_id)
            and self._replay_vote_last_beatmap_id
            == int(self.last_played_map.beatmap_id)
        )
        active_vote = same_vote and sent_at <= self._replay_vote_until
        if same_vote and not active_vote:
            return

        if not active_vote:
            self._clear_replay_vote("new vote")
            if self.state == STARTING:
                if not self._stop_match_countdown():
                    return
                self.starting_map = None
                self.starting_item_id = None
                self._clear_start_recovery_tracking()
                self.set_state(IDLE)
            self._replay_vote_item_id = int(self.current_item_id)
            self._replay_vote_last_beatmap_id = int(self.last_played_map.beatmap_id)
            self._replay_vote_player_count = len(eligible)
            self._replay_vote_required = self.skip_votes_required(len(eligible))
            self._replay_vote_until = now + 8.0
            self._replay_votes.add(uid)
            if len(self._replay_votes) >= self._replay_vote_required:
                self._complete_replay_vote()
                return
            self.say(
                f"Replay vote: {len(self._replay_votes)}/{self._replay_vote_required} "
                f"— type {self.prefix}replay "
                f"({self._replay_vote_required - len(self._replay_votes)} more)."
            )
            return

        self._replay_votes.add(uid)
        if len(self._replay_votes) >= self._replay_vote_required:
            self._complete_replay_vote()

    def _append_future_map(self, candidate) -> int | None:
        """Append one known map and retain its playlist identity if returned."""
        if not self._operator_map_allowed(candidate):
            raise ValueError('Chart is excluded by operator policy or quarantine')
        rules = self._playlist_rules_for_map(candidate)
        resp = self._referee_operation_with_rejoin(
            "AddPlaylistItem",
            lambda: self.hub.add_playlist_item(
                self.room_id, candidate.beatmap_id,
                **rules,
            ),
        )
        item_id = (resp or {}).get("id") if isinstance(resp, dict) else None
        if item_id is not None:
            item_id = int(item_id)
            self._playlist_maps[item_id] = candidate
        self.queued.append((item_id, candidate))
        self._prefetch_map_pp(candidate)
        self._playlist_dirty = True
        return item_id

    def replay_last_map(self, replay_map) -> bool:
        """Make the last completed map the immediate next map, safely.

        osu! only permits appending future playlist entries.  The selected
        current entry is therefore edited to the replay map, then every known
        future entry is removed and appended again in its original order.
        This makes the replay next without losing the existing queue.
        """
        if not self._operator_map_allowed(replay_map):
            self.say('That map is excluded from this lobby.')
            return False
        if getattr(replay_map, 'is_dt', False):
            if not self._dt_selection_enabled():
                self.say('Enable DT with .dt before replaying a DT map.')
                return False
            if not self._check_dt_revision(replay_map, force=True):
                return False
        if self.state != IDLE or not self.room_id or self.current_item_id is None:
            return False
        try:
            if not self.reconcile_playlist(
                reason="pre-replay", fetch_remote=True,
                ensure_depth=False, trim_extras=False,
            ):
                return False
        except HubTransportError:
            raise
        except Exception as exc:
            log.warning("could not audit playlist before replay: %s", exc)
            return False
        if self.current_map is None or self.current_item_id is None:
            return False
        if int(self.current_map.beatmap_id) == int(replay_map.beatmap_id):
            return True

        preserved = list(self.queued)
        if any(item_id is None for item_id, _candidate in preserved):
            log.warning("replay postponed because a future playlist item has no server id")
            return False
        try:
            self.apply_map(replay_map, announce=True)
            for item_id, _candidate in preserved:
                self._referee_operation_with_rejoin(
                    "RemovePlaylistItem",
                    lambda item_id=int(item_id): self.hub.remove_playlist_item(
                        self.room_id, item_id),
                )
                self._server_playlist_items.pop(int(item_id), None)
                self._playlist_maps.pop(int(item_id), None)
            self.queued.clear()
            for _item_id, candidate in preserved:
                self._append_future_map(candidate)
        except HubTransportError:
            raise
        except Exception as exc:
            log.exception("could not rebuild queue for replay")
            self._playlist_dirty = True
            self._last_playlist_audit = 0.0
            return False

        self._playlist_dirty = True
        self._last_playlist_audit = 0.0
        self._sync_countdown_wait_item()
        self.set_state(IDLE)
        log.info("replay queued next: %s; restored %d future item(s)", replay_map.label, len(preserved))
        return True

    def queue_freedom_dive(self, user_id: int) -> None:
        """Hidden Standard-only novelty command: put FREEDOM DiVE next."""
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return
        if uid not in self.gameplay_player_ids():
            return
        # This uses the Standard ranked library only. It is never available to
        # native Mania or the separate Loved library.
        if self.cfg.get("ruleset_id", 0) != 0 or getattr(self.pool, "loved_mode", False):
            self.say("That map is only available in Standard Ranked lobbies.")
            return
        if not self.room_id or self.current_item_id is None:
            self.say("The playlist is still syncing — try again in a moment.")
            return
        if self.state in (STARTING, PLAYING, SCORING):
            if getattr(self, "_pending_freedom_dive_user_id", None) is None:
                self._pending_freedom_dive_user_id = uid
                self.say("FREEDOM DiVE will be queued next after this map.")
            else:
                self.say("FREEDOM DiVE is already scheduled for the next map.")
            return
        if self.state != IDLE:
            self.say("The playlist is still syncing — try again in a moment.")
            return
        try:
            if not self.reconcile_playlist(
                reason="pre-freedomdive", fetch_remote=True,
                ensure_depth=False, trim_extras=False,
            ):
                self.say("Playlist is still syncing — try again in a moment.")
                return
        except HubTransportError:
            raise
        except Exception as exc:
            log.warning("could not audit playlist before FREEDOM DiVE: %s", exc)
            self.say("Playlist is still syncing — try again in a moment.")
            return
        if self.current_map is not None and int(self.current_map.beatmap_id) == _FREEDOM_DIVE.beatmap_id:
            self.say("FREEDOM DiVE is already next.")
            return
        if any(int(candidate.beatmap_id) == _FREEDOM_DIVE.beatmap_id for _item_id, candidate in self.queued):
            self.say("FREEDOM DiVE is already in the queue.")
            return
        preserved = list(self.queued)
        if any(item_id is None for item_id, _candidate in preserved):
            self.say("Playlist is still syncing — try again in a moment.")
            return
        try:
            cooldown = self._external_float(
                self.cfg.get("freedom_dive_cooldown_seconds", 14400.0),
                60.0, 604800.0,
            )
            claimed, remaining, claim_time = self.process_control.claim_cooldown(
                "freedomdive", cooldown,
            )
        except Exception as exc:
            # Avoid a cross-room cooldown bypass when its shared guard is down.
            log.warning("could not claim FREEDOM DiVE cooldown: %s", exc)
            self.say("FREEDOM DiVE is temporarily unavailable — try again later.")
            return
        if not claimed:
            minutes = max(1, math.ceil(remaining / 60.0))
            self.say(f"FREEDOM DiVE is on cooldown ({minutes} minute{'s' if minutes != 1 else ''} remaining).")
            return
        try:
            # Append first, then remove/re-add prior futures so the server's
            # append-only ordering makes FREEDOM DiVE the next queued map.
            self._append_future_map(_FREEDOM_DIVE)
            freedom_entry = self.queued.pop()
            for item_id, _candidate in preserved:
                self._referee_operation_with_rejoin(
                    "RemovePlaylistItem",
                    lambda item_id=int(item_id): self.hub.remove_playlist_item(
                        self.room_id, item_id),
                )
                self._server_playlist_items.pop(int(item_id), None)
                self._playlist_maps.pop(int(item_id), None)
            self.queued = [freedom_entry]
            for _item_id, candidate in preserved:
                self._append_future_map(candidate)
        except HubTransportError:
            self.process_control.release_cooldown("freedomdive", claim_time)
            raise
        except Exception:
            self.process_control.release_cooldown("freedomdive", claim_time)
            log.exception("could not queue FREEDOM DiVE")
            self._playlist_dirty = True
            self._last_playlist_audit = 0.0
            self.say("FREEDOM DiVE is syncing into the queue.")
            return
        self._playlist_dirty = True
        self._last_playlist_audit = 0.0
        self.say("FREEDOM DiVE queued next.")

    def _run_pending_freedom_dive(self) -> bool:
        """Queue a verified in-match request at the safe map boundary."""
        uid = getattr(self, "_pending_freedom_dive_user_id", None)
        if uid is None or self.state != IDLE:
            return False
        self._pending_freedom_dive_user_id = None
        self.queue_freedom_dive(uid)
        # The queue operation may reconcile and mutate several future items;
        # yield this rotate pass before any automatic countdown is considered.
        return True

    def _complete_vote_skip(self) -> None:
        skipped_map = self.current_map
        skipped_item_id = self.current_item_id
        votes_cast = len(self._skip_votes)
        votes_required = int(self._skip_vote_required or 1)
        player_count = int(self._skip_vote_player_count or 0)

        self.say(f"Skip vote passed ({votes_cast}/{votes_required}) — skipping.")
        if skipped_map is not None:
            try:
                self.ratings.record_skip(
                    beatmap_id=int(skipped_map.beatmap_id),
                    room_id=int(self.room_id) if self.room_id is not None else None,
                    playlist_item_id=(
                        int(skipped_item_id)
                        if skipped_item_id is not None else None
                    ),
                    player_count=player_count,
                    votes_cast=votes_cast,
                    votes_required=votes_required,
                )
            except Exception:
                # A DB write must never block live room rotation.
                log.exception(
                    "could not record vote-skip for beatmap %s",
                    getattr(skipped_map, "beatmap_id", None),
                )

        self._clear_skip_vote("vote passed")
        self.ready.clear()
        self.skip_map()

    def force_skip(self, actor_user_id: int) -> None:
        """Privileged skip that does not require public votes."""
        if self.state == SCORING:
            self.say("Scoring is still finishing; force-skip again in a moment.")
            return

        target_map = (
            self.playing_map if self.state == PLAYING else self.current_map
        )
        target_item_id = (
            self.playing_item_id if self.state == PLAYING
            else self.current_item_id
        )
        if target_map is None:
            self.say("No current map to force-skip.")
            return

        actor_name = (
            self._known_username(int(actor_user_id))
            or str(actor_user_id)
        )
        try:
            self.ratings.record_skip(
                beatmap_id=int(target_map.beatmap_id),
                room_id=(
                    int(self.room_id)
                    if self.room_id is not None else None
                ),
                playlist_item_id=(
                    int(target_item_id)
                    if target_item_id is not None else None
                ),
                player_count=self.gameplay_player_count(),
                votes_cast=0,
                votes_required=0,
                skip_type="force",
                actor_user_id=int(actor_user_id),
                actor_username=actor_name,
            )
        except Exception:
            log.exception(
                "could not record force-skip for beatmap %s",
                target_map.beatmap_id,
            )

        self._clear_skip_vote("force-skip")
        self.say("Skipping.")

        if self.state == PLAYING:
            # During gameplay the referee API owns advancement. Abort the
            # match and let the server promote the next playlist item rather
            # than editing playlist state underneath an active match.
            self._force_skip_abort_pending = True
            self._explicit_abort_pending = True
            self.hub.abort_match(self.room_id)
            return

        if self.state == STARTING:
            self._stop_match_countdown()
            self.starting_map = None
            self.starting_item_id = None
            self._clear_start_recovery_tracking()
            self.set_state(IDLE)

        self.ready.clear()
        self.skip_map()

    def cast_skip_vote(
        self,
        user_id: int,
        vote_time: float | None = None,
        command_item_id: int | None = None,
        command_beatmap_id: int | None = None,
    ) -> None:
        """Open or participate in the public eight-second vote-skip window.

        vote_time is the chat message's own timestamp when available.
        command_item_id and command_beatmap_id freeze the map visible when this
        chat poll batch started. Both are required because lazer idle skips
        replace the beatmap while retaining the current playlist item id.
        """
        if self.state == PLAYING:
            self.say("Can't skip mid-match.")
            return
        if self.state == SCORING:
            # The next current item is already visible, but score/rotation work
            # is still mutating the between-map state. Let that finish first.
            return
        if self.current_map is None or self.current_item_id is None:
            return

        if (
            command_item_id is not None
            and int(command_item_id) != int(self.current_item_id)
        ):
            log.debug(
                "ignoring stale .skip from chat batch item %s; current item is %s",
                command_item_id, self.current_item_id,
            )
            return
        if command_beatmap_id is not None and (
            self.current_map is None
            or int(command_beatmap_id) != int(self.current_map.beatmap_id)
        ):
            log.debug(
                "ignoring stale .skip from chat batch beatmap %s; current "
                "beatmap is %s",
                command_beatmap_id,
                getattr(self.current_map, "beatmap_id", None),
            )
            return

        eligible = self.skip_voter_ids()
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return
        if uid not in eligible:
            return

        now = time.time()
        try:
            sent_at = float(vote_time) if vote_time is not None else now
        except (TypeError, ValueError):
            sent_at = now

        same_vote_item = (
            self._skip_vote_until > 0
            and self._skip_vote_item_id == int(self.current_item_id)
        )
        active_vote = (
            same_vote_item
            and sent_at <= self._skip_vote_until
        )

        # If the old window has expired but has not yet been cleared by the
        # watchdog, do not turn a late message into a brand-new vote and spam a
        # second prompt. The watchdog performs one forced chat catch-up at the
        # deadline, clears the old window, and only a later command may open a
        # fresh vote.
        if same_vote_item and not active_vote:
            return

        if not active_vote:
            self._clear_skip_vote("new vote")

            # Stop an already-running numeric countdown before opening the vote
            # window. Returning locally to IDLE also suppresses any second
            # StartMatch until the eight-second hold expires.
            if self.state == STARTING:
                self._stop_match_countdown()
                self.starting_map = None
                self.starting_item_id = None
                self._clear_start_recovery_tracking()
                self.set_state(IDLE)

            player_count = len(eligible)
            required = self.skip_votes_required(player_count)
            self._skip_vote_item_id = int(self.current_item_id)
            self._skip_vote_required = required
            self._skip_vote_player_count = player_count
            self._skip_vote_until = now + 8.0
            self._skip_votes.add(uid)

            if len(self._skip_votes) >= required:
                self._complete_vote_skip()
                return

            self.say(
                f"Skip vote: {len(self._skip_votes)}/{required} — type "
                f"{self.prefix}skip ({required - len(self._skip_votes)} more)."
            )
            return

        # Votes still count normally; only opening and outcome are announced.
        self._skip_votes.add(uid)
        if len(self._skip_votes) >= self._skip_vote_required:
            self._complete_vote_skip()

    def skip_map(self) -> None:
        """Manual skip while idle.

        Skipping is different from normal post-match advancement: the current
        playlist item has not been played, so replacing it is intentional. If
        the replacement already existed in the pre-download queue, remove that
        duplicate queued entry first.
        """
        # RoomHub's AddPlaylistItem completion can legitimately return no
        # item id. Usually the later referee event fills it in, but a second
        # rapid .skip can arrive first. Promoting that anonymous queued map
        # without removing its server entry creates current+future duplicates
        # (the reported Blue Bird / Petit Rabbit's failure). Resolve playlist
        # truth synchronously before any mutation, and fail closed if the
        # server still cannot identify the queued entry.
        anonymous_maps = [
            queued_map for item_id, queued_map in self.queued
            if item_id is None
        ]
        if anonymous_maps:
            try:
                resolved = self.reconcile_playlist(
                    reason="pre-skip queued-id resolution",
                    fetch_remote=True,
                    ensure_depth=False,
                    trim_extras=False,
                )
            except HubTransportError:
                raise
            except Exception as exc:
                resolved = False
                log.warning("could not resolve queued item before skip: %s", exc)
            resolved_beatmaps = {
                int(queued_map.beatmap_id)
                for item_id, queued_map in self.queued
                if item_id is not None
            }
            missing_maps = [
                queued_map for queued_map in anonymous_maps
                if int(queued_map.beatmap_id) not in resolved_beatmaps
            ]
            # A just-written playlist item can be absent from an immediately
            # following GET snapshot. Preserve the local candidate and wait
            # for a later event/snapshot instead of mistaking that lag for a
            # confirmed removal and selecting a different replacement.
            queued_beatmaps = {
                int(queued_map.beatmap_id) for _item_id, queued_map in self.queued
            }
            for queued_map in missing_maps:
                if int(queued_map.beatmap_id) not in queued_beatmaps:
                    self.queued.append((None, queued_map))
                    queued_beatmaps.add(int(queued_map.beatmap_id))
            if not resolved or missing_maps or any(
                item_id is None for item_id, _m in self.queued
            ):
                log.warning(
                    "skip postponed because a queued playlist item has no "
                    "server id yet"
                )
                self.say(
                    "Playlist is still syncing — try skip again in a moment.",
                    automatic=True,
                )
                return

        skipped_map = self.current_map
        nxt = None
        while self.queued and nxt is None:
            item_id, candidate = self.queued.pop(0)
            if item_id is not None:
                try:
                    self._referee_operation_with_rejoin(
                        "RemovePlaylistItem",
                        lambda item_id=item_id: self.hub.remove_playlist_item(
                            self.room_id, item_id),
                    )
                    self._playlist_maps.pop(int(item_id), None)
                except HubTransportError:
                    raise
                except Exception as e:
                    log.warning(
                        "could not remove queued item %s before skip: %s",
                        item_id, e,
                    )

            # Difficulty and mod settings can change after a variant was
            # queued (for example, .easier followed by .skip). Revalidate before
            # promotion so an obsolete variant cannot abort the room worker.
            try:
                self._playlist_rules_for_map(candidate)
            except ValueError as exc:
                log.warning(
                    "queued replacement %s no longer eligible: %s; trying another",
                    getattr(candidate, "beatmap_id", None), exc,
                )
                continue
            if not self._check_dt_revision(candidate, force=True):
                continue

            # Recheck queued maps when they are about to become current. A set
            # can be download-disabled after it was originally queued.
            if self._map_is_downloadable(candidate, force=True):
                nxt = candidate
            else:
                log.warning(
                    "queued replacement %s became unavailable; trying another",
                    getattr(candidate, "beatmap_id", None),
                )

        if nxt is None:
            nxt = self._next_downloadable_map()

        if not nxt:
            self.say("Pool exhausted.")
            return

        self.apply_map(nxt, announce=True)
        self._record_mania_round(skipped_map)
        self.fill_queue()
        self.set_state(IDLE)

    def fill_queue(self) -> None:
        """Keep queue_ahead future items; with 2 this means 3 maps total."""
        n = max(0, int(self.cfg.get("queue_ahead", 0)))
        if n <= 0:
            return

        used = set()
        if self.current_map is not None:
            used.add(int(self.current_map.beatmap_id))
        used.update(int(m.beatmap_id) for _item_id, m in self.queued)

        attempts = 0
        max_attempts = max(20, n * 10)
        while len(self.queued) < n and attempts < max_attempts:
            attempts += 1
            m = self._next_downloadable_map()
            if not m:
                break
            if int(m.beatmap_id) in used:
                continue

            try:
                rules = self._playlist_rules_for_map(m)
                resp = self._referee_operation_with_rejoin(
                    "AddPlaylistItem",
                    lambda: self.hub.add_playlist_item(
                        self.room_id, m.beatmap_id,
                        **rules,
                    ),
                )
            except HubTransportError:
                raise
            except Exception as e:
                # Definite non-membership business failures can still be retried
                # by a later audit. Error 2 membership loss is handled above.
                log.warning("could not queue ahead: %s; will retry later", e)
                return

            item_id = (resp or {}).get("id") if isinstance(resp, dict) else None
            if item_id is not None:
                item_id = int(item_id)
                self._playlist_maps[item_id] = m
            self.queued.append((item_id, m))
            used.add(int(m.beatmap_id))
            self._playlist_dirty = True
            self._prefetch_map_pp(m)
            log.debug("queued ahead silently: %s", m.label)

    def non_gameplay_user_ids(self) -> set[int]:
        """Return identities that must never count as gameplay players.

        The authenticated room operator is an invariant even when lazer's
        referee list is temporarily missing or stale during room recovery.
        """
        ids = {int(uid) for uid in self.referee_ids}
        try:
            authenticated_user_id = int(self.cfg.get("_authenticated_user_id"))
        except (TypeError, ValueError):
            authenticated_user_id = 0
        if authenticated_user_id > 0:
            ids.add(authenticated_user_id)
        return ids

    def gameplay_player_ids(self) -> set[int]:
        """Users who should count toward starting a match.

        Referees and the authenticated operator are excluded entirely. A
        normal user explicitly spectating also does not count until they
        return to a playable lobby status.
        """
        excluded = self.non_gameplay_user_ids()
        return {
            uid for uid, info in self.players.items()
            if uid not in excluded
            and not self._is_project_banned(uid)
            and info.get("status", "idle") != "spectating"
        }

    def gameplay_player_count(self) -> int:
        return len(self.gameplay_player_ids())

    def results_screen_start_is_held(self, active: set[int] | None = None) -> bool:
        """Pause automatic intermission while every gameplay user views results.

        Referees/spectators do not count. The referee protocol suppresses the
        native Results transition, leaving finished_play as the cached state
        until the player returns to Idle/Ready. Unknown states are not inferred
        as results. Only cached referee status is read; no API requests.
        Returning to the lobby begins a fresh ordinary auto-start wait, rather
        than charging time spent reading results against that wait.
        """
        active = set(active if active is not None else self.gameplay_player_ids())
        held = bool(active) and all(
            str(self.players.get(uid, {}).get("status") or "").casefold()
            in ("finished_play", "results")
            for uid in active
        )
        previous = getattr(self, "_results_screen_start_hold", False)
        self._results_screen_start_hold = held
        if self.state == IDLE and (held or previous):
            self._idle_since = time.time()
        return held

    def _close_if_armed_and_empty(self) -> bool:
        """Stop the bot once no regular players remain in the room."""
        # ``players`` contains normal gameplay users and spectators, but not
        # the authenticated operator/referees. It therefore exactly models
        # "only the ref account is left" for this shutdown command.
        if not self._close_when_empty or self.players:
            return False
        self._close_when_empty = False
        self._supervisor_reopenable_stop = True
        log.info("close-when-empty condition met; closing room %s", self.room_id)
        self.say("Lobby is empty — closing.")
        self.running = False
        return True

    def _has_recent_lobby_rater(self) -> bool:
        """Return whether a current lobby member rated any of the last 3 maps.

        Only good/bad votes qualify. Neutral feedback is intentionally stored
        for future use but does not yet stand in for a rating habit. Referees
        and the authenticated operator are excluded, while spectators remain
        eligible because they are still present to give map feedback in chat.
        """
        recent_map_ids = list(self._recent_rating_map_ids)
        if len(recent_map_ids) < 3:
            return False

        lobby_members = {
            int(uid) for uid in self.players
            if int(uid) not in self.non_gameplay_user_ids()
        }
        if not lobby_members:
            return False

        try:
            recent_raters = self.ratings.rater_ids_for_maps(
                recent_map_ids
            )
        except Exception:
            log.exception("could not check recent map feedback habits")
            return False
        return bool(lobby_members & recent_raters)

    def auto_difficulty_pp_values(
            self, active: set[int] | None = None
    ) -> tuple[list[float], tuple[int, float] | None, int]:
        """Return PP values used by auto difficulty.

        In lobbies with more than four PP-known gameplay players, exclude one
        weakest player (the lowest total-PP value) before taking the median.
        This keeps one low-ranked outlier from pulling the whole room's auto
        star range down while still letting smaller rooms represent everyone.

        Returns (pp_values_used, dropped_(user_id, pp)_or_None, known_count).
        """
        active = set(active if active is not None else self.gameplay_player_ids())
        rows: list[tuple[int, float]] = []
        for uid in active:
            raw_pp = self.players.get(uid, {}).get("pp")
            if raw_pp is None:
                continue
            try:
                pp = float(raw_pp)
            except (TypeError, ValueError):
                continue
            if pp <= 0:
                continue
            rows.append((uid, pp))

        known_count = len(rows)
        dropped: tuple[int, float] | None = None
        if known_count > 4:
            dropped = min(rows, key=lambda row: row[1])
            rows.remove(dropped)

        return [pp for _uid, pp in rows], dropped, known_count

    def single_player_start_is_held(self, active: set[int] | None = None) -> bool:
        """Return True when automatic starts should wait for a lone player.

        The hold only applies while the exact same single gameplay player is
        still present. As soon as the roster changes (another player joins, the
        held player leaves, etc.) the hold is cleared automatically.
        """
        held_uid = self._single_player_start_hold_uid
        if held_uid is None:
            return False

        active = set(active if active is not None else self.gameplay_player_ids())
        if active == {held_uid}:
            return True

        log.info(
            "single-player start hold cleared because gameplay roster changed "
            "(held=%s active=%s)",
            held_uid, sorted(active),
        )
        self._single_player_start_hold_uid = None
        return False

    def first_player_download_wait_is_active(
            self, active: set[int] | None = None) -> bool:
        """Return True while the first player after an empty room is loading.

        osu!'s referee events do not include a reliable "has current beatmap"
        boolean. READY is the usable signal that the lone player has finished
        loading/downloading and can enter gameplay. The gate is intentionally
        limited to the first gameplay player after an empty period; once a
        second gameplay player appears, normal auto-start behavior resumes.
        """
        wait_uid = self._first_player_download_wait_uid
        if wait_uid is None:
            return False

        if wait_uid not in self.players:
            self._first_player_download_wait_uid = None
            return False

        active = set(active if active is not None else self.gameplay_player_ids())

        # The player may temporarily appear as spectating while downloading.
        # Keep the gate in that case rather than treating the room as ready.
        if not active or active == {wait_uid}:
            return True

        log.info(
            "first-player download wait cleared because another gameplay "
            "player is present (wait_uid=%s active=%s)",
            wait_uid, sorted(active),
        )
        self._first_player_download_wait_uid = None
        return False

    @staticmethod
    def _is_recoverable_start_state_error(exc: Exception) -> bool:
        """Return True for server replies that mean local room state is stale.

        These are *definite* referee-hub rejections, not transport failures,
        but they are still recoverable. Live lazer has shown that a successful
        StartMatch completion can be followed by silent loss of the referee
        room subscription; the next StartMatch then returns Error 9 because the
        match is already running, and CloseRoom says the referee is no longer
        in the room. Treat those replies as a reason to rejoin/audit, never as
        a fatal process error.
        """
        text = str(exc).casefold()
        return (
            "error 9" in text
            and "current room state" in text
        ) or (
            "user is not in the room" in text
            or "user is not in room" in text
            or "not in the room" in text
        )

    def _clear_start_recovery_tracking(self) -> None:
        self._start_command_acknowledged = False
        self._start_event_gap = False
        self._start_rejoin_last_attempt = 0.0
        self._start_snapshot_players.clear()
        self._start_countdown_seen = False
        self._start_countdown_stopped_at = 0.0
        self._active_countdown_seconds = 0.0

    def _recover_missed_start_event(self) -> bool:
        """Reconcile a StartMatch that was acknowledged but produced no event.

        Returns True while the bot should remain in STARTING. Returns False if
        server truth proves the start never happened and normal IDLE behavior
        may resume. If the starting playlist item is now marked played, synthesize
        the missing completion path so rotation/scoring can continue without a
        blind second StartMatch.
        """
        if self.state != STARTING or self.starting_item_id is None:
            return False

        rid = int(self.room_id) if self.room_id is not None else None
        if rid is None:
            return False

        start_id = int(self.starting_item_id)
        now = time.time()
        self._start_event_gap = True

        probe = getattr(self.api, "room_probe", None)
        status, room = ("unknown", None)
        if callable(probe):
            try:
                status, room = probe(rid)
            except Exception as e:
                log.warning(
                    "missed-StartMatch recovery could not probe room %s: %s; "
                    "holding STARTING rather than retrying",
                    rid, e,
                )
                status, room = ("unknown", None)

        if status == "missing":
            # Let the existing confirmed-404 recovery own room replacement.
            self._room_missing_confirmations += 1
            needed = max(2, int(self.cfg.get("room_missing_confirmations", 2)))
            if self._room_missing_confirmations >= needed:
                self._recreate_missing_room("missed MatchStarted recovery")
                self._clear_start_recovery_tracking()
                return True
            log.warning(
                "room %s returned 404 during missed-StartMatch recovery (%d/%d); "
                "holding STARTING for confirmation",
                rid, self._room_missing_confirmations, needed,
            )
            return True

        if status == "ok" and room:
            self._room_missing_confirmations = 0
            playlist = room.get("playlist") or []
            start_item = None
            for raw in playlist:
                try:
                    if int(raw.get("id")) == start_id:
                        start_item = raw
                        break
                except Exception:
                    continue

            selected = room.get("playlist_item_id")
            if selected is None:
                selected = (room.get("current_playlist_item") or {}).get("id")

            played = bool(start_item and (
                start_item.get("played") or start_item.get("was_played")
            ))
            advanced = False
            if selected is not None:
                try:
                    advanced = int(selected) != start_id
                except Exception:
                    advanced = False

            # A played starting item (or a selected-item advance with that item
            # explicitly marked played) is hard server evidence that the first
            # StartMatch ran even though MatchStarted was lost.
            if played and (advanced or start_item is not None):
                log.warning(
                    "server confirms playlist item %s was played even though "
                    "MatchStarted was missed; synthesizing completion instead "
                    "of sending another StartMatch",
                    start_id,
                )
                self.playing_item_id = start_id
                self.playing_map = self.starting_map or self._map_for_item_id(start_id)
                self.playing_player_ids = set(self._start_snapshot_players)
                self._sync_from_room_snapshot(room, "missed MatchStarted completion")
                payload = {"playlist_item_id": start_id, "recovered": True}
                self.starting_map = None
                self.starting_item_id = None
                self._clear_start_recovery_tracking()
                self.handle_match_completed(payload)
                return True

            # Re-sync the visible queue without mutating it. During an active
            # match the starting item commonly remains unplayed until the end,
            # so "still current and unplayed" is NOT evidence that StartMatch
            # failed.
            try:
                self._sync_from_room_snapshot(room, "missed MatchStarted audit")
                self._last_playlist_audit = now
            except Exception as e:
                log.warning("missed-start snapshot sync failed: %s", e)

        # Refresh referee room membership/subscription periodically even when
        # the websocket itself never disconnected. This is the exact failure
        # mode observed when the same-account lazer client was booted mid-game.
        if now - float(self._start_rejoin_last_attempt or 0.0) >= 8.0:
            self._start_rejoin_last_attempt = now
            try:
                join = getattr(self.hub, "join_room", None)
                if callable(join):
                    join(rid)
                else:
                    self.hub.invoke("JoinRoom", [rid])
                log.warning(
                    "refreshed referee membership for room %s while recovering "
                    "a missed MatchStarted event",
                    rid,
                )
                try:
                    self._join_room_chat()
                except Exception:
                    pass
            except HubTransportError:
                raise
            except Exception as e:
                text = str(e).casefold()
                if "already" in text and "room" in text:
                    pass
                else:
                    # JoinRoom can itself be refused while the match is in a
                    # state where joining is illegal. That is not fatal; the
                    # room is still alive and we continue auditing server truth.
                    log.warning(
                        "JoinRoom(%s) during missed-start recovery was refused: "
                        "%s; continuing to hold STARTING",
                        rid, e,
                    )

        # Give the acknowledged start enough time to finish naturally. Use the
        # actual starting map length when known, otherwise the active pool cap.
        # Only after that generous bound, and only if the item is still not
        # marked played, may we conclude the start probably never happened.
        cd = float(self.cfg.get("countdown_seconds", 15))
        map_len = 0.0
        try:
            map_len = float(getattr(self.starting_map, "hit_length", 0) or 0)
        except Exception:
            map_len = 0.0
        expected_max = max(map_len, float(getattr(self.pool, "max_length", 0) or 0))
        give_up_after = cd + expected_max + 90.0
        elapsed = now - self._state_since
        if elapsed > give_up_after and status == "ok" and room:
            playlist = room.get("playlist") or []
            start_item = next((x for x in playlist
                               if str(x.get("id")) == str(start_id)), None)
            still_unplayed = bool(start_item) and not bool(
                start_item.get("played") or start_item.get("was_played")
            )
            selected = room.get("playlist_item_id")
            if selected is None:
                selected = (room.get("current_playlist_item") or {}).get("id")
            still_selected = selected is None or str(selected) == str(start_id)
            if still_unplayed and still_selected:
                log.error(
                    "StartMatch was acknowledged but no MatchStarted/completion "
                    "appeared for %.0fs and server still shows item %s unplayed; "
                    "giving up conservatively and returning to IDLE",
                    elapsed, start_id,
                )
                self.ready.clear()
                self.starting_map = None
                self.starting_item_id = None
                self._clear_start_recovery_tracking()
                self.set_state(IDLE)
                return False

        return True

    def start_match(self) -> None:
        if self.state != IDLE:
            return

        # An abort/cancel path can return to IDLE without passing through the
        # normal post-match rotate(). Never start a new countdown while an
        # admin-requested playlist rebuild is still pending.
        if self._pending_soft_restart:
            if not self.perform_soft_restart():
                return

        # Absolute gate before every countdown: verify osu!'s current item and
        # make sure the visible unplayed window is exactly current + queue_ahead.
        # If the remote snapshot cannot resolve a current map, do not start and
        # risk attaching scores/feedback to the wrong beatmap.
        if not self.reconcile_playlist(
                reason="pre-start", fetch_remote=True,
                ensure_depth=True, trim_extras=True):
            log.warning("start postponed until playlist state is consistent")
            return

        if getattr(self.current_map, 'is_dt', False):
            if not self._dt_selection_enabled() or not self._check_dt_revision(self.current_map, force=True):
                log.warning('DT pre-start verification failed; holding start and requesting safe rebuild')
                self._pending_soft_restart = True
                return

        # Availability can change after a map was first queued (for example a
        # copyright/download restriction). Recheck the actual current item
        # immediately before StartMatch and replace it instead of letting the
        # room countdown into a map players cannot download.
        if (
            self.current_map is not None
            and not self._map_is_downloadable(self.current_map, force=True)
        ):
            log.warning(
                "pre-start availability check rejected current beatmap %s; "
                "replacing it",
                self.current_map.beatmap_id,
            )
            self.say(
                "Current map is unavailable for download; replacing it.",
                automatic=True,
            )
            self._clear_skip_vote("current map unavailable")
            self.ready.clear()
            self.skip_map()
            return

        minimum = int(self.cfg.get("min_players", 1))
        actual = self.gameplay_player_count()
        if actual < minimum:
            log.info("not starting: %d/%d gameplay players", actual, minimum)
            return

        active = self.gameplay_player_ids()
        if len(active) >= 2 and not (self.ready & active):
            self._say_problem_once("start-no-ready", "At least one player must ready up to start.")
            return

        # Final room-settings gate before countdown. If an earlier active
        # match forced a title update to be deferred, IDLE is the last safe
        # point to apply it before StartMatch makes room settings illegal.
        self.apply_pending_room_name_update()

        self._sync_countdown_wait_item()
        cd = float(self.cfg.get("countdown_seconds", 15) or 15)
        if not self._manual_start_bypass_ready:
            cd += self._active_chat_countdown_extension()
        # Give newly unready players one shared grace window, rather than
        # allowing them to block every map. Referees/spectators do not count.
        granted = getattr(self, "_unready_start_grace_users", set())
        granted.intersection_update(active)
        fresh_unready = (active - self.ready - granted) if len(active) >= 2 else set()
        if fresh_unready:
            cd += 5
        self._unready_start_grace_users = granted
        granted.update(fresh_unready)
        # The referee hub contract requires an integer countdown.  Keep the
        # arithmetic above in seconds so configurable extensions remain easy
        # to combine, then normalize exactly once before crossing that API
        # boundary.  Sending JSON 10.0 instead of 10 is rejected by osu! and
        # would otherwise stop the room worker on its first match.
        cd = max(1, int(round(cd)))
        self._active_countdown_seconds = float(cd)
        self._start_immediate_retry = False
        self._explicit_abort_pending = False

        # This snapshot is what later score/rating logic follows. Do it before
        # StartMatch so PlaylistItemChanged cannot move current_map underneath
        # us during the countdown.
        self.starting_map = self.current_map
        self.starting_item_id = self.current_item_id
        self._start_command_acknowledged = False
        self._start_event_gap = False
        self._start_rejoin_last_attempt = 0.0
        self._start_snapshot_players = set(self.gameplay_player_ids())
        self._start_countdown_seen = False
        self._start_countdown_stopped_at = 0.0

        # Enter STARTING BEFORE sending the hub command. If TLS/SignalR dies
        # after osu! receives StartMatch but before the completion arrives, the
        # command outcome is unknowable. Keeping STARTING prevents a blind
        # second StartMatch; after reconnect the existing countdown-recovery
        # path audits server truth instead.
        # Consume elapsed .wait time only after all pre-flight checks pass.
        # Requester identities remain map-scoped, so a different player may
        # still use .wait during the numeric countdown unless waits were
        # explicitly suppressed by .start or start-pressure chat.
        self._lock_countdown_wait_for_start()
        self._countdown_wait_force_item_id = None
        self._countdown_wait_force_beatmap_id = None
        self.set_state(STARTING)
        try:
            self._referee_operation_with_rejoin(
                "StartMatch",
                lambda: self.hub.start_match(self.room_id, countdown=cd),
            )
            # A successful completion means osu! accepted the command. Do not
            # later resend StartMatch merely because CountdownStarted or
            # MatchStarted was lost with the referee room subscription.
            self._start_command_acknowledged = True
        except HubTransportError:
            self._start_event_gap = True
            log.warning(
                "StartMatch transport result unknown; preserving STARTING "
                "state and recovering the referee connection without retrying"
            )
            raise
        except Exception as e:
            if self._is_recoverable_start_state_error(e):
                # Live failure mode: the first StartMatch may already be
                # running, but this referee silently lost room membership and
                # a later retry gets Error 9 / not-in-room. Holding STARTING
                # is safer than crashing or sending yet another start.
                self._start_command_acknowledged = True
                self._start_event_gap = True
                log.warning(
                    "StartMatch was rejected with stale room/referee state: %s; "
                    "holding STARTING and rejoining/auditing instead of crashing",
                    e,
                )
                return
            granted.difference_update(fresh_unready)
            # A definite unrelated server/business rejection did not start a
            # countdown and remains a real error.
            self.starting_map = None
            self.starting_item_id = None
            self._clear_start_recovery_tracking()
            self.set_state(IDLE)
            raise
        # lazer already renders the countdown. Optional feedback hints wait
        # for six maps, ten minutes, and a quiet gap after useful chat.
        self._maybe_prompt_for_rating()

    def _stop_match_countdown(self) -> bool:
        """Stop a pending countdown using the dedicated referee operation."""
        try:
            stop = getattr(self.hub, "stop_match_countdown", None)
            if callable(stop):
                stop(self.room_id)
                return True

            # RefereeHub exposes generic invoke(). This keeps this file usable
            # without requiring a simultaneous hub.py replacement.
            invoke = getattr(self.hub, "invoke", None)
            if callable(invoke):
                invoke("StopMatchCountdown", [int(self.room_id)])
                return True

            # Local fake/older test doubles may only expose abort_match.
            self.hub.abort_match(self.room_id)
            return True
        except HubTransportError:
            # StopMatchCountdown may have reached the server. Preserve STARTING
            # and reconcile after transport recovery instead of pretending the
            # countdown definitely stopped.
            raise
        except Exception as e:
            log.warning("could not stop pending match countdown: %s", e)
            return False

    def cancel_start_if_too_few_players(self) -> bool:
        """Cancel a pending countdown if the room drops below min_players."""
        if self.state != STARTING:
            return False

        minimum = int(self.cfg.get("min_players", 1))
        actual = self.gameplay_player_count()
        if actual >= minimum:
            return False

        log.info("cancelling pending start: %d/%d gameplay players remain",
                 actual, minimum)
        self._stop_match_countdown()
        self.ready.clear()
        self.starting_map = None
        self.starting_item_id = None
        self._clear_start_recovery_tracking()
        self.set_state(IDLE)
        self.say("Start cancelled — waiting for players.", automatic=True)
        return True

    def ready_threshold_met(self) -> bool:
        """Start once enough actual gameplay players have readied up.

        A one-player lobby is deliberately strict: that player must currently
        be READY. The auto-start timer is never allowed to substitute for READY
        when only one gameplay player is present.
        """
        active = self.gameplay_player_ids()
        n = len(active)
        if n == 0:
            return False
        r = len(self.ready & active)
        if n == 1:
            return r == 1
        # An AFK or undownloaded player must not hold ready players hostage.
        return r > 0

    def cancel_single_player_start_if_unready(
        self, status: str | None = None, *, previously_ready: bool = False,
    ) -> bool:
        """Cancel a countdown when the lone player explicitly becomes unready.

        READY is a hard requirement for a one-player lobby. Ignore gameplay
        terminal/transitional statuses here so a late ``playing`` event cannot
        race MatchStarted and make us stop a match that has actually begun.
        """
        if self.state != STARTING:
            return False

        active = self.gameplay_player_ids()
        if len(active) != 1:
            return False

        # A player/admin who explicitly typed .start asked for the countdown
        # now. Do not immediately undo that request merely because the lone
        # player has not toggled READY. An actual READY -> unready transition
        # withdraws that readiness even after .start: cancel before the server
        # consumes this playlist item at the end of a nobody-ready countdown.
        # The bypass lasts only for this STARTING state and is cleared when it
        # ends. Initial/repeated unready observations keep the manual behavior.
        if self._manual_start_bypass_ready and not previously_ready:
            return False

        uid = next(iter(active))
        normalized = str(status or "").casefold()
        if normalized in ("playing", "finished_play"):
            return False
        if uid in self.ready:
            return False

        log.info(
            "cancelling single-player countdown: user %s is no longer READY",
            uid,
        )
        self._stop_match_countdown()
        self.ready.clear()
        self.starting_map = None
        self.starting_item_id = None
        self._clear_start_recovery_tracking()
        self.set_state(IDLE)
        self.say("Start cancelled — waiting for ready.", automatic=True)
        return True

    # Periodic unsolicited command reminders were intentionally removed.
    # Players can use .help when they want the command list.

    def _start_from_command(self, user_id: int, *, privileged: bool) -> None:
        """Start the configured countdown immediately for an explicit .start.

        Public callers are validated before reaching this helper. Privileged
        callers retain the historical ability to clear recovery/download
        holds. A failed pre-start audit must not leave the READY bypass armed
        for a later automatic start.
        """
        if self.state not in (IDLE, STARTING):
            return

        if self.state == IDLE:
            active = self.gameplay_player_ids()
            if len(active) >= 2 and not (self.ready & active):
                self._say_problem_once("start-no-ready", "At least one player must ready up to start.")
                return

        # An explicit .start overrides pending and future .wait requests for
        # this map. If a countdown is already active, leave it running and only
        # apply the map-scoped wait lock.
        self._sync_countdown_wait_item()
        self._suppress_countdown_wait(
            f"explicit {'privileged' if privileged else 'player'} .start"
        )
        if self.state != IDLE:
            return

        if self._single_player_start_hold_uid is not None:
            log.info(
                "%s .start cleared single-player start hold for user %s",
                "privileged" if privileged else "player",
                self._single_player_start_hold_uid,
            )
            self._single_player_start_hold_uid = None
        if self._first_player_download_wait_uid is not None:
            log.info(
                "%s .start cleared first-player download wait for user %s",
                "privileged" if privileged else "player",
                self._first_player_download_wait_uid,
            )
            self._first_player_download_wait_uid = None

        self._manual_start_bypass_ready = True
        self.start_match()
        if self.state != STARTING:
            self._manual_start_bypass_ready = False
            self._countdown_wait_force_item_id = None
            self._countdown_wait_force_beatmap_id = None

    def set_state(self, s: str) -> None:
        if s != self.state:
            log.info("state %s -> %s", self.state, s)
        now = time.time()
        self.state = s
        if s == PLAYING:
            getattr(self, "_problem_notices", {}).pop("readiness", None)
        self._state_since = now
        if s != STARTING:
            self._start_immediate_retry = False
            self._manual_start_bypass_ready = False
        if s == IDLE:
            self._idle_since = now

    def _finish_aborted_match(self, announce: bool = True) -> None:
        """Return cleanly to IDLE after a server-confirmed abort."""
        was_active = self.state in (STARTING, PLAYING)
        aborted_map = self.playing_map or self.starting_map or self.current_map
        if self._activity_match_key or self.playing_item_id is not None:
            try:
                self.ratings.record_match_end(
                    match_key=self._activity_match_key,
                    room_id=self.room_id,
                    playlist_item_id=self.playing_item_id,
                    result="aborted",
                )
            except Exception:
                log.exception("could not mark aborted match in activity history")
        self._activity_match_key = None
        self.ready.clear()
        self.starting_map = None
        self.starting_item_id = None
        self._clear_start_recovery_tracking()
        self.playing_map = None
        self.playing_item_id = None
        self.playing_player_ids.clear()
        self.scoring_item_id = None
        self._explicit_abort_pending = False
        self._early_abort_pending = None
        self._match_started_at = 0.0
        self.set_state(IDLE)
        if was_active:
            self._notify_discord_lobby_event(
                "MATCH ABORTED",
                "Match aborted",
                map_key=self._discord_map_key(aborted_map),
            )
        if was_active and announce:
            self.say("Match aborted.", automatic=True)

    def _local_playlist_confirms_early_abort(self, item_id: int) -> bool:
        item = self._server_playlist_items.get(int(item_id))
        if item and (item.get("played") or item.get("expired")):
            return True
        current = self._server_current_item_id
        return current is not None and int(current) != int(item_id)

    def _room_snapshot_confirms_early_abort(
        self, room: dict | None, item_id: int
    ) -> bool:
        items = (room or {}).get("playlist") or []
        if not items:
            return False

        for raw in items:
            try:
                raw_id = int(raw.get("id"))
            except (TypeError, ValueError):
                continue
            if raw_id == int(item_id) and (
                self._playlist_item_played(raw)
                or bool(raw.get("expired", False))
            ):
                return True

        active = [
            raw for raw in items
            if not self._playlist_item_played(raw)
            and not bool(raw.get("expired", False))
        ]
        active.sort(key=self._playlist_item_order)
        if not active:
            return True
        try:
            current_id = int(active[0].get("id"))
        except (TypeError, ValueError):
            return False
        return current_id != int(item_id)

    def _resolve_pending_early_abort(
        self, reason: str, *, verify_remote: bool = False
    ) -> bool:
        """Resolve an abort received immediately after MatchStarted.

        lazer can emit a genuinely stale MatchAborted near countdown rollover,
        but it can also abort a real match in the same time window when all
        players leave gameplay. The old code guessed from timing alone. We now
        wait for playlist truth: if the played item is marked played/expired or
        the current item advances, the abort is real. Otherwise a short remote
        check lets the match continue as before.
        """
        pending = self._early_abort_pending
        if not pending:
            return False
        if self.state != PLAYING:
            self._early_abort_pending = None
            return False

        item_id = int(pending["item_id"])
        age = time.time() - float(pending["at"])

        confirmed = self._local_playlist_confirms_early_abort(item_id)
        room = None
        if not confirmed and verify_remote and age >= 1.0:
            try:
                room = self.api.room(self.room_id)
            except Exception as e:
                log.warning(
                    "could not verify early MatchAborted for item %s: %s",
                    item_id, e,
                )
            confirmed = self._room_snapshot_confirms_early_abort(room, item_id)

        if confirmed:
            log.warning(
                "early MatchAborted confirmed by %s for item %s; "
                "recovering to IDLE",
                reason, item_id,
            )
            if room:
                self._sync_from_room_snapshot(room, "early abort confirmation")
            self._finish_aborted_match(announce=True)
            return True

        if verify_remote and age >= 1.0 and room is not None:
            log.warning(
                "early MatchAborted for item %s was not confirmed by playlist "
                "state; treating it as the stale countdown event",
                item_id,
            )
            self._early_abort_pending = None
        return False

    # ---------------------------------------------------------- players

    @staticmethod
    def _format_lobby_time(seconds: float) -> str:
        """Format cumulative playtime as total hours:minutes (never days)."""
        total = max(0, int(seconds or 0))
        hours, rem = divmod(total, 3600)
        minutes = rem // 60
        return f"{hours}:{minutes:02d}"

    @staticmethod
    def _format_match_remaining(seconds: float) -> str:
        """Format a short, approximate remaining-match duration for chat."""
        total = max(0, int(round(float(seconds or 0))))
        minutes, remainder = divmod(total, 60)
        if minutes:
            return f"about {minutes}m {remainder:02d}s"
        return f"about {total}s"

    def _match_remaining_seconds(self) -> float | None:
        """Share the approximate map timer between .time and join greetings."""
        playing = self.playing_map or self.current_map
        try:
            length = float(getattr(playing, "hit_length", 0) or 0)
            started = float(self._match_started_at or self._state_since)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(length) or length <= 0 or not math.isfinite(started):
            return None
        elapsed = max(0.0, time.time() - started)
        return max(0.0, length - elapsed)

    def _sync_countdown_wait_item(self) -> int | None:
        """Reset .wait state when either current item or beatmap changes."""
        try:
            current_item_id = getattr(self, "current_item_id", None)
            item_id = int(current_item_id) if current_item_id is not None else None
        except (TypeError, ValueError):
            item_id = None
        try:
            current_map = getattr(self, "current_map", None)
            raw_beatmap_id = getattr(current_map, "beatmap_id", None)
            beatmap_id = int(raw_beatmap_id) if raw_beatmap_id is not None else None
        except (TypeError, ValueError):
            beatmap_id = None
        previous_item = getattr(self, "_countdown_wait_item_id", None)
        previous_beatmap = getattr(self, "_countdown_wait_beatmap_id", None)
        if not hasattr(self, "_countdown_wait_item_id"):
            self._countdown_wait_item_id = previous_item
        if not hasattr(self, "_countdown_wait_beatmap_id"):
            self._countdown_wait_beatmap_id = previous_beatmap
        requesters = getattr(self, "_countdown_wait_requesters", None)
        if requesters is None:
            requesters = self._countdown_wait_requesters = set()
        if not hasattr(self, "_countdown_wait_delay_seconds"):
            self._countdown_wait_delay_seconds = 0.0
        if not hasattr(self, "_countdown_wait_locked_item_id"):
            self._countdown_wait_locked_item_id = None
        if not hasattr(self, "_countdown_wait_locked_beatmap_id"):
            self._countdown_wait_locked_beatmap_id = None
        if not hasattr(self, "_countdown_wait_force_item_id"):
            self._countdown_wait_force_item_id = None
        if not hasattr(self, "_countdown_wait_force_beatmap_id"):
            self._countdown_wait_force_beatmap_id = None
        if (item_id, beatmap_id) != (previous_item, previous_beatmap):
            self._countdown_wait_item_id = item_id
            self._countdown_wait_beatmap_id = beatmap_id
            requesters.clear()
            self._countdown_wait_delay_seconds = 0.0
            self._countdown_wait_locked_item_id = None
            self._countdown_wait_locked_beatmap_id = None
            self._countdown_wait_force_item_id = None
            self._countdown_wait_force_beatmap_id = None
        return item_id

    def _start_wait_seconds(self) -> float:
        try:
            return self._external_float(
                self.cfg.get("start_wait_seconds", 10.0), 1.0, 60.0
            )
        except (TypeError, ValueError):
            return 10.0

    def _request_countdown_wait(self, user_id: int) -> None:
        """Add one player's delay, cancelling an active countdown if needed."""
        if self.state not in (IDLE, STARTING):
            return
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return
        active = self.gameplay_player_ids()
        if uid not in active:
            return
        item_id = self._sync_countdown_wait_item()
        beatmap_id = self._countdown_wait_beatmap_id
        if item_id is None or (
            self._countdown_wait_locked_item_id == item_id
            and self._countdown_wait_locked_beatmap_id == beatmap_id
        ):
            return
        if uid in self._countdown_wait_requesters:
            return

        cancelled_countdown = self.state == STARTING
        if cancelled_countdown:
            try:
                starting_beatmap_id = int(self.starting_map.beatmap_id)
            except (AttributeError, TypeError, ValueError):
                starting_beatmap_id = None
            if (
                self.starting_item_id is None
                or int(self.starting_item_id) != int(item_id)
                or starting_beatmap_id != beatmap_id
            ):
                log.warning(
                    "ignoring .wait during countdown because local current "
                    "map %s/%s differs from start snapshot %s/%s",
                    item_id, beatmap_id,
                    self.starting_item_id, starting_beatmap_id,
                )
                return
            # If transport dies after sending StopMatchCountdown, its outcome
            # is unknowable. Preserve STARTING and let normal reconciliation
            # establish server truth instead of pretending the stop succeeded.
            if not self._stop_match_countdown():
                return
            self.starting_map = None
            self.starting_item_id = None
            self._clear_start_recovery_tracking()
            self.set_state(IDLE)

            # The ordinary IDLE timer has already elapsed (that is why a
            # countdown existed). Make the new delay begin now rather than
            # charging auto_start_seconds a second time.
            try:
                auto_start = max(
                    0.0, float(self.cfg.get("auto_start_seconds", 45) or 0)
                )
            except (TypeError, ValueError):
                auto_start = 45.0
            self._idle_since = time.time() - auto_start

        self._countdown_wait_requesters.add(uid)
        delay = self._start_wait_seconds()
        self._countdown_wait_delay_seconds += delay
        total = int(round(self._countdown_wait_delay_seconds))
        action = "Countdown cancelled; start delayed" if cancelled_countdown else "Start delayed"
        self.say(
            f"{action} {int(round(delay))}s — {total}s total. "
            f"Type {self.prefix}start to begin immediately.",
            automatic=True,
        )
        log.info(
            "player %s requested countdown wait for item %s beatmap %s "
            "(+%.1fs, total %.1fs)",
            uid, item_id, beatmap_id, delay,
            self._countdown_wait_delay_seconds,
        )

    def _lock_countdown_wait_for_start(self) -> None:
        """Consume elapsed waits once a StartMatch attempt is actually sent."""
        item_id = self._sync_countdown_wait_item()
        if item_id is None:
            return
        self._countdown_wait_delay_seconds = 0.0

    def _suppress_countdown_wait(self, reason: str) -> bool:
        """Silently disable pending/future waits for the current exact map."""
        item_id = self._sync_countdown_wait_item()
        if item_id is None:
            return False
        beatmap_id = self._countdown_wait_beatmap_id
        changed = (
            self._countdown_wait_locked_item_id != item_id
            or self._countdown_wait_locked_beatmap_id != beatmap_id
            or self._countdown_wait_delay_seconds > 0.0
        )
        self._countdown_wait_locked_item_id = item_id
        self._countdown_wait_locked_beatmap_id = beatmap_id
        self._countdown_wait_delay_seconds = 0.0
        if changed:
            log.info(
                "suppressing .wait for item %s beatmap %s (%s)",
                item_id, beatmap_id, reason,
            )
        return changed

    @staticmethod
    def _chat_is_countdown_frustration(text: str) -> bool:
        normalized = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
        if not normalized:
            return True
        if normalized in {"start", "go", "begin", "ready", "r"}:
            return True
        return any(phrase in normalized for phrase in _COUNTDOWN_FRUSTRATION_PHRASES)

    def _active_chat_countdown_extension(self) -> float:
        """Return a quiet five-second extension for genuinely active chatter."""
        now = time.time()
        try:
            window = self._external_float(
                self.cfg.get("active_chat_countdown_window_seconds", 45.0),
                10.0, 300.0,
            )
            minimum_messages = self._external_int(
                self.cfg.get("active_chat_countdown_min_messages", 4), 2, 30
            )
            minimum_speakers = self._external_int(
                self.cfg.get("active_chat_countdown_min_speakers", 2), 1, 20
            )
            extension = self._external_float(
                self.cfg.get("active_chat_countdown_extension_seconds", 5.0),
                0.0, 30.0,
            )
        except (TypeError, ValueError):
            window, minimum_messages, minimum_speakers, extension = 45.0, 4, 2, 5.0

        recent = [
            entry for entry in getattr(self, "_recent_human_chat", ())
            if now - float(entry[0]) <= window
            and not self._chat_is_countdown_frustration(entry[2])
        ]
        speakers = {str(entry[1]).casefold() for entry in recent}
        if len(recent) < minimum_messages or len(speakers) < minimum_speakers:
            return 0.0
        log.info(
            "extending automatic countdown by %.1fs after %d recent messages "
            "from %d speakers; start-pressure/frustration excluded",
            extension, len(recent), len(speakers),
        )
        return extension

    def _send_mid_match_join_intro(self, user_id: int) -> bool:
        """Orient a player who arrives while a map is already playing."""
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return False
        if self.state != PLAYING or uid in self._mid_match_intro_sent:
            return False
        player = self.players.get(uid) or {}
        if not player or uid in self.non_gameplay_user_ids():
            return False
        username = str(player.get("name") or uid)
        remaining = self._match_remaining_seconds()
        if remaining:
            timing = self._format_match_remaining(remaining)
        else:
            timing = "a little time"
        self.say(
            self._language_personal(user_id, f"{username}, {timing} left—you're in next."
            + f" {self.prefix}time for remaining."
            + self._join_help_tip(uid), "join"),
            automatic=True,
        )
        self._mid_match_intro_sent.add(uid)
        log.info("sent mid-match introduction to player %s", uid)
        return True

    def _record_lobby_join(self, user_id: int, info: dict) -> None:
        now = time.time()
        info["lobby_joined_at"] = now
        info["lobby_time_checkpoint_at"] = now
        try:
            self.pp_stats.record_lobby_join(
                user_id=int(user_id),
                username=str(info.get("name") or user_id),
                joined_at=now,
            )
        except Exception:
            # Presence tracking is useful but must never interrupt room logic.
            log.exception("could not record lobby join for user %s", user_id)
        if self.room_id is not None:
            try:
                self.ratings.record_room_player_join(
                    room_id=int(self.room_id),
                    user_id=int(user_id),
                    username=str(info.get("name") or user_id),
                    joined_at=now,
                )
            except Exception:
                log.exception(
                    "could not record per-room join for user %s", user_id
                )

    def _flush_player_lobby_time(self, user_id: int, info: dict | None) -> None:
        if not info:
            return
        checkpoint = info.get("lobby_time_checkpoint_at")
        if checkpoint is None:
            return
        now = time.time()
        seconds = max(0.0, now - float(checkpoint))
        if seconds <= 0.0:
            return
        try:
            self.pp_stats.add_lobby_time(
                user_id=int(user_id),
                username=str(info.get("name") or user_id),
                seconds=seconds,
                left_at=now,
            )
        except Exception:
            log.exception("could not save lobby time for user %s", user_id)
        if self.room_id is not None:
            try:
                self.ratings.add_room_player_time(
                    room_id=int(self.room_id),
                    user_id=int(user_id),
                    username=str(info.get("name") or user_id),
                    seconds=seconds,
                    left_at=now,
                )
            except Exception:
                log.exception(
                    "could not save per-room lobby time for user %s", user_id
                )
        # Advance the checkpoint so a later clean-shutdown flush cannot
        # double-count a player whose leave/reset path already saved time.
        info["lobby_time_checkpoint_at"] = now

    def _flush_all_lobby_time(self) -> None:
        for uid, info in list(self.players.items()):
            self._flush_player_lobby_time(uid, info)

    def _ban_store(self):
        path=getattr(self,'cfg',{}).get('moderation_bans_file')
        if not path:
            return None
        store=getattr(self,'_project_ban_store',None)
        if store is None or str(store.path)!=str(path):
            store=self._project_ban_store=BanStore(path)
        store.refresh()
        return store

    def _moderation_protected(self, uid):
        return uid in self.non_gameplay_user_ids() or self._is_moderator(uid)

    def _is_project_banned(self, uid):
        store=self._ban_store()
        return bool(store and (not store.loaded or uid in store.bans)
                    and not self._moderation_protected(uid))

    def _kick_moderation_target(self, uid):
        if self._moderation_protected(uid) or not self.room_id:
            return False
        # The existing broker restricts KickPlayer to the worker's owned room.
        self._referee_operation_with_rejoin('KickPlayer',lambda:self.hub.kick(self.room_id,uid))
        log.warning('MODERATION kick requested user=%s room=%s',uid,self.room_id)
        return True

    def _enforce_project_bans(self):
        store=self._ban_store()
        if not store or not store.loaded or not self.room_id:
            return
        attempts=getattr(self,'_ban_kick_attempts',{})
        self._ban_kick_attempts=attempts
        for uid in list(self.players):
            if uid not in store.bans or self._moderation_protected(uid):
                continue
            proof=getattr(self,'_ban_presence_proofs',{}).get(uid,0)
            previous=attempts.get(uid)
            if previous and previous[0]==self.room_id:
                if time.monotonic()-previous[1]<30 or (previous[3] and proof<=previous[2]):
                    continue
            attempts[uid]=(self.room_id,time.monotonic(),proof,True)
            try:
                self._kick_moderation_target(uid)
            except HubTransportError:
                raise
            except Exception as error:
                attempts[uid]=(self.room_id,time.monotonic(),proof,False)
                log.warning('Ban removal not confirmed for user %s: %s',uid,error)

    def _moderation_command(self, cmd, actor, raw):
        store=self._ban_store()
        if not store:
            self.say('Moderation persistence is not configured; ask the owner.')
            return
        store.refresh(force=True)
        if not store.loaded or store.last_error:
            self.say('Ban storage is unavailable; no moderation changes made.')
            return
        if cmd=='bans':
            try:
                page=int(raw or '1')
                if page<1:raise ValueError()
            except ValueError:
                self.say(f'Usage: {self.prefix}bans [page]');return
            rows=sorted(store.bans.values(),key=lambda r:r['user_id'])
            if not rows:self.say('No project bans.');return
            total=(len(rows)+4)//5
            if page>total:self.say(f'Ban pages: 1–{total}.');return
            self.say(f'Project bans {page}/{total}: '+', '.join(f"{r.get('username',r['user_id'])} ({r['user_id']}; "+(f"{max(1, int((r['expires_at']-time.time()+59)//60))}m left" if r.get('expires_at') is not None else 'permanent')+")" for r in rows[(page-1)*5:page*5]))
            return
        target,separator,reason=raw.partition('|')
        target=target.strip().strip('"')
        reason=reason.strip()
        duration=None
        duration_label=None
        if cmd=='ban':
            parts=target.rsplit(None,1)
            if len(parts)==2 and re.fullmatch(r'[0-9]+[A-Za-z]+',parts[1]):
                from bot.moderation import parse_ban_duration
                try:duration=parse_ban_duration(parts[1])
                except ValueError as error:
                    self.say(str(error));return
                target=parts[0].strip().strip('"')
                duration_label=parts[1].lower()
        if duration is not None and self.cfg.get('_rolling_worker'):
            # Older workers regard every stored ban as permanent. Do not
            # create timed entries until every room understands expiry.
            try:
                if self.cfg.get('_host_root'):
                    from poolrotater_host.layout import load
                    from bot.moderation import read_rolling_status
                    from poolrotater_host.config import contained
                    root=Path(self.cfg['_host_root'])
                    _, host=load(root)
                    expected={r['alias'] for r in host['rooms']}
                    status=read_rolling_status(contained(root,'state/rolling/status.json'))
                    same_rooms=set(status['rooms'])==expected
                else:
                    status=json.loads((store.path.parent.parent/'rolling/status.json').read_text())
                    same_rooms=len(status['rooms'])==4
                versions=[tuple(map(int,r['version'].lstrip('v').split('.'))) for r in status['rooms'].values()]
                ready=(0<=time.time()-status['generated_at']<10 and same_rooms
                       and all(v>=(6,7,34) for v in versions))
            except (OSError,ValueError,KeyError,TypeError):
                ready=False
            if not ready:
                rooms='configured' if self.cfg.get('_host_root') else 'four'
                self.say(f'Temporary bans unlock once all {rooms} rooms finish updating; permanent bans still work.');return
        if not target or (separator and cmd!='ban') or len(reason)>200:
            self.say(f'Usage: {self.prefix}{cmd} <username|id>'+(' [30m|24h|7d] [| reason]' if cmd=='ban' else ''));return
        # Persisted ban names can be resolved after the user has left/renamed.
        saved=next((r for r in store.bans.values() if str(r.get('username','')).casefold()==target.casefold()),None)
        resolved=(saved['user_id'],saved.get('username',target)) if saved and cmd=='unban' else self._resolve_user_reference(target)
        if not resolved:
            self.say('User not found. Use their exact osu! username or numeric ID.');return
        uid,name=resolved
        if uid<=0 or (cmd!='unban' and self._moderation_protected(uid)):
            self.say('Cannot kick or ban the bot, referees, admins, or moderators.');return
        if cmd=='kick':
            if uid not in self.players:self.say(f'{name} is not in this room.');return
            try:
                self._kick_moderation_target(uid)
                log.warning('MODERATION manual kick actor=%s target=%s',actor,uid)
                self.say(f'Kick requested for {name}.')
            except HubTransportError:
                self.say('Kick outcome unconfirmed; check the roster before retrying.')
                raise
            except Exception as error:
                log.warning('Kick rejected for %s: %s',uid,error);self.say('Kick failed; no ban was added.')
            return
        try:
            changed=store.change(cmd,uid,name,actor,self._known_username(actor) or str(actor),
                                 self.cfg.get('_instance_name','room'),reason,duration=duration)
        except Exception as error:
            log.exception('Moderation persistence failed: %s',error)
            self.say('Could not save moderation change; no ban/unban applied.');return
        if cmd=='ban':
            detail=(' for '+duration_label if duration_label else ' permanently') if changed else ''
            self.say(f'Project ban '+('saved' if changed else 'already active')+f' for {name}'+detail+'; removal requested.')
            self._enforce_project_bans()
        else:
            self.say(f'{name} '+('unbanned' if changed else 'is not banned')+'.')

    def _queue_stats_reply(self, user_id, parts, target_name, *, announce):
        """Remember a bounded request; the room thread posts its answer once ready."""
        requests = getattr(self, '_pending_stats_replies', None)
        if requests is None:
            requests = self._pending_stats_replies = {}
        key = (int(user_id), tuple(parts[1:]))
        if key not in requests:
            if len(requests) >= 32:
                if announce:
                    self.say('Statistics are busy right now; please try again shortly.')
                return
            now = time.monotonic()
            requests[key] = {'parts': list(parts), 'player': self.players.get(int(user_id)),
                             'room_id': getattr(self, 'room_id', None),
                             'channel_id': getattr(self, 'channel_id', None),
                             'expires_at': now + 90, 'retry_at': now + 1,
                             'target_name': str(target_name)}
            if announce:
                requester = str((self.players.get(int(user_id)) or {}).get('name') or user_id)
                self.say(f"{requester} — fetching stats for {target_name}; I'll post them here when ready.")

    def _flush_stats_replies(self):
        requests = getattr(self, '_pending_stats_replies', None)
        if not requests:
            return
        now = time.monotonic()
        for key, work in list(requests.items()):
            uid = key[0]
            if (uid not in self.players or self.players[uid] is not work['player']
                    or getattr(self, 'room_id', None) != work['room_id']
                    or getattr(self, 'channel_id', None) != work['channel_id']
                    or self._is_project_banned(uid)):
                requests.pop(key, None)
                continue
            if now >= work['expires_at']:
                requests.pop(key, None)
                self.say(f"Stats for {work['target_name']} are unavailable right now; please try later.")
                continue
            if now < work['retry_at']:
                continue
            work['retry_at'] = now + 1
            if self._show_player_stats(uid, work['parts'], pending=True) is not False:
                requests.pop(key, None)
            return  # one bounded, local-cache lookup per room tick

    def _show_player_stats(self, user_id, parts, *, pending=False):
        mania = self.cfg.get('ruleset_id', 0) == 3
        requester_name = str(
            (self.players.get(user_id) or {}).get("name")
            or self.pp_stats.username_for_user_id(user_id)
            or user_id
        )

        target_user_id = int(user_id)
        target_name = requester_name

        # Anyone may look up another player's AutoHost stats by username.
        # Players are public by default, but a target can opt out with
        # `.hidden`; a player can always see their own stats. Everything
        # after `.stats` is treated as the username so names with spaces
        # remain supported.
        if len(parts) > 1:
            requested_name = " ".join(parts[1:]).strip()
            resolved = None
            wanted = requested_name.casefold()

            # Prefer the live lobby so a newly joined player is queryable
            # before their first completed DB write.
            for uid, info in self.players.items():
                live_name = str(info.get("name") or uid)
                if live_name.casefold() == wanted:
                    resolved = (int(uid), live_name)
                    break

            if resolved is None:
                try:
                    resolved = self.pp_stats.find_user_by_username(
                        requested_name
                    )
                except Exception:
                    log.exception(
                        "could not resolve AutoHost stats username %r",
                        requested_name,
                    )

            if resolved is None and getattr(self.pp_stats, "reader", None) is not None:
                try:
                    resolved = self.pp_stats.shared_username(requested_name)
                except Exception:
                    log.warning("shared statistics username cache unavailable")
                if resolved is None:
                    try:
                        lookup_status = self.pp_stats.reader.lookup_status(
                            requested_name, ruleset_id=self.pp_stats.ruleset_id)
                    except Exception:
                        lookup_status = None
                    if lookup_status == 'not_found':
                        self.say(f"{requester_name} — no shared AutoHost stats are available for {requested_name} yet.")
                    else:
                        self._queue_stats_reply(user_id, parts, requested_name, announce=not pending)
                        return False
                    return True

            if resolved is None:
                self.say(
                    f"{requester_name} — no AutoHost stats found for "
                    f"{requested_name}."
                )
                return True

            target_user_id, target_name = resolved

        if target_user_id != int(user_id):
            try:
                target_hidden = self.pp_stats.stats_hidden(target_user_id)
            except Exception:
                log.exception(
                    "could not read AutoHost stats visibility for user %s",
                    target_user_id,
                )
                self.say(
                    f"{requester_name} — I couldn't check {target_name}'s "
                    "stats visibility right now."
                )
                return True

            if not target_hidden and getattr(self.pp_stats, "reader", None) is not None:
                try:
                    network_hidden = self.pp_stats.shared_stats_hidden(target_user_id)
                except Exception:
                    network_hidden = None
                if network_hidden is None:
                    self._queue_stats_reply(user_id, parts, target_name, announce=not pending)
                    return False
                target_hidden = network_hidden

            if target_hidden:
                self.say(
                    f"{requester_name} — {target_name} has hidden their "
                    "AutoHost stats."
                )
                return True

        # Personal improvement is persistent across room/process restarts.
        # PP totals are based on positive profile gains detected from top-100
        # scores made in this AutoHost. Lobby time is also persistent; if
        # the target is currently present, include the live unflushed slice.
        try:
            personal = self.pp_stats.user_stats(target_user_id)
        except Exception:
            log.exception(
                "could not read AutoHost PP stats for user %s",
                target_user_id,
            )
            personal = {
                "total_gain": 0.0,
                "achievement_count": 0,
                "best_gain": 0.0,
                "total_lobby_seconds": 0.0,
            }

        total_gain = float(personal.get("total_gain") or 0.0)
        achievement_count = int(personal.get("achievement_count") or 0)
        best_gain = float(personal.get("best_gain") or 0.0)
        total_lobby_seconds = float(
            personal.get("total_lobby_seconds") or 0.0
        )

        live_info = self.players.get(target_user_id)
        if live_info is not None:
            checkpoint = live_info.get("lobby_time_checkpoint_at")
            if checkpoint is not None:
                live_slice = max(0.0, time.time() - float(checkpoint))
                if personal.get("network_statistics"):
                    personal["local_tracked_lobby_seconds"] = personal.get("local_tracked_lobby_seconds", 0) + live_slice
                    personal["local_unreconciled_lobby_seconds"] = personal.get("local_unreconciled_lobby_seconds", 0) + live_slice
                else:
                    total_lobby_seconds += live_slice

        playtime = self._format_lobby_time(total_lobby_seconds)
        coverage_note = ""
        if achievement_count:
            self.say(
                f"{target_name} — {'Mania' if mania else 'AutoHost'} gains: +{total_gain:.1f}pp "
                f"across {achievement_count} gain"
                f"{'s' if achievement_count != 1 else ''} "
                f"(best +{best_gain:.1f}pp) | Playtime: {playtime}.{coverage_note}"
            )
        else:
            self.say(
                f"{target_name} — {'Mania' if mania else 'AutoHost'} gains: +0.0pp tracked so far "
                f"| Playtime: {playtime}.{coverage_note}"
            )

        return True

    def on_user_joined(self, user_id: int) -> None:
        user_id = int(user_id)
        if self.cfg.get('moderation_bans_file'):
            proofs=getattr(self,'_ban_presence_proofs',{})
            self._ban_presence_proofs=proofs
            proofs[user_id]=time.monotonic()
        if user_id in self.non_gameplay_user_ids():
            log.info(
                "non-gameplay operator/referee joined room; not counting as "
                "player: %s",
                user_id,
            )
            return
        if self._is_project_banned(user_id):
            store=self._ban_store()
            # Invalid initial storage cannot authorize kicking anyone.
            if store and user_id in store.bans:
                self.players.setdefault(user_id,{'name':store.bans[user_id].get('username',str(user_id)),
                                                 'status':'idle'})
                self._enforce_project_bans()
                return
        if user_id in self.players:
            return
        # The referee has already proved occupancy; profile data must never
        # determine whether this real player exists in the roster.
        self.players[user_id] = {"pp": None, "name": str(user_id), "status": "idle"}
        if self.cfg.get('background_player_profiles', False):
            # Presence is already authoritative. Optional profile/top-score
            # network I/O must not hold commands or READY processing behind it.
            self._defer_player_profile(user_id)
            return
        mode_kwargs = {"mode": "mania"} if self.cfg.get("ruleset_id", 0) == 3 else {}
        try:
            u = self.api.user(user_id, **mode_kwargs)
        except (ApiAdmissionDeferred, ApiProfileReadUnavailable):
            self._defer_player_profile(user_id)
            return
        if not u:
            self._record_lobby_join(user_id, self.players[user_id])
            return
        stats = u.get("statistics") or {}
        pp = stats.get("pp") or 0.0
        name = u.get("username", str(user_id))

        # Cache the WHOLE top 100, not just the cut-off. We need the full
        # list to work out which slot a new score takes and how much profile
        # pp it actually adds. Fetched once on join -- re-polling after every
        # map would blow the request budget instantly.
        try:
            raw = self.api.user_top_scores(user_id, limit=100, **mode_kwargs)
        except (ApiAdmissionDeferred, ApiProfileReadUnavailable):
            self._defer_player_profile(user_id, profile=u)
            return
        top = sorted(
            ((s.get("pp") or 0.0, (s.get("beatmap") or {}).get("id") or 0)
             for s in raw if s.get("pp")),
            reverse=True)
        threshold = top[-1][0] if len(top) >= 100 else 0.0

        self.players[user_id].update({"pp": pp, "name": name,
                                      "country_code": u.get("country_code"),
                                      "threshold": threshold, "top": top})
        self._record_lobby_join(user_id, self.players[user_id])
        log.info("joined: %s (%.0fpp, #100 = %.0f)", name, pp, threshold)
        self.mark_auto_retune_pending()

    def _profile_fields(self, user_id, profile):
        """Unknown statistics stay unknown; only a qualified reply enriches PP."""
        if type(profile) is not dict or not profile:
            return None
        if profile.get("id") is not None and profile.get("id") != user_id:
            return None
        name = profile.get("username")
        if type(name) is not str or not name:
            return None
        stats = profile.get("statistics")
        pp = stats.get("pp") if type(stats) is dict else None
        if pp is not None and (type(pp) not in (int, float) or not math.isfinite(pp) or pp < 0):
            return None
        return {"name": name, "country_code": profile.get("country_code"), "pp": pp}

    def _defer_player_profile(self, user_id, *, profile=None):
        """Record the join once, then queue enrichment independently of READY."""
        fields = self._profile_fields(user_id, profile)
        self.players.setdefault(user_id, {"pp": None, "name": str(user_id), "status": "idle"})
        if fields is not None:
            self.players[user_id].update(fields)
        self._record_lobby_join(user_id, self.players[user_id])
        if fields is not None and fields["pp"] is not None:
            self.mark_auto_retune_pending()
        pending = getattr(self, "_pending_player_profiles", None)
        if pending is None:
            pending = self._pending_player_profiles = {}
        if len(pending) >= 32:
            log.warning("profile enrichment queue full; keeping unknown roster statistics")
            return
        now = time.monotonic()
        pending[user_id] = {"room_id": self.room_id, "channel_id": self.channel_id,
                            "expires_at": now + 300, "retry_at": now + 1,
                            "attempts": 1, "profile": profile if fields is not None else None,
                            "player": self.players[user_id]}

    def _flush_player_profiles(self):
        """At most one pending roster enrichment per tick; never sleep or rejoin."""
        if self.cfg.get('background_player_profiles', False):
            return self._flush_background_player_profiles()
        pending = getattr(self, "_pending_player_profiles", None)
        if not pending:
            return
        now = time.monotonic()
        for uid, work in list(pending.items()):
            if (now >= work["expires_at"] or uid not in self.players
                    or self.players[uid] is not work["player"]
                    or (self.room_id, self.channel_id) != (work["room_id"], work["channel_id"])
                    or uid in self.non_gameplay_user_ids() or self._is_project_banned(uid)):
                pending.pop(uid, None)
        for uid, work in list(pending.items()):
            if now < work["retry_at"]:
                continue
            mode_kwargs = {"mode": "mania"} if self.cfg.get("ruleset_id", 0) == 3 else {}
            fields = None
            try:
                profile = work["profile"]
                if profile is None:
                    profile = self.api.user(uid, **mode_kwargs)
                fields = self._profile_fields(uid, profile)
                if fields is not None:
                    work["profile"] = profile
                    raw = self.api.user_top_scores(uid, limit=100, **mode_kwargs)
                    if type(raw) is list:
                        top = []
                        for score in raw:
                            if type(score) is not dict:
                                continue
                            pp = score.get("pp")
                            beatmap = score.get("beatmap")
                            if type(pp) not in (int, float) or not math.isfinite(pp) or pp <= 0:
                                continue
                            bid = beatmap.get("id", 0) if type(beatmap) is dict else 0
                            if type(bid) is not int or bid < 0:
                                continue
                            top.append((pp, bid))
                        top.sort(reverse=True)
                        fields.update(top=top, threshold=top[-1][0] if len(top) >= 100 else 0.0)
                        self.players[uid].update(fields)  # retain current READY/playing/lobby metadata
                        if fields["pp"] is not None:
                            self.mark_auto_retune_pending()
                        pending.pop(uid, None)
                        # A deferred username must still receive its configured
                        # greeting once enrichment succeeds in this same room.
                        self._send_custom_join_greeting(uid)
                        return
            except (ApiAdmissionDeferred, ApiProfileReadUnavailable):
                pass  # Optional GET only; authority/child-cleanup/RPC failures still propagate.
            if fields is not None:
                self.players[uid].update(fields)
                if fields["pp"] is not None:
                    self.mark_auto_retune_pending()
            pending.pop(uid, None)
            work["attempts"] = min(work["attempts"] + 1, 5)
            work["retry_at"] = now + min(15, 2 ** (work["attempts"] - 1))
            pending[uid] = work  # rotate behind other due profiles, avoiding starvation
            return

    def _flush_background_player_profiles(self):
        """One bounded read job; apply its result only on this room thread.

        Background work never changes players, READY state, room mutations,
        ratings or greetings. Parent RPC still checks the exact worker authority.
        Late results are discarded across departures/rejoins/room replacements.
        """
        pending = getattr(self, '_pending_player_profiles', {})
        now = time.monotonic()
        for uid, work in list(pending.items()):
            if (now >= work['expires_at'] or uid not in self.players
                    or self.players[uid] is not work['player']
                    or (self.room_id, self.channel_id) != (work['room_id'], work['channel_id'])
                    or uid in self.non_gameplay_user_ids() or self._is_project_banned(uid)):
                pending.pop(uid, None)
        active = getattr(self, '_background_profile_job', None)
        if active is not None:
            try:
                profile, raw, error = active['result'].get_nowait()
            except queue.Empty:
                return
            self._background_profile_job = None
            uid, work = active['uid'], active['work']
            if pending.get(uid) is not work:
                return
            if error is not None and not isinstance(error, (ApiAdmissionDeferred, ApiProfileReadUnavailable)):
                raise error
            fields = self._profile_fields(uid, profile)
            if fields is not None:
                work['profile'] = profile
                if error is None and type(raw) is list:
                    top = []
                    for score in raw:
                        if type(score) is not dict:
                            continue
                        pp, beatmap = score.get('pp'), score.get('beatmap')
                        bid = beatmap.get('id', 0) if type(beatmap) is dict else 0
                        if (type(pp) not in (int, float) or not math.isfinite(pp) or pp <= 0
                                or type(bid) is not int or bid < 0):
                            continue
                        top.append((pp, bid))
                    top.sort(reverse=True)
                    fields.update(top=top, threshold=top[-1][0] if len(top) >= 100 else 0.0)
                self.players[uid].update(fields)
                if fields['pp'] is not None:
                    self.mark_auto_retune_pending()
                if error is None and type(raw) is list:
                    pending.pop(uid, None)
                    self._send_custom_join_greeting(uid)
                    return
            pending.pop(uid, None)
            work['attempts'] = min(work['attempts'] + 1, 5)
            work['retry_at'] = now + min(15, 2 ** (work['attempts'] - 1))
            pending[uid] = work
            return
        for uid, work in pending.items():
            if now < work['retry_at']:
                continue
            result = queue.Queue(maxsize=1)
            api, profile = self.api, work['profile']
            mode = {'mode': 'mania'} if self.cfg.get('ruleset_id', 0) == 3 else {}
            def fetch(api=api, uid=uid, profile=profile, mode=mode, result=result):
                raw = None
                try:
                    if profile is None:
                        profile = api.user(uid, **mode)
                    # Validate without reading/mutating any instance state.
                    if (type(profile) is dict and profile.get('username')
                            and profile.get('id', uid) == uid):
                        raw = api.user_top_scores(uid, limit=100, **mode)
                    result.put_nowait((profile, raw, None))
                except Exception as error:
                    result.put_nowait((profile, raw, error))
            thread = threading.Thread(target=fetch, name='optional-player-profile', daemon=True)
            self._background_profile_job = {'uid': uid, 'work': work, 'result': result, 'thread': thread}
            try:
                thread.start()
            except Exception:
                self._background_profile_job = None
                raise
            return

    def mark_auto_retune_pending(self) -> None:
        """Remember that roster changes should affect the NEXT queue refill."""
        if self.cfg.get("auto_difficulty", True) and not self.manual_range:
            self._auto_retune_pending = True

    def _send_custom_join_greeting(self, user_id: int) -> bool:
        """Send a configured per-player greeting at most once per process-wide cooldown."""
        player = self.players.get(int(user_id)) or {}
        username = str(player.get("name") or user_id)
        template = configured_greeting(self.cfg, int(user_id), username)
        if not template:
            return False
        try:
            cooldown = self._external_float(
                self.cfg.get("custom_join_greeting_cooldown_seconds", 300.0),
                0.0,
                86400.0,
            )
        except (TypeError, ValueError):
            log.warning("invalid custom_join_greeting_cooldown_seconds; using 300 seconds")
            cooldown = 300.0
        try:
            message = template.format(
                name=username,
                room=str(self.cfg.get("_instance_name") or "lobby"),
            )
        except (KeyError, ValueError) as exc:
            log.warning("custom greeting for %s ignored: %s", username, exc)
            return False
        message = message.strip()[:450]
        if not message:
            return False
        if not JOIN_GREETING_COOLDOWN.claim(int(user_id), cooldown):
            return False
        self.say(message, automatic=True)
        log.info("sent custom join greeting for %s in %s", username, self.cfg.get("_instance_name", "lobby"))
        return True

    def _track_empty_room(self) -> bool:
        """Track actual occupancy, including spectators, excluding referees."""
        excluded = self.non_gameplay_user_ids()
        if any(int(uid) not in excluded for uid in self.players):
            self._empty_since = None
            self._empty_reset_done = False
            self._empty_reset_retry_at = 0.0
            return False
        if getattr(self, "_empty_since", None) is None:
            self._empty_since = time.monotonic()
        return True

    def _maybe_reset_empty_autodiff(self) -> bool:
        """Reset difficulty, source and queue once per empty period."""
        if not self._track_empty_room():
            return False
        auto_mode = bool(
            self.cfg.get("auto_difficulty", True) and not self.manual_range
        )
        if (not self.room_id or not self.running
                or self.state != IDLE or self._empty_reset_done):
            return False
        try:
            delay = self._external_float(
                self.cfg.get("auto_empty_reset_seconds", 300), 0, 86400)
        except (ValueError, TypeError):
            delay = 300.0
        now = time.monotonic()
        if (delay <= 0 or now - self._empty_since < delay
                or now < self._empty_reset_retry_at):
            return False
        # Back off after an empty-pool/transport failure instead of rebuilding
        # or polling repeatedly every 0.4-second loop tick.
        self._empty_reset_retry_at = now + 60
        lo = float(self.cfg.get("sr_min", self.pool.sr_min))
        hi = float(self.cfg.get("sr_max", self.pool.sr_max))
        length = int(self.cfg.get("max_length", self.pool.max_length))
        if self.pool.reset_session_pool(lo, hi, length) <= 0:
            log.warning("empty-room baseline has no eligible maps; keeping current pool")
            return False
        self._clear_community_difficulty("empty room difficulty reset")
        self._fixed_range_rebuild_pending = False
        self._auto_retune_pending = False
        self._pending_room_name_range = (lo, hi)
        if not self.perform_soft_restart(announce=False):
            return False
        # A failed title change is retried with the same bounded backoff.
        if self._pending_room_name_range is not None:
            return False
        self._empty_reset_done = True
        self._fixed_difficulty_dirty = False
        suffix = (
            " AutoDiff resumes when players return."
            if auto_mode else " Fixed difficulty restored."
        )
        self.say(
            f"Empty-room reset: {self.pool.source_mode_label()}, "
            f"back to {lo:.2f}-{hi:.2f}* with a fresh queue."
            f"{suffix}", automatic=True,
        )
        log.info("empty room %s reset to JSON range %.2f-%.2f*",
                 self.room_id, lo, hi)
        return True

    def _set_auto_range_with_density_guard(
            self, desired_lo: float, desired_hi: float
    ) -> tuple[int, bool, bool]:
        """Apply AutoDiff with a length-first sparse-pool fallback.

        Order of operations:
          1. exact pp-derived SR band at the admin-set max length;
          2. same exact SR band with progressively longer maps;
          3. only then widen the lower SR edge if the pool is still too thin.

        Temporary length expansion never rewrites ``cfg['max_length']``. A later
        AutoDiff decision retries the configured baseline, so normal pools return
        to the admin-set limit automatically. Manual ``.range`` is unaffected.

        Returns ``(map_count, length_expanded, sr_expanded)``.
        """
        desired_lo = float(desired_lo)
        desired_hi = float(desired_hi)

        if getattr(self.pool, "loved_mode", False):
            # Loved rooms are intentionally governed by AutoDiff SR alone.
            # They never length-expand or widen outside the computed band.
            try:
                base_length = int(self.cfg.get("max_length", 0))
            except (TypeError, ValueError):
                base_length = 0
            n = self.pool.set_window(desired_lo, desired_hi, max(0, base_length))
            if n <= 0:
                log.warning(
                    "Loved AutoDiff exact band %.2f-%.2f* has no eligible maps; "
                    "keeping the previous known-good pool",
                    desired_lo, desired_hi,
                )
            return n, False, False

        try:
            configured_min = int(self.cfg.get("auto_pool_min_maps", 30))
        except (TypeError, ValueError):
            configured_min = 30
        queue_floor = max(1, int(self.cfg.get("queue_ahead", 1)) + 4)
        pool_limit = max(
            1, int(getattr(self.pool, "limit", configured_min) or configured_min)
        )
        min_maps = max(queue_floor, min(configured_min, pool_limit))

        try:
            base_length = int(self.cfg.get("max_length", self.pool.max_length))
        except (TypeError, ValueError):
            base_length = int(self.pool.max_length)
        base_length = max(1, base_length)

        try:
            length_step = int(self.cfg.get("auto_pool_length_step", 30))
        except (TypeError, ValueError):
            length_step = 30
        length_step = max(5, length_step)

        try:
            configured_max_length = int(
                self.cfg.get("auto_pool_max_length", 300)
            )
        except (TypeError, ValueError):
            configured_max_length = 300
        max_auto_length = max(base_length, configured_max_length)
        if getattr(self.pool, 'real_max_length', None) is not None:
            max_auto_length = min(max_auto_length, self.pool.real_max_length)

        try:
            widen_step = float(self.cfg.get("auto_pool_widen_step", 0.25))
        except (TypeError, ValueError):
            widen_step = 0.25
        widen_step = max(0.05, widen_step)

        try:
            max_widen = float(self.cfg.get("auto_pool_max_widen_down", 1.50))
        except (TypeError, ValueError):
            max_widen = 1.50
        max_widen = max(0.0, max_widen)

        # 1) Exact SR + configured max length. This also restores a previously
        # expanded ceiling as soon as the pool is healthy at baseline again.
        n = self.pool.set_window(desired_lo, desired_hi, base_length)
        if n >= min_maps:
            return n, False, False

        # 2) Preserve difficulty and increase length in small steps.
        best_n = n
        active_length = base_length
        while best_n < min_maps and active_length < max_auto_length:
            trial_length = min(max_auto_length, active_length + length_step)
            trial_n = self.pool.set_window(
                desired_lo, desired_hi, trial_length
            )
            active_length = trial_length
            if trial_n > 0:
                best_n = trial_n
            if active_length >= max_auto_length:
                break

        length_expanded = int(self.pool.max_length) > base_length
        if best_n >= min_maps:
            if length_expanded:
                log.warning(
                    "AutoDiff density guard kept %.2f-%.2f* but extended max "
                    "length %ds -> %ds for %d eligible maps (target %d)",
                    desired_lo, desired_hi, base_length, self.pool.max_length,
                    best_n, min_maps,
                )
            return best_n, length_expanded, False

        # 3) Still sparse after length expansion: only now lower the SR floor.
        if max_widen <= 0.0:
            return best_n, length_expanded, False

        # Once AutoDiff itself reaches the 7.50* lower-edge cap, sparse-pool
        # fallback may lengthen maps but must not undo that cap by widening down.
        # Below the cap, the historical downward density widening remains intact.
        if desired_lo >= AUTO_SR_AUTO_MIN_CAP - 1e-9:
            lower_limit = desired_lo
        else:
            lower_limit = max(AUTO_SR_FLOOR, desired_lo - max_widen)
        trial_lo = desired_lo
        # We already exhausted the length-first phase, so any SR fallback keeps
        # the maximum temporary length allowance. This also handles an exact SR
        # band that had zero maps at every shorter length.
        fallback_length = max_auto_length

        while best_n < min_maps and trial_lo > lower_limit + 1e-9:
            trial_lo = max(lower_limit, round(trial_lo - widen_step, 2))
            trial_n = self.pool.set_window(
                trial_lo, desired_hi, fallback_length
            )
            if trial_n > 0:
                best_n = trial_n
            if trial_lo <= lower_limit + 1e-9:
                break

        if best_n <= 0:
            return 0, False, False

        # Phase 3 may be the first successful candidate when the exact SR band
        # had zero maps, so recompute this from the actual accepted window.
        length_expanded = int(self.pool.max_length) > base_length
        sr_expanded = self.pool.sr_min < desired_lo - 0.001
        if sr_expanded:
            log.warning(
                "AutoDiff density guard extended max length %ds -> %ds first, "
                "then widened %.2f-%.2f* -> %.2f-%.2f* for %d eligible maps "
                "(target %d)",
                base_length, self.pool.max_length, desired_lo, desired_hi,
                self.pool.sr_min, self.pool.sr_max, best_n, min_maps,
            )
        elif best_n < min_maps:
            log.warning(
                "AutoDiff %.2f-%.2f* still has only %d/%d desired maps after "
                "length expansion to %ds and SR fallback",
                desired_lo, desired_hi, best_n, min_maps, self.pool.max_length,
            )

        return best_n, length_expanded, sr_expanded

    def apply_pending_auto_retune(self, force: bool = False) -> None:
        """Apply a deferred auto range at a safe between-map boundary."""
        if getattr(self, "_recalled_window", None) is not None:
            return
        if not force and not self._auto_retune_pending:
            return

        # Consume the pending flag now. A later roster event will set it again.
        self._auto_retune_pending = False

        if not self.cfg.get("auto_difficulty", True):
            return
        if self.manual_range and not force:
            return

        active = self.gameplay_player_ids()
        pps, dropped_auto_player, auto_pp_count = (
            self.auto_difficulty_pp_values(active)
        )
        if not pps:
            return

        med = float(median(pps))
        half = float(self.cfg.get("auto_sr_half_width", AUTO_SR_HALF_WIDTH))
        bias = self._effective_auto_bias()
        target = target_sr_for_pp(med, half_width=half, bias=bias)
        lo, hi = auto_range_for_pp(med, half_width=half, bias=bias)

        same_range = (
            abs(lo - self.pool.sr_min) < 0.001
            and abs(hi - self.pool.sr_max) < 0.001
        )
        # A previous exact range can still be unhealthy. If AutoDiff temporarily
        # expanded max length, do not fast-path forever: retry the admin baseline
        # so the relaxation disappears once it is no longer needed.
        try:
            density_min = int(self.cfg.get("auto_pool_min_maps", 30))
        except (TypeError, ValueError):
            density_min = 30
        density_min = max(
            1, min(density_min, int(getattr(self.pool, "limit", density_min)))
        )
        try:
            base_length = int(self.cfg.get("max_length", self.pool.max_length))
        except (TypeError, ValueError):
            base_length = int(self.pool.max_length)
        using_base_length = int(self.pool.max_length) <= base_length
        if same_range and using_base_length and len(self.pool.maps) >= density_min:
            log.debug(
                "deferred auto range unchanged at %.2f-%.2f* "
                "(median %.0fpp, bias %+.2f*)", lo, hi, med, bias)
            return

        n, length_expanded, sr_expanded = (
            self._set_auto_range_with_density_guard(lo, hi)
        )
        if n <= 0:
            # The requested range may simply have no farm_report maps. The
            # pool object has already restored its previous known-good state.
            # Keep this silent in lobby chat; a range shortage is an internal
            # data condition, not something players need spammed with.
            log.warning(
                "auto retune %.2f-%.2f* (target %.2f*, median %.0fpp, "
                "bias %+.2f*) had no eligible maps; keeping %.2f-%.2f*",
                lo, hi, target, med, bias,
                self.pool.sr_min, self.pool.sr_max,
            )
            return

        self.update_room_name_for_range(
            self.pool.sr_min, self.pool.sr_max)
        dropped_note = ""
        if dropped_auto_player is not None:
            dropped_uid, dropped_pp = dropped_auto_player
            dropped_name = self.players.get(dropped_uid, {}).get(
                "name", str(dropped_uid)
            )
            dropped_note = (
                f"; excluded lowest {dropped_name} ({dropped_pp:.0f}pp)"
            )

        density_notes = []
        if length_expanded:
            density_notes.append(
                f"length auto-expanded to {int(self.pool.max_length)}s"
            )
        if sr_expanded:
            density_notes.append("SR floor density-expanded downward")
        density_note = (
            "; " + ", ".join(density_notes) if density_notes else ""
        )
        log.info(
            "auto retuned between maps to %.2f-%.2f* (target %.2f*, "
            "bias %+.2f*) for median %.0fpp from %d/%d PP-known player(s) "
            "(%d maps)%s%s",
            self.pool.sr_min, self.pool.sr_max,
            target, bias, med, len(pps), auto_pp_count, n, dropped_note,
            density_note,
        )
        # Auto retunes stay visible in the room title and session log, but are
        # deliberately silent in chat.

    def _format_pp_achievement_message(self, achievements: list[dict]) -> str:
        """Return one <=400-char multiline PP-achievement announcement.

        Keep the familiar detailed format when it fits.  In unusually large
        lobbies, compact the lines (and, only if necessary, displayed names)
        so every achiever is still represented in the single chat message
        instead of silently truncating later players in say().
        """
        full_lines = [
            f"{a['name']} — {a['pp']:.0f}pp ({a['accuracy']:.2f}%) — "
            f"new #{a['rank']} top play, +{a['gain']:.1f}pp profile!"
            for a in achievements
        ]
        full = "PP achievements:\n" + "\n".join(full_lines)
        if len(full) <= 400:
            return full

        compact_lines = [
            f"{a['name']} {a['pp']:.0f}pp #{a['rank']} +{a['gain']:.1f}"
            for a in achievements
        ]
        compact = "PP achievements:\n" + "\n".join(compact_lines)
        if len(compact) <= 400:
            return compact

        # Worst-case fallback for a full lobby with long usernames.  Work out
        # a shared display-name budget so all achievements remain visible.
        static = [
            f" {a['pp']:.0f}p #{a['rank']} +{a['gain']:.1f}"
            for a in achievements
        ]
        header = "PP achievements:\n"
        separators = max(0, len(achievements) - 1)
        available_for_names = 400 - len(header) - separators - sum(
            len(x) for x in static
        )
        per_name = max(3, available_for_names // max(1, len(achievements)))

        lines = []
        for a, suffix in zip(achievements, static):
            name = str(a["name"])
            if len(name) > per_name:
                if per_name >= 2:
                    name = name[:per_name - 1] + "…"
                else:
                    name = name[:per_name]
            lines.append(name + suffix)

        msg = header + "\n".join(lines)
        # This should only be reachable with an implausibly large room.  Keep
        # say()'s hard safety limit intact rather than emitting multiple chat
        # messages, which would defeat the purpose of this change.
        return msg[:400]

    # ---------------------------------------------------------- scoring

    def handle_match_completed(self, payload: dict) -> None:
        self._complete_preference_match()
        # The map that actually entered gameplay was frozen at MatchStarted.
        # Treat that as authoritative for both score lookup and map feedback.
        #
        # Do NOT prefer MatchCompleted's playlist_item_id here. In live lazer
        # rooms that event can arrive after playlist advancement, and the id in
        # the payload has proven unreliable enough to make the rating prompt
        # lag by a map.
        self.scoring_item_id = (self.playing_item_id
                                or payload.get("playlist_item_id")
                                or self.current_item_id)

        completed_map = self.playing_map

        try:
            self.ratings.record_match_end(
                match_key=self._activity_match_key,
                room_id=self.room_id,
                playlist_item_id=self.scoring_item_id,
                result="completed",
            )
        except Exception:
            log.exception("could not mark match completed in activity history")

        # Fallback only if we somehow missed MatchStarted / playing_map.
        if completed_map is None and self.scoring_item_id is not None:
            completed_map = self._map_for_item_id(int(self.scoring_item_id))
        if completed_map is None:
            log.error(
                "completed match item %s has no resolvable map; feedback will "
                "stay disabled rather than target the current/next map",
                self.scoring_item_id,
            )

        self.last_played_map = completed_map
        # Freeze match provenance with the feedback target, before promotion or
        # later preference changes can describe a different song/match.
        required = set(self.cfg.get('required_mods', []))
        if completed_map is not None and getattr(completed_map, 'is_dt', False):
            required.add('DT')
        self._rating_context = {'ruleset_id': self.cfg.get('ruleset_id'),
                                'room_id': self.room_id,
                                'playlist_item_id': self.scoring_item_id,
                                'required_mods': sorted(required)}
        self._record_mania_round(completed_map)
        if completed_map is not None and callable(getattr(self.pool, 'record_song_play', None)):
            self.pool.record_song_play(completed_map)
        if self.last_played_map:
            self._recent_rating_map_ids.append(
                int(self.last_played_map.beatmap_id)
            )
            log.info("rating target set to map that entered gameplay: %s "
                     "(playing_item=%s completed_payload_item=%s)",
                     self.last_played_map.label,
                     self.playing_item_id,
                     payload.get("playlist_item_id"))

        self._rating_window_until = time.time() + self.cfg.get(
            "rating_window_seconds", 90)
        self._arm_rating_prompt_after_map()

        # Playlist promotion can race MatchCompleted in either direction. Arm
        # one next-map PP announcement here, then let the already-promoted map
        # or the next playlist event satisfy it exactly once.
        self._post_match_pp_announce_pending = True
        self._post_match_pp_completed_beatmap_id = (
            int(self.last_played_map.beatmap_id)
            if self.last_played_map is not None else None
        )
        self._maybe_announce_post_match_pp()

        # Community difficulty votes use completed maps as their clock. This
        # also performs one decay step for temporary preference layers whose
        # original voters have all left the lobby.
        self._advance_community_difficulty_after_map()

        self.set_state(SCORING)
        self._notify_discord_lobby_event(
            "MATCH END",
            (
                f"Finished: {completed_map.label}"
                if completed_map is not None else "Match finished (map unresolved)"
            ),
            map_key=self._discord_map_key(completed_map),
        )
        self._maybe_announce_website()
        # Scores are not queryable the instant gameplay ends; the server still
        # has to ingest submissions. Delay then retry rather than firing
        # immediately and getting an empty list.
        self._match_end_at = time.time()
        self._score_api_retry_at = 0.0
        self._score_api_failures = 0

    def try_score_report(self) -> bool:
        elapsed = time.time() - self._match_end_at
        if elapsed < self.cfg.get("score_delay_seconds", 4):
            return False
        if time.monotonic() < getattr(self, '_score_api_retry_at', 0.0):
            return False

        try:
            data = self.api.room_item_scores(self.room_id, self.scoring_item_id)
        except (ApiAdmissionDeferred, ApiScoreReadUnavailable):
            failures = min(getattr(self, '_score_api_failures', 0) + 1, 4)
            self._score_api_failures = failures
            self._score_api_retry_at = time.monotonic() + min(2 ** (failures - 1), 5)
            if failures == 1:
                log.warning('match scores temporarily unavailable; retrying within the existing scoring window')
            return False  # the original 25-second rotation timeout remains authoritative
        self._score_api_retry_at = 0.0
        self._score_api_failures = 0
        scores = (data or {}).get("scores") or []
        if not scores and elapsed < 20:
            return False       # keep retrying until the timeout

        bid = self.last_played_map.beatmap_id if self.last_played_map else 0

        # Silent farm-evidence collection. These records are NOT human ratings:
        # they are stored separately in ratings.db so farm-finder can use them
        # as empirical "this map produced notable top plays in a real lobby"
        # signals later.
        evidence_records: list[dict] = []
        scored_users: set[int] = set()
        pp_achievements: list[dict] = []

        if scores and self._activity_match_key:
            activity_scores = []
            for score in scores:
                score_uid = score.get("user_id")
                if score_uid is None:
                    continue
                score_uid = int(score_uid)
                score_info = self.players.get(score_uid) or {}
                score_user = score.get("user") or {}
                activity_scores.append({
                    "user_id": score_uid,
                    "username": str(
                        score_info.get("name")
                        or score_user.get("username")
                        or score_uid
                    ),
                    "score_pp": score.get("pp"),
                    "accuracy": (
                        float(score.get("accuracy")) * 100.0
                        if score.get("accuracy") is not None else None
                    ),
                })
            try:
                self.ratings.record_match_scores(
                    match_key=self._activity_match_key,
                    scores=activity_scores,
                )
            except Exception:
                log.exception("could not save match score activity history")

        for s in scores:
            uid = s.get("user_id")
            pp = s.get("pp")
            if not uid or not pp:
                continue

            uid = int(uid)

            # Count score submitters from the actual match. If we have a
            # MatchStarted participant snapshot, ignore unrelated/spectating
            # users for the evidence denominator.
            if not self.playing_player_ids or uid in self.playing_player_ids:
                scored_users.add(uid)

            info = self.players.get(uid)
            if not info or not info.get("top"):
                continue

            gain, rank, new_top = gain_from(info["top"], pp, bid)
            if rank is None:
                continue          # didn't make their top 100

            info["top"] = new_top
            info["threshold"] = new_top[-1][0] if len(new_top) >= 100 else 0.0

            acc = (s.get("accuracy") or 0) * 100

            evidence_records.append({
                "user_id": uid,
                "rank": int(rank),
                "pp": float(pp),
                "gain": float(gain),
                "accuracy": float(acc),
            })

            pp_achievements.append({
                "user_id": uid,
                "name": str(info["name"]),
                "pp": float(pp),
                "accuracy": float(acc),
                "rank": int(rank),
                "gain": float(gain),
            })

        # Profiles/top scores can safely stay cached across ordinary joins,
        # but a submitted score may have changed both values. Invalidate only
        # those users so a later rejoin gets a correct PP/top-100 snapshot.
        invalidate_profile = getattr(self.api, "invalidate_profile_cache", None)
        if callable(invalidate_profile):
            for scored_user_id in scored_users:
                try:
                    invalidate_profile(scored_user_id)
                except Exception:
                    log.debug(
                        "could not invalidate cached profile for user %s",
                        scored_user_id,
                        exc_info=True,
                    )

        if pp_achievements:
            # Save every detected profile-PP improvement, not only maps that also
            # meet the separate "notable performance evidence" threshold. This is
            # what makes .stats an honest per-player total going forward.
            try:
                inserted = self.pp_stats.record_achievements(
                    room_id=self.room_id,
                    playlist_item_id=self.scoring_item_id,
                    beatmap_id=bid,
                    achievements=pp_achievements,
                )
                log.debug(
                    "saved %d/%d AutoHost PP achievement(s) for map %s",
                    inserted, len(pp_achievements), bid,
                )
            except Exception:
                # PP-history is useful but must never interrupt room rotation.
                log.exception("could not save AutoHost PP achievements")

            # Every score that enters the player's top 100 is kept in the
            # backend database. Chat is intentionally much quieter: announce
            # only a new top-15 play OR at least +5.0 profile PP.
            announced_achievements = [
                a for a in pp_achievements
                if int(a.get("rank") or 999) <= 15
                or float(a.get("gain") or 0.0) >= 5.0
            ]
            if announced_achievements:
                self.say(
                    self._format_pp_achievement_message(
                        announced_achievements
                    ),
                    automatic=True,
                )
            if len(announced_achievements) != len(pp_achievements):
                log.debug(
                    "PP announcement filter: announced %d/%d achievement(s) "
                    "(top 15 or >=5.0pp gain)",
                    len(announced_achievements), len(pp_achievements),
                )

        if bid and evidence_records and self.cfg.get("ruleset_id", 0) != 3:
            participant_count = len(self.playing_player_ids)
            if participant_count <= 0:
                participant_count = max(len(scored_users),
                                        self.gameplay_player_count())

            try:
                self.ratings.record_performance_evidence(
                    beatmap_id=bid,
                    room_id=self.room_id,
                    playlist_item_id=self.scoring_item_id,
                    participant_count=participant_count,
                    scored_count=len(scored_users),
                    records=evidence_records,
                    notable_ratio=float(self.cfg.get(
                        "performance_evidence_ratio", 0.40)),
                    min_top20=int(self.cfg.get(
                        "performance_evidence_min_top20", 2)),
                    min_top30=int(self.cfg.get(
                        "performance_evidence_min_top30", 3)),
                )
            except Exception:
                # Evidence collection is useful but must never interrupt room
                # rotation or score reporting.
                log.exception("could not save silent performance evidence")

        return True

    # ------------------------------------------------------------- loop

    def _notify_ready_after_stopped_countdown(self) -> None:
        """Explain a failed countdown, without blaming transport gaps on READY.

        Called only after the stopped-countdown audit. A late MatchStarted or
        a server snapshot showing PLAYING must not produce a false reminder.
        Unchanged readiness problems get at most one reminder per five minutes.
        """
        active = self.gameplay_player_ids()
        if self.state != IDLE or not active or self.ready & active:
            return
        self._say_problem_once(
            "readiness", "Match didn't start—ready up to try again.",
            context=(getattr(self, "room_id", None), getattr(self.current_map, "beatmap_id", None),
                     tuple(sorted(active)), "not_ready"),
        )

    def _restore_map_after_readiness_stop(self, stopped_map) -> bool:
        """Keep a map consumed by a readiness-related countdown cancellation.

        lazer promotes the next playlist item when a match-start countdown is
        cancelled, even though nobody entered gameplay. Replacing that newly
        selected item with the frozen attempted map lets slow downloads/imports
        finish without silently skipping the map. A fresh READY signal is then
        required before automatic start is allowed again.
        """
        if self.state != IDLE or stopped_map is None:
            return False
        active = set(self.gameplay_player_ids())
        if not active:
            return False
        try:
            self.apply_map(stopped_map, announce=False)
        except HubTransportError:
            raise
        except Exception as exc:
            log.warning(
                "could not restore map after readiness-related countdown stop: %s",
                exc,
            )
            return False

        self._readiness_retry_beatmap_id = int(stopped_map.beatmap_id)
        self._say_problem_once(
            "readiness", "Countdown reset—same map kept. Ready up when it's imported.",
            context=(getattr(self, "room_id", None), int(stopped_map.beatmap_id),
                     tuple(sorted(active)), "map_restored"),
        )
        log.info(
            "restored beatmap %s after readiness-related countdown stop; "
            "waiting for a fresh READY signal",
            stopped_map.beatmap_id,
        )
        return True

    def readiness_retry_wait_is_active(self, active: set[int] | None = None) -> bool:
        """Hold only automatic starts until the retained map gets fresh READY."""
        retry_bid = getattr(self, "_readiness_retry_beatmap_id", None)
        if retry_bid is None:
            return False
        current_bid = getattr(self.current_map, "beatmap_id", None)
        if current_bid is None or int(current_bid) != int(retry_bid):
            log.info("readiness retry hold cleared because the current map changed")
            self._readiness_retry_beatmap_id = None
            return False
        active = set(self.gameplay_player_ids()) if active is None else set(active)
        if not active:
            self._readiness_retry_beatmap_id = None
            return False
        return not bool(self.ready & active)

    def _record_stopped_countdown_reason(self) -> str:
        """Classify and persist a stopped countdown that never started.

        This runs only after the existing grace period and server audit path
        have established that MatchStarted did not follow. Counts contain no
        player identifiers and never affect match recovery behavior.
        """
        active = set(self.gameplay_player_ids())
        ready = set(self.ready) & active
        minimum = max(0, int(self.cfg.get("min_players", 1) or 1))
        started_with = set(getattr(self, "_start_snapshot_players", set()))
        if len(active) < minimum:
            reason = "below_minimum_players"
        elif started_with - active:
            reason = "player_left_during_countdown"
        elif not ready:
            reason = "no_players_ready"
        elif len(ready) < len(active):
            reason = "some_players_not_ready"
        else:
            reason = "all_ready_server_or_user_cancelled"
        try:
            self.ratings.record_countdown_stop(
                room_id=int(self.room_id),
                playlist_item_id=self.starting_item_id,
                reason=reason,
                gameplay_players=len(active),
                ready_players=len(ready),
                minimum_players=minimum,
            )
        except Exception:
            log.exception("could not record CountdownStopped reason")
        log.info(
            "CountdownStopped classified: reason=%s players=%d ready=%d minimum=%d",
            reason, len(active), len(ready), minimum,
        )
        return reason

    def handle_event(self, name: str, payload: dict) -> None:
        if name in ("HubDisconnected", "HubReconnected"):
            # SignalRCore reconnects the websocket, but referee room membership
            # is connection-scoped. The main loop performs the rejoin + server
            # audit outside the callback thread. Ignore stale transport events
            # that were queued before a recovery which has already completed.
            connected = getattr(self.hub, "is_connected", lambda: True)()
            just_recovered = (
                time.time() - float(self._last_transport_recovery_at or 0.0)
                < 5.0
            )
            if name == "HubDisconnected" and connected:
                return
            if name == "HubReconnected" and just_recovered:
                return
            self._hub_rejoin_pending = True
            return

        event_room_id = payload.get("room_id") if isinstance(payload, dict) else None
        if (self.room_id is not None and event_room_id is not None
                and int(event_room_id) != int(self.room_id)):
            log.debug(
                "ignoring stale %s event for old room %s (current room %s)",
                name, event_room_id, self.room_id,
            )
            return

        if name == "CountdownStarted":
            if self.state == STARTING:
                self._start_countdown_seen = True
            return

        if name == "CountdownStopped":
            if self.state == STARTING:
                self._start_countdown_stopped_at = time.time()
            return

        if name == "MatchStarted":
            minimum = int(self.cfg.get("min_players", 1))
            actual = self.gameplay_player_count()

            if actual < minimum:
                # A MatchStarted can race with somebody leaving during the
                # countdown. Never enter PLAYING with an undersized room.
                log.warning("MatchStarted with only %d/%d gameplay players; "
                            "aborting", actual, minimum)
                try:
                    self.hub.abort_match(self.room_id)
                except HubTransportError:
                    # AbortMatch outcome is unknown; keep server reconciliation
                    # in control rather than falsely declaring the room IDLE.
                    raise
                except Exception as e:
                    log.warning("could not abort undersized match start: %s", e)
                self.ready.clear()
                self.starting_map = None
                self.starting_item_id = None
                self._clear_start_recovery_tracking()
                self.set_state(IDLE)
                return

            if self.state == PLAYING:
                incoming_item_id = payload.get("playlist_item_id")
                if incoming_item_id is not None:
                    incoming_item_id = int(incoming_item_id)
                if (
                    incoming_item_id is not None
                    and self.playing_item_id is not None
                    and incoming_item_id != int(self.playing_item_id)
                ):
                    # Server truth has moved to a different match while our
                    # local state still says PLAYING. This is another recovery
                    # path for an abort/promotion event that arrived oddly.
                    log.warning(
                        "new MatchStarted item %s arrived while locally PLAYING "
                        "item %s; accepting server rollover",
                        incoming_item_id, self.playing_item_id,
                    )
                    self._finish_aborted_match(announce=False)
                else:
                    # Duplicate MatchStarted notifications are harmless.
                    log.debug("duplicate MatchStarted ignored")
                    return

            if self.state == IDLE:
                # The server is authoritative. This can happen if somebody
                # joined during a countdown and our local recovery reached
                # IDLE just before the server delivered MatchStarted.
                log.info("MatchStarted arrived while locally IDLE; accepting "
                         "server state instead of aborting the match")
            elif self.state != STARTING:
                log.warning("unexpected MatchStarted while state=%s; ignoring",
                            self.state)
                return

            self._clear_skip_vote("match started")
            self._clear_replay_vote("match started")
            self._mid_match_intro_sent.clear()
            self.ready.clear()
            self._single_player_start_hold_uid = None
            self._readiness_retry_beatmap_id = None
            self._difficulty_vote_changes_since_match = 0
            self._difficulty_vote_rebuild_pending = False
            self.playing_player_ids = set(self.gameplay_player_ids())

            payload_item_id = payload.get("playlist_item_id")
            if payload_item_id is not None:
                payload_item_id = int(payload_item_id)

            # MatchStarted is the authoritative statement of what ACTUALLY
            # entered gameplay. A StartMatch snapshot is only a pre-flight
            # expectation and must never override a different server event.
            self.playing_item_id = (
                payload_item_id
                or self.starting_item_id
                or self.current_item_id
            )

            if (payload_item_id is not None
                    and self.starting_item_id is not None
                    and int(self.starting_item_id) != payload_item_id):
                log.warning(
                    "MatchStarted corrected stale StartMatch snapshot: "
                    "snapshot=%s actual=%s",
                    self.starting_item_id, payload_item_id,
                )

            self.playing_map = self._map_for_item_id(self.playing_item_id)

            # A rare missed playlist event can leave the actual MatchStarted id
            # unknown locally. Refresh metadata once; if it still cannot be
            # resolved, leave playing_map unset rather than rating the wrong map.
            if self.playing_map is None and payload_item_id is not None:
                try:
                    room = self.api.room(self.room_id)
                except Exception as e:
                    room = None
                    log.warning("could not resolve MatchStarted item %s: %s",
                                payload_item_id, e)
                if room:
                    self._sync_from_room_snapshot(room, "MatchStarted repair")
                    self.playing_map = self._map_for_item_id(payload_item_id)

            if self.playing_map is None:
                if (payload_item_id is None or self.starting_item_id is None
                        or int(self.starting_item_id) == int(self.playing_item_id or -1)):
                    self.playing_map = self.starting_map or self.current_map
                else:
                    log.error(
                        "actual MatchStarted item %s could not be mapped; "
                        "feedback for this match will be disabled rather than "
                        "assigned to the wrong map",
                        self.playing_item_id,
                    )

            if self.playing_item_id is not None and self.playing_map is not None:
                self._playlist_maps[int(self.playing_item_id)] = self.playing_map

            log.info(
                "match map frozen from actual MatchStarted: %s (item=%s)",
                self.playing_map.label if self.playing_map else "UNRESOLVED",
                self.playing_item_id,
            )
            self._note_preference_match()

            match_started_at = time.time()
            self._match_started_at = match_started_at
            # Display evidence is frozen only at the real provider event; it
            # never controls gameplay or synthesizes a missed start.
            self._display_match = {
                "playlist_item_id": self.playing_item_id,
                "beatmap_id": getattr(self.playing_map, "beatmap_id", None),
                "started_at": match_started_at,
                "clock_rate": self._server_playlist_items.get(self.playing_item_id, {}).get("display_clock_rate"),
                "participants": None,  # eligible roster is not actual participation
            }
            activity_beatmap_id = (
                getattr(self.playing_map, "beatmap_id", None)
                if self.playing_map is not None else None
            )
            if activity_beatmap_id is None and self.playing_item_id is not None:
                activity_item = self._server_playlist_items.get(
                    int(self.playing_item_id), {}
                )
                activity_beatmap_id = activity_item.get("beatmap_id")
            if self.room_id is not None and activity_beatmap_id:
                participants = [
                    {
                        "user_id": int(uid),
                        "username": str(
                            (self.players.get(uid) or {}).get("name") or uid
                        ),
                    }
                    for uid in sorted(self.playing_player_ids)
                ]
                try:
                    self._activity_match_key = self.ratings.record_match_start(
                        room_id=int(self.room_id),
                        playlist_item_id=self.playing_item_id,
                        beatmap_id=int(activity_beatmap_id),
                        map_label=(
                            self.playing_map.label
                            if self.playing_map is not None else None
                        ),
                        star_rating=(
                            float(self.playing_map.sr)
                            if self.playing_map is not None else None
                        ),
                        participants=participants,
                        started_at=match_started_at,
                    )
                except Exception:
                    self._activity_match_key = None
                    log.exception("could not record MatchStarted activity history")
            else:
                self._activity_match_key = None
                log.warning(
                    "MatchStarted activity history skipped because beatmap id "
                    "could not be resolved"
                )

            self.starting_map = None
            self.starting_item_id = None
            self._clear_start_recovery_tracking()
            self._explicit_abort_pending = False
            self._early_abort_pending = None
            self.set_state(PLAYING)
            self._notify_discord_lobby_event(
                "MATCH START",
                (
                    f"Started: {self.playing_map.label}"
                    if self.playing_map is not None
                    else "Match started (map unresolved)"
                ),
                map_key=self._discord_map_key(self.playing_map),
            )

        elif name == "MatchCompleted":
            # A silently-evicted referee can miss MatchStarted yet receive the
            # later completion after rejoining. Recover the frozen StartMatch
            # snapshot so scoring/ratings target the map that actually ran.
            if self.state == STARTING and self.starting_item_id is not None:
                completed_id = payload.get("playlist_item_id")
                if completed_id is not None:
                    try:
                        completed_id = int(completed_id)
                    except Exception:
                        completed_id = None
                if completed_id is None or completed_id == int(self.starting_item_id):
                    recovered_id = int(self.starting_item_id)
                    log.warning(
                        "MatchCompleted arrived without MatchStarted for item %s; "
                        "recovering from StartMatch snapshot",
                        recovered_id,
                    )
                    self.playing_item_id = recovered_id
                    self.playing_map = (
                        self.starting_map or self._map_for_item_id(recovered_id)
                    )
                    self.playing_player_ids = set(self._start_snapshot_players)
                    self.starting_map = None
                    self.starting_item_id = None
                    self._clear_start_recovery_tracking()
            self._early_abort_pending = None
            self.handle_match_completed(payload)

        elif name == "MatchAborted":
            now = time.time()
            minimum = int(self.cfg.get("min_players", 1))
            actual = self.gameplay_player_count()

            stale_after_start = (
                self.state == PLAYING
                and not self._explicit_abort_pending
                and self._match_started_at > 0
                and (now - self._match_started_at) <= 3.0
                and actual >= minimum
            )

            if stale_after_start:
                item_id = (
                    payload.get("playlist_item_id")
                    or self.playing_item_id
                    or self.current_item_id
                )
                if item_id is None:
                    self._finish_aborted_match(announce=True)
                    return
                self._early_abort_pending = {
                    "item_id": int(item_id),
                    "at": now,
                    "actual": actual,
                    "minimum": minimum,
                }
                log.warning(
                    "early MatchAborted %.2fs after MatchStarted with %d/%d "
                    "players; waiting for playlist confirmation before "
                    "deciding whether it is stale",
                    now - self._match_started_at,
                    actual,
                    minimum,
                )
                self._resolve_pending_early_abort(
                    "already-known playlist state", verify_remote=False
                )
                return

            force_skip_abort = self._force_skip_abort_pending
            self._force_skip_abort_pending = False
            self._finish_aborted_match(announce=not force_skip_abort)

        elif name == "UserJoined":
            uid = int(payload.get("user_id") or 0)
            if uid:
                store=self._ban_store()
                if store and uid in store.bans and not self._moderation_protected(uid):
                    self.on_user_joined(uid)
                    return  # No profile fetches, welcomes or vote participation.
                # Capture whether there were any gameplay players BEFORE this
                # join. If this is the first player after an empty period, the
                # old IDLE timer may already be minutes/hours old; without a
                # download gate the bot can start a countdown immediately.
                was_gameplay_empty = self.gameplay_player_count() == 0
                was_truly_empty = not self._occupants()
                was_playing = getattr(self, "state", IDLE) == PLAYING

                # Update roster/pp data only. Do not touch an active countdown
                # or retune the pool because someone joined.
                self.on_user_joined(uid)
                if len(self._occupants()) > 1:
                    self._preference_choice = None
                    self._preference_played = None
                    self._release_recalled_difficulty()
                self._track_empty_room()

                custom_greeting_sent = False
                if uid in self.players:
                    player_name = str(
                        (self.players.get(uid) or {}).get("name") or uid
                    )
                    self._notify_discord_lobby_event(
                        "JOIN", f"{player_name} joined"
                    )
                    custom_greeting_sent = self._send_custom_join_greeting(uid)

                if (
                    was_gameplay_empty
                    and uid not in self.non_gameplay_user_ids()
                    and uid in self.players
                ):
                    self._first_player_download_wait_uid = int(uid)
                    self._idle_since = time.time()
                    player_name = str(
                        (self.players.get(uid) or {}).get("name") or uid
                    )
                    recalled = self._restore_solo_difficulty(uid) if was_truly_empty else None
                    if not custom_greeting_sent and not was_playing:
                        self.say(
                            self._first_player_welcome(uid, player_name, recalled),
                            automatic=True,
                        )
                    log.info(
                        "first gameplay player joined an empty room: user %s; "
                        "waiting for READY before auto-start so the current "
                        "beatmap can finish downloading",
                        uid,
                    )
                elif (
                    self._first_player_download_wait_uid is not None
                    and self.gameplay_player_count() > 1
                ):
                    log.info(
                        "first-player download wait cleared because another "
                        "gameplay player joined"
                    )
                    self._first_player_download_wait_uid = None

                if was_playing and uid in self.players:
                    self._send_mid_match_join_intro(uid)

        elif name in ("UserLeft", "UserKicked"):
            uid = int((payload.get("kicked_user_id") if name == "UserKicked" else payload.get("user_id")) or 0)
            if uid and uid not in self.non_gameplay_user_ids():
                self._save_departing_preference(uid, kicked=name == "UserKicked")
                info = self.players.pop(uid, None)
                getattr(self, "_pending_player_profiles", {}).pop(uid, None)
                self._on_difficulty_voter_left(uid)
                player_name = str((info or {}).get("name") or uid)
                action = "was kicked" if name == "UserKicked" else "left"
                self._notify_discord_lobby_event(
                    "LEAVE", f"{player_name} {action}"
                )
                self._flush_player_lobby_time(uid, info)
                if self.room_id is not None:
                    try:
                        self.ratings.record_room_player_leave(room_id=int(self.room_id),user_id=uid,username=player_name)
                    except Exception:
                        log.exception('could not record activity leave')
                self.ready.discard(uid)
                getattr(self, "_unready_start_grace_users", set()).discard(uid)
                self._refresh_skip_vote_quorum()
                self._refresh_replay_vote_quorum()
                if uid == self._single_player_start_hold_uid:
                    log.info(
                        "single-player start hold cleared because user %s left",
                        uid,
                    )
                    self._single_player_start_hold_uid = None
                if uid == self._first_player_download_wait_uid:
                    log.info(
                        "first-player download wait cleared because user %s left",
                        uid,
                    )
                    self._first_player_download_wait_uid = None
                self.mark_auto_retune_pending()
                self._track_empty_room()
                self._close_if_armed_and_empty()

            # Safety only: if the countdown no longer has the configured
            # minimum number of players, cancel it so the lobby can recover.
            # This is unrelated to difficulty retuning.
            self.cancel_start_if_too_few_players()

        elif name == "UserStatusChanged":
            uid = int(payload.get("user_id") or 0)
            status = payload.get("status")
            if uid and uid not in self.non_gameplay_user_ids():
                previously_ready = uid in self.ready and uid in self.gameplay_player_ids()
                if uid in self.players:
                    self.players[uid]["status"] = status
                if status == "ready":
                    self.ready.add(uid)
                    if uid == self._single_player_start_hold_uid:
                        log.info(
                            "single-player start hold cleared because user %s "
                            "readied again",
                            uid,
                        )
                        self._single_player_start_hold_uid = None
                    if uid == self._first_player_download_wait_uid:
                        log.info(
                            "first-player download wait cleared because user %s "
                            "is READY; current beatmap is available",
                            uid,
                        )
                        self._first_player_download_wait_uid = None
                        # READY should be allowed to trigger the normal early-
                        # start threshold immediately on the next loop.
                else:
                    self.ready.discard(uid)

                self.results_screen_start_is_held()

                # In a one-player lobby READY is a hard gate, not merely an
                # early-start hint. If that lone player unreadies during the
                # countdown, stop it and wait for READY again. Multi-player
                # lobbies retain the normal threshold behavior.
                self.cancel_single_player_start_if_unready(
                    status, previously_ready=previously_ready,
                )

                # Status changes affect the NEXT auto-difficulty calculation.
                self.mark_auto_retune_pending()
                self._refresh_skip_vote_quorum()
                self._refresh_replay_vote_quorum()
                self._refresh_difficulty_vote_quorum()

        elif name == "PlaylistItemAdded":
            item = self._event_playlist_item(payload)
            self._remember_playlist_item(item)

        elif name == "PlaylistItemChanged":
            item = self._event_playlist_item(payload)
            self._adopt_playlist_item(item)
            self._resolve_pending_early_abort(
                "PlaylistItemChanged", verify_remote=False
            )

        elif name == "PlaylistItemRemoved":
            item = self._event_playlist_item(payload)
            raw_id = (item.get("id") if item else None)
            if raw_id is None and isinstance(payload, dict):
                raw_id = payload.get("playlist_item_id")
            if raw_id is not None:
                item_id = int(raw_id)
                self._server_playlist_items.pop(item_id, None)
                self._playlist_maps.pop(item_id, None)
                getattr(self, '_standard_playlist_edits', {}).pop(item_id, None)
                self.queued = [pair for pair in self.queued if pair[0] != item_id]
                self._playlist_dirty = True

        elif name == "RoomSettingsChanged":
            current = payload.get("playlist_item_id") if isinstance(payload, dict) else None
            if current is not None:
                self._server_current_item_id = int(current)
                self._adopt_current_item_id(
                    int(current), "RoomSettingsChanged"
                )
                self._resolve_pending_early_abort(
                    "RoomSettingsChanged", verify_remote=False
                )

    @staticmethod
    def _is_referee_membership_error(exc: Exception) -> bool:
        """Return True only for definite Error 2 referee-membership loss.

        This is intentionally narrower than general room-state recovery. A
        timeout/connection failure has an unknown command outcome and belongs to
        the existing transport-recovery path instead.
        """
        text = str(exc).casefold()
        return (
            "not a referee in the specified room" in text
            or ("error 2" in text and "not a referee" in text)
        )

    def _abandon_room_after_referee_loss(
            self, operation: str, last_error: Exception) -> None:
        """Best-effort CloseRoom, then fail the process so the launcher restarts.

        Once three verified JoinRoom attempts have failed we no longer trust any
        local room state. CloseRoom itself may also return Error 2 if osu! has
        already destroyed or detached us from the room; that still counts as an
        unrecoverable room and must not become an infinite retry loop.
        """
        rid = int(self.room_id) if self.room_id is not None else None
        if rid is None:
            raise RuntimeError(
                f"referee membership recovery failed during {operation}: "
                f"{last_error}"
            )

        log.error(
            "referee membership for room %s could not be restored after 3 "
            "attempts during %s; attempting CloseRoom once before exiting",
            rid, operation,
        )

        close_error = None
        try:
            self.hub.close_room(rid)
            log.warning(
                "closed room %s after unrecoverable referee membership loss", rid
            )
        except Exception as e:
            close_error = e
            log.error(
                "CloseRoom(%s) after referee membership loss was refused: %s; "
                "treating the old room as unrecoverable anyway",
                rid, e,
            )

        try:
            self.ratings.record_match_end(
                match_key=self._activity_match_key,
                room_id=rid,
                playlist_item_id=self.playing_item_id,
                result="referee_membership_lost",
            )
            self.ratings.record_room_close(rid, "referee_membership_lost")
        except Exception:
            log.exception(
                "could not finalize activity history after referee membership loss"
            )

        # Prevent shutdown() from hammering the same stale room a second time.
        # The resilient launcher sees the RuntimeError as a non-zero exit and can
        # start a fresh lobby.
        self.room_id = None
        self.channel_id = None
        self._hub_rejoin_pending = False

        detail = f"; CloseRoom also failed: {close_error}" if close_error else ""
        raise RuntimeError(
            f"lost referee membership for room {rid} during {operation}; "
            f"3 rejoin attempts failed: {last_error}{detail}"
        )

    def _referee_operation_with_rejoin(self, operation: str, call):
        """Run a referee operation and recover a definite Error 2 in place.

        Normal success is a single call. If osu! says this account is no longer
        a referee in the room, try JoinRoom up to three times. Each successful
        JoinRoom is verified by retrying the *same* operation, which is safe here
        because Error 2 is a definite server rejection rather than an unknown
        transport outcome. Transport errors still escape to _recover_hub_transport
        and never consume these membership retries.
        """
        try:
            return call()
        except HubTransportError:
            raise
        except Exception as first_error:
            if not self._is_referee_membership_error(first_error):
                raise
            last_error = first_error

        rid = int(self.room_id) if self.room_id is not None else None
        if rid is None:
            raise RuntimeError(
                f"{operation} reported referee membership loss with no room id"
            )

        attempts = max(1, int(self.cfg.get("referee_rejoin_attempts", 3)))
        # Keep the requested behavior at three by default while still allowing a
        # local override for testing. Cap it so a bad config cannot retry forever.
        attempts = min(attempts, 10)
        delay = max(0.5, float(
            self.cfg.get("referee_rejoin_delay_seconds", 2.0)))

        log.warning(
            "lost referee membership in room %s during %s: %s",
            rid, operation, last_error,
        )

        for attempt in range(1, attempts + 1):
            invalidate = getattr(self.hub, "invalidate_membership", None)
            if callable(invalidate):
                invalidate(rid)
            wait_connected = getattr(self.hub, "wait_connected", None)
            if callable(wait_connected) and not wait_connected(5.0):
                # This is now transport loss, not a definite membership refusal.
                raise HubDisconnected(
                    f"hub disconnected while recovering referee membership in "
                    f"room {rid} during {operation}"
                )

            log.warning(
                "referee rejoin attempt %d/%d for room %s after %s",
                attempt, attempts, rid, operation,
            )
            try:
                join = getattr(self.hub, "join_room", None)
                if callable(join):
                    join(rid)
                else:
                    self.hub.invoke("JoinRoom", [rid])
            except HubTransportError:
                raise
            except Exception as join_error:
                text = str(join_error).casefold()
                # Some server builds answer "already in room" after membership
                # was restored by a racing event/reconnect. Verify via the real
                # operation instead of counting that wording as a failure.
                if "already" not in text or "room" not in text:
                    last_error = join_error
                    log.warning(
                        "referee rejoin attempt %d/%d for room %s was refused: %s",
                        attempt, attempts, rid, join_error,
                    )
                    if attempt < attempts:
                        time.sleep(delay)
                    continue

            try:
                result = call()
            except HubTransportError:
                raise
            except Exception as verify_error:
                if not self._is_referee_membership_error(verify_error):
                    # Membership was restored; this is an unrelated business
                    # rejection and belongs to the original caller.
                    raise
                last_error = verify_error
                log.warning(
                    "referee rejoin attempt %d/%d joined room %s but %s still "
                    "returned Error 2",
                    attempt, attempts, rid, operation,
                )
                if attempt < attempts:
                    time.sleep(delay)
                continue

            # Membership is only considered restored after the original referee
            # operation itself succeeds. Refresh chat membership opportunistically.
            try:
                self._join_room_chat()
            except Exception:
                pass
            self._hub_rejoin_pending = False
            self._room_missing_confirmations = 0
            self._last_transport_recovery_at = time.time()
            log.warning(
                "referee membership restored in room %s on attempt %d/%d; %s "
                "succeeded",
                rid, attempt, attempts, operation,
            )
            return result

        self._abandon_room_after_referee_loss(operation, last_error)

    def _recreate_missing_room(self, reason: str) -> None:
        """Replace a server-side room that has disappeared while unattended."""
        old_room_id = int(self.room_id) if self.room_id else None
        if old_room_id is None:
            return

        log.error(
            "room %s is confirmed missing on osu! during %s; creating a "
            "replacement lobby instead of running against stale local state",
            old_room_id, reason,
        )
        try:
            self.ratings.record_match_end(
                match_key=self._activity_match_key,
                room_id=old_room_id,
                playlist_item_id=self.playing_item_id,
                result="room_missing",
            )
            self.ratings.record_room_close(old_room_id, "server_missing")
        except Exception:
            log.exception("could not finalize missing-room activity history")

        forget = getattr(self.hub, "forget_missing_room", None)
        if callable(forget):
            forget(old_room_id)
        self._reset_room_runtime_state_for_reopen()
        if hasattr(self.hub, "wait_connected") and not self.hub.wait_connected(30.0):
            raise HubDisconnected(
                "cannot create replacement room while referee hub is disconnected")

        time.sleep(1.0)
        self.create_room()
        self._idle_since = time.time()
        log.warning(
            "automatic room replacement complete: old room %s -> new room %s",
            old_room_id, self.room_id,
        )

    def _recover_hub_transport(self, cause) -> None:
        """Reconnect/rejoin the same referee room without replaying commands."""
        if not self.room_id:
            raise HubDisconnected(f"transport recovery has no active room: {cause}")

        rid = int(self.room_id)
        deadline = time.monotonic() + max(30.0, float(self.cfg.get(
            "hub_recovery_seconds", 120.0)))
        log.warning(
            "referee transport interrupted in room %s: %s; waiting to "
            "reconnect and rejoin without blind command retries",
            rid, cause,
        )

        last_error = None
        while self.running and time.monotonic() < deadline:
            self._drain_discord_control_commands(recovering=True)
            if self.process_control.pending_shutdown() is not None:
                self.running = False
            if not self.running:
                return
            wait_connected = getattr(self.hub, "wait_connected", None)
            if callable(wait_connected) and not wait_connected(1.0):
                continue

            # First determine whether there is still a room to recover. A real
            # 404 is different from a network failure.
            probe = getattr(self.api, "room_probe", None)
            status, room = ("unknown", None)
            if callable(probe):
                try:
                    status, room = probe(rid)
                except Exception as e:
                    last_error = e
                    time.sleep(2.0)
                    continue

            if status == "missing":
                self._room_missing_confirmations = max(
                    self._room_missing_confirmations + 1, 1)
                needed = max(2, int(self.cfg.get(
                    "room_missing_confirmations", 2)))
                if self._room_missing_confirmations >= needed:
                    self._recreate_missing_room("transport recovery")
                    self._hub_rejoin_pending = False
                    return
                time.sleep(2.0)
                continue
            if status == "unknown":
                time.sleep(2.0)
                continue

            self._room_missing_confirmations = 0
            try:
                join = getattr(self.hub, "join_room", None)
                if callable(join):
                    join(rid)
                else:
                    self.hub.invoke("JoinRoom", [rid])
            except HubTransportError as e:
                last_error = e
                time.sleep(2.0)
                continue
            except Exception as e:
                # Some server versions may report that an already-restored
                # referee is already in the room. That is harmless.
                text = str(e).casefold()
                if "already" not in text or "room" not in text:
                    last_error = e
                    log.warning("JoinRoom(%s) during recovery was refused: %s", rid, e)
                    time.sleep(2.0)
                    continue

            try:
                if room:
                    self._sync_from_room_snapshot(room, "hub recovery")
                    self._last_playlist_audit = time.time()
                if not self._join_room_chat() and self.cfg.get("_headless_supervised", False):
                    invalidate = getattr(self.hub, "invalidate_membership", None)
                    if callable(invalidate):
                        invalidate(rid)
                    raise HubDisconnected("chat membership not restored yet")
                if not self.running:
                    return
                self._chat_websocket_needs_reconcile = True
                self._hub_rejoin_pending = False
                self._last_transport_recovery_at = time.time()

                # Only mutate/refill between maps. STARTING and PLAYING preserve
                # their local state until existing timeout/event reconciliation
                # establishes what happened while disconnected.
                if self.state in (IDLE, SCORING):
                    self.reconcile_playlist(
                        reason="hub recovery", fetch_remote=False,
                        ensure_depth=True, trim_extras=True,
                    )
                log.warning(
                    "referee transport recovery complete in room %s; state=%s",
                    rid, self.state,
                )
                return
            except HubTransportError as e:
                last_error = e
                time.sleep(2.0)
                continue

        if not self.running:
            return
        raise HubDisconnected(
            f"could not recover referee connection for room {rid}: {last_error}")

    def run(self) -> None:
        try:
            self.create_room()
        except Exception:
            # Do not paint the public Discord channel red merely because the
            # process is starting. After an abrupt crash Discord may still
            # correctly show the last-known green/open indicator. A successful
            # create_room() publishes a forced green/open snapshot once the new
            # room is actually ready. If startup fails, *then* publish red so a
            # stale green indicator is not left behind indefinitely.
            self._update_discord_public_status(
                force=True,
                is_open=False,
                lifecycle="Startup failed",
                rename_channel=True,
            )
            try:
                self.discord_control.flush(timeout=1.5)
            except Exception:
                log.debug(
                    "could not flush failed-start Discord public status",
                    exc_info=True,
                )
            # create_room already closed the room if it got that far.
            raise

        self._idle_since = time.time()
        self._start_chat_patrol()
        try:
            while self.running:
                try:
                    self._loop()
                except HubTransportError as e:
                    if not self.running:
                        break
                    self._recover_hub_transport(e)
        finally:
            self.shutdown()

    def _loop(self) -> None:
        while self.running:
            lifecycle = getattr(self, "_lifecycle", None)
            if lifecycle is not None and not lifecycle.tick(self):
                continue
            if self.room_id and not getattr(self.hub, "is_connected", lambda: True)():
                self._recover_hub_transport("referee transport watchdog")
                if not self.running:
                    break
            try:
                while True:
                    name, payload = self.events.get_nowait()
                    self.handle_event(name, payload)
            except queue.Empty:
                pass

            if self._hub_rejoin_pending and self.room_id:
                self._recover_hub_transport("SignalR reconnect/disconnect event")

            self._reload_config_from_disk()
            self._refresh_operator_exclusions()
            self._enforce_project_bans()
            shutdown_event = self.process_control.pending_shutdown()
            if shutdown_event is not None:
                log.warning(
                    "shared shutdown requested for %s by %s",
                    shutdown_event["target"],
                    shutdown_event["requested_by"] or "unknown",
                )
                self.running = False
                continue
            if time.monotonic()-getattr(self,'_activity_checkpoint_at',0)>=60:
                self._flush_all_lobby_time()
                self._activity_checkpoint_at=time.monotonic()

            # Apply queued live commands with fresh referee/configuration and
            # shutdown state, before background work can wait on paced HTTP.
            # All room mutations stay serialized on this same thread.
            if self.channel_id:
                self.poll_chat()
                if not self.running:
                    continue

            self._drain_feedback_replies()
            self._drain_ai_results()
            self._drain_discord_control_commands()
            self._flush_room_chat()
            self._flush_stats_replies()
            self._flush_player_profiles()

            # This is snapshot-driven, so the inexpensive call below only
            # queues a Discord edit after a player/map/range/state change.
            self._update_discord_public_status()

            self._maybe_language_offer()

            if self._maybe_reset_empty_autodiff():
                continue

            if self.state == SCORING:
                if self.try_score_report() or (time.time() - self._match_end_at) > 25:
                    self.rotate()

            elif self.state == IDLE:
                # Automatic SR changes are intentionally NOT applied here.
                # Joins/leaves/status changes only set _auto_retune_pending;
                # rotate() consumes the latest roster once, after a real map
                # completes.  This prevents repeated title/chat/queue churn
                # while players are downloading and readying between maps.
                if self._run_pending_fixed_range_rebuild():
                    time.sleep(0.4)
                    continue

                if self._run_pending_difficulty_vote_rebuild():
                    time.sleep(0.4)
                    continue

                if self._maybe_queue_automatic_hard_restart():
                    # The old room was closed and a replacement was created.
                    # Let its first playlist/chat setup settle before starting.
                    time.sleep(0.4)
                    continue

                if self._pending_soft_restart:
                    self.perform_soft_restart()
                    # Restarting resets _idle_since and may send several hub
                    # calls; do not immediately launch a countdown in the same
                    # loop iteration.
                    time.sleep(0.4)
                    continue

                audit_every = max(3.0, float(self.cfg.get(
                    "playlist_audit_seconds", 8.0)))
                if time.time() - self._last_playlist_audit >= audit_every:
                    self.reconcile_playlist(
                        reason="idle watchdog", fetch_remote=True,
                        ensure_depth=True, trim_extras=True,
                    )

                wait = self.cfg.get("auto_start_seconds", 45)
                minimum = int(self.cfg.get("min_players", 1))
                active = self.gameplay_player_ids()
                waiting_for_results = self.results_screen_start_is_held(active)
                enough = len(active) >= minimum
                held_for_lone_player = self.single_player_start_is_held(active)
                waiting_for_first_download = (
                    self.first_player_download_wait_is_active(active)
                )
                waiting_for_retry_ready = (
                    self.readiness_retry_wait_is_active(active)
                )
                now = time.time()

                # A first .skip vote pauses/rescinds any match-start countdown
                # and reserves eight seconds for additional votes. If the vote
                # expires without passing, clear it silently and resume the
                # normal ready/auto-start logic.
                if self._skip_vote_until > 0:
                    if self._skip_vote_item_id != self.current_item_id:
                        self._clear_skip_vote("map changed")
                    elif now >= self._skip_vote_until:
                        # Chat normally polls every several seconds. Do one
                        # forced catch-up exactly when the eight-second vote
                        # expires so votes typed near the end are read before
                        # the window is discarded. Message timestamps ensure
                        # anything actually sent after the deadline does not
                        # count toward the expired vote.
                        self.poll_chat(force=True)
                        if self._skip_vote_until > 0:
                            self._clear_skip_vote("vote window expired")
                    else:
                        time.sleep(0.4)
                        continue

                # Replay votes use the same short protected window as skips.
                # Their map context includes both the current upcoming item and
                # the previous completed map, so neither can spill into a later
                # rotation if chat delivery is delayed.
                if self._replay_vote_until > 0:
                    if (
                        self._replay_vote_item_id != self.current_item_id
                        or self.last_played_map is None
                        or self._replay_vote_last_beatmap_id
                        != int(self.last_played_map.beatmap_id)
                    ):
                        self._clear_replay_vote("map context changed")
                    elif now >= self._replay_vote_until:
                        self.poll_chat(force=True)
                        if self._replay_vote_until > 0:
                            self._clear_replay_vote("vote window expired")
                    else:
                        time.sleep(0.4)
                        continue

                self._sync_countdown_wait_item()
                wait_delay = max(0.0, float(self._countdown_wait_delay_seconds))
                waited = now - self._idle_since > wait + wait_delay
                # A pending .wait must also suppress the normal READY-based
                # early start.  Once the added delay has elapsed, the regular
                # auto-start gate resumes.
                early_ready = (
                    self.ready_threshold_met() and wait_delay <= 0.0
                )
                # Elapsed time cannot start a room with nobody ready.
                single_player_ready = (
                    bool(self.ready & active)
                )
                if (
                    enough
                    and not waiting_for_results
                    and single_player_ready
                    and not held_for_lone_player
                    and not waiting_for_first_download
                    and not waiting_for_retry_ready
                    and (waited or early_ready)
                ):
                    if not waited and early_ready:
                        log.info("ready threshold met (%d/%d), starting early",
                                 len(self.ready & active), len(active))
                    self.start_match()

            elif self.state == STARTING:
                minimum = int(self.cfg.get("min_players", 1))
                actual = self.gameplay_player_count()
                cd = float(
                    self._active_countdown_seconds
                    or self.cfg.get("countdown_seconds", 15)
                )
                elapsed = time.time() - self._state_since

                # A CountdownStopped we actually received is useful evidence: if
                # no MatchStarted follows within a few seconds, the countdown was
                # probably cancelled rather than silently running. The Aug-30
                # same-account eviction failure was different: StartMatch was
                # acknowledged, then *all* countdown/match events disappeared.
                stopped_without_start = (
                    self._start_countdown_stopped_at > 0
                    and time.time() - self._start_countdown_stopped_at > 3.0
                )

                if stopped_without_start:
                    countdown_stop_reason = self._record_stopped_countdown_reason()
                    stopped_map = self.starting_map
                    log.warning(
                        "CountdownStopped was seen but MatchStarted did not follow; "
                        "returning to IDLE after server audit (reason=%s)",
                        countdown_stop_reason,
                    )
                    self.ready.clear()
                    self.starting_map = None
                    self.starting_item_id = None
                    self._clear_start_recovery_tracking()
                    self.set_state(IDLE)
                    self.reconcile_playlist(
                        reason="stopped-countdown recovery", fetch_remote=True,
                        ensure_depth=True, trim_extras=True,
                    )
                    # The reported failure is specifically the all-unready
                    # transition while downloads/imports are still finishing.
                    # A lobby with at least one READY player keeps its existing
                    # multiplayer behavior.
                    readiness_stop = countdown_stop_reason == "no_players_ready"
                    restored = (
                        self._restore_map_after_readiness_stop(stopped_map)
                        if readiness_stop else False
                    )
                    if not restored:
                        self._notify_ready_after_stopped_countdown()

                elif actual < minimum and elapsed <= cd + 7.0:
                    # During the ordinary countdown window, preserve the old
                    # too-few-players cancellation behavior. Once an acknowledged
                    # start has outlived that window with no events, local roster
                    # state may itself be stale, so do not send AbortMatch blindly.
                    self.cancel_start_if_too_few_players()

                elif elapsed > cd + 7.0 and self._start_command_acknowledged:
                    if not self._start_event_gap:
                        log.warning(
                            "StartMatch was acknowledged but no MatchStarted event "
                            "arrived; suspected silent referee room/subscription "
                            "loss. Holding STARTING and recovering membership "
                            "instead of retrying StartMatch"
                        )
                    self._recover_missed_start_event()

                elif elapsed > cd + 7.0:
                    # No positive StartMatch completion was ever received. This
                    # is the older unknown/failed-countdown path. Reconcile once
                    # and return to IDLE, with the single-player anti-burn hold.
                    active = self.gameplay_player_ids()
                    if len(active) == 1:
                        held_uid = next(iter(active))
                        self._single_player_start_hold_uid = held_uid
                        log.warning(
                            "countdown finished but MatchStarted was not seen; "
                            "single-player start hold enabled for user %s so "
                            "the bot does not burn through playlist items. "
                            "Waiting for that player to ready again/rejoin, "
                            "another player to join, or admin .start",
                            held_uid,
                        )
                    else:
                        log.warning(
                            "countdown finished without a confirmed StartMatch; "
                            "returning to IDLE and reconciling playlist"
                        )
                    self.ready.clear()
                    self.starting_map = None
                    self.starting_item_id = None
                    self._clear_start_recovery_tracking()
                    self.set_state(IDLE)
                    self.reconcile_playlist(
                        reason="countdown recovery", fetch_remote=True,
                        ensure_depth=True, trim_extras=True,
                    )

            elif self.state == PLAYING:
                if self._early_abort_pending:
                    self._resolve_pending_early_abort(
                        "remote playlist audit", verify_remote=True
                    )
                    if self.state != PLAYING:
                        time.sleep(0.4)
                        continue

                # Safety valve: use the actual playing-map length for unlimited
                # Loved pools, otherwise the larger of it and the pool ceiling.
                playing_length = float(
                    getattr(self.playing_map, "hit_length", 0) or 0
                )
                configured_ceiling = float(self.pool.max_length or 0)
                expected_length = max(playing_length, configured_ceiling)
                if expected_length <= 0:
                    expected_length = 3600.0
                if time.time() - self._state_since > expected_length + 180:
                    log.warning("PLAYING timed out, assuming match ended")
                    try:
                        self.ratings.record_match_end(
                            match_key=self._activity_match_key,
                            room_id=self.room_id,
                            playlist_item_id=self.playing_item_id,
                            result="timeout_recovery",
                        )
                    except Exception:
                        log.exception(
                            "could not mark timed-out match in activity history"
                        )
                    self._activity_match_key = None
                    self.set_state(IDLE)

            time.sleep(0.4)

    def shutdown(self) -> None:
        """Close the room. MUST run even on Ctrl+C.

        CloseRoom only works while still joined as referee -- once the
        connection drops you are no longer in the room and the call fails
        with InvalidStateException, leaving it open and occupying one of the
        four room slots a non-bot account gets.
        """
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self.running = False
        self._room_ready = False
        try:
            self._update_discord_public_status(
                force=True, is_open=False, lifecycle="Offline"
            )
        except Exception:
            # A retired worker may already be fenced from its broker. Finish
            # local cleanup and lobby-time persistence even if publishing fails.
            log.debug("could not publish final Discord status", exc_info=True)
        try:
            self.discord_control.flush(timeout=1.5)
        except Exception:
            log.debug("could not flush Discord public status", exc_info=True)
        try:
            self._stop_chat_patrol(timeout=2.0)
        except Exception:
            log.exception("could not stop chat patrol cleanly")
        try:
            self._detach_chat_websocket()
        except Exception:
            log.exception("could not detach real-time chat route cleanly")
        try:
            self.ai.stop(timeout=2.0)
        except Exception:
            log.exception("could not stop AI worker cleanly")
        try:
            self.pp_projection.stop(timeout=1.0)
        except Exception:
            log.exception("could not stop map PP worker cleanly")
        try:
            self.discord_modlog.stop(timeout=2.0)
        except Exception:
            log.exception("could not stop Discord mod-log worker cleanly")
        try:
            self.discord_control.stop(timeout=3.0)
        except Exception:
            log.exception("could not stop Discord control worker cleanly")
        self._flush_all_lobby_time()
        if not self.room_id or not self.cfg.get("close_room_on_exit", True):
            return
        closing_room_id = int(self.room_id)
        try:
            self.hub.close_room(closing_room_id)
            log.info("room %s closed", closing_room_id)
            try:
                self.ratings.record_match_end(
                    match_key=self._activity_match_key,
                    room_id=closing_room_id,
                    playlist_item_id=self.playing_item_id,
                    result="room_closed",
                )
                self.ratings.record_room_close(closing_room_id, "shutdown")
            except Exception:
                log.exception("could not finalize shutdown activity history")
        except Exception as e:
            log.error("could not close room %s: %s", closing_room_id, e)
            log.error("after this process exits, close manually: python close_room.py --config \"%s\" %s",
                      self.cfg.get("_config_path", "instances/room1.json"), closing_room_id)
        finally:
            self.room_id = None
