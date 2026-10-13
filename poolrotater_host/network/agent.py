"""Independent heartbeat scheduling; no provider calls or local-room control."""
import random
import time

from ..config import ConfigError
from ..layout import atomic_json, contained, file_lease
from .contracts import NetworkError


class Agent:
    def __init__(self, client, observation, *, clock=time.time, jitter=random.uniform):
        self.client=client; self.observation=observation; self.clock=clock; self.jitter=jitter
        self.failures=0
        self.path=contained(client.root,'state/network-agent.json')

    def step(self):
        """One bounded call. Backoff persisted as diagnostics, no secrets or exception text."""
        now=self.clock();phase='idle';error=None;delay=60
        try:
            status=self.client.status()
            if not status['configured']:phase='not-configured';delay=300
            elif status.get('pending_operation'):
                # Enrollment/rotation require explicit operator recovery; leaving
                # retries while sharing is already suppressed locally.
                if status['pending_operation']=='leave':self.client.resume();phase='left'
                else:phase='operator-recovery-required';delay=300
            elif not status['enrolled']:phase='not-enrolled';delay=300
            elif status.get('account_binding_matches') is False:phase='local-review-required';error='local-state-refused';delay=300
            elif status['credential_expires_at']<=now:phase='reenrollment-required';delay=300
            else:self.client.publish(self.observation());phase='connected'
            self.failures=0
        except NetworkError as exc:
            self.failures=min(self.failures+1,8)
            phase='reenrollment-required' if exc.code in ('host_revoked','credential_expired','credential_retired','credential_refused') else 'retrying'
            # Fixed locally known code, never echo remote/transport exception text.
            error='authority-refused' if phase=='reenrollment-required' else 'hub-unavailable'
            delay=300 if phase=='reenrollment-required' else min(300,5*2**(self.failures-1))
            if exc.code in ('network_account_mismatch','foreign_network_installation','foreign_network_heartbeat',
                            'network_profile_changed','network_trust_changed','invalid_network_profile',
                            'invalid_network_credential','invalid_network_pending'):
                phase='local-review-required';error='local-state-refused';delay=300
        except (ConfigError,OSError,ValueError,KeyError,TypeError):
            phase='local-review-required';error='local-state-refused';delay=300
        delay=max(5,min(330,delay+self.jitter(0,min(30,delay*.1))))
        receipt={'format':1,'phase':phase,'error':error,'attempted_at':now,'retry_seconds':round(delay,2),
                 'live_rooms_affected':False}
        atomic_json(self.path,receipt)
        return receipt

    def run(self,stop):
        # Own lease prevents duplicate agents without taking supervisor/provider locks.
        with file_lease(contained(self.client.root,'state/network-agent.lock')):
            while not stop.is_set():
                result=self.step()
                stop.wait(result['retry_seconds'])
