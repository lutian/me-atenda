"""Real PostgreSQL + pgvector proof. Opt in ONLY with a disposable TEST_DATABASE_URL."""
import concurrent.futures
import os
import unittest
import uuid
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.store import Store
import test_api as fixtures


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'TEST_DATABASE_URL not set; PostgreSQL proof missing')
class PostgreSQL(unittest.TestCase):
    def setUp(self):
        # Each test owns a fresh schema; existing tables/data are never touched.
        import psycopg
        from psycopg import sql
        self.root = Store(os.environ['TEST_DATABASE_URL'])
        self.schema = 'test_' + uuid.uuid4().hex
        with self.root.connection() as db:
            db.execute('CREATE EXTENSION IF NOT EXISTS vector')
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        outer = self
        class Isolated(Store):
            def connection(self):
                db = super().connection()
                db.execute(sql.SQL('SET search_path TO {}, public').format(sql.Identifier(outer.schema)))
                return db
        self.store = Isolated(os.environ['TEST_DATABASE_URL'])
        self.addCleanup(self.cleanup_schema)
        self.store.migrate()
        self.provider = fixtures.Providers()
        self.settings = Settings(admin_key='local-test-key', public_base='https://voice.example/prefix',
                                 twilio_token='test-token', account_sid='AC'+'2'*32)
        self.client = TestClient(create_app(self.settings, self.store, self.provider))
        self.headers = {'X-API-Key': 'local-test-key'}
        self.post('/products', {'name': 'Produto', 'description': 'Descrição'})
        self.post('/leads', {'name': 'Ana', 'phone': '+5511999999999', 'company': ''})
    def cleanup_schema(self):
        from psycopg import sql
        with self.root.connection() as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
    def post(self, path, data): return self.client.post(path, json=data, headers=self.headers)
    callback = fixtures.API.callback

    def test_atomic_reservation_across_connections_and_release(self):
        def dial(_): return self.post('/calls', {'lead_id': 1, 'product_id': 1})
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(dial, range(8)))
        self.assertEqual(sorted(r.status_code for r in results), [201] + [409]*7)
        self.assertEqual(len(self.provider.dials), 1)
        calls = self.client.get('/calls', headers=self.headers).json()
        self.assertEqual(len(calls), 1)
        id = calls[0]['id']
        terminal = {'CallSid': 'CA'+'1'*32, 'CallStatus': 'completed', 'CallDuration': '10'}
        self.assertEqual(self.callback(f'/twilio/status/{id}', terminal).status_code, 200)
        self.assertEqual(self.callback(f'/twilio/status/{id}', terminal).status_code, 200)
        terminal['CallStatus'] = 'ringing'
        self.callback(f'/twilio/status/{id}', terminal)
        row = self.client.get(f'/calls/{id}', headers=self.headers).json()
        self.assertEqual(row['status_final'], 'inconclusivo')
        self.assertEqual(row['duration_seconds'], 10)
        # New provider SID for the next call, since Twilio SIDs are unique.
        async def next_dial(call, phone): return 'CA'+'3'*32
        self.provider.dial = next_dial
        self.assertEqual(dial(0).status_code, 201)

    def test_recording_retry_and_transcript_survive_restart(self):
        response = self.post('/calls', {'lead_id': 1, 'product_id': 1})
        id = response.json()['id']
        data = {'CallSid': 'CA'+'1'*32, 'RecordingSid': 'RE'+'4'*32, 'RecordingStatus': 'completed'}
        async def fail(sid): raise OSError('offline')
        self.provider.download = fail
        self.assertEqual(self.callback(f'/twilio/recording/{id}', data).status_code, 503)
        self.provider.download = fixtures.Providers().download
        self.assertEqual(self.callback(f'/twilio/recording/{id}', data).status_code, 200)
        restarted = TestClient(create_app(self.settings, self.store, self.provider))
        self.assertEqual(restarted.get(f'/calls/{id}/audio', headers=self.headers).content, b'RIFFaudio')
        self.assertEqual(restarted.get('/leads', headers=self.headers).json()[0]['status'], 'ligando')


if __name__ == '__main__': unittest.main()
