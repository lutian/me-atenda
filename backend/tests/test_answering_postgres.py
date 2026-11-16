"""Real PostgreSQL serialization across independent apps, fake providers only."""
import concurrent.futures
import os
from pathlib import Path
import unittest
from fastapi.testclient import TestClient
from app.main import create_app
import test_api as api
import test_postgres as pg
import test_answering as contract


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'disposable PostgreSQL required')
class AnsweringPostgreSQL(unittest.TestCase):
    setUp = pg.PostgreSQL.setUp
    cleanup_schema = pg.PostgreSQL.cleanup_schema
    post = pg.PostgreSQL.post
    raw = api.API.raw_callback
    request = contract.Answering.request
    websocket_start = contract.Answering.websocket_start
    test_initial_answeredby_never_substitutes_for_dedicated_amd = contract.Answering.test_initial_answeredby_never_substitutes_for_dedicated_amd
    test_stream_bound_late_callbacks_leave_state_and_intro_unchanged = contract.Answering.test_stream_bound_late_callbacks_leave_state_and_intro_unchanged
    test_expired_human_cannot_claim_stream = contract.Answering.test_expired_human_cannot_claim_stream
    test_generic_asset_only_valid_wav_cache_and_no_generation = contract.Answering.test_generic_asset_only_valid_wav_cache_and_no_generation
    test_pending_generic_audio_is_not_publicly_ready = contract.Answering.test_pending_generic_audio_is_not_publicly_ready
    test_callback_signature_sid_and_exact_origin_query_rejections = contract.Answering.test_callback_signature_sid_and_exact_origin_query_rejections
    def seed(self): return self.post('/calls', {'lead_id':1,'product_id':1})

    def test_deadline_is_not_reset_by_second_app_voice_retry(self):
        import datetime as dt
        from unittest.mock import patch
        self.seed()
        start = dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)
        with patch('app.answering_store.clock', return_value=start): self.request('voice')
        self.client = TestClient(create_app(self.settings,type(self.store)(self.store.url),self.provider))
        with patch('app.answering_store.clock', return_value=start+dt.timedelta(seconds=34)):
            self.assertNotIn('Play', self.request('voice').text)
            self.assertIn('Pause', self.request('gate').text)
        with patch('app.answering_store.clock', return_value=start+dt.timedelta(seconds=35)):
            self.assertIn('Hangup', self.request('gate').text)
            self.request('amd', AnsweredBy='human')
            self.assertIn('Hangup', self.request('gate').text)
        self.assertEqual(self.client.get('/calls/1',headers=self.headers).json()['status_final'], 'inconclusivo')
        self.assertEqual(self.seed().status_code, 409)

    def test_two_apps_voice_and_amd_races_one_issue_first_verdict_wins(self):
        self.seed()
        clients = [self.client, TestClient(create_app(self.settings,type(self.store)(self.store.url),self.provider))]
        def callback(index, mode, verdict=None):
            holder = type('Request', (), {})()
            holder.client = clients[index%2]; holder.settings = self.settings
            data = {'CallSid':contract.SID}
            if verdict: data['AnsweredBy'] = verdict
            return api.API.raw_callback(holder, '/twilio/'+mode+'/1', data)
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            voices = list(pool.map(lambda i: callback(i,'voice'), range(6)))
            self.assertEqual([r.status_code for r in voices], [200]*6)
            self.assertEqual(sum('<Play>' in r.text for r in voices), 1)
            verdicts = list(pool.map(lambda i: callback(i,'amd','human' if i%2 else 'machine_end_beep'), range(6)))
            self.assertEqual([r.status_code for r in verdicts], [200]*6)
        gate = self.request('gate').text
        before = self.client.get('/calls', headers=self.headers).json()
        winner = 'human' if '<Connect>' in gate else 'machine_end_beep'
        self.assertEqual(before[0]['status_final'], 'ligando' if winner=='human' else 'caixa_postal')
        for verdict in ('human','fax','unknown','machine_end_beep'):
            self.request('amd', AnsweredBy=verdict)
            self.assertEqual(self.request('gate').text, gate)
        self.assertEqual(self.client.get('/calls', headers=self.headers).json(), before)
        self.assertEqual(len(self.provider.dials), 1)

    def test_two_apps_simultaneous_voice_and_human_callback(self):
        import threading
        self.seed()
        barrier = threading.Barrier(2)
        other = TestClient(create_app(self.settings,type(self.store)(self.store.url),self.provider))
        def human():
            holder = type('Request', (), {})()
            holder.client = other; holder.settings = self.settings
            barrier.wait(2)
            return api.API.raw_callback(holder, '/twilio/amd/1', {'CallSid':contract.SID,'AnsweredBy':'human'})
        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = pool.submit(human)
            barrier.wait(2)
            self.assertIn('<Play>', self.request('voice').text)
            self.assertEqual(result.result(2).status_code, 200)
        self.assertIn('Connect', self.request('gate').text)
        self.assertNotIn('Play', self.request('voice').text)

    def test_first_signed_callback_binds_before_rest_response_consistently(self):
        # Callback can precede completion of provider.dial / REST bind.
        async def dial(call, phone):
            import asyncio
            def early():
                response = self.request('amd', AnsweredBy='human')
                self.assertEqual(response.status_code, 200)
                self.assertIn('<Play>', self.request('voice').text)
            await asyncio.to_thread(early)
            return contract.SID
        self.provider.dial = dial
        self.assertEqual(self.seed().status_code, 201)
        self.assertIn('<Connect>', self.request('gate').text)
        before = self.client.get('/calls', headers=self.headers).json()
        self.assertEqual(self.raw('/twilio/amd/1', {'CallSid':'CA'+'9'*32,'AnsweredBy':'fax'}).status_code, 409)
        self.assertEqual(self.client.get('/calls', headers=self.headers).json(), before)

    def test_upgrade_003_reapply_preserves_recordings_history_outcome_and_slot(self):
        from psycopg import sql
        old = self.schema+'_old'
        with self.root.connection() as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(old)))
        def cleanup():
            with self.root.connection() as db:
                db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(old)))
        self.addCleanup(cleanup)
        original = self.store.connection
        def connection():
            db = original()
            db.execute(sql.SQL('SET search_path TO {}, public').format(sql.Identifier(old)))
            return db
        self.store.connection = connection
        with self.store.connection() as db:
            for name in ('001_initial.sql','002_observability.sql','003_opening_audio.sql'):
                db.execute((Path(__file__).parent.parent/'migrations'/name).read_text())
        self.store.product({'name':'Existing','description':'Existing'}, [])
        self.store.lead({'name':'Existing','phone':'+15551234567','company':''})
        self.store.reserve(1,1); self.store.append(1,'user','Existing history')
        self.store.recording(1,'RE'+'4'*32,b'RIFFexisting')
        self.store.outcome(1,'sucesso')
        before = self.client.get('/calls', headers=self.headers).json()
        self.store.migrate(); self.store.migrate()
        self.assertEqual(self.client.get('/calls', headers=self.headers).json(), before)
        self.assertEqual(self.client.get('/calls/1/audio', headers=self.headers).content, b'RIFFexisting')
        self.assertIn('Hangup', self.request('voice').text)
        self.request('amd', AnsweredBy='human')
        self.assertIn('Hangup', self.request('gate').text)
        self.assertEqual(self.client.get('/calls', headers=self.headers).json(), before)
        self.assertEqual(self.seed().status_code, 409)
