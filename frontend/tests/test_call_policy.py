"""Call policy UI/client checks: synthetic data, no provider calls."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from contextlib import contextmanager

FRONTEND = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(FRONTEND))
from client import APIClient, APIError


class ClientPolicy(unittest.TestCase):
    def test_fixed_authenticated_read_save_and_local_validation(self):
        calls=[]
        api=APIClient('http://synthetic.local','synthetic-key')
        @contextmanager
        def response(request,timeout):
            calls.append(request)
            class Result:
                def read(self): return b'{"max_duration_seconds":180}'
            yield Result()
        api._http.open=response
        self.assertEqual(api.call_policy(),{'max_duration_seconds':180})
        self.assertEqual(api.save_call_policy(180),{'max_duration_seconds':180})
        self.assertEqual([r.method for r in calls],['GET','PATCH'])
        self.assertTrue(all(r.full_url.endswith('/settings/calls') for r in calls))
        self.assertTrue(all(r.get_header('X-api-key')=='synthetic-key' for r in calls))
        self.assertEqual(json.loads(calls[1].data),{'max_duration_seconds':180})
        for value in (None,0,14401,True,1.5,'180'):
            with self.assertRaises(ValueError): api.save_call_policy(value)
        self.assertEqual(len(calls),2)


class FakeAPI:
    def __init__(self):
        self.seconds=180; self.writes=[]; self.read_error=False; self.write_error=False
    def list(self,resource): return []
    def call_policy(self):
        if self.read_error: raise APIError('Synthetic read failure')
        return {'max_duration_seconds':self.seconds}
    def save_call_policy(self,seconds):
        if self.write_error: raise APIError('Synthetic save failure')
        self.writes.append(seconds); self.seconds=seconds
        return {'max_duration_seconds':seconds}


@unittest.skipUnless(importlib.util.find_spec('streamlit'), 'Streamlit image required')
class SettingsUI(unittest.TestCase):
    def setUp(self):
        from streamlit.testing.v1 import AppTest
        import client
        self.api=FakeAPI()
        factory=patch.object(client,'APIClient',lambda *a:self.api)
        factory.start(); self.addCleanup(factory.stop)
        self.app=AppTest.from_file(str(FRONTEND/'app.py'),default_timeout=5).run()
    def show(self):
        self.app.sidebar.radio[0].set_value('Configurações').run()
        self.assertFalse(self.app.exception)
    def test_default_load_and_explicit_save_only(self):
        self.show()
        self.assertEqual(self.app.number_input[0].value,180)
        self.assertEqual(self.api.writes,[])
        self.app.number_input[0].set_value(90).run()
        self.assertEqual(self.api.writes,[])
        self.app.button[0].click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.api.writes,[90])
        self.assertEqual(self.app.number_input[0].value,90)
        self.assertTrue(self.app.success)
    def test_saved_value_is_loaded_without_resetting_to_default(self):
        self.api.seconds=240
        self.show()
        self.assertEqual(self.app.number_input[0].value,240)
        self.assertEqual(self.api.writes,[])
    def test_read_failure_never_exposes_a_default_save_form(self):
        self.api.read_error=True
        self.show()
        self.assertTrue(self.app.error)
        self.assertFalse(self.app.number_input)
        self.assertEqual(self.api.writes,[])
    def test_save_failure_never_reports_success_or_retries(self):
        self.show(); self.api.write_error=True
        self.app.number_input[0].set_value(90)
        self.app.button[0].click().run()
        self.assertFalse(self.app.exception)
        self.assertTrue(self.app.error)
        self.assertFalse(self.app.success)
        self.assertEqual(self.api.writes,[])
        self.assertEqual(self.api.seconds,180)
