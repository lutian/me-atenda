"""Filler utterances (S2): "Alô?" mid-call is kept for audit but never answered nor sent to the LLM."""
import base64
import hashlib
import hmac
import unittest
from starlette.websockets import WebSocketDisconnect
from app.voice import is_filler
import test_api as fixtures


class Fillers(unittest.TestCase):
    def test_greeting_only_utterances_are_fillers(self):
        for text in ('Alô?', 'Alô? Alô?', 'alo', 'Oi.', 'Olá!', 'Oi, alô?', 'Hum...', 'Hello?', '  ALÔ  '):
            self.assertTrue(is_filler(text), text)

    def test_utterances_with_content_are_not_fillers(self):
        for text in ('Oi, sim. Alo.', 'Pode falar.', 'Sim, podemos.', 'Eu.', 'Não tenho interesse', 'Fim!', ''):
            self.assertFalse(is_filler(text), text)


class FillerConversation(unittest.TestCase):
    setUp = fixtures.API.setUp
    seed = fixtures.API.seed
    post = fixtures.API.post
    callback = fixtures.API.callback

    def test_filler_is_recorded_but_not_answered_nor_sent_to_llm(self):
        self.assertEqual(self.seed().status_code, 201)
        heard = iter(['Alô? Alô?', 'Pode falar.'])
        histories, spoken = [], []
        async def transcribe(pcm): return next(heard)
        async def answer(history, context, agent_name='Bia', scripts=()):
            histories.append([dict(turn) for turn in history])
            return {'text': 'Cuida da parte técnica?', 'outcome': None, 'script': None}
        async def speak(text, voice=None):
            spoken.append(text)
            return b'\xff' * 160
        self.provider.transcribe = transcribe; self.provider.answer = answer; self.provider.speak = speak
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
            for _ in range(2):
                for payload in [voiced] + [silent]*30:
                    sequence += 1
                    ws.send_json({'event': 'media', 'sequenceNumber': str(sequence), 'streamSid': stream,
                                  'media': {'track': 'inbound', 'payload': payload}})
            ws.receive_json()  # Only the real utterance produces reply audio.
            ws.send_json({'event': 'stop', 'sequenceNumber': str(sequence+1), 'streamSid': stream,
                          'stop': {'callSid': 'CA'+'1'*32}})
            with self.assertRaises(WebSocketDisconnect): ws.receive_json()
        call = self.client.get('/calls/1', headers=self.headers).json()
        self.assertEqual([(t['role'], t['content']) for t in call['transcription_json']][1:],
                         [('user', 'Alô? Alô?'), ('user', 'Pode falar.'), ('assistant', 'Cuida da parte técnica?')])
        self.assertEqual(len(histories), 1)
        self.assertEqual([t['content'] for t in histories[0] if t['role'] == 'user'], ['Pode falar.'])
        self.assertEqual(spoken, ['Cuida da parte técnica?'])
        self.assertNotEqual(call['status_final'], 'falha_tecnica')


if __name__ == '__main__': unittest.main()
