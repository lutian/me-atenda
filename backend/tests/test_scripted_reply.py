"""Scripted replies (S3): prepared pitch/goodbye audio is played without live synthesis."""
import base64
import hashlib
import hmac
import unittest
from starlette.websockets import WebSocketDisconnect
import test_api as fixtures

PITCH = 'Integramos cartórios ao seu sistema. Você cuida da parte técnica?'
GOODBYE = 'Entendi, obrigado pelo seu tempo.'


class ScriptedReply(unittest.TestCase):
    setUp = fixtures.API.setUp
    post = fixtures.API.post
    callback = fixtures.API.callback

    def converse(self, replies, prepared=True, utterances=1):
        """Dial, connect, send `utterances` lead turns; return (spoken, offered scripts, call row)."""
        self.assertEqual(self.post('/products', {'name': 'Produto', 'description': 'D'}).status_code, 201)
        self.assertEqual(self.post('/leads', {'name': 'Ana', 'phone': '+5511999999999'}).status_code, 201)
        if prepared:
            self.assertEqual(self.client.put('/products/1/messages', json={'pitch': PITCH, 'goodbye': GOODBYE},
                                             headers=self.headers).status_code, 200)
        self.assertEqual(self.post('/calls', {'lead_id': 1, 'product_id': 1}).status_code, 201)
        spoken, offered = [], []
        original = self.provider.speak
        async def speak(text, voice=None):
            spoken.append(text)
            return await original(text, voice)
        async def answer(history, context, agent_name='Bia', scripts=()):
            offered.append(list(scripts))
            return replies.pop(0)
        self.provider.speak = speak; self.provider.answer = answer
        self.callback('/twilio/voice/1', {'CallSid': 'CA'+'1'*32, 'AnsweredBy': 'human'})
        url = self.settings.public_base + '/twilio/stream/1'
        sig = base64.b64encode(hmac.new(b'test-token', url.encode(), hashlib.sha1).digest()).decode()
        stream = 'MZ'+'5'*32
        voiced = base64.b64encode(fixtures.voiced_frame()).decode()
        silent = base64.b64encode(b'\xff'*160).decode()
        with self.client.websocket_connect('/twilio/stream/1', headers={'X-Twilio-Signature': sig}) as ws:
            ws.send_json({'event': 'start', 'streamSid': stream, 'start': {
                'streamSid': stream, 'callSid': 'CA'+'1'*32,
                'mediaFormat': {'encoding': 'audio/x-mulaw', 'sampleRate': 8000, 'channels': 1}}})
            ws.receive_json()
            sequence = 1
            try:
                for _ in range(utterances):
                    for payload in [voiced] + [silent]*30:
                        sequence += 1
                        ws.send_json({'event': 'media', 'sequenceNumber': str(sequence), 'streamSid': stream,
                                      'media': {'track': 'inbound', 'payload': payload}})
                    ws.receive_json()  # Reply audio for this turn.
                ws.send_json({'event': 'stop', 'sequenceNumber': str(sequence+1), 'streamSid': stream,
                              'stop': {'callSid': 'CA'+'1'*32}})
                while True: ws.receive_json()
            except WebSocketDisconnect: pass
        return spoken, offered, self.client.get('/calls/1', headers=self.headers).json()

    def test_acceptance_plays_prepared_pitch_once_without_tts(self):
        spoken, offered, call = self.converse([{'text': '', 'outcome': None, 'script': 'pitch'},
                                               {'text': 'Posso mandar detalhes?', 'outcome': None, 'script': None}],
                                              utterances=2)
        self.assertEqual(offered, [['pitch', 'goodbye'], ['goodbye']])  # Pitch is offered only once.
        self.assertEqual([t['content'] for t in call['transcription_json'] if t['role'] == 'assistant'][1:],
                         [PITCH, 'Posso mandar detalhes?'])
        self.assertEqual(spoken, ['Posso mandar detalhes?'])  # Pitch came from the prepared audio.

    def test_refusal_plays_prepared_goodbye_and_hangs_up(self):
        spoken, _, call = self.converse([{'text': '', 'outcome': 'sem_interesse', 'script': 'goodbye'}])
        self.assertEqual(call['transcription_json'][-2:][1], {'role': 'assistant', 'content': GOODBYE})
        self.assertEqual(call['status_final'], 'sem_interesse')
        self.assertEqual(spoken, [])
        self.assertEqual(self.provider.stopped, ['CA'+'1'*32, 'CA'+'1'*32])

    def test_call_without_prepared_messages_offers_no_script(self):
        spoken, offered, call = self.converse([{'text': 'Posso explicar?', 'outcome': None, 'script': None}],
                                              prepared=False)
        self.assertEqual(offered, [[]])
        self.assertEqual(spoken, ['Posso explicar?'])


if __name__ == '__main__': unittest.main()
