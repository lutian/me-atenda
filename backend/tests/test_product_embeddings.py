"""Products keep the embedding model of their vectors (S5); outdated products are unavailable until updated."""
import unittest
import test_api as fixtures


class ProductEmbeddings(unittest.TestCase):
    setUp = fixtures.API.setUp
    post = fixtures.API.post

    def product(self, id=1):
        return next(p for p in self.client.get('/products', headers=self.headers).json() if p['id'] == id)

    def switch_embedding_model(self, name='text-embedding-3-large'):
        new = self.post('/models', {'name': name, 'kind': 'embedding', 'costs': {}}).json()['id']
        ids = {k: v['id'] for k, v in self.client.get('/settings/models', headers=self.headers).json().items()}
        self.assertEqual(self.client.put('/settings/models', json=dict(ids, embedding=new), headers=self.headers).status_code, 200)
        return new

    def ready_to_dial(self):
        self.assertEqual(self.post('/products', {'name': 'Produto', 'description': 'D'}).status_code, 201)
        self.assertEqual(self.post('/leads', {'name': 'Ana', 'phone': '+5511999999999'}).status_code, 201)

    def test_product_records_the_embedding_model_in_use(self):
        created = self.post('/products', {'name': 'Produto', 'description': 'D'}).json()
        self.assertEqual((created['embedding_model'], created['embeddings_current']), ('text-embedding-3-small', True))
        self.assertEqual(self.product()['embeddings_current'], True)

    def test_outdated_product_cannot_be_drafted_nor_dialed_until_updated(self):
        self.ready_to_dial()
        self.switch_embedding_model()
        product = self.product()
        self.assertEqual((product['embedding_model'], product['embeddings_current']), ('text-embedding-3-small', False))
        for response in (self.client.post('/products/1/messages/draft', headers=self.headers),
                         self.post('/calls', {'lead_id': 1, 'product_id': 1})):
            self.assertEqual(response.status_code, 409)
            self.assertIn('embeddings', response.json()['detail'])
        self.assertEqual(self.provider.dials, [])
        embedded = []
        original = self.provider.embed
        async def embed(texts):
            embedded.append(list(texts))
            return await original(texts)
        self.provider.embed = embed
        updated = self.client.post('/products/1/embeddings', headers=self.headers)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual((updated.json()['embedding_model'], updated.json()['embeddings_current']),
                         ('text-embedding-3-large', True))
        self.assertEqual(embedded, [['Produto\nD']])
        self.assertEqual(self.post('/calls', {'lead_id': 1, 'product_id': 1}).status_code, 201)

    def test_failed_update_keeps_the_product_outdated(self):
        self.ready_to_dial()
        self.switch_embedding_model()
        async def embed(texts): raise RuntimeError('offline')
        self.provider.embed = embed
        self.assertEqual(self.client.post('/products/1/embeddings', headers=self.headers).status_code, 503)
        self.assertFalse(self.product()['embeddings_current'])

    def test_editing_an_outdated_product_also_updates_its_embeddings(self):
        self.ready_to_dial()
        self.switch_embedding_model()
        self.assertTrue(self.client.patch('/products/1', json={'description': 'Nova'},
                                          headers=self.headers).json()['embeddings_current'])

    def test_deleting_the_old_model_keeps_the_product_outdated(self):
        self.ready_to_dial()
        old = next(m['id'] for m in self.client.get('/models', headers=self.headers).json() if m['name'] == 'text-embedding-3-small')
        self.switch_embedding_model()
        self.assertEqual(self.client.delete(f'/models/{old}', headers=self.headers).status_code, 204)
        self.assertEqual((self.product()['embedding_model'], self.product()['embeddings_current']), (None, False))

    def test_update_endpoint_errors(self):
        self.assertEqual(self.client.post('/products/9/embeddings', headers=self.headers).status_code, 404)
        self.assertEqual(self.client.post('/products/1/embeddings').status_code, 401)


if __name__ == '__main__': unittest.main()
