"""Scripted replies (S3): prepared pitch/goodbye audio is played without live synthesis."""
import base64
import hashlib
import hmac
import unittest
from starlette.websockets import WebSocketDisconnect
from app.voice import classify_reply
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
            self.assertEqual(self.client.put('/products/1/messages', json={'opening': 'Olá, aqui é a Bia. Posso falar com você?', 'pitch': PITCH, 'goodbye': GOODBYE},
                                             headers=self.headers).status_code, 200)
        self.assertEqual(self.post('/calls', {'lead_id': 1, 'product_id': 1}).status_code, 201)
        spoken, offered = [], []
        original = self.provider.speak
        async def speak(text, voice=None):
            spoken.append(text)
            return await original(text, voice)
        async def answer(history, context, agent_name='Bia', scripts=(), goal=None):
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


    def heard(self, text):
        async def transcribe(pcm): return text
        embeds = []
        async def embed(texts):
            embeds.append(texts); return [[0.0] * 1536 for _ in texts]
        self.provider.transcribe = transcribe; self.provider.embed = embed
        return embeds

    def test_obvious_acceptance_plays_pitch_without_llm(self):
        spoken, offered, call = None, None, None
        self.heard('Sim, pode falar.')
        spoken, offered, call = self.converse([])
        self.assertEqual(offered, [])  # The LLM was never asked.
        self.assertEqual(call['transcription_json'][-1], {'role': 'assistant', 'content': PITCH})
        self.assertEqual(spoken, [])

    def test_obvious_refusal_and_wrong_number_play_goodbye_without_llm(self):
        for text, outcome in (('Não tenho interesse.', 'sem_interesse'), ('É engano, número errado.', 'numero_errado')):
            with self.subTest(text=text):
                self.setUp()
                self.heard(text)
                spoken, offered, call = self.converse([])
                self.assertEqual(offered, [])
                self.assertEqual(call['transcription_json'][-1], {'role': 'assistant', 'content': GOODBYE})
                self.assertEqual(call['status_final'], outcome)
                self.assertEqual(spoken, [])

    def test_unclear_reply_goes_to_llm(self):
        self.heard('Por que sim?')
        spoken, offered, call = self.converse([{'text': 'Para apresentar o produto.', 'outcome': None, 'script': None}])
        self.assertEqual(offered, [['pitch', 'goodbye']])
        self.assertEqual(spoken, ['Para apresentar o produto.'])

    def test_rule_applies_only_to_the_opening_question(self):
        # After the pitch, "não" may answer anything; only the LLM can judge it.
        answers = iter(['Pode falar.', 'Não.'])
        async def transcribe(pcm): return next(answers)
        self.provider.transcribe = transcribe
        spoken, offered, call = self.converse([{'text': 'Quem cuida disso?', 'outcome': None, 'script': None}],
                                              utterances=2)
        self.assertEqual(offered, [['goodbye']])
        self.assertEqual(spoken, ['Quem cuida disso?'])

    def test_calls_without_prepared_messages_keep_using_llm(self):
        self.heard('Sim, pode falar.')
        spoken, offered, call = self.converse([{'text': 'Posso explicar?', 'outcome': None, 'script': None}],
                                              prepared=False)
        self.assertEqual(offered, [[]])


class ReplyRule(unittest.TestCase):
    def test_clear_answers(self):
        cases = {'yes': ['Sim', 'Sim, pode falar.', 'Pode falar.', 'Pois não.', 'Claro!', 'Sou eu.',
                         'É comigo.', 'Pode sim', 'Oi, sim. Alô.', 'Sim, podemos.', 'Tudo bem, fala.'],
                 'no': ['Não.', 'Não tenho interesse.', 'Não, obrigado.', 'Agora não.', 'Sem interesse',
                        'Não quero.'],
                 'wrong': ['Número errado.', 'É engano.', 'Ligou errado.', 'Não, número errado.']}
        for verdict, texts in cases.items():
            for text in texts:
                self.assertEqual(classify_reply(text), verdict, text)

    def test_unclear_answers_go_to_llm(self):
        for text in ('Por que sim?', 'Quem fala?', 'Não conheço essa empresa.', 'Não sou eu.', 'Sim, mas agora não posso.', 'Não sou eu, é o João.',
                     'Eu.', 'Tenho interesse', 'Assim não dá pra falar', 'Fim!', '',
                     'Sim, mas antes preciso entender melhor o que vocês fazem e quanto custa isso'):
            self.assertIsNone(classify_reply(text), text)


if __name__ == '__main__': unittest.main()
