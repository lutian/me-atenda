"""Public-interface offline contract checks; no provider calls."""
import base64
import hashlib
import hmac
import unittest
from fastapi.testclient import TestClient
from app.main import create_app
from app.config import Settings


class MemoryStore:
    """API test double, deliberately NOT evidence of PostgreSQL concurrency."""
    def __init__(self):
        self.products = {}
        self.leads = {}
        self.calls = {}
        self.turns = []
        self.events = []
        self.snapshots = {}
    def event_start(self, event):
        self.events.append(dict(event, id=len(self.events)+1))
        return len(self.events)
    def event_finish(self, id, event): self.events[id-1].update(event)
    def account_call(self, id, snapshot): self.snapshots.setdefault(id, snapshot)
    def health(self): return True
    def list(self, table): return list(getattr(self, table).values())
    def get(self, table, id):
        row = getattr(self, table).get(id)
        if row is None: raise LookupError()
        return dict(row)
    def product(self, data, chunks, id=None):
        id = id or len(self.products) + 1
        self.products[id] = dict(id=id, **data)
        return self.products[id]
    def lead(self, data, id=None):
        id = id or len(self.leads) + 1
        self.leads[id] = dict(id=id, status='pendente', **data)
        return self.leads[id]
    def import_leads(self, rows): return [self.lead(row) for row in rows]
    def delete(self, table, id):
        self.get(table, id)
        del getattr(self, table)[id]
    def reserve(self, lead_id, product_id):
        self.get('leads', lead_id); self.get('products', product_id)
        if any(c['active'] for c in self.calls.values()): raise ValueError('active call')
        id = len(self.calls) + 1
        self.calls[id] = dict(id=id, lead_id=lead_id, product_id=product_id,
            status_final='ligando', active=True, sid=None, stream_sid=None,
            duration_seconds=0, recording_status='pending', transcription_json=[], audio=None)
        return dict(self.calls[id])
    def audio(self, id): return self.calls[id]['audio']
    def outcome(self, id, status):
        if self.calls[id]['active'] and self.calls[id]['status_final'] == 'ligando':
            self.calls[id]['status_final'] = status
    def absent(self, id): self.calls[id]['recording_status'] = 'absent'
    def retrieve(self, product_id, embedding): return 'Produto de teste'
    def bind(self, id, sid): self.calls[id]['sid'] = sid
    def identify(self, id, sid):
        c = self.calls[id]
        if c['sid'] is None: c['sid'] = sid
        if c['sid'] != sid: raise ValueError('call mismatch')
        return dict(c)
    def finish(self, id, status, duration=0):
        c = self.calls[id]
        if c['active']:
            if c['status_final'] != 'ligando': status = c['status_final']
            c.update(active=False, status_final=status, duration_seconds=duration)
        return dict(c)
    def recording(self, id, sid, data=None, failed=False):
        c = self.calls[id]
        if c['recording_status'] != 'available':
            c.update(recording_status='failed' if failed else 'available', audio=data)
    def human(self, id): self.calls[id]['human'] = True
    def claim_stream(self, id, sid, stream):
        c = self.identify(id, sid)
        if not c['active'] or not c.get('human') or self.calls[id]['stream_sid']:
            raise ValueError('stream mismatch')
        self.calls[id]['stream_sid'] = stream
        return c
    def append(self, id, role, content):
        self.calls[id]['transcription_json'].append(dict(role=role, content=content))
    def context(self, product_id, vector): return ['Produto de teste']


class Providers:
    def __init__(self): self.dials = []; self.downloads = []; self.stopped = []
    async def embed(self, texts): return [[0.0] * 1536 for _ in texts]
    async def dial(self, call, phone): self.dials.append((call, phone)); return 'CA' + '1' * 32
    async def download(self, sid): self.downloads.append(sid); return b'RIFFaudio'
    async def hangup(self, sid): self.stopped.append(sid)
    async def transcribe(self, pcm): return 'Tenho interesse'
    async def answer(self, history, context): return {'text': 'Vamos conversar?', 'outcome': None}
    async def speak(self, text): return b'\xff' * 160


class API(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore(); self.provider = Providers()
        self.settings = Settings(admin_key='local-test-key', public_base='https://voice.example/prefix',
                                 twilio_token='test-token', account_sid='AC' + '2' * 32)
        self.client = TestClient(create_app(self.settings, self.store, self.provider))
        self.headers = {'X-API-Key': 'local-test-key'}
    def post(self, path, data): return self.client.post(path, json=data, headers=self.headers)
    def seed(self):
        self.assertEqual(self.post('/products', {'name': 'Produto', 'description': 'Descrição'}).status_code, 201)
        self.assertEqual(self.post('/leads', {'name': 'Ana', 'phone': '+5511999999999', 'company': 'ACME'}).status_code, 201)
        return self.post('/calls', {'lead_id': 1, 'product_id': 1})
    def callback(self, path, data, valid=True):
        material = self.settings.public_base + path + ''.join(k + str(data[k]) for k in sorted(data))
        sig = base64.b64encode(hmac.new(b'test-token', material.encode(), hashlib.sha1).digest()).decode()
        return self.client.post(path, data=data, headers={'X-Twilio-Signature': sig if valid else 'bad'})
    def test_admin_and_validation_are_non_mutating(self):
        self.assertEqual(self.client.get('/health').status_code, 200)
        for route in ['/products', '/leads', '/calls']:
            self.assertEqual(self.client.get(route).status_code, 401)
        self.assertEqual(self.post('/leads', {'name': 'Ana', 'phone': 'invalid', 'company': ''}).status_code, 422)
        self.assertEqual(self.post('/leads/import', [{'name': 'A', 'phone': '+5511999999999', 'company': ''},
                                                  {'name': 'B', 'phone': 'bad', 'company': ''}]).status_code, 422)
        self.assertEqual(self.client.get('/leads', headers=self.headers).json(), [])
    def test_crud_embeddings_and_import(self):
        self.seed()
        r = self.client.patch('/products/1', json={'name': 'Novo'}, headers=self.headers)
        self.assertEqual(r.status_code, 200); self.assertEqual(r.json()['description'], 'Descrição')
        r = self.client.patch('/leads/1', json={'company': 'Nova'}, headers=self.headers)
        self.assertEqual(r.json()['company'], 'Nova')
        r = self.post('/leads/import', [{'name': 'B', 'phone': '+5511888888888', 'company': ''}])
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.client.delete('/leads/2', headers=self.headers).status_code, 204)
        self.assertEqual(self.client.get('/calls/1/audio', headers=self.headers).status_code, 404)
    def test_single_call_and_callback_terminal_idempotency(self):
        self.assertEqual(self.seed().status_code, 201)
        self.assertEqual(self.post('/calls', {'lead_id': 1, 'product_id': 1}).status_code, 409)
        self.assertEqual(len(self.provider.dials), 1)
        payload = {'CallSid': 'CA' + '1' * 32, 'CallStatus': 'completed', 'CallDuration': '42'}
        self.assertEqual(self.callback('/twilio/status/1', payload).status_code, 200)
        payload['CallStatus'] = 'ringing'
        self.assertEqual(self.callback('/twilio/status/1', payload).status_code, 200)
        c = self.client.get('/calls/1', headers=self.headers).json()
        self.assertEqual(c['status_final'], 'inconclusivo'); self.assertEqual(c['duration_seconds'], 42)
        self.assertEqual(c['audio_url'], None)
    def test_exact_signature_and_sid_binding(self):
        self.seed()
        data = {'CallSid': 'CA' + '1' * 32, 'AnsweredBy': 'human'}
        self.assertEqual(self.callback('/twilio/voice/1', data, False).status_code, 403)
        self.assertEqual(self.client.get('/calls/1', headers=self.headers).json()['status_final'], 'ligando')
        data['CallSid'] = 'CA' + '3' * 32
        self.assertEqual(self.callback('/twilio/voice/1', data).status_code, 409)
        data['CallSid'] = 'CA' + '1' * 32
        r = self.callback('/twilio/voice/1', data)
        self.assertIn('wss://voice.example/prefix/twilio/stream/1', r.text)
        self.assertIn('<Connect>', r.text)
    def test_amd_does_not_conflate_unknown_with_ivr(self):
        for amd, expected in [('machine_end_beep', 'caixa_postal'), ('fax', 'numero_errado'),
                              ('unknown', 'inconclusivo')]:
            self.setUp(); self.seed()
            r = self.callback('/twilio/voice/1', {'CallSid': 'CA'+'1'*32, 'AnsweredBy': amd})
            self.assertIn('<Hangup', r.text); self.assertNotIn('<Connect>', r.text)
            self.assertEqual(self.client.get('/calls/1', headers=self.headers).json()['status_final'], expected)
    def test_recording_is_authenticated_retryable_and_never_fetches_url(self):
        self.seed()
        data = {'CallSid': 'CA'+'1'*32, 'RecordingSid': 'RE'+'4'*32,
                'RecordingStatus': 'completed', 'RecordingUrl': 'http://169.254.169.254/secrets'}
        self.assertEqual(self.callback('/twilio/recording/1', data).status_code, 200)
        self.assertEqual(self.provider.downloads, ['RE'+'4'*32])
        self.assertEqual(self.callback('/twilio/recording/1', data).status_code, 200)
        self.assertEqual(len(self.provider.downloads), 1)
        self.assertEqual(self.client.get('/calls/1/audio', headers=self.headers).content, b'RIFFaudio')
    def test_recording_failure_visible_and_retryable(self):
        self.seed()
        async def fail(sid): raise OSError('provider offline')
        self.provider.download = fail
        data = {'CallSid': 'CA'+'1'*32, 'RecordingSid': 'RE'+'4'*32, 'RecordingStatus': 'completed'}
        self.assertEqual(self.callback('/twilio/recording/1', data).status_code, 503)
        self.assertEqual(self.client.get('/calls/1', headers=self.headers).json()['recording_status'], 'failed')
        self.provider.download = Providers().download
        self.assertEqual(self.callback('/twilio/recording/1', data).status_code, 200)
    def test_ws_rejects_unsigned_connection(self):
        self.seed()
        from starlette.websockets import WebSocketDisconnect
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect('/twilio/stream/1'): pass


if __name__ == '__main__': unittest.main()
