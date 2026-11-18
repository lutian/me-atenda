"""Real PostgreSQL persistence/reservation proof in disposable schemas only."""
import os
import unittest
import test_call_policy as policy_cases
import test_postgres as postgres_cases


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'Disposable PostgreSQL required')
class PolicyPostgreSQL(policy_cases.Policy):
    setUp = postgres_cases.PostgreSQL.setUp
    cleanup_schema = postgres_cases.PostgreSQL.cleanup_schema

    def test_old_history_unknown_snapshot_and_reentrant_policy_persistence(self):
        # Build the legacy fixture without claiming it used the new180s cap.
        with self.store.connection() as db:
            db.execute('DROP TABLE call_policy')
            db.execute('ALTER TABLE calls DROP COLUMN max_duration_seconds')
            db.execute("INSERT INTO calls(lead_id,product_id,active,duration_seconds,"
                       "transcription_json,recording_status,audio) VALUES(1,1,FALSE,11,"
                       "'[{\"role\":\"user\",\"content\":\"Synthetic history\"}]',"
                       "'available',%s)", (b'RIFFsynthetic',))
        before = self.client.get('/calls/1',headers=self.headers).json()
        self.store.migrate()
        self.assertEqual(self.policy(),{'max_duration_seconds':180})
        self.assertEqual(self.save({'max_duration_seconds':90}).status_code,200)
        self.store.migrate(); self.store.migrate()
        self.assertEqual(self.policy(),{'max_duration_seconds':90})
        self.assertEqual(self.client.get('/calls/1',headers=self.headers).json(),before)
        self.assertEqual(self.client.get('/calls/1/audio',headers=self.headers).content,b'RIFFsynthetic')
        with self.store.connection() as db:
            self.assertIsNone(db.execute('SELECT max_duration_seconds FROM calls WHERE id=1').fetchone()['max_duration_seconds'])
        self.assertEqual(self.post('/calls',{'lead_id':1,'product_id':1}).status_code,201)
        self.assertEqual(self.provider.dials[-1][0]['max_duration_seconds'],90)
        self.store.migrate()
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT max_duration_seconds FROM calls WHERE id=2').fetchone()['max_duration_seconds'],90)
        self.assertEqual(self.post('/calls',{'lead_id':1,'product_id':1}).status_code,409)
        self.assertEqual(len(self.provider.dials),1)
