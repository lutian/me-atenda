"""Public-interface offline contract checks; no provider calls."""
import audioop
import base64
import json
import hashlib
import hmac
import unittest
import threading
from types import SimpleNamespace
from fastapi.testclient import TestClient
from app.main import create_app
from app.config import Settings
from app.voice import NOTICE


def voiced_frame(): return audioop.lin2ulaw(b'\x00\x20' * 160, 2)


class MemoryStore:
    """API test double, deliberately NOT evidence of PostgreSQL concurrency."""
    def __init__(self):
        self.products = {}
        self.leads = {}
        self.calls = {}
        self.turns = []
        self.events = []
        self.snapshots = {}
        self.openings = {}; self.pins = {}; self.preparing = threading.Lock()
        self.answering = {}; self.answer_lock = threading.Lock()
        self.max_duration_seconds = 180
        self.profile = {'agent_name': 'Bia', 'tts_voice': None}
        self.messages = {}
    def preparation_guard(self):
        if not self.preparing.acquire(blocking=False): raise ValueError('Speech preparation busy')
        return SimpleNamespace(close=self.preparing.release)
    def opening_inputs(self, lead_id, product_id):
        self.get('leads', lead_id)
        product = self.get('products', product_id)
        if any(c['active'] for c in self.calls.values()): raise ValueError('active call')
        return product
    def opening_begin(self, digest, text, model, voice, format):
        if digest in self.openings:
            row = self.openings[digest]
            if row['audio'] is None: raise RuntimeError('Unknown preparation')
            return row['audio']
        self.openings[digest] = dict(text=text, audio=None)
    def opening_save(self, digest, audio, ledger):
        if not 1 <= len(audio) <= 720000: raise ValueError('Invalid audio')
        self.openings[digest]['audio'] = audio
    def opening_intro(self, id):
        row = self.openings[self.pins[id][2]]
        return row['text'], row['audio']
    def answer_setup(self, id, sid, action, verdict=None):
        from app.answering import transition
        from app.answering_store import clock
        with self.answer_lock:
            row = self.get('calls', id)
            eligible = row['active'] and row['status_final'] == 'ligando' and not row['stream_sid']
            if row['sid'] is not None or eligible: row = self.identify(id, sid)
            if not eligible: return 'hangup', None
            state = self.answering.setdefault(id, dict(hello_issued_at=None, verdict=None, polls=0))
            directive, outcome, human = transition(row,state,action,verdict,clock(None))
            digest = None
            if directive == 'hello':
                digest = self.pins[id][1]
                if self.openings[digest]['text'] != 'Alô?': raise LookupError()
            if outcome: self.outcome(id,outcome)
            if human: self.human(id)
            return directive, digest
    def hello_audio(self, digest):
        row = self.openings.get(digest)
        if not row or row['text'] != 'Alô?' or row['audio'] is None: raise LookupError()
        return row['audio']
    def abort_undialed(self, id):
        assert self.calls[id]['sid'] is None
        self.calls[id].update(active=False, status_final='falha_tecnica')
    def event_start(self, event):
        self.events.append(dict(event, id=len(self.events)+1))
        return len(self.events)
    def event_finish(self, id, event): self.events[id-1].update(event)
    def pricing_settings(self): return getattr(self, 'pricing', None)
    # Model catalog double: same contract as Store.
    def catalog(self):
        if not hasattr(self, 'ai_models'): self.ai_models, self.active = {}, {}
        return self.ai_models
    def models(self):
        from app.catalog import model_view
        rows = sorted(self.catalog().values(), key=lambda m: (m['kind'], m['name']))
        return [model_view(m, m['costs'], self.active.get(m['kind']) == m['id']) for m in rows]
    def model(self, id):
        found = [m for m in self.models() if m['id'] == id]
        if not found: raise LookupError()
        return found[0]
    def model_create(self, name, kind, costs):
        if any(m['name'] == name for m in self.catalog().values()): raise ValueError('Model name already exists')
        id = max(self.ai_models, default=0) + 1
        self.ai_models[id] = {'id': id, 'name': name, 'kind': kind, 'costs': dict(costs)}
        return self.model(id)
    def model_update(self, id, name, kind, costs):
        row = self.catalog().get(id)
        if row is None: raise LookupError()
        if self.active.get(row['kind']) == id and kind != row['kind']: raise ValueError('Model in use')
        if any(m['name'] == name and m['id'] != id for m in self.ai_models.values()): raise ValueError('Model name already exists')
        row.update(name=name, kind=kind, costs=dict(costs))
        return self.model(id)
    def model_delete(self, id):
        if id in self.active.values(): raise ValueError('Model in use')
        if self.catalog().pop(id, None) is None: raise LookupError()
        for product in self.products.values():  # ON DELETE SET NULL.
            if product.get('embedding_model_id') == id: product['embedding_model_id'] = None
    def model_selection(self):
        self.catalog()
        return {k: {'id': id, 'name': self.ai_models[id]['name']} for k, id in self.active.items()}
    def select_models(self, mapping):
        for kind, id in mapping.items():
            if id not in self.catalog(): raise LookupError()
            if self.ai_models[id]['kind'] != kind: raise ValueError('Model does not belong to this option')
        self.active.update(mapping)
        return self.model_selection()
    def ensure_models(self, defaults):
        for kind, name in defaults.items():
            self.catalog()
            if kind in self.active: continue
            found = [m for m in self.ai_models.values() if m['name'] == name]
            if not found: found = [self.ai_models[self.model_create(name, kind, {})['id']]]
            if found[0]['kind'] == kind: self.active[kind] = found[0]['id']
        return self.model_selection()
    def save_pricing_settings(self, config): self.pricing = json.loads(json.dumps(config))
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
    def call_policy(self): return {'max_duration_seconds':self.max_duration_seconds}
    def save_call_policy(self, seconds):
        self.max_duration_seconds = seconds
        return self.call_policy()
    def product_messages(self, product_id):
        self.get('products', product_id)
        return dict(self.messages.get(product_id, {'opening': None, 'pitch': None, 'goodbye': None,
                                                   'agent_name': None}))
    def save_product_messages(self, product_id, opening, pitch, goodbye, agent_name):
        self.get('products', product_id)
        self.messages[product_id] = {'opening': opening, 'pitch': pitch, 'goodbye': goodbye,
                                     'agent_name': agent_name}
        return dict(self.messages[product_id])
    def openings_ready(self, digests):
        return all(self.openings.get(d, {}).get('audio') for d in digests)
    def opening_scripts(self, id):
        pins = self.pins.get(id, ())
        return {kind: (self.openings[pins[i]]['text'], self.openings[pins[i]]['audio']) if len(pins) > i else None
                for kind, i in (('pitch', 3), ('goodbye', 4))}
    def agent_profile(self): return dict(self.profile)
    def save_agent_profile(self, name, voice):
        self.profile = {'agent_name': name, 'tts_voice': voice}
        return self.agent_profile()
    def reserve(self, lead_id, product_id, opening=None, profile=None):
        self.get('leads', lead_id); product = self.get('products', product_id)
        if opening and opening[0] != product['name']: raise ValueError('Spoken product changed')
        if profile is not None and profile != self.profile: raise ValueError('Agent profile changed')
        if any(c['active'] for c in self.calls.values()): raise ValueError('active call')
        id = len(self.calls) + 1
        self.calls[id] = dict(id=id, lead_id=lead_id, product_id=product_id,
            status_final='ligando', active=True, human=False, sid=None, stream_sid=None,
            duration_seconds=0, recording_status='pending', transcription_json=[], audio=None,
            max_duration_seconds=self.max_duration_seconds, **self.profile)
        if opening: self.pins[id] = opening
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
    def finish(self, id, status, duration=0, terminal_sid=None):
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
        if not c['active'] or c['status_final'] != 'ligando' or not c.get('human') or self.calls[id]['stream_sid']:
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
    async def answer(self, history, context, agent_name="Bia", scripts=(), goal=None): return {'text': 'Vamos conversar?', 'outcome': None}
    async def speak(self, text, voice=None): return b'\xff' * 160
    async def draft(self, name, description, agent_name, goal=None):
        return {'opening': 'Abertura de teste?', 'pitch': 'Pitch de teste?'}


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
    def raw_callback(self, path, data, valid=True):
        material = self.settings.public_base + path + ''.join(k + str(data[k]) for k in sorted(data))
        sig = base64.b64encode(hmac.new(b'test-token', material.encode(), hashlib.sha1).digest()).decode()
        return self.client.post(path, data=data, headers={'X-Twilio-Signature': sig if valid else 'bad'})
    def callback(self, path, data, valid=True):
        # Existing media tests follow real signed answer -> dedicated AMD -> gate, never a human bypass.
        response = API.raw_callback(self, path, data, valid)
        if valid and response.status_code == 200 and path.startswith('/twilio/voice/') and 'AnsweredBy' in data:
            id = path.rsplit('/',1)[1]
            amd = API.raw_callback(self, '/twilio/amd/'+id, data)
            if amd.status_code != 200: return amd
            return API.raw_callback(self, '/twilio/gate/'+id, {'CallSid':data['CallSid']})
        return response
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
