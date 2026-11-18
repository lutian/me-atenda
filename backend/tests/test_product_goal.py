"""Call goal per product: stored with the product, steers drafts and replies, and defines success."""
import base64
import hashlib
import hmac
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from starlette.websockets import WebSocketDisconnect
from app.config import Settings
from app.providers import Providers
import test_api as fixtures

GOAL = 'Agendar uma demonstração de 30 minutos com o responsável técnico.'


class ProductGoalAPI(unittest.TestCase):
    setUp = fixtures.API.setUp
    seed = fixtures.API.seed
    post = fixtures.API.post
    callback = fixtures.API.callback

    def patch(self, body, id=1):
        return self.client.patch(f'/products/{id}', json=body, headers=self.headers)

    def test_goal_is_optional_saved_updated_and_cleared(self):
        created = self.post('/products', {'name': 'Produto', 'description': 'Descrição', 'goal': f'  {GOAL}  '})
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()['goal'], GOAL)
        without = self.post('/products', {'name': 'Outro', 'description': 'Descrição'})
        self.assertEqual(without.status_code, 201)
        self.assertIsNone(without.json()['goal'])
        # Editing other fields keeps the goal; an empty goal clears it.
        self.assertEqual(self.patch({'name': 'Produto 2'}).json()['goal'], GOAL)
        self.assertEqual(self.patch({'goal': 'Vender o plano anual.'}).json()['goal'], 'Vender o plano anual.')
        self.assertIsNone(self.patch({'goal': '  '}).json()['goal'])
        self.assertEqual(self.patch({'goal': 'x' * 501}).status_code, 422)
        self.assertEqual(self.post('/products', {'name': 'P', 'description': 'D', 'goal': 'x' * 501}).status_code, 422)
        listed = {p['id']: p['goal'] for p in self.client.get('/products', headers=self.headers).json()}
        self.assertEqual(listed, {1: None, 2: None})

    def test_draft_receives_the_goal(self):
        self.assertEqual(self.post('/products', {'name': 'Produto', 'description': 'Descrição', 'goal': GOAL}).status_code, 201)
        seen = []
        async def draft(name, description, agent_name, goal=None):
            seen.append(goal)
            return {'opening': 'Olá', 'pitch': 'Pitch'}
        self.provider.draft = draft
        self.assertEqual(self.client.post('/products/1/messages/draft', headers=self.headers).status_code, 200)
        self.assertEqual(seen, [GOAL])

    def test_conversation_replies_pursue_the_product_goal(self):
        self.assertEqual(self.seed().status_code, 201)
        self.assertEqual(self.patch({'goal': GOAL}).status_code, 200)
        goals = []
        async def transcribe(pcm): return 'Pode falar.'
        async def answer(history, context, agent_name='Bia', scripts=(), goal=None):
            goals.append(goal)
            return {'text': 'Podemos marcar amanhã?', 'outcome': None, 'script': None}
        self.provider.transcribe = transcribe; self.provider.answer = answer
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
            for payload in [voiced] + [silent]*30:
                sequence += 1
                ws.send_json({'event': 'media', 'sequenceNumber': str(sequence), 'streamSid': stream,
                              'media': {'track': 'inbound', 'payload': payload}})
            ws.receive_json()
            ws.send_json({'event': 'stop', 'sequenceNumber': str(sequence+1), 'streamSid': stream,
                          'stop': {'callSid': 'CA'+'1'*32}})
            with self.assertRaises(WebSocketDisconnect): ws.receive_json()
        self.assertEqual(goals, [GOAL])


class ProductGoalPrompts(unittest.IsolatedAsyncioTestCase):
    def provider(self, content):
        p = Providers(Settings())
        create = AsyncMock(return_value=NS(usage=None, choices=[NS(message=NS(content=content))]))
        p._openai = NS(chat=NS(completions=NS(create=create)))
        return p, create

    async def test_answer_prompt_defines_success_by_the_goal(self):
        p, create = self.provider('{"text":"Podemos marcar?","outcome":null}')
        await p.answer([], 'Contexto', 'Bia', (), GOAL)
        prompt = create.await_args.kwargs['messages'][0]['content']
        self.assertIn(f'Objetivo da ligação: {GOAL}', prompt)
        self.assertIn('Somente classifique sucesso se o cliente aceitou explicitamente o objetivo da ligação', prompt)
        self.assertNotIn('aceitou explicitamente um próximo passo', prompt)
        await p.answer([], 'Contexto', 'Bia')
        prompt = create.await_args.kwargs['messages'][0]['content']
        self.assertNotIn('Objetivo da ligação', prompt)
        self.assertIn('Somente classifique sucesso se o cliente aceitou explicitamente um próximo passo', prompt)

    async def test_draft_pitch_leads_to_the_goal(self):
        p, create = self.provider('{"opening":"Olá","pitch":"Pitch"}')
        await p.draft('Cartória', 'Integração', 'Bia', GOAL)
        messages = create.await_args.kwargs['messages']
        self.assertEqual(messages[1]['content'], f'Produto: Cartória\nDescrição: Integração\nObjetivo da ligação: {GOAL}')
        self.assertIn('objetivo da ligação', messages[0]['content'])
        await p.draft('Cartória', 'Integração', 'Bia')
        self.assertEqual(create.await_args.kwargs['messages'][1]['content'], 'Produto: Cartória\nDescrição: Integração')


if __name__ == '__main__': unittest.main()
