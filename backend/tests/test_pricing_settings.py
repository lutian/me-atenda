"""Rates per model and Twilio edited in Settings; new events and calls are priced with them."""
import asyncio
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from app.main import create_app
from app.providers import Providers as RealProviders
import test_api as fixtures

RATES = {'models': {'gpt-4o-mini': {'input_per_million': '0.15', 'cached_input_per_million': '0.075',
                                    'output_per_million': '0.60'},
                    'text-embedding-3-small': {'input_per_million': '0.02'},
                    'tts-1': {'characters_per_million': '15'}},
         'twilio': {'voice_per_minute': '0.10', 'amd_per_call': '0.0075'}}
MODELS = {'chat': 'gpt-4o-mini', 'embedding': 'text-embedding-3-small', 'tts': 'tts-1'}


class PricingSettings(unittest.TestCase):
    setUp = fixtures.API.setUp
    seed = fixtures.API.seed
    post = fixtures.API.post

    def pricing(self, client=None):
        result = (client or self.client).get('/settings/pricing', headers=self.headers)
        self.assertEqual(result.status_code, 200)
        return result.json()

    def save(self, body):
        return self.client.put('/settings/pricing', json=body, headers=self.headers)

    def test_default_is_empty_and_lists_the_configured_models(self):
        self.assertEqual(self.pricing(), {'source': 'none', 'version': None, 'models': {}, 'twilio': {},
                                          'configured_models': MODELS})
        self.assertEqual(self.client.get('/settings/pricing').status_code, 401)
        self.assertEqual(self.client.put('/settings/pricing', json=RATES).status_code, 401)

    def test_save_versions_rates_and_new_calls_use_them(self):
        result = self.save(RATES)
        self.assertEqual(result.status_code, 200)
        saved = result.json()
        self.assertEqual((saved['source'], saved['models'], saved['twilio']), ('settings', RATES['models'], RATES['twilio']))
        self.assertRegex(saved['version'], r'^painel-\d{8}T\d{6}Z$')
        self.assertEqual(self.pricing(), saved)
        self.assertEqual(self.seed().status_code, 201)
        snapshot = self.store.snapshots[1]
        self.assertEqual((snapshot['version'], snapshot['twilio']), (saved['version'], RATES['twilio']))
        # A restarted backend keeps the rates saved in the panel.
        restarted = TestClient(create_app(self.settings, self.store, self.provider))
        self.assertEqual(self.pricing(restarted), saved)

    def test_invalid_rates_are_rejected_and_nothing_changes(self):
        self.assertEqual(self.save(RATES).status_code, 200)
        before = self.pricing()
        for body in ({'models': {'gpt-4o-mini': {'input_per_million': '-1'}}, 'twilio': {}},
                     {'models': {'gpt-4o-mini': {'input_per_million': 'abc'}}, 'twilio': {}},
                     {'models': {'gpt-4o-mini': {'input_per_million': 0.15}}, 'twilio': {}},
                     {'models': {'gpt-4o-mini': {'per_call': '1'}}, 'twilio': {}},
                     {'models': {}, 'twilio': {'per_sms': '1'}},
                     {'models': {}, 'twilio': {}, 'version': 'x'},
                     {'models': {'bad model!': {'input_per_million': '1'}}, 'twilio': {}}):
            self.assertEqual(self.save(body).status_code, 422, body)
        self.assertEqual(self.pricing(), before)

    def test_environment_pricing_is_the_initial_default_until_saved(self):
        self.settings.observability_pricing_json = json.dumps({'version': 'env-v1', 'currency': 'USD',
                                                               'models': {'tts-1': {'characters_per_million': '15'}}})
        client = TestClient(create_app(self.settings, self.store, self.provider))
        self.assertEqual(self.pricing(client)['source'], 'environment')
        self.assertEqual(self.pricing(client)['version'], 'env-v1')
        self.assertEqual(client.put('/settings/pricing', json=RATES, headers=self.headers).status_code, 200)
        self.assertEqual(self.pricing(client)['source'], 'settings')

    def test_clearing_all_rates_is_allowed(self):
        self.assertEqual(self.save(RATES).status_code, 200)
        cleared = self.save({'models': {}, 'twilio': {}}).json()
        self.assertEqual((cleared['source'], cleared['models'], cleared['twilio']), ('settings', {}, {}))


class PricedEvents(unittest.TestCase):
    def test_saved_rates_price_the_next_provider_events(self):
        store = fixtures.MemoryStore()
        from app.config import Settings
        s = Settings(admin_key='local-test-key', public_base='https://voice.example/prefix',
                     twilio_token='test-token', account_sid='AC' + '2' * 32)
        provider = RealProviders(s)
        provider._openai = NS(chat=NS(completions=NS(create=AsyncMock(return_value=NS(
            usage=NS(prompt_tokens=1000, completion_tokens=100, total_tokens=1100,
                     prompt_tokens_details=NS(cached_tokens=0)),
            choices=[NS(message=NS(content='{"text":"Olá","outcome":null}'))])))))
        client = TestClient(create_app(s, store, provider))
        headers = {'X-API-Key': 'local-test-key'}
        asyncio.run(provider.answer([], ''))
        self.assertIsNone(store.events[-1]['estimated_cost_usd'])  # No rates yet.
        self.assertEqual(client.put('/settings/pricing', json=RATES, headers=headers).status_code, 200)
        asyncio.run(provider.answer([], ''))
        # 1000 × 0.15 + 100 × 0.60 per million tokens.
        self.assertEqual(store.events[-1]['estimated_cost_usd'], '0.00021')


if __name__ == '__main__': unittest.main()
