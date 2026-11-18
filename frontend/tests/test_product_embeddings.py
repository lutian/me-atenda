"""Outdated product embeddings: a button only when needed, and no dispatch until updated."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

FRONTEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FRONTEND))
import test_prepared_messages as dispatch


class FakeAPI(dispatch.FakeAPI):
    def __init__(self):
        super().__init__()
        self.status = {'opening': 'O', 'pitch': 'P', 'goodbye': 'G', 'ready': True}
        self.current = False; self.refreshed = []
    def list(self, resource):
        if resource == 'products':
            return [{'id': 1, 'name': 'Cartória', 'description': 'D', 'goal': None,
                     'embedding_model': 'text-embedding-3-small', 'embeddings_current': self.current},
                    {'id': 2, 'name': 'Atual', 'description': 'D', 'goal': None,
                     'embedding_model': 'text-embedding-3-large', 'embeddings_current': True}]
        return super().list(resource)
    def update_embeddings(self, product_id):
        self.refreshed.append(product_id); self.current = True
        return {'id': product_id, 'embeddings_current': True}


@unittest.skipUnless(importlib.util.find_spec('streamlit'), 'Streamlit image required')
class OutdatedEmbeddingsUI(unittest.TestCase):
    def setUp(self):
        from streamlit.testing.v1 import AppTest
        import client
        self.api = FakeAPI()
        factory = patch.object(client, 'APIClient', lambda *a: self.api)
        factory.start(); self.addCleanup(factory.stop)
        self.app = AppTest.from_file(str(FRONTEND / 'app.py'), default_timeout=5).run()

    def update_buttons(self):
        return [b for b in self.app.button if b.label == 'Atualizar embeddings']

    def test_button_only_for_outdated_products(self):
        self.assertEqual([b.key for b in self.update_buttons()], ['embeddings-1'])
        self.update_buttons()[0].click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.api.refreshed, [1])
        self.assertEqual(self.update_buttons(), [])

    def test_outdated_product_blocks_messages_and_dispatch(self):
        self.app.sidebar.radio[0].set_value('Leads').run()
        self.app.selectbox(key='dispatch-lead').set_value(1)
        self.app.selectbox(key='dispatch-product').set_value(1).run()
        self.app.checkbox[0].check().run()
        self.assertTrue(any('embeddings' in e.value for e in self.app.error))
        self.assertTrue(next(b for b in self.app.button if b.label == 'Iniciar UMA ligação').disabled)
        self.assertEqual(self.api.drafts, 0)


if __name__ == '__main__': unittest.main()
