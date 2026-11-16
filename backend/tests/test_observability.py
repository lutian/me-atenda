"""Offline S18 contracts; no inference or network requests."""
import asyncio
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock
from app.config import Settings
from app.providers import Providers


class Ledger:
    def __init__(self): self.rows = []
    def event_start(self, event):
        self.rows.append(dict(event, id=len(self.rows)+1))
        return len(self.rows)
    def event_finish(self, id, event): self.rows[id-1].update(event)


class Instrumentation(unittest.IsolatedAsyncioTestCase):
    def provider(self):
        from app.observability import Recorder
        self.ledger = Ledger()
        s = Settings(observability_pricing_json=json.dumps({'version': 'test', 'currency': 'USD',
            'models': {'gpt-4o-mini': {'input_per_million': '2', 'cached_input_per_million': '1',
                                     'output_per_million': '4'}}}))
        p = Providers(s)
        p.observer = Recorder(self.ledger, s)
        p._openai = NS(chat=NS(completions=NS(create=AsyncMock())))
        return p

    async def test_usage_survives_invalid_output_and_is_redacted(self):
        from app.observability import call_scope
        p = self.provider()
        p.ai.chat.completions.create.return_value = NS(
            usage=NS(prompt_tokens=100, completion_tokens=20, total_tokens=120,
                     prompt_tokens_details=NS(cached_tokens=40)),
            _request_id='req_safe123', choices=[NS(message=NS(content='private malformed output'))])
        with call_scope(7), self.assertRaises(ValueError): await p.answer([], 'private context')
        e = self.ledger.rows[0]
        self.assertEqual((e['call_id'], e['status'], e['input_tokens'], e['output_tokens'],
                          e['cached_input_tokens'], e['total_tokens']), (7, 'error', 100, 20, 40, 120))
        self.assertEqual(e['estimated_cost_usd'], '0.00024')
        self.assertEqual(e['error_code'], 'invalid_response')
        self.assertNotIn('private', json.dumps(e))
        self.assertGreaterEqual(e['elapsed_ms'], 0)

    async def test_missing_usage_unknown_and_cancellation_safe(self):
        p = self.provider()
        p.ai.chat.completions.create.return_value = NS(usage=None, choices=[NS(message=NS(
            content='{"text":"Olá","outcome":null}'))])
        await p.answer([], '')
        self.assertIsNone(self.ledger.rows[0]['total_tokens'])
        self.assertIsNone(self.ledger.rows[0]['estimated_cost_usd'])
        self.assertEqual(self.ledger.rows[0]['cost_status'], 'unknown')
        p.ai.chat.completions.create.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError): await p.answer([], '')
        self.assertEqual(self.ledger.rows[1]['status'], 'cancelled')
        self.assertEqual(self.ledger.rows[1]['error_message'], 'Operation cancelled')

    async def test_embed_global_vs_query_and_non_token_operations(self):
        from app.observability import call_scope
        p = self.provider()
        p._openai.embeddings = NS(create=AsyncMock(return_value=NS(
            usage=NS(prompt_tokens=5, total_tokens=5), data=[NS(embedding=[1])], _request_id='unsafe secret')))
        self.assertEqual(await p.embed(['private']), [[1]])
        with call_scope(4): await p.embed(['private'])
        self.assertEqual([e['call_id'] for e in self.ledger.rows], [None, 4])
        self.assertIsNone(self.ledger.rows[0]['provider_request_id'])
        p._transcribe = lambda pcm: 'local'
        self.assertEqual(await p.transcribe(b'\0\0'), 'local')
        self.assertEqual(self.ledger.rows[-1]['cost_status'], 'not_applicable')
        self.assertIsNone(self.ledger.rows[-1]['total_tokens'])


    async def test_tts_characters_only_after_response_and_sdk_no_retries(self):
        from unittest.mock import patch
        p = self.provider()
        p.observer.prices['models']['tts-1'] = {'characters_per_million':'10'}
        p._openai.audio = NS(speech=NS(create=AsyncMock(side_effect=OSError('private failure'))))
        with self.assertRaises(OSError): await p.speak('olá')
        self.assertIsNone(self.ledger.rows[-1]['estimated_cost_usd'])
        p.ai.audio.speech.create.side_effect = None
        p.ai.audio.speech.create.return_value = NS(content=b'\x00\x00'*24)
        await p.speak('olá')
        self.assertEqual(self.ledger.rows[-1]['characters'], 3)
        self.assertEqual(self.ledger.rows[-1]['estimated_cost_usd'], '0.00003')
        self.assertIsNone(self.ledger.rows[-1]['total_tokens'])
        p._openai = None
        with patch('openai.AsyncOpenAI') as client:
            p.ai
            self.assertEqual(client.call_args.kwargs['max_retries'], 0)

    async def test_accounting_failure_does_not_erase_dial_result_or_rejection(self):
        import httpx
        from app.providers import CallCreationRejected
        p = self.provider()
        p.s.account_sid = 'AC'+'2'*32
        self.ledger.event_finish = lambda *args: (_ for _ in ()).throw(OSError('private database detail'))
        p.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(201,json={'sid':'CA'+'1'*32})))
        with self.assertLogs('app.observability', level='ERROR') as logs:
            self.assertEqual(await p.dial({'id':7}, '+15551234567'), 'CA'+'1'*32)
        self.assertIn('accounting_finalize_failed event_id=1', logs.output[0])
        self.assertNotIn('private', str(logs.output))
        self.assertEqual(self.ledger.rows[0]['error_code'], 'interrupted')
        p.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(400,json={'status':400,'code':21211})))
        with self.assertLogs('app.observability', level='ERROR'), self.assertRaises(CallCreationRejected):
            await p.dial({'id':8}, '+15551234567')
        before = len(self.ledger.rows)
        self.ledger.event_start = lambda event: (_ for _ in ()).throw(OSError('private'))
        with self.assertRaises(OSError): await p.dial({'id':9}, '+15551234567')
        self.assertEqual(len(self.ledger.rows), before)

    async def test_fixed_billing_resource_sid_and_currency_validation(self):
        import httpx
        from app.observability import call_scope
        p = self.provider()
        p.s.account_sid = 'AC'+'2'*32
        sid = 'CA'+'1'*32
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200,json={'sid':sid,'account_sid':p.s.account_sid,
                                           'price':'-0.25','price_unit':'eur'})
        p.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        with call_scope(1): self.assertEqual(await p.billing(sid), ('0.25','EUR'))
        self.assertEqual(str(seen[0].url), 'https://api.twilio.com/2010-04-01/Accounts/'+p.s.account_sid+'/Calls/'+sid+'.json')
        self.assertEqual(seen[0].method, 'GET')
        with self.assertRaises(ValueError): await p.billing('http://169.254.169.254/')
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.ledger.rows[0]['operation'], 'billing_query')
        self.assertIsNone(self.ledger.rows[0]['estimated_cost_usd'])


    async def test_hangup_download_and_dial_share_safe_scope(self):
        import httpx
        from app.observability import call_scope
        p = self.provider()
        p.s.account_sid = 'AC'+'2'*32
        def handler(request):
            if '/Recordings/' in request.url.path:
                return httpx.Response(200, content=b'RIFFvalid', headers={'content-type':'audio/wav'})
            return httpx.Response(201, json={'sid':'CA'+'1'*32})
        p.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with call_scope(9):
            self.assertEqual(await p.dial({'id':9}, '+15551234567'), 'CA'+'1'*32)
            await p.hangup('CA'+'1'*32)
            self.assertEqual(await p.download('RE'+'4'*32), b'RIFFvalid')
        self.assertEqual([e['operation'] for e in self.ledger.rows], ['dial','hangup','download'])
        self.assertTrue(all(e['call_id']==9 and e['status']=='success' for e in self.ledger.rows))
        self.assertNotIn('+15551234567', json.dumps(self.ledger.rows))
        self.assertTrue(all(e['total_tokens'] is None for e in self.ledger.rows))
        self.assertTrue(all(e['elapsed_ms']>=0 for e in self.ledger.rows))
        self.assertEqual(self.ledger.rows[-1]['cost_status'], 'not_applicable')


class Pricing(unittest.TestCase):
    def test_strict_pricing_and_partial_known_totals(self):
        from app.observability import pricing, summarize
        for rate in ['NaN', '-1', 'Infinity', 1]:
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                pricing(json.dumps({'version':'v', 'currency':'USD', 'models':{
                    'm': {'input_per_million':rate}}}))
        with self.assertRaises(ValueError): pricing('{"version":"v","currency":"BRL"}')
        events = [dict(status='success', input_tokens=10, output_tokens=2, cached_input_tokens=3,
                       total_tokens=12, estimated_cost_usd='0.2', cost_status='estimated', operation='answer'),
                  dict(status='error', input_tokens=None, output_tokens=None, cached_input_tokens=None,
                       total_tokens=None, estimated_cost_usd=None, cost_status='unknown', operation='embed')]
        result = summarize(events, 1)
        self.assertEqual(result['total_tokens'], 12)
        self.assertEqual(result['unknown_usage_events'], 1)
        self.assertEqual(result['estimated_cost_usd'], '0.2')
        self.assertEqual(result['cost_status'], 'partial')
        self.assertTrue(result['excluded_costs'])
        self.assertEqual(summarize([], None)['cost_status'], 'no_usage')
        self.assertEqual(summarize([], None)['billed_connectivity']['status'], 'not_applicable')

    def test_native_charge_validation(self):
        from app.observability import native_charge
        self.assertEqual(native_charge({'price':'-0.125', 'price_unit':'USD'}), ('0.125', 'USD'))
        self.assertEqual(native_charge({'price':None, 'price_unit':'USD'}), (None, None))
        for price, unit in [('0.1','USD'), ('NaN','USD'), ('-1','private'), ('-1','ZZZ')]:
            with self.subTest(price=price, unit=unit), self.assertRaises(ValueError):
                native_charge({'price':price, 'price_unit':unit})


if __name__ == '__main__': unittest.main()
