"""Prepared messages (S1): LLM-drafted pitch reviewed by the operator, audio generated before dialing."""
import unittest
import test_api as fixtures

GOODBYE = 'Entendi, obrigado pelo seu tempo.'
PITCH = ('Queremos integrar o Produto ao sistema de vocês para processar transcrições. '
         'Você é responsável pela parte técnica?')


class PreparedMessages(unittest.TestCase):
    setUp = fixtures.API.setUp
    post = fixtures.API.post
    callback = fixtures.API.callback

    def product(self):
        self.assertEqual(self.post('/products', {'name': 'Produto', 'description': 'Integração de cartórios'}).status_code, 201)

    def messages(self, id=1):
        result = self.client.get(f'/products/{id}/messages', headers=self.headers)
        self.assertEqual(result.status_code, 200)
        return result.json()

    def save(self, body, id=1):
        return self.client.put(f'/products/{id}/messages', json=body, headers=self.headers)

    def spy(self):
        spoken = []
        original = self.provider.speak
        async def speak(text, voice=None):
            spoken.append((text, voice))
            return await original(text, voice)
        self.provider.speak = speak
        return spoken

    def test_draft_uses_product_and_agent_without_persisting(self):
        self.product()
        seen = []
        async def draft(name, description, agent_name):
            seen.append((name, description, agent_name))
            return PITCH
        self.provider.draft = draft
        result = self.client.post('/products/1/messages/draft', headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), {'pitch': PITCH, 'goodbye': GOODBYE})
        self.assertEqual(seen, [('Produto', 'Integração de cartórios', 'Bia')])
        self.assertEqual(self.messages(), {'pitch': None, 'goodbye': None, 'ready': False})
        self.assertEqual(self.client.post('/products/9/messages/draft', headers=self.headers).status_code, 404)
        self.assertEqual(self.client.post('/products/1/messages/draft').status_code, 401)

    def test_draft_provider_failure_is_503_and_nonmutating(self):
        self.product()
        async def draft(*args): raise RuntimeError('provider offline https://secret')
        self.provider.draft = draft
        result = self.client.post('/products/1/messages/draft', headers=self.headers)
        self.assertEqual(result.status_code, 503)
        self.assertNotIn('secret', result.text)
        self.assertEqual(self.messages()['pitch'], None)

    def test_save_generates_all_four_audios_once_with_agent_voice(self):
        self.product()
        self.assertEqual(self.client.patch('/settings/agent', json={'agent_name': 'Bia', 'tts_voice': 'coral'},
                                           headers=self.headers).status_code, 200)
        spoken = self.spy()
        result = self.save({'pitch': PITCH, 'goodbye': GOODBYE})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), {'pitch': PITCH, 'goodbye': GOODBYE, 'ready': True})
        opening = ('Olá, meu nome é Bia, sou Representante de Produto, gostaria de apresentar para vocês '
                   'o nosso produto. É com você que posso falar?')
        self.assertEqual(spoken, [('Alô?', 'coral'), (opening, 'coral'), (PITCH, 'coral'), (GOODBYE, 'coral')])
        self.assertEqual(self.messages(), result.json())
        # Saving again and dialing reuse the prepared audio: no new synthesis.
        self.assertEqual(self.save({'pitch': PITCH, 'goodbye': GOODBYE}).status_code, 200)
        self.assertEqual(self.post('/leads', {'name': 'Ana', 'phone': '+5511999999999'}).status_code, 201)
        self.assertEqual(self.post('/calls', {'lead_id': 1, 'product_id': 1}).status_code, 201)
        self.assertEqual(len(spoken), 4)

    def test_invalid_bodies_are_rejected_without_synthesis(self):
        self.product()
        spoken = self.spy()
        for body in ({}, {'pitch': PITCH}, {'pitch': '', 'goodbye': GOODBYE}, {'pitch': 'x'*401, 'goodbye': GOODBYE},
                     {'pitch': PITCH, 'goodbye': 'x'*201}, {'pitch': PITCH, 'goodbye': GOODBYE, 'other': 1}):
            self.assertEqual(self.save(body).status_code, 422, body)
        self.assertEqual(self.save({'pitch': PITCH, 'goodbye': GOODBYE}, id=9).status_code, 404)
        self.assertEqual(self.client.put('/products/1/messages', json={'pitch': PITCH, 'goodbye': GOODBYE}).status_code, 401)
        self.assertEqual(spoken, [])
        self.assertEqual(self.messages()['pitch'], None)

    def test_voice_change_and_product_edit_invalidate_readiness(self):
        self.product()
        self.assertEqual(self.save({'pitch': PITCH, 'goodbye': GOODBYE}).status_code, 200)
        self.assertEqual(self.client.patch('/settings/agent', json={'agent_name': 'Pedro', 'tts_voice': 'onyx'},
                                           headers=self.headers).status_code, 200)
        self.assertEqual(self.messages(), {'pitch': PITCH, 'goodbye': GOODBYE, 'ready': False})
        self.assertEqual(self.client.patch('/products/1', json={'description': 'Nova descrição'},
                                           headers=self.headers).status_code, 200)
        self.assertEqual(self.messages(), {'pitch': None, 'goodbye': None, 'ready': False})

    def test_synthesis_failure_keeps_texts_but_not_ready(self):
        self.product()
        async def speak(text, voice=None):
            if text == GOODBYE: raise RuntimeError('tts offline')
            return b'\xff' * 160
        self.provider.speak = speak
        result = self.save({'pitch': PITCH, 'goodbye': GOODBYE})
        self.assertEqual(result.status_code, 503)
        self.assertEqual(self.messages(), {'pitch': PITCH, 'goodbye': GOODBYE, 'ready': False})


if __name__ == '__main__': unittest.main()
