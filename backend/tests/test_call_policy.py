"""Public policy/dispatch contracts with fake external providers only."""
import asyncio
import concurrent.futures
import threading
import unittest
from urllib.parse import parse_qs
import httpx
import test_api as fixtures
import test_adapters as adapters


class Policy(unittest.TestCase):
    setUp = fixtures.API.setUp
    post = fixtures.API.post
    seed = fixtures.API.seed
    callback = fixtures.API.callback

    def policy(self):
        result = self.client.get('/settings/calls', headers=self.headers)
        self.assertEqual(result.status_code, 200)
        return result.json()

    def save(self, value):
        return self.client.patch('/settings/calls', json=value, headers=self.headers)

    def test_default_saved_bounds_and_rejected_updates_are_nonmutating(self):
        self.assertEqual(self.policy(), {'max_duration_seconds':180})
        for value in (1, 90, 14400, 180):
            result = self.save({'max_duration_seconds':value})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json(), {'max_duration_seconds':value})
            self.assertEqual(self.policy(), result.json())
        before = self.client.get('/calls', headers=self.headers).json()
        for value in (0, -1, 14401, None, True, 1.5, '180'):
            self.assertEqual(self.save({'max_duration_seconds':value}).status_code, 422)
        for value in ({}, {'max_duration_seconds':180,'other':1}):
            self.assertEqual(self.save(value).status_code, 422)
        self.assertEqual(self.policy(), {'max_duration_seconds':180})
        self.assertEqual(self.client.get('/calls',headers=self.headers).json(),before)
        self.assertEqual(self.provider.dials, [])

    def test_policy_requires_admin_and_cannot_disable_limit(self):
        for method in ('get','patch'):
            response = getattr(self.client,method)('/settings/calls', **(
                {'json':{'max_duration_seconds':1}} if method=='patch' else {}))
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.policy(), {'max_duration_seconds':180})
        self.assertEqual(self.provider.dials, [])

    def test_reserved_limit_does_not_change_when_policy_changes(self):
        entered, release = threading.Event(), threading.Event()
        original = self.provider.dial
        async def blocked(call, phone):
            entered.set()
            if not await asyncio.to_thread(release.wait, 4): raise AssertionError('dial not released')
            return await original(call,phone)
        self.provider.dial = blocked
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(self.seed)
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.save({'max_duration_seconds':60}).status_code,200)
            finally: release.set()
            self.assertEqual(result.result(timeout=5).status_code,201)
        self.assertEqual(self.provider.dials[0][0]['max_duration_seconds'],180)
        self.assertEqual(self.policy(), {'max_duration_seconds':60})
        sid = self.provider.dials[0][0].get('sid') or 'CA'+'1'*32
        self.assertEqual(self.callback('/twilio/status/1',{
            'CallSid':sid,'CallStatus':'completed','CallDuration':'1'}).status_code,200)
        async def next_dial(call,phone):
            self.provider.dials.append((call,phone)); return 'CA'+'3'*32
        self.provider.dial=next_dial
        self.assertEqual(self.post('/calls',{'lead_id':1,'product_id':1}).status_code,201)
        self.assertEqual(self.provider.dials[-1][0]['max_duration_seconds'],60)

    def test_policy_changed_during_preparation_applies_at_reservation(self):
        entered,release=threading.Event(),threading.Event()
        async def speak(text):
            entered.set()
            if not await asyncio.to_thread(release.wait,4): raise AssertionError('speech not released')
            return b'\xff'*160
        self.provider.speak=speak
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            result=pool.submit(self.seed)
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.save({'max_duration_seconds':90}).status_code,200)
            finally: release.set()
            self.assertEqual(result.result(timeout=5).status_code,201)
        self.assertEqual(len(self.provider.dials),1)
        self.assertEqual(self.provider.dials[0][0]['max_duration_seconds'],90)


class Carrier(unittest.IsolatedAsyncioTestCase):
    async def test_wire_uses_pinned_duration_and_preserves_existing_fields(self):
        seen=[]
        def transport(request):
            seen.append(parse_qs(request.content.decode()))
            return httpx.Response(201,json={'sid':'CA'+'1'*32})
        provider=adapters.Adapters.provider(self,transport)
        for limit in (180,90):
            await provider.dial({'id':7,'max_duration_seconds':limit},'synthetic')
            self.assertEqual(seen[-1]['TimeLimit'],[str(limit)])
            self.assertEqual(seen[-1]['Timeout'],['30'])
            self.assertEqual(seen[-1]['AsyncAmd'],['true'])
            self.assertEqual(seen[-1]['Record'],['true'])
        for value in (None,0,14401,True,'180'):
            with self.assertRaises(ValueError):
                await provider.dial({'id':7,'max_duration_seconds':value},'synthetic')
        self.assertEqual(len(seen),2)
