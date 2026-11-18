"""Model catalog against real PostgreSQL: seeded prices, selection and restarts."""
import os
import unittest
from fastapi.testclient import TestClient
from app.main import create_app
import test_postgres as postgres_cases

SOL = {'input_per_million': '2.00', 'cached_input_per_million': '0.10', 'output_per_million': '10.00'}


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'Disposable PostgreSQL required')
class ModelCatalogPostgreSQL(unittest.TestCase):
    setUp = postgres_cases.PostgreSQL.setUp
    cleanup_schema = postgres_cases.PostgreSQL.cleanup_schema
    post = postgres_cases.PostgreSQL.post

    def models(self, client=None):
        return {m['name']: m for m in (client or self.client).get('/models', headers=self.headers).json()}

    def test_seeded_catalog_with_official_prices(self):
        models = self.models()
        self.assertEqual(models['gpt-6.1-sol']['costs'], SOL)
        self.assertEqual(models['gpt-6.2-sol']['costs'], SOL)
        self.assertEqual(models['text-embedding-3-small']['costs'], {'input_per_million': '0.02'})
        self.assertEqual(models['tts-1']['costs'], {'characters_per_million': '15.00'})
        self.assertEqual(models['gpt-6.2-sol']['kind'], 'text')

    def test_selection_restart_and_deleted_model_is_not_reseeded(self):
        models = self.models()
        selection = {k: v['id'] for k, v in self.client.get('/settings/models', headers=self.headers).json().items()}
        self.assertEqual(self.client.put('/settings/models', json=dict(selection, text=models['gpt-6.2-sol']['id']),
                                         headers=self.headers).status_code, 200)
        self.assertEqual(self.client.delete(f"/models/{models['gpt-6.2-sol']['id']}", headers=self.headers).status_code, 409)
        self.assertEqual(self.client.delete(f"/models/{models['gpt-6.1-sol']['id']}", headers=self.headers).status_code, 204)
        self.store.migrate()
        restarted = TestClient(create_app(self.settings, self.store, self.provider))
        self.assertNotIn('gpt-6.1-sol', self.models(restarted))
        self.assertEqual(restarted.get('/settings/models', headers=self.headers).json()['text']['name'], 'gpt-6.2-sol')
        self.assertEqual(self.models(restarted)['gpt-6.2-sol']['costs'], SOL)


    def product(self, client=None):
        return next(p for p in (client or self.client).get('/products', headers=self.headers).json() if p['id'] == 1)

    def test_upgrade_binds_existing_products_once(self):
        # Simulate a database from before the catalog: products without the column.
        with self.store.connection() as db:
            db.execute('ALTER TABLE products DROP COLUMN embedding_model_id')
        self.store.migrate()
        self.assertEqual((self.product()['embedding_model'], self.product()['embeddings_current']),
                         ('text-embedding-3-small', True))
        # Reapplying migrations never rebinds a product whose model was deleted.
        models = self.models()
        new = self.post('/models', {'name': 'text-embedding-3-large', 'kind': 'embedding', 'costs': {}}).json()['id']
        ids = {k: v['id'] for k, v in self.client.get('/settings/models', headers=self.headers).json().items()}
        self.assertEqual(self.client.put('/settings/models', json=dict(ids, embedding=new), headers=self.headers).status_code, 200)
        self.assertEqual(self.client.delete(f"/models/{models['text-embedding-3-small']['id']}",
                                            headers=self.headers).status_code, 204)
        self.store.migrate()
        self.assertEqual((self.product()['embedding_model'], self.product()['embeddings_current']), (None, False))
        self.assertEqual(self.client.post('/products/1/embeddings', headers=self.headers).json()['embedding_model'],
                         'text-embedding-3-large')


if __name__ == '__main__': unittest.main()
