"""Agent voice/name settings (S4): persisted, validated, snapshotted per call, and spoken."""
import base64
import hashlib
import hmac
import unittest
from starlette.websockets import WebSocketDisconnect
import test_api as fixtures

TTS1_VOICES = ['alloy', 'ash', 'coral', 'echo', 'fable', 'onyx', 'nova', 'sage', 'shimmer']


class AgentProfile(unittest.TestCase):
    setUp = fixtures.API.setUp
    post = fixtures.API.post
    seed = fixtures.API.seed
    callback = fixtures.API.callback

    def profile(self):
        result = self.client.get('/settings/agent', headers=self.headers)
        self.assertEqual(result.status_code, 200)
        return result.json()

    def save(self, value):
        return self.client.patch('/settings/agent', json=value, headers=self.headers)

    def test_default_lists_model_voices_and_uses_deployment_voice(self):
        self.assertEqual(self.profile(), {'agent_name': 'Bia', 'tts_voice': 'alloy', 'voices': TTS1_VOICES})

    def test_saved_profile_and_rejected_updates_are_nonmutating(self):
        result = self.save({'agent_name': '  Pedro ', 'tts_voice': 'onyx'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), {'agent_name': 'Pedro', 'tts_voice': 'onyx', 'voices': TTS1_VOICES})
        for value in ({'agent_name': '', 'tts_voice': 'onyx'}, {'agent_name': 'x'*41, 'tts_voice': 'onyx'},
                      {'agent_name': 'Ana', 'tts_voice': 'verse'}, {'agent_name': 'Ana'},
                      {'agent_name': 'Ana', 'tts_voice': 'onyx', 'other': 1}, {'agent_name': None, 'tts_voice': 'onyx'}):
            self.assertEqual(self.save(value).status_code, 422, value)
        self.assertEqual(self.profile()['agent_name'], 'Pedro')
        self.assertEqual(self.profile()['tts_voice'], 'onyx')

    def test_profile_requires_admin(self):
        self.assertEqual(self.client.get('/settings/agent').status_code, 401)
        self.assertEqual(self.client.patch('/settings/agent', json={'agent_name': 'X', 'tts_voice': 'ash'}).status_code, 401)
        self.assertEqual(self.profile()['agent_name'], 'Bia')

    def test_call_policy_contract_is_unchanged(self):
        self.assertEqual(self.client.get('/settings/calls', headers=self.headers).json(), {'max_duration_seconds': 180})

    def test_call_speaks_with_snapshotted_name_and_voice(self):
        self.assertEqual(self.save({'agent_name': 'Pedro', 'tts_voice': 'onyx'}).status_code, 200)
        voices, names = [], []
        original = self.provider.speak
        async def speak(text, voice=None):
            voices.append(voice)
            return await original(text, voice)
        async def answer(history, context, agent_name='Bia', scripts=(), goal=None):
            names.append(agent_name)
            return {'text': 'Posso explicar?', 'outcome': None}
        self.provider.speak = speak; self.provider.answer = answer
        self.assertEqual(self.seed().status_code, 201)
        # Settings changed after reservation never alter the reserved call.
        self.assertEqual(self.save({'agent_name': 'Bia', 'tts_voice': 'coral'}).status_code, 200)
        self.assertEqual(voices, ['onyx', 'onyx'])  # Hello and opening prepared with the chosen voice.
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
            for sequence, payload in enumerate([voiced] + [silent]*30, start=2):
                ws.send_json({'event': 'media', 'sequenceNumber': str(sequence), 'streamSid': stream,
                              'media': {'track': 'inbound', 'payload': payload}})
            ws.receive_json()  # Live reply audio proves the answer was spoken.
            ws.send_json({'event': 'stop', 'sequenceNumber': '40', 'streamSid': stream,
                          'stop': {'callSid': 'CA'+'1'*32}})
            with self.assertRaises(WebSocketDisconnect): ws.receive_json()
        call = self.client.get('/calls/1', headers=self.headers).json()
        self.assertEqual(call['transcription_json'][0]['content'],
                         'Olá, meu nome é Pedro, sou Representante de Produto, gostaria de apresentar '
                         'para vocês o nosso produto. É com você que posso falar?')
        self.assertEqual(names, ['Pedro'])
        self.assertEqual(voices, ['onyx', 'onyx', 'onyx'])


if __name__ == '__main__': unittest.main()
