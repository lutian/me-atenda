"""Public signed callback and non-PII asset contracts; no external providers."""
import datetime as dt
import struct
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
import test_api as fixtures

SID = 'CA'+'1'*32


class Answering(unittest.TestCase):
    setUp = fixtures.API.setUp
    seed = fixtures.API.seed
    post = fixtures.API.post
    raw = fixtures.API.raw_callback

    def request(self, mode, **extra):
        return self.raw('/twilio/'+mode+'/1', dict(CallSid=SID, **extra))

    def test_pending_answer_issues_one_hello_then_human_gate(self):
        self.assertEqual(self.seed().status_code, 201)
        response = self.request('voice')
        root = ET.fromstring(response.text)
        self.assertEqual([e.tag for e in root], ['Play','Redirect'])
        self.assertTrue(root[0].text.startswith(self.settings.public_base+'/media/hello/'))
        self.assertEqual((root[1].attrib,root[1].text), ({'method':'POST'},self.settings.public_base+'/twilio/gate/1'))
        self.assertNotIn('Connect', response.text)
        self.assertNotIn('Play', self.request('voice').text)
        waiting = self.request('gate').text
        self.assertIn('<Pause length="1"', waiting)
        self.assertNotIn('Connect', waiting)
        self.assertEqual(self.request('amd', AnsweredBy='human').status_code, 200)
        self.assertIn('<Connect>', self.request('gate').text)
        self.assertEqual(self.client.get('/calls/1', headers=self.headers).json()['transcription_json'], [])

    def websocket_start(self, ws):
        stream = 'MZ'+'5'*32
        ws.send_json({'event':'start','streamSid':stream,'start':{'streamSid':stream,
            'callSid':SID,'mediaFormat':{'encoding':'audio/x-mulaw','sampleRate':8000,'channels':1}}})
        return stream

    def test_initial_answeredby_never_substitutes_for_dedicated_amd(self):
        from starlette.websockets import WebSocketDisconnect
        from test_stream_diagnostics import sign
        self.seed()
        self.assertIn('<Play>', self.request('voice', AnsweredBy='human').text)
        self.assertIn('Pause', self.request('gate').text)
        with self.client.websocket_connect('/twilio/stream/1', headers={
                'X-Twilio-Signature':sign(self.settings.public_base+'/twilio/stream/1')}) as ws:
            self.websocket_start(ws)
            with self.assertRaises(WebSocketDisconnect) as closed: ws.receive_json()
            self.assertEqual(closed.exception.code, 1011)
        self.assertEqual(self.client.get('/calls/1',headers=self.headers).json()['transcription_json'], [])

    def test_stream_bound_late_callbacks_leave_state_and_intro_unchanged(self):
        from starlette.websockets import WebSocketDisconnect
        from test_stream_diagnostics import sign
        self.seed(); self.request('voice'); self.request('amd', AnsweredBy='human')
        self.assertIn('Connect', self.request('gate').text)
        with self.client.websocket_connect('/twilio/stream/1', headers={
                'X-Twilio-Signature':sign(self.settings.public_base+'/twilio/stream/1')}) as ws:
            stream = self.websocket_start(ws)
            self.assertEqual(ws.receive_json()['event'], 'media')
            before = self.client.get('/calls',headers=self.headers).json()
            for verdict in ('human','fax','unknown','machine_end_beep'):
                self.request('amd', AnsweredBy=verdict)
            self.assertIn('Hangup', self.request('voice').text)
            self.assertIn('Hangup', self.request('gate').text)
            self.assertEqual(self.client.get('/calls',headers=self.headers).json(), before)
            ws.send_json({'event':'stop','sequenceNumber':'1','streamSid':stream,'stop':{'callSid':SID}})
            with self.assertRaises(WebSocketDisconnect) as closed: ws.receive_json()
            self.assertEqual(closed.exception.code, 1000)
        self.assertEqual(before[0]['transcription_json'], [
            {'role':'assistant','content':fixtures.NOTICE.format(product_name='Produto')}])

    def test_expired_human_cannot_claim_stream(self):
        from starlette.websockets import WebSocketDisconnect
        from test_stream_diagnostics import sign
        self.seed()
        start = dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)
        with patch('app.answering_store.clock', return_value=start):
            self.request('voice'); self.request('amd', AnsweredBy='human')
        with patch('app.answering_store.clock', return_value=start+dt.timedelta(seconds=35)):
            self.assertIn('Hangup', self.request('gate').text)
        with self.client.websocket_connect('/twilio/stream/1', headers={
                'X-Twilio-Signature':sign(self.settings.public_base+'/twilio/stream/1')}) as ws:
            self.websocket_start(ws)
            with self.assertRaises(WebSocketDisconnect): ws.receive_json()
        call = self.client.get('/calls/1',headers=self.headers).json()
        self.assertEqual((call['status_final'],call['transcription_json']), ('inconclusivo',[]))

    def test_human_first_conflicts_and_duplicate_voice_never_reissue(self):
        self.seed()
        self.assertEqual(self.request('amd', AnsweredBy='human').json(), {'ok':True})
        first = self.request('voice').text
        self.assertIn('<Play>', first); self.assertNotIn('Connect', first)
        for verdict in ('human','machine_end_beep','fax','unknown'):
            self.assertEqual(self.request('amd', AnsweredBy=verdict).status_code, 200)
            self.assertIn('<Connect>', self.request('gate').text)
            self.assertNotIn('Play', self.request('voice').text)
        terminal = self.raw('/twilio/status/1', {'CallSid':SID,'CallStatus':'completed'})
        self.assertEqual(terminal.status_code, 200)
        before = self.client.get('/calls', headers=self.headers).json()
        self.request('amd', AnsweredBy='machine_end_beep')
        self.assertIn('Hangup', self.request('voice').text)
        self.assertIn('Hangup', self.request('gate').text)
        self.assertEqual(self.client.get('/calls', headers=self.headers).json(), before)

    def test_nonhuman_outcomes_and_invalid_verdicts(self):
        for verdict, outcome in [('machine_end_beep','caixa_postal'),('fax','numero_errado'),('unknown','inconclusivo')]:
            self.setUp(); self.seed()
            self.assertIn('<Play>', self.request('voice').text)
            for invalid in ({}, {'AnsweredBy':'invented'}):
                self.assertEqual(self.request('amd', **invalid).status_code, 422)
            self.assertEqual(self.request('amd', AnsweredBy=verdict).status_code, 200)
            self.assertIn('Hangup', self.request('gate').text)
            self.request('amd', AnsweredBy='human')
            self.assertEqual(self.client.get('/calls/1', headers=self.headers).json()['status_final'], outcome)
            self.assertEqual(self.post('/calls', {'lead_id':1,'product_id':1}).status_code, 409)
            self.raw('/twilio/status/1', {'CallSid':SID,'CallStatus':'completed'})
            self.assertEqual(self.post('/calls', {'lead_id':1,'product_id':1}).status_code, 201)

    def test_deadline_and_poll_budget_without_sleep_or_timer_reset(self):
        for deadline in (True,False):
            self.setUp(); self.seed()
            start = dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)
            with patch('app.answering_store.clock', return_value=start):
                self.request('voice')
                if not deadline:
                    for _ in range(34):
                        self.assertIn('Pause', self.request('gate').text)
                        self.assertNotIn('Play', self.request('voice').text)
            with patch('app.answering_store.clock', return_value=start+dt.timedelta(seconds=35 if deadline else 0)):
                self.assertIn('Hangup', self.request('gate').text)
                self.request('amd', AnsweredBy='human')
                self.assertIn('Hangup', self.request('gate').text)
            self.assertEqual(self.client.get('/calls/1', headers=self.headers).json()['status_final'], 'inconclusivo')
            self.assertEqual(self.post('/calls', {'lead_id':1,'product_id':1}).status_code, 409)

    def test_callback_signature_sid_and_exact_origin_query_rejections(self):
        self.seed(); self.request('voice')
        paths = ['/calls','/leads','/products']
        def snapshot(): return [self.client.get(p, headers=self.headers).json() for p in paths]
        before = snapshot()
        for mode in ('voice','gate','amd'):
            path = '/twilio/'+mode+'/1'
            data = {'CallSid':SID,'AnsweredBy':'human'}
            self.assertEqual(self.raw(path, data, False).status_code, 403)
            self.assertEqual(self.raw(path, dict(data,CallSid='CA'+'9'*32)).status_code, 409)
            self.assertEqual(self.raw(path, dict(data,CallSid='invalid')).status_code, 422)
            from test_stream_diagnostics import sign
            material = ''.join(k+data[k] for k in sorted(data))
            good = sign(self.settings.public_base+path+'?x=%2F&y=1'+material)
            self.assertEqual(self.client.post(path, data=data, headers={
                'X-Twilio-Signature':good,'Host':'evil.example','X-Forwarded-Host':'evil.example'}).status_code, 403)
            self.assertEqual(snapshot(), before)
            # A signature for an attacker origin cannot be rescued by Host/Forwarded headers.
            foreign = sign('https://evil.example'+path+material)
            self.assertEqual(self.client.post(path, data=data, headers={
                'X-Twilio-Signature':foreign,'Host':'evil.example','X-Forwarded-Host':'evil.example'}).status_code, 403)
            self.assertEqual(snapshot(), before)
        # Invalid callbacks consume no gate polls.
        for _ in range(34): self.assertIn('Pause', self.request('gate').text)
        self.assertIn('Hangup', self.request('gate').text)

    def test_pending_generic_audio_is_not_publicly_ready(self):
        from app.openings import identity, HELLO
        from unittest.mock import AsyncMock
        speech = AsyncMock(wraps=self.provider.speak)
        self.provider.speak = speech
        with patch.object(self.store, 'opening_save', side_effect=OSError('cache unavailable')):
            self.assertEqual(self.seed().status_code, 503)
        path = '/media/hello/'+identity(HELLO,self.settings)+'.wav'
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(speech.await_count, 1)
        self.assertEqual(self.client.get('/calls',headers=self.headers).json(), [])

    def test_generic_asset_only_valid_wav_cache_and_no_generation(self):
        self.seed()
        root = ET.fromstring(self.request('voice').text)
        path = root[0].text.removeprefix(self.settings.public_base)
        async def forbidden(text): raise AssertionError('GET must never synthesize')
        self.provider.speak = forbidden
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['content-type'], 'audio/wav')
        body = response.content
        self.assertEqual((body[:4], body[8:12]), (b'RIFF', b'WAVE'))
        self.assertEqual(struct.unpack_from('<I',body,4)[0], len(body)-8)
        self.assertEqual(struct.unpack_from('<HHIIHH',body,20), (7,1,8000,8000,1,8))
        self.assertEqual(body[-160:], b'\xff'*160)
        self.assertIn('immutable', response.headers['cache-control'])
        etag = response.headers['etag']
        cached = self.client.get(path, headers={'If-None-Match':etag})
        self.assertEqual((cached.status_code,cached.content), (304,b''))
        from app.openings import identity, NOTICE
        intro = identity(NOTICE.format(product_name='Produto'), self.settings)
        for digest in (intro,'0'*64,'../recordings','UPPER','a'*63):
            self.assertEqual(self.client.get('/media/hello/'+digest+'.wav').status_code, 404)
        self.assertEqual(self.client.get(path+'?text=secret').status_code, 404)
        self.assertEqual(self.client.get('/calls/1/audio').status_code, 401)
