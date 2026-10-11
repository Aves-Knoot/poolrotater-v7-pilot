"""Long-running synthetic service for offline lifecycle qualification only."""
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import threading
import math

from ..config import ConfigError
from ..layout import atomic_json
from .installed_authorization import authority as installed_authority, authorize
from .runtime import RuntimeApi, RuntimeHub


@contextmanager
def authority(root, release):
    if os.environ.get('POOLROTATER_PROVISIONING_SANDBOX') != '1':
        raise ConfigError('service fixture required')
    with installed_authority(root, release) as (auth, provider):
        # Only a pristine invented fixture is auto-authorized. Restart never
        # replaces/cancels/replays incomplete authority or resets room ownership.
        if not any(path.exists() for path in (auth.token_path, auth.pending_path,
                                             auth.candidate_path, auth.account_path)):
            authorize(auth, provider)
        yield auth


def probe_candidate(root, release, candidate):
    from offline_host_controller import host_controller
    hub = RuntimeHub(); api = RuntimeApi(hub)
    with authority(root, release) as auth, host_controller(root, release, api=api, hub=hub, authentication=auth) as supervisor:
        try: supervisor.prepare(next(iter(supervisor.configs)), candidate)
        except ConfigError:
            if supervisor.handles or hub.calls: raise ConfigError('candidate executed before refusal')
            return {'candidate_refused_before_exec': True, 'workers_started': 0, 'live_ready': False}
        raise ConfigError('unregistered candidate unexpectedly executed')


def calculator_check(root):
    if os.environ.get('POOLROTATER_CALCULATOR_QUALIFICATION') != '1':
        raise ConfigError('dedicated native calculator fixture required')
    from osu_tools import OsuCalculator
    path = Path(root) / 'downloads/invented-native.osu'
    header = '''osu file format v14

[General]
AudioFilename: invented.wav
Mode: 0

[Metadata]
Title: Invented fixture
Artist: Invented
Creator: Offline fixture
Version: Synthetic
BeatmapID: 0
BeatmapSetID: -1

[Difficulty]
HPDrainRate:5
CircleSize:4
OverallDifficulty:5
ApproachRate:5
SliderMultiplier:1.4
SliderTickRate:1

[TimingPoints]
0,500,4,2,1,100,1,0

[HitObjects]
'''
    path.write_text(header + ''.join(f'{64+(i%4)*100},{64+(i%3)*100},{1000+i*200},1,0,0:0:0:0:\n'
                                   for i in range(120)))
    path.chmod(0o600)
    result = OsuCalculator(prepared_cache_size=1, max_workers=1).calculate(str(path), mode=0, acc=100)
    if not result.is_success or not all(math.isfinite(float(v)) and float(v) > 0 for v in (result.pp, result.stars)):
        raise ConfigError('native fixture calculation failed')
    return {'native_calculator': True, 'uid': os.geteuid(), 'pp': result.pp, 'stars': result.stars,
            'objects': 120, 'invented_map': True, 'live_ready': False}


def run(root, release):
    from offline_host_controller import host_controller
    root = Path(root)
    stop = threading.Event()
    previous = {number: signal.signal(number, lambda *_: stop.set()) for number in (signal.SIGTERM, signal.SIGINT)}
    status_path = root / 'state/service-fixture.json'
    hub = RuntimeHub(); api = RuntimeApi(hub)
    workers = []
    acquired = False
    try:
        with authority(root, release) as auth, host_controller(root, release, api=api, hub=hub, authentication=auth) as supervisor:
            acquired = True
            for alias in supervisor.configs:
                if stop.is_set(): break
                worker = supervisor.prepare(alias, release)
                supervisor.activate(worker); workers.append(worker)
            if not stop.is_set():
                status = {'format': 1, 'execution': 'offline-fixture', 'live_ready': False,
                          'phase': 'running', 'pid': os.getpid(), 'uid': os.geteuid(),
                          'version': supervisor.manifest['version'], 'worker_pids': [w.process.pid for w in workers],
                          'room_count': len(hub.rooms), 'verified_account': 99}
                atomic_json(status_path, status)
            while not stop.wait(.1):
                if any(not w.fresh() for w in workers):
                    raise ConfigError('service fixture worker health failed')
        if hub.rooms or any(w.process.poll() is None for w in workers):
            raise ConfigError('service cleanup incomplete')
        atomic_json(status_path, {'format': 1, 'execution': 'offline-fixture', 'live_ready': False,
                                 'phase': 'stopped', 'pid': os.getpid(), 'workers_reaped': True,
                                 'closed_rooms': len(workers)})
    except Exception:
        if acquired:
            atomic_json(status_path, {'format': 1, 'execution': 'offline-fixture', 'live_ready': False,
                                     'phase': 'recovery_required', 'pid': os.getpid()})
        raise
    finally:
        for number, handler in previous.items(): signal.signal(number, handler)
