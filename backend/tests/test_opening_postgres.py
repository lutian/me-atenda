"""Public REST proof against isolated disposable PostgreSQL; external speech is fake."""
import asyncio
import concurrent.futures
import threading
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.main import create_app
import test_postgres as pg


@unittest.skipUnless(pg.os.environ.get('TEST_DATABASE_URL'), 'disposable PostgreSQL required')
class Openings(unittest.TestCase):
    setUp = pg.PostgreSQL.setUp
    cleanup_schema = pg.PostgreSQL.cleanup_schema
    post = pg.PostgreSQL.post
    callback = pg.PostgreSQL.callback

    def track(self):
        self.spoken = []
        async def speak(text):
            self.spoken.append(text)
            return b'\xff' * 320
        self.provider.speak = speak

    def finish(self):
        rows = self.client.get('/calls', headers=self.headers).json()
        id = rows[-1]['id']
        self.assertEqual(self.callback(f'/twilio/status/{id}', {'CallSid':'CA'+'1'*32,
            'CallStatus':'completed'}).status_code, 200)
        async def dial(call, phone):
            self.provider.dials.append((call, phone))
            return 'CA'+format(call['id'], '032x')
        self.provider.dial = dial

    def test_reuse_restart_exact_inputs_and_hidden_bytes(self):
        self.track()
        self.assertEqual(self.post('/calls', {'lead_id':1,'product_id':1}).status_code, 201)
        self.assertEqual(self.spoken[0], 'Alô?')
        self.assertIn('Representante de Produto,', self.spoken[1])
        self.finish()
        self.post('/leads', {'name':'B','phone':'+15551234567'})
        self.client.patch('/products/1', json={'description':'changed'}, headers=self.headers)
        self.client = TestClient(create_app(self.settings, type(self.store)(self.store.url), self.provider))
        self.assertEqual(self.post('/calls', {'lead_id':2,'product_id':1}).status_code, 201)
        self.assertEqual(len(self.spoken), 2)
        self.assertNotIn('opening_audio', self.client.get('/calls', headers=self.headers).text)
        self.assertNotIn('digest', self.client.get('/products', headers=self.headers).text)
        self.store.finish(2, 'inconclusivo')
        self.client.patch('/products/1', json={'name':'Other'}, headers=self.headers)
        self.assertEqual(self.post('/calls', {'lead_id':1,'product_id':1}).status_code, 201)
        self.assertEqual(len(self.spoken), 3)
        self.store.finish(3, 'inconclusivo')
        self.client.patch('/products/1', json={'name':'Produto'}, headers=self.headers)
        self.assertEqual(self.post('/calls', {'lead_id':1,'product_id':1}).status_code, 201)
        self.assertEqual(len(self.spoken), 3)

    def test_ineligible_requests_never_synthesize(self):
        self.track()
        for lead, product in ((99,1), (1,99)):
            r = self.post('/calls', {'lead_id':lead,'product_id':product})
            self.assertEqual((r.status_code,r.json()), (404, {'detail':'Not found'}))
        self.assertEqual(self.client.get('/calls', headers=self.headers).json(), [])
        self.assertEqual(self.spoken, [])
        self.store.reserve(1,1)
        self.assertEqual(self.post('/calls', {'lead_id':1,'product_id':1}).status_code, 409)
        self.assertEqual(self.spoken, [])
        self.assertEqual(self.provider.dials, [])

    def test_two_apps_cold_exclusion_and_no_dial_before_ready(self):
        entered = threading.Event(); release = threading.Event(); spoken = []
        async def speak(text):
            spoken.append(text); entered.set()
            if not await asyncio.to_thread(release.wait, 3): raise TimeoutError()
            self.assertEqual(self.provider.dials, [])
            return b'\xff'*320
        self.provider.speak = speak
        other = TestClient(create_app(self.settings, type(self.store)(self.store.url), self.provider))
        with concurrent.futures.ThreadPoolExecutor() as pool:
            first = pool.submit(self.post, '/calls', {'lead_id':1,'product_id':1})
            try:
                self.assertTrue(entered.wait(2))
                r = other.post('/calls', json={'lead_id':1,'product_id':1}, headers=self.headers)
                self.assertEqual((r.status_code, r.json()), (409, {'detail':'Speech preparation busy'}))
                self.assertEqual(self.client.get('/calls', headers=self.headers).json(), [])
            finally: release.set()
            self.assertEqual(first.result(3).status_code, 201)
        self.assertEqual(len(spoken), 2)
        self.assertEqual(len(self.provider.dials), 1)

    def test_edit_or_delete_during_preparation_never_dials(self):
        for action in ('edit', 'delete'):
            with self.subTest(action=action):
                entered = threading.Event(); release = threading.Event()
                async def speak(text):
                    entered.set()
                    await asyncio.to_thread(release.wait, 3)
                    return b'\xff'*160
                self.provider.speak = speak
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(self.post, '/calls', {'lead_id':1,'product_id':1})
                    try:
                        self.assertTrue(entered.wait(2))
                        if action == 'edit':
                            self.client.patch('/products/1', json={'name':'Changed'}, headers=self.headers)
                        else: self.client.delete('/products/1', headers=self.headers)
                    finally: release.set()
                    self.assertIn(future.result(3).status_code, (404,409))
                self.assertEqual(self.provider.dials, [])
                self.assertEqual(self.client.get('/calls', headers=self.headers).json(), [])

    def test_invalid_audio_cache_and_ledger_fail_closed(self):
        for failure in ('empty', 'large', 'provider', 'cache', 'ledger'):
            with self.subTest(failure=failure):
                self.track()
                # Different voice gives each attempt an independent identity.
                self.settings.tts_voice = failure
                async def speak(text):
                    self.spoken.append(text)
                    if failure == 'provider': raise OSError('private detail')
                    return b'' if failure == 'empty' else b'x'*720001 if failure == 'large' else b'x'*160
                self.provider.speak = speak
                method = 'opening_save' if failure == 'cache' else 'account_call'
                context = patch.object(self.store, method, side_effect=OSError('private detail'))
                with context if failure in ('cache','ledger') else patch.object(self.provider, 'speak', speak):
                    r = self.post('/calls', {'lead_id':1,'product_id':1})
                self.assertEqual(r.status_code, 503)
                self.assertNotIn('private', r.text)
                self.assertEqual(self.provider.dials, [])
                self.assertFalse(any(row['active'] or row['sid'] for row in self.store.list('calls')))
                count = len(self.spoken)
                if failure != 'ledger':
                    self.assertEqual(self.post('/calls', {'lead_id':1,'product_id':1}).status_code, 503)
                    self.assertEqual(len(self.spoken), count)

    def test_migration_reentrant_preserves_history_recording_and_slot(self):
        self.store.reserve(1,1)
        self.store.append(1, 'user', 'history')
        self.store.recording(1, 'RE'+'4'*32, b'RIFFexisting')
        before = self.client.get('/calls', headers=self.headers).json()
        self.store.migrate(); self.store.migrate()
        self.assertEqual(self.client.get('/calls', headers=self.headers).json(), before)
        self.assertEqual(self.client.get('/calls/1/audio', headers=self.headers).content, b'RIFFexisting')
        self.assertEqual(self.post('/calls', {'lead_id':1,'product_id':1}).status_code, 409)
        self.assertEqual(self.provider.dials, [])
