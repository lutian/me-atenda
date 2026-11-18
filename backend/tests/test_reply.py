"""Real SDK serialization and an active conversational reply; no paid network."""
import asyncio
import audioop
import base64
import hashlib
import hmac
import json
import unittest

import httpx
from openai import AsyncOpenAI
from starlette.websockets import WebSocketDisconnect
from app.config import Settings
from app.providers import Providers
import test_api as fixtures


def transport(request):
    body = json.loads(request.content)
    if 'max_tokens' in body:
        return httpx.Response(400, json={'error': {
            'message': 'Use max_completion_tokens instead.',
            'type': 'invalid_request_error', 'param': 'max_tokens',
            'code': 'unsupported_parameter'}})
    assert request.url.path == '/v1/chat/completions'
    assert body['max_completion_tokens'] == 300
    assert body['response_format'] == {'type': 'json_object'}
    return httpx.Response(200, json={
        'id': 'synthetic-reply', 'object': 'chat.completion', 'created': 1,
        'model': body['model'], 'choices': [{'index': 0, 'finish_reason': 'stop',
        'message': {'role': 'assistant', 'content':
                    '{"text":"Vamos conversar?","outcome":null}'}}]})


def adapter(model='gpt-6.1-sol'):
    provider = Providers(Settings(chat_model=model))
    provider._openai = AsyncOpenAI(
        api_key='offline-placeholder', base_url='https://offline.invalid/v1/',
        max_retries=0, http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(transport)))
    return provider


class ReplyWire(unittest.IsolatedAsyncioTestCase):
    async def test_modern_and_default_models_keep_bounded_structured_reply(self):
        for model in ('gpt-6.1-sol', Settings().chat_model):
            with self.subTest(model=model):
                provider = adapter(model)
                try:
                    result = await provider.answer(
                        [{'role': 'user', 'content': 'Pode apresentar.'}], 'Teste')
                    self.assertEqual(result, {'text': 'Vamos conversar?', 'outcome': None, 'script': None})
                    self.assertEqual(provider.s.chat_model, model)
                    self.assertEqual(provider.ai.max_retries, 0)
                finally:
                    await provider.ai.close()


class ActiveReply(unittest.TestCase):
    setUp = fixtures.API.setUp
    seed = fixtures.API.seed
    post = fixtures.API.post
    callback = fixtures.API.callback

    def test_reply_reaches_caller_before_stop_without_hangup(self):
        self.seed()
        trace = []
        reply = adapter()
        self.addCleanup(lambda: asyncio.run(reply.ai.close()))
        async def transcribe(pcm):
            trace.append('transcribe')
            return 'Pode apresentar.'
        async def embed(texts):
            trace.append('embed')
            return [[0.0] * 1536 for _ in texts]
        def retrieve(product_id, vector):
            trace.append('retrieve')
            return 'Produto fictício'
        async def answer(history, context, agent_name="Bia", scripts=(), goal=None):
            trace.append('answer')
            return await reply.answer(history, context)
        async def speak(text, voice=None):
            trace.append('speak')
            return b'\xff' * 160
        self.provider.transcribe = transcribe
        self.provider.embed = embed
        self.store.retrieve = retrieve
        self.provider.answer = answer
        self.provider.speak = speak
        sid, stream = 'CA' + '1' * 32, 'MZ' + '5' * 32
        self.callback('/twilio/voice/1', {'CallSid': sid, 'AnsweredBy': 'human'})
        path = '/twilio/stream/1'
        sig = base64.b64encode(hmac.new(
            b'test-token', (self.settings.public_base + path).encode(), hashlib.sha1
        ).digest()).decode()
        with self.client.websocket_connect(path, headers={'X-Twilio-Signature': sig}) as ws:
            ws.send_json({'event': 'start', 'streamSid': stream, 'start': {
                'streamSid': stream, 'callSid': sid, 'mediaFormat': {
                    'encoding': 'audio/x-mulaw', 'sampleRate': 8000, 'channels': 1}}})
            self.assertEqual(ws.receive_json()['event'], 'media')
            voice = audioop.lin2ulaw(b'\x00\x20' * 160, 2)
            for sequence, audio in enumerate([voice] + [b'\xff' * 160] * 30, 1):
                ws.send_json({'event': 'media', 'sequenceNumber': str(sequence),
                    'streamSid': stream, 'media': {'track': 'inbound',
                    'payload': base64.b64encode(audio).decode()}})
            frame = ws.receive_json()  # No stop/completion until the reply is observed.
            self.assertEqual(frame, {'event': 'media', 'streamSid': stream,
                'media': {'payload': base64.b64encode(b'\xff' * 160).decode()}})
            call = self.client.get('/calls/1', headers=self.headers).json()
            self.assertEqual(self.post('/calls', {'lead_id': 1, 'product_id': 1}).status_code, 409)
            self.assertEqual(call['status_final'], 'ligando')
            self.assertEqual([t['role'] for t in call['transcription_json']],
                             ['assistant', 'user', 'assistant'])
            self.assertEqual(call['transcription_json'][-1]['content'], 'Vamos conversar?')
            # Prepared introductions replay cached bytes; only the live reply invokes TTS.
            self.assertEqual(trace, ['transcribe', 'embed', 'retrieve', 'answer', 'speak'])
            self.assertEqual(self.provider.stopped, [])
            ws.send_json({'event': 'stop', 'sequenceNumber': '32',
                          'streamSid': stream, 'stop': {'callSid': sid}})
            with self.assertRaises(WebSocketDisconnect) as closed:
                ws.receive_json()
            self.assertEqual(closed.exception.code, 1000)
        self.assertEqual(self.post('/calls', {'lead_id': 1, 'product_id': 1}).status_code, 409)
        for _ in range(2):
            self.callback('/twilio/status/1', {'CallSid': sid, 'CallStatus': 'completed'})
        final = self.client.get('/calls/1', headers=self.headers).json()
        self.assertEqual(self.post('/calls', {'lead_id': 1, 'product_id': 1}).status_code, 201)
        self.assertEqual(len(final['transcription_json']), 3)
        self.assertEqual(self.provider.stopped, [sid])  # Explicit stop ends the media session.
