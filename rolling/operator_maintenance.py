"""Main-thread maintenance notices and match-boundary drain, owned by parent."""
import queue
import time

from bot.api import ApiAdmissionDeferred, ApiChatDeliveryUncertain


class WorkerDrain:
    def __init__(self):
        self.job = None
        self.phase = 'idle'
        self.active_seen = False
        self.notice_at = 0.
        self.retry_at = 0.
        self.attempts = 0
        self.room_id = None
        self.failure = {}

    def command(self, job):
        # Called only by Lifecycle.tick on the gameplay thread.
        if job['phase'] == 'cancelled':
            if self.job is None:self.job=dict(job)
            if self.job and self.job['id'] == job['id']:
                self.phase = 'cancelled'
            return
        if self.job and self.job['id'] == job['id']: return
        self.job = dict(job)
        self.phase = 'announcing' if job['mode'] == 'one-map' else 'waiting_empty'
        self.active_seen = False
        self.notice_at = self.retry_at = 0.
        self.attempts = 0
        self.room_id = None
        self.failure = {}

    def status(self):
        return {'id':self.job['id'] if self.job else None, 'phase':self.phase,
                'mode':self.job['mode'] if self.job else None,
                'notice_confirmed':bool(self.notice_at), 'active_map_observed':self.active_seen,
                **self.failure}

    def tick(self, bot):
        if not self.job or self.phase in ('idle','cancelled','notice_uncertain','notice_failed'):
            return True
        if time.time() >= self.job['expires_at']:
            self.phase = 'cancelled'; return True
        if self.room_id is None: self.room_id = bot.room_id
        if bot.room_id != self.room_id:
            self.phase = 'room_changed'; return True
        # A drained worker still observes fresh joins/disconnects. Never starve
        # referee delivery just because ordinary gameplay is paused.
        if self.phase == 'ready':
            try:
                while True:
                    name, payload = bot.events.get_nowait()
                    bot.handle_event(name, payload)
            except queue.Empty:
                pass
        safe = (bot.running and bot._room_ready and not bot._hub_rejoin_pending
                and bot.hub.is_connected() and bot.events.empty())
        if not safe: return True
        # Empty rooms need no occupied-match warning. The parent still checks
        # the independent referee roster under its lock before closing. A late
        # join must return to announcing, never inherit occupied-close consent.
        if self.job['mode'] == 'one-map' and bot.state == 'IDLE' and not bot.players:
            self.phase = 'ready'
            time.sleep(.05)
            return False
        if self.job['mode'] == 'one-map' and self.phase == 'ready' and not self.notice_at:
            self.phase = 'announcing'
        if self.phase == 'announcing':
            if not bot.channel_id or time.monotonic() < self.retry_at: return True
            if self.attempts >= 10:
                self.phase = 'notice_failed'; return True
            self.attempts += 1
            try:
                # say() deliberately hides uncertain cosmetic sends. Maintenance
                # must distinguish confirmation from deferred/unknown delivery.
                bot._deliver_room_chat(
                    'Maintenance: this lobby will close after the current map finishes '
                    '(or after the next map if idle). Thanks for playing!',
                    automatic=False, lobby_only=True, discord_reply_channel=None)
            except ApiAdmissionDeferred:
                self.retry_at = time.monotonic()+min(15.,2.**min(self.attempts,4))
                return True
            except ApiChatDeliveryUncertain:
                self.phase = 'notice_uncertain'; return True
            except Exception as error:
                # A refused maintenance notice does not authorize terminating
                # this occupied worker. Keep gameplay and report safe metadata.
                from poolrotater_host.service_diagnostics import failure_details
                self.failure = failure_details(error)
                self.phase = 'notice_failed'; return True
            self.notice_at = time.time()
            self.phase = 'waiting_map'
        if self.job['mode'] == 'one-map':
            if bot.state == 'PLAYING': self.active_seen = True
            ready = self.active_seen and bot.state == 'IDLE'
        else:
            ready = bot.state == 'IDLE' and not bot.players
        self.phase = 'ready' if ready else ('waiting_map' if self.job['mode']=='one-map' else 'waiting_empty')
        if ready:
            time.sleep(.05)
            return False
        return True
