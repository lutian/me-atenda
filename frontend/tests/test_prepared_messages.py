"""Prepared messages UI/client: synthetic data, no provider calls."""
import importlib.util
import json
from contextlib import contextmanager
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

FRONTEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FRONTEND))
from client import APIClient, APIError

PITCH = 'Integramos cartórios. Você cuida da parte técnica?'
GOODBYE = 'Entendi, obrigado pelo seu tempo.'
OPENING = 'Olá, aqui é a Bia, da Cartória. Falo com o responsável técnico?'


class ClientMessages(unittest.TestCase):
    def test_fixed_paths_payloads_and_local_validation(self):
        calls = []
        api = APIClient('http://synthetic.local', 'synthetic-key')
        bodies = [{'opening': None, 'pitch': None, 'goodbye': None, 'ready': False},
                  {'opening': OPENING, 'pitch': PITCH, 'goodbye': GOODBYE},
                  {'opening': OPENING, 'pitch': PITCH, 'goodbye': GOODBYE, 'ready': True}]
        @contextmanager
        def response(request, timeout):
            calls.append((request, timeout))
            body = json.dumps(bodies[len(calls) - 1]).encode()
            class Result:
                def read(self): return body
            yield Result()
        api._http.open = response
        self.assertEqual(api.product_messages(2), bodies[0])
        self.assertEqual(api.draft_messages(2), bodies[1])
        self.assertEqual(api.save_messages(2, OPENING, f' {PITCH} ', GOODBYE), bodies[2])
        self.assertEqual([(r.method, r.full_url) for r, _ in calls],
                         [('GET', 'http://synthetic.local/products/2/messages'),
                          ('POST', 'http://synthetic.local/products/2/messages/draft'),
                          ('PUT', 'http://synthetic.local/products/2/messages')])
        self.assertEqual(json.loads(calls[2][0].data), {'opening': OPENING, 'pitch': PITCH, 'goodbye': GOODBYE})
        self.assertGreaterEqual(calls[2][1], 60)  # Four syntheses can exceed the default timeout.
        for opening, pitch, goodbye in (('', PITCH, GOODBYE), ('x'*401, PITCH, GOODBYE), (None, PITCH, GOODBYE),
                                        (OPENING, '', GOODBYE), (OPENING, 'x'*401, GOODBYE), (OPENING, PITCH, ''),
                                        (OPENING, PITCH, 'x'*201), (OPENING, None, GOODBYE)):
            with self.assertRaises(ValueError): api.save_messages(2, opening, pitch, goodbye)
        with self.assertRaises(ValueError): api.product_messages(None)
        self.assertEqual(len(calls), 3)

    def test_invalid_status_is_rejected(self):
        api = APIClient('http://synthetic.local', 'synthetic-key')
        @contextmanager
        def response(request, timeout):
            class Result:
                def read(self): return b'{"opening":"o","pitch":"x","goodbye":"y"}'
            yield Result()
        api._http.open = response
        with self.assertRaises(APIError): api.product_messages(1)


class FakeAPI:
    def __init__(self):
        self.status = {'opening': None, 'pitch': None, 'goodbye': None, 'ready': False}
        self.saved = []; self.dispatched = []; self.drafts = 0
    def list(self, resource):
        if resource == 'leads': return [{'id': 1, 'name': 'Ana', 'phone': '+5511999999999', 'company': ''}]
        if resource == 'products': return [{'id': 1, 'name': 'Cartória', 'description': 'D'}]
        return []
    def product_messages(self, id): return dict(self.status)
    def draft_messages(self, id):
        self.drafts += 1
        return {'opening': OPENING, 'pitch': PITCH, 'goodbye': GOODBYE}
    def save_messages(self, id, opening, pitch, goodbye):
        self.saved.append((id, opening, pitch, goodbye))
        self.status = {'opening': opening, 'pitch': pitch, 'goodbye': goodbye, 'ready': True}
        return dict(self.status)
    def dispatch(self, lead_id, product_id):
        self.dispatched.append((lead_id, product_id)); return {'id': 1}
    def call(self, call_id): return {'id': call_id, 'status_final': 'ligando', 'transcription_json': []}


@unittest.skipUnless(importlib.util.find_spec('streamlit'), 'Streamlit image required')
class DispatchUI(unittest.TestCase):
    def setUp(self):
        from streamlit.testing.v1 import AppTest
        import client
        self.api = FakeAPI()
        factory = patch.object(client, 'APIClient', lambda *a: self.api)
        factory.start(); self.addCleanup(factory.stop)
        self.app = AppTest.from_file(str(FRONTEND / 'app.py'), default_timeout=5).run()
        self.app.sidebar.radio[0].set_value('Leads').run()
        self.app.selectbox(key='dispatch-lead').set_value(1)
        self.app.selectbox(key='dispatch-product').set_value(1).run()
        self.app.checkbox[0].check().run()

    def call_button(self):
        return next(b for b in self.app.button if b.label == 'Iniciar UMA ligação')

    def test_call_is_blocked_until_reviewed_messages_are_generated(self):
        self.assertFalse(self.app.exception)
        self.assertTrue(self.call_button().disabled)
        self.app.button(key='draft-1').click().run()
        self.assertEqual(self.api.drafts, 1)
        self.assertEqual(self.app.text_area(key='opening-1').value, OPENING)
        self.assertEqual(self.app.text_area(key='pitch-1').value, PITCH)
        self.assertEqual(self.app.text_area(key='goodbye-1').value, GOODBYE)
        self.assertEqual(self.api.saved, [])  # Draft is never saved without confirmation.
        self.assertTrue(self.call_button().disabled)
        self.app.text_area(key='opening-1').set_value('Abertura revisada?')
        self.app.text_area(key='pitch-1').set_value('Pitch revisado?').run()
        self.app.button(key='prepare-1').click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.api.saved, [(1, 'Abertura revisada?', 'Pitch revisado?', GOODBYE)])
        self.assertTrue(self.app.success)
        # The rerun after saving asks for consent again before the real call.
        self.app.checkbox[0].check().run()
        self.assertFalse(self.call_button().disabled)
        self.call_button().click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.api.dispatched, [(1, 1)])

    def test_previously_prepared_messages_are_preloaded_and_ready(self):
        from streamlit.testing.v1 import AppTest
        self.api.status = {'opening': OPENING, 'pitch': PITCH, 'goodbye': 'Até logo.', 'ready': True}
        # A fresh session, like reopening the Leads screen later.
        app = AppTest.from_file(str(FRONTEND / 'app.py'), default_timeout=5).run()
        app.sidebar.radio[0].set_value('Leads').run()
        app.selectbox(key='dispatch-lead').set_value(1)
        app.selectbox(key='dispatch-product').set_value(1).run()
        self.assertEqual(app.text_area(key='opening-1').value, OPENING)
        self.assertEqual(app.text_area(key='pitch-1').value, PITCH)
        self.assertEqual(app.text_area(key='goodbye-1').value, 'Até logo.')
        self.assertTrue(any('Áudios prontos' in s.value for s in app.success))
        app.checkbox[0].check().run()
        self.assertFalse(next(b for b in app.button if b.label == 'Iniciar UMA ligação').disabled)
        self.assertEqual(self.api.saved, [])  # Nothing regenerated.

    def test_saved_messages_are_loaded_and_goodbye_defaults(self):
        self.assertEqual(self.app.text_area(key='opening-1').value, '')
        self.assertEqual(self.app.text_area(key='pitch-1').value, '')
        self.assertEqual(self.app.text_area(key='goodbye-1').value, GOODBYE)


if __name__ == '__main__': unittest.main()
