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

    def test_human_terminal_concurrency_dedup_and_slot_release(self):
        id = self.post('/calls', {'lead_id': 1, 'product_id': 1}).json()['id']
        sid = 'CA'+'1'*32
        self.assertEqual(self.callback(f'/twilio/voice/{id}', {'CallSid': sid, 'AnsweredBy': 'human'}).status_code, 200)
        payload = {'CallSid': sid, 'CallStatus': 'completed', 'CallDuration': '6'}
        def terminal(_): return self.callback(f'/twilio/status/{id}', payload)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as workers:
            self.assertEqual([r.status_code for r in workers.map(terminal, range(8))], [200]*8)
        row = self.client.get(f'/calls/{id}', headers=self.headers).json()
        self.assertEqual((row['status_final'], row['duration_seconds'], row['transcription_json']), ('falha_tecnica', 6, []))
        events = self.client.get(f'/observability/events?call_id={id}', headers=self.headers).json()
        self.assertEqual(sorted(e['operation'] for e in events), ['connectivity_estimate', 'media_stream'])
        self.assertEqual(sum(e['error_code'] == 'media_stream_not_established' for e in events), 1)
        self.assertFalse(self.store.backfill_media_failure(id))
        async def next_dial(call, phone): return 'CA'+'3'*32
        self.provider.dial = next_dial
        self.assertEqual(self.post('/calls', {'lead_id': 1, 'product_id': 1}).status_code, 201)

    def test_media_diagnostic_failure_rolls_back_terminal_release(self):
        from unittest.mock import patch
        id = self.post('/calls', {'lead_id': 1, 'product_id': 1}).json()['id']
        sid = 'CA'+'1'*32
        self.callback(f'/twilio/voice/{id}', {'CallSid': sid, 'AnsweredBy': 'human'})
        original = self.store.media_failure_locked
        def fail_after_insert(db, row):
            original(db, row)
            raise RuntimeError('synthetic persistence failure')
        payload = {'CallSid': sid, 'CallStatus': 'completed', 'CallDuration': '6'}
        with patch.object(self.store, 'media_failure_locked', side_effect=fail_after_insert):
            with self.assertRaisesRegex(RuntimeError, 'synthetic persistence failure'):
                self.callback(f'/twilio/status/{id}', payload)
        self.assertEqual(self.client.get(f'/calls/{id}', headers=self.headers).json()['status_final'], 'ligando')
        self.assertEqual(self.client.get(f'/observability/events?call_id={id}', headers=self.headers).json(), [])
        self.assertEqual(self.post('/calls', {'lead_id': 1, 'product_id': 1}).status_code, 409)
        self.assertEqual(self.callback(f'/twilio/status/{id}', payload).status_code, 200)

    def test_explicit_backfill_guards_concurrent_dedup_and_preserves_history(self):
        unbound = self.store.reserve(1, 1)['id']
        self.store.human(unbound)
        self.store.finish(unbound, 'inconclusivo', 6)
        self.assertFalse(self.store.backfill_media_failure(unbound))
        row = self.store.reserve(1, 1)
        id = row['id']
        self.assertFalse(self.store.backfill_media_failure(id))
        self.store.bind(id, 'CA'+'1'*32)
        self.store.finish(id, 'inconclusivo', 6)
        self.assertFalse(self.store.backfill_media_failure(id))  # no persisted human
        row = self.store.reserve(1, 1)
        streamed = row['id']
        self.store.bind(streamed, 'CA'+'3'*32)
        self.store.human(streamed)
        self.store.claim_stream(streamed, 'CA'+'3'*32, 'MZ'+'5'*32)
        self.store.finish(streamed, 'inconclusivo', 6)
        self.assertFalse(self.store.backfill_media_failure(streamed))
        row = self.store.reserve(1, 1)
        legacy = row['id']
        self.store.bind(legacy, 'CA'+'4'*32)
        self.store.human(legacy)
        self.store.finish(legacy, 'inconclusivo', 6)  # legacy outcome, no terminal callback
        before = self.client.get(f'/calls/{legacy}', headers=self.headers).json()
        lead_before = self.client.get('/leads', headers=self.headers).json()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as workers:
            self.assertEqual(sum(workers.map(self.store.backfill_media_failure, [legacy]*8)), 1)
        self.assertEqual(self.client.get(f'/calls/{legacy}', headers=self.headers).json(), before)
        self.assertEqual(self.client.get('/leads', headers=self.headers).json(), lead_before)
        events = self.client.get(f'/observability/events?call_id={legacy}', headers=self.headers).json()
        self.assertEqual([e['operation'] for e in events], ['media_stream'])
        self.assertEqual(events[0]['error_code'], 'media_stream_not_established')

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
