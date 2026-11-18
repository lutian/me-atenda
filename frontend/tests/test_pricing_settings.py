"""Settings: Twilio USD rates, sent as decimal strings."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

FRONTEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FRONTEND))
import test_call_policy as base
from client import APIClient, APIError


class FakeAPI(base.FakeAPI):
    def __init__(self):
        super().__init__()
        self.prices = {'source': 'none', 'version': None, 'twilio': {}}
        self.price_writes = []
    def pricing_settings(self): return dict(self.prices)
    def save_pricing_settings(self, twilio):
        from client import RATE  # Same rule as the real client.
        if not all(RATE.fullmatch(r) for r in twilio.values()): raise ValueError('Rates must be non-negative numbers')
        self.price_writes.append(twilio)
        self.prices = dict(self.prices, source='settings', version='painel-1', twilio=twilio)
        return dict(self.prices)


class ClientValidation(unittest.TestCase):
    def test_rates_must_be_non_negative_decimal_strings(self):
        api = APIClient('http://127.0.0.1:9', 'k')
        for twilio in ({'voice_per_minute': '-1'}, {'voice_per_minute': '1,5'}, {'voice_per_minute': 1}):
            with self.assertRaises(ValueError): api.save_pricing_settings(twilio)
        with self.assertRaises(APIError): APIClient._pricing({'source': 'none'})


@unittest.skipUnless(importlib.util.find_spec('streamlit'), 'Streamlit image required')
class PricingUI(unittest.TestCase):
    def setUp(self):
        from streamlit.testing.v1 import AppTest
        import client
        self.api = FakeAPI()
        factory = patch.object(client, 'APIClient', lambda *a: self.api)
        factory.start(); self.addCleanup(factory.stop)
        self.app = AppTest.from_file(str(FRONTEND / 'app.py'), default_timeout=5).run()
        self.app.sidebar.radio[0].set_value('Configurações').run()

    def test_saves_only_filled_twilio_rates(self):
        self.app.text_input(key='price-twilio-voice_per_minute').set_value(' 0.10 ')
        next(b for b in self.app.button if b.label == 'Salvar custos da Twilio').click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.api.price_writes, [{'voice_per_minute': '0.10'}])

    def test_saved_rates_are_prefilled_and_invalid_values_are_not_sent(self):
        self.api.prices.update(source='settings', twilio={'amd_per_call': '0.0075'})
        self.app.run()
        self.assertEqual(self.app.text_input(key='price-twilio-amd_per_call').value, '0.0075')
        self.app.text_input(key='price-twilio-voice_per_minute').set_value('-2')
        next(b for b in self.app.button if b.label == 'Salvar custos da Twilio').click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.api.price_writes, [])
        self.assertTrue(self.app.error)


if __name__ == '__main__': unittest.main()
