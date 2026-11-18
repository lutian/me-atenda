"""Twilio rates edited in Settings; model rates come from the model catalog."""
import json
import unittest
from fastapi.testclient import TestClient
from app.main import create_app
import test_api as fixtures

TWILIO = {'voice_per_minute': '0.10', 'amd_per_call': '0.0075'}


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

    def test_default_is_empty(self):
        self.assertEqual({k: v for k, v in self.pricing().items() if k != 'version'}, {'source': 'none', 'twilio': {}})
        self.assertEqual(self.client.get('/settings/pricing').status_code, 401)
        self.assertEqual(self.client.put('/settings/pricing', json={'twilio': TWILIO}).status_code, 401)

    def test_save_versions_rates_and_new_calls_use_them(self):
        saved = self.save({'twilio': TWILIO})
        self.assertEqual(saved.status_code, 200)
        saved = saved.json()
        self.assertEqual((saved['source'], saved['twilio']), ('settings', TWILIO))
        self.assertRegex(saved['version'], r'^painel-\d{8}T\d{6}Z$')
        self.assertEqual(self.seed().status_code, 201)
        snapshot = self.store.snapshots[1]
        self.assertEqual((snapshot['version'], snapshot['twilio']), (saved['version'], TWILIO))
        restarted = TestClient(create_app(self.settings, self.store, self.provider))
        self.assertEqual(self.pricing(restarted)['twilio'], TWILIO)

    def test_invalid_rates_are_rejected_and_nothing_changes(self):
        self.assertEqual(self.save({'twilio': TWILIO}).status_code, 200)
        before = self.pricing()
        for body in ({'twilio': {'voice_per_minute': '-1'}}, {'twilio': {'voice_per_minute': 'abc'}},
                     {'twilio': {'voice_per_minute': 0.1}}, {'twilio': {'per_sms': '1'}},
                     {'twilio': {}, 'models': {}}, {'twilio': {}, 'version': 'x'}):
            self.assertEqual(self.save(body).status_code, 422, body)
        self.assertEqual(self.pricing(), before)

    def test_environment_twilio_rates_are_the_initial_default_until_saved(self):
        self.settings.observability_pricing_json = json.dumps({'version': 'env-v1', 'currency': 'USD',
                                                               'twilio': {'voice_per_minute': '0.05'}})
        client = TestClient(create_app(self.settings, self.store, self.provider))
        self.assertEqual((self.pricing(client)['source'], self.pricing(client)['twilio']),
                         ('environment', {'voice_per_minute': '0.05'}))
        self.assertEqual(client.put('/settings/pricing', json={'twilio': TWILIO}, headers=self.headers).status_code, 200)
        self.assertEqual(self.pricing(client)['source'], 'settings')

    def test_clearing_all_rates_is_allowed(self):
        self.assertEqual(self.save({'twilio': TWILIO}).status_code, 200)
        cleared = self.save({'twilio': {}}).json()
        self.assertEqual((cleared['source'], cleared['twilio']), ('settings', {}))


if __name__ == '__main__': unittest.main()
