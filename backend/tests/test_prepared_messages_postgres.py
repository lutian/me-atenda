"""Prepared messages against real PostgreSQL (cache, pins, cascade)."""
import os
import unittest
import test_prepared_messages as cases
import test_postgres as postgres_cases


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'Disposable PostgreSQL required')
class PreparedMessagesPostgreSQL(cases.PreparedMessages):
    setUp = postgres_cases.PostgreSQL.setUp
    cleanup_schema = postgres_cases.PostgreSQL.cleanup_schema

    def product(self):
        # The PostgreSQL fixture already seeds product 1; give it this suite's description.
        self.assertEqual(self.client.patch('/products/1', json={'description': 'Integração de cartórios'},
                                           headers=self.headers).status_code, 200)

    def test_dial_pins_prepared_pitch_and_goodbye(self):
        self.product()
        self.assertEqual(self.save({'pitch': cases.PITCH, 'goodbye': cases.GOODBYE}).status_code, 200)
        self.store.migrate()
        self.assertEqual(self.post('/leads', {'name': 'Ana', 'phone': '+5511999999999'}).status_code, 201)
        self.assertEqual(self.post('/calls', {'lead_id': 1, 'product_id': 1}).status_code, 201)
        scripts = self.store.opening_scripts(1)
        self.assertEqual(scripts['pitch'][0], cases.PITCH)
        self.assertEqual(scripts['goodbye'][0], cases.GOODBYE)
        self.assertTrue(scripts['pitch'][1])
