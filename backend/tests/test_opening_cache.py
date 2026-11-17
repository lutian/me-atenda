"""Cancellation, accounting and cached media through public REST/WebSocket interfaces."""
import asyncio
import base64
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import httpx
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from app.main import create_app
from app.providers import Providers
from app.voice import NOTICE
from test_stream_diagnostics import sign
import test_opening_postgres as opening_tests
import test_postgres as pg


@unittest.skipUnless(pg.os.environ.get('TEST_DATABASE_URL'), 'disposable PostgreSQL required')
class Cache(unittest.TestCase):
    setUp = pg.PostgreSQL.setUp
    cleanup_schema = pg.PostgreSQL.cleanup_schema
    post = pg.PostgreSQL.post
    callback = pg.PostgreSQL.callback
    track = opening_tests.Openings.track

    def test_timeout_and_http_cancellation_keep_guard_until_owner_stops(self):
        for cancel in (True, False):
            with self.subTest(cancel=cancel):
                entered = threading.Event(); release = threading.Event(); closed = threading.Event()
                spoken = []
                async def speak(text):
                    spoken.append(text); entered.set()
                    await asyncio.to_thread(release.wait, 2)
                    return b'\xff'*160
                self.provider.speak = speak
                original = self.store.preparation_guard
                def guard():
                    owner = original()
                    def close():
                        owner.close(); closed.set()
                    return NS(close=close)
                async def scenario():
                    app = create_app(self.settings, self.store, self.provider)
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                            base_url='http://test', headers=self.headers) as client:
                        task = asyncio.create_task(client.post('/calls', json={'lead_id':1,'product_id':1}))
                        try:
                            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                            if cancel:
                                task.cancel()
                                with self.assertRaises(asyncio.CancelledError): await task
                            else: self.assertEqual((await task).status_code, 503)
                            r = await client.post('/calls', json={'lead_id':1,'product_id':1})
                            self.assertEqual(r.status_code, 409)
                            self.assertFalse(closed.is_set())
                            self.assertEqual((await client.get('/calls')).json(), [])
                            self.assertEqual(self.provider.dials, [])
                        finally: release.set()
                        self.assertTrue(await asyncio.to_thread(closed.wait, 1))
                        self.assertEqual((await client.get('/calls')).json(), [])
                # Separate voices prevent reuse between the two scenarios.
                self.settings.tts_voice = str(cancel)
                # Cancellation must not race the separate artificial 50ms timeout scenario.
                with patch.object(self.store, 'preparation_guard', guard), patch(
                        'app.openings.PREP_SECONDS', 20 if cancel else .05):
                    asyncio.run(scenario())
                self.assertEqual(spoken, ['Alô?'])

    def test_instrumented_preparation_global_once_and_replay_has_no_charge(self):
        self.settings.observability_pricing_json = ('{"version":"v1","currency":"USD",'
            '"models":{"tts-1":{"characters_per_million":"10"}}}')
        provider = Providers(self.settings)
        provider.embed = self.provider.embed; provider.dial = self.provider.dial
        provider.hangup = self.provider.hangup
        speech = AsyncMock(return_value=NS(content=b'\x00\x00'*963))
        provider._openai = NS(audio=NS(speech=NS(create=speech)))
        self.client = TestClient(create_app(self.settings, self.store, provider))
        self.assertEqual(self.post('/calls', {'lead_id':1,'product_id':1}).status_code, 201)
        expected = NOTICE.format(product_name='Produto')
        self.client.patch('/products/1', json={'name':'After reservation'}, headers=self.headers)
        stream = 'MZ'+'5'*32
        self.callback('/twilio/voice/1', {'CallSid':'CA'+'1'*32,'AnsweredBy':'human'})
        with self.client.websocket_connect('/twilio/stream/1', headers={
                'X-Twilio-Signature':sign(self.settings.public_base+'/twilio/stream/1')}) as ws:
            ws.send_json({'event':'start','streamSid':stream,'start':{'streamSid':stream,
                'callSid':'CA'+'1'*32,'mediaFormat':{'encoding':'audio/x-mulaw','sampleRate':8000,'channels':1}}})
            for _ in range(3):
                frame = ws.receive_json()
                self.assertEqual(frame, {'event':'media','streamSid':stream,
                    'media':{'payload':base64.b64encode(b'\xff'*160).decode()}})
            ws.send_json({'event':'stop','streamSid':stream,'sequenceNumber':'1','stop':{'callSid':'CA'+'1'*32}})
            with self.assertRaises(WebSocketDisconnect) as closed: ws.receive_json()
            self.assertEqual(closed.exception.code, 1000)
        self.assertEqual(speech.await_count, 2)
        call = self.client.get('/calls/1', headers=self.headers).json()
        self.assertEqual(call['transcription_json'], [{'role':'assistant','content':expected}])
        events = self.client.get('/observability/events', headers=self.headers).json()
        self.assertEqual([(e['operation'],e['call_id'],e['status']) for e in events],
                         [('speak',None,'success')]*2)
        self.assertTrue(all(e['estimated_cost_usd'] is not None for e in events))
        self.assertEqual(self.client.get('/observability/summary?call_id=1', headers=self.headers).json()['request_count'], 0)
        self.callback('/twilio/status/1', {'CallSid':'CA'+'1'*32,'CallStatus':'completed'})
        self.client.patch('/products/1', json={'name':'Produto'}, headers=self.headers)
        self.post('/leads', {'name':'B','phone':'+15551234567'})
        async def next_dial(call, phone): return 'CA'+'2'*32
        provider.dial = next_dial
        self.client = TestClient(create_app(self.settings, type(self.store)(self.store.url), provider))
        self.assertEqual(self.post('/calls', {'lead_id':2,'product_id':1}).status_code, 201)
        self.assertEqual(speech.await_count, 2)
        events = self.client.get('/observability/events', headers=self.headers).json()
        self.assertEqual(sum(e['operation']=='speak' for e in events), 2)

    def test_missing_pin_fails_safe_without_live_synthesis(self):
        self.track()
        self.assertEqual(self.post('/calls', {'lead_id':1,'product_id':1}).status_code, 201)
        self.callback('/twilio/voice/1', {'CallSid':'CA'+'1'*32,'AnsweredBy':'human'})
        with patch.object(self.store, 'opening_intro', side_effect=ValueError('missing')):
            with self.client.websocket_connect('/twilio/stream/1', headers={
                    'X-Twilio-Signature':sign(self.settings.public_base+'/twilio/stream/1')}) as ws:
                stream = 'MZ'+'5'*32
                ws.send_json({'event':'start','streamSid':stream,'start':{'streamSid':stream,
                    'callSid':'CA'+'1'*32,'mediaFormat':{'encoding':'audio/x-mulaw','sampleRate':8000,'channels':1}}})
                with self.assertRaises(WebSocketDisconnect) as closed: ws.receive_json()
                self.assertEqual(closed.exception.code, 1011)
        self.assertEqual(len(self.spoken), 2)
        call = self.client.get('/calls/1', headers=self.headers).json()
        self.assertEqual((call['transcription_json'],call['status_final']), ([], 'falha_tecnica'))
        self.assertEqual(self.post('/calls', {'lead_id':1,'product_id':1}).status_code, 409)

    def test_accounting_start_or_finalize_failure_does_not_dial_or_retry(self):
        for method in ('event_start','event_finish'):
            with self.subTest(method=method):
                self.settings.tts_voice = method
                provider = Providers(self.settings)
                speech = AsyncMock(return_value=NS(content=b'\x00\x00'*480))
                provider._openai = NS(audio=NS(speech=NS(create=speech)))
                provider.dial = self.provider.dial
                self.client = TestClient(create_app(self.settings, self.store, provider))
                with patch.object(self.store, method, side_effect=OSError('private')):
                    r = self.post('/calls', {'lead_id':1,'product_id':1})
                self.assertEqual(r.status_code, 503)
                self.assertEqual(self.provider.dials, [])
                self.assertEqual(self.client.get('/calls', headers=self.headers).json(), [])
                count = speech.await_count
                self.assertEqual(self.post('/calls', {'lead_id':1,'product_id':1}).status_code, 503)
                self.assertEqual(speech.await_count, count)

    def test_model_voice_and_format_are_cache_inputs(self):
        self.track()
        for index, (model, voice, format) in enumerate([
                ('tts-1','alloy','v1'), ('other','alloy','v1'),
                ('other','nova','v1'), ('other','nova','v2')], 1):
            self.settings.tts_model = model; self.settings.tts_voice = voice
            with patch('app.openings.FORMAT', format):
                self.assertEqual(self.post('/calls', {'lead_id':1,'product_id':1}).status_code, 201)
            self.store.finish(index, 'inconclusivo')
            async def dial(call, phone): return 'CA'+format_id(call['id'])
            self.provider.dial = dial
        self.assertEqual(len(self.spoken), 8)


def format_id(id): return f'{id:032x}'
