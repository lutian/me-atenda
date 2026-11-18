"""S18 authenticated endpoints and honest billing separation, offline only."""
import unittest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.observability import billed_empty, summarize
import test_api as fixtures
from test_api import MemoryStore, Providers


class AccountingStore(MemoryStore):
    def __init__(self):
        super().__init__()
        self.events = []
        self.bills = {}
        self.snapshots = {}
    def event_start(self, event):
        self.events.append(dict(event, id=len(self.events)+1))
        return len(self.events)
    def event_finish(self, id, event): self.events[id-1].update(event)
    def account_call(self, id, snapshot): self.snapshots.setdefault(id, snapshot)
    def observability_events(self, call_id=None, limit=100):
        return [e for e in reversed(self.events) if call_id is None or e['call_id'] == call_id][:limit]
    def observability_summary(self, call_id=None):
        return summarize(self.observability_events(call_id, 100000), call_id, self.bills.get(call_id))
    def billing_save(self, id, sid, status, amount=None, currency=None, error_code=None):
        if self.get('calls', id)['sid'] != sid: raise ValueError('call SID mismatch')
        if self.bills.get(id, {}).get('status') == 'available' and status != 'available': return self.bills[id]
        self.bills[id] = dict(status=status, amount=amount, currency=currency,
                              updated_at='2026-01-01T00:00:00+00:00', error_code=error_code)
        return self.bills[id]


class ObservabilityAPI(unittest.TestCase):
    post = fixtures.API.post
    seed = fixtures.API.seed
    callback = fixtures.API.callback
    def setUp(self):
        self.store = AccountingStore()
        self.provider = Providers()
        self.settings = Settings(admin_key='local-test-key', public_base='https://voice.example/prefix',
                                 twilio_token='test-token', account_sid='AC'+'2'*32)
        self.client = TestClient(create_app(self.settings, self.store, self.provider))
        self.headers = {'X-API-Key':'local-test-key'}

    def test_auth_validation_scope_and_empty_summary(self):
        for route in ['/observability/events', '/observability/summary']:
            r = self.client.get(route)
            self.assertEqual((r.status_code, r.json()), (401, {'detail':'Invalid API key'}))
        r = self.client.post('/calls/1/billing/reconcile')
        self.assertEqual(r.status_code, 401)
        for query in ['limit=0', 'limit=201', 'limit=bad', 'call_id=bad']:
            self.assertEqual(self.client.get('/observability/events?'+query, headers=self.headers).status_code, 422)
        r = self.client.get('/observability/events', headers=self.headers)
        self.assertEqual((r.status_code, r.json()), (200, []))
        result = self.client.get('/observability/summary', headers=self.headers).json()
        self.assertEqual(result, summarize([], None))
        self.assertEqual(self.post('/calls/99/billing/reconcile', {}).status_code, 404)
        self.assertEqual(self.store.events, [])
        self.assertEqual(self.store.bills, {})

    def test_filtered_events_and_summary_use_all_known_usage(self):
        from app.observability import Recorder
        recorder = Recorder(self.store, self.settings)
        for scope, tokens in [(None, 10), (1, 3), (1, 5)]:
            event = recorder.event(scope, 'openai', 'embed', 'text-embedding-3-small')
            event.update(status='success', error_code=None, error_message=None,
                         input_tokens=tokens, total_tokens=tokens)
            self.store.event_start(event)
        events = self.client.get('/observability/events?call_id=1&limit=1', headers=self.headers).json()
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]['call_id'], events[0]['total_tokens']), (1,5))
        summary = self.client.get('/observability/summary?call_id=1', headers=self.headers).json()
        self.assertEqual((summary['request_count'], summary['input_tokens'], summary['total_tokens']), (2,8,8))
        self.assertIsNone(summary['estimated_cost_usd'])
        self.assertEqual(summary['cost_status'], 'unknown')
        summary = self.client.get('/observability/summary', headers=self.headers).json()
        self.assertEqual((summary['request_count'], summary['total_tokens']), (3,18))
        self.assertEqual(summary['billed_connectivity']['status'], 'not_applicable')

    def test_reconciliation_bound_sid_idempotent_and_sanitized(self):
        self.assertEqual(self.seed().status_code, 201)
        seen = []
        async def billing(sid): seen.append(sid); return '0.125', 'BRL'
        self.provider.billing = billing
        for _ in range(2):
            r = self.post('/calls/1/billing/reconcile', {})
            self.assertEqual(r.status_code, 200)
            self.assertEqual((r.json()['status'], r.json()['amount'], r.json()['currency']),
                             ('available', '0.125', 'BRL'))
        self.assertEqual(seen, ['CA'+'1'*32]*2)
        result = self.client.get('/observability/summary?call_id=1', headers=self.headers).json()
        self.assertEqual(result['billed_connectivity']['amount'], '0.125')
        self.assertIsNone(result['estimated_cost_usd'])
        self.assertEqual(len(self.provider.dials), 1)
        async def fail(sid): raise OSError('secret phone +5511999999999 header token')
        self.provider.billing = fail
        r = self.post('/calls/1/billing/reconcile', {})
        self.assertEqual(r.status_code, 200)
        # An unsuccessful refresh must not erase an available bill.
        self.assertEqual(r.json()['status'], 'available')
        self.assertNotIn('secret', r.text)
        self.assertEqual(self.store.get('calls', 1)['status_final'], 'ligando')

    def test_call_without_bound_sid_pending_and_prior_crud_preserved(self):
        self.seed()
        self.store.calls[1]['sid'] = None
        self.assertEqual(self.post('/calls/1/billing/reconcile', {}).json(), billed_empty(1))
        self.assertEqual(self.client.get('/products', headers=self.headers).json(),
                         [{'id':1,'name':'Produto','description':'Descrição','goal':None}])
        self.assertEqual(self.callback('/twilio/status/1', {'CallSid':'CA'+'1'*32,
            'CallStatus':'completed','CallDuration':'61'}).status_code, 200)
        self.assertEqual(self.client.get('/calls/1', headers=self.headers).json()['duration_seconds'], 61)


if __name__ == '__main__': unittest.main()
