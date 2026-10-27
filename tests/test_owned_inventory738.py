"""Bounded owned history covers pinned ordering, cursor refusal and real TLS."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy
from dataclasses import replace
import http.client
from urllib.parse import urlencode
import unittest
from unittest.mock import patch

from poolrotater_host.oauth import AuthError
from poolrotater_host.owned_inventory import collect,history_path,history_cursor,is_history_path,HISTORY_PATH,PAGE_SIZE
from poolrotater_host.provider_contract import prepare,exchange_fixture,OWNED_PATH
from poolrotater_host.provider_rooms import _fetch
import test_v7_provider_rooms as rooms


def room(rid, *, pinned=False, active=False):
    return {'id':rid,'user_id':99,'type':'head_to_head','active':active,'pinned':pinned,
            'starts_at':'2026-01-01T00:00:00Z','ends_at':None if active else '2026-01-02T00:00:00Z',
            'name':'private room title','password':'not-for-the-report','participants':[7]}


def paged(values, requested):
    def fetch(path):
        requested.append(path);cursor=history_cursor(path)
        selected=sorted(values,key=lambda r:(int(r['pinned']),r['id']),reverse=True)
        if cursor is not None:
            key=(int(cursor['pinned']),cursor['id'])
            selected=[r for r in selected if (int(r['pinned']),r['id'])<key]
        return 200,copy.deepcopy(selected[:PAGE_SIZE])
    return fetch


class OwnedInventoryContractTests(unittest.TestCase):
    def test_only_canonical_owned_history_queries_are_allowed(self):
        for cursor in (None,{'pinned':True,'id':1},{'pinned':False,'id':9999999999999999999}):
            path=history_path(cursor);self.assertEqual(history_cursor(path),cursor)
            request=prepare('GET',path,bearer='invented')
            self.assertEqual(request.path,path);self.assertIsNone(request.body)
            self.assertEqual(request.maximum_reply,1024**2)
            self.assertEqual(request.headers['x-api-version'],'20220705')
        for path in (HISTORY_PATH+'&is_active=1',HISTORY_PATH+'&sort=ended',HISTORY_PATH+'#x',
                     'https://osu.ppy.sh'+HISTORY_PATH,HISTORY_PATH+'&cursor%5Bpinned%5D=0',
                     HISTORY_PATH+'&cursor%5Bpinned%5D=0&cursor%5Bid%5D=01',
                     HISTORY_PATH+'&cursor%5Bpinned%5D=0&cursor%5Bid%5D=1&cursor%5Bid%5D=2',
                     HISTORY_PATH.replace('limit=50','limit=500')):
            with self.subTest(path=path):
                self.assertFalse(is_history_path(path))
                with self.assertRaises(AuthError):prepare('GET',path,bearer='invented')
        with self.assertRaises(AuthError):prepare('POST',HISTORY_PATH,bearer='invented')
        self.assertEqual(prepare('GET',OWNED_PATH,bearer='invented').path,OWNED_PATH)

    def test_invalid_cursor_values_do_not_create_arbitrary_paths(self):
        for cursor in ({},{'id':1,'pinned':0},{'id':True,'pinned':False},
                       {'id':0,'pinned':False},{'id':10**19,'pinned':False},
                       {'id':1,'pinned':False,'path':'/api/v2/me'}):
            with self.subTest(cursor=cursor),self.assertRaises(AuthError):history_path(cursor)

    def test_mutated_prepared_request_refuses_before_dispatch(self):
        request=prepare('GET',HISTORY_PATH,bearer='invented')
        connection=http.client.HTTPSConnection('127.0.0.1')
        with patch.object(connection,'request') as dispatch:
            for changed in (replace(request,path=HISTORY_PATH+'&is_active=0'),
                            replace(request,headers={**request.headers,'x-api-version':'99999999'}),
                            replace(request,method='DELETE'),replace(request,maximum_reply=1024**3)):
                with self.assertRaises(AuthError):exchange_fixture(connection,changed,timeout=1)
            dispatch.assert_not_called()
        connection.close()


class OwnedInventoryProjectionTests(unittest.TestCase):
    def test_collects_pinned_old_and_unpinned_new_rooms_through_empty_page(self):
        values=[room(1,pinned=True),room(2,pinned=True)]+[room(rid)for rid in range(3,114)]
        before=copy.deepcopy(values);requested=[];report=collect(paged(values,requested),99)
        self.assertTrue(report['history_complete']);self.assertEqual(report['requests'],4)
        self.assertEqual(len(report['rooms']),113)
        self.assertEqual([r['room_id']for r in report['rooms'][:3]],[2,1,113])
        self.assertNotIn('is_active',requested[0]);self.assertEqual(len(requested),4)
        self.assertEqual(report['rooms'][-1]['room_id'],3)
        self.assertFalse(report['absence_recovery_qualified']);self.assertFalse(report['recovery_applied'])
        self.assertFalse(report['start_allowed']);self.assertEqual(values,before)
        self.assertTrue(all(not({'name','password','participants'}&set(r))for r in report['rooms']))

    def test_short_page_still_requires_terminal_empty_page(self):
        requested=[];collect(paged([room(4)],requested),99)
        self.assertEqual(requested,[HISTORY_PATH,history_path({'pinned':False,'id':4})])

    def test_empty_history_is_not_absence_or_start_authority(self):
        report=collect(lambda path:(200,[]),99)
        self.assertTrue(report['history_complete'])
        self.assertFalse(report['absence_recovery_qualified']);self.assertFalse(report['start_allowed'])

    def test_active_and_future_rooms_remain_visible_without_granting_recovery(self):
        future=room(6,active=True);future['starts_at']='2030-01-01T00:00:00Z'
        report=collect(paged([room(5),future],[]),99)
        self.assertTrue(report['rooms'][0]['active']);self.assertFalse(report['rooms'][0]['ended'])
        self.assertFalse(report['absence_recovery_qualified'])

    def test_ignored_cursor_or_duplicate_ids_refuse(self):
        for fetch in (lambda path:(200,[room(5)]),lambda path:(200,[room(5),room(5)])):
            with self.assertRaises(AuthError):collect(fetch,99)

    def test_wrong_sort_foreign_identity_and_malformed_metadata_refuse(self):
        for rows in ([room(1),room(2)], [{**room(1),'user_id':100}],
                     [{**room(1),'id':True}],[{**room(1),'pinned':1}],
                     [{**room(1),'starts_at':None}],[{**room(1),'starts_at':'2026-01-01'}],
                     [{**room(1),'type':'playlists'}],[{**room(1),'active':'false'}]):
            with self.subTest(rows=rows),self.assertRaises(AuthError):collect(lambda p:(200,rows),99)

    def test_page_size_payload_size_time_and_page_cap_refuse_incomplete(self):
        with self.assertRaises(AuthError):collect(lambda path:(200,[room(i)for i in range(1,PAGE_SIZE+2)]),99)
        with patch('poolrotater_host.owned_inventory.MAX_BYTES',1):
            with self.assertRaises(AuthError):collect(lambda p:(200,[]),99)
        with patch('poolrotater_host.owned_inventory.MAX_PAGES',1):
            with self.assertRaises(AuthError) as e:collect(lambda p:(200,[room(5)]),99)
            self.assertEqual(e.exception.code,'ownership_inventory_incomplete')
        for values in ([10,10,400],[10,10,9]):
            iterator=iter(values)
            with self.assertRaises(AuthError):collect(lambda p:(200,[]),99,clock=lambda:next(iterator))

    def test_http_and_invalid_response_failures_retain_safe_diagnostics(self):
        for code in (401,403,429,503):
            with self.subTest(code=code),self.assertRaises(AuthError)as e:collect(lambda p:(code,{}),99)
            self.assertEqual(e.exception.http_status,code)
        for result in (None,(True,[]),(200,{}),(200,[],None)):
            with self.assertRaises(AuthError):collect(lambda p:result,99)


class OwnedInventoryTLSTests(rooms.RoomCase):
    def test_authenticated_tls_history_keeps_ended_rooms_and_cursor_privacy(self):
        auth=self.authorized();self.provider.history_rooms=[room(1,pinned=True)]+[room(i)for i in range(2,56)]
        with auth.verified_owner():
            report=collect(lambda path:_fetch(auth,path),99)
        self.assertEqual(len(report['rooms']),55);self.assertEqual(report['requests'],3)
        calls=[r for r in self.provider.calls if is_history_path(r['path'])]
        self.assertEqual(len(calls),3)
        self.assertTrue(all(r['method']=='GET'and r['headers'].get('x-api-version')=='20220705'for r in calls))
        self.assertEqual(self.provider.history_rooms[0]['password'],'not-for-the-report')
        self.assertFalse(any('password'in r or 'name'in r for r in report['rooms']))
        self.assertFalse(report['absence_recovery_qualified'])

    def test_authenticated_history_refuses_incomplete_response_after_first_page(self):
        auth=self.authorized();self.provider.history_rooms=[room(i)for i in range(1,61)]
        def change(path):
            if is_history_path(path)and history_cursor(path)is not None:self.provider.history_rooms=None
        self.provider.on_read=change
        with auth.verified_owner(),self.assertRaises(AuthError)as e:collect(lambda path:_fetch(auth,path),99)
        self.assertEqual(e.exception.code,'ownership_inventory_unavailable')
        self.assertEqual(e.exception.http_status,503)
