"""Product form: optional call goal next to name and description."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

FRONTEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FRONTEND))
GOAL = 'Agendar uma demonstração com o responsável técnico.'


class FakeAPI:
    def __init__(self):
        self.created = []; self.updated = []
        self.products = [{'id': 1, 'name': 'Cartória', 'description': 'D', 'goal': GOAL},
                         {'id': 2, 'name': 'Antigo', 'description': 'D', 'goal': None}]
    def list(self, resource): return self.products if resource == 'products' else []
    def create(self, resource, payload): self.created.append((resource, payload)); return payload
    def update(self, resource, id, payload): self.updated.append((resource, id, payload)); return payload


@unittest.skipUnless(importlib.util.find_spec('streamlit'), 'Streamlit image required')
class ProductGoalUI(unittest.TestCase):
    def setUp(self):
        from streamlit.testing.v1 import AppTest
        import client
        self.api = FakeAPI()
        factory = patch.object(client, 'APIClient', lambda *a: self.api)
        factory.start(); self.addCleanup(factory.stop)
        self.app = AppTest.from_file(str(FRONTEND / 'app.py'), default_timeout=5).run()

    def areas(self):
        return {area.label: area for area in self.app.text_area}

    def test_create_with_goal(self):
        self.app.text_input[0].set_value('Novo')
        self.areas()['Descrição comercial'].set_value('Descrição')
        self.areas()['Objetivo da ligação (opcional)'].set_value(GOAL)
        next(b for b in self.app.button if b.label == 'Salvar cadastro').click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.api.created, [('products', {'name': 'Novo', 'description': 'Descrição', 'goal': GOAL})])

    def test_goal_is_optional_on_create(self):
        self.app.text_input[0].set_value('Sem objetivo')
        self.areas()['Descrição comercial'].set_value('Descrição')
        next(b for b in self.app.button if b.label == 'Salvar cadastro').click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.api.created, [('products', {'name': 'Sem objetivo', 'description': 'Descrição', 'goal': ''})])

    def test_edit_shows_saved_goal_and_handles_missing_goal(self):
        self.app.selectbox(key='edit-products').set_value(1).run()
        goals = [a.value for a in self.app.text_area if a.label == 'Objetivo da ligação (opcional)']
        self.assertEqual(goals[-1], GOAL)
        self.app.selectbox(key='edit-products').set_value(2).run()
        self.assertFalse(self.app.exception)
        goals = [a.value for a in self.app.text_area if a.label == 'Objetivo da ligação (opcional)']
        self.assertEqual(goals[-1], '')


if __name__ == '__main__': unittest.main()
