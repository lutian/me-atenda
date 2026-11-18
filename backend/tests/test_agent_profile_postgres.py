"""Agent profile persistence and per-call snapshot against real PostgreSQL."""
import os
import unittest
import test_agent_profile as profile_cases
import test_postgres as postgres_cases


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'Disposable PostgreSQL required')
class AgentProfilePostgreSQL(profile_cases.AgentProfile):
    setUp = postgres_cases.PostgreSQL.setUp
    cleanup_schema = postgres_cases.PostgreSQL.cleanup_schema

    def test_profile_survives_migrations_and_rejects_concurrent_change(self):
        self.assertEqual(self.save({'agent_name': 'Pedro', 'tts_voice': 'onyx'}).status_code, 200)
        self.store.migrate(); self.store.migrate()
        self.assertEqual(self.profile()['agent_name'], 'Pedro')
        self.assertEqual(self.post('/products', {'name': 'Produto', 'description': 'D'}).status_code, 201)
        self.assertEqual(self.post('/leads', {'name': 'Ana', 'phone': '+5511999999999'}).status_code, 201)
        with self.assertRaisesRegex(ValueError, 'Agent profile changed'):
            self.store.reserve(1, 1, None, {'agent_name': 'Bia', 'tts_voice': None})
        row = self.store.reserve(1, 1, None, {'agent_name': 'Pedro', 'tts_voice': 'onyx'})
        self.assertEqual((row['agent_name'], row['tts_voice']), ('Pedro', 'onyx'))
