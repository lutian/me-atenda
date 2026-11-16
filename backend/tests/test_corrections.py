"""Verifier regressions, through public REST/WS and the real Twilio adapter."""
import asyncio
import audioop
import base64
import hashlib
import hmac
import json
import threading
import time
import unittest
import httpx
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from app.main import create_app
from app.providers import Providers
import test_api as fixtures


class Corrections(unittest.TestCase):
    setUp = fixtures.API.setUp
    seed = fixtures.API.seed
    post = fixtures.API.post
    callback = fixtures.API.callback

    def adapter(self, handler):
        provider = Providers(self.settings)
        provider.embed = self.provider.embed  # Only embeddings are doubled.
        provider.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler),
            auth=(self.settings.account_sid, self.settings.twilio_token), follow_redirects=False)
        self.client = TestClient(create_app(self.settings, self.store, provider))

    def test_definitive_invalid_number_releases_reservation(self):
        requests = []
        def handler(request):
            requests.append(request)
            self.assertTrue(request.url.path.endswith('/Calls.json'))
            return httpx.Response(400, json={'code': 21211, 'message': 'Invalid To Phone Number', 'status': 400})
        self.adapter(handler)
        response = self.seed()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {'detail': 'Twilio rejected call creation: numero_errado'})
        history = self.client.get('/calls', headers=self.headers).json()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['status_final'], 'numero_errado')
        self.assertEqual(history[0]['transcription_json'], [])
        self.assertIsNone(history[0]['audio_url'])
        # A second actual creation attempt, not a conflict with the rejected call.
        self.assertEqual(self.post('/calls', {'lead_id': 1, 'product_id': 1}).status_code, 422)
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(self.client.get('/calls', headers=self.headers).json()), 2)

    def test_ambiguous_transport_and_binding_failures_keep_reservation(self):
        for branch in ('transport', 'binding'):
            with self.subTest(branch=branch):
                self.setUp()
                requests = []
                def handler(request):
                    requests.append(request)
                    if branch == 'transport': raise httpx.ReadTimeout('response lost', request=request)
                    return httpx.Response(201, json={'sid': 'CA'+'1'*32})
                self.adapter(handler)
                if branch == 'binding':
                    def fail_bind(id, sid): raise OSError('database unavailable after creation')
                    self.store.bind = fail_bind
                response = self.seed()
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json(), {'detail': 'Dial uncertain; active slot retained until terminal callback or reconciliation'})
                row = self.client.get('/calls/1', headers=self.headers).json()
                self.assertEqual(row['status_final'], 'falha_tecnica')
                self.assertEqual(self.post('/calls', {'lead_id': 1, 'product_id': 1}).status_code, 409)
                self.assertEqual(len(requests), 1)
                self.assertEqual(len(self.client.get('/calls', headers=self.headers).json()), 1)

    def test_carrier_completion_drains_three_accepted_utterances(self):
        for ending in ('stop', 'disconnect'):
            with self.subTest(ending=ending):
                self.setUp(); self.seed()
                self.callback('/twilio/voice/1', {'CallSid': 'CA'+'1'*32, 'AnsweredBy': 'human'})
                entered = threading.Event(); release = threading.Event()
                transcribed = []; sales = []; speech = []
                async def transcribe(pcm):
                    if not transcribed:
                        entered.set()
                        if not await asyncio.to_thread(release.wait, 15): raise TimeoutError('test release missing')
                    transcribed.append(len(pcm))
                    return 'utterance ' + str(len(transcribed))
                async def answer(history, context):
                    sales.append(history)
                    return {'text': 'unexpected sales reply', 'outcome': None}
                async def speak(text):
                    speech.append(text)
                    return b'\xff'*160
                self.provider.transcribe = transcribe
                self.provider.answer = answer
                self.provider.speak = speak
                accepted = threading.Event(); session_closed = threading.Event()
                close_codes = []
                application = create_app(self.settings, self.store, self.provider)
                async def observed_application(scope, receive, send):
                    if scope['type'] != 'websocket':
                        return await application(scope, receive, send)
                    last_sequence = None
                    async def observed_receive():
                        nonlocal last_sequence
                        # Requesting the next ASGI message proves the preceding media
                        # frame passed protocol processing, including queue insertion.
                        if last_sequence == 94: accepted.set()
                        message = await receive()
                        if message['type'] == 'websocket.receive':
                            last_sequence = int(json.loads(message['text']).get('sequenceNumber', 0))
                        return message
                    async def observed_send(message):
                        if message['type'] == 'websocket.close': close_codes.append(message['code'])
                        await send(message)
                    try: await application(scope, observed_receive, observed_send)
                    finally: session_closed.set()
                self.client = TestClient(observed_application)
                url = self.settings.public_base + '/twilio/stream/1'
                sig = base64.b64encode(hmac.new(b'test-token', url.encode(), hashlib.sha1).digest()).decode()
                stream = 'MZ'+'5'*32
                started = time.monotonic()
                try:
                    with self.client.websocket_connect('/twilio/stream/1', headers={'X-Twilio-Signature': sig}) as ws:
                        ws.send_json({'event': 'start', 'streamSid': stream, 'start': {
                            'streamSid': stream, 'callSid': 'CA'+'1'*32,
                            'mediaFormat': {'encoding': 'audio/x-mulaw', 'sampleRate': 8000, 'channels': 1}}})
                        self.assertEqual(ws.receive_json()['event'], 'media')
                        sequence = 1
                        for utterance_index in range(3):
                            voiced = base64.b64encode(audioop.lin2ulaw(b'\x00\x20'*160, 2)).decode()
                            silent = base64.b64encode(b'\xff'*160).decode()
                            for payload in [voiced] + [silent]*30:
                                sequence += 1
                                ws.send_json({'event': 'media', 'sequenceNumber': str(sequence), 'streamSid': stream,
                                              'media': {'track': 'inbound', 'payload': payload}})
                            if utterance_index == 0:
                                self.assertTrue(entered.wait(2), 'first utterance never reached STT')
                        self.assertTrue(accepted.wait(2), 'three utterances were not accepted before completion')
                        result = self.callback('/twilio/status/1', {'CallSid': 'CA'+'1'*32,
                                               'CallStatus': 'completed', 'CallDuration': '12'})
                        self.assertEqual(result.status_code, 200)
                        # A new call owns the slot before the old consumer finishes.
                        self.assertEqual(self.post('/calls', {'lead_id': 1, 'product_id': 1}).status_code, 201)
                        if ending == 'stop':
                            ws.send_json({'event': 'stop', 'sequenceNumber': str(sequence+1), 'streamSid': stream,
                                          'stop': {'callSid': 'CA'+'1'*32}})
                        else: ws.close()
                        release.set()
                        if ending == 'stop':
                            with self.assertRaises(WebSocketDisconnect) as closed: ws.receive_json()
                            self.assertEqual(closed.exception.code, 1000)
                        # TestClient context exit cancels its application task. Wait for
                        # actual disconnect draining before allowing that cancellation.
                        self.assertTrue(session_closed.wait(3), 'accepted utterances did not drain boundedly')
                finally: release.set()
                self.assertEqual(close_codes, [1000])
                self.assertLess(time.monotonic() - started, 5, 'draining must not wait on a stranded sentinel')
                row = self.client.get('/calls/1', headers=self.headers).json()
                self.assertEqual([r['content'] for r in row['transcription_json'] if r['role']=='user'],
                                 ['utterance 1', 'utterance 2', 'utterance 3'])
                self.assertEqual(row['status_final'], 'inconclusivo')
                self.assertEqual(row['duration_seconds'], 12)
                self.assertEqual(len(transcribed), 3)
                self.assertEqual(sales, [])
                self.assertEqual(len(speech), 1)  # Opening notice only.
                self.assertEqual(self.client.get('/calls/2', headers=self.headers).json()['status_final'], 'ligando')
                self.assertEqual(self.post('/calls', {'lead_id': 1, 'product_id': 1}).status_code, 409)


if __name__ == '__main__': unittest.main()
