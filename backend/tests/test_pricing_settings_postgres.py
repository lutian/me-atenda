"""Pricing settings against real PostgreSQL: stored config, restart and call snapshot."""
import os
import unittest
from fastapi.testclient import TestClient
from app.main import create_app
import test_pricing_settings as cases
import test_postgres as postgres_cases


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'Disposable PostgreSQL required')
class PricingSettingsPostgreSQL(cases.PricingSettings):
    setUp = postgres_cases.PostgreSQL.setUp
    cleanup_schema = postgres_cases.PostgreSQL.cleanup_schema

    def test_save_versions_rates_and_new_calls_use_them(self):
        saved = self.save({'twilio': cases.TWILIO}).json()
        self.store.migrate()  # Reapplying migrations keeps the saved rates.
        restarted = TestClient(create_app(self.settings, self.store, self.provider))
        self.assertEqual(self.pricing(restarted)['twilio'], cases.TWILIO)
        self.assertEqual(restarted.post('/calls', json={'lead_id': 1, 'product_id': 1}, headers=self.headers).status_code, 201)
        with self.store.connection() as db:
            snapshot = db.execute('SELECT pricing_snapshot FROM call_accounting WHERE call_id=1').fetchone()['pricing_snapshot']
        self.assertEqual((snapshot['version'], snapshot['twilio']), (saved['version'], cases.TWILIO))


if __name__ == '__main__': unittest.main()
