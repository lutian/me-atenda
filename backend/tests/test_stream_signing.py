"""Actual TwiML signing contract, fake providers, and a loopback wire upgrade."""
import audioop
import base64
import json
import socket
import threading
import unittest
from contextlib import contextmanager
from urllib.parse import urlsplit
from xml.etree import ElementTree

import httpx
import uvicorn
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import ConnectionClosedOK
from websockets.sync.client import connect

from app.main import create_app
from test_stream_diagnostics import TransactionStore, sign
import test_api as fixtures


class WireSocket:
    def __init__(self, socket): self.socket = socket
    def send_json(self, data): self.socket.send(json.dumps(data))
    def receive_json(self): return json.loads(self.socket.recv(timeout=3))


class WireClient(httpx.Client):
    @contextmanager
    def websocket_connect(self, path, headers):
        with connect(str(self.base_url).replace('http:', 'ws:').rstrip('/') + path,
                     additional_headers=headers, proxy=None, open_timeout=3, close_timeout=3) as ws:
            # Unlike TestClient's ASGI accept, this is a real HTTP upgrade response.
            assert ws.response.status_code == 101
            yield WireSocket(ws)


@contextmanager
def loopback(app):
    ready = threading.Event()
    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets)
            ready.set()
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        server = Server(uvicorn.Config(app, lifespan='off', log_level='critical', access_log=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        thread.start()
        try:
            if not ready.wait(3): raise AssertionError('loopback server did not start')
            with WireClient(base_url=f'http://127.0.0.1:{listener.getsockname()[1]}',
                            timeout=3, trust_env=False) as client:
                yield client
        finally:
            server.should_exit = True
            thread.join(3)
            if thread.is_alive(): raise AssertionError('loopback server did not stop')


class Signing(unittest.TestCase):
    seed = fixtures.API.seed
    post = fixtures.API.post
    callback = fixtures.API.callback

    def setUp(self):
        fixtures.API.setUp(self)
        # Exercise configured port as well as prefix, independently of transport Host.
        self.settings.public_base = 'https://voice.example:8443/prefix'
        self.store = TransactionStore()
        self.app = create_app(self.settings, self.store, self.provider)
        self.client = TestClient(self.app)

    def generated(self):
        self.assertEqual(self.seed().status_code, 201)
        response = self.callback('/twilio/voice/1', {'CallSid': 'CA'+'1'*32, 'AnsweredBy': 'human'})
        self.assertEqual(response.status_code, 200)
        url = ElementTree.fromstring(response.text).find('./Connect/Stream').attrib['url']
        self.assertEqual(urlsplit(url).scheme, 'wss')
        return url

    def snapshot(self):
        return [(r.status_code, r.json()) for r in
                (self.client.get(path, headers=self.headers) for path in
                 ('/calls', '/leads', '/products', '/observability/events', '/observability/summary'))]

    def session(self, slash=False, wire=False):
        url = self.generated()
        # The signed material comes from the returned Stream, not public_url or HTTPS helpers.
        signature = sign(url + ('/' if slash else ''))
        path = urlsplit(url).path.removeprefix('/prefix')
        stream = 'MZ'+'5'*32
        start = {'event': 'start', 'streamSid': stream, 'start': {
            'streamSid': stream, 'callSid': 'CA'+'1'*32,
            'mediaFormat': {'encoding': 'audio/x-mulaw', 'sampleRate': 8000, 'channels': 1}}}
        spoken = []; sales = []; transcribed = []
        async def speak(text):
            spoken.append(text)
            return b'\xff'*160
        async def transcribe(pcm):
            transcribed.append(len(pcm))
            return 'accepted final utterance'
        async def answer(*args):
            sales.append(args)
            raise AssertionError('terminal call must not sell')
        self.provider.speak = speak
        self.provider.transcribe = transcribe
        self.provider.answer = answer
        with self.client.websocket_connect(path, headers={'X-Twilio-Signature': signature}) as ws:
            ws.send_json({'event': 'connected', 'protocol': 'Call', 'version': '1.0.0'})
            ws.send_json(start)
            self.assertEqual(ws.receive_json(), {'event': 'media', 'streamSid': stream,
                'media': {'payload': base64.b64encode(b'\xff'*160).decode()}})
            voice = audioop.lin2ulaw(b'\x00\x20'*160, 2)
            ws.send_json({'event': 'media', 'sequenceNumber': '2', 'streamSid': stream,
                          'media': {'track': 'inbound', 'payload': base64.b64encode(voice).decode()}})
            # Completion before stop must preserve/drain accepted speech without more sales.
            terminal = {'CallSid': 'CA'+'1'*32, 'CallStatus': 'completed', 'CallDuration': '12'}
            for _ in range(2):
                r = self.callback('/twilio/status/1', terminal)
                self.assertEqual((r.status_code, r.json()), (200, {'ok': True}))
            self.assertEqual(self.post('/calls', {'lead_id': 1, 'product_id': 1}).status_code, 201)
            ws.send_json({'event': 'stop', 'sequenceNumber': '3', 'streamSid': stream,
                          'stop': {'callSid': 'CA'+'1'*32}})
            with self.assertRaises(ConnectionClosedOK if wire else WebSocketDisconnect) as closed:
                ws.receive_json()
            self.assertEqual(closed.exception.rcvd.code if wire else closed.exception.code, 1000)
        call = self.client.get('/calls/1', headers=self.headers).json()
        self.assertEqual((call['status_final'], call['duration_seconds']), ('inconclusivo', 12))
        self.assertEqual(call['transcription_json'], [
            {'role': 'assistant', 'content': spoken[0]}, {'role': 'user', 'content': 'accepted final utterance'}])
        self.assertEqual((len(spoken), len(transcribed), sales), (1, 1, []))
        self.assertGreater(transcribed[0], 0)
        self.assertEqual(self.provider.stopped, ['CA'+'1'*32])
        self.assertEqual(self.client.get('/observability/events', headers=self.headers).json(), [])
        self.assertEqual(self.post('/calls', {'lead_id': 1, 'product_id': 1}).status_code, 409)

    def test_generated_wss_exact_and_slash_full_session(self):
        for slash in (False, True):
            with self.subTest(slash=slash):
                self.setUp()
                self.session(slash)

    def test_loopback_http_101_and_full_session(self):
        with loopback(self.app) as client:
            self.client = client
            self.session(wire=True)

    def test_callback_scheme_and_slash_stay_exact(self):
        self.seed()
        data = {'CallSid': 'CA'+'1'*32, 'AnsweredBy': 'human'}
        form = ''.join(k+data[k] for k in sorted(data))
        before = self.snapshot()
        for url in ('wss://voice.example:8443/prefix/twilio/voice/1',
                    'https://voice.example:8443/prefix/twilio/voice/1/'):
            r = self.client.post('/twilio/voice/1', data=data,
                                 headers={'X-Twilio-Signature': sign(url+form)})
            self.assertEqual((r.status_code, r.json()), (403, {'detail': 'Invalid Twilio signature'}))
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.callback('/twilio/voice/1', data).status_code, 200)

    def test_wss_bound_call_and_stream_guards(self):
        for variant in ('nonhuman', 'wrong_call', 'wrong_stream', 'inactive', 'reconnect'):
            with self.subTest(variant=variant):
                self.setUp()
                url = self.generated()
                if variant == 'nonhuman':
                    # Start from a fresh call with no signed human callback.
                    self.setUp(); self.seed()
                if variant == 'inactive':
                    self.callback('/twilio/status/1', {'CallSid': 'CA'+'1'*32, 'CallStatus': 'completed'})
                stream = 'MZ'+'5'*32
                start = {'event': 'start', 'streamSid': stream, 'start': {
                    'streamSid': 'MZ'+'6'*32 if variant == 'wrong_stream' else stream,
                    'callSid': 'CA'+('9' if variant == 'wrong_call' else '1')*32,
                    'mediaFormat': {'encoding': 'audio/x-mulaw', 'sampleRate': 8000, 'channels': 1}}}
                if variant == 'reconnect':
                    with self.client.websocket_connect('/twilio/stream/1', headers={'X-Twilio-Signature': sign(url)}) as ws:
                        ws.send_json(start); self.assertEqual(ws.receive_json()['event'], 'media')
                        ws.send_json({'event': 'stop', 'sequenceNumber': '1', 'streamSid': stream,
                                      'stop': {'callSid': 'CA'+'1'*32}})
                        with self.assertRaises(WebSocketDisconnect): ws.receive_json()
                before = self.snapshot()
                with self.client.websocket_connect('/twilio/stream/1', headers={'X-Twilio-Signature': sign(url)}) as ws:
                    ws.send_json(start)
                    with self.assertRaises(WebSocketDisconnect) as closed: ws.receive_json()
                    self.assertEqual(closed.exception.code, 1011)
                self.assertEqual(self.snapshot(), before)
