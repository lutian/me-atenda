"""Real pgvector integration proof, skipped honestly without disposable PostgreSQL."""
import concurrent.futures
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import unittest
from fastapi.testclient import TestClient
from app.main import create_app
from app.observability import Recorder
from app.providers import Providers
import test_postgres as fixtures


class ObservabilityPostgreSQL(fixtures.PostgreSQL):
    # Inherit the isolated-schema cleanup and the prior lifecycle preservation tests.
    def instrumented(self):
        p = Providers(self.settings)
        p._openai = NS(embeddings=NS(create=AsyncMock(return_value=NS(
            usage=NS(prompt_tokens=12,total_tokens=12), data=[NS(embedding=[0.0]*1536)]))))
        p.observer = Recorder(self.store, self.settings)
        return p

    def test_usage_persists_restarts_and_global_not_charged_to_call(self):
        p = self.instrumented()
        client = TestClient(create_app(self.settings, self.store, p))
        r = client.post('/products', json={'name':'Another','description':'Text'}, headers=self.headers)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.post('/calls', {'lead_id':1,'product_id':1}).status_code, 201)
        # Store helpers record only safe event envelopes; all observations read via REST.
        event = p.observer.event(1, 'openai', 'answer', self.settings.chat_model)
        event.update(status='success', error_code=None, error_message=None, input_tokens=20,
                     output_tokens=3, total_tokens=23, estimated_cost_usd='0.01', cost_status='estimated')
        id = self.store.event_start(event)
        self.store.event_finish(id, event)
        # Finalization is idempotent; a repeated writer cannot erase returned usage.
        self.store.event_finish(id, dict(event, total_tokens=999))
        restarted = TestClient(create_app(self.settings, self.store, self.provider))
        global_summary = restarted.get('/observability/summary', headers=self.headers).json()
        call_summary = restarted.get('/observability/summary?call_id=1', headers=self.headers).json()
        self.assertEqual(global_summary['total_tokens'], 35)
        self.assertEqual(call_summary['total_tokens'], 23)
        self.assertEqual(call_summary['estimated_cost_usd'], '0.01')
        rows = restarted.get('/observability/events?call_id=1&limit=1', headers=self.headers).json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], id)
        self.assertEqual(rows[0]['total_tokens'], 23)

    def test_concurrent_terminal_callbacks_dedup_and_frozen_rounding(self):
        self.assertEqual(self.post('/calls', {'lead_id':1,'product_id':1}).status_code, 201)
        # The first snapshot was frozen empty by the API: it cannot be replaced later.
        self.store.account_call(1, {'version':'later','currency':'USD','twilio':{'voice_per_minute':'99'}})
        payload = {'CallSid':'CA'+'1'*32,'CallStatus':'completed','CallDuration':'61'}
        def callback(_): return self.callback('/twilio/status/1', payload).status_code
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as workers:
            self.assertEqual(list(workers.map(callback, range(8))), [200]*8)
        rows = self.client.get('/observability/events?call_id=1', headers=self.headers).json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['billed_minutes'], 2)
        self.assertIsNone(rows[0]['estimated_cost_usd'])
        # A second call freezes actual configured voice/AMD rates and bills only once.
        async def next_dial(call, phone): return 'CA'+'3'*32
        self.provider.dial = next_dial
        self.settings.observability_pricing_json = ('{"version":"v1","currency":"USD",'
            '"twilio":{"voice_per_minute":"0.1","amd_per_call":"0.02"}}')
        self.client = TestClient(create_app(self.settings, self.store, self.provider))
        r = self.post('/calls', {'lead_id':1,'product_id':1})
        id = r.json()['id']
        for duration in ('1','60','61','61'):
            self.assertEqual(self.callback(f'/twilio/status/{id}', {'CallSid':'CA'+'3'*32,
                'CallStatus':'completed','CallDuration':duration}).status_code, 200)
        result = self.client.get(f'/observability/summary?call_id={id}', headers=self.headers).json()
        self.assertEqual(result['estimated_cost_usd'], '0.22')
        self.assertEqual(result['cost_status'], 'partial')
        self.assertEqual(result['request_count'], 0)  # synthetic usage is not a provider request
        self.assertEqual(self.store.get('calls', id)['status_final'], 'inconclusivo')
        async def billing(sid): return '0.15','EUR'
        self.provider.billing = billing
        def reconcile(_): return self.post(f'/calls/{id}/billing/reconcile', {}).status_code
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as workers:
            self.assertEqual(list(workers.map(reconcile, range(6))), [200]*6)
        result = self.client.get(f'/observability/summary?call_id={id}', headers=self.headers).json()
        self.assertEqual(result['billed_connectivity']['amount'], '0.15')
        self.assertEqual(result['billed_connectivity']['currency'], 'EUR')
        self.assertEqual(result['estimated_cost_usd'], '0.22')

    def test_additive_001_upgrade_and_reapplication_preserve_history(self):
        # Simulate an existing001-only installation in another isolated schema.
        # The harness's own schema teardown owns all tables created here.
        from psycopg import sql
        schema = self.schema + '_old'
        with self.root.connection() as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        def cleanup():
            with self.root.connection() as db:
                db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        self.addCleanup(cleanup)
        original_connection = self.store.connection
        def connection():
            db = original_connection()
            db.execute(sql.SQL('SET search_path TO {}, public').format(sql.Identifier(schema)))
            return db
        self.store.connection = connection
        with self.store.connection() as db:
            db.execute((Path(__file__).parent.parent/'migrations/001_initial.sql').read_text())
        self.store.product({'name':'Existing','description':'Unchanged'}, [])
        self.store.lead({'name':'Existing','phone':'+15551234567','company':''})
        # Seed genuine pre-policy history using the old schema, not new reserve().
        with self.store.connection() as db:
            call = db.execute('INSERT INTO calls(lead_id,product_id) VALUES(1,1) RETURNING *').fetchone()
            db.execute("UPDATE leads SET status='ligando' WHERE id=1")
        self.store.append(call['id'], 'user', 'Existing history')
        self.store.recording(call['id'], 'RE'+'4'*32, b'RIFFexisting')
        self.store.migrate()
        self.store.migrate()
        restarted = TestClient(create_app(self.settings, self.store, self.provider))
        self.assertEqual(restarted.get('/products',headers=self.headers).json()[0]['name'], 'Existing')
        result = restarted.get('/calls/1',headers=self.headers).json()
        self.assertEqual(result['status_final'], 'ligando')
        self.assertEqual(result['transcription_json'], [{'role':'user','content':'Existing history'}])
        self.assertEqual(restarted.get('/calls/1/audio', headers=self.headers).content, b'RIFFexisting')
        self.assertEqual(self.post('/calls', {'lead_id':1,'product_id':1}).status_code, 409)
        self.assertEqual(restarted.get('/observability/events', headers=self.headers).json(), [])


if __name__ == '__main__': unittest.main()
