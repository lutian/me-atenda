"""Settings: register AI models per option with costs, and pick the model in use for each option."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

FRONTEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FRONTEND))
import test_call_policy as base
from client import APIClient, APIError

SOL = {'input_per_million': '2.00', 'cached_input_per_million': '0.10', 'output_per_million': '10.00'}


class FakeAPI(base.FakeAPI):
    def __init__(self):
        super().__init__()
        self.catalog = {1: {'id': 1, 'name': 'gpt-6.1-sol', 'kind': 'text', 'costs': dict(SOL), 'active': True},
                        2: {'id': 2, 'name': 'tts-1', 'kind': 'audio', 'costs': {'characters_per_million': '15.00'}, 'active': True},
                        3: {'id': 3, 'name': 'text-embedding-3-small', 'kind': 'embedding',
                            'costs': {'input_per_million': '0.02'}, 'active': True}}
        self.created = []; self.updated = []; self.deleted = []; self.selected = []
    def models(self): return [dict(m) for m in self.catalog.values()]
    def model_selection(self):
        return {m['kind']: {'id': m['id'], 'name': m['name']} for m in self.catalog.values() if m['active']}
    def create_model(self, name, kind, costs):
        self.created.append((name, kind, costs))
        id = max(self.catalog) + 1
        self.catalog[id] = {'id': id, 'name': name, 'kind': kind, 'costs': costs, 'active': False}
        return dict(self.catalog[id])
    def update_model(self, id, name, kind, costs):
        self.updated.append((id, name, kind, costs)); self.catalog[id].update(name=name, kind=kind, costs=costs)
        return dict(self.catalog[id])
    def delete_model(self, id): self.deleted.append(id); self.catalog.pop(id)
    def select_models(self, text, audio, embedding):
        self.selected.append((text, audio, embedding))
        for m in self.catalog.values(): m['active'] = m['id'] in (text, audio, embedding)
        return self.model_selection()


class ClientValidation(unittest.TestCase):
    def test_model_fields_are_validated_before_any_request(self):
        api = APIClient('http://127.0.0.1:9', 'k')
        for args in (('', 'text', {}), ('bad name!', 'text', {}), ('m', 'video', {}),
                     ('m', 'text', {'input_per_million': '-1'}), ('m', 'text', {'characters_per_million': '1'}),
                     ('m', 'audio', {'input_per_million': '1'})):
            with self.assertRaises(ValueError): api.create_model(*args)
        with self.assertRaises(APIError): APIClient._model({'id': 1, 'name': 'x'})


@unittest.skipUnless(importlib.util.find_spec('streamlit'), 'Streamlit image required')
class ModelCatalogUI(unittest.TestCase):
    def setUp(self):
        from streamlit.testing.v1 import AppTest
        import client
        self.api = FakeAPI()
        factory = patch.object(client, 'APIClient', lambda *a: self.api)
        factory.start(); self.addCleanup(factory.stop)
        self.app = AppTest.from_file(str(FRONTEND / 'app.py'), default_timeout=5).run()
        self.app.sidebar.radio[0].set_value('Configurações').run()

    def click(self, label):
        next(b for b in self.app.button if b.label == label).click().run()
        self.assertFalse(self.app.exception)

    def test_register_model_with_the_costs_of_its_option(self):
        self.app.selectbox(key='new-model-kind').set_value('audio').run()
        labels = [t.label for t in self.app.text_input if t.key and t.key.startswith('new-model-cost-')]
        self.assertEqual(labels, ['Por 1 milhão de caracteres'])
        self.app.text_input(key='new-model-name').set_value('gpt-4o-mini-tts')
        self.app.text_input(key='new-model-cost-characters_per_million').set_value('12')
        self.click('Cadastrar modelo')
        self.assertEqual(self.api.created, [('gpt-4o-mini-tts', 'audio', {'characters_per_million': '12'})])

    def test_new_text_model_like_gpt_6_2_sol(self):
        self.app.text_input(key='new-model-name').set_value('gpt-6.2-sol')
        for rate, value in SOL.items(): self.app.text_input(key=f'new-model-cost-{rate}').set_value(value)
        self.click('Cadastrar modelo')
        self.assertEqual(self.api.created, [('gpt-6.2-sol', 'text', SOL)])

    def test_edit_costs_and_delete_a_model(self):
        self.app.selectbox(key='edit-model').set_value(1).run()
        self.assertEqual(self.app.text_input(key='edit-model-cost-1-output_per_million').value, '10.00')
        self.app.text_input(key='edit-model-cost-1-output_per_million').set_value('')
        self.click('Salvar modelo')
        self.assertEqual(self.api.updated, [(1, 'gpt-6.1-sol', 'text', {'input_per_million': '2.00',
                                             'cached_input_per_million': '0.10', 'output_per_million': None})])
        self.app.selectbox(key='edit-model').set_value(2).run()
        delete = next(b for b in self.app.button if b.label == 'Excluir modelo')
        self.assertTrue(delete.disabled)  # Requires confirmation.
        self.app.checkbox(key='delete-model-confirm-2').check().run()
        self.click('Excluir modelo')
        self.assertEqual(self.api.deleted, [2])

    def test_select_the_model_in_use_per_option(self):
        self.api.create_model('gpt-6.2-sol', 'text', SOL)
        self.app.run()
        self.assertEqual(self.app.selectbox(key='active-text').options, ['gpt-6.1-sol', 'gpt-6.2-sol'])
        self.app.selectbox(key='active-text').set_value(4)
        self.click('Salvar modelos em uso')
        self.assertEqual(self.api.selected, [(4, 2, 3)])
