"""Model catalog (S1-S3): models per option with their own costs; Settings picks the model in use."""
import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.providers import Providers as RealProviders
import test_api as fixtures

SOL = {'name': 'gpt-6.2-sol', 'kind': 'text',
       'costs': {'input_per_million': '2.00', 'cached_input_per_million': '0.10', 'output_per_million': '10.00'}}


def settings():
    return Settings(admin_key='local-test-key', public_base='https://voice.example/prefix',
                    twilio_token='test-token', account_sid='AC' + '2' * 32)


class ModelCatalog(unittest.TestCase):
    setUp = fixtures.API.setUp
    post = fixtures.API.post

    def models(self):
        result = self.client.get('/models', headers=self.headers)
        self.assertEqual(result.status_code, 200)
        return {m['name']: m for m in result.json()}

    def selection(self):
        return self.client.get('/settings/models', headers=self.headers).json()

    def test_environment_models_are_the_initial_selection(self):
        models = self.models()
        self.assertEqual({n: (m['kind'], m['active']) for n, m in models.items()},
                         {'gpt-4o-mini': ('text', True), 'tts-1': ('audio', True),
                          'text-embedding-3-small': ('embedding', True)})
        self.assertEqual(models['tts-1']['costs'], {'characters_per_million': None})
        self.assertEqual({k: v['name'] for k, v in self.selection().items()},
                         {'text': 'gpt-4o-mini', 'audio': 'tts-1', 'embedding': 'text-embedding-3-small'})
        self.assertEqual(self.client.get('/models').status_code, 401)

    def test_create_update_and_delete_models_with_costs(self):
        created = self.post('/models', SOL)
        self.assertEqual(created.status_code, 201)
        self.assertEqual({k: created.json()[k] for k in ('name', 'kind', 'costs', 'active')},
                         {'name': 'gpt-6.2-sol', 'kind': 'text', 'costs': SOL['costs'], 'active': False})
        id = created.json()['id']
        self.assertEqual(self.post('/models', SOL).status_code, 409)  # Unique name.
        updated = self.client.patch(f'/models/{id}', json={'costs': {'input_per_million': '1.50',
            'cached_input_per_million': None, 'output_per_million': '8'}}, headers=self.headers)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()['costs'], {'input_per_million': '1.50', 'cached_input_per_million': None,
                                                   'output_per_million': '8'})
        self.assertEqual(self.client.patch(f'/models/{id}', json={'name': 'gpt-6.2-sol-mini'},
                                           headers=self.headers).json()['name'], 'gpt-6.2-sol-mini')
        self.assertEqual(self.client.delete(f'/models/{id}', headers=self.headers).status_code, 204)
        self.assertNotIn('gpt-6.2-sol-mini', self.models())
        self.assertEqual(self.client.delete(f'/models/{id}', headers=self.headers).status_code, 404)

    def test_invalid_models_are_rejected(self):
        for body in ({'name': 'x', 'kind': 'video', 'costs': {}},
                     {'name': 'bad name!', 'kind': 'text', 'costs': {}},
                     {'name': 'm', 'kind': 'text', 'costs': {'input_per_million': '-1'}},
                     {'name': 'm', 'kind': 'text', 'costs': {'input_per_million': 2}},
                     {'name': 'm', 'kind': 'text', 'costs': {'characters_per_million': '1'}},
                     {'name': 'm', 'kind': 'audio', 'costs': {'output_per_million': '1'}},
                     {'name': 'm', 'kind': 'embedding', 'costs': {}, 'other': 1}):
            self.assertEqual(self.post('/models', body).status_code, 422, body)
        self.assertEqual(set(self.models()), {'gpt-4o-mini', 'tts-1', 'text-embedding-3-small'})

    def test_model_in_use_cannot_be_deleted_nor_change_kind(self):
        text = self.models()['gpt-4o-mini']['id']
        self.assertEqual(self.client.delete(f'/models/{text}', headers=self.headers).status_code, 409)
        self.assertEqual(self.client.patch(f'/models/{text}', json={'kind': 'audio'},
                                           headers=self.headers).status_code, 409)
        self.assertIn('gpt-4o-mini', self.models())

    def test_select_models_per_option(self):
        sol = self.post('/models', SOL).json()['id']
        hd = self.post('/models', {'name': 'tts-1-hd', 'kind': 'audio', 'costs': {'characters_per_million': '30'}}).json()['id']
        embedding = self.models()['text-embedding-3-small']['id']
        result = self.client.put('/settings/models', json={'text': sol, 'audio': hd, 'embedding': embedding},
                                 headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual({k: v['name'] for k, v in result.json().items()},
                         {'text': 'gpt-6.2-sol', 'audio': 'tts-1-hd', 'embedding': 'text-embedding-3-small'})
        self.assertEqual((self.settings.chat_model, self.settings.tts_model), ('gpt-6.2-sol', 'tts-1-hd'))
        # Wrong option or unknown model: nothing changes.
        self.assertEqual(self.client.put('/settings/models', json={'text': hd, 'audio': hd, 'embedding': embedding},
                                         headers=self.headers).status_code, 422)
        self.assertEqual(self.client.put('/settings/models', json={'text': 999, 'audio': hd, 'embedding': embedding},
                                         headers=self.headers).status_code, 404)
        self.assertEqual(self.selection()['text']['name'], 'gpt-6.2-sol')
        # The selection survives a restart; the environment is only the initial default.
        restarted = TestClient(create_app(settings(), self.store, self.provider))
        self.assertEqual(restarted.get('/settings/models', headers=self.headers).json()['audio']['name'], 'tts-1-hd')

    def test_audio_model_change_requires_new_prepared_audio(self):
        self.assertEqual(self.post('/products', {'name': 'Produto', 'description': 'D'}).status_code, 201)
        body = {'opening': 'Olá?', 'pitch': 'Pitch', 'goodbye': 'Tchau.'}
        self.assertTrue(self.client.put('/products/1/messages', json=body, headers=self.headers).json()['ready'])
        hd = self.post('/models', {'name': 'tts-1-hd', 'kind': 'audio', 'costs': {}}).json()['id']
        ids = {k: v['id'] for k, v in self.selection().items()}
        self.assertEqual(self.client.put('/settings/models', json=dict(ids, audio=hd), headers=self.headers).status_code, 200)
        self.assertFalse(self.client.get('/products/1/messages', headers=self.headers).json()['ready'])


class CatalogPricing(unittest.TestCase):
    def test_selected_model_and_its_costs_price_new_events(self):
        store, s = fixtures.MemoryStore(), settings()
        provider = RealProviders(s)
        create = AsyncMock(return_value=NS(usage=NS(prompt_tokens=1000, completion_tokens=100, total_tokens=1100,
                                                    prompt_tokens_details=NS(cached_tokens=0)),
                                           choices=[NS(message=NS(content='{"text":"Olá","outcome":null}'))]))
        provider._openai = NS(chat=NS(completions=NS(create=create)))
        client = TestClient(create_app(s, store, provider))
        headers = {'X-API-Key': 'local-test-key'}
        sol = client.post('/models', json=SOL, headers=headers).json()['id']
        ids = {k: v['id'] for k, v in client.get('/settings/models', headers=headers).json().items()}
        self.assertEqual(client.put('/settings/models', json=dict(ids, text=sol), headers=headers).status_code, 200)
        asyncio.run(provider.answer([], ''))
        self.assertEqual(create.await_args.kwargs['model'], 'gpt-6.2-sol')
        self.assertEqual(store.events[-1]['model'], 'gpt-6.2-sol')
        self.assertEqual(store.events[-1]['estimated_cost_usd'], '0.003')  # 1000 × 2 + 100 × 10 per million.
        # Editing the cost prices the next event; the previous one keeps its snapshot.
        client.patch(f'/models/{sol}', json={'costs': {'input_per_million': '1', 'cached_input_per_million': '0.1',
                                                       'output_per_million': '10'}}, headers=headers)
        asyncio.run(provider.answer([], ''))
        self.assertEqual([e['estimated_cost_usd'] for e in store.events[-2:]], ['0.003', '0.002'])


if __name__ == '__main__': unittest.main()
