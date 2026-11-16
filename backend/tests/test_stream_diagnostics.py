"""S22 offline authentication and trusted terminal diagnostics; no providers."""
import base64
import hashlib
import hmac
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch
from starlette.websockets import WebSocketDisconnect
from app.main import create_app
from app.store import Store
from app.observability import summarize
from fastapi.testclient import TestClient
import test_api as fixtures


def sign(material, token=b'test-token'):
    return base64.b64encode(hmac.new(token, material.encode(), hashlib.sha1).digest()).decode()


class Handshake(unittest.TestCase):
    def setUp(self):
        fixtures.API.setUp(self)
        self.store = TransactionStore()
        self.client = TestClient(create_app(self.settings, self.store, self.provider))

    seed = fixtures.API.seed
    post = fixtures.API.post
    callback = fixtures.API.callback

    def test_exact_and_documented_slash_signatures(self):
        self.seed()
        for material in ('https://voice.example/prefix/twilio/stream/1?b=%2F&a=1',
                         'https://voice.example/prefix/twilio/stream/1/?b=%2F&a=1',
                         'wss://voice.example/prefix/twilio/stream/1?b=%2F&a=1',
                         'wss://voice.example/prefix/twilio/stream/1/?b=%2F&a=1'):
            with self.subTest(material=material), patch('app.main.session') as session:
                async def close(ws, *args): await ws.close(code=1000)
                session.side_effect = close
                with self.client.websocket_connect('/twilio/stream/1?b=%2F&a=1',
                        headers={'X-Twilio-Signature': sign(material)}) as ws:
                    with self.assertRaises(WebSocketDisconnect) as closed: ws.receive_json()
                    self.assertEqual(closed.exception.code, 1000)
                session.assert_called_once()

    def test_rejections_leave_all_state_unchanged_and_logs_bounded(self):
        self.seed()
        routes = ['/calls', '/leads', '/products', '/observability/events', '/observability/summary']
        def snapshot(): return [self.client.get(route, headers=self.headers).json() for route in routes]
        before = snapshot()
        materials = [None, 'bad',
            sign('https://evil.example/prefix/twilio/stream/1?b=%2F&a=1'),
            sign('https://voice.example/other/twilio/stream/1?b=%2F&a=1'),
            sign('https://voice.example/prefix/twilio/stream/2?b=%2F&a=1'),
            sign('https://voice.example/prefix/twilio/stream/1?a=1&b=%2F'),
            sign('https://voice.example/prefix/twilio/stream/1?b=/&a=1'),
            sign('wss://evil.example/prefix/twilio/stream/1?b=%2F&a=1'),
            sign('wss://voice.example:8443/prefix/twilio/stream/1?b=%2F&a=1'),
            sign('wss://voice.example/other/twilio/stream/1?b=%2F&a=1'),
            sign('wss://voice.example/prefix/twilio/stream/2?b=%2F&a=1'),
            sign('wss://voice.example/prefix/twilio/stream/1?a=1&b=%2F'),
            sign('wss://voice.example/prefix/twilio/stream/1?b=/&a=1'),
            sign('ws://voice.example/prefix/twilio/stream/1?b=%2F&a=1'),
            sign('http://voice.example/prefix/twilio/stream/1?b=%2F&a=1'),
            sign('ftp://voice.example/prefix/twilio/stream/1?b=%2F&a=1'),
            sign('wss://voice.example/prefix/twilio/stream/1?b=%2F&a=1', b'wrong'),
            sign('https://voice.example/prefix/twilio/stream/1?b=%2F&a=1', b'wrong')]
        with patch('app.main.time.monotonic', return_value=100), self.assertLogs('app.main', 'WARNING') as logs:
            for signature in materials * 2:
                headers = {'Host': 'evil.example', 'X-Forwarded-Host': 'evil.example'}
                if signature is not None: headers['X-Twilio-Signature'] = signature
                with self.assertRaises(WebSocketDisconnect) as closed:
                    with self.client.websocket_connect('/twilio/stream/1?b=%2F&a=1', headers=headers): pass
                self.assertEqual(closed.exception.code, 1008)
                self.assertEqual(snapshot(), before)
        self.assertEqual(logs.output, ['WARNING:app.main:media_stream_signature_rejected'])


class TransactionStore(fixtures.MemoryStore, Store):
    """SQL transition double only; real locking proof lives in test_postgres."""
    def __init__(self): fixtures.MemoryStore.__init__(self)
    @contextmanager
    def connection(self): yield self
    def locked(self, db, id): return self.get('calls', id)
    def execute(self, sql, args):
        self.result = None
        if sql.startswith('UPDATE calls SET active=FALSE'):
            status, duration, id = args
            self.calls[id].update(active=False, status_final=status, duration_seconds=duration)
            self.result = dict(self.calls[id])
        elif sql.startswith('UPDATE calls SET duration_seconds'):
            duration, id = args
            self.calls[id]['duration_seconds'] = duration
        elif sql.startswith('UPDATE leads SET status='):
            status, id = args
            self.leads[id]['status'] = status
        elif sql.startswith('INSERT INTO integration_events'):
            import json
            id, payload, key = args
            if not any(e.get('stable_key') == key for e in self.events):
                self.events.append(dict(json.loads(payload), id=len(self.events)+1, stable_key=key))
                self.result = {'id': len(self.events)}
        else: raise AssertionError(sql)
        return self
    def fetchone(self): return self.result
    def connectivity_locked(self, db, row): pass
    finish = Store.finish
    def observability_events(self, call_id=None, limit=100):
        return [e for e in reversed(self.events) if call_id is None or e['call_id'] == call_id][:limit]
    def observability_summary(self, call_id=None): return summarize(self.observability_events(call_id), call_id)


class Diagnostics(unittest.TestCase):
    seed = fixtures.API.seed
    post = fixtures.API.post
    callback = fixtures.API.callback

    def setUp(self):
        fixtures.API.setUp(self)
        self.store = TransactionStore()
        self.client = TestClient(create_app(self.settings, self.store, self.provider))

    def test_terminal_human_no_stream_visible_once_and_no_speech_or_charges(self):
        self.seed()
        self.callback('/twilio/voice/1', {'CallSid': 'CA'+'1'*32, 'AnsweredBy': 'human'})
        payload = {'CallSid': 'CA'+'1'*32, 'CallStatus': 'completed', 'CallDuration': '6'}
        self.provider.speak = AsyncMock(wraps=self.provider.speak)
        def snapshot():
            return [self.client.get(route, headers=self.headers).json() for route in
                    ('/calls', '/leads', '/observability/events', '/observability/summary')]
        before = snapshot()
        self.assertEqual(self.callback('/twilio/status/1', payload, False).status_code, 403)
        self.assertEqual(snapshot(), before)
        wrong = dict(payload, CallSid='CA'+'9'*32)
        self.assertEqual(self.callback('/twilio/status/1', wrong).status_code, 409)
        self.assertEqual(snapshot(), before)
        self.assertEqual(self.callback('/twilio/status/1', dict(payload, CallStatus='ringing')).status_code, 200)
        self.assertEqual(snapshot(), before)
        for _ in range(2):
            response = self.callback('/twilio/status/1', payload)
            self.assertEqual((response.status_code, response.json()), (200, {'ok': True}))
        call = self.client.get('/calls/1', headers=self.headers).json()
        self.assertEqual((call['status_final'], call['duration_seconds'], call['transcription_json']),
                         ('falha_tecnica', 6, []))
        for suffix in ('', '?call_id=1'):
            events = self.client.get('/observability/events'+suffix, headers=self.headers).json()
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual((event['operation'], event['provider'], event['status'], event['error_code']),
                ('media_stream', 'twilio', 'error', 'media_stream_not_established'))
            self.assertEqual(event['error_message'], 'Human call ended without an established media stream')
            self.assertEqual(event['cost_status'], 'not_applicable')
            for key in ('total_tokens', 'input_tokens', 'output_tokens', 'characters', 'estimated_cost_usd'):
                self.assertIsNone(event[key])
            summary = self.client.get('/observability/summary'+suffix, headers=self.headers).json()
            self.assertEqual((summary['error_count'], summary['total_tokens'], summary['estimated_cost_usd']), (1, 0, None))
        self.provider.speak.assert_not_awaited()
        self.assertEqual(self.post('/calls', {'lead_id': 1, 'product_id': 1}).status_code, 201)

    def test_backfill_guards_idempotency_and_original_outcome(self):
        self.seed()
        self.assertFalse(self.store.backfill_media_failure(1))
        self.store.finish(1, 'inconclusivo', 6)
        self.assertFalse(self.store.backfill_media_failure(1))
        self.store.calls[1]['human'] = True
        self.store.calls[1]['stream_sid'] = 'MZ'+'5'*32
        self.assertFalse(self.store.backfill_media_failure(1))
        self.store.calls[1]['stream_sid'] = None
        before = self.client.get('/calls/1', headers=self.headers).json()
        self.assertTrue(self.store.backfill_media_failure(1))
        self.assertFalse(self.store.backfill_media_failure(1))
        self.assertEqual(self.client.get('/calls/1', headers=self.headers).json(), before)
        self.assertEqual(len(self.client.get('/observability/events', headers=self.headers).json()), 1)

    def test_terminal_preserves_commercial_outcome_and_bound_stream(self):
        for status, stream, human in [('sucesso', None, True), ('ligando', 'MZ'+'5'*32, True),
                                      ('ligando', None, False)]:
            self.setUp(); self.seed()
            self.store.calls[1].update(status_final=status, stream_sid=stream, human=human)
            payload = {'CallSid': 'CA'+'1'*32, 'CallStatus': 'completed'}
            self.assertEqual(self.callback('/twilio/status/1', payload).status_code, 200)
            expected = status if status != 'ligando' else 'inconclusivo'
            self.assertEqual(self.client.get('/calls/1', headers=self.headers).json()['status_final'], expected)
            self.assertEqual(len(self.client.get('/observability/events', headers=self.headers).json()),
                             int(human and stream is None))
