"""Execute owner status frame selection helpers in QuickJS, without network or DOM."""
import json
from pathlib import Path
import unittest
import quickjs

ROOT = Path(__file__).resolve().parents[3]

SOURCE = (ROOT / 'website/live/template/script.js').read_text()


def function_source(name, after=0):
    start = SOURCE.index('function ' + name + '(', after)
    opening = SOURCE.index('{', start)
    depth = 0
    for index in range(opening, len(SOURCE)):
        if SOURCE[index] == '{':
            depth += 1
        elif SOURCE[index] == '}':
            depth -= 1
            if depth == 0:
                return SOURCE[start:index + 1]
    raise AssertionError('unclosed JS function ' + name)


OWNER_HELPERS = SOURCE.index('var OWNER_GRACE_SECONDS')
SHAPE = function_source('shape', OWNER_HELPERS)
FRESH = function_source('fresh', OWNER_HELPERS)
RETAINED = function_source('retainedFrame', OWNER_HELPERS)
SELECT = function_source('selectFrame', OWNER_HELPERS)


class OwnerFreshnessTests(unittest.TestCase):
    def context(self, clock=2000):
        context = quickjs.Context()
        context.eval(f'let clock={clock}, offset=0, last=null, lastObservedAt=0; const OWNER_GRACE_SECONDS=180; function now(){{return clock;}}')
        for function in (SHAPE, FRESH, RETAINED, SELECT):
            context.eval(function)
        return context

    def frame(self):
        return {
            'schema': 1, 'available': True, 'generated_at': 1900, 'expires_at': 1960,
            'rooms': [
                {'alias': 'room1', 'room_id': 101, 'state': 'PLAYING', 'players': 7, 'current_map': {'stars': 4}, 'settings': {'active_range': [3, 5]}, 'playback': {'started_at': 1890}, 'queue_known': True, 'queue': [{'beatmap_id': 1}]},
                {'alias': 'room2', 'room_id': None, 'state': 'OFFLINE', 'players': None, 'current_map': None, 'settings': None, 'playback': None, 'queue_known': False, 'queue': []},
                {'alias': 'room3', 'room_id': 103, 'state': 'IDLE', 'players': 0, 'current_map': {}, 'settings': {}, 'playback': None, 'queue_known': True, 'queue': []},
                {'alias': 'room4', 'room_id': 104, 'state': 'IDLE', 'players': 0, 'current_map': {}, 'settings': {}, 'playback': None, 'queue_known': True, 'queue': []},
            ],
        }

    def test_frame_lifecycle_with_reserved_room_slot(self):
        context = self.context(clock=1900)
        frame = self.frame()
        context.eval('var initial=' + json.dumps(frame))
        self.assertEqual(context.eval('selectFrame(initial,false).valid'), True)
        # A fetch failure while the producer frame remains fresh preserves its live certainty unchanged.
        context.eval('clock=1940')
        self.assertEqual(context.eval('selectFrame(null,true).data===last'), True)
        self.assertEqual(context.eval('selectFrame(null,true).data.rooms[0].players'), 7)
        # Once expired, the retained view keeps identity/settings and scrubs live claims.
        context.eval('clock=2000')
        context.eval('var stale=selectFrame(null,true).data')
        self.assertEqual(context.eval('stale._retained'), True)
        self.assertEqual(context.eval('stale.rooms[0].room_id'), 101)
        self.assertEqual(context.eval('stale.rooms[0].settings.active_range[0]'), 3)
        self.assertEqual(context.eval("stale.rooms[0].state==='STALE' && stale.rooms[0].players===null && stale.rooms[0].playback===null && stale.rooms[0].queue_known===false && stale.available===false && stale.service.online===undefined"), True)
        context.eval('clock=2141')
        self.assertEqual(context.eval('selectFrame(null,true).data===null'), True)

    def test_newer_offline_overrides_and_older_frame_is_ignored(self):
        context = self.context()
        frame = self.frame()
        context.eval('var base=' + json.dumps(frame))
        context.eval('clock=1900; selectFrame(base,false)')
        offline = dict(frame, generated_at=1920, expires_at=1980)
        offline['rooms'] = [dict(room) for room in frame['rooms']]
        offline['rooms'][0] = dict(offline['rooms'][0], room_id=None, state='OFFLINE', current_map=None, settings=None)
        context.eval('var offline=' + json.dumps(offline))
        context.eval('clock=1930; selectFrame(offline,false)')
        self.assertEqual(context.eval("last.generated_at===1920 && last.rooms[0].state==='OFFLINE'"), True)
        older = dict(frame, generated_at=1910, expires_at=1970)
        context.eval('var older=' + json.dumps(older))
        self.assertEqual(context.eval('selectFrame(older,false).ignored'), True)
        self.assertEqual(context.eval('last.generated_at'), 1920)

    def test_malformed_frame_is_rejected_and_owner_poll_timeout_is_15s(self):
        context = self.context(clock=1900)
        frame = self.frame()
        context.eval('var good=' + json.dumps(frame) + '; selectFrame(good,false)')
        malformed = []
        value = json.loads(json.dumps(frame)); value['rooms'][3]['alias'] = 'room1'; malformed.append(('duplicate alias', value))
        value = json.loads(json.dumps(frame)); value['rooms'] = [value['rooms'][1], value['rooms'][0], value['rooms'][2], value['rooms'][3]]; malformed.append(('alias reorder', value, True))
        value = json.loads(json.dumps(frame)); value['expires_at'] = value['generated_at']; malformed.append(('zero TTL', value))
        value = json.loads(json.dumps(frame)); value['expires_at'] = value['generated_at'] - 1; malformed.append(('negative TTL', value))
        value = json.loads(json.dumps(frame)); value['generated_at'] = 1931; malformed.append(('future frame', value))
        value = json.loads(json.dumps(frame)); value['rooms'][0].pop('state'); malformed.append(('missing state', value))
        value = json.loads(json.dumps(frame)); value['rooms'][0]['room_id'] = -1; malformed.append(('negative room ID', value))
        value = json.loads(json.dumps(frame)); value['rooms'][0]['current_map'] = []; malformed.append(('wrong map type', value))
        value = json.loads(json.dumps(frame)); value['rooms'][0]['settings'] = 'bad'; malformed.append(('wrong settings type', value))
        for item in malformed:
            label, candidate, *accepted = item
            with self.subTest(frame=label):
                context.eval('var candidate=' + json.dumps(candidate))
                if accepted:
                    self.assertTrue(context.eval('shape(candidate)'))
                else:
                    self.assertFalse(context.eval('shape(candidate)'))
                    self.assertEqual(context.eval('selectFrame(candidate,false).data===last'), True)
                    self.assertEqual(context.eval('last.generated_at'), 1900)
        # Expired cache cannot be resurrected by a response timestamp older than the cached observation.
        old = json.loads(json.dumps(frame))
        newer = json.loads(json.dumps(frame)); newer['generated_at'] = 1950; newer['expires_at'] = 2010
        context.eval('clock=1950; var oldResponse=' + json.dumps(old) + '; var newer=' + json.dumps(newer) + '; selectFrame(newer,false); clock=2191')
        self.assertEqual(context.eval('selectFrame(oldResponse,false).ignored'), True)
        self.assertEqual(context.eval('last.generated_at'), 1950)
        self.assertEqual(context.eval('retainedFrame()===null'), True)
        self.assertIn("timeout=setTimeout(function(){controller.abort();},15000)", SOURCE)
        self.assertIn('update(null,true)', SOURCE)


if __name__ == '__main__':
    unittest.main()
